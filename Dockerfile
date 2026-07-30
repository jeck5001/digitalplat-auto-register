FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV HOME=/home/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY pyproject.toml README.md setup.py ./
COPY src ./src

RUN pip install . \
    && python -m compileall -q /app/src \
    && digitalplat-register --help > /dev/null \
    && mkdir /app/data \
    && chown -R app:app /app /home/app

USER app

# The package does not bundle the Camoufox browser binary.
RUN camoufox fetch

ENTRYPOINT ["digitalplat-register"]
CMD ["--help"]
