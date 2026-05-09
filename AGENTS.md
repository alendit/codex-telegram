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

### Hard Constraints

- Shape work packages around useful stopping points that move toward the final
  direction. If work stopped after the package, the project should be better off
  than if the package had not been built; this does not need to hold for every
  internal slice.
- Treat available information explicitly. Make likely changes easy, keep
  uncertain decisions local and reversible, and model stable behavior directly.
  Use small module, adapter, function, data-mapping, or config boundaries to
  contain uncertainty. Add an abstraction only when the contract is real, stable
  enough to name, and makes the next likely change cheaper.
- Give each behavior a clear owning component.
- Prefer application services over putting business logic into UI, transport,
  webhook, tool, or callback handlers.
- Keep dependencies flowing from unstable code toward stable code.
- Keep core policy isolated from side effects.
- Do not mix unrelated domains into one coordinating component.
- Do not create shared interfaces that implementations can only satisfy by
  narrowing behavior, ignoring requirements, or throwing.
- Make compatibility expectations explicit. Keep legacy handling or legacy paths
  only when a real compatibility requirement exists; otherwise remove obsolete
  paths by default and mention that cleanup explicitly.
- Do not add fallback paths, broad defensive handling, or error swallowing unless
  that layer can make a correct domain decision; unexpected errors should surface
  to the top and fail in obvious ways.

### Required Self-Check For Design-Sensitive Changes

Add these answers to the final note, PR description, or equivalent handoff:

1. If this work package stopped here, would the project be better off than if it
   had not been built?
2. What final direction does this move toward?
3. Which decisions are likely to change, uncertain, or stable, and are uncertain
   ones local and reversible instead of hidden behind speculative abstraction?
4. What component owns this behavior, and why?
5. Does this change increase or reduce coupling?
6. Did any dependency start pointing the wrong way?
7. Could any side effect be moved outward into an adapter or shell?
8. Are the abstractions and interfaces semantically real?
9. What compatibility expectations apply, and were obsolete paths removed unless
   required?
10. What legacy code can we remove now?
11. Where are expected errors handled, and where do unexpected errors surface?
12. What tests prove the core behavior independently of the full system? If the
    design changed, would tests change narrowly, or would unrelated tests need
    rewrites?

## Development

- Prefer small, focused changes that preserve the existing layering.
- After each completed change, verify the focused slice and commit it before
  starting the next unrelated change.
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
