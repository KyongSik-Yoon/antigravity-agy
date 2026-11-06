---
description: List the models agy currently offers (live `agy models`), with the active one starred
argument-hint: "[--json]"
allowed-tools: Bash(node:*)
---

Run and return stdout verbatim:
```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" models $ARGUMENTS
```

Either the id (left column) or the name (right column) works for `--model` and `/agy:config set-model`.
