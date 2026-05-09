FROM python:3.13-bookworm

ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN uv sync --no-dev

CMD ["uv", "run", "codex-telegram"]
