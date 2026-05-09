---
name: codex-telegram-bridge-control
description: Use when a runtime agent needs to inspect or operate the current codex-telegram bridge, send a short operational notice, refresh the Telegram status card, or route a Telegram command through the app-owned bridge API.
---

# Codex Telegram Bridge Control

Use the injected `logical_thread_id` for immediate Telegram bridge operations.
This is the short-lived Telegram bridge id.

Use `anchor_id` only for durable conversation bindings such as external
webhooks or subscriptions. Do not use `codex_thread_id` for Telegram bridge
control unless a helper explicitly asks for it.

Commands:

```bash
codex-telegram-bridge status --thread-id <logical_thread_id>
codex-telegram-bridge command --thread-id <logical_thread_id> "/status"
codex-telegram-bridge notify --thread-id <logical_thread_id> --text "short notice"
codex-telegram-bridge notify --thread-id <logical_thread_id> --level warning --text "needs attention"
codex-telegram-bridge refresh --thread-id <logical_thread_id>
```

Rules:

- Prefer `codex-telegram-bridge command` for behavior that matches a Telegram
  slash command, so Telegram and bridge API command behavior stay shared.
- Use native `request_user_input` for asking the user questions.
- Use `notify` only for one-way operational notices.
- Do not create a separate Telegram bot, webhook listener, or remote shell
  endpoint from this runtime.
- Do not print secrets.
