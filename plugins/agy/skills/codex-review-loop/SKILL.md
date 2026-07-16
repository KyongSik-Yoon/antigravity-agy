---
name: codex-review-loop
description: "Run a bounded Codex-bot GitLab MR fix loop: poll a review, triage findings, verify, push, and repeat until convergence or a round cap. Use for codex 리뷰 루프, 반복 리뷰 반영, MR 자동 수정, or multi-round Codex findings; skip a one-off review pass."
---

# Codex Review Loop

Drive the codex review bot on a GitLab MR to convergence: each push triggers a new
bot review; each round triages the findings (fix or rebut with evidence), verifies,
pushes, and schedules the next poll. Terminates on convergence tone, round cap, or
rebuttal-only stagnation.

## Preconditions

- MR exists and the codex bot is enabled for the project (it posts a review note per
  pushed head: `Codex code review for mr_update @ <sha>` + `<!-- codex-review:bot-note -->` marker).
- `glab` CLI authenticated. Working branch of the MR checked out locally.
- Round cap from the user (default 8 if unspecified).
- If the MR changes harness/policy docs, run `scripts/check-harness-refs.sh` and the
  changed-skill static validation command BEFORE entering the loop. For each changed
  skill directory, run
  `python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" .agents/skills/<skill-name>`.
  This catches self-findable cross-document contradictions before the bot drips them
  out over many rounds.

## State file

Keep loop state in the private per-loop directory (`$loop_tmp/codex-loop-state.json`):

```json
{
  "mr": 529,
  "scratchDir": "/absolute/private/codex-review-loop.xxxxx",
  "loopId": "uuid-or-random-token",
  "owner": "user@host",
  "lockRemote": "origin",
  "lockRef": "refs/heads/codex-review-locks/mr-529",
  "lockObject": "<remote lock commit>",
  "maxRounds": 8,
  "status": "active",
  "runnersStopped": false,
  "generation": 0,
  "writerId": null,
  "writerRecoveryHashes": ["<sha256 of private recovery capability>"],
  "fixRoundsDone": 0,
  "awaitingReviewForSha": "<full local pushed commit sha>",
  "processedReviewShas": ["<full GitLab review sha1>", "<full GitLab review sha2>"],
  "rebuttals": { "<finding-slug>": "<evidence summary>" },
  "rebuttalOnlyStreak": 0,
  "observabilityFailureStreak": 0,
  "cleanupPhase": "active"
}
```

`rebuttals` doubles as the triage log — if a later round re-raises a rebutted finding,
reply on the discussion again instead of re-triaging from scratch. `processedReviewShas`
is a local cache only — **the source of truth for "which reviews are triaged" lives on
the MR itself**: every triaged review's 🔎 summary note carries a triage-receipt reply
(step 4). Pending work = summary notes without a receipt. This survives session loss
and works from any machine; rebuild the state file from the receipts when in doubt.

`lockRef` is a remote Git ref used as an MR-level mutex. A new loop first creates a
unique lock commit containing `loopId` and `owner`, then creates the fixed ref with a
non-force `git push`; the Git server accepts only one concurrent creation. The loop
must verify that `lockRef` still points to `lockObject` and that the current local
state still fences the active `writerId` before every external side effect, then
release it only during guarded cleanup. Release uses a server-side
compare-and-delete (`--force-with-lease=<ref>:<lockObject>`), so a new owner cannot be
deleted by a stale cleanup. A missing ref is treated as an already-completed release;
a different lock object is a hard stop for direct release, active state, and state-less
or bootstrap recovery: never steal it automatically. The sole convergence exception is
cleanup backed by durable terminal state with `runnersStopped:true`: it preserves the
replacement object and advances only local cleanup. The runtime reports remote handling
as `missing`, `released`, or `replaced`; `replaced` is accepted only by that terminal,
stopped cleanup path. A crashed loop is resumed from its handoff directory so its owner
can release the lock safely.
`lock reconcile-acquire` always requires the exact private bootstrap recovery file;
the CLI and handler both reject calls that omit this capability before any remote
observation or mutation.
The Git ref is a cooperative cross-loop mutex, not a server-side conditional for a
GitLab REST request. `guarded-exec` therefore checks it both before and after each
effect. Ownership loss that is still present at the post-check makes the result
explicitly indeterminate: stop, then reconcile the effect's discussion marker/receipt
or branch head. A transient force-update-and-restore (ABA) between the two observations
is not detectable; do not claim that the ref prevents or detects every out-of-protocol
mutation at the exact instant of a REST call.

`generation` increments under the included runtime helper's OS file lock for every
state transition. `writerId` identifies the current session runner generation shared
by its round, watcher, and scheduled fallback handoff;
observability and cleanup writes carrying an older writer ID are rejected. Atomic
rename protects readers from partial JSON, while the lock plus generation/writer
checks protect the state machine from lost updates and stale writers.
The private `.codex-review-loop.writer-recovery` capability is durable but separate
from state. It permits an explicit takeover only after all previous runners are known
stopped; recovery rotates the capability crash-safely so a retained old token cannot
take over again. It is not an invitation to steal a live runner.

## Codex runtime contract

This skill is designed for Codex. Use one background watcher and one long fallback
per awaited SHA. If the current Codex surface has no native wakeup/task runtime,
do not emulate it with a foreground sleep loop: keep the state file, report the
next action, and resume the loop in a later turn. Never create multiple watchers
for the same SHA. Stop the prior watcher before re-arming.

The state file is loop-private data under the unique `scratchDir`, not a repository
artifact. New loops use the durable user state directory
`${XDG_STATE_HOME:-$HOME/.local/state}/codex-review-loop` by default (override with
`CODEX_REVIEW_LOOP_STATE_HOME`); do not use `/tmp` as the default because a temporary
directory purge must not orphan the remote MR lock. The directory and every loop root
must be owned by the user and mode `700`. Never place concurrent loop state in a shared
top-level path. Persist the absolute `scratchDir` in the handoff/state payload so a
later shell or turn reopens the same directory instead of creating a new one.
Bounded external commands require Linux subreaper + `/proc` descendant containment;
the runtime fails closed if unavailable. On timeout it terminates descendants even if
they call `setsid()`, then applies a separate bounded output-drain step. Exit 125 means
the leader returned but detached descendants remained; the runtime terminates them and
does not accept the command as successful.

## Per-loop durable workspace

Create one private durable workspace for each loop invocation and reuse only its paths
during that invocation. The directory must survive individual shell commands, turns,
reboots, and temporary-directory cleanup until the loop reaches a terminal state.
Never use fixed shared filenames for state, discussions, reply bodies, or receipts:

