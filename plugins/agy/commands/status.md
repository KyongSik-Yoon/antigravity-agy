---
description: Show status of agy background jobs
argument-hint: "[job-id] [--all]"
allowed-tools: Bash(node:*)
---

Raw arguments: `$ARGUMENTS`

Run and return stdout verbatim:
```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" status "$ARGUMENTS"
```
