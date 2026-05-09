# Architecture

`codex-telegram` is split into stable application code and unstable runtime
edges.

```text
Telegram adapter -> application core -> Codex app-server adapter
                         |
                         v
                 persistence adapters
```

The application core owns conversation lifecycle, thread policy, approvals,
runtime settings, webhook semantics, turn orchestration, and stable internal
models. Adapters own Telegram Bot API details, Codex app-server websocket
protocol translation, SQLite persistence, filesystem checks, and optional
speech-to-text transport.

Key invariants:

- Core code does not import Telegram SDK types.
- Telegram code does not build raw app-server protocol payloads.
- Persistence stores only application-owned state, not Codex conversation history.
- Runtime and deployment details stay outside domain and application modules.
- Secrets belong in environment or deployment secret stores, not in TOML or git.