```bash
umask 077
resume_cleanup_only=0
# 플러그인 루트 디렉토리를 기준으로 runtime.py 경로 설정
runtime_helper="${CODEX_REVIEW_LOOP_RUNTIME:-${CLAUDE_PLUGIN_ROOT}/skills/codex-review-loop/scripts/runtime.py}"
[ -f "$runtime_helper" ] && [ ! -L "$runtime_helper" ] || exit 1
if [ -n "${CODEX_REVIEW_LOOP_DIR:-}" ]; then
  resume_dir="${CODEX_REVIEW_LOOP_DIR%/}"
  case "$resume_dir" in /*) ;; *) echo "loop dir must be absolute" >&2; exit 1 ;; esac
  [ -n "$resume_dir" ] || exit 1
  resume_parent="$(cd -P -- "$(dirname "$resume_dir")" && pwd -P)" || exit 1
  resume_canonical="$resume_parent/$(basename "$resume_dir")"
  case "$(basename "$resume_canonical")" in codex-review-loop.*) ;; *) exit 1 ;; esac
  resume_root_exists=0
  if [ -d "$resume_dir" ] && [ ! -L "$resume_dir" ]; then
    [ -O "$resume_dir" ] || exit 1
    existing_canonical="$(cd -P -- "$resume_dir" && pwd -P)" || exit 1
    [ "$existing_canonical" = "$resume_canonical" ] || exit 1
    resume_mode="$(stat -c '%a' "$resume_canonical" 2>/dev/null || stat -f '%Lp' "$resume_canonical" 2>/dev/null)" || exit 1
    case "$resume_mode" in 700|0700) ;; *) echo "loop dir must be mode 700" >&2; exit 1 ;; esac
    resume_root_exists=1
  else
    [ ! -e "$resume_dir" ] && [ ! -L "$resume_dir" ] || exit 1
  fi
  resume_marker="$resume_canonical/.codex-review-loop.marker"
  resume_tombstone="$resume_canonical/.codex-review-loop.cleanup"
  resume_bootstrap="$resume_canonical/.codex-review-loop.lock-bootstrap"
  resume_external_tombstone="$resume_canonical.cleanup"
  resume_state="$resume_canonical/codex-loop-state.json"
  resume_publish_temp_present=0
  resume_empty_root=0
  resume_marker_only=0
  for resume_publish_temp in \
      "$resume_canonical"/.codex-review-loop.marker.tmp.* \
      "$resume_canonical"/.codex-review-loop.lock-bootstrap.tmp.*; do
    [ -e "$resume_publish_temp" ] || [ -L "$resume_publish_temp" ] || continue
    [ -f "$resume_publish_temp" ] && [ ! -L "$resume_publish_temp" ] && [ -O "$resume_publish_temp" ] || exit 1
    publish_temp_links="$(stat -c '%h' "$resume_publish_temp" 2>/dev/null || stat -f '%l' "$resume_publish_temp" 2>/dev/null)" || exit 1
    [ "$publish_temp_links" = 1 ] || exit 1
    publish_temp_mode="$(stat -c '%a' "$resume_publish_temp" 2>/dev/null || stat -f '%Lp' "$resume_publish_temp" 2>/dev/null)" || exit 1
    case "$publish_temp_mode" in 600|0600) ;; *) exit 1 ;; esac
    resume_publish_temp_present=1
  done
  if [ "$resume_root_exists" -eq 1 ] && [ -z "$(find "$resume_canonical" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    resume_empty_root=1
  fi
  if [ "$resume_root_exists" -eq 1 ] && [ -f "$resume_marker" ] && [ ! -L "$resume_marker" ] && [ ! -e "$resume_state" ] && [ ! -e "$resume_bootstrap" ] && [ "$(find "$resume_canonical" -mindepth 1 -maxdepth 1 -print | wc -l)" -eq 1 ]; then
    resume_marker_only=1
  fi
  resume_terminal_state=0
  if [ "$resume_root_exists" -eq 1 ] && [ -f "$resume_state" ] && [ ! -L "$resume_state" ]; then
    if jq -e '(.status == "converged" or .status == "capped" or .status == "stagnated" or .status == "aborted") and .runnersStopped == true' "$resume_state" >/dev/null 2>&1; then
      resume_terminal_state=1
    fi
  fi
  if [ "$resume_root_exists" -eq 0 ]; then
    [ -f "$resume_external_tombstone" ] && [ ! -L "$resume_external_tombstone" ] || exit 1
    resume_cleanup_only=1
  elif [ "$resume_terminal_state" -eq 1 ] || [ "$resume_empty_root" -eq 1 ] || [ "$resume_marker_only" -eq 1 ] || [ "$resume_publish_temp_present" -eq 1 ] || [ -e "$resume_tombstone" ] || [ -L "$resume_tombstone" ] || [ -e "$resume_bootstrap" ] || [ -L "$resume_bootstrap" ] || [ -e "$resume_external_tombstone" ] || [ -L "$resume_external_tombstone" ]; then
    if [ -e "$resume_external_tombstone" ] || [ -L "$resume_external_tombstone" ]; then
      [ -f "$resume_external_tombstone" ] && [ ! -L "$resume_external_tombstone" ] && [ -O "$resume_external_tombstone" ] || exit 1
      external_links="$(stat -c '%h' "$resume_external_tombstone" 2>/dev/null || stat -f '%l' "$resume_external_tombstone" 2>/dev/null)" || exit 1
      [ "$external_links" = 1 ] || exit 1
      external_mode="$(stat -c '%a' "$resume_external_tombstone" 2>/dev/null || stat -f '%Lp' "$resume_external_tombstone" 2>/dev/null)" || exit 1
      case "$external_mode" in 600|0600) ;; *) exit 1 ;; esac
      grep -Fxq "codex-review-loop:$resume_canonical" "$resume_external_tombstone" || exit 1
    fi
    if [ -e "$resume_tombstone" ] || [ -L "$resume_tombstone" ]; then
      [ -f "$resume_tombstone" ] && [ ! -L "$resume_tombstone" ] && [ -O "$resume_tombstone" ] || exit 1
      resume_tombstone_links="$(stat -c '%h' "$resume_tombstone" 2>/dev/null || stat -f '%l' "$resume_tombstone" 2>/dev/null)" || exit 1
      [ "$resume_tombstone_links" = 1 ] || exit 1
      resume_tombstone_mode="$(stat -c '%a' "$resume_tombstone" 2>/dev/null || stat -f '%Lp' "$resume_tombstone" 2>/dev/null)" || exit 1
      case "$resume_tombstone_mode" in 600|0600) ;; *) exit 1 ;; esac
      [ "$(cat "$resume_tombstone")" = "codex-review-loop:$resume_canonical" ] || exit 1
    fi
    if [ -e "$resume_marker" ] || [ -L "$resume_marker" ]; then
      [ -f "$resume_marker" ] && [ ! -L "$resume_marker" ] && [ -O "$resume_marker" ] || exit 1
      [ "$(cat "$resume_marker")" = "codex-review-loop:$resume_canonical" ] || exit 1
      resume_marker_mode="$(stat -c '%a' "$resume_marker" 2>/dev/null || stat -f '%Lp' "$resume_marker" 2>/dev/null)" || exit 1
      case "$resume_marker_mode" in 600|0600) ;; *) exit 1 ;; esac
    fi
    if [ -e "$resume_bootstrap" ] || [ -L "$resume_bootstrap" ]; then
      [ -f "$resume_bootstrap" ] && [ ! -L "$resume_bootstrap" ] && [ -O "$resume_bootstrap" ] || exit 1
      bootstrap_links="$(stat -c '%h' "$resume_bootstrap" 2>/dev/null || stat -f '%l' "$resume_bootstrap" 2>/dev/null)" || exit 1
      [ "$bootstrap_links" = 1 ] || exit 1
      bootstrap_mode="$(stat -c '%a' "$resume_bootstrap" 2>/dev/null || stat -f '%Lp' "$resume_bootstrap" 2>/dev/null)" || exit 1
      case "$bootstrap_mode" in 600|0600) ;; *) exit 1 ;; esac
      bootstrap_ref="$(sed -n 's/^lockRef://p' "$resume_bootstrap")"
      bootstrap_loop_id="$(sed -n 's/^loopId://p' "$resume_bootstrap")"
      bootstrap_owner="$(sed -n 's/^owner://p' "$resume_bootstrap")"
      bootstrap_object="$(sed -n 's/^lockObject://p' "$resume_bootstrap")"
      case "$bootstrap_ref" in refs/heads/codex-review-locks/mr-[0-9]*) ;; *) exit 1 ;; esac
      bootstrap_remote="$(sed -n 's/^lockRemote://p' "$resume_bootstrap")"
      [ -n "$bootstrap_remote" ] && [ -n "$bootstrap_loop_id" ] && [ -n "$bootstrap_owner" ] || exit 1
      case "$bootstrap_object" in ''|*[!0-9a-fA-F]*) exit 1 ;; esac
      [ "${#bootstrap_object}" -ge 40 ] && [ "${#bootstrap_object}" -le 64 ] || exit 1
    fi
    resume_cleanup_only=1
  else
    [ -f "$resume_marker" ] && [ ! -L "$resume_marker" ] && [ -O "$resume_marker" ] || exit 1
    [ "$(cat "$resume_marker")" = "codex-review-loop:$resume_canonical" ] || exit 1
    resume_marker_mode="$(stat -c '%a' "$resume_marker" 2>/dev/null || stat -f '%Lp' "$resume_marker" 2>/dev/null)" || exit 1
    case "$resume_marker_mode" in 600|0600) ;; *) exit 1 ;; esac
    [ -f "$resume_state" ] && [ ! -L "$resume_state" ] || {
      echo "resume state file is missing or symlinked" >&2
      exit 1
    }
    MR="$(jq -r 'if (.mr|type == "number" and . >= 1 and floor == .) then (.mr|tostring) else empty end' "$resume_state")" || exit 1
  fi
  CODEX_REVIEW_LOOP_DIR="$resume_canonical"
  export CODEX_REVIEW_LOOP_DIR
fi
if [ "$resume_cleanup_only" -eq 1 ]; then
  recovery_root="$resume_canonical"
  recovery_remote="${CODEX_REVIEW_LOOP_REMOTE:-origin}"
  recovery_ref="refs/heads/codex-review-locks/mr-${MR:-0}"
  recovery_lock=""
  recovery_loop_id=""
  recovery_owner=""
  recovery_pre_lock=0
  if [ -f "$recovery_root.cleanup" ] && [ ! -L "$recovery_root.cleanup" ]; then
    recovery_token_root="$(sed -n 's/^codex-review-loop://p' "$recovery_root.cleanup")"
    [ "$recovery_token_root" = "$recovery_root" ] || exit 1
    if grep -Fxq 'phase:pre-lock' "$recovery_root.cleanup"; then
      [ "$(cat "$recovery_root.cleanup")" = "codex-review-loop:$recovery_root
phase:pre-lock" ] || exit 1
      recovery_pre_lock=1
    else
      recovery_remote="$(sed -n 's/^lockRemote://p' "$recovery_root.cleanup")"
      recovery_ref="$(sed -n 's/^lockRef://p' "$recovery_root.cleanup")"
      recovery_lock="$(sed -n 's/^lockObject://p' "$recovery_root.cleanup")"
      recovery_loop_id="$(sed -n 's/^loopId://p' "$recovery_root.cleanup")"
      recovery_owner="$(sed -n 's/^owner://p' "$recovery_root.cleanup")"
      [ -n "$recovery_remote" ] && [ -n "$recovery_loop_id" ] && [ -n "$recovery_owner" ] || exit 1
      case "$recovery_ref" in refs/heads/codex-review-locks/mr-[0-9]*) ;; *) exit 1 ;; esac
      case "$recovery_lock" in ''|*[!0-9a-fA-F]*) exit 1 ;; esac
      [ "${#recovery_lock}" -ge 40 ] && [ "${#recovery_lock}" -le 64 ] || exit 1
    fi
  fi
  if [ -f "$recovery_root/codex-loop-state.json" ] && [ ! -L "$recovery_root/codex-loop-state.json" ]; then
    recovery_remote="$(jq -r '.lockRemote // "origin"' "$recovery_root/codex-loop-state.json")" || exit 1
    recovery_ref="$(jq -r '.lockRef // empty' "$recovery_root/codex-loop-state.json")" || exit 1
    recovery_lock="$(jq -r '.lockObject // empty' "$recovery_root/codex-loop-state.json")" || exit 1
    recovery_loop_id="$(jq -r '.loopId // empty' "$recovery_root/codex-loop-state.json")" || exit 1
    recovery_owner="$(jq -r '.owner // empty' "$recovery_root/codex-loop-state.json")" || exit 1
  elif [ -f "$recovery_root/.codex-review-loop.lock-bootstrap" ] && [ ! -L "$recovery_root/.codex-review-loop.lock-bootstrap" ]; then
    recovery_remote="$(sed -n 's/^lockRemote://p' "$recovery_root/.codex-review-loop.lock-bootstrap")"
    recovery_ref="$(sed -n 's/^lockRef://p' "$recovery_root/.codex-review-loop.lock-bootstrap")"
    recovery_lock="$(sed -n 's/^lockObject://p' "$recovery_root/.codex-review-loop.lock-bootstrap")"
    recovery_loop_id="$(sed -n 's/^loopId://p' "$recovery_root/.codex-review-loop.lock-bootstrap")"
    recovery_owner="$(sed -n 's/^owner://p' "$recovery_root/.codex-review-loop.lock-bootstrap")"
  elif [ -f "$recovery_root.cleanup" ] && [ ! -L "$recovery_root.cleanup" ]; then
    recovery_remote="$(sed -n 's/^lockRemote://p' "$recovery_root.cleanup")"
    recovery_ref="$(sed -n 's/^lockRef://p' "$recovery_root.cleanup")"
    recovery_lock="$(sed -n 's/^lockObject://p' "$recovery_root.cleanup")"
    recovery_loop_id="$(sed -n 's/^loopId://p' "$recovery_root.cleanup")"
    recovery_owner="$(sed -n 's/^owner://p' "$recovery_root.cleanup")"
  fi
  if { [ "$resume_empty_root" -eq 1 ] || [ "$resume_marker_only" -eq 1 ] || [ "$resume_publish_temp_present" -eq 1 ]; } && [ ! -f "$recovery_root/codex-loop-state.json" ] && [ ! -f "$recovery_root/.codex-review-loop.lock-bootstrap" ] && [ ! -e "$recovery_root.cleanup" ]; then
    recovery_pre_lock=1
  fi
  recovery_bootstrap="$recovery_root/.codex-review-loop.lock-bootstrap"
  recovery_remote_handled=0
  if [ "$recovery_pre_lock" -eq 0 ] && [ -n "$recovery_lock" ]; then
    if python3 "$runtime_helper" classify-cleanup-authority \
        --root "$recovery_root" \
        --state "$recovery_root/codex-loop-state.json" \
        --external "$recovery_root.cleanup" \
        --remote "$recovery_remote" --ref "$recovery_ref" \
        --lock-object "$recovery_lock" --loop-id "$recovery_loop_id" \
        --owner "$recovery_owner" >/dev/null; then
      recovery_remote_handled=1
    else
      authority_rc=$?
      [ "$authority_rc" -eq 4 ] || exit "$authority_rc"
    fi
  fi
  if [ "$recovery_pre_lock" -eq 0 ] && [ "$recovery_remote_handled" -eq 0 ] && [ -f "$recovery_bootstrap" ]; then
    if python3 "$runtime_helper" lock reconcile-acquire \
        --remote "$recovery_remote" --ref "$recovery_ref" --lock-object "$recovery_lock" \
        --loop-id "$recovery_loop_id" --owner "$recovery_owner" \
        --recovery-file "$recovery_bootstrap" >/dev/null; then
      :
    else
      reconcile_rc=$?
      if [ "$reconcile_rc" -eq 3 ]; then
        echo "lock reconciliation capability is invalid; preserve $recovery_root without remote fallback" >&2
        exit 3
      fi
      observed_recovery="$(python3 "$runtime_helper" run --timeout 20 -- \
        git ls-remote "$recovery_remote" "$recovery_ref" 2>/dev/null || true)"
      observed_recovery="$(printf '%s\n' "$observed_recovery" | awk 'NR == 1 {print $1}')"
      if [ -z "$observed_recovery" ] || [ "$observed_recovery" = "$recovery_lock" ] || [ "$reconcile_rc" -eq 124 ]; then
        echo "initial lock outcome remains indeterminate; preserve $recovery_root" >&2
        exit 2
      fi
      if [ ! -f "$recovery_root/codex-loop-state.json" ] && \
          grep -Eq '^outcome:(pending|indeterminate)$' "$recovery_bootstrap"; then
        # A different live object proves this pre-state candidate cannot win.
        # Continue through durable pre-lock cleanup; it rechecks the other owner
        # and accepts only pending/indeterminate bootstrap payloads.
        recovery_pre_lock=1
      else
        # State/writer initialization or a normalized acquired bootstrap means
        # ownership loss needs explicit diagnosis; preserve every authority.
        echo "initial lock candidate no longer owns $recovery_ref; preserve $recovery_root" >&2
        exit 2
      fi
    fi
  fi
  # runtime.py keeps the sibling token until the directory has been removed;
  # this closes the unlink/rmdir crash window. MR may remain set in the shell.
  recovery_cleanup_mode=()
  [ "$recovery_pre_lock" -eq 1 ] && recovery_cleanup_mode=(--pre-lock-only)
  python3 "$runtime_helper" cleanup \
    --root "$recovery_root" \
    --state "$recovery_root/codex-loop-state.json" \
    --marker "$recovery_root/.codex-review-loop.marker" \
    --internal "$recovery_root/.codex-review-loop.cleanup" \
    --external "$recovery_root.cleanup" \
    --remote "$recovery_remote" \
    --ref "$recovery_ref" \
    --lock-object "$recovery_lock" \
    --loop-id "$recovery_loop_id" \
    --owner "$recovery_owner" \
    "${recovery_cleanup_mode[@]}" || exit $?
  exit 0
fi
: "${MR:?set MR to the numeric merge-request IID before creating loop state}"
case "$MR" in ''|*[!0-9]*) echo "MR must be numeric" >&2; exit 1 ;; esac
max_rounds="${MAX_ROUNDS:-8}"
case "$max_rounds" in ''|*[!0-9]*|0) echo "MAX_ROUNDS must be a positive integer" >&2; exit 1 ;; esac
new_loop=0
if [ -n "${CODEX_REVIEW_LOOP_DIR:-}" ]; then
  loop_tmp="$CODEX_REVIEW_LOOP_DIR"
  case "$loop_tmp" in /*) ;; *) echo "loop dir must be absolute" >&2; exit 1 ;; esac
  [ -d "$loop_tmp" ] && [ ! -L "$loop_tmp" ] && [ -O "$loop_tmp" ] || exit 1
else
  if [ -n "${CODEX_REVIEW_LOOP_STATE_HOME:-}" ]; then
    loop_state_home="$CODEX_REVIEW_LOOP_STATE_HOME"
  elif [ -n "${XDG_STATE_HOME:-}" ]; then
    loop_state_home="$XDG_STATE_HOME/codex-review-loop"
  else
    : "${HOME:?HOME is required when XDG_STATE_HOME is unset}"
    loop_state_home="$HOME/.local/state/codex-review-loop"
  fi
  case "$loop_state_home" in /*) ;; *) echo "loop state home must be absolute" >&2; exit 1 ;; esac
  umask 077
  mkdir -p -- "$loop_state_home" || exit 1
  [ -d "$loop_state_home" ] && [ ! -L "$loop_state_home" ] && [ -O "$loop_state_home" ] || exit 1
  loop_state_home="$(cd -P -- "$loop_state_home" && pwd -P)" || exit 1
  loop_state_home_mode="$(stat -c '%a' "$loop_state_home" 2>/dev/null || stat -f '%Lp' "$loop_state_home" 2>/dev/null)" || exit 1
  case "$loop_state_home_mode" in 700|0700) ;; *) echo "loop state home must be mode 700" >&2; exit 1 ;; esac
  loop_tmp="$(mktemp -d "$loop_state_home/codex-review-loop.XXXXXX")" || exit 1
  chmod 700 "$loop_tmp"
  new_loop=1
fi
loop_tmp_canonical="$(cd -P -- "$loop_tmp" && pwd -P)" || exit 1
[ -d "$loop_tmp_canonical" ] && [ ! -L "$loop_tmp_canonical" ] && [ -O "$loop_tmp_canonical" ] || exit 1
loop_tmp="$loop_tmp_canonical"
if [ "$new_loop" -eq 1 ]; then
  python3 "$runtime_helper" sync-directory --directory "$loop_tmp_canonical" --parent || exit 1
fi
printf '%s\n' "$loop_tmp"  # carry this canonical path into every later command/turn
case "$(basename "$loop_tmp")" in codex-review-loop.*) ;; *) exit 1 ;; esac
loop_mode="$(stat -c '%a' "$loop_tmp" 2>/dev/null || stat -f '%Lp' "$loop_tmp" 2>/dev/null)" || exit 1
case "$loop_mode" in 700|0700) ;; *) echo "loop dir must be mode 700" >&2; exit 1 ;; esac
loop_marker="$loop_tmp/.codex-review-loop.marker"
cleanup_tombstone="$loop_tmp/.codex-review-loop.cleanup"
external_cleanup_tombstone="$loop_tmp.cleanup"
lock_bootstrap="$loop_tmp/.codex-review-loop.lock-bootstrap"
writer_recovery="$loop_tmp/.codex-review-loop.writer-recovery"
lock_remote="${CODEX_REVIEW_LOOP_REMOTE:-origin}"
lock_ref="refs/heads/codex-review-locks/mr-$MR"
if [ "$new_loop" -eq 1 ]; then
  [ ! -e "$loop_marker" ] && [ ! -L "$loop_marker" ] || exit 1
  [ ! -e "$external_cleanup_tombstone" ] && [ ! -L "$external_cleanup_tombstone" ] || exit 1
  python3 "$runtime_helper" init-marker \
    --root "$loop_tmp_canonical" --marker "$loop_marker" || exit 1
fi
[ -f "$loop_marker" ] && [ ! -L "$loop_marker" ] || exit 1
[ "$(cat "$loop_marker")" = "codex-review-loop:$loop_tmp_canonical" ] || exit 1
marker_mode="$(stat -c '%a' "$loop_marker" 2>/dev/null || stat -f '%Lp' "$loop_marker" 2>/dev/null)" || exit 1
case "$marker_mode" in 600|0600) ;; *) echo "loop marker must be mode 600" >&2; exit 1 ;; esac
state_file="$loop_tmp/codex-loop-state.json"
mr_disc="$loop_tmp/mr-disc.json"
codex_reply="$loop_tmp/codex-reply.md"
codex_receipt="$loop_tmp/codex-receipt.md"
if [ "$new_loop" -eq 1 ]; then
  loop_id="$(python3 -c 'import secrets; print(secrets.token_hex(16))')" || exit 1
  owner="$(id -un)@$(hostname 2>/dev/null || printf unknown)" || exit 1
  lock_object=""
  acquire_rc=0
  acquire_was_indeterminate=0
  if lock_object="$(python3 "$runtime_helper" lock acquire \
      --remote "$lock_remote" --ref "$lock_ref" --loop-id "$loop_id" --owner "$owner" \
      --recovery-file "$lock_bootstrap")"; then
    :
  else
    acquire_rc=$?
    [ "$acquire_rc" -eq 124 ] && acquire_was_indeterminate=1
    # The helper records the candidate before push. Reconcile by retrying the
    # exact same object: this is idempotent if the timed-out push committed and
    # non-force creation remains exclusive if it did not.
    bootstrap_object="$(sed -n 's/^lockObject://p' "$lock_bootstrap" 2>/dev/null || true)"
    if [ -n "$bootstrap_object" ]; then
      if lock_object="$(python3 "$runtime_helper" lock reconcile-acquire \
          --remote "$lock_remote" --ref "$lock_ref" --lock-object "$bootstrap_object" \
          --loop-id "$loop_id" --owner "$owner" --recovery-file "$lock_bootstrap")"; then
        acquire_rc=0
      else
        acquire_rc=$?
      fi
    fi
    if [ "$acquire_rc" -eq 124 ]; then
      echo "MR lock outcome is indeterminate; preserve $loop_tmp and retry reconcile-acquire" >&2
      exit 2
    fi
    if [ "$acquire_rc" -eq 3 ]; then
      echo "MR lock reconciliation capability is invalid; preserve $loop_tmp without remote fallback" >&2
      exit 3
    fi
    observed_lock_raw=""
    if ! observed_lock_raw="$(python3 "$runtime_helper" run --timeout 20 -- git ls-remote "$lock_remote" "$lock_ref" 2>/dev/null)"; then
      echo "MR lock status is indeterminate; preserve $loop_tmp for recovery" >&2
      exit 2
    fi
    observed_lock="$(printf '%s\n' "$observed_lock_raw" | awk 'NR == 1 {print $1}')"
    if [ -n "$bootstrap_object" ] && [ "$observed_lock" = "$bootstrap_object" ]; then
      lock_object="$bootstrap_object"
      acquire_rc=0
    elif [ "$acquire_was_indeterminate" -eq 1 ] && [ -z "$observed_lock" ]; then
      echo "initial lock push remains indeterminate; preserve $loop_tmp for later reconciliation" >&2
      exit 2
    else
      pre_lock_cleanup_rc=0
      python3 "$runtime_helper" cleanup \
        --root "$loop_tmp_canonical" --state "$state_file" --marker "$loop_marker" \
        --internal "$cleanup_tombstone" --external "$external_cleanup_tombstone" \
        --remote "$lock_remote" --ref "$lock_ref" --lock-object "$bootstrap_object" \
        --loop-id "$loop_id" --owner "$owner" --pre-lock-only || pre_lock_cleanup_rc=$?
      if [ "$pre_lock_cleanup_rc" -ne 0 ]; then
        echo "pre-lock cleanup is incomplete; preserve $loop_tmp_canonical" >&2
      fi
      exit 2
    fi
  fi

  abort_new_loop() {
    local cleanup_rc
    cleanup_rc=0
    python3 "$runtime_helper" cleanup \
      --root "$loop_tmp_canonical" --state "$state_file" --marker "$loop_marker" \
      --internal "$cleanup_tombstone" --external "$external_cleanup_tombstone" \
      --remote "$lock_remote" --ref "$lock_ref" --lock-object "$lock_object" \
      --loop-id "$loop_id" --owner "$owner" || cleanup_rc=$?
    if [ "$cleanup_rc" -ne 0 ]; then
      echo "initialization cleanup is incomplete; resume with CODEX_REVIEW_LOOP_DIR=$loop_tmp_canonical" >&2
      return "$cleanup_rc"
    fi
    return 0
  }
  if [ -e "$state_file" ] || [ -L "$state_file" ]; then
    abort_new_loop || exit $?
    exit 1
  fi
  if ! python3 "$runtime_helper" init-state \
      --root "$loop_tmp_canonical" --state "$state_file" --marker "$loop_marker" \
      --bootstrap "$lock_bootstrap" --writer-recovery "$writer_recovery" \
      --mr "$MR" --max-rounds "$max_rounds" \
      --loop-id "$loop_id" --owner "$owner" --remote "$lock_remote" \
      --ref "$lock_ref" --lock-object "$lock_object"; then
    abort_new_loop || exit $?
    exit 1
  fi
fi
[ -f "$state_file" ] && [ ! -L "$state_file" ] || exit 1
restore_loop_mr() {
  local saved_mr
  saved_mr="$(jq -r 'if (.mr|type == "number" and . >= 1 and floor == .) then (.mr|tostring) else empty end' "$state_file")" || return 1
  case "$saved_mr" in ''|*[!0-9]*) return 1 ;; esac
  if [ -n "${MR:-}" ] && [ "$MR" != "$saved_mr" ]; then
    echo "MR does not match the loop state" >&2
    return 1
  fi
  MR="$saved_mr"
  export MR
}
restore_loop_mr || exit 1
validate_loop_state() {
  jq -e --arg mr "$MR" --arg dir "$loop_tmp_canonical" '
      (.status) as $status |
      (.cleanupPhase) as $phase |
      type == "object" and
      (.mr == ($mr|tonumber)) and
      (.scratchDir == $dir) and
      (.loopId|type == "string" and length > 0) and
      (.owner|type == "string" and length > 0) and
      (.lockRemote|type == "string" and length > 0) and
      (.lockRef == ("refs/heads/codex-review-locks/mr-" + ($mr|tostring))) and
      (.lockObject|type == "string" and test("^[0-9a-fA-F]{40,64}$")) and
      (.maxRounds|type == "number" and . >= 1 and floor == .) and
      (.status|type == "string") and
      ((["active", "converged", "capped", "stagnated", "aborted"] | index($status)) != null) and
      (.runnersStopped|type == "boolean") and
      ((($status == "active") and (.runnersStopped == false)) or (($status != "active") and (.runnersStopped == true))) and
      (.generation|type == "number" and . >= 0 and floor == .) and
      (.writerId == null or (.writerId|type == "string" and length > 0)) and
      (.writerRecoveryHashes|type == "array" and length >= 1 and all(.[]; type == "string" and test("^[0-9a-f]{64}$"))) and
      (.fixRoundsDone|type == "number" and . >= 0 and floor == .) and
      (.awaitingReviewForSha == null or (.awaitingReviewForSha|type == "string")) and
      (.processedReviewShas|type == "array") and
      (.rebuttals|type == "object") and
      (.rebuttalOnlyStreak|type == "number" and . >= 0 and floor == .) and
      (.observabilityFailureStreak|type == "number" and . >= 0 and floor == .) and
      ((["active", "ready", "payloads_removed", "tombstone", "lock_released"] | index($phase)) != null)
    ' "$state_file" >/dev/null
}
if ! validate_loop_state; then
  echo "loop state identity or schema validation failed" >&2
  exit 1
fi
loop_id="$(jq -r '.loopId // empty' "$state_file")" || exit 1
owner="$(jq -r '.owner // empty' "$state_file")" || exit 1
lock_remote="$(jq -r '.lockRemote // empty' "$state_file")" || exit 1
lock_ref="$(jq -r '.lockRef // empty' "$state_file")" || exit 1
lock_object="$(jq -r '.lockObject // empty' "$state_file")" || exit 1
[ -n "$loop_id" ] && [ -n "$owner" ] && [ -n "$lock_remote" ] && [ -n "$lock_ref" ] && [ -n "$lock_object" ] || exit 1
assert_remote_lock() {
  # caller_writer is the immutable ID acquired by this runner. Never reconstruct
  # authority from the shared state's latest writerId: that lets a stale runner
  # impersonate its replacement.
  local caller_writer="$1" generation
  [ -n "$caller_writer" ] || { echo "caller writer ID is required" >&2; return 1; }
  generation="$(jq -r '.generation' "$state_file")" || return 1
  python3 "$runtime_helper" lock assert --remote "$lock_remote" --ref "$lock_ref" \
    --lock-object "$lock_object" --loop-id "$loop_id" --owner "$owner" \
    --state "$state_file" --writer-id "$caller_writer" --expected-generation "$generation"
}
guarded_external_impl() {
  # Read the expected fence before entering the helper. guarded-exec rechecks it
  # under the state flock and keeps that flock through the bounded command.
  local stdin_mode="$1" caller_writer="$2" timeout="$3" generation
  shift 3
  [ -n "$caller_writer" ] || { echo "caller writer ID is required" >&2; return 1; }
  generation="$(jq -r '.generation' "$state_file")" || return 1
  if [ "$stdin_mode" = 1 ]; then
    python3 "$runtime_helper" guarded-exec --state "$state_file" \
      --writer-id "$caller_writer" --expected-generation "$generation" \
      --remote "$lock_remote" --ref "$lock_ref" \
      --lock-object "$lock_object" --loop-id "$loop_id" --owner "$owner" \
      --timeout "$timeout" --stdin -- "$@"
    return $?
  fi
  python3 "$runtime_helper" guarded-exec --state "$state_file" \
    --writer-id "$caller_writer" --expected-generation "$generation" \
    --remote "$lock_remote" --ref "$lock_ref" \
    --lock-object "$lock_object" --loop-id "$loop_id" --owner "$owner" \
    --timeout "$timeout" -- "$@"
}
guarded_external() {
  local caller_writer="$1" timeout="$2"
  shift 2
  guarded_external_impl 0 "$caller_writer" "$timeout" "$@"
}
guarded_external_stdin() {
  local caller_writer="$1" timeout="$2"
  shift 2
  guarded_external_impl 1 "$caller_writer" "$timeout" "$@"
}
chmod 600 "$state_file" || exit 1
assert_private_artifact() {
  local artifact="$1" links
  if [ -e "$artifact" ] || [ -L "$artifact" ]; then
    [ -f "$artifact" ] && [ ! -L "$artifact" ] && [ -O "$artifact" ] || return 1
    links="$(stat -c '%h' "$artifact" 2>/dev/null || stat -f '%l' "$artifact" 2>/dev/null)" || return 1
    [ "$links" = 1 ] || return 1
  fi
}
for artifact in "$mr_disc" "$codex_reply" "$codex_receipt" "$state_file.lock" "$writer_recovery"; do
  assert_private_artifact "$artifact" || exit 1
done
```

