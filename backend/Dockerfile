# api 和 worker 共用这个镜像，只是启动命令不同（见 docker-compose.yml）
FROM python:3.12-slim

# git：克隆被分析的仓库；ripgrep：沙箱里的 grep 工具。
# 不装 JDK / Maven —— 系统只读代码、从不构建或运行被分析的仓库（SPEC 18.2）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 用 pip 装 uv（而不是从 ghcr.io 拷二进制）：有些网络环境拉不到 ghcr.io
RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" PYTHONPATH=/app

# 先只拷依赖清单再安装，代码改动时可以复用这一层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

# 不用 root 跑：即使被分析仓库里有恶意内容触发了什么漏洞，权限也受限
RUN useradd -m -u 1000 app && mkdir -p /data/repos && chown -R app /data /app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
