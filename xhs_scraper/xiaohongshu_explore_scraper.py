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

import os

XHS_BASE_URL = "https://www.xiaohongshu.com"
XHS_EXPLORE_URL = f"{XHS_BASE_URL}/explore"

# 从环境变量获取搜索词，默认为 ["python有偿"]
_env_keywords = os.environ.get("SEARCH_KEYWORDS")
if _env_keywords:
    # 支持逗号分隔的多个关键词，例如 "python,java,go"
    SEARCH_KEYWORDS: list[str] = [k.strip() for k in _env_keywords.split(",") if k.strip()]
else:
    SEARCH_KEYWORDS: list[str] = ["python有偿"]

HEADFUL: bool = True
SCROLLS: int = 1


def build_search_url(keyword: str, sort: str = "general") -> str:
    # 小红书 web 端常见搜索入口：/search_result?keyword=<kw>&sort=<sort>
    # sort values: general (综合), time_descending (最新), popularity_descending (最热)
    return f"{XHS_BASE_URL}/search_result?keyword={quote(keyword)}&sort={sort}"


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


def _parse_publish_time_from_text(text: str | None) -> str | None:
    if not text:
        return None
    
    # 匹配常见的相对时间或绝对时间格式
    # 1. 相对时间：xx分钟前, xx小时前, 昨天 xx:xx, 前天 xx:xx
    # 2. 绝对时间：yyyy-mm-dd, mm-dd
    
    patterns = [
        r"(\d+分钟前)",
        r"(\d+小时前)",
        r"(昨天\s*\d{1,2}:\d{2})",
        r"(前天\s*\d{1,2}:\d{2})",
        r"(\d{1,2}-\d{1,2})",       # mm-dd
        r"(\d{4}-\d{1,2}-\d{1,2})", # yyyy-mm-dd
        r"(\d+天前)",
    ]
    
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
            
    return None


def switch_to_newest_sort(page: Page) -> None:
    """通过 UI 交互切换到“最新”排序"""
    print("尝试通过 UI 切换到“最新”排序...")
    try:
        # 1. 点击“筛选”按钮
        # 优先点击整个 .filter 容器，通常比点击内部 span 更稳
        filter_container = page.locator(".filter").first
        
        if filter_container.is_visible():
            # 尝试 Hover 触发（有时 hover 就会显示下拉）
            try:
                filter_container.hover()
                page.wait_for_timeout(500)
            except:
                pass
            
            # 强制点击
            filter_container.click(force=True)
            page.wait_for_timeout(1000)
            
            # 如果面板没出来（检测不到“最新”），尝试点击 icon
            if not page.locator(".filter-panel .tags", has_text="最新").first.is_visible():
                print("点击 .filter 未展开，尝试点击图标...")
                icon = page.locator(".filter .filter-icon").first
                if icon.is_visible():
                    icon.click(force=True)
                    page.wait_for_timeout(1500)

            # 2. 点击“最新”
            # 尝试直接找 filter-panel 里的“最新”文本 span
            newest_btn = page.locator(".filter-panel span", has_text="最新").first
            
            if newest_btn.is_visible():
                newest_btn.click(force=True)
                print("已点击“最新”按钮，等待页面刷新...")
                page.wait_for_timeout(3500)
            else:
                print("展开筛选后未找到“最新”按钮，尝试备用选择器...")
                # 备用：查找包含“最新”的 div.tags
                newest_tag = page.locator(".filter-panel .tags", has_text="最新").first
                if newest_tag.is_visible():
                    newest_tag.click(force=True)
                    print("已通过 tags 点击“最新”，等待刷新...")
                    page.wait_for_timeout(3500)
                else:
                    print("无法找到“最新”选项。")
                    try:
                        debug_ss = Path("xhs_scraper/res_docs/debug_filter_failed.png")
                        _ensure_parent(debug_ss)
                        page.screenshot(path=debug_ss)
                        print(f"已保存调试截图: {debug_ss}")
                    except:
                        pass
        else:
            print("未找到“筛选”(.filter) 按钮，跳过 UI 切换。")
            
    except Exception as e:
        print(f"切换排序失败: {e}")


