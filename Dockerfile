FROM python:3.12-slim-bookworm

ENV HOME=/home/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/home/app/.cache/ms-playwright

WORKDIR /app

# 1. 基础系统工具与运行用户创建
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
       gosu \
       ca-certificates \
       curl \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. 分层安装 Python 依赖与 Firefox 最小共享依赖库，清理系统文档与多语言包
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install-deps firefox \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/share/doc/* /usr/share/man/* /usr/share/locale/* /usr/share/info/*

# 3. 复制应用源码并安装，深度裁剪第三方库内部测试包与头文件
COPY pyproject.toml README.md setup.py ./
COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint

RUN pip install --no-cache-dir --no-deps . \
    && find /usr/local/lib/python3.12/site-packages -type d \( -name "tests" -o -name "test" -o -name "__pycache__" \) -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12/site-packages -type f \( -name "*.pyc" -o -name "*.pyo" -o -name "*.c" -o -name "*.h" \) -delete \
    && python -m compileall -q /app/src \
    && mkdir -p /app/data /home/app/.cache \
    && chown -R app:app /app /home/app \
    && chmod 0755 /usr/local/bin/docker-entrypoint

# 4. 下载 Camoufox 浏览器并立即清除下载压缩包源文件。
# camoufox 依赖 fpgen，而 fpgen 把模型写进 site-packages/fpgen/data：
# 该目录随 pip 安装属 root，所以在 USER app 下 fetch 会报
# "cannot read or write fpgen's model file"。浏览器缓存仍要落在 app 的
# HOME 下，因此这里以 root 下载，再把缓存目录整体归还给 app。
RUN camoufox fetch \
    && find /home/app/.cache -type f \( -name "*.zip" -o -name "*.tar.*" -o -name "*.gz" -o -name "*.xz" \) -delete \
    && chown -R app:app /home/app/.cache /usr/local/lib/python3.12/site-packages/fpgen/data

# 5. 清理全局临时文件与缓存
RUN rm -rf /tmp/* /var/tmp/* /root/.cache

ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["digitalplat-register-web", "--host", "0.0.0.0", "--port", "8400"]
