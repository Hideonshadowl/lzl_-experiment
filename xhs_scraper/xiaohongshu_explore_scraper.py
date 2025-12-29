#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""小红书 搜索结果抓取（真实浏览器自动化）

目标：按指定搜索词，从 https://www.xiaohongshu.com/search_result 页面抓取“卡片级”公开信息。

特点/限制：
- 该脚本不尝试绕过验证码/风控；如遇登录或验证码，请在 headful 模式下手动完成。
- 页面结构经常变化；本脚本使用多策略提取，尽量提高鲁棒性。

输出：
- JSON（原始列表；默认覆盖写入 res_docs/xhs_search.json）

用法：
    1) 在脚本顶部 SEARCH_KEYWORDS 填入搜索词
    2) 运行：python xiaohongshu_explore_scraper.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from urllib.parse import quote
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from playwright.sync_api import Browser, Error, Page, Playwright, sync_playwright

XHS_BASE_URL = "https://www.xiaohongshu.com"
XHS_EXPLORE_URL = f"{XHS_BASE_URL}/explore"

# 直接在这里填写你要抓取的搜索词（可写多个，会依次抓取并汇总到同一个输出文件里）
SEARCH_KEYWORDS: list[str] = [
    # 示例："武功山旅游攻略",
]


def build_search_url(keyword: str) -> str:
    # 小红书 web 端常见搜索入口：/search_result?keyword=<kw>
    return f"{XHS_BASE_URL}/search_result?keyword={quote(keyword)}"


@dataclass
class ExploreCard:
    keyword: str | None = None
    url: str | None = None
    title: str | None = None
    content: str | None = None
    author: str | None = None
    cover_url: str | None = None
    like_text: str | None = None
    like_count: int | None = None
    publish_time: str | None = None
    raw_text: str | None = None


