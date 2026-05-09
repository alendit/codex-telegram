# Compatibility Notes

This project currently has no retained compatibility paths.

As of 2026-05-09, command aliases, unsupported backend-switching commands,
legacy config-key mapping, and SQLite repair migrations for old persisted
schemas are intentionally unsupported. Keep new behavior direct by default:

- Add only the canonical command names to the Telegram command executor and help
  catalog.
- Require current config keys instead of mapping older names onto new policy.
- Keep SQLite initialization focused on the current schema. Do not add startup
  migrations for obsolete tables or column shapes unless there is a new explicit
  compatibility requirement.
- Create webhooks against conversation anchors, not bridge/thread ids.

If a future change needs compatibility, document the exact supported path, owner,
reason, and removal condition here in the same change that adds the code.
