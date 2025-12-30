# 使用官方 Playwright 镜像（包含 Python 和 浏览器环境）
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

# 设置工作目录
WORKDIR /app

# 复制项目文件
COPY . /app

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 安装 tzdata (用于设置时区)，使用非交互模式
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y tzdata && rm -rf /var/lib/apt/lists/*

# 创建结果目录（确保有权限写入）
RUN mkdir -p res_docs && chmod 777 res_docs

# 设置环境变量
# 默认关闭 HEADFUL 模式，因为容器通常是无头的
ENV HEADFUL=false
# 设置 Python 输出不缓冲，方便查看日志
ENV PYTHONUNBUFFERED=1
# 设置时区为上海
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 运行调度脚本
CMD ["python", "scheduler.py"]
