# xhs_scraper

这个项目用 Playwright **无头模式（headless）** 按“搜索词”抓取小红书搜索结果页（`/search_result`）的“卡片级”公开信息，并输出到 `res_docs/` 目录。

## 环境准备

建议使用项目自带虚拟环境 `.venv/`。

1. 安装依赖

```zsh
cd "/Users/hideonbush./Library/Mobile Documents/com~apple~CloudDocs/test_code/xhs_scraper"
"./.venv/bin/python" -m pip install -r requirements.txt
```

2. 安装 Playwright 浏览器（只需一次）

```zsh
"./.venv/bin/python" -m playwright install chromium
```

## 📮 把结果发到邮箱

新增脚本 `send_xhs_search_email.py`：会把 `res_docs/xhs_search.json` 渲染成高可读邮件（HTML + 纯文本）并发送。

### 1) 配置 .env（你自己填）

把 `.env.example` 复制为 `.env`，然后填写你的邮箱与授权码（**不要提交到 git**，已在 `.gitignore` 忽略）。

### 2) 先本地预览（推荐）

会生成 `res_docs/xhs_search_email_preview.html`，不发邮件。

```zsh
"./.venv/bin/python" send_xhs_search_email.py --dry-run
```

### 3) 发送邮件

```zsh
"./.venv/bin/python" send_xhs_search_email.py
```

## 配置搜索词（直接改脚本）

在 `xiaohongshu_explore_scraper.py` 顶部修改：

- `SEARCH_KEYWORDS = ['口红', '穿搭']`

脚本会依次抓取这些搜索词，并把结果合并写到一个 JSON 文件里。

## 无头模式运行（默认）

脚本默认就是无头模式（不加 `--headful`）。并且默认输出到 `res_docs/`，每次都会覆盖同一个文件。

```zsh
"./.venv/bin/python" xiaohongshu_explore_scraper.py
```

运行后会生成/覆盖：

- `res_docs/xhs_search.json`

## 可选：手动登录/验证码（有头模式）

如果遇到登录/验证码，可以用有头模式打开窗口完成操作：

```zsh
"./.venv/bin/python" xiaohongshu_explore_scraper.py --headful --login-wait-sec 120 --scrolls 10
```

## 调参（在脚本里改）

目前常用参数已简化为“直接在脚本里改变量”，包括：

- `scrolls`、`scroll_pause_ms`
- `headful`、`login_wait_sec`
- `profile_dir`
