---
description: Cancel a running agy background job
argument-hint: "[job-id]"
allowed-tools: Bash(node:*)
---

Raw arguments: `$ARGUMENTS`

Run and return stdout verbatim:
```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" cancel $ARGUMENTS
```
