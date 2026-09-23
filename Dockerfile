# =============================================================================
# 阿卡莉 1.0 云端服务镜像
# 说明：Docker 内固定 Python 3.12（依赖兼容性优于 3.14；本机 .venv 仍为 3.14.6）。
# 构建：docker build -t akali:1.0 .
# 运行：见 docker-compose.yml（自动带 nginx 反代 + redis）
# =============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    AKALI_NO_BROWSER=1

# 基础系统依赖（时区/证书）；如需 docx 转 HTML 的中文字体可再追加 fonts-noto-cjk
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层独立，便于缓存
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 项目代码 + 知识库（KB 也可不打包，改由启动同步从对象存储拉取）
COPY . .

# 配额落盘目录（FX_QUOTA_DIR=/data/quota 时用；compose 里挂卷持久化）
RUN mkdir -p /data/quota

EXPOSE 8000

# HOST/PORT 由环境变量决定（compose 中设 HOST=0.0.0.0）
CMD ["python", "用户端特用/web_app.py"]
