#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""小红书：按“小红书号/账号名”定位用户，并抓取其最近 10 条笔记。

功能：
- 在脚本顶部填写两个输入：用户名关键词（XHS_NAME，用于搜索）+ 小红书号（XHS_ID，用于精准匹配）
- 先用 XHS_NAME 搜索所有用户结果，再根据 XHS_ID 在用户卡片信息中精准匹配；若未命中则回退为“包含匹配 + 粉丝最多”
- 进入该用户主页，抓取最近 10 条笔记（链接/标题/点赞等）

说明/限制：
- 不尝试绕过验证码/风控；如遇登录/验证码请使用 --headful 并手动处理
- 页面结构可能变化：选择器采用多策略兜底，但仍可能需要按实际页面微调

输出：
- JSON：res_docs/xhs_user_recent_posts.json（覆盖写入）

用法：
- 方式1（推荐）：在脚本顶部填写 XHS_NAME 与 XHS_ID，然后运行：python xhs_top_user_recent_posts.py
- 有头（推荐第一次登录/验证码）：python xhs_top_user_recent_posts.py --headful --login-wait-sec 120
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from playwright.sync_api import Error, Page, Playwright, sync_playwright


XHS_BASE_URL = "https://www.xiaohongshu.com"
OUT_JSON = Path("res_docs/xhs_user_recent_posts.json").resolve()

# 输入1：用户名关键词（用于搜索“用户”列表）
# 例如："易烊千玺"
XHS_NAME: str = "易烊千玺"

# 输入2：小红书号（用于在搜索结果中精准匹配用户）
# 例如："918365379"
XHS_ID: str = "918365379"


@dataclass
class UserHit:
    query: str
    username: str | None = None
    profile_url: str | None = None
    fans_text: str | None = None
    fans_count: int | None = None
    matched_by: str | None = None
    raw_text: str | None = None


