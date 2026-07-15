---
name: agy
description: Run Google Antigravity (`agy` CLI) headlessly for a second opinion, code review, or handing off a coding task — mirror of the codex skill. Use when the user asks to run agy/antigravity, wants Gemini's take, or wants to orchestrate agy alongside Claude/Codex.
---

# agy

Thin wrapper over `${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs`, which drives the `agy` CLI in headless (`-p`) mode.

## Commands

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" task [--background] [--write] [--yolo] [--resume] [--model "<name>"] "<prompt>"
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" review [--base <ref>] [--scope auto|working-tree|branch]
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" adversarial-review [--base <ref>] [focus text]
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" status [job-id] [--all]
node "${CLAUDE_PLUGIN_ROOT}/scripts/agy-companion.mjs" result [job-id]
```

## Permission model (safe by default)

- default `task` / all `review` = **read-only** (`--mode plan`)
- `--write` = edits allowed, tools still prompt (`--mode accept-edits`)
- `--yolo` = the only path that adds `--dangerously-skip-permissions`

## Notes

- Background jobs stored under `~/.claude/agy/jobs/`. Poll with `status`, collect with `result`.
- `--model` takes the exact string from `agy models` (e.g. `"Gemini 3.1 Pro (High)"`, `"Claude Opus 4.6 (Thinking)"`).
- `review` needs a git repo. Feeds `git diff` to agy and asks for findings.
- Requires the `agy` CLI installed and logged in.
