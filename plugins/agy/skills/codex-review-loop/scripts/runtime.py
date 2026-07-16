#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex 리뷰 루프 스킬을 위한 안전한 런타임 프리미티브.

이 스킬은 주로 워크플로우 안내이지만, 스크래치 상태와 MR 소유권은 실행 가능하고 테스트 가능한
프리미티브가 필요합니다. 이 모듈은 의도적으로 GitLab 클라이언트 로직이 없으며,
로컬 상태 프로토콜과 소형 Git-ref 뮤텍스만을 소유합니다.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import signal
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Iterator, Literal


TERMINAL_STATUSES = {"converged", "capped", "stagnated", "aborted"}
CLEANUP_PHASES = {"active", "ready", "payloads_removed", "tombstone", "lock_released"}
CLEANUP_NEXT_PHASE = {
    "active": "ready",
    "ready": "payloads_removed",
    "payloads_removed": "tombstone",
    "tombstone": "lock_released",
}
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
GIT_COMMAND_TIMEOUT_SECONDS = 20.0
PROCESS_GROUP_GRACE_SECONDS = 1.0
PR_SET_CHILD_SUBREAPER = 36
_SUBREAPER_ENABLED: bool | None = None
PRIVATE_ARTIFACT_TEMP_PATTERNS = (
    ".codex-review-loop.marker.tmp.*",
    ".codex-review-loop.cleanup.tmp.*",
    ".codex-review-loop.lock-bootstrap.tmp.*",
    ".codex-review-loop.writer-recovery.tmp.*",
)
GIT_CONTAINMENT_CONFIG = (
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.autoDetach=false",
)
LockReleaseResult = Literal["missing", "released", "replaced"]


class RuntimeErrorMessage(Exception):
    """An expected, user-actionable runtime failure."""


def error(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _assert_private_regular(path: Path, mode: int = 0o600) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        error(f"private artifact is unavailable: {path}: {exc}")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        error(f"private artifact is not an owned regular file: {path}")
    if metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != mode:
        error(f"private artifact has unsafe links or mode: {path}")


def _open_private(path: Path, flags: int) -> int:
    if not NOFOLLOW:
        error("O_NOFOLLOW is required for private review-loop artifacts")
    try:
        fd = os.open(path, flags | NOFOLLOW | CLOEXEC)
    except OSError as exc:
        error(f"cannot open private artifact: {path}: {exc}")
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(fd)
        error(f"opened private artifact failed safety checks: {path}")
    return fd


def _fsync_directory(directory: Path) -> None:
    if not NOFOLLOW:
        error("O_NOFOLLOW is required for private review-loop artifacts")
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, os.O_RDONLY | directory_flag | NOFOLLOW | CLOEXEC)
    except OSError as exc:
        error(f"cannot open private artifact directory for durability: {directory}: {exc}")
    try:
        os.fsync(fd)
    except OSError as exc:
        error(f"cannot fsync private artifact directory: {directory}: {exc}")
    finally:
        os.close(fd)


