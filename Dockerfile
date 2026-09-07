# Multi-stage build: dependencies are resolved once in the builder and the
# runtime image carries only the virtualenv and the application.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only what the dependency resolution needs, so a source change does not
# invalidate the dependency layer.
COPY pyproject.toml README.md ./
COPY app/__init__.py app/__init__.py
RUN pip install --upgrade pip && pip install .


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_MODE=demo

# Runs unprivileged: the bot needs no root capability.
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app config ./config
COPY --chown=app:app data ./data
COPY --chown=app:app alembic.ini pyproject.toml README.md ./

# SQLite and any imported exports live here; mount a volume to keep them.
RUN mkdir -p /app/var && chown app:app /app/var

USER app

EXPOSE 8080

# Проверяем живой веб-сервер, а не импортируемость модуля: импорт проходит и у
# наглухо упавшего бота.
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status==200 else 1)" \
    || exit 1

CMD ["python", "-m", "app.main"]
