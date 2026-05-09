# AGENTS.md

This workspace is the backend project root for a `codex-telegram` app-server
runtime.

## Intent

- Serve Telegram-facing Codex conversations through the sibling
  `codex-telegram` application.
- Keep runtime state explicit and portable.
- Treat `/agent` as the working project root and `/root/.codex` as Codex home.

## Rules

- Do not print secrets.
- Prefer small, inspectable operations.
- Use `request_user_input` when a task is blocked by a user decision.
- Use `codex-telegram-bridge` to inspect or notify the current Telegram bridge.
- Use `codex-telegram-webhook` to manage durable webhook bindings.
- Use `codex-telegram-send-attachment` to send files from `/attachments`.
- Do not create a separate Telegram bot, webhook listener, or remote shell
  endpoint from this runtime.

## Paths

- `/agent`: assistant workspace.
- `/attachments`: shared outbound attachment root.
- `/root/.codex`: persisted Codex runtime state.