def _now_ts() -> int:
    return int(time.time())


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _normalize_url(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href:
        return None
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://www.xiaohongshu.com" + href
    return href


def _strip_tracking_params(url: str | None) -> str | None:
    """去掉常见追踪参数，避免一条笔记出现多条不同 url 导致去重失败。"""

    if not url:
        return None
    # 仅做保守处理：去掉 ? 及之后
    return url.split("?", 1)[0]


def _dedupe_keep_order(items: Iterable[ExploreCard]) -> list[ExploreCard]:
    seen: set[str] = set()
    out: list[ExploreCard] = []
    for it in items:
        it.url = _strip_tracking_params(it.url)
        key = it.url or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(it)
    return out


def _first_non_empty(*values: str | None) -> str | None:
    for v in values:
        if v and v.strip():
            return v.strip()
    return None


def enrich_cards_from_detail_pages(
    page: Page,
    cards: list[ExploreCard],
    limit: int,
    delay_ms: int,
) -> list[ExploreCard]:
    """逐条打开详情页，补齐正文与发布时间。

    说明：
    - 这个过程更容易触发风控，所以默认只补齐前 N 条，并做低频延迟。
    - 若页面结构变化，这里的选择器可能需要调整。
    """

    target = cards[: max(0, limit)] if limit > 0 else []
    if not target:
        return cards

    print(f"开始补齐详情页字段：{len(target)} 条（delay={delay_ms}ms）")

    for idx, card in enumerate(target, start=1):
        if not card.url:
            continue
        try:
            page.goto(card.url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(800)

            # 正文内容：多策略
            content = None
            try:
                # 常见：meta description
                content = page.locator('meta[name="description"]').get_attribute(
                    "content"
                )
            except Exception:
                content = None

            if not content:
                try:
                    # 兜底：拿 body 文本中较像正文的段落（可能包含其它信息）
                    body_text = page.locator("body").inner_text(timeout=3000)
                    # 取前 800 字做摘要式正文
                    content = (
                        body_text.strip().split("\n", 1)[0][:800] if body_text else None
                    )
                except Exception:
                    content = None

            # 发布时间：多策略
            publish_time = None
            try:
                publish_time = page.locator(
                    'meta[property="og:updated_time"]'
                ).get_attribute("content")
            except Exception:
                publish_time = None

            if not publish_time:
                # 某些页面会在文本里出现类似“编辑于 xxxx-xx-xx”
                try:
                    t = page.locator("body").inner_text(timeout=3000)
                    m = re.search(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})", t or "")
                    if m:
                        publish_time = m.group(1)
                except Exception:
                    publish_time = None

            card.content = _first_non_empty(card.content, content)
            card.publish_time = _first_non_empty(card.publish_time, publish_time)

            # 详情页里点赞数有时更准：如果能找到数字就覆盖 like_count
            try:
                t = page.locator("body").inner_text(timeout=3000)
                # 很粗的抓取：出现“赞/点赞”附近的数字
                m2 = re.search(r"(\d+(?:\.\d+)?万?)\s*(?:赞|点赞)", t or "")
                if m2:
                    lc = _parse_like_count(m2.group(1))
                    if lc is not None:
                        card.like_count = card.like_count or lc
            except Exception:
                pass

            print(f"  [{idx}/{len(target)}] ✓ {card.url}")

        except Exception as e:
            print(f"  [{idx}/{len(target)}] ✗ {card.url} -> {type(e).__name__}: {e}")

        page.wait_for_timeout(max(0, delay_ms))

    return cards


def _looks_like_note_url(url: str | None) -> bool:
    if not url:
        return False
    # 列表页会混入频道/导航链接，这里只保留像“笔记详情”的链接
    if url.startswith(f"{XHS_BASE_URL}/explore?"):
        return False
    return bool(re.search(r"/explore/[0-9a-fA-F]{10,}", url))


def _parse_like_count(like_text: str | None) -> int | None:
    if not like_text:
        return None
    s = like_text.strip()
    if not s:
        return None

    # 常见格式："1.2万"、"356"、"1万+"、"1千+"、"10+" 等
    m = re.search(r"(\d+(?:\.\d+)?)(万|千)?", s)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "万":
        return int(num * 10_000)
    if unit == "千":
        return int(num * 1_000)
    return int(num)


def wait_for_user_login_if_needed(page: Page, timeout_sec: int) -> None:
    """给用户时间手动登录/过验证码。

    这个函数不会尝试任何绕过，只会：
    - 打印提示
    - 等待若干秒，让你在浏览器里完成动作
    """

    if timeout_sec <= 0:
        return

    print(
        "\n如果页面提示登录/验证码，请在打开的浏览器窗口中手动完成。"
        f"\n我会等待 {timeout_sec} 秒，然后继续滚动采集...\n"
    )

    # 简单判断：如果页面包含明显的登录字样，给出更强提示
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        if any(k in body_text for k in ["登录", "手机号", "验证码"]):
            print("检测到疑似登录界面/提示，请先完成登录再等待结束。\n")
    except Exception:
        pass

    for remaining in range(timeout_sec, 0, -1):
        if remaining % 5 == 0 or remaining <= 3:
            print(f"  ...{remaining}s")
        time.sleep(1)


def scroll_page(page: Page, scrolls: int, scroll_pause_ms: int) -> None:
    for i in range(scrolls):
        if page.is_closed():
            raise RuntimeError("页面已关闭，无法继续滚动。")
        page.mouse.wheel(0, 2400)
        page.wait_for_timeout(scroll_pause_ms)
        if (i + 1) % 3 == 0:
            # 偶尔等一下，让懒加载更充分
            page.wait_for_timeout(int(scroll_pause_ms * 1.5))


def extract_cards(page: Page, keyword: str | None = None) -> list[ExploreCard]:
    """从页面里尽量提取卡片信息。

    由于 DOM 经常变，这里采取：
    1) 以 a[href] 为入口，抓取可能的笔记链接
    2) 在链接附近找图片、作者、标题等
    """

    cards: list[ExploreCard] = []

    # 优先按“卡片容器”提取（通常比直接扫 a 更稳）
    # 这些选择器不保证永远存在，但能覆盖一部分常见结构
    containers = page.locator("article, section, div:has(a[href^='/explore/'])")

    container_count = containers.count()
    for i in range(min(container_count, 300)):
        c = containers.nth(i)
        try:
            a = c.locator("a[href^='/explore/']").first
            if a.count() == 0:
                continue
            href = _normalize_url(a.get_attribute("href"))
            if not _looks_like_note_url(href):
                continue

            img_url = None
            try:
                img = c.locator("img").first
                if img.count() > 0:
                    img_url = img.get_attribute("src") or img.get_attribute("data-src")
            except Exception:
                img_url = None
            if img_url and img_url.startswith("data:image"):
                img_url = None

            # 从 container 内找“像标题/作者/点赞”的文本
            title = None
            author = None
            like_text = None

            try:
                # 通常标题会比较显眼，先抓 container 的可见文本
                text = c.inner_text(timeout=800)
                text = text.strip() if text else None
            except Exception:
                text = None

            # 过滤：登录页/扫码页/页脚/导航等大块文本（这些不是笔记卡片）
            if text and any(
                k in text
                for k in [
                    "手机号登录",
                    "扫码",
                    "登录后推荐",
                    "用户协议",
                    "隐私政策",
                    "重新发送",
                    "沪ICP备",
                    "行吟信息科技",
                    "增值电信业务",
                    "创作中心",
                    "业务合作",
                    "个性化推荐算法",
                ]
            ):
                continue

            if text:
                lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
                # 标题：取一个较长但不夸张的行（避免把作者/点赞当标题）
                for ln in lines[:8]:
                    if 4 <= len(ln) <= 80 and not re.search(r"\d+\s*(?:赞|点赞)$", ln):
                        title = title or ln
                        break

                # 点赞：包含数字且往往较短
                for ln in lines[:12]:
                    if re.search(r"\d", ln) and len(ln) <= 20:
                        like_text = like_text or ln

                # 作者：取一个短文本且不含数字
                for ln in lines[:12]:
                    if 1 <= len(ln) <= 20 and not re.search(r"\d", ln) and ln != title:
                        author = author or ln

            like_count = _parse_like_count(like_text)

            cards.append(
                ExploreCard(
                    keyword=keyword,
                    url=href,
                    title=title,
                    content=None,
                    author=author,
                    cover_url=img_url,
                    like_text=like_text,
                    like_count=like_count,
                    publish_time=None,
                    raw_text=text,
                )
            )
        except Exception:
            continue

    # 兜底：如果容器策略抓得太少，再退回到扫 a 的方式
    if len(cards) < 8:
        anchors = page.locator('a[href^="/explore/"]')
        count = anchors.count()
        for i in range(min(count, 500)):
            a = anchors.nth(i)
            try:
                href = _normalize_url(a.get_attribute("href"))
                if not _looks_like_note_url(href):
                    continue
                text = None
                try:
                    text = a.inner_text(timeout=500).strip() or None
                except Exception:
                    text = None

                cards.append(
                    ExploreCard(
                        keyword=keyword,
                        url=href,
                        title=text,
                        content=None,
                        author=None,
                        cover_url=None,
                        like_text=None,
                        like_count=None,
                        publish_time=None,
                        raw_text=text,
                    )
                )
            except Exception:
                continue

    return _dedupe_keep_order(cards)


def save_outputs(cards: list[ExploreCard], out_json: Path) -> tuple[Path, Path]:
    _ensure_parent(out_json)

    payload: list[dict[str, Any]] = [asdict(c) for c in cards]

    out_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 按需求：仅输出 JSON（不生成 CSV）
    return out_json, out_json


def launch_browser(
    playwright: Playwright, headful: bool, user_data_dir: Path | None
) -> tuple[Browser, Page]:
    """启动浏览器。

    - 如果提供 user_data_dir：使用持久化上下文，方便复用登录态。
    - 否则：普通临时上下文。
    """

    if user_data_dir:
        user_data_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=not headful,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        page = context.pages[0] if context.pages else context.new_page()
        # 这里返回一个“类 Browser”的对象不太直观，所以统一返回 context.browser
        return context.browser, page

    browser = playwright.chromium.launch(
        headless=not headful,
    )
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
    )
    page = context.new_page()
    return browser, page


