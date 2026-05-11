FROM python:3.13-bookworm

ARG VERSION=0.4.0
ARG VCS_REF=""

LABEL org.opencontainers.image.title="codex-telegram" \
      org.opencontainers.image.description="Telegram bridge for Codex backed by codex app-server." \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/alendit/codex-telegram" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

CMD ["uv", "run", "codex-telegram"]
