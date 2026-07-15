---
description: Fetch the result of a finished agy background job
argument-hint: "[job-id]"
allowed-tools: Bash(node:*)
---

Raw arguments: `$ARGUMENTS`

Run and return stdout verbatim:
```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" result "$ARGUMENTS"
```
