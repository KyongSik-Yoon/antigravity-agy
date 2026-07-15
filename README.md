# antigravity-agy

[English](README.md) · [한국어](README.ko.md)

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
| `/agy:cancel [job-id]` | Cancel a running background job |
| `/agy:config [set-model "<name>"]` | Show or persist the default model |
| `/agy:hint` | Cheat-sheet: active model, available models, commands |

## Model selection

Default model: **`Gemini 3.5 Flash (High)`**.

Per-call: `--model "<name>"` (exact string from `agy models`, e.g. `"Gemini 3.1 Pro (High)"`).
A `--model` passed to `task` is **persisted** as the new default.

Persisted default (`~/.claude/agy/config.json`):

```
/agy:config set-model "Gemini 3.1 Pro (High)"
/agy:config                 # show
/agy:config clear-model     # back to Gemini 3.5 Flash (High)
```

Precedence: `--model` flag > `AGY_MODEL` env var > saved config > built-in default.
Applies to `task` and `review`.

## Permission model (safe by default)

- default = **read-only** (`--mode plan`)
- `--write` = edits allowed, tools still prompt (`--mode accept-edits`)
- `--yolo` = the only flag that adds `--dangerously-skip-permissions`

## Data & environment safety

- **Env scrubbing**: credential-shaped vars (`*TOKEN*`, `*SECRET*`, `*_KEY`, `AWS_*`, `GITHUB*`, `ANTHROPIC`, `OPENAI`, …) are stripped before launching `agy`, so a third-party subprocess never inherits your tokens. `AGY_*` is kept. Opt out with `AGY_KEEP_ENV=1`.
- **Review egress guard**: `review` / `adversarial-review` send your `git diff` to Google. They print a provider notice and **refuse** if the diff looks like it contains secrets (private keys, AWS/GitHub tokens, `api_key=`…). Override a false positive with `--force`.

## Background jobs

`task --background` spawns a detached worker and returns a job id. The file-based
store under `~/.claude/agy/jobs/` is supervised:

- **atomic writes** (temp + rename) — readers never see partial JSON
- **liveness** — the worker's pid is recorded; `status`/`result` reconcile a job stuck
  on `running` whose worker died (crash/OOM/reboot) to `failed`
- **cancel** — `/agy:cancel` kills the worker process group
- **retention** — only the newest 50 finished jobs are kept
- **hard timeout** — the wrapper kills a hung `agy` (and its whole process group) at
  its `--print-timeout` + 60s, so a foreground call can never block forever. Tune the
  grace with `AGY_HARD_GRACE_MS`.

## How it works

`scripts/agy-companion.mjs` wraps `agy -p` (headless). `agy` has no native background/job
store, so the companion adds the supervised file-based one above. `review`
feeds `git diff` to agy and asks for findings.

## License

MIT
