---
name: codex-telegram-webhooks
description: Use when setting up or explaining codex-telegram external event webhooks for the current Telegram/Codex conversation anchor, so outside systems can POST events that reactivate the conversation without changing Telegram focus.
---

# Codex Telegram Webhooks

This runtime runs inside a `codex-telegram` deployment. Webhook setup belongs to
the sibling `codex-telegram` service, not to this app-server container.

Use `codex-telegram-webhook` to create, list, or revoke durable webhook
subscriptions. Use the current `chat_key` and conversation `anchor_id` from the
developer instructions injected into this conversation. If an anchor id is not
available, create the subscription with `--codex-backend-id` and
`--codex-thread-id`.

The helper talks to the sibling service admin API at
`CODEX_TELEGRAM_WEBHOOK_ADMIN_URL`. Its admin bearer token comes from
`CODEX_TELEGRAM_WEBHOOK_ADMIN_TOKEN`; the deployment maps that value from the
application's canonical `CODEX_TELEGRAM_WEBHOOK_TOKEN`.

Examples:

```bash
codex-telegram-webhook create "front-door" --chat-key "chat:123" --anchor-id "anchor1234"
codex-telegram-webhook create "ci" --chat-key "chat:123" --codex-backend-id "primary" --codex-thread-id "codex1234"
codex-telegram-webhook list --chat-key "chat:123" --anchor-id "anchor1234"
codex-telegram-webhook revoke "front-door" --chat-key "chat:123"
```

Rules:

- Never print or reveal `CODEX_TELEGRAM_WEBHOOK_ADMIN_TOKEN`.
- If only `CODEX_TELEGRAM_WEBHOOK_TOKEN` is present, treat it as the same admin
  token for helper use. Do not print it.
- The per-webhook bearer secret returned by `create` is shown once; give it only
  to the external system that will call the event URL.
- External systems should call `POST /events/<id>` with `Authorization: Bearer
  <event-secret>` and a JSON object containing fields such as `input`, `prompt`,
  `text`, `message`, `metadata`, `payload`, or other useful event fields.
- `Idempotency-Key` is supported for external retry safety.
- Events run against the bound conversation anchor and use normal Telegram progress,
  approval, final-reply, and active-turn steering behavior.
- Bridge windows expire independently of webhook bindings. A webhook event
  creates or reuses a bridge for the anchor without changing Telegram focus.
- Do not create a separate webhook listener, Telegram bot, remote shell
  endpoint, or Home Assistant-specific bridge from this runtime.
