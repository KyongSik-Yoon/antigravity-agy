---
description: Run an adversarial agy review that hunts for bugs and security holes in the current diff
argument-hint: "[--base <ref>] [--scope auto|working-tree|branch] [focus text]"
disable-model-invocation: true
allowed-tools: Bash(node:*), Bash(git:*)
---

Run an adversarial agy review of the current git diff. Review-only.

Raw arguments: `$ARGUMENTS`

Run:
```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" adversarial-review $ARGUMENTS
```
Return the command stdout verbatim. No paraphrase, no commentary.
