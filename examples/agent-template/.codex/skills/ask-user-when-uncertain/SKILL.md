---
name: ask-user-when-uncertain
description: Use whenever a task is blocked by uncertainty, missing requirements, or a user decision; prefer the supported single-choice or multiple-choice user-input tool instead of guessing or asking only in plain text.
---

# Ask User When Uncertain

When uncertainty affects the next action, ask the user through the supported
choice-based user-input tool.

Use it for:

- missing requirements that would change the implementation or operation
- irreversible, risky, expensive, or externally visible decisions
- multiple plausible approaches where the best choice depends on user intent
- confirmations needed before proceeding with a destructive or broad action

Question shape:

- Prefer one question. Use at most three only when the decisions are independent.
- Use single-choice when exactly one option should be selected.
- Use multiple-choice when several options may be selected together.
- Provide two or three concrete choices with short labels and clear tradeoffs.
- Put the recommended choice first and label it as recommended when there is a
  safe default.
- Keep free-text fallback available for details that do not fit the choices.

Do not use the tool for trivial uncertainty that can be resolved by inspecting
the workspace, running a safe command, or using an established repo convention.
If the user has explicitly asked for autonomous execution, make the best local
decision and record assumptions instead of stopping unless the choice is risky.