def _write_private_file(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        _assert_private_regular(path)
        error(f"private artifact already exists: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if not NOFOLLOW:
        error("O_NOFOLLOW is required for private review-loop artifacts")
    try:
        fd = os.open(path, flags | NOFOLLOW | CLOEXEC, 0o600)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, "short private artifact write")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        error(f"cannot create private artifact: {path}: {exc}")
    _assert_private_regular(path)
    _fsync_directory(path.parent)


def _write_private(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        _assert_private_regular(path)
        error(f"private artifact already exists: {path}")
    temp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    _write_private_file(temp_path, payload)
    try:
        if path.exists() or path.is_symlink():
            _assert_private_regular(path)
            error(f"private artifact already exists: {path}")
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink()
        except OSError:
            pass
        error(f"cannot atomically publish private artifact: {path}: {exc}")
    _assert_private_regular(path)
    _fsync_directory(path.parent)


@contextmanager
def _state_lock(lock_path: Path) -> Iterator[None]:
    if lock_path.exists() or lock_path.is_symlink():
        _assert_private_regular(lock_path)
        fd = _open_private(lock_path, os.O_RDWR)
    else:
        if not NOFOLLOW:
            error("O_NOFOLLOW is required for private review-loop artifacts")
        try:
            fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC,
                0o600,
            )
        except FileExistsError:
            _assert_private_regular(lock_path)
            fd = _open_private(lock_path, os.O_RDWR)
        except OSError as exc:
            error(f"cannot create state lock: {lock_path}: {exc}")
    try:
        os.fchmod(fd, 0o600)
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            error(f"state lock failed safety checks: {lock_path}")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_state(state_path: Path) -> dict[str, Any]:
    fd = _open_private(state_path, os.O_RDONLY)
    try:
        with os.fdopen(fd, "rb") as handle:
            try:
                state = json.load(handle)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                error(f"loop state is not valid JSON: {state_path}: {exc}")
    except OSError as exc:
        error(f"cannot read loop state: {state_path}: {exc}")
    if not isinstance(state, dict):
        error(f"loop state must be an object: {state_path}")
    return state


def _write_state(state_path: Path, state: dict[str, Any]) -> None:
    temp_path = state_path.with_name(f"{state_path.name}.tmp.{os.getpid()}")
    if temp_path.exists() or temp_path.is_symlink():
        error(f"state temp path already exists: {temp_path}")
    payload = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    _write_private_file(temp_path, payload)
    try:
        os.replace(temp_path, state_path)
    except OSError as exc:
        try:
            temp_path.unlink()
        except OSError:
            pass
        error(f"cannot atomically replace loop state: {state_path}: {exc}")
    _assert_private_regular(state_path)
    _fsync_directory(state_path.parent)


def _replace_private_payload(path: Path, payload: bytes) -> None:
    temp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    _write_private_file(temp_path, payload)
    try:
        _assert_private_regular(path)
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink()
        except OSError:
            pass
        error(f"cannot atomically replace private artifact: {path}: {exc}")
    _assert_private_regular(path)
    _fsync_directory(path.parent)


def _require_generation(state: dict[str, Any], expected: int | None) -> int:
    generation = state.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        error("loop state generation is invalid")
    if expected is not None and generation != expected:
        error(f"stale loop-state generation: expected {expected}, found {generation}", 3)
    return generation


def _require_writer(state: dict[str, Any], writer_id: str) -> None:
    if not writer_id:
        error("review-loop writer ID is required")
    if state.get("writerId") != writer_id:
        error("stale review-loop writer rejected", 3)


def _require_active_state(state: dict[str, Any]) -> None:
    if state.get("status") != "active" or state.get("runnersStopped") is True:
        error("review-loop state is no longer active")


def _require_sha(value: str, field: str) -> None:
    if not value or not all(character in "0123456789abcdefABCDEF" for character in value):
        error(f"{field} must be a hexadecimal commit SHA")


def _normalize_commit_sha(value: str, field: str) -> str:
    _require_sha(value, field)
    result = _git("rev-parse", "--verify", f"{value}^{{commit}}", check=False)
    normalized = result.stdout.strip().lower()
    if result.returncode != 0 or len(normalized) not in {40, 64}:
        error(f"{field} must resolve to a full local commit SHA")
    _require_sha(normalized, field)
    return normalized


def _normalize_external_sha(value: str, field: str) -> str:
    _require_sha(value, field)
    if len(value) not in {40, 64}:
        error(f"{field} must be a full 40- or 64-character SHA")
    return value.lower()


def _require_state_collection(state: dict[str, Any], field: str, expected_type: type) -> Any:
    value = state.get(field)
    if not isinstance(value, expected_type):
        error(f"loop state field is invalid: {field}")
    return value


def _require_lock_ref(ref: str) -> None:
    prefix = "refs/heads/codex-review-locks/mr-"
    if not ref.startswith(prefix) or not ref[len(prefix):].isdigit():
        error(f"invalid review-loop lock ref: {ref}")


def _require_lock_object(lock_object: str) -> None:
    if len(lock_object) < 40 or len(lock_object) > 64:
        error("review-loop lock object must be 40 to 64 hexadecimal characters")
    _require_sha(lock_object, "review-loop lock object")


def initialize_marker(args: argparse.Namespace) -> None:
    root = Path(args.root)
    marker = Path(args.marker)
    if marker != root / ".codex-review-loop.marker":
        error("marker path is not derived from the loop root")
    _validate_root(root)
    _write_private(marker, _internal_payload(root))


def sync_directory(args: argparse.Namespace) -> None:
    directory = Path(args.directory)
    _validate_root(directory)
    _fsync_directory(directory)
    if args.parent:
        _fsync_directory(directory.parent)


def initialize_state(args: argparse.Namespace) -> None:
    root = Path(args.root)
    state_path = Path(args.state)
    marker = Path(args.marker)
    bootstrap = Path(args.bootstrap)
    writer_recovery = Path(args.writer_recovery)
    expected_paths = {
        "state": root / "codex-loop-state.json",
        "marker": root / ".codex-review-loop.marker",
        "bootstrap": root / ".codex-review-loop.lock-bootstrap",
        "writer recovery": root / ".codex-review-loop.writer-recovery",
    }
    for name, supplied in {
        "state": state_path,
        "marker": marker,
        "bootstrap": bootstrap,
        "writer recovery": writer_recovery,
    }.items():
        if supplied != expected_paths[name]:
            error(f"initialization path is not derived from the loop root: {name}")
    _validate_root(root)
    _validate_token(marker, _internal_payload(root))
    if state_path.exists() or state_path.is_symlink():
        error(f"loop state already exists: {state_path}")
    if not bootstrap.exists() or bootstrap.is_symlink():
        error("lock bootstrap record is missing")
    if args.mr < 1 or args.max_rounds < 1:
        error("MR and max rounds must be positive")
    if not args.loop_id or not args.owner or not args.remote:
        error("loop identity is incomplete")
    _require_lock_ref(args.ref)
    _require_lock_object(args.lock_object)
    _validate_token(bootstrap, _lock_record_payload(args.remote, args.ref, args.loop_id, args.owner, args.lock_object))
    recovery_token = secrets.token_hex(32)
    _write_private(writer_recovery, f"{recovery_token}\n".encode())
    _write_state(
        state_path,
        {
            "mr": args.mr,
            "scratchDir": str(root),
            "loopId": args.loop_id,
            "owner": args.owner,
            "lockRemote": args.remote,
            "lockRef": args.ref,
            "lockObject": args.lock_object,
            "maxRounds": args.max_rounds,
            "status": "active",
            "runnersStopped": False,
            "generation": 0,
            "writerId": None,
            "writerRecoveryHashes": [hashlib.sha256(recovery_token.encode()).hexdigest()],
            "fixRoundsDone": 0,
            "awaitingReviewForSha": None,
            "processedReviewShas": [],
            "rebuttals": {},
            "rebuttalOnlyStreak": 0,
            "observabilityFailureStreak": 0,
            "cleanupPhase": "active",
        },
    )
    _safe_unlink(bootstrap, [0], 0)


def recover_writer(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    lock_path = Path(args.lock or f"{state_path}.lock")
    recovery_path = Path(args.writer_recovery)
    with _state_lock(lock_path):
        state = _read_state(state_path)
        generation = _require_generation(state, args.expected_generation)
        _require_active_state(state)
        if state.get("writerId") != args.expected_writer:
            error("writer recovery expected writer does not match current state", 3)
        scratch_dir = state.get("scratchDir")
        if not isinstance(scratch_dir, str) or recovery_path != Path(scratch_dir) / ".codex-review-loop.writer-recovery":
            error("writer recovery path does not match loop state", 3)
        recovery_fd = _open_private(recovery_path, os.O_RDONLY)
        try:
            recovery_token = os.read(recovery_fd, 4096).decode("utf-8").strip()
        except UnicodeDecodeError:
            error("writer recovery capability is not valid UTF-8", 3)
        finally:
            os.close(recovery_fd)
        recovery_hash = hashlib.sha256(recovery_token.encode()).hexdigest()
        accepted_hashes = state.get("writerRecoveryHashes")
        if (
            not recovery_token
            or not isinstance(accepted_hashes, list)
            or not all(isinstance(item, str) for item in accepted_hashes)
            or recovery_hash not in accepted_hashes
        ):
            error("writer recovery capability is stale or invalid", 3)
        new_writer = f"recovered-{secrets.token_hex(12)}"
        new_token = secrets.token_hex(32)
        new_hash = hashlib.sha256(new_token.encode()).hexdigest()
        # Accept both capabilities across the state/file replacement cut point.
        # Either side of a crash can safely repeat recovery and rotate again.
        state["writerId"] = new_writer
        state["writerRecoveryHashes"] = list(dict.fromkeys([recovery_hash, new_hash]))
        state["generation"] = generation + 1
        _write_state(state_path, state)
        _replace_private_payload(recovery_path, f"{new_token}\n".encode())
        state["writerRecoveryHashes"] = [new_hash]
        _write_state(state_path, state)
    print(new_writer)


def mutate_state(args: argparse.Namespace) -> None:
    state_path = Path(args.state)
    lock_path = Path(args.lock or f"{state_path}.lock")
    with _state_lock(lock_path):
        state = _read_state(state_path)
        generation = _require_generation(state, args.expected_generation)
        operation = args.operation
        if operation == "writer":
            if args.expected_generation is None:
                error("writer state update requires an expected generation")
            expected = None if args.expected_writer == "__null__" else args.expected_writer
            if state.get("writerId") != expected:
                error("stale review-loop writer generation rejected", 3)
            _require_active_state(state)
            if expected is None:
                if args.caller_writer_id:
                    error("initial writer claim must not present a predecessor", 3)
            elif args.caller_writer_id != expected:
                error("writer handoff rejected: caller does not own the current generation", 3)
            if not args.writer_id:
                error("review-loop writer ID is required")
            if args.writer_id == expected:
                error("writer handoff requires a fresh writer ID")
            state["writerId"] = args.writer_id
        elif operation == "observability":
            if args.expected_generation is None:
                error("observability state update requires an expected generation")
            _require_writer(state, args.writer_id)
            _require_active_state(state)
            if args.value < 0:
                error("observability failure streak must not be negative")
            state["observabilityFailureStreak"] = args.value
        elif operation == "round":
            _require_writer(state, args.writer_id)
            _require_active_state(state)
            if args.expected_generation is None:
                error("round state update requires an expected generation")
            changed = False
            duplicate_awaiting_review = False
            duplicate_processed_review = False
            if args.processed_review_sha is not None:
                processed_review_sha = _normalize_external_sha(args.processed_review_sha, "processed review SHA")
                processed = _require_state_collection(state, "processedReviewShas", list)
                if not all(isinstance(item, str) for item in processed):
                    error("loop state field is invalid: processedReviewShas")
                migrated_processed: list[str] = []
                for item in processed:
                    _require_sha(item, "stored processed review SHA")
                    normalized_item = item.lower()
                    if len(normalized_item) not in {40, 64}:
                        if len(normalized_item) < 7 or len(normalized_item) > 63:
                            error("stored processed review SHA has an invalid length")
                        if processed_review_sha.startswith(normalized_item):
                            normalized_item = processed_review_sha
                    if normalized_item not in migrated_processed:
                        migrated_processed.append(normalized_item)
                if migrated_processed != processed:
                    state["processedReviewShas"] = migrated_processed
                    processed = migrated_processed
                    changed = True
                if processed_review_sha not in processed:
                    processed.append(processed_review_sha)
                    changed = True
                else:
                    duplicate_processed_review = True
            if args.awaiting_review_sha is not None:
                awaiting_review_sha = _normalize_commit_sha(args.awaiting_review_sha, "awaiting review SHA")
                stored_awaiting = state.get("awaitingReviewForSha")
                if stored_awaiting is not None:
                    if not isinstance(stored_awaiting, str):
                        error("loop state field is invalid: awaitingReviewForSha")
                    normalized_stored_awaiting = _normalize_commit_sha(
                        stored_awaiting,
                        "stored awaiting review SHA",
                    )
                    if normalized_stored_awaiting != stored_awaiting:
                        state["awaitingReviewForSha"] = normalized_stored_awaiting
                        stored_awaiting = normalized_stored_awaiting
                        changed = True
                if stored_awaiting == awaiting_review_sha:
                    duplicate_awaiting_review = True
                else:
                    state["awaitingReviewForSha"] = awaiting_review_sha
                    fix_rounds = state.get("fixRoundsDone")
                    if not isinstance(fix_rounds, int) or isinstance(fix_rounds, bool) or fix_rounds < 0:
                        error("loop state field is invalid: fixRoundsDone")
                    state["fixRoundsDone"] = fix_rounds + 1
                    state["rebuttalOnlyStreak"] = 0
                    changed = True
            if args.rebuttal_key is not None or args.rebuttal_evidence is not None:
                if not args.rebuttal_key or args.rebuttal_evidence is None:
                    error("rebuttal key and evidence must be supplied together")
                rebuttals = _require_state_collection(state, "rebuttals", dict)
                if not all(isinstance(key, str) and isinstance(value, str) for key, value in rebuttals.items()):
                    error("loop state field is invalid: rebuttals")
                rebuttals[args.rebuttal_key] = args.rebuttal_evidence
                changed = True
            if args.increment_rebuttal_only:
                streak = state.get("rebuttalOnlyStreak")
                if not isinstance(streak, int) or isinstance(streak, bool) or streak < 0:
                    error("loop state field is invalid: rebuttalOnlyStreak")
                state["rebuttalOnlyStreak"] = streak + 1
                changed = True
            if not changed:
                if duplicate_awaiting_review or duplicate_processed_review:
                    return
                error("round state update has no transition")
        elif operation == "cleanup":
            if args.expected_generation is None:
                error("cleanup state update requires an expected generation")
            if args.phase not in CLEANUP_PHASES - {"active"}:
                error(f"invalid cleanup phase: {args.phase}")
            _require_writer(state, args.writer_id)
            if state.get("status") not in TERMINAL_STATUSES or state.get("runnersStopped") is not True:
                error("cleanup requires a stopped terminal loop")
            if CLEANUP_NEXT_PHASE.get(state.get("cleanupPhase")) != args.phase:
                error(f"invalid cleanup phase transition: {state.get('cleanupPhase')} -> {args.phase}")
            state["cleanupPhase"] = args.phase
        elif operation == "terminal":
            if args.expected_generation is None:
                error("terminal state update requires an expected generation")
            if args.status not in TERMINAL_STATUSES:
                error(f"invalid terminal status: {args.status}")
            _require_writer(state, args.writer_id)
            _require_active_state(state)
            state["status"] = args.status
            state["runnersStopped"] = True
        else:
            error(f"unknown state operation: {operation}")
        state["generation"] = generation + 1
        _write_state(state_path, state)


def _require_descendant_containment() -> None:
    global _SUBREAPER_ENABLED
    if _SUBREAPER_ENABLED is None:
        enabled = False
        if sys.platform.startswith("linux") and Path("/proc").is_dir():
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                enabled = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
            except (AttributeError, OSError):
                enabled = False
        _SUBREAPER_ENABLED = enabled
    if not _SUBREAPER_ENABLED:
        error("strict descendant containment is unavailable on this platform")


def _descendant_pids(parent_pid: int) -> set[int]:
    children: dict[int, set[int]] = {}
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            stat_line = (entry / "stat").read_text(encoding="utf-8")
            remainder = stat_line[stat_line.rfind(")") + 2 :].split()
            pid = int(entry.name)
            process_parent = int(remainder[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(process_parent, set()).add(pid)
    descendants: set[int] = set()
    pending = list(children.get(parent_pid, set()))
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        pending.extend(children.get(pid, set()))
    return descendants


def _reap_adopted_children() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    process_group_id = process.pid
    term_signalled: set[int] = set()
    if hasattr(os, "killpg"):
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        process.terminate()
    deadline = time.monotonic() + PROCESS_GROUP_GRACE_SECONDS
    while _descendant_pids(os.getpid()) and time.monotonic() < deadline:
        # Catch descendants forked from a TERM handler after the previous scan,
        # without repeatedly invoking a handler already given its grace period.
        current_descendants = _descendant_pids(os.getpid())
        for pid in sorted(current_descendants - term_signalled, reverse=True):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            term_signalled.add(pid)
        try:
            process.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.01)
    if hasattr(os, "killpg"):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    try:
        process.wait(timeout=PROCESS_GROUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    # Re-scan until the subreaper observes a stable empty descendant set. A
    # process can fork from a TERM handler between any one PID snapshot and the
    # following signal, so a single final kill pass is not a containment proof.
    stable_empty_scans = 0
    containment_deadline = time.monotonic() + PROCESS_GROUP_GRACE_SECONDS
    while time.monotonic() < containment_deadline:
        descendants = _descendant_pids(os.getpid())
        if descendants:
            stable_empty_scans = 0
            for pid in sorted(descendants, reverse=True):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        else:
            _reap_adopted_children()
            if not _descendant_pids(os.getpid()):
                stable_empty_scans += 1
                if stable_empty_scans >= 3:
                    break
            else:
                stable_empty_scans = 0
        time.sleep(0.01)
    _reap_adopted_children()
    if _descendant_pids(os.getpid()):
        error("descendant containment did not reach a stable empty state", 125)


def _run_command(
    command: list[str],
    *,
    input_text: str | None = None,
    environment: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    _require_descendant_containment()
    process_environment = os.environ.copy()
    process_environment["GIT_TERMINAL_PROMPT"] = "0"
    if environment:
        process_environment.update(environment)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_environment,
            start_new_session=True,
        )
    except OSError as exc:
        error(f"command is unavailable: {command[0]}: {exc}")
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        # A short-lived grandchild can exit before its leader and be adopted by
        # this subreaper as a zombie. It has no remaining execution capability,
        # so reap it before deciding whether a live descendant escaped.
        _reap_adopted_children()
        if _descendant_pids(os.getpid()):
            _terminate_process_tree(process)
            message = "command exited while descendant processes were still running"
            stderr = f"{stderr.rstrip()}\n{message}\n" if stderr else f"{message}\n"
            return subprocess.CompletedProcess(command, 125, stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as timeout_error:
        _terminate_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=PROCESS_GROUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
            stdout = timeout_error.stdout or ""
            stderr = timeout_error.stderr or ""
        return subprocess.CompletedProcess(command, 124, stdout, stderr)


def _emit_command_result(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        sys.stdout.write(result.stdout)
        sys.stdout.flush()
    if result.stderr:
        sys.stderr.write(result.stderr)
        sys.stderr.flush()


def _git_command(*arguments: str) -> list[str]:
    return ["git", *GIT_CONTAINMENT_CONFIG, *arguments]


def _git(*arguments: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = _run_command(
        _git_command(*arguments),
        input_text=input_text,
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    if check and result.returncode != 0:
        if result.returncode == 124:
            error(f"git command timed out after {GIT_COMMAND_TIMEOUT_SECONDS:g}s: git {' '.join(arguments)}", 124)
        error(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result


def _remote_lock_sha(remote: str, ref: str) -> str:
    result = _git("ls-remote", remote, ref)
    return result.stdout.split()[0] if result.stdout.split() else ""


def _lock_commit(loop_id: str, owner: str) -> str:
    payload = f"codex-review-loop-lock\nloopId={loop_id}\nowner={owner}\n"
    blob = _git("hash-object", "-w", "--stdin", input_text=payload).stdout.strip()
    tree = _git("mktree", input_text=f"100644 blob {blob}\tlock\n").stdout.strip()
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "codex-review-loop",
            "GIT_AUTHOR_EMAIL": "codex-review-loop@localhost",
            "GIT_COMMITTER_NAME": "codex-review-loop",
            "GIT_COMMITTER_EMAIL": "codex-review-loop@localhost",
        }
    )
    result = _run_command(
        _git_command("commit-tree", tree),
        input_text="codex review loop ownership\n",
        environment=environment,
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        error(result.stderr.strip() or "could not create the review-loop lock object")
    return result.stdout.strip()


def _lock_payload_from_git(git_directory: Path | None, lock_object: str) -> str | None:
    prefix = _git_command() if git_directory is None else _git_command("--git-dir", str(git_directory))

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return _run_command(
            [*prefix, *arguments],
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )

    commit_type = run("cat-file", "-t", lock_object)
    if commit_type.returncode != 0 or commit_type.stdout.strip() != "commit":
        return None
    tree = run("show", "-s", "--format=%T", lock_object)
    if tree.returncode != 0 or not tree.stdout.strip():
        return None
    entry = run("ls-tree", tree.stdout.strip(), "--", "lock")
    fields = entry.stdout.split()
    if entry.returncode != 0 or len(fields) < 3 or fields[1] != "blob":
        return None
    payload = run("cat-file", "blob", fields[2])
    return payload.stdout if payload.returncode == 0 else None


def _validate_lock_payload(lock_object: str, loop_id: str, owner: str, remote: str, ref: str) -> None:
    expected = f"codex-review-loop-lock\nloopId={loop_id}\nowner={owner}\n"
    payload = _lock_payload_from_git(None, lock_object)
    if payload is None:
        configured_remote = _git("remote", "get-url", remote, check=False).stdout.strip()
        fetch_remote = configured_remote or remote
        with tempfile.TemporaryDirectory(prefix="codex-review-loop-lock-") as temporary:
            git_directory = Path(temporary) / "objects.git"
            initialized = _run_command(
                _git_command("init", "--bare", str(git_directory)),
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            fetched = _run_command(
                [
                    *_git_command(),
                    "--git-dir",
                    str(git_directory),
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    fetch_remote,
                    f"{ref}:refs/heads/codex-review-loop-lock-check",
                ],
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            fetched_sha = _run_command(
                [
                    *_git_command(),
                    "--git-dir",
                    str(git_directory),
                    "rev-parse",
                    "refs/heads/codex-review-loop-lock-check",
                ],
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
            if initialized.returncode != 0 or fetched.returncode != 0 or fetched_sha.stdout.strip() != lock_object:
                details = "; ".join(
                    f"{name}=exit {result.returncode}: {result.stderr.strip() or result.stdout.strip() or '<no output>'}"
                    for name, result in [
                        ("init", initialized),
                        ("fetch", fetched),
                        ("rev-parse", fetched_sha),
                    ]
                    if result.returncode != 0 or (name == "rev-parse" and result.stdout.strip() != lock_object)
                )
                error(f"review-loop lock object could not be fetched from the remote: {lock_object}; {details}", 3)
            payload = _lock_payload_from_git(git_directory, lock_object)
    if payload != expected:
        error("review-loop lock payload does not match the state owner", 3)


def _validate_state_writer(state: dict[str, Any], writer_id: str, expected_generation: int | None) -> None:
    _require_generation(state, expected_generation)
    current_writer = state.get("writerId")
    if current_writer is None:
        if writer_id:
            error("review-loop lock assertion has no current writer", 3)
        _require_active_state(state)
        return
    if not isinstance(current_writer, str) or not current_writer:
        error("loop state writerId is invalid", 3)
    if current_writer != writer_id:
        error("stale review-loop writer rejected before external effect", 3)
    _require_active_state(state)


def _validate_state_lock_identity(state: dict[str, Any], args: argparse.Namespace) -> None:
    expected = {
        "lockRemote": args.remote,
        "lockRef": args.ref,
        "lockObject": args.lock_object,
        "loopId": args.loop_id,
        "owner": args.owner,
    }
    for field, value in expected.items():
        if state.get(field) != value:
            error(f"loop state lock identity mismatch: {field}", 3)


def _require_lock_identity(args: argparse.Namespace, *, require_object: bool) -> None:
    if not args.remote or not args.loop_id or not args.owner:
        error("review-loop lock identity is incomplete", 3)
    _require_lock_ref(args.ref)
    if require_object:
        _require_lock_object(args.lock_object)


def acquire_lock(args: argparse.Namespace) -> None:
    _require_lock_identity(args, require_object=False)
    lock_object = _lock_commit(args.loop_id, args.owner)
    if args.recovery_file:
        _write_private(
            Path(args.recovery_file),
            _lock_record_payload(
                args.remote,
                args.ref,
                args.loop_id,
                args.owner,
                lock_object,
                outcome="pending",
            ),
        )
    current = _remote_lock_sha(args.remote, args.ref)
    if current:
        error(f"MR lock is already held: {args.ref} -> {current}", 2)
    result = _git("push", "--porcelain", "--atomic", args.remote, f"{lock_object}:{args.ref}", check=False)
    if result.returncode != 0:
        if result.returncode == 124:
            if args.recovery_file:
                _replace_private_payload(
                    Path(args.recovery_file),
                    _lock_record_payload(
                        args.remote,
                        args.ref,
                        args.loop_id,
                        args.owner,
                        lock_object,
                        outcome="indeterminate",
                    ),
                )
            error("MR lock acquisition timed out; outcome is indeterminate and recovery record was preserved", 124)
        error(result.stderr.strip() or f"could not acquire MR lock: {args.ref}", 2)
    if _remote_lock_sha(args.remote, args.ref) != lock_object:
        error(f"MR lock verification failed: {args.ref}")
    if args.recovery_file:
        _replace_private_payload(
            Path(args.recovery_file),
            _lock_record_payload(args.remote, args.ref, args.loop_id, args.owner, lock_object),
        )
    print(lock_object)


def reconcile_lock_acquire(args: argparse.Namespace) -> None:
    _require_lock_identity(args, require_object=True)
    if not getattr(args, "recovery_file", ""):
        error("lock reconciliation requires its bootstrap recovery capability", 3)
    recovery_path = Path(args.recovery_file)
    normal_payload = _lock_record_payload(
        args.remote,
        args.ref,
        args.loop_id,
        args.owner,
        args.lock_object,
    )
    # The bootstrap record is the capability to reconcile this exact
    # candidate. Validate it before any remote observation or mutation.
    _validate_token_one_of(
        recovery_path,
        [
            normal_payload,
            _lock_record_payload(
                args.remote,
                args.ref,
                args.loop_id,
                args.owner,
                args.lock_object,
                outcome="pending",
            ),
            _lock_record_payload(
                args.remote,
                args.ref,
                args.loop_id,
                args.owner,
                args.lock_object,
                outcome="indeterminate",
            ),
        ],
        error_code=3,
    )
    _validate_lock_payload(args.lock_object, args.loop_id, args.owner, args.remote, args.ref)

    def resolved() -> None:
        _replace_private_payload(recovery_path, normal_payload)

    current = _remote_lock_sha(args.remote, args.ref)
    if current == args.lock_object:
        resolved()
        print(args.lock_object)
        return
    if current:
        error(f"MR lock is held by another object: {args.ref} -> {current}", 2)
    result = _git(
        "push",
        "--porcelain",
        "--atomic",
        args.remote,
        f"{args.lock_object}:{args.ref}",
        check=False,
    )
    if result.returncode == 124:
        error("MR lock reconciliation timed out; preserve the recovery record and retry reconciliation", 124)
    after = _remote_lock_sha(args.remote, args.ref)
    if after == args.lock_object:
        resolved()
        print(args.lock_object)
        return
    if after:
        error(f"MR lock reconciliation lost to another object: {args.ref} -> {after}", 2)
    error(result.stderr.strip() or "MR lock reconciliation proved that the candidate was not installed", 2)


def _assert_remote_lock_only(args: argparse.Namespace) -> None:
    _require_lock_identity(args, require_object=True)
    current = _remote_lock_sha(args.remote, args.ref)
    if current != args.lock_object:
        error(f"review-loop lock ownership changed: expected {args.lock_object}, found {current or '<missing>'}", 3)
    _validate_lock_payload(
        args.lock_object,
        getattr(args, "loop_id", ""),
        getattr(args, "owner", ""),
        args.remote,
        args.ref,
    )


def assert_lock(args: argparse.Namespace) -> None:
    state_path = getattr(args, "state", None)
    if state_path:
        state_path = Path(state_path)
        lock_path = Path(getattr(args, "lock", None) or f"{state_path}.lock")
        with _state_lock(lock_path):
            state = _read_state(state_path)
            _validate_state_writer(
                state,
                getattr(args, "writer_id", ""),
                getattr(args, "expected_generation", None),
            )
            _validate_state_lock_identity(state, args)
            _assert_remote_lock_only(args)
        return
    _assert_remote_lock_only(args)


def guarded_exec(args: argparse.Namespace) -> None:
    if args.timeout <= 0:
        error("timeout must be positive")
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        error("guarded-exec requires a command")
    _require_descendant_containment()
    input_text = sys.stdin.read() if args.stdin else None
    state_path = Path(args.state)
    lock_path = Path(args.lock or f"{state_path}.lock")
    with _state_lock(lock_path):
        state = _read_state(state_path)
        _validate_state_writer(state, args.writer_id, args.expected_generation)
        _validate_state_lock_identity(state, args)
        _assert_remote_lock_only(args)
        result = _run_command(
            command,
            input_text=input_text,
            timeout=args.timeout,
        )
        _assert_remote_lock_only(args)
    _emit_command_result(result)
    raise SystemExit(result.returncode)


def release_lock(args: argparse.Namespace) -> LockReleaseResult:
    _require_lock_identity(args, require_object=True)
    allow_replaced = getattr(args, "allow_replaced", False)
    current = _remote_lock_sha(args.remote, args.ref)
    if not current:
        return "missing"
    if current != args.lock_object:
        if allow_replaced:
            return "replaced"
        error(f"review-loop lock ownership changed: expected {args.lock_object}, found {current}", 3)
    _validate_lock_payload(
        args.lock_object,
        getattr(args, "loop_id", ""),
        getattr(args, "owner", ""),
        args.remote,
        args.ref,
    )
    # Deletion is a server-side compare-and-delete. A separate ls-remote check
    # is only an observation; --force-with-lease makes the deletion conditional
    # on the exact object observed above, so a new owner cannot be deleted.
    result = _git(
        "push",
        "--porcelain",
        f"--force-with-lease={args.ref}:{args.lock_object}",
        args.remote,
        f":{args.ref}",
        check=False,
    )
    if result.returncode != 0:
        after = _remote_lock_sha(args.remote, args.ref)
        if not after:
            return "released"
        if after != args.lock_object:
            if allow_replaced:
                return "replaced"
            error(f"review-loop lock ownership changed: expected {args.lock_object}, found {after}", 3)
        error(result.stderr.strip() or f"could not release MR lock: {args.ref}")
    return "released"


def _lock_record_payload(
    remote: str,
    ref: str,
    loop_id: str,
    owner: str,
    lock_object: str,
    *,
    outcome: str = "",
) -> bytes:
    payload = (
        f"lockRemote:{remote}\n"
        f"lockRef:{ref}\n"
        f"loopId:{loop_id}\n"
        f"owner:{owner}\n"
        f"lockObject:{lock_object}\n"
    )
    if outcome:
        payload += f"outcome:{outcome}\n"
    return payload.encode()


def _token_payload(root: Path, remote: str, ref: str, lock_object: str, loop_id: str = "", owner: str = "") -> bytes:
    return (
        f"codex-review-loop:{root}\n"
        f"lockRemote:{remote}\n"
        f"lockRef:{ref}\n"
        f"loopId:{loop_id}\n"
        f"owner:{owner}\n"
        f"lockObject:{lock_object}\n"
    ).encode()


def _lock_released_token_payload(
    root: Path,
    remote: str,
    ref: str,
    lock_object: str,
    loop_id: str = "",
    owner: str = "",
) -> bytes:
    return _token_payload(root, remote, ref, lock_object, loop_id, owner) + b"phase:lock-released\n"


def _pre_lock_token_payload(root: Path) -> bytes:
    return f"codex-review-loop:{root}\nphase:pre-lock\n".encode()


def _internal_payload(root: Path) -> bytes:
    return f"codex-review-loop:{root}".encode()


def _validate_root(root: Path) -> None:
    if not root.is_absolute() or root.name == "" or not root.name.startswith("codex-review-loop."):
        error(f"invalid review-loop root: {root}")
    try:
        metadata = os.lstat(root)
    except OSError as exc:
        error(f"review-loop root is unavailable: {root}: {exc}")
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        error(f"review-loop root failed ownership or mode checks: {root}")
    if os.path.realpath(root) != str(root):
        error(f"review-loop root is symlinked: {root}")


def _read_token(path: Path) -> bytes:
    _assert_private_regular(path)
    fd = _open_private(path, os.O_RDONLY)
    try:
        return os.read(fd, 64 * 1024)
    finally:
        os.close(fd)


def _validate_token(path: Path, payload: bytes) -> None:
    actual = _read_token(path)
    if actual != payload:
        error(f"review-loop recovery token contents do not match: {path}")


def _validate_token_one_of(path: Path, payloads: list[bytes], error_code: int = 1) -> None:
    actual = _read_token(path)
    if actual not in payloads:
        error(f"review-loop recovery token contents do not match: {path}", error_code)


def _safe_unlink(path: Path, delete_count: list[int], fail_after: int) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _assert_private_regular(path)
    delete_count[0] += 1
    path.unlink()
    _fsync_directory(path.parent)
    if fail_after and delete_count[0] == fail_after:
        error(f"test failpoint after cleanup unlink {delete_count[0]}", 77)


def _known_artifacts(root: Path) -> list[Path]:
    exact = [
        root / ".codex-review-loop.marker",
        root / ".codex-review-loop.cleanup",
        root / "codex-loop-state.json",
        root / "codex-loop-state.json.lock",
        root / "mr-disc.json",
        root / "codex-reply.md",
        root / "codex-receipt.md",
        root / ".codex-review-loop.lock-bootstrap",
        root / ".codex-review-loop.writer-recovery",
    ]
    patterns = [
        "codex-loop-state.json.tmp.*",
        "mr-disc.pages.*",
        "mr-disc.parsed.*",
        *PRIVATE_ARTIFACT_TEMP_PATTERNS,
    ]
    for pattern in patterns:
        exact.extend(Path(item) for item in glob.glob(str(root / pattern)))
    return exact


def _validate_known_tree(root: Path) -> None:
    allowed = {path.name for path in _known_artifacts(root)}
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        error(f"cannot inspect review-loop root: {root}: {exc}")
    for entry in entries:
        if entry.name not in allowed:
            error(f"unexpected review-loop cleanup artifact: {entry}")
        _assert_private_regular(entry)


def _ensure_external_token(path: Path, payload: bytes, allowed: list[bytes]) -> bytes:
    if path.exists() or path.is_symlink():
        actual = _read_token(path)
        if actual not in allowed:
            error(f"review-loop recovery token contents do not match: {path}")
        return actual
    _write_private(path, payload)
    return payload


def classify_cleanup_authority(args: argparse.Namespace) -> None:
    root = Path(args.root)
    state_path = Path(args.state)
    external = Path(args.external)
    if not root.is_absolute() or str(root) != os.path.realpath(str(root)):
        error(f"review-loop root must be an absolute canonical path: {root}")
    if state_path != root / "codex-loop-state.json" or external != root.with_name(root.name + ".cleanup"):
        error("cleanup authority paths are not derived from the loop root")
    _require_lock_identity(args, require_object=True)
    normal_token = _token_payload(
        root, args.remote, args.ref, args.lock_object, args.loop_id, args.owner,
    )
    released_token = _lock_released_token_payload(
        root, args.remote, args.ref, args.lock_object, args.loop_id, args.owner,
    )
    state: dict[str, Any] | None = None
    if state_path.exists() or state_path.is_symlink():
        _assert_private_regular(state_path)
        state = _read_state(state_path)
        if (
            state.get("scratchDir") != str(root)
            or state.get("lockRemote", "") != args.remote
            or state.get("lockRef", "") != args.ref
            or state.get("lockObject", "") != args.lock_object
            or state.get("loopId", "") != args.loop_id
            or state.get("owner", "") != args.owner
        ):
            error("cleanup authority does not match loop state")
    state_released = bool(
        state is not None
        and state.get("status") in TERMINAL_STATUSES
        and state.get("runnersStopped") is True
        and state.get("cleanupPhase") == "lock_released"
    )
    if not external.exists() and not external.is_symlink():
        if state_released:
            error("released cleanup state is missing its sibling recovery token")
        raise SystemExit(4)
    actual = _read_token(external)
    if actual not in {normal_token, released_token}:
        error(f"review-loop recovery token contents do not match: {external}")
    if actual == released_token:
        if state is None or state_released:
            print("remote-handling-complete")
            return
        error("released cleanup token conflicts with non-terminal loop state")
    if state_released:
        # Crash after the state transition but before token upgrade. Cleanup
        # will atomically upgrade the validated ordinary token before unlinking state.
        print("remote-handling-complete")
        return
    raise SystemExit(4)


def cleanup(args: argparse.Namespace) -> None:
    root = Path(args.root)
    external = Path(args.external)
    internal = Path(args.internal)
    state_path = Path(args.state)
    marker = Path(args.marker)
    lock_object = args.lock_object or ""
    pre_lock_only = args.pre_lock_only

    if not root.is_absolute() or str(root) != os.path.realpath(str(root)):
        error(f"review-loop root must be an absolute canonical path: {root}")
    expected_paths = {
        "state": root / "codex-loop-state.json",
        "marker": root / ".codex-review-loop.marker",
        "internal": root / ".codex-review-loop.cleanup",
        "external": root.with_name(root.name + ".cleanup"),
    }
    supplied_paths = {"state": state_path, "marker": marker, "internal": internal, "external": external}
    for name, supplied in supplied_paths.items():
        if supplied != expected_paths[name]:
            error(f"cleanup path is not derived from the loop root: {name}")

    if not root.exists():
        token = (
            _pre_lock_token_payload(root)
            if pre_lock_only
            else _token_payload(root, args.remote, args.ref, lock_object, args.loop_id, args.owner)
        )
        allowed_tokens = [token]
        if not pre_lock_only and lock_object:
            allowed_tokens.append(
                _lock_released_token_payload(
                    root, args.remote, args.ref, lock_object, args.loop_id, args.owner,
                )
            )
        _validate_token_one_of(external, allowed_tokens)
        # Every cleanup path releases (or deliberately skips) the remote lock
        # before deleting the root. Root absence therefore proves that remote
        # handling is complete; never inspect a ref that a new owner may hold.
        for publish_temp in glob_paths(external.parent, f"{external.name}.tmp.*"):
            _safe_unlink(publish_temp, [0], 0)
        _safe_unlink(external, [0], 0)
        return

    _validate_root(root)
    marker_payload = _internal_payload(root)
    marker_exists = marker.exists() or marker.is_symlink()
    bootstrap = root / ".codex-review-loop.lock-bootstrap"
    if not state_path.exists():
        # Initialization recovery has no state file yet. The marker, exact
        # derived paths, and bootstrap/recovery token still bind cleanup to the
        # one loop root; releasing the exact lock object is safe here.
        state: dict[str, Any] | None = None
    else:
        if pre_lock_only:
            error("pre-lock cleanup cannot remove initialized loop state")
        _assert_private_regular(state_path)
        loaded_state = _read_state(state_path)
        if not args.loop_id:
            args.loop_id = loaded_state.get("loopId", "")
        if not args.owner:
            args.owner = loaded_state.get("owner", "")
        bootstrap_recovery = (
            bootstrap.exists()
            and loaded_state.get("status") == "active"
            and loaded_state.get("runnersStopped") is False
            and loaded_state.get("generation") == 0
            and loaded_state.get("writerId") is None
            and loaded_state.get("cleanupPhase") == "active"
        )
        if bootstrap_recovery:
            if (
                loaded_state.get("scratchDir") != str(root)
                or loaded_state.get("lockRemote", "") != args.remote
                or loaded_state.get("lockRef", "") != args.ref
                or loaded_state.get("lockObject", "") != lock_object
                or loaded_state.get("loopId", "") != args.loop_id
                or loaded_state.get("owner", "") != args.owner
            ):
                error("bootstrap recovery state identity does not match the lock record")
            state = None
        else:
            state = loaded_state
            if state.get("scratchDir") != str(root) or state.get("status") not in TERMINAL_STATUSES:
                error("cleanup state identity or terminal status is invalid")
            if state.get("runnersStopped") is not True or state.get("cleanupPhase") not in CLEANUP_PHASES:
                error("cleanup state is not safely stopped")
            if (
                state.get("lockRemote", "") != args.remote
                or state.get("lockRef", "") != args.ref
                or state.get("lockObject", "") != lock_object
                or state.get("loopId", "") != args.loop_id
                or state.get("owner", "") != args.owner
            ):
                error("cleanup lock identity does not match loop state")
    publish_temps = set(
        glob_paths(
            root,
            ".codex-review-loop.marker.tmp.*",
            ".codex-review-loop.lock-bootstrap.tmp.*",
        )
    )
    prepublish_only = (
        pre_lock_only
        and state is None
        and not marker_exists
        and not bootstrap.exists()
        and not lock_object
        and set(root.iterdir()) == publish_temps
    )
    token = (
        _pre_lock_token_payload(root)
        if pre_lock_only
        else _token_payload(root, args.remote, args.ref, lock_object, args.loop_id, args.owner)
    )
    lock_released_token = (
        _lock_released_token_payload(
            root, args.remote, args.ref, lock_object, args.loop_id, args.owner,
        )
        if not pre_lock_only and lock_object
        else b""
    )
    allow_lock_released_token = bool(
        lock_released_token
        and (
            not state_path.exists()
            or (state is not None and state["cleanupPhase"] == "lock_released")
        )
    )
    allowed_external_tokens = [
        token,
        *([lock_released_token] if allow_lock_released_token else []),
    ]
    if marker_exists:
        _validate_token(marker, marker_payload)
    elif prepublish_only:
        # Atomic publication can crash after a complete private temp write but
        # before rename. No remote effect starts before marker/bootstrap publish,
        # so a root containing only those validated temp artifacts is safe to
        # tombstone and remove without lock ownership data.
        pass
    else:
        # The marker is one of the final tombstone artifacts. A crash can land
        # after unlinking it but before rmdir; the sibling token remains the
        # durable, exact-root recovery authority for that cut point.
        _validate_token_one_of(
            external,
            allowed_external_tokens,
        )
    _validate_known_tree(root)
    if bootstrap.exists() or bootstrap.is_symlink():
        if not args.loop_id or not args.owner or not lock_object:
            error("bootstrap lock record is incomplete")
        _require_lock_identity(args, require_object=True)
        normal_bootstrap = _lock_record_payload(
            args.remote, args.ref, args.loop_id, args.owner, lock_object,
        )
        if pre_lock_only:
            current = _remote_lock_sha(args.remote, args.ref)
            if not current or current == lock_object:
                error("pre-lock cleanup requires definitive ownership by another lock object", 3)
            _validate_token_one_of(
                bootstrap,
                [
                    _lock_record_payload(
                        args.remote, args.ref, args.loop_id, args.owner, lock_object, outcome="pending",
                    ),
                    _lock_record_payload(
                        args.remote, args.ref, args.loop_id, args.owner, lock_object, outcome="indeterminate",
                    ),
                ],
            )
        else:
            _validate_token(bootstrap, normal_bootstrap)
    if internal.exists() or internal.is_symlink():
        _validate_token(internal, marker_payload)

    # Publish recovery authority only after every existing state/bootstrap/tree
    # authority has been validated. A caller with the wrong identity must not be
    # able to poison an absent sibling token before failing validation.
    external_payload = _ensure_external_token(
        external,
        token,
        allowed_external_tokens,
    )
    remote_handling_complete = bool(
        lock_released_token and external_payload == lock_released_token
    )

    fail_after = int(os.environ.get("CODEX_REVIEW_LOOP_FAIL_AFTER_UNLINK", "0") or "0")
    delete_count = [0]
    if state is not None:
        phase = state["cleanupPhase"]
        writer_id = state.get("writerId", "")
        _require_writer(state, writer_id)

        def persist_cleanup_phase(next_phase: str) -> None:
            generation = state["generation"]
            mutate_state(
                argparse.Namespace(
                    state=str(state_path),
                    lock=f"{state_path}.lock",
                    operation="cleanup",
                    phase=next_phase,
                    expected_generation=generation,
                    expected_writer="",
                    writer_id=writer_id,
                    value=0,
                    status="",
                )
            )
            state["generation"] = generation + 1
            state["cleanupPhase"] = next_phase

        if phase == "active":
            persist_cleanup_phase("ready")
            phase = "ready"
        if phase == "ready":
            for artifact in [root / "mr-disc.json", root / "codex-reply.md", root / "codex-receipt.md", *glob_paths(root, "codex-loop-state.json.tmp.*", "mr-disc.pages.*", "mr-disc.parsed.*", *PRIVATE_ARTIFACT_TEMP_PATTERNS)]:
                _safe_unlink(artifact, delete_count, fail_after)
            persist_cleanup_phase("payloads_removed")
            phase = "payloads_removed"
        if phase == "payloads_removed" and not internal.exists():
            _write_private(internal, _internal_payload(root))
        if phase in {"payloads_removed", "tombstone"}:
            if not internal.exists():
                _write_private(internal, _internal_payload(root))
            _validate_token(internal, _internal_payload(root))
            if phase == "payloads_removed":
                persist_cleanup_phase("tombstone")
    elif state is None:
        # A legacy tombstone may predate the state file. It still has a bounded
        # allowlist, so remove payloads before the marker/token and let rmdir
        # prove that no unknown artifact was accepted.
        for artifact in [
            root / "mr-disc.json",
            root / "codex-reply.md",
            root / "codex-receipt.md",
            *glob_paths(root, "codex-loop-state.json.tmp.*", "mr-disc.pages.*", "mr-disc.parsed.*", *PRIVATE_ARTIFACT_TEMP_PATTERNS),
        ]:
            _safe_unlink(artifact, delete_count, fail_after)

    if (
        lock_object
        and not pre_lock_only
        and not remote_handling_complete
        and (state is None or state["cleanupPhase"] != "lock_released")
    ):
        release_lock(
            argparse.Namespace(
                remote=args.remote,
                ref=args.ref,
                lock_object=lock_object,
                loop_id=args.loop_id,
                owner=args.owner,
                allow_replaced=state is not None,
            )
        )
        if os.environ.get("CODEX_REVIEW_LOOP_FAIL_AFTER_LOCK_RELEASE") == "1":
            error("test failpoint after remote lock release", 77)
        # A terminal, stopped state may observe a new owner after its successful
        # delete but before this local phase write. Never inspect or mutate that
        # new object; `lock_released` means this old loop's remote handling is
        # complete, whether release_lock returned missing, released, or replaced.
        if state is not None:
            persist_cleanup_phase("lock_released")
    if state is not None and state["cleanupPhase"] == "lock_released":
        if not lock_released_token:
            error("terminal cleanup is missing lock identity for its released-phase token")
        if external_payload != lock_released_token:
            _replace_private_payload(external, lock_released_token)
            external_payload = lock_released_token
        remote_handling_complete = True

    for artifact in [
        state_path,
        Path(f"{state_path}.lock"),
        internal,
        root / ".codex-review-loop.lock-bootstrap",
        root / ".codex-review-loop.writer-recovery",
        marker,
    ]:
        _safe_unlink(artifact, delete_count, fail_after)
    try:
        root.rmdir()
    except OSError as exc:
        # The external token remains the durable recovery handle. A later run
        # validates it and either removes the remaining artifact or reports it.
        error(f"review-loop directory is not empty after guarded cleanup: {root}: {exc}")
    # Persist the directory removal before the sibling recovery authority can
    # be deleted. A system crash must not resurrect an apparently pre-lock root
    # after its phase-qualified token has been removed.
    _fsync_directory(root.parent)
    if os.environ.get("CODEX_REVIEW_LOOP_FAIL_BEFORE_EXTERNAL_UNLINK") == "1":
        error("test failpoint before recovery-token unlink", 77)
    for publish_temp in glob_paths(external.parent, f"{external.name}.tmp.*"):
        _safe_unlink(publish_temp, delete_count, fail_after)
    _safe_unlink(external, delete_count, fail_after)


def glob_paths(root: Path, *patterns: str) -> list[Path]:
    result: list[Path] = []
    for pattern in patterns:
        result.extend(Path(item) for item in glob.glob(str(root / pattern)))
    return result


def remove_fetch_temps(args: argparse.Namespace) -> None:
    root = Path(args.root)
    _validate_root(root)
    allowed_prefixes = ("mr-disc.pages.", "mr-disc.parsed.")
    delete_count = [0]
    for value in args.path:
        path = Path(value)
        if path.parent != root or not path.name.startswith(allowed_prefixes):
            error(f"fetch temp path is outside the private allowlist: {path}")
        _safe_unlink(path, delete_count, 0)


def run_with_timeout(args: argparse.Namespace) -> None:
    if args.timeout <= 0:
        error("timeout must be positive")
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        error("run requires a command")
    result = _run_command(command, timeout=args.timeout)
    _emit_command_result(result)
    raise SystemExit(result.returncode)


def parse_iso(args: argparse.Namespace) -> None:
    value = args.value
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        error(f"invalid server timestamp: {value}", 1)
    if parsed.tzinfo is None:
        error(f"server timestamp has no timezone: {value}", 1)
    print(int(parsed.timestamp()))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    marker = sub.add_parser("init-marker")
    marker.add_argument("--root", required=True)
    marker.add_argument("--marker", required=True)
    marker.set_defaults(handler=initialize_marker)

    sync = sub.add_parser("sync-directory")
    sync.add_argument("--directory", required=True)
    sync.add_argument("--parent", action="store_true")
    sync.set_defaults(handler=sync_directory)

    initial_state = sub.add_parser("init-state")
    initial_state.add_argument("--root", required=True)
    initial_state.add_argument("--state", required=True)
    initial_state.add_argument("--marker", required=True)
    initial_state.add_argument("--bootstrap", required=True)
    initial_state.add_argument("--writer-recovery", required=True)
    initial_state.add_argument("--mr", type=int, required=True)
    initial_state.add_argument("--max-rounds", type=int, required=True)
    initial_state.add_argument("--loop-id", required=True)
    initial_state.add_argument("--owner", required=True)
    initial_state.add_argument("--remote", required=True)
    initial_state.add_argument("--ref", required=True)
    initial_state.add_argument("--lock-object", required=True)
    initial_state.set_defaults(handler=initialize_state)

    state = sub.add_parser("state-update")
    state.add_argument("--state", required=True)
    state.add_argument("--lock")
    state.add_argument("--operation", choices=["writer", "observability", "round", "cleanup", "terminal"], required=True)
    state.add_argument("--expected-generation", type=int)
    state.add_argument("--expected-writer", default="__null__")
    state.add_argument("--caller-writer-id", default="")
    state.add_argument("--writer-id", default="")
    state.add_argument("--value", type=int, default=0)
    state.add_argument("--phase", default="")
    state.add_argument("--status", default="")
    state.add_argument("--processed-review-sha")
    state.add_argument("--awaiting-review-sha")
    state.add_argument("--rebuttal-key")
    state.add_argument("--rebuttal-evidence")
    state.add_argument("--increment-rebuttal-only", action="store_true")
    state.set_defaults(handler=mutate_state)

    recover = sub.add_parser("recover-writer")
    recover.add_argument("--state", required=True)
    recover.add_argument("--lock")
    recover.add_argument("--writer-recovery", required=True)
    recover.add_argument("--expected-generation", type=int, required=True)
    recover.add_argument("--expected-writer", required=True)
    recover.set_defaults(handler=recover_writer)

    lock = sub.add_parser("lock")
    lock_sub = lock.add_subparsers(dest="lock_command", required=True)
    acquire = lock_sub.add_parser("acquire")
    acquire.add_argument("--remote", required=True)
    acquire.add_argument("--ref", required=True)
    acquire.add_argument("--loop-id", required=True)
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--recovery-file")
    acquire.set_defaults(handler=acquire_lock)
    reconcile = lock_sub.add_parser("reconcile-acquire")
    reconcile.add_argument("--remote", required=True)
    reconcile.add_argument("--ref", required=True)
    reconcile.add_argument("--lock-object", required=True)
    reconcile.add_argument("--loop-id", required=True)
    reconcile.add_argument("--owner", required=True)
    reconcile.add_argument("--recovery-file", required=True)
    reconcile.set_defaults(handler=reconcile_lock_acquire)
    for name, handler in [("assert", assert_lock), ("release", release_lock)]:
        item = lock_sub.add_parser(name)
        item.add_argument("--remote", required=True)
        item.add_argument("--ref", required=True)
        item.add_argument("--lock-object", required=True)
        item.add_argument("--loop-id", required=True)
        item.add_argument("--owner", required=True)
        if name == "assert":
            item.add_argument("--state")
            item.add_argument("--lock")
            item.add_argument("--writer-id", default="")
            item.add_argument("--expected-generation", type=int)
        item.set_defaults(handler=handler)

    guarded = sub.add_parser("guarded-exec")
    guarded.add_argument("--state", required=True)
    guarded.add_argument("--lock")
    guarded.add_argument("--writer-id", default="")
    guarded.add_argument("--expected-generation", type=int, required=True)
    guarded.add_argument("--remote", required=True)
    guarded.add_argument("--ref", required=True)
    guarded.add_argument("--lock-object", required=True)
    guarded.add_argument("--loop-id", required=True)
    guarded.add_argument("--owner", required=True)
    guarded.add_argument("--timeout", type=float, required=True)
    guarded.add_argument("--stdin", action="store_true")
    guarded.add_argument("command", nargs=argparse.REMAINDER)
    guarded.set_defaults(handler=guarded_exec)

    authority = sub.add_parser("classify-cleanup-authority")
    authority.add_argument("--root", required=True)
    authority.add_argument("--state", required=True)
    authority.add_argument("--external", required=True)
    authority.add_argument("--remote", required=True)
    authority.add_argument("--ref", required=True)
    authority.add_argument("--lock-object", required=True)
    authority.add_argument("--loop-id", required=True)
    authority.add_argument("--owner", required=True)
    authority.set_defaults(handler=classify_cleanup_authority)

    clean = sub.add_parser("cleanup")
    clean.add_argument("--root", required=True)
    clean.add_argument("--state", required=True)
    clean.add_argument("--marker", required=True)
    clean.add_argument("--internal", required=True)
    clean.add_argument("--external", required=True)
    clean.add_argument("--remote", default="")
    clean.add_argument("--ref", default="")
    clean.add_argument("--lock-object", default="")
    clean.add_argument("--loop-id", default="")
    clean.add_argument("--owner", default="")
    clean.add_argument("--pre-lock-only", action="store_true")
    clean.set_defaults(handler=cleanup)

    remove_temps = sub.add_parser("remove-fetch-temps")
    remove_temps.add_argument("--root", required=True)
    remove_temps.add_argument("--path", action="append", required=True)
    remove_temps.set_defaults(handler=remove_fetch_temps)

    timeout = sub.add_parser("run")
    timeout.add_argument("--timeout", type=float, required=True)
    timeout.add_argument("command", nargs=argparse.REMAINDER)
    timeout.set_defaults(handler=run_with_timeout)

    iso = sub.add_parser("parse-iso")
    iso.add_argument("value")
    iso.set_defaults(handler=parse_iso)
    return root


def main() -> None:
    arguments = parser().parse_args()
    arguments.handler(arguments)


if __name__ == "__main__":
    main()
