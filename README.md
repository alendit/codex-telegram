# codex-telegram

Direct Telegram bot for Codex backed by `codex app-server`.

`codex-telegram` is an async Python application with a narrow architecture:
Telegram adapter -> application core -> Codex app-server adapter. Telegram owns
chat UX, the core owns conversation policy, and `codex app-server` owns Codex
runtime execution.

## Quick Start

### Create A Telegram Bot

1. Open Telegram and start a chat with `@BotFather`.
2. Send `/newbot`.
3. Choose a display name and username for the bot.
4. Copy the bot token BotFather returns. It is the value for
   `TELEGRAM_BOT_TOKEN`.
5. Start a chat with your new bot and send `/start`.

For a first local run, keep `TELEGRAM_ALLOW_FROM=*`. For a real deployment,
replace it with a comma-separated allowlist of numeric Telegram user or group
ids. You can start open, send one message, read the chat id from the bot logs,
then lock the allowlist down.

### Local Python Run

Install dependencies:

```bash
uv sync --extra dev
```

Create local config:

```bash
cp deploy/.env.example .env
cp config/codex_telegram.example.toml config/codex_telegram.toml
```

Edit `.env` and set at least:

```bash
TELEGRAM_BOT_TOKEN=...
CODEX_APP_SERVER_WS_TOKEN=...
```

Make sure the Codex CLI is installed and authenticated in the environment that
will run `codex app-server`.

Load `.env` into your shell:

```bash
set -a
. ./.env
set +a
```

Start a local `codex app-server` in another terminal. The token must match
`CODEX_APP_SERVER_WS_TOKEN`:

```bash
printf '%s' "$CODEX_APP_SERVER_WS_TOKEN" > ./codex-app-server.token
chmod 600 ./codex-app-server.token
```

```bash
codex --enable codex_hooks app-server \
  --listen ws://127.0.0.1:4312 \
  --ws-auth capability-token \
  --ws-token-file ./codex-app-server.token
```

Run the bot:

```bash
uv run codex-telegram
```

In Telegram, send `/help` to the bot. A command list means Telegram delivery and
the bot process are working. Then send `/current` to verify the active
conversation state and backend connection.

### Docker Compose Run

The Compose scaffold starts both `codex-telegram` and a local `codex
app-server` runtime:

```bash
cp deploy/.env.example deploy/.env
cp config/codex_telegram.example.toml config/codex_telegram.toml
```

