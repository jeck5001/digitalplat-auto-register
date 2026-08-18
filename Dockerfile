FROM python:3.12-slim-bookworm

ENV HOME=/home/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/home/app/.cache/ms-playwright

WORKDIR /app

# 安装 gosu 及基础系统工具
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
       gosu \
       ca-certificates \
       curl \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 分层安装 Python 依赖并仅安装 Firefox 最小依赖库
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install-deps firefox \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 复制项目代码并安装
COPY pyproject.toml README.md setup.py ./
COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint

RUN pip install --no-cache-dir --no-deps . \
    && python -m compileall -q /app/src \
    && mkdir -p /app/data /home/app/.cache \
    && chown -R app:app /app /home/app \
    && chmod 0755 /usr/local/bin/docker-entrypoint

USER app

# 下载 Camoufox 专属浏览器二进制
RUN camoufox fetch

USER root

ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["digitalplat-register-web", "--host", "0.0.0.0", "--port", "8400"]
