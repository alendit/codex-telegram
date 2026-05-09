# AGENTS.md

This repository is a standalone Telegram-facing Codex service. It runs an async
Python application that talks directly to `codex app-server` and keeps Telegram
as a client adapter, not as the owner of Codex behavior.

## Project Rules

- Use `uv` for dependency management and commands.
- Keep the app fully async in runtime paths.
- Keep the codebase Codex-only and deployment-agnostic.
- Do not add unrelated automation-platform APIs, event schemas, or bridge glue
  to `src/codex_telegram`.
- Do not commit secrets, tokens, chat ids, hostnames, private IP addresses, or
  deployment-specific config.
- Keep public examples generic. Real deployment values belong in private config
  or deployment repositories.

## Architecture

Design changes around these layers:

1. Domain and application core: conversation lifecycle, thread policy,
   approvals, command semantics, settings, webhooks, and stable models.
2. Backend adapter: `codex app-server` websocket/API translation.
3. Client adapters: Telegram intake, formatting, callbacks, media, and delivery.
4. Runtime/deployment layer: compose examples, persisted volumes, config, and
   startup wiring.

Dependency direction should stay:

```text
Telegram adapter ----\
                      \
Other client adapters --> application core --> codex app-server adapter
                      /
Persistence adapter --/
```

Core code must not import Telegram SDK types, raw app-server payload builders,
or deployment/container wiring. Side effects belong in adapters or entrypoints.

## Development

- Prefer small, focused changes that preserve the existing layering.
- Add abstractions only when they name a real stable contract.
- Handle expected errors at the layer with useful context; let unexpected errors
  surface clearly.
- Use structured logging helpers from `src/codex_telegram/observability.py`
  instead of free-form log strings in repo-owned code.
- When changing Telegram-facing behavior, update command rendering/help and
  relevant adapter tests together.

## Verification

Use focused checks for the touched area, and run the full suite before publishing
substantial changes:

```sh
uv run --extra dev pytest
```

Before public release, also run a secret scan such as `gitleaks` and search for
private deployment markers. Git history for public releases must not contain
secrets or deployment-specific details.
