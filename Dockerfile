FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV HOME=/home/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY pyproject.toml README.md requirements.txt setup.py ./
COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint

RUN pip install . \
    && python -m compileall -q /app/src \
    && digitalplat-register --help > /dev/null \
    && digitalplat-register-web --help > /dev/null \
    && mkdir -p /app/data \
    && chown -R app:app /app /home/app \
    && chmod 0755 /usr/local/bin/docker-entrypoint

USER app

# The package does not bundle the Camoufox browser binary.
RUN camoufox fetch

USER root

ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["digitalplat-register-web", "--host", "0.0.0.0", "--port", "8400"]