Keep `loop_tmp` private to the current loop and do not place it in the repository.
When updating state, invoke the included runtime helper; it writes a unique 0600
temporary file, takes the state lock, and atomically replaces `state_file`. Never
rewrite a shared fixed path. Do not install an `EXIT` trap here: the first setup shell
would delete the directory before the next command.
`status` starts as `active` and `runnersStopped` stays `false` while a watcher or
fallback may still be running. After stopping every watcher/fallback and recording
the terminal report, atomically update the state to one of `converged`, `capped`,
`stagnated`, or `aborted` and set `runnersStopped` to `true`; validate the resulting
state before cleanup. An active state must never be cleaned up.
The observabilityFailureStreak field starts at `0`, is reset to `0` after a valid notes
response, and is atomically incremented for exit 4 or exit 5 before
deciding whether to re-arm. Re-arm only when the previous value is below `2`; once
the value is `2`, leave the long fallback as the only timer and surface the outage.
The current session runner generation is the single logical writer: its round and
watcher use the retained ID, and its fallback handoff carries that same capability. It uses the
helper once for each failed observation and once for each valid notes response. The
versions lookup never resets the streak; it only supplies the stable push-time anchor.
Persist the transition before the re-arm decision; the helper's lock, generation, and
writer-ID checks apply to every state update:

```bash
claim_writer() {
  # expected_writer is CAS state only. caller_writer is the immutable capability
  # retained by the previous runner/handoff and must never be read from state here.
  local expected_writer="$1" caller_writer="$2" new_writer generation
  new_writer="runner-$(python3 -c 'import secrets; print(secrets.token_hex(12))')" || return 1
  generation="$(jq -r '.generation' "$state_file")" || return 1
  python3 "$runtime_helper" state-update \
    --state "$state_file" --operation writer \
    --expected-generation "$generation" \
    --expected-writer "$expected_writer" --caller-writer-id "$caller_writer" \
    --writer-id "$new_writer" || return 1
  printf '%s\n' "$new_writer"
}

persist_observability_failure_streak() {
  local writer_id="$1" value="$2" generation
  case "$value" in ''|*[!0-9]*) return 1 ;; esac
  assert_private_artifact "$state_file" || return 1
  generation="$(jq -r '.generation' "$state_file")" || return 1
  python3 "$runtime_helper" state-update \
    --state "$state_file" --operation observability \
    --expected-generation "$generation" \
    --writer-id "$writer_id" --value "$value"
}

persist_review_transition() {
  local writer_id="$1" generation="$2" review_sha="$3"
  python3 "$runtime_helper" state-update \
    --state "$state_file" --operation round \
    --expected-generation "$generation" --writer-id "$writer_id" \
    --processed-review-sha "$review_sha"
}

persist_push_transition() {
  local writer_id="$1" generation="$2" pushed_sha="$3"
  python3 "$runtime_helper" state-update \
    --state "$state_file" --operation round \
    --expected-generation "$generation" --writer-id "$writer_id" \
    --awaiting-review-sha "$pushed_sha"
}

persist_rebuttal_transition() {
  local writer_id="$1" generation="$2" finding_slug="$3" evidence="$4"
  python3 "$runtime_helper" state-update \
    --state "$state_file" --operation round \
    --expected-generation "$generation" --writer-id "$writer_id" \
    --rebuttal-key "$finding_slug" --rebuttal-evidence "$evidence" \
    --increment-rebuttal-only
}

record_observability_failure() {
  local writer_id="$1" streak next_streak
  streak="$(jq -r '.observabilityFailureStreak' "$state_file")" || return 2
  case "$streak" in ''|*[!0-9]*) return 2 ;; esac
  if [ "$streak" -ge 2 ]; then
    return 1
  fi
  next_streak=$((streak + 1))
  persist_observability_failure_streak "$writer_id" "$next_streak" || return 2
  [ "$next_streak" -ge 2 ] && return 1
  return 0
}

# The active session runner first claims a fresh `writerId` with
# `claim_writer` after the previous runner is stopped. It calls
# `record_observability_failure "$writer_id"` once for a failed observation.
# Return 0 when a short re-arm is allowed, 1 when the cap leaves only the long
# fallback, and 2 when the state transition itself failed.
# After every valid notes response, call:
# persist_observability_failure_streak "$writer_id" 0 || exit 1

persist_terminal_state() {
  local writer_id="$1" status="$2" generation
  generation="$(jq -r '.generation' "$state_file")" || return 1
  python3 "$runtime_helper" state-update \
    --state "$state_file" --operation terminal --status "$status" \
    --expected-generation "$generation" --writer-id "$writer_id"
}
```
`cleanupPhase` is a terminal-state protocol, not a comment-only hint. The terminal
transition first stops the active state atomically; guarded cleanup then changes the
phase from `active` to `ready`, payload removal changes it to `payloads_removed`, and
a private cleanup tombstone changes it to `tombstone`. Remote lock release is a
compare-and-delete operation; after remote handling returns `missing`, `released`, or
the terminal-only `replaced` result, the helper records `lock_released` before
state/marker deletion, then atomically upgrades the sibling recovery token with
`phase:lock-released` and fsyncs its directory before deleting state. Here
`lock_released` means the old loop's remote-lock handling is complete, not necessarily
that this invocation deleted the ref. A state-less retry may skip the remote ref only
when this exact phase-qualified token matches the old loop identity; an ordinary or
malformed token remains strict, and bootstrap recovery with a still-present state file
rejects the phase token unless that state itself is already `lock_released`. If a process dies
between the release and that state write, retry treats a missing exact lock as an
already-completed release. If a new owner has already recreated the ref, a terminal,
stopped cleanup leaves that different object untouched and advances only its local
phase; before terminal stop, a different object remains a hard error. If any deletion
fails, the tombstone (and the state file
when it still exists) remains; a later invocation recognizes the tombstone and retries
only the known artifacts. This makes cleanup idempotent and recoverable after a
failure between two unlink operations.

