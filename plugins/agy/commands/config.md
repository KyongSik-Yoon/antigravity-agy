---
description: Show or set the default agy model
argument-hint: "[set-model \"<name>\" | clear-model]"
allowed-tools: Bash(node:*)
---

Raw arguments: `$ARGUMENTS`

Run and return stdout verbatim:
```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" config $ARGUMENTS
```

Model name must match `agy models` exactly, e.g. `"Gemini 3.1 Pro (High)"`.