Edit `deploy/.env`, set `TELEGRAM_BOT_TOKEN` and
`CODEX_APP_SERVER_WS_TOKEN`, then start the stack:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.example.yaml up --build
```

The example uses named volumes for `/agent`, `/root/.codex`, SQLite state, and
shared attachments. You still need a valid Codex runtime login/config inside the
`codex-home` volume before the app-server can execute real turns. For production
use, copy the scaffold into a private deployment repo and replace the storage
and ingress choices there.

Run tests:

```bash
uv run pytest
```

## Feature Overview

- Direct Telegram message handling.
- Durable per-chat conversation anchors.
- Focused conversation windows with `/focus`, `/to`, `/current`, and `/history`.
- Codex thread discovery and attachment.
- Project binding through `/project`.
- Runtime overrides for profile, model, effort, summary, verbosity, follow-up mode, and fast mode.
- Approval prompts with inline Telegram buttons.
- Progress updates, typing indicators, and final reply delivery.
- Optional realtime text conversation mode.
- MCP and skill discovery through the focused Codex runtime.
- Optional voice input transcription.
- Durable external event webhooks.
- Queued attachment send-back from runtime files into Telegram.

## Configuration Details

Runtime configuration is split between environment variables and TOML.

Environment variables are for secrets and deployment paths:

- `TELEGRAM_BOT_TOKEN`: required Telegram bot token.
- `TELEGRAM_ALLOW_FROM`: comma-separated numeric chat allowlist, or `*`.
- `CODEX_APP_SERVER_WS_URL`: default backend websocket URL.
- `CODEX_APP_SERVER_WS_TOKEN`: optional app-server websocket bearer token.
- `CODEX_TELEGRAM_DB_PATH`: SQLite state path.
- `CODEX_TELEGRAM_CONFIG`: TOML config path.
- `SPEECH_TO_TEXT_BASE_URL`: optional speech-to-text endpoint.
- `SPEECH_TO_TEXT_API_KEY`: optional speech-to-text bearer token.
- `CODEX_TELEGRAM_WEBHOOK_TOKEN`: admin token for webhook and attachment helper APIs.
- `CODEX_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL`: public URL returned for event webhooks.
- `CODEX_TELEGRAM_WEBHOOK_ADMIN_URL`: internal helper API URL.
- `CODEX_TELEGRAM_ATTACHMENT_ADMIN_URL`: internal attachment helper API URL.

TOML config lives in `config/codex_telegram.toml` by default. Start from
`config/codex_telegram.example.toml`; values matching application defaults are
present but commented out.

`CODEX_TELEGRAM_WEBHOOK_TOKEN` is only required when the webhook listener,
runtime bridge helper API, or attachment send-back helper API is enabled. The
example `.env` includes it so the Docker scaffold works when those optional
paths are turned on.

Important TOML areas:

- `[telegram]`: topic sessions, typing cadence, wait notices, bridge windows, and language hints.
- `[speech_to_text]`: optional voice transcription provider.
- `[webhook]`: optional durable event webhook listener.
- `[attachments]`: shared attachment root visible to both services.
- `[app_servers.*]`: named Codex app-server backends.
- `[defaults]`: default runtime profile and default Project shortcut.
- `[client_default_profiles]`: optional chat-specific default profiles.
- `[client_allowed_projects]`: optional chat-specific project allowlists.
- `[profiles.*]`: Codex runtime profile definitions.

## Deployment Overview

The public repository includes generic deployment scaffolding in `deploy/`.
Use it to understand the service shape, then keep your production stack in a
private deployment repository.

The minimal deployment has two services:

- `codex-telegram`: Telegram intake, command handling, persistence, progress, webhooks, and attachment queueing.
- `codex-app-server`: Codex runtime backend exposed over websocket.

Do not commit real `.env` files, chat allowlists, private hostnames, tunnel
credentials, or provider tokens. Rotate any credential that was ever committed.

## Detailed Features

### Conversations And Threads

Each chat has durable conversation anchors and short-lived focused bridge
windows. `/new` starts a fresh conversation, `/threads` lists attachable Codex
threads, `/attach_thread` binds an existing backend thread, and `/focus` changes
the active Telegram window.

### Projects And Directories

`/project` binds conversations to configured project roots. `/dir`, `/cd`, and
`/cwd` control the next-turn working directory. Project and directory policy is
owned by the application layer rather than Telegram-specific code.

### Approvals

When Codex requests an approval or asks a supported user-input question, the bot
renders a Telegram prompt with inline buttons and sends the selected response
back to `codex app-server`.

### Runtime Settings

Profiles define model, provider, sandbox, approval, network, and verbosity
defaults. Users can override supported fields per conversation with commands
such as `/profile`, `/model`, `/effort`, `/summary`, `/verbosity`,
`/followup_mode`, and `/fast`.

### MCP And Skills

The `/mcp` and `/skills` commands inspect the focused Codex runtime. `/skill`
submits a normal Codex turn with a selected skill.

### Webhooks

The optional webhook API lets outside systems trigger a bound conversation by
posting an event payload. Events use the same progress, approval, and final
reply path as Telegram messages.

### Attachments

Runtime agents can place a file under the shared attachment root and use the
included helper to queue it for Telegram delivery.

## Development And Testing

Useful commands:

```bash
uv sync --extra dev
uv run pytest
./scripts/build-image.sh
./scripts/build-runtime-images.sh
```

Core tests cover application policy, Telegram rendering and command behavior,
SQLite persistence, app-server protocol mapping, webhooks, approvals, profiles,
and helper entrypoints.

## Security Notes

- Never commit `.env`, root `.codex`, runtime state, tokens, chat allowlists, or private deployment topology.
- Keep production compose files and CI/CD wiring in a private deployment repo.
- Rotate any secret that ever appears in git history.
- Use a history-aware secret scanner before making a repository public.