After stopping the watcher/fallback and recording the terminal report, explicitly run
the guarded cleanup below once; keep the directory until then so resume can recover.
The included runtime owns every phase transition and takes an OS file lock around
each generation increment:

```bash
persist_cleanup_phase() {
  local writer_id="$1" phase="$2" generation
  generation="$(jq -r '.generation' "$state_file")" || return 1
  python3 "$runtime_helper" state-update --state "$state_file" --operation cleanup \
    --phase "$phase" --writer-id "$writer_id" --expected-generation "$generation"
}

cleanup_loop() {
  validate_loop_state || return 1
  python3 "$runtime_helper" cleanup \
    --root "$loop_tmp_canonical" \
    --state "$state_file" \
    --marker "$loop_marker" \
    --internal "$cleanup_tombstone" \
    --external "$external_cleanup_tombstone" \
    --remote "$lock_remote" \
    --ref "$lock_ref" \
    --lock-object "$lock_object" \
    --loop-id "$loop_id" \
    --owner "$owner"
}
cleanup_loop
```

`runtime.py` creates the sibling recovery token before deleting any payload. It then
advances `active → ready → payloads_removed → tombstone → lock_released`, releases the
exact remote lock with CAS, removes state/marker/internal token, and calls `rmdir`. The sibling token is
removed only after `rmdir` succeeds; if the process stops between any unlink or after
`rmdir`, a later invocation validates that token and finishes idempotently. Unknown,
symlinked, or multiply-linked artifacts abort cleanup without deleting anything else.

Cleanup removes only the loop's known regular artifacts and then uses `rmdir`; it
never recursively deletes a caller-supplied directory or an unexpected file.
Never run cleanup on a caller-supplied directory that lacks the marker or the
validated internal/sibling recovery token, exact canonical path, owner, and mode-0700
checks above. A startup path with a valid recovery token must validate the same name,
owner, mode, link count, and exact canonical contents, then remove only the allowlisted
artifacts and retry `rmdir`; it must not accept a new MR for that directory.
Watcher and fallback payloads must carry this absolute `state_file` (and awaited SHA),
not reconstruct a path from a different state-home or temporary directory in a later shell.
Before writing any fixed reply/receipt/snapshot path, call `assert_private_artifact`
and reject symlinks, non-regular files, and link counts other than one. Prefer a
fresh `mktemp` file inside `loop_tmp` for command output, then validate the fixed
destination and atomically replace it.

## Pipeline lag — batch, don't drip