@dataclass
class UserPost:
    query: str
    username: str | None = None
    profile_url: str | None = None
    post_url: str | None = None
    title: str | None = None
    like_text: str | None = None
    like_count: int | None = None
    raw_text: str | None = None
    error: str | None = None


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _normalize_url(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href:
        return None
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return XHS_BASE_URL + href
    return href


def _strip_tracking_params(url: str | None) -> str | None:
    if not url:
        return None
    return url.split("?", 1)[0]


def _parse_cn_number(s: str | None) -> int | None:
    """解析常见中文数量：1.2万 / 356 / 1千 / 10+ 等。"""

    if not s:
        return None
    t = s.strip()
    if not t:
        return None

    # 去掉可能的“粉丝/关注/获赞”字样，只保留数值+单位
    t = re.sub(r"粉丝|关注|获赞|赞|人|\+", "", t)
    t = t.strip()

    m = re.search(r"(\d+(?:\.\d+)?)(万|千)?", t)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "万":
        return int(num * 10_000)
    if unit == "千":
        return int(num * 1_000)
    return int(num)


def _looks_like_profile_url(url: str | None) -> bool:
    if not url:
        return False
    # 小红书 web 端用户主页常见：/user/profile/<id>
    if "/user/profile/" in url:
        return True
    return False


def _looks_like_note_url(url: str | None) -> bool:
    if not url:
        return False
    url = _strip_tracking_params(url)
    return bool(url and re.search(r"/explore/[0-9a-fA-F]{10,}", url))


def _safe_inner_text(locator, timeout_ms: int = 800) -> str:
    try:
        return (locator.inner_text(timeout=timeout_ms) or "").strip()
    except Exception:
        return ""


def _extract_like_from_card_text(card_text: str) -> tuple[str | None, int | None]:
    """从用户主页列表页卡片文本里提取点赞数（尽量）。

    说明：小红书卡片上的数值可能是“赞/点赞”，也可能只显示一个数字。
    这里做启发式解析：优先匹配带“赞”字样，否则取最后一个像数字的 token。
    """

    if not card_text:
        return None, None
    t = card_text.strip()
    if not t:
        return None, None

    m = re.search(r"(\d+(?:\.\d+)?\s*(?:万|千)?)\s*赞", t)
    if m:
        like_text = m.group(1).strip()
        return like_text, _parse_cn_number(like_text)

    nums = re.findall(r"\b(\d+(?:\.\d+)?\s*(?:万|千)?)\b", t)
    if nums:
        like_text = nums[-1].strip()
        return like_text, _parse_cn_number(like_text)
    return None, None


def _first_non_empty_text(
    page: Page, selectors: list[str], timeout_ms: int = 800
) -> str:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() <= 0:
                continue
            t = _safe_inner_text(loc, timeout_ms=timeout_ms)
            if t:
                return t
        except Exception:
            continue
    return ""


def extract_note_detail(
    page: Page, post_url: str, timeout_ms: int = 60_000
) -> dict[str, Any]:
    """进入笔记详情页，尽可能提取标题与互动信息。

    返回字段：title, like_text, like_count, raw_text

    注意：当前脚本默认不跳转详情页；此函数保留做未来兜底/调试。
    """

    page.goto(post_url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(900)

    # 标题：常见为 h1 / data-testid / meta 等
    title = _first_non_empty_text(
        page,
        selectors=[
            "h1",
            "[data-testid*='note-title']",
            "[class*='title']",
        ],
    )

    if not title:
        # 兜底：从 <title> 或 og:title
        try:
            og = page.locator("meta[property='og:title']").first
            if og.count() > 0:
                t = (og.get_attribute("content") or "").strip()
                if t:
                    title = t
        except Exception:
            pass
        if not title:
            try:
                t = page.title().strip()
                if t:
                    title = t
            except Exception:
                pass

    # 互动区文本（点赞/收藏/评论等）页面结构变化很大：做多策略兜底
    # 先抓整页可见文本作为 raw_text（截断以免太大）
    raw = ""
    try:
        raw = (page.locator("body").inner_text(timeout=1500) or "").strip()
    except Exception:
        raw = ""
    raw_text = raw[:2000] if raw else None

    # 从 raw_text 尝试解析“赞/点赞”数量
    like_text = None
    like_count = None
    if raw:
        # 常见："xxx 赞" / "点赞 xxx" / "赞 xxx"，只取第一个命中
        patterns = [
            r"(\d+(?:\.\d+)?\s*(?:万|千)?)\s*赞",
            r"点赞\s*(\d+(?:\.\d+)?\s*(?:万|千)?)",
            r"赞\s*(\d+(?:\.\d+)?\s*(?:万|千)?)",
        ]
        for pat in patterns:
            m = re.search(pat, raw)
            if m:
                like_text = m.group(1).strip()
                like_count = _parse_cn_number(like_text)
                break

    # 如果 raw 解析不到，再尝试从按钮/图标附近找数字
    if like_text is None:
        candidates = [
            "text=/赞\\s*\\d+|\\d+\\s*赞/",
            "[class*='like']",
            "[data-testid*='like']",
        ]
        t = _first_non_empty_text(page, candidates, timeout_ms=800)
        if t:
            m = re.search(r"(\d+(?:\.\d+)?\s*(?:万|千)?)", t)
            if m:
                like_text = m.group(1).strip()
                like_count = _parse_cn_number(like_text)

    return {
        "title": title or None,
        "like_text": like_text,
        "like_count": like_count,
        "raw_text": raw_text,
    }


def wait_for_user_login_if_needed(page: Page, timeout_sec: int) -> None:
    if timeout_sec <= 0:
        return
    print(
        "\n如果页面提示登录/验证码，请在打开的浏览器窗口中手动完成。"
        f"\n我会等待 {timeout_sec} 秒，然后继续...\n"
    )
    page.wait_for_timeout(timeout_sec * 1000)


def build_search_url(query: str) -> str:
    return f"{XHS_BASE_URL}/search_result?keyword={quote(query)}"


def goto_user_tab(page: Page) -> None:
    """尽量切到搜索结果页的“用户”tab。"""

    # 策略1：点击包含“用户”的 tab
    candidates = [
        "text=用户",
        "role=tab[name='用户']",
        "[data-testid*='user']",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=1200)
                page.wait_for_timeout(800)
                return
        except Exception:
            continue

    # 策略2：如果页面上有“用户”字样但无法点击，就不强求


def scroll_page(page: Page, scrolls: int, pause_ms: int) -> None:
    for _ in range(scrolls):
        page.mouse.wheel(0, 2400)
        page.wait_for_timeout(pause_ms)


def extract_user_hits(page: Page, query: str, limit: int = 30) -> list[UserHit]:
    hits: list[UserHit] = []

    # 用户卡片通常包含指向 /user/profile/ 的链接
    anchors = page.locator("a[href*='/user/profile/']")
    count = min(anchors.count(), 300)

    for i in range(count):
        a = anchors.nth(i)
        try:
            href = _normalize_url(a.get_attribute("href"))
            if not _looks_like_profile_url(href):
                continue

            # 尝试在 a 附近拿到卡片文本
            try:
                card = a.locator("xpath=ancestor::div[1]")
                text = card.inner_text(timeout=800)
            except Exception:
                text = a.inner_text(timeout=800)

            text = text.strip() if text else ""

            # 粉丝数：从文本里找“粉丝”附近数值
            fans_text = None
            m = re.search(r"(\d+(?:\.\d+)?\s*(?:万|千)?)[^\n]{0,6}粉丝", text)
            if m:
                fans_text = m.group(1)
            else:
                # 兜底：找一个像“1.2万”且附近包含“粉丝”
                m2 = re.search(r"粉丝\s*(\d+(?:\.\d+)?\s*(?:万|千)?)", text)
                if m2:
                    fans_text = m2.group(1)

            fans_count = _parse_cn_number(fans_text)

            # 用户名：文本第一行通常是用户名
            username = None
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            if lines:
                username = lines[0][:50]

            hits.append(
                UserHit(
                    query=query,
                    username=username,
                    profile_url=_strip_tracking_params(href),
                    fans_text=fans_text,
                    fans_count=fans_count,
                    matched_by=None,
                    raw_text=text or None,
                )
            )

            if len(hits) >= limit:
                break
        except Exception:
            continue

    # 去重（按 profile_url）
    uniq: dict[str, UserHit] = {}
    for h in hits:
        if h.profile_url and h.profile_url not in uniq:
            uniq[h.profile_url] = h
    return list(uniq.values())


def pick_top_fans_user(hits: list[UserHit]) -> Optional[UserHit]:
    if not hits:
        return None
    return sorted(
        hits, key=lambda x: (x.fans_count or -1, x.username or ""), reverse=True
    )[0]


def pick_user_by_xhs_id(
    hits: list[UserHit], xhs_id: str
) -> tuple[Optional[UserHit], str]:
    """按 xhs_id 从候选用户中挑选。

    返回： (user, matched_by)
    matched_by: exact | contains | top_fans | none
    """

    xhs_id_norm = (xhs_id or "").strip().lower()
    if not xhs_id_norm:
        return None, "none"

    # 1) 优先匹配：用户卡片里出现“小红书号/ID/小红书ID”等字段
    # 说明：小红书 web 端不同区域展示文字不完全一致，这里用多正则兜底。
    id_patterns = [
        re.compile(r"小红书号\s*[:：]?\s*" + re.escape(xhs_id_norm) + r"\b", re.I),
        re.compile(r"小红书\s*id\s*[:：]?\s*" + re.escape(xhs_id_norm) + r"\b", re.I),
        re.compile(r"\bid\s*[:：]?\s*" + re.escape(xhs_id_norm) + r"\b", re.I),
    ]
    for h in hits:
        t = (h.raw_text or "").strip().lower()
        if not t:
            continue
        if any(p.search(t) for p in id_patterns):
            h.matched_by = "xhs_id"
            return h, "xhs_id"

    # 2) 回退：精确匹配 username（有时用户会把小红书号当作 username 来填）
    for h in hits:
        if (h.username or "").strip().lower() == xhs_id_norm:
            h.matched_by = "exact"
            return h, "exact"

    # 3) 包含匹配（不区分大小写）
    contains = [h for h in hits if xhs_id_norm in ((h.username or "").strip().lower())]
    if contains:
        top = pick_top_fans_user(contains)
        if top:
            top.matched_by = "contains"
        return top, "contains"

    # 4) 找不到：回退粉丝最多
    top = pick_top_fans_user(hits)
    if top:
        top.matched_by = "top_fans"
        return top, "top_fans"
    return None, "none"


def extract_recent_posts(
    page: Page, query: str, user: UserHit, n: int = 10
) -> list[UserPost]:
    posts: list[UserPost] = []

    if not user.profile_url:
        return posts

    page.goto(user.profile_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(1200)

    # 主页里通常会出现 /explore/<id> 的笔记链接。
    # 需要边滚动边追加解析，直到拿够 n 条。
    processed: set[str] = set()  # 按 post_url 去重
    max_rounds = 18

    def process_visible_cards() -> None:
        anchors = page.locator("a[href^='/explore/']")
        count = min(anchors.count(), 600)
        for i in range(count):
            a = anchors.nth(i)
            try:
                href = _normalize_url(a.get_attribute("href"))
                href = _strip_tracking_params(href)
                if not _looks_like_note_url(href):
                    continue
                if href in processed:
                    continue

                # 不跳转：直接从“主页列表页”的卡片容器提取
                card_text = ""
                try:
                    card = a.locator(
                        "xpath=ancestor::*[self::section or self::article or self::div][1]"
                    )
                    card_text = _safe_inner_text(card, timeout_ms=1200)
                    if len(card_text) < 10:
                        card2 = a.locator("xpath=ancestor::div[2]")
                        card_text = (
                            _safe_inner_text(card2, timeout_ms=1200) or card_text
                        )
                except Exception:
                    card_text = ""

                title = None
                if card_text:
                    lines = [ln.strip() for ln in card_text.split("\n") if ln.strip()]
                    if lines:
                        title = lines[0][:120]
                        if title == (user.username or "") and len(lines) >= 2:
                            title = lines[1][:120]

                like_text, like_count = _extract_like_from_card_text(card_text)

                posts.append(
                    UserPost(
                        query=query,
                        username=user.username,
                        profile_url=user.profile_url,
                        post_url=href,
                        title=title,
                        like_text=like_text,
                        like_count=like_count,
                        raw_text=(card_text[:1200] if card_text else None),
                        error=None,
                    )
                )
                processed.add(href)

                if len(posts) >= n:
                    return
            except Exception:
                continue

    for _ in range(max_rounds):
        process_visible_cards()
        if len(posts) >= n:
            break
        page.mouse.wheel(0, 2600)
        page.wait_for_timeout(900)

    return posts[:n]


def save_json(payload: dict[str, Any], out_path: Path) -> Path:
    _ensure_parent(out_path)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_path


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="小红书：按小红书号/账号名定位用户并抓取最近10条笔记"
    )
    ap.add_argument(
        "--name", default="", help="用户名关键词（不传则用脚本内 XHS_NAME）"
    )
    ap.add_argument("--xhs-id", default="", help="小红书号（不传则用脚本内 XHS_ID）")
    ap.add_argument(
        "--headful", action="store_true", help="显示浏览器窗口（用于手动登录/验证码）"
    )
    ap.add_argument(
        "--profile-dir", default=None, help="复用登录态的浏览器用户数据目录"
    )
    ap.add_argument(
        "--login-wait-sec", type=int, default=35, help="有头模式下等待登录/验证码的秒数"
    )
    ap.add_argument("--scrolls", type=int, default=8, help="搜索页滚动次数")
    ap.add_argument("--scroll-pause-ms", type=int, default=900, help="搜索页滚动间隔")
    ap.add_argument("--max-users", type=int, default=30, help="最多解析多少个用户候选")
    ap.add_argument("--posts", type=int, default=10, help="抓取最近多少条笔记")
    ap.add_argument("--out", default=str(OUT_JSON), help="输出 JSON 路径（覆盖写入）")

    args = ap.parse_args(argv)

    name = (args.name or XHS_NAME).strip()
    xhs_id = (args.xhs_id or XHS_ID).strip()
    if not name or not xhs_id:
        raise SystemExit(
            "请提供两个输入：用户名关键词（用于搜索）+ 小红书号（用于精准匹配）。\n"
            "1) 在脚本顶部填写：XHS_NAME = 'xxx' 与 XHS_ID = 'xxxxxx'\n"
            "2) 或命令行传参：--name 'xxx' --xhs-id 'xxxxxx'\n"
        )

    out_path = Path(args.out).expanduser().resolve()

    with sync_playwright() as p:
        if args.profile_dir:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(Path(args.profile_dir).expanduser().resolve()),
                headless=not args.headful,
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = p.chromium.launch(
                headless=not args.headful,
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
            page = context.new_page()
            
            # 尝试加载 cookies.json
            cookies_files = [Path("cookies.json"), Path("res_docs/cookies.json")]
            for cp in cookies_files:
                if cp.exists() and cp.is_file():
                    print(f"发现 Cookies 文件：{cp}")
                    try:
                        cookies_data = json.loads(cp.read_text(encoding="utf-8"))
                        if isinstance(cookies_data, list):
                            context.add_cookies(cookies_data)
                            print(f"成功注入 {len(cookies_data)} 条 Cookies。")
                            break
                    except Exception as e:
                        print(f"注入 Cookies 失败 ({cp}): {e}")

        try:
            # 先用用户名关键词搜索用户列表
            url = build_search_url(name)
            print(f"打开搜索页：{url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1200)

            if args.headful and args.login_wait_sec > 0:
                wait_for_user_login_if_needed(page, args.login_wait_sec)

            # 切到“用户”tab
            goto_user_tab(page)
            page.wait_for_timeout(900)

            # 滚动加载
            scroll_page(page, args.scrolls, args.scroll_pause_ms)

            hits = extract_user_hits(page, query=name, limit=args.max_users)
            selected, matched_by = pick_user_by_xhs_id(hits, xhs_id=xhs_id)
            if not selected:
                payload = {
                    "name": name,
                    "xhs_id": xhs_id,
                    "error": "未找到用户结果",
                    "users": [asdict(h) for h in hits],
                    "posts": [],
                }
                save_json(payload, out_path)
                print(f"未找到用户，已输出：{out_path}")
                return 2

            print(
                f"选择用户：{selected.username or ''} fans={selected.fans_count} url={selected.profile_url} matched_by={matched_by}"
            )

            posts = extract_recent_posts(page, query=name, user=selected, n=args.posts)

            payload = {
                "name": name,
                "xhs_id": xhs_id,
                "matched_by": matched_by,
                "selected_user": asdict(selected),
                "users_top10": [
                    asdict(h)
                    for h in sorted(
                        hits, key=lambda x: (x.fans_count or -1), reverse=True
                    )[:10]
                ],
                "posts": [asdict(p) for p in posts],
            }

            save_json(payload, out_path)
            print(f"完成：user={selected.username} posts={len(posts)} out={out_path}")
            return 0

        except Error as e:
            print(f"Playwright 错误：{e}")
            return 2
        finally:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
