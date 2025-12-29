from playwright.sync_api import sync_playwright
import time
from pathlib import Path
from urllib.parse import quote

def main():
    with sync_playwright() as p:
        user_data_dir = Path(".xhs_profile").expanduser().resolve()
        # 确保目录存在
        user_data_dir.mkdir(parents=True, exist_ok=True)
        print(f"Using persistent context at: {user_data_dir}")
        
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"]  # 尝试规避检测
        )
        
        page = browser.pages[0] if browser.pages else browser.new_page()
        keyword = "python有偿"
        url = f"https://www.xiaohongshu.com/search_result?keyword={quote(keyword)}&sort=time_descending"
        
        print(f"打开页面: {url}")
        page.goto(url)
        page.wait_for_timeout(5000) # 等待加载

        # 尝试切换最新（虽然 url 加了，但为了保险还是手动切一下，这里省略，假设 url 生效或默认就是最新）
        
        print("开始分析卡片结构...")
        cards = page.locator("section.note-item")
        count = cards.count()
        print(f"找到 {count} 个卡片")

        debug_log = []

        for i in range(min(count, 10)):
            card = cards.nth(i)
            html = card.evaluate("el => el.outerHTML")
            text = card.inner_text()
            
            # 尝试提取各个部分
            title_el = card.locator(".title span").first
            has_title = title_el.count() > 0
            title_text = title_el.inner_text() if has_title else "N/A"

            author_el = card.locator(".author-wrapper .name").first
            has_author = author_el.count() > 0
            author_text = author_el.inner_text() if has_author else "N/A"
            
            # 备用作者选择器
            author_alt = card.locator(".user .name").first
            author_alt_text = author_alt.inner_text() if author_alt.count() > 0 else "N/A"

            debug_log.append(f"--- Card {i+1} ---")
            debug_log.append(f"Full Text: {text.replace(chr(10), ' | ')}")
            debug_log.append(f"Selector .title span: {title_text}")
            debug_log.append(f"Selector .author-wrapper .name: {author_text}")
            debug_log.append(f"Selector .user .name: {author_alt_text}")
            debug_log.append(f"HTML Snippet: {html[:500]}...") # 只看前500字符

        print("\n".join(debug_log))
        
        # 将完整 HTML 写入文件以便详细分析
        with open("xhs_scraper/res_docs/card_debug.html", "w", encoding="utf-8") as f:
            f.write("<!-- Debug Output -->\n")
            for i in range(min(count, 10)):
                f.write(f"<!-- Card {i+1} -->\n")
                f.write(cards.nth(i).evaluate("el => el.outerHTML"))
                f.write("\n\n")
        
        print("详细 HTML 已保存到 xhs_scraper/res_docs/card_debug.html")
        browser.close()

if __name__ == "__main__":
    main()
