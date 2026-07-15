---
description: Run an agy (Google Antigravity) code review against local git state
argument-hint: "[--base <ref>] [--scope auto|working-tree|branch]"
disable-model-invocation: true
allowed-tools: Bash(node:*), Bash(git:*)
---

Run an agy review of the current git diff. Review-only: do not fix anything.

Raw arguments: `$ARGUMENTS`

Run:
```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" review $ARGUMENTS
```
Return the command stdout verbatim. No paraphrase, no commentary. Do not fix the issues it reports.