The bot review takes 5–8 minutes; a fix push takes ~2. So the in-flight review is
almost always for the **previous** head, and pushing per-finding makes each review
surface work one sha late — an 8-round drip that could have been 2–3 batches.

Rules:

- **Before pushing fixes, check for an in-flight review** (a `starting for mr_update @
  <sha>` note without its completed counterpart). If one is running, HOLD the push:
  keep the fixes uncommitted, arm the step-5 watch for that review, triage its findings
  into the same batch, then commit+push ONCE.
- A review that lands for an older sha is still triaged normally — findings already
  resolved by uncommitted or pushed fixes get a reply (with the fixing sha), not a
  re-fix.
- Watch/wakeup prompts must carry the batch context: which fixes sit uncommitted,
  which review is being awaited.
- **Stale-review timeout:** the bot's own starting note says it gives up at 30m. If a
  `starting` note is older than ~30 minutes with no completed counterpart, treat that
  review as failed: report it, stop awaiting it, and release the held batch (push).
  Never let one dead review pin fixes uncommitted forever.

## Round procedure

Before the first fetch in a new or resumed Codex turn, claim one session writer and
retain it for the round, watcher, and fallback handoff. A resumed wakeup must carry
`CODEX_REVIEW_LOOP_CALLER_WRITER` from the scheduling turn; never reconstruct it from
`.writerId`. Stop the previous known watcher/task before claiming its successor.

```bash
previous_writer="$(jq -r '.writerId // "__null__"' "$state_file")" || exit 1
caller_writer="${CODEX_REVIEW_LOOP_CALLER_WRITER:-}"
if [ "$previous_writer" = "__null__" ]; then
  [ -z "$caller_writer" ] || { echo "initial writer claim has an unexpected predecessor" >&2; exit 1; }
  writer_id="$(claim_writer "$previous_writer" "$caller_writer")" || exit 1
elif [ -z "$caller_writer" ]; then
  # Durable recovery is only valid after the operator/runtime has proved that
  # previous session, watcher, and fallback tasks are stopped.
  recovery_generation="$(jq -r '.generation' "$state_file")" || exit 1
  writer_id="$(python3 "$runtime_helper" recover-writer \
    --state "$state_file" --writer-recovery "$writer_recovery" \
    --expected-generation "$recovery_generation" --expected-writer "$previous_writer")" || exit 1
else
  [ "$caller_writer" = "$previous_writer" ] || {
    echo "writer handoff capability is missing or stale; do not read writerId as authority" >&2
    exit 1
  }
  writer_id="$(claim_writer "$previous_writer" "$caller_writer")" || exit 1
fi
export CODEX_REVIEW_LOOP_CALLER_WRITER="$writer_id"
```

### 1. Fetch findings

```bash
# --paginate: GitLab caps per_page at 100; long loops (reviews + findings + replies)
# exceed it and a single page silently drops the newest reviews or oldest receipts.
# --paginate emits one JSON array per page — merge with jq -s 'add'.
# Preserve the previous good snapshot if either the API or JSON validation fails.
set -o pipefail
: "${writer_id:?active review-loop writer ID must be retained by the caller}"
assert_remote_lock "$writer_id" || exit 1
restore_loop_mr || exit 1
mr_pages="$(mktemp "$loop_tmp/mr-disc.pages.XXXXXX")" || exit 1
mr_parsed="$(mktemp "$loop_tmp/mr-disc.parsed.XXXXXX")" || {
  python3 "$runtime_helper" remove-fetch-temps --root "$loop_tmp" --path "$mr_pages"
  exit 1
}
chmod 600 "$mr_pages" "$mr_parsed" || exit 1
assert_remote_lock "$writer_id" || exit 1
if ! guarded_external "$writer_id" 20 glab api --paginate "projects/:id/merge_requests/$MR/discussions?per_page=100" > "$mr_pages"; then
  python3 "$runtime_helper" remove-fetch-temps --root "$loop_tmp" --path "$mr_pages" --path "$mr_parsed"
  echo "discussion fetch failed; do not treat it as an empty review" >&2
  exit 1
fi
if ! jq -s -e 'if all(.[]; type == "array") then (add | if type == "array" then . else error("merged discussions are not an array") end) else error("a discussion page is not an array") end' \
    "$mr_pages" > "$mr_parsed"; then
  python3 "$runtime_helper" remove-fetch-temps --root "$loop_tmp" --path "$mr_pages" --path "$mr_parsed"
  echo "discussion payload validation failed; do not triage a partial snapshot" >&2
  exit 1
fi
chmod 600 "$mr_pages" "$mr_parsed"
assert_private_artifact "$mr_disc" || exit 1
mv -f -- "$mr_parsed" "$mr_disc"
python3 "$runtime_helper" remove-fetch-temps --root "$loop_tmp" --path "$mr_pages"
```

Parse notes for `Codex code review` + the awaited sha. Severity detail notes follow the
summary note in separate discussions (match by sha + severity emoji). To find pending
work independent of local state, list completed 🔎 summary notes whose discussion has NO
`🧾 트리아지 완료` receipt reply — those reviews are untriaged, regardless of what any
prompt or state file claims. A 0-finding convergence summary is only a **candidate
verification signal** until every earlier non-zero review has its fixed/rebutted
findings recorded and its summary discussion receipt + resolve completed.
A review counts as DONE only when its receipt exists AND its summary discussion is
resolved — a receipted-but-unresolved summary means the turn died between the receipt
POST and the resolve PUT: finish that resolve first on resume (it is an unfinished
close, not pending triage). Note: pipe JSON
through a python script **file** (or `python3 /dev/fd/3 3<<'EOF'` heredoc) — an inline
`python3 -c '<korean text>'` heredoc fails on UTF-8 source encoding.

### 2. Triage — rebut before fixing (Advisor contract)

For every Critical/Major, attempt a rebuttal against the **current code** first; fix
only survivors. Minors: fix if cheap and real, rebut if design-intent. Recurring
rebuttal categories from practice:

- **Ported byte-identical files** (see the port covenant in `frontend/README.md`):
  - Bug also present in the origin repo and not triggered by our usage → do NOT fork-fix.
    Reply "upstream bug, fix lands via re-copy", record it in README known-issues.
  - Bug triggered because *our usage exceeds the origin's assumptions* → fix it and
    register the file in the README adaptation list with a header comment.
  - Unused ported file (no imports) → rebut with usage evidence; defer to consumption time.
- **Fact-check the premise**: codex sometimes asserts "this MR removed X" when X never
  existed in the origin. Verify against the origin repo before accepting.
- **Design intent**: locale-invariant technical labels, vanilla-parity operational
  identifiers, etc. Rebut with the design rationale.

### 3. Fix + verify

Apply fixes, then run the project verification relevant to the touched area
(frontend: `vue-tsc --noEmit` 0 errors + `vite build` green + `vitest`; backend:
scoped `:module:test`). Never push unverified.

### 4. Commit + push + reply

- Commit message: `수정: Codex N차 리뷰 반영 — <요약>` (이 저장소의 한국어 커밋 규칙).
- Every GitLab read/write and every `git commit`/remote Git command must run through
  `guarded_external "$writer_id"` (or `guarded_external_stdin "$writer_id"` for piped
  request bodies). The caller must retain the immutable ID it acquired; never read the
  latest state writer as caller authority. The helper
  holds the state flock from writer/generation + remote-lock validation through the
  bounded command and performs a second remote-lock check afterward, so a replacement
  local writer cannot pass a check and act afterward. Remote ownership loss that
  persists through the post-check is surfaced as indeterminate; transient ABA remains
  outside this cooperative mutex's guarantees.
- Use `guarded_external "$writer_id" 120 git commit ...` and
  `guarded_external "$writer_id" 120 git push`; a
  missing or changed lock is a hard stop and must never be repaired by stealing the ref.
- Push triggers the next bot review automatically.
- Exit 124 from POST, PUT, or `git push` means the server-side result is uncertain.
  Never retry blindly: re-fetch discussions/receipts or the remote branch head through
  `guarded_external "$writer_id"`, determine whether the marker/ref transition already happened,
  and execute only the still-missing transition.
- Exit 3 after a command can mean the remote lock changed during the effect. The effect
  may already exist; apply the same marker/ref reconciliation and never retry blindly.
- **Comment bodies must never pass through shell interpolation.** Replies quote
  review text and code, which can contain backticks/`$()` — inside `-f body="..."`
  those execute locally before glab runs, with repo + GitLab credentials in scope.
  Always write the body to a temp file with a safe file-writing operation (for example
  `apply_patch` for a static body) or pass it through `jq --arg`; never interpolate
  raw review text into shell source, then:

  ```bash
  restore_loop_mr || exit 1
  jq -n --rawfile body "$codex_reply" '{body: $body}' |
    guarded_external_stdin "$writer_id" 20 glab api -X POST -H "Content-Type: application/json" \
      "projects/:id/merge_requests/$MR/discussions/$discussion_id/notes" --input -
  ```

- For each rebutted finding, reply on its discussion (file-based, as above) with body
  `수정하지 않음(근거): ...` — self-contained: claim, evidence, where it is documented.
- **For each FIXED finding, reply after the push succeeds, but defer resolving its
  discussion until a later review of the pushed SHA verifies that the finding is gone.**
  A reply records the fixing SHA; an immediate resolve would claim verification before
  the follow-up review.

  ```bash
  # 1) reply — bot notes are individual (non-resolvable) notes; the reply converts the
  #    note into a thread whose notes ARE resolvable. Include the fix sha.
  #    Body written to a file first: "✅ 반영됨 @ <fix sha> — <한 줄 요약>"
  restore_loop_mr || exit 1
  jq -n --rawfile body "$codex_reply" '{body: $body}' |
    guarded_external_stdin "$writer_id" 20 glab api -X POST -H "Content-Type: application/json" \
      "projects/:id/merge_requests/$MR/discussions/$discussion_id/notes" --input -
  # 2) after a later review verifies the pushed SHA, resolve the discussion.
  restore_loop_mr || exit 1
  guarded_external "$writer_id" 20 glab api -X PUT \
    "projects/:id/merge_requests/$MR/discussions/$discussion_id?resolved=true"
  ```

  Resolve only the severity-detail discussions (🟠/🟡 findings) that were actually fixed
  and later verified; leave pending severity details, the 🔎 summary notes, and rebutted
  findings unresolved. Rebuttals stay open for the reviewer/user to judge. The reply must
  name the fix SHA — that is the audit link.