def wait_for_user_login_if_needed(page: Page, timeout_sec: int) -> None:
    """给用户时间手动登录/过验证码。
    
    智能策略：
    1. 检查页面是否有登录弹窗/验证码。
    2. 如果有，等待其消失（用户完成登录）。
    3. 如果没有，直接返回（不浪费时间）。
    """
    if timeout_sec <= 0:
        return

    print("\n[登录检测] 检查是否有登录弹窗...")
    
    # 稍微等一下让弹窗浮现
    page.wait_for_timeout(2000)
    
    login_selectors = [
        ".login-container", 
        ".login-modal", 
        "iframe[src*='login']", 
        "div:has-text('手机号登录')",
        "div:has-text('验证码')",
        "div:has-text('安全验证')"
    ]
    
    is_login_visible = False
    for sel in login_selectors:
        try:
            if page.locator(sel).first.is_visible():
                is_login_visible = True
                print(f"[登录检测] 发现登录/验证元素 ({sel})，暂停脚本等待手动操作...")
                break
        except:
            pass
            
    if not is_login_visible:
        # 再兜底检查一下 body 文本
        try:
            body_text = page.locator("body").inner_text(timeout=1000)
            if "登录后" in body_text or "手机号登录" in body_text:
                is_login_visible = True
                print("[登录检测] 发现页面包含“登录”相关提示，暂停脚本等待手动操作...")
        except:
            pass

    if is_login_visible:
        print(f"请在打开的浏览器中完成登录/验证。最长等待 {timeout_sec} 秒...")
        # 轮询直到弹窗消失或超时
        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            still_visible = False
            # 只要任意一个选择器还可见，就认为还没登录完
            for sel in login_selectors:
                try:
                    if page.locator(sel).first.is_visible():
                        still_visible = True
                        break
                except:
                    pass
            
            if not still_visible:
                # 再次检查 body 文本兜底（可选，这里为了体验流畅先简化）
                print("[登录检测] 登录弹窗似乎已消失，继续执行任务！")
                return
                
            time.sleep(1)
            remaining = int(timeout_sec - (time.time() - start_time))
            if remaining % 5 == 0:
                print(f"  ...剩余等待 {remaining}s")
        
        print("[登录检测] 等待超时，尝试继续执行（可能失败）...")
    else:
        print("[登录检测] 未发现明显阻断弹窗，无需等待。")


def save_cookies(page: Page, out_path: Path) -> None:
    """保存当前 Cookies 到文件，方便下次复用。"""
    try:
        cookies = page.context.cookies()
        if cookies:
            _ensure_parent(out_path)
            out_path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"✅ 已保存最新的 Cookies 到：{out_path} (可提交到仓库供服务器使用)")
    except Exception as e:
        print(f"⚠️ 保存 Cookies 失败: {e}")


