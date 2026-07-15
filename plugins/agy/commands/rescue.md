---
description: Delegate investigation, a fix, or follow-up work to the agy (Google Antigravity) rescue subagent
argument-hint: "[--background] [--write] [--yolo] [--resume] [--model <name>] [what agy should do]"
context: fork
allowed-tools: Bash(node:*)
---

Route this request to the `agy:agy-rescue` subagent.
The final user-visible response must be agy's output verbatim.

Raw user request:
$ARGUMENTS

Rules:

- `--background` runs the subagent in a Claude background task; otherwise foreground.
- Default is READ-ONLY. Keep `--write` / `--yolo` only if the user explicitly included them, and forward them to `task`.
- Keep `--resume`, `--model` for the forwarded `task` call. Do not treat them as task text.
- The subagent is a thin forwarder: one `Bash` call to `node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" task ...`, returning stdout as-is.
- Return the agy companion stdout verbatim. No paraphrase, no commentary.
- If no request was supplied, ask what agy should do.
