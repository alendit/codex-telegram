# Deployment Scaffold

This directory contains generic deployment examples for `codex-telegram`.
Copy these files into your own private deployment repository before adding
host paths, registry names, tunnels, CI, or production secrets.

## Files

- `.env.example`: required environment variables with placeholder values.
- `docker-compose.example.yaml`: a minimal two-service stack with
  `codex-telegram` and `codex-app-server`.
- `codex-app-server/`: optional runtime image scaffold for running
  `codex app-server` with the helper scripts and example agent template.

## Required Secrets

- `TELEGRAM_BOT_TOKEN`
- `CODEX_APP_SERVER_WS_TOKEN`
- `CODEX_TELEGRAM_WEBHOOK_TOKEN` when webhooks or runtime helper APIs are enabled.

Store real values in your deployment system, not in git.

## Quick Compose Run

From the repository root:

```bash
cp deploy/.env.example deploy/.env
cp config/codex_telegram.example.toml config/codex_telegram.toml
```

Edit `deploy/.env`:

- set `TELEGRAM_BOT_TOKEN` from BotFather
- set a new random `CODEX_APP_SERVER_WS_TOKEN`
- leave `TELEGRAM_ALLOW_FROM=*` only for an initial local test

Start the stack:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.example.yaml up --build
```

Open Telegram, start a chat with the bot, and send `/help`. If the bot replies,
Telegram delivery and the application container are working. Send `/current` to
check the conversation/backend state.

The app-server container persists Codex state in the `codex-home` volume. Make
sure that volume contains whatever Codex auth/config your runtime needs before
expecting real Codex turns to complete.

## State

The example compose file uses named volumes for:

- Telegram application state and SQLite persistence.
- Shared outbound attachments.
- The app-server `/agent` workspace.
- The app-server Codex home at `/root/.codex`.

For a real deployment, replace named volumes with your preferred persistent
storage only in a private deployment repo.