def scroll_page(page: Page, scrolls: int, scroll_pause_ms: int) -> None:
    """滚动页面指定次数，每次间隔一定时间。"""
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
    # 策略 1：使用精确的 class 选择器（推荐）
    containers = page.locator("section.note-item")
    
    # 策略 2：如果找不到精确 class，尝试通用标签
    if containers.count() == 0:
        containers = page.locator("article, section")
        
    # 策略 3：最后兜底（风险较高，可能会选中父级容器）
    if containers.count() == 0:
        containers = page.locator("div:has(a[href^='/explore/'])")

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

            # --- 0. 基础数据获取 ---
            publish_time = None
            try:
                # 尝试获取所有文本，用换行符分隔
                raw_text = c.inner_text()
            except Exception as e:
                raw_text = ""

            # --- 1. 纯文本解析逻辑 (最稳健) ---
            if raw_text:
                lines = [ln.strip() for ln in raw_text.split("\n") if ln.strip()]
                
                # 倒序查找关键行
                # 通常结构是：[标题区...] -> [作者] -> [时间/点赞]
                
                # 步骤 A: 找时间行
                time_idx = -1
                for i in range(len(lines) - 1, -1, -1):
                    line = lines[i]
                    # 匹配常见时间格式
                    if re.search(r'(\d+(?:分钟|小时|天)前|昨天|前天|\d{2}-\d{2}|\d{4}-\d{2}-\d{2})', line):
                        publish_time = line
                        time_idx = i
                        break
                
                # 步骤 B: 找点赞行
                if not like_text:
                    for i in range(len(lines) - 1, -1, -1):
                        if i == time_idx: continue
                        line = lines[i]
                        # 简单认为包含数字且很短的非时间行可能是点赞
                        if (re.match(r'^\d+$', line) or line == "赞" or re.match(r'^\d+万$', line)) and len(line) < 10:
                            like_text = line
                            break
                
                # 步骤 C: 找作者
                if time_idx > 0:
                    author_idx = time_idx - 1
                    possible_author = lines[author_idx]
                    if not re.search(r'(\d+(?:分钟|小时|天)前|昨天|前天)', possible_author):
                        author = possible_author
                        # 步骤 D: 找标题
                        if author_idx > 0:
                            title = " ".join(lines[:author_idx])
                elif time_idx == -1 and len(lines) >= 2:
                    # 没找到时间，盲猜倒数第二行是作者
                    author = lines[-2]
                    title = " ".join(lines[:-2])

            # --- 2. 封面图 (仍然尝试 CSS) ---
            try:
                if not img_url:
                    cover_el = c.locator(".cover").first
                    if cover_el.count() > 0:
                        style = cover_el.get_attribute("style") or ""
                        m_bg = re.search(r'url\("?(.+?)"?\)', style)
                        if m_bg:
                            img_url = m_bg.group(1)
            except:
                pass

            # --- 3. 数据清洗 ---
            like_count = _parse_like_count(like_text)
            
            # 尝试从文本中解析发布时间 (如果 CSS 没抓到)
            if not publish_time and raw_text:
                publish_time = _parse_publish_time_from_text(raw_text)

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
                    publish_time=publish_time,
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

                publish_time = _parse_publish_time_from_text(text)

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
                        publish_time=publish_time,
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
    scrolls = SCROLLS
    scroll_pause_ms = 900
    # 默认开启 headful 模式，方便手动登录或观察页面
    # Support environment variable for container deployment
    import os
    headful = os.environ.get("HEADFUL", str(HEADFUL)).lower() == "true"
    # 排序方式：general (综合), time_descending (最新), popularity_descending (最热)
    sort_type = "time_descending"
    
    login_wait_sec = 30
    keep_open = False
    detail_limit = 0
    detail_delay_ms = 1200
    # Use relative path based on script location
    out_json = Path(__file__).parent / "res_docs/xhs_search.json"

    user_data_dir = None

    with sync_playwright() as p:
        browser, page = launch_browser(p, headful=headful, user_data_dir=user_data_dir)
        
        # --- 新增：尝试加载 cookies.json ---
        # 优先从当前目录加载，也可以指定其他路径
        cookies_files = [Path("cookies.json"), Path("res_docs/cookies.json")]
        for cp in cookies_files:
            if cp.exists() and cp.is_file():
                print(f"发现 Cookies 文件：{cp}")
                try:
                    cookies_data = json.loads(cp.read_text(encoding="utf-8"))
                    if isinstance(cookies_data, list):
                        # Playwright 的 add_cookies 需要特定的字段，通常从 EditThisCookie 导出的就够用
                        # 过滤掉不支持的字段（可选，Playwright 通常会忽略多余字段，但 sameSite 需要注意大小写）
                        page.context.add_cookies(cookies_data)
                        print(f"成功注入 {len(cookies_data)} 条 Cookies。")
                        
                        # 注入后刷新页面以生效（如果已经在某个页面）
                        # 但这里还没打开页面，所以无需刷新
                        break
                except Exception as e:
                    print(f"注入 Cookies 失败 ({cp}): {e}")
        # -------------------------------------

        try:
            # 过滤空关键词：
            # - 若有关键词：按关键词抓搜索页
            # - 若全为空：自动回退到 Explore 首页抓取
            keywords = [k.strip() for k in SEARCH_KEYWORDS if k and k.strip()]

            all_cards: list[ExploreCard] = []

            def _safe_scroll_and_extract(
                *, label: str, keyword: str | None
            ) -> list[ExploreCard]:
                accumulated_cards = []
                
                # 1. 初始提取（避免滚动后顶部元素被回收）
                print(f"初始提取（{label}）...")
                accumulated_cards.extend(extract_cards(page, keyword=keyword))

                print(f"开始滚动加载（{label}）：{scrolls} 次")
                try:
                    # 分步滚动并提取
                    for i in range(scrolls):
                        scroll_page(page, scrolls=1, scroll_pause_ms=scroll_pause_ms)
                        # 每滚一次都提取一次，确保不漏
                        # (虽然会有重复，但后面有去重逻辑)
                        # print(f"  滚动 {i+1}/{scrolls} 后提取...") 
                        accumulated_cards.extend(extract_cards(page, keyword=keyword))
                        
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

                return accumulated_cards

            if keywords:
                for kw in keywords:
                    url = build_search_url(kw, sort=sort_type)
                    print(f"打开搜索页（sort={sort_type}）：{url}")
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    # 增加初始等待时间，确保页面元素（如筛选按钮）加载完成
                    page.wait_for_timeout(3000)

                    if headful and login_wait_sec > 0:
                        wait_for_user_login_if_needed(page, login_wait_sec)
                        # 尝试保存 Cookies
                        save_cookies(page, Path("res_docs/cookies.json"))

                    # 如果需要“最新”排序，尝试通过 UI 点击切换
                    # （因为 URL 参数 sort=time_descending 在某些版本/账号下可能无效）
                    if sort_type == "time_descending":
                        switch_to_newest_sort(page)

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
                    save_cookies(page, Path("res_docs/cookies.json"))

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
                # 在关闭浏览器前，再次尝试保存最新的 Cookies
                if page and not page.is_closed():
                    save_cookies(page, Path("res_docs/cookies.json"))
            except Exception as e:
                print(f"退出前保存 Cookies 失败: {e}")
                
            try:
                browser.close()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
