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

Model is checked against the live `agy models` list; pass an id or name from `/agy:models`, e.g. `gemini-4-argon-high` or `"Gemini 4 Argon (High)"`. An unknown name is rejected with the list.
