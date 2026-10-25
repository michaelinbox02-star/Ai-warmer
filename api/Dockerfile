FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home-dir /app app \
    && mkdir -p /data/secrets \
    && chown -R app:app /app /data

COPY api/pyproject.toml ./pyproject.toml
COPY api/app ./app
COPY api/alembic ./alembic
COPY api/alembic.ini ./alembic.ini
# The baseline migration executes this file in one transaction.
COPY schema.sql ./schema.sql

RUN pip install --upgrade pip \
    && pip install .

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
