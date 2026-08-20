FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app

# Зависимости отдельным слоем — правка кода не пересобирает их заново.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
# Миграции едут в образ: применять их на сервере нужно тем же артефактом,
# что и код, иначе схема и приложение расходятся.
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 999 --no-create-home seshat \
    && chown -R seshat:seshat /app
USER seshat

CMD ["python", "-m", "seshat"]
