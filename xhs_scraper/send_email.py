import smtplib
import json
import ssl
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from pathlib import Path

def send_email():
    # Configuration
    sender_email = "17751038289@163.com"
    receiver_email = "17751038289@163.com"
    password = "ZASqLxTenaxpRsWt"  # SMTP authorization code
    smtp_server = "smtp.163.com"
    smtp_port = 465

    # File path
    # Use relative path compatible with both local and container environments
    # Assuming the script is run from the xhs_scraper directory or the project root is mapped correctly
    json_path = Path(__file__).parent / "res_docs/xhs_search.json"

    # Read and parse JSON
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error reading JSON file: {e}")
        return

    # Create root message (related type for inline images)
    message = MIMEMultipart("related")
    message["Subject"] = f"搜索结果汇总 - {len(data)}条"
    message["From"] = sender_email
    message["To"] = receiver_email

    # Create alternative part for text/html
    msg_alternative = MIMEMultipart("alternative")
    message.attach(msg_alternative)

    # HTML Header
    html_content = """
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f5f5f5; }
            .container { max-width: 600px; margin: 0 auto; background-color: #ffffff; padding: 15px; }
            .card { border-bottom: 1px solid #eeeeee; padding: 15px 0; }
            .card-table { width: 100%; border-collapse: collapse; }
            .img-cell { width: 110px; vertical-align: top; }
            .content-cell { vertical-align: top; padding-left: 12px; }
            .title { font-size: 16px; font-weight: bold; color: #333333; line-height: 1.4; margin-bottom: 6px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
            .info { font-size: 12px; color: #999999; line-height: 1.6; margin-bottom: 8px; }
            .btn { display: inline-block; background-color: #ff2442; color: #ffffff !important; padding: 6px 12px; border-radius: 15px; font-size: 12px; text-decoration: none; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2 style="color: #333; text-align: center; border-bottom: 2px solid #ff2442; padding-bottom: 10px;">内容列表</h2>
    """

    print("Generating email content and downloading images...")
    
    # Process items
    for idx, item in enumerate(data):
        title = item.get('title') or "无标题"
        author = item.get('author') or "未知作者"
        publish_time = item.get('publish_time') or "未知"
        like_count = item.get('like_count') or 0
        url = item.get('url') or "#"
        cover_url = item.get('cover_url') or ""
        
        img_html = '<div style="width:100px;height:100px;background:#eee;border-radius:8px;display:flex;align-items:center;justify-content:center;color:#999;font-size:12px;">无封面</div>'
        
        if cover_url:
            try:
                # Use a unique Content-ID
                cid = f"img_{idx}"
                
                # Download image
                req = urllib.request.Request(
                    cover_url, 
                    headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                )
                
                with urllib.request.urlopen(req, timeout=10) as response:
                    img_data = response.read()
                    
                image = MIMEImage(img_data)
                image.add_header('Content-ID', f'<{cid}>')
                image.add_header('Content-Disposition', 'inline', filename=f'{cid}.jpg')
                message.attach(image)
                
                img_html = f'<img src="cid:{cid}" width="100" height="100" style="display:block; object-fit: cover; border-radius: 8px;">'
                print(f"Downloaded image for item {idx+1}")
                
            except Exception as e:
                print(f"Failed to download image for item {idx+1}: {e}")
                img_html = f'<div style="width:100px;height:100px;background:#eee;border-radius:8px;display:flex;align-items:center;justify-content:center;color:red;font-size:12px;">加载失败</div>'

        html_content += f"""
            <div class="card">
                <table class="card-table">
                    <tr>
                        <td class="img-cell">{img_html}</td>
                        <td class="content-cell">
                            <div class="title">{title}</div>
                            <div class="info">
                                <span>👤 {author}</span><br>
                                <span>🕒 {publish_time}</span> &nbsp;|&nbsp; <span>❤️ {like_count}</span>
                            </div>
                            <a href="{url}" class="btn">查看详情</a>
                        </td>
                    </tr>
                </table>
            </div>
        """

    html_content += """
        <div style="text-align:center; padding: 20px 0; color: #999; font-size: 12px;">
            Already at the bottom
        </div>
        </div>
    </body>
    </html>
    """

    # Attach HTML content
    msg_alternative.attach(MIMEText("您的邮箱客户端不支持 HTML 格式，请升级。", "plain"))
    msg_alternative.attach(MIMEText(html_content, "html"))

    # Send email
    context = ssl.create_default_context()
    try:
        print(f"Connecting to {smtp_server}...")
        with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
            server.login(sender_email, password)
            print("Login successful. Sending email...")
            server.sendmail(
                sender_email, receiver_email, message.as_string()
            )
        print("Email sent successfully!")
    except Exception as e:
        print(f"Failed to send email: {e}")

if __name__ == "__main__":
    send_email()
