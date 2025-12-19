FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（gcc用于编译某些Python包）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件和安装项目
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# 复制应用代码
COPY qqbot/ ./qqbot/

# 复制基础配置
COPY .env ./.env

# 暴露端口（NoneBot2默认8080）
EXPOSE 8080

# 启动命令
CMD ["nb", "run"]
