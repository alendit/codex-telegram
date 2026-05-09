# Compatibility Notes

This document lists retained compatibility paths, why they still exist, who owns
them, and what evidence is needed before removal. It is intentionally about
runtime and persisted-state compatibility, not general user documentation.

## Retained Paths

| Path | Owner | Why retained | Removal condition |
| --- | --- | --- | --- |
| Command aliases `/abort` and `/stop` for `/interrupt` | `adapters/telegram/commands.py` | Existing Telegram users may still use the old stop words for active-turn cancellation. | Remove after command telemetry, smoke usage, or an operator decision confirms only `/interrupt` should remain. |
| Command aliases `/cd` and `/cwd` for `/dir` | `adapters/telegram/commands.py` | These preserve shell-like and earlier cwd-oriented command habits while directory policy moved behind `/dir`. | Remove after users and README-facing docs depend only on `/dir`. |
| Command alias `/clearparams` for `/resetparams` | `adapters/telegram/commands.py` | Older command wording still maps to the same runtime-override clear operation. | Remove after there is no known usage or persisted shortcut/help text requiring `/clearparams`. |
| Compatibility response for `/backends`, `/select_backend`, and `/select-backend` | `adapters/telegram/commands.py` | Earlier UX explored backend switching, but this service is Codex-only; the response prevents silent confusion from old commands. | Remove once old command discovery paths are gone and unknown-command behavior is preferred. |
| `LogicalThread` as the Telegram-facing bridge shape | `domain/models.py`, `application/conversations.py`, `application/service.py` | The durable model is now anchor plus bridge window, but many application and Telegram read models still use `LogicalThread` as a stable compatibility shape. | Replace after command/status/webhook surfaces consume explicit anchor/bridge read models and tests no longer need `LogicalThread` fixtures. |
| SQLite `threads` to `conversation_anchors` plus `bridge_threads` migration | `adapters/persistence/sqlite.py` | Existing databases can predate the anchor/bridge split and still contain the older logical-thread table state. | Remove only with a schema-version cutoff or a one-time deployed migration that proves all supported databases have already populated anchors and bridges. |
| Legacy webhook `thread_id` fallback to `anchor_id` | `adapters/persistence/sqlite.py`, `application/service.py`, `application/webhooks.py` | Subscriptions created before anchor-backed webhooks can still point at a bridge/thread id. Startup migration resolves the anchor when possible and disables unresolvable rows. | Remove after webhook rows are guaranteed to carry `anchor_id` and webhook creation/management no longer accepts or depends on thread ids. |
| Legacy workspace tables `workspace_catalog` and `thread_workspaces` | `adapters/persistence/sqlite.py` | Project support replaced workspace tables and scopes roots by backend connection; older databases need their rows copied into Projects. | Remove only after a schema-version cutoff or deployed-state check proves no supported database still has these tables. |
| Legacy non-null `overrides.fast_mode` schema | `adapters/persistence/sqlite.py` | Fast mode became tri-state, so old databases with a non-null column need migration into `fast_mode_is_set`. | Remove after all supported databases are known to have the nullable schema and `fast_mode_is_set`. |
| Legacy delivery watermark schema without `thread_id` | `adapters/persistence/sqlite.py` | Delivery watermarks became thread-scoped after anchor/bridge support; older rows need mapping through transcript messages or anchor latest bridge state. | Remove after all supported databases are known to have thread-scoped watermarks. |

## Cleanup Rule

Do not remove one of these paths because it looks obsolete in the current code.
Remove it only when the removal condition is met and the same change updates
tests plus this document. If the evidence is operational-state inspection,
record the exact database/config source and date in the commit or PR notes.