- **Leave a triage receipt on the review's 🔎 summary note** once every fixed finding has
  passed a later-SHA verification and every rebuttal has been recorded — this is the
  cross-machine "processed" marker (state file is only a cache). Keep the summary
  unresolved while any finding is still pending.

  ```bash
  # Body via file for consistency: "🧾 트리아지 완료 — fixed <N> / rebutted <M> @ <fix sha>"
  restore_loop_mr || exit 1
  jq -n --rawfile body "$codex_receipt" '{body: $body}' |
    guarded_external_stdin "$writer_id" 20 glab api -X POST -H "Content-Type: application/json" \
      "projects/:id/merge_requests/$MR/discussions/$summary_discussion_id/notes" --input -
  ```

  Then **resolve the summary discussion too** (`PUT ...?resolved=true`) — only after the
  receipt is posted and all fixed finding discussions have the later-review evidence.
  Run `restore_loop_mr || exit 1` and the PUT through
  `guarded_external "$writer_id" 20 glab api`
  as well; a stale runner must not close a newer runner's summary.
  The receipt reply itself is the processed marker (rebuild reads receipt text, not
  resolve state), while resolving the summary is the final close for projects that
  require all discussions resolved. A convergence review (0 findings) needs no receipt
  of its own, but it does not terminate the loop until the receipt-derived reconciliation
  below proves that every earlier non-zero review is closed.
- **State transition (atomic, last) — two separate granularities:**
  - *Per review*: only after that review's finding replies, later-SHA verification,
    resolves, receipt, and summary resolve all succeeded, append the full GitLab review
    SHA to `processedReviewShas`. This external SHA need not exist in the local object DB.
  - *Per push*: exactly ONCE per actual push, no matter how many reviews the batch
    covered — set `awaitingReviewForSha` to the pushed local commit SHA and increment
    `fixRoundsDone`. A rebuttal-only round pushes nothing: skip the per-push transition
    entirely and write the receipt as `rebutted <M> @ <reviewed sha> (no push)`.

  Use the runtime's typed `round` operation for these fields; do not edit them with
  ad-hoc `jq` assignments. `persist_review_transition` appends a processed review,
  `persist_push_transition` sets the awaited SHA and increments the fix-round count,
  and `persist_rebuttal_transition` records evidence while incrementing the
  rebuttal-only streak. The runtime stores both domains as lowercase full SHAs: it
  accepts only a full GitLab review SHA, resolves a local push SHA through Git, and
  migrates matching legacy short values without spending another fix round. Each helper
  reads the current generation and supplies the caller-retained writer ID, so a stale
  watcher cannot overwrite another runner.

  If the turn dies mid-way, do NOT trust the state file on resume:
  re-derive from the MR (receipts + resolved threads) — done = receipt + resolved summary.
  Comment POSTs are NOT idempotent: on resume, check each discussion for an existing
  reply carrying the same marker (`✅ 반영됨`/`🧾 트리아지 완료`) + fix sha and POST only
  the missing ones; re-running resolve is harmless.

### 5. Arm the review watch

Do NOT poll on a fixed long-fallback timer — detection then lags up to the full
interval after the review lands. Two-layer setup instead:

1. **Background watch (primary signal)** — a Codex background command/task until-loop that
   exits the moment a terminal condition appears; the active runtime resumes the session
   immediately (~30s detection lag):

   ```bash
   # MR/SHA = awaited pushed head. Single active watcher — stop any previous
   # watch before arming; and do NOT layer Monitor on top (single mechanism).
   # since = SERVER-side push time of the awaited head: created_at of the MR
   # version (diff) whose head is $SHA. Not `git log %cI` — committer date is
   # when the commit was CREATED, not pushed; an old commit pushed late would
   # start with an already-expired deadline. Server-sourced also means the same
   # clock and the same ISO offset rendering as note.created_at, so the string
   # comparison in the stop-note baseline is safe (never mix a locally rendered
   # UTC-Z string with the server's offset format). Stable across re-arms.
   restore_loop_mr || exit 1
   : "${writer_id:?session writer ID must be retained before arming the watcher}"
   watcher_id="$writer_id"
   assert_remote_lock "$watcher_id" || exit 1
   handle_observability_failure() {
     record_observability_failure "$watcher_id"
     record_rc=$?
     case "$record_rc" in
       0|1) return 0 ;;
       *) return 1 ;;
     esac
   }
   assert_remote_lock "$watcher_id" || exit 1
   versions_json=$(guarded_external "$watcher_id" 20 glab api "projects/:id/merge_requests/$MR/versions" 2>/dev/null) || {
     handle_observability_failure || exit 1
     exit 5
   }
   since=$(printf '%s' "$versions_json" | jq -r --arg sha "$SHA" '[.[] | select(.head_commit_sha | startswith($sha))] | first | .created_at // empty') || {
     handle_observability_failure || exit 1
     exit 5
   }
   [ -n "$since" ] || {
     handle_observability_failure || exit 1
     exit 5
   }   # cannot establish push time — observability failure, do not guess
   # Stale clock anchors to the REVIEW, not the watcher. Until the review starts,
   # deadline = push time + 2100 (start timeout: bot never picked it up) — computed
   # from $since, NOT from arm-time now: every anchor must be derivable from stable
   # facts (push commit time, server-side created_at) so a re-armed watcher (after
   # exit 4, a false wake, or a session resume) recomputes the SAME deadline —
   # re-arming never resets or extends either clock. The moment a healthy poll
   # sees the awaited sha's in-progress note, the deadline re-anchors ONCE to that
   # note's created_at + 2100 — a queue-delayed review that starts late gets its
   # full 35m, not the tail of the start timeout. If the computed deadline is
   # already past at arm time, do not arm — run the exit-3 procedure directly.
   since_epoch="$(python3 "$runtime_helper" parse-iso "$since")" || {
     handle_observability_failure || exit 1
     exit 5
   }
   deadline=$(( since_epoch + 2100 ))
   anchored=0
   fails=0
   while :; do
     # timeout: a hung request would otherwise block the loop body — the deadline
     # check would never run, the fallback would see a live task and re-schedule
     # forever, and the loop would stall permanently.
     assert_remote_lock "$watcher_id" || exit 1
     notes=$(guarded_external "$watcher_id" 20 glab api "projects/:id/merge_requests/$MR/notes?order_by=created_at&sort=desc&per_page=100" 2>/dev/null)
     if printf '%s' "$notes" | jq -e 'type == "array"' >/dev/null 2>&1; then
       fails=0
       persist_observability_failure_streak "$watcher_id" 0 || exit 1
       sig=$(printf '%s' "$notes" | jq -r --arg sha "$SHA" --arg since "$since" '
         [.[] | select(.author.username == "codex")] as $bot
         | ([$bot[] | select(.body | test("^🔎 \\*\\*Codex code review\\*\\* for `mr_(open|update)` @ `" + $sha))]
            | length > 0) as $done
         | ([$bot[] | select(.body | test("^🔎 (\\*\\*)?Codex code review(\\*\\*)?( starting)? for `mr_(open|update)` @ `" + $sha))]
            | length > 0) as $started
         | ([$bot[] | select(.created_at > $since and (.body | startswith("⏭️ Codex auto-review stopped")))]
            | map(.created_at) | max // "") as $stop
         | ([$bot[] | select(.created_at > $since and (.body | startswith("🔄 Auto-review counter reset")))]
            | map(.created_at) | max // "") as $reset
         | if $done then "done"
           elif $stop != "" and $stop > $reset then "stopped"
           elif $stop != "" and $reset != "" and ($started | not) then "reset"
           else "wait" end')
       case "$sig" in
         done|stopped) exit 0 ;;  # summary landed / VALID budget stop — step 1 judges which
         reset) exit 6 ;;         # stop voided by a newer reset AND the review never
                                  # started — only then is waiting futile
       esac
       if [ "$anchored" -eq 0 ]; then
         start_at=$(printf '%s' "$notes" | jq -r --arg sha "$SHA" '[.[]
           | select(.author.username == "codex")
           | select(.body | test("^🔎 (\\*\\*)?Codex code review(\\*\\*)?( starting)? for `mr_(open|update)` @ `" + $sha))
           ] | first | .created_at // empty')
         if [ -n "$start_at" ]; then
           start_epoch="$(python3 "$runtime_helper" parse-iso "$start_at")" || {
             handle_observability_failure || exit 1
             exit 5
           }
           deadline=$(( start_epoch + 2100 )); anchored=1
         fi
       fi
       [ $(date +%s) -gt $deadline ] && exit 3   # HEALTHY responses, review overdue
     else
       fails=$((fails+1))                        # auth/network/5xx/rate-limit/bad JSON
       if [ "$fails" -ge 10 ]; then
         handle_observability_failure || exit 1
         exit 4
       fi                                      # ~5m of consecutive query failures
     fi
     sleep 30
   done
   ```

   - **Cover every terminal condition**: completed review note for the awaited sha,
     auto-review stop note, and the deadline. A success-only match hangs silently
     when the bot dies.
   - **Match the summary HEADER structurally and position-anchored (`test("^🔎 ...")`),
     never by substring co-occurrence** — every prior weaker form false-fired live:
     a naive grep broke on markdown (`**`/backticks) and `mr_open` vs `mr_update`;
     bare `contains` matched quote of trigger strings (a codex review OF this skill
     quoting `MAX_REVIEWS_PER_MR`; our OWN triage reply naming the fix sha — hence
     also **filter to bot-authored notes first**, `.author.username == "codex"`);
     and another head's review can mention the awaited sha mid-body, which would
     wake-loop since step 1 would just re-arm onto the same note. The completed
     summary always BEGINS with `🔎 **Codex code review** for \`mr_open|mr_update\`
     @ \`<sha>\`` — anchoring `^` + the literal `**` + the header sha excludes all
     of those (quotes sit mid-body; detail notes begin `🟠`; the `starting` note is
     the same note heartbeat-edited in place but reads `🔎 Codex code review
     starting for ...` without `**`, so no extra in-progress markers are needed).
     Do NOT guess the outcome wording after the header — observed variants include
     `3 issues found`, `1 issue found` (singular — a plural-only match spun a live
     watch to stale), and free-form convergence text; the verdict on WHAT landed
     belongs to step 1, not the watch. Residual risks stay asymmetric by
     construction: a false WAKE costs one step-1 fetch (in-flight → hold, re-arm
     with the anchored deadline); a false stale is recovered by the receipt-derived
     pending set — delayed, not lost.
   - **Anchoring is asymmetric by design**: the completed-note condition is sha-anchored
     with NO time baseline — re-arming after the review already landed must fire
     immediately (step 1 no-ops via receipts). The stop-note condition IS baselined,
     to the awaited head's PUSH time (not the watcher's arm time — a stop posted in
     the push→arm gap or before a fallback re-arm is still valid): after a counter
     reset + retrigger push, the OLD stop note still sits in recent notes and must
     not kill the new watch.
   - Exit 0 is a **signal, not a verdict**: always run step 1 (full fetch) and judge
     there — real review vs stop note vs quoted-text false-positive (cost of a false
     fire: one fetch, not a wrong termination). The "stop note older than the latest
     `🔄 Auto-review counter reset` note is void" rule is enforced INSIDE the watcher
     (the stop/reset latest-timestamp comparison above), not only in step 1 — a
     watcher that treats every post-push stop as terminal would re-fire on the same
     voided note after every re-arm: an endless wake/fetch/re-arm loop. **Exit 6**
     fires only when ALL three hold: a stop exists, a newer reset voided it, and the
     awaited sha's review never started ($started false) — then the head will not be
     re-reviewed retroactively and waiting is futile: the round proceeds to push (a
     new head triggers normally) or, with nothing to push, retriggers via the
     empty-commit path (Budget section). A reset alone (no stop — our push was never
     budget-blocked) or a reset landing while the awaited review is already running
     changes nothing: keep waiting. Control signals (stop/reset), like summaries,
     are matched ONLY on bot-authored notes — a user comment quoting the stop/reset
     prefix must not fake exit 0/6.
   - One API call per poll (`per_page=100`, newest first). If a burst of newer notes
     ever pushes the awaited summary off the page, the watch exits 3 (stale) and the
     next round's receipt-derived pending set still recovers the review — delayed,
     not lost.
   - **Exit codes separate "review overdue" from "cannot observe"** — they demand
     opposite reactions. Exit 3 fires only after a HEALTHY response confirms no
     terminal note past the deadline: apply the stale-review rule (Pipeline lag
     section) — report, stop awaiting, release any held batch. Two exit-3 flavors
     (step 1 tells them apart from the notes): review started and blew its 35m
     (`anchored=1` — the bot's own self-abandon case) vs review never started at
     all (start timeout — check for a stop note / budget exhaustion / bot outage
     before declaring anything dead). Exit 4 means the
     notes API itself was unreachable (auth expiry, network, rate limit, 5xx,
     malformed JSON) for ~5 minutes straight: the review may well be alive, so do
     NOT run the stale procedure and do NOT release a held batch — report the
     outage, check connectivity (`guarded_external "$writer_id" 20 glab auth status`), and update
     observabilityFailureStreak atomically before re-arming (the completed-note
     condition has no baseline, so anything that landed during the outage is caught
     on the first healthy poll; the review-anchored deadline also survives re-arms).
     Exit 5 (push time could not be established from the versions API) is the same
     family: observability failure, not review state — increment the same state field
     and re-arm only while its previous value is below 2, never stale. Exit 5 fires
     pre-sleep, so an unbounded report-and-re-arm turns a permanent failure (revoked
     auth, sustained 5xx) into an arm→exit→wake tight loop. Past the cap, stop
     re-arming — leave the long fallback as the only timer and surface the outage.
   - The hold-push wait (in-flight review, Pipeline lag section) reuses this same
     watch shape.
2. **Long fallback** — the Codex runtime's single-slot wakeup with a 2400-second delay.
   If no wakeup surface exists, record this as the next-turn handoff instead. It must stay LONGER
   than the watch deadline (2100s) so a slow/dead review surfaces via the watch's
   exit 3 first, never as a fallback racing a live watcher. The wakeup prompt must
   carry the full round procedure, the state file path, the awaited sha, and
   `CODEX_REVIEW_LOOP_CALLER_WRITER=$writer_id`. The fired turn uses that retained
   capability only as `caller_writer` for a fresh CAS claim; it never substitutes the
   current state value.
   The wakeup surface is single-slot — a new schedule REPLACES the pending
   one, so stale-generation fallbacks do not accumulate; still, if a fired fallback's
   sha differs from the state file's `awaitingReviewForSha`, treat it as stale:
   no-op and re-schedule for the CURRENT sha. When the fallback fires, branch on
   actual state:
   - primary watch task still running → re-schedule the fallback only; never arm a
     second watch.
   - watch gone, review pending (summary without receipt) → run the round procedure.
   - watch gone, NO summary — the lost-notification case (the watch exited 0/3/4/5/6
     but the event never reached the session) → re-fetch and re-derive in the SAME
     order as the watcher itself, from the same recomputable anchors: completed
     summary for the awaited sha → round procedure; valid stop (newer than any
     reset) → budget-exhaustion handling; stop voided by a newer reset AND review
     never started → the exit-6 procedure (push, or empty-commit retrigger — NOT
     stale, NOT re-arm: a fresh watcher would just exit 6 again); deadline (push
     time via versions API, or starting note created_at) already past → exit-3
     stale procedure; not past → re-arm the watch (same anchors, clock unaffected);
     notes/versions API failing → the exit-4/5 observability procedure.
   - watch gone, review already handled → do NOT "re-arm and end": the completed-note
     condition has no time baseline, so a fresh watcher on a processed sha fires
     instantly — re-arming loops wake/no-op/wake forever. Either the round just
     pushed (then the new sha's watch is armed by the round itself) or the loop is
     done (terminate: stop the fallback, arm nothing).

## Bot review budget (`MAX_REVIEWS_PER_MR`)

The bot reviews at most N pushes per MR (observed N=8), then posts
`⏭️ Codex auto-review stopped: this MR reached MAX_REVIEWS_PER_MR`. Handle it:

- **Detect** that note when the awaited review never starts — do not keep polling
  blindly; report to the user. Only a project member can reset the counter (the bot
  posts `🔄 Auto-review counter reset by @<user>` when they do). Ordering matters:
  a stop note older than the latest reset note is void, not a stop.
- **After a reset, the current head is NOT reviewed retroactively** — only newer
  pushes trigger. If there is nothing left to fix, retrigger with an empty commit:
  `guarded_external "$writer_id" 120 git commit --allow-empty -m "작업: Codex 리뷰 재트리거"`
  followed by `guarded_external "$writer_id" 120 git push`.
- Budget the loop: with per-finding drip pushes the 8-review budget burns in ~8 fixes;
  batching (above) also conserves review budget, not just wall-clock.

## Termination

Stop the watcher and long fallback, then report when ANY of:

1. **Convergence tone after reconciliation** — the review's Overall says it found
   nothing blocking (e.g. "병합을 막을 만한 이슈는 확인하지 못했다", "no issues found",
   0 findings). Before stopping, re-fetch the MR and process every earlier non-zero
   summary without a receipt: use this later review as the verification SHA, resolve
   only findings verified gone, post each receipt, resolve each summary, then re-fetch
   and assert `pending non-convergence summaries == 0`. A Minor-only round whose
   findings are all design-intent rebuttals qualifies only after the same reconciliation.
2. **Round cap** — `fixRoundsDone >= maxRounds`.
3. **Stagnation** — a rebuttal-only round produces no push, so no new review will come;
   treat as converged after replying (there is nothing left the loop can change).

Final report: table of rounds (findings → fixed/rebutted), verification status, and
links (MR, last pipeline if deployed). If rebutted finding threads remain open, list
them explicitly — they are deliberate merge blockers awaiting the user's judgment
(resolve or overrule), not loop failures; the loop never resolves them itself.

## Anti-patterns

- Fixing byte-identical ported files without registering the adaptation — silently
  breaks the re-copy sync covenant.
- Pushing without running verification because "the fix is trivial".
- Accepting a Major at face value — always attempt the rebuttal first; codex premises
  are sometimes factually wrong about what the MR changed.
- Polling with foreground `sleep` chains — blocks the session; the watch loop must be
  a background command/task. Likewise a fixed-interval fallback as the primary poll —
  detection lags up to the full interval; wakeup is the long fallback only.
- Re-arguing a finding already in `rebuttals` — reply with the recorded evidence.
- Calling `PUT ...?resolved=true` on a bot note directly — codex notes are individual
  (non-resolvable) notes and the call 400s. Reply first (converts to a resolvable
  thread), then resolve.
- Resolving a finding thread before a later review verifies the pushed fix — the resolve
  is a claim that the fix SHA named in the reply actually removes the finding.
