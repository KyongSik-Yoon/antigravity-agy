# antigravity-agy

Orchestrate **Google Antigravity** (the `agy` CLI) from **Claude Code** — a mirror of the
OpenAI `codex` plugin, but for `agy`. Delegate coding tasks, get a second opinion, and run
code reviews from Gemini / Claude / GPT models exposed by Antigravity, with no OAuth wiring:
each CLI keeps its own auth, Claude just shells out.

## Requirements

- [`agy`](https://antigravity.google) CLI installed **and logged in**
- Node.js 18+

## Install

```
/plugin marketplace add KyongSik-Yoon/antigravity-agy
/plugin install agy@antigravity-agy
```

## Commands

| Command | What it does |
|---------|--------------|
| `/agy:rescue [task]` | Hand a coding/diagnosis task to agy (via the `agy-rescue` subagent) |
| `/agy:review` | Review the current git diff |
| `/agy:adversarial-review [focus]` | Hunt for bugs/security holes in the current diff |
| `/agy:status [--all]` | Status of background jobs |
| `/agy:result [job-id]` | Fetch a finished background job's output |
| `/agy:config [set-model "<name>"]` | Show or persist the default model |

## Model selection

Per-call: `--model "<name>"` (exact string from `agy models`, e.g. `"Gemini 3.1 Pro (High)"`).

Persisted default (`~/.claude/agy/config.json`):

```
/agy:config set-model "Gemini 3.1 Pro (High)"
/agy:config                 # show
/agy:config clear-model
```

Precedence: `--model` flag > `AGY_MODEL` env var > saved config > agy default. Applies to `task` and `review`.

## Permission model (safe by default)

- default = **read-only** (`--mode plan`)
- `--write` = edits allowed, tools still prompt (`--mode accept-edits`)
- `--yolo` = the only flag that adds `--dangerously-skip-permissions`

## How it works

`scripts/agy-companion.mjs` wraps `agy -p` (headless). `agy` has no native background/job
store, so the companion adds a small file-based one under `~/.claude/agy/jobs/`. `review`
feeds `git diff` to agy and asks for findings.

## License

MIT