def main(argv: list[str]) -> int:
    # 依然保留部分可调参数（不含搜索词；搜索词直接在 SEARCH_KEYWORDS 里改）
    scrolls = 10
    scroll_pause_ms = 900
    headful = False
    profile_dir = ".xhs_profile"
    login_wait_sec = 35
    keep_open = False
    detail_limit = 0
    detail_delay_ms = 1200
    out_json = Path("res_docs/xhs_search.json").expanduser().resolve()

    user_data_dir = None
    if isinstance(profile_dir, str) and profile_dir.strip():
        user_data_dir = Path(profile_dir).expanduser().resolve()

    with sync_playwright() as p:
        browser, page = launch_browser(p, headful=headful, user_data_dir=user_data_dir)
        try:
            # 过滤空关键词：
            # - 若有关键词：按关键词抓搜索页
            # - 若全为空：自动回退到 Explore 首页抓取
            keywords = [k.strip() for k in SEARCH_KEYWORDS if k and k.strip()]

            all_cards: list[ExploreCard] = []

            def _safe_scroll_and_extract(
                *, label: str, keyword: str | None
            ) -> list[ExploreCard]:
                print(f"开始滚动加载（{label}）：{scrolls} 次")
                try:
                    scroll_page(page, scrolls=scrolls, scroll_pause_ms=scroll_pause_ms)
                except Error as e:
                    if "TargetClosedError" in str(e) or "has been closed" in str(e):
                        print(
                            "\n检测到浏览器窗口被关闭（或页面崩溃），已停止采集。\n"
                            "建议：\n"
                            "- 运行期间不要手动关闭浏览器窗口\n"
                            "- 如果只是想让窗口停久一点，临时把 keep_open=True\n"
                            "- 需要更多登录时间，临时把 login_wait_sec=180\n"
                        )
                        raise SystemExit(2)
                    raise

                print(f"开始解析卡片（{label}）...")
                cards_local = extract_cards(page, keyword=keyword)
                if len(cards_local) < 8:
                    page.wait_for_timeout(1200)
                    cards_local = extract_cards(page, keyword=keyword)

                if detail_limit > 0:
                    cards_local = enrich_cards_from_detail_pages(
                        page,
                        cards_local,
                        limit=detail_limit,
                        delay_ms=detail_delay_ms,
                    )
                return cards_local

            if keywords:
                for kw in keywords:
                    url = build_search_url(kw)
                    print(f"打开搜索页：{url}")
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(1200)

                    if headful and login_wait_sec > 0:
                        wait_for_user_login_if_needed(page, login_wait_sec)

                    if page.is_closed():
                        print(
                            "\n检测到浏览器页面已被关闭，所以无法继续采集。\n"
                            "请保持窗口打开，重新运行脚本即可。"
                        )
                        return 2

                    all_cards.extend(
                        _safe_scroll_and_extract(label=f"keyword={kw}", keyword=kw)
                    )
            else:
                # Explore 模式
                print(
                    "未配置搜索词（SEARCH_KEYWORDS 为空），自动切换到 Explore 首页抓取："
                    f"{XHS_EXPLORE_URL}"
                )
                page.goto(
                    XHS_EXPLORE_URL, wait_until="domcontentloaded", timeout=60_000
                )
                page.wait_for_timeout(1200)

                if headful and login_wait_sec > 0:
                    wait_for_user_login_if_needed(page, login_wait_sec)

                if page.is_closed():
                    print(
                        "\n检测到浏览器页面已被关闭，所以无法继续采集。\n"
                        "请保持窗口打开，重新运行脚本即可。"
                    )
                    return 2

                all_cards.extend(
                    _safe_scroll_and_extract(label="explore", keyword=None)
                )

            all_cards = _dedupe_keep_order(all_cards)
            out_json_path, _ = save_outputs(all_cards, out_json)

            print("\n完成：")
            print(f"- 搜索词数量：{len(keywords)}（为 0 表示 explore 模式）")
            print(f"- 卡片数量：{len(all_cards)}")
            print(f"- JSON：{out_json_path}")

            # 简单提示：如果几乎为 0，说明被拦或 DOM 变了
            if len(all_cards) == 0:
                print(
                    "\n提示：结果为 0，可能原因：\n"
                    "- 页面要求登录/触发验证码（建议使用 --headful 并手动完成）\n"
                    "- 页面 DOM 更新导致选择器失效（我可以帮你根据当前页面更新提取逻辑）\n"
                )

            if headful and keep_open:
                print(
                    "\n已开启 --keep-open：浏览器将保持打开。\n完成所有操作后，在此终端按回车退出并关闭浏览器..."
                )
                try:
                    input()
                except KeyboardInterrupt:
                    pass

        finally:
            try:
                browser.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
