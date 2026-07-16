#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""리뷰 루프 런타임 프리미티브를 위한 실행 가능한 회귀 테스트."""

from __future__ import annotations

import concurrent.futures
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().with_name("runtime.py")
SKILL = SCRIPT.parent.parent / "SKILL.md"
RUNTIME_SPEC = importlib.util.spec_from_file_location("codex_review_loop_runtime", SCRIPT)
assert RUNTIME_SPEC and RUNTIME_SPEC.loader
RUNTIME = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(RUNTIME)


def run_runtime(*arguments: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class ReviewLoopRuntimeTest(unittest.TestCase):
    def make_state(self, directory: Path) -> Path:
        directory.mkdir()
        directory.chmod(0o700)
        recovery_token = "test-writer-recovery-capability"
        recovery = directory / ".codex-review-loop.writer-recovery"
        recovery.write_text(recovery_token + "\n", encoding="utf-8")
        recovery.chmod(0o600)
        state = directory / "codex-loop-state.json"
        state.write_text(
            json.dumps(
                {
                    "mr": 560,
                    "scratchDir": str(directory),
                    "maxRounds": 8,
                    "status": "active",
                    "runnersStopped": False,
                    "fixRoundsDone": 0,
                    "awaitingReviewForSha": None,
                    "processedReviewShas": [],
                    "rebuttals": {},
                    "rebuttalOnlyStreak": 0,
                    "observabilityFailureStreak": 0,
                    "cleanupPhase": "active",
                    "generation": 0,
                    "writerId": "watcher-1",
                    "writerRecoveryHashes": [hashlib.sha256(recovery_token.encode()).hexdigest()],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        state.chmod(0o600)
        return state

    def set_lock_identity(
        self,
        state: Path,
        *,
        remote: Path,
        ref: str,
        lock_object: str,
        loop_id: str,
        owner: str,
    ) -> None:
        state_data = json.loads(state.read_text(encoding="utf-8"))
        state_data.update(
            {
                "lockRemote": str(remote),
                "lockRef": ref,
                "lockObject": lock_object,
                "loopId": loop_id,
                "owner": owner,
            }
        )
        state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
        state.chmod(0o600)

    def test_private_artifact_is_not_published_before_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / ".codex-review-loop.cleanup"
            with mock.patch.object(RUNTIME.os, "replace", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    RUNTIME._write_private(target, b"complete recovery token\n")
            self.assertFalse(target.exists())
            temporary_files = list(target.parent.glob(f"{target.name}.tmp.*"))
            self.assertEqual(1, len(temporary_files))
            self.assertEqual(b"complete recovery token\n", temporary_files[0].read_bytes())

    def test_cleanup_accepts_and_removes_bootstrap_publish_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            root.mkdir()
            root.chmod(0o700)
            marker = root / ".codex-review-loop.marker"
            state = root / "codex-loop-state.json"
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            initialized = run_runtime("init-marker", "--root", str(root), "--marker", str(marker))
            self.assertEqual(0, initialized.returncode, initialized.stderr)
            publish_temp = root / ".codex-review-loop.lock-bootstrap.tmp.999"
            publish_temp.write_text("fully written but unpublished\n", encoding="utf-8")
            publish_temp.chmod(0o600)
            cleaned = run_runtime(
                "cleanup", "--root", str(root), "--state", str(state),
                "--marker", str(marker), "--internal", str(internal), "--external", str(external),
                "--pre-lock-only",
            )
            self.assertEqual(0, cleaned.returncode, cleaned.stderr)
            self.assertFalse(root.exists())
            self.assertFalse(external.exists())

    def test_cleanup_accepts_marker_publish_temp_as_prepublish_only_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            root.mkdir()
            root.chmod(0o700)
            marker = root / ".codex-review-loop.marker"
            state = root / "codex-loop-state.json"
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            publish_temp = root / ".codex-review-loop.marker.tmp.999"
            publish_temp.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            publish_temp.chmod(0o600)
            interrupted = run_runtime(
                "cleanup", "--root", str(root), "--state", str(state),
                "--marker", str(marker), "--internal", str(internal), "--external", str(external),
                "--pre-lock-only",
                env={**os.environ, "CODEX_REVIEW_LOOP_FAIL_BEFORE_EXTERNAL_UNLINK": "1"},
            )
            self.assertEqual(77, interrupted.returncode, interrupted.stderr)
            self.assertFalse(root.exists())
            self.assertTrue(external.exists())
            recovered = run_runtime(
                "cleanup", "--root", str(root), "--state", str(state),
                "--marker", str(marker), "--internal", str(internal), "--external", str(external),
                "--pre-lock-only",
            )
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertFalse(external.exists())

    def test_pre_lock_cleanup_accepts_empty_unpublished_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            root.mkdir()
            root.chmod(0o700)
            result = run_runtime(
                "cleanup", "--root", str(root),
                "--state", str(root / "codex-loop-state.json"),
                "--marker", str(root / ".codex-review-loop.marker"),
                "--internal", str(root / ".codex-review-loop.cleanup"),
                "--external", str(root.with_name(root.name + ".cleanup")),
                "--pre-lock-only",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(root.exists())

    def test_cleanup_fsyncs_parent_after_root_removal_before_token_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            root.mkdir()
            root.chmod(0o700)
            external = root.with_name(root.name + ".cleanup")
            arguments = RUNTIME.argparse.Namespace(
                root=str(root),
                state=str(root / "codex-loop-state.json"),
                marker=str(root / ".codex-review-loop.marker"),
                internal=str(root / ".codex-review-loop.cleanup"),
                external=str(external),
                remote="",
                ref="",
                lock_object="",
                loop_id="",
                owner="",
                pre_lock_only=True,
            )
            observed: list[tuple[Path, bool]] = []
            real_fsync = RUNTIME._fsync_directory

            def record_fsync(directory: Path) -> None:
                observed.append((directory, root.exists()))
                real_fsync(directory)

            with mock.patch.object(RUNTIME, "_fsync_directory", side_effect=record_fsync), mock.patch.dict(
                os.environ, {"CODEX_REVIEW_LOOP_FAIL_BEFORE_EXTERNAL_UNLINK": "1"}
            ):
                with self.assertRaises(SystemExit) as interrupted:
                    RUNTIME.cleanup(arguments)
            self.assertEqual(77, interrupted.exception.code)
            self.assertFalse(root.exists())
            self.assertTrue(external.exists())
            self.assertIn((root.parent, False), observed)
            recovered = run_runtime(
                "cleanup", "--root", str(root),
                "--state", str(root / "codex-loop-state.json"),
                "--marker", str(root / ".codex-review-loop.marker"),
                "--internal", str(root / ".codex-review-loop.cleanup"),
                "--external", str(external), "--pre-lock-only",
            )
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertFalse(external.exists())

    def test_pre_lock_cleanup_accepts_published_marker_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            root.mkdir()
            root.chmod(0o700)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            result = run_runtime(
                "cleanup", "--root", str(root), "--state", str(root / "codex-loop-state.json"),
                "--marker", str(marker), "--internal", str(root / ".codex-review-loop.cleanup"),
                "--external", str(root.with_name(root.name + ".cleanup")), "--pre-lock-only",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(root.exists())

    def test_state_updates_are_serialized_and_generation_is_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.make_state(Path(temporary) / "codex-review-loop.test")

            def update(value: int) -> subprocess.CompletedProcess[str]:
                last_result: subprocess.CompletedProcess[str] | None = None
                for _ in range(32):
                    generation = str(json.loads(state.read_text(encoding="utf-8"))["generation"])
                    last_result = run_runtime(
                        "state-update",
                        "--state",
                        str(state),
                        "--operation",
                        "observability",
                        "--expected-generation",
                        generation,
                        "--writer-id",
                        "watcher-1",
                        "--value",
                        str(value),
                    )
                    if last_result.returncode != 3:
                        return last_result
                assert last_result is not None
                return last_result

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(update, range(8)))

            self.assertTrue(all(result.returncode == 0 for result in results), results)
            final_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(8, final_state["generation"])
            self.assertIn(final_state["observabilityFailureStreak"], range(8))

            stale = run_runtime(
                "state-update",
                "--state",
                str(state),
                "--operation",
                "observability",
                "--expected-generation",
                str(json.loads(state.read_text(encoding="utf-8"))["generation"]),
                "--writer-id",
                "old-watcher",
                "--value",
                "0",
            )
            self.assertEqual(3, stale.returncode)

    def test_writer_claim_and_handoff_require_the_retained_caller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.make_state(Path(temporary) / "codex-review-loop.test")
            state_data = json.loads(state.read_text(encoding="utf-8"))
            state_data["writerId"] = None
            state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
            state.chmod(0o600)

            initial = run_runtime(
                "state-update", "--state", str(state), "--operation", "writer",
                "--expected-generation", "0", "--expected-writer", "__null__",
                "--writer-id", "runner-1",
            )
            self.assertEqual(0, initial.returncode, initial.stderr)
            handoff = run_runtime(
                "state-update", "--state", str(state), "--operation", "writer",
                "--expected-generation", "1", "--expected-writer", "runner-1",
                "--caller-writer-id", "runner-1", "--writer-id", "runner-2",
            )
            self.assertEqual(0, handoff.returncode, handoff.stderr)
            stale_reacquire = run_runtime(
                "state-update", "--state", str(state), "--operation", "writer",
                "--expected-generation", "2", "--expected-writer", "runner-2",
                "--caller-writer-id", "runner-1", "--writer-id", "runner-1-reacquired",
            )
            self.assertEqual(3, stale_reacquire.returncode, stale_reacquire.stderr)
            self.assertEqual("runner-2", json.loads(state.read_text(encoding="utf-8"))["writerId"])

    def test_writer_recovery_rotates_durable_capability_after_session_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            state = self.make_state(root)
            recovery = root / ".codex-review-loop.writer-recovery"
            old_token = "recovery-capability-one"
            recovery.write_text(old_token + "\n", encoding="utf-8")
            recovery.chmod(0o600)
            state_data = json.loads(state.read_text(encoding="utf-8"))
            state_data["writerRecoveryHashes"] = [hashlib.sha256(old_token.encode()).hexdigest()]
            state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
            state.chmod(0o600)

            recovered = run_runtime(
                "recover-writer", "--state", str(state), "--writer-recovery", str(recovery),
                "--expected-generation", "0", "--expected-writer", "watcher-1",
            )
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            recovered_writer = recovered.stdout.strip()
            self.assertTrue(recovered_writer.startswith("recovered-"))
            recovered_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(recovered_writer, recovered_state["writerId"])
            self.assertEqual(1, recovered_state["generation"])
            self.assertNotEqual(old_token, recovery.read_text(encoding="utf-8").strip())

            recovery.write_text(old_token + "\n", encoding="utf-8")
            recovery.chmod(0o600)
            stale_capability = run_runtime(
                "recover-writer", "--state", str(state), "--writer-recovery", str(recovery),
                "--expected-generation", "1", "--expected-writer", recovered_writer,
            )
            self.assertEqual(3, stale_capability.returncode, stale_capability.stderr)

    def test_mr_lock_allows_only_one_concurrent_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            bare = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"

            def acquire(loop_id: str) -> subprocess.CompletedProcess[str]:
                return run_runtime(
                    "lock",
                    "acquire",
                    "--remote",
                    str(bare),
                    "--ref",
                    ref,
                    "--loop-id",
                    loop_id,
                    "--owner",
                    f"tester@{loop_id}",
                    "--recovery-file",
                    str(root / f"{loop_id}.bootstrap"),
                    cwd=repository,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first, second = list(pool.map(acquire, ["loop-a", "loop-b"]))

            self.assertEqual(1, sum(result.returncode == 0 for result in (first, second)), (first, second))
            lock_sha = subprocess.run(
                ["git", "--git-dir", str(bare), "rev-parse", ref],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            owner = first.stdout.strip() if first.returncode == 0 else second.stdout.strip()
            self.assertEqual(owner, lock_sha)
            owner_id = "loop-a" if first.returncode == 0 else "loop-b"
            owner_name = f"tester@{owner_id}"
            reconciled = run_runtime(
                "lock", "reconcile-acquire", "--remote", str(bare), "--ref", ref,
                "--lock-object", owner, "--loop-id", owner_id,
                "--owner", owner_name, "--recovery-file", str(root / f"{owner_id}.bootstrap"),
                cwd=repository,
            )
            self.assertEqual(0, reconciled.returncode, reconciled.stderr)
            self.assertEqual(owner, reconciled.stdout.strip())
            subprocess.run(["git", "reflog", "expire", "--expire=now", "--all"], cwd=repository, check=True)
            subprocess.run(["git", "prune", "--expire=now"], cwd=repository, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repository, check=True)
            asserted = run_runtime(
                "lock", "assert", "--remote", "origin", "--ref", ref,
                "--lock-object", owner, "--loop-id", owner_id, "--owner", owner_name,
                cwd=repository,
            )
            self.assertEqual(0, asserted.returncode, asserted.stderr)
            released = run_runtime(
                "lock",
                "release",
                "--remote",
                str(bare),
                "--ref",
                ref,
                "--lock-object",
                owner,
                "--loop-id",
                owner_id,
                "--owner",
                owner_name,
                cwd=repository,
            )
            self.assertEqual(0, released.returncode, released.stderr)

    def test_pre_lock_cleanup_accepts_pending_bootstrap_after_definitive_lock_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repo"
            bare = base / "remote.git"
            root = base / "codex-review-loop.loser"
            root.mkdir()
            root.chmod(0o700)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            bootstrap = root / ".codex-review-loop.lock-bootstrap"
            ref = "refs/heads/codex-review-locks/mr-560"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            incumbent = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "incumbent", "--owner", "tester@incumbent", cwd=repository,
            )
            self.assertEqual(0, incumbent.returncode, incumbent.stderr)
            loser = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "loser", "--owner", "tester@loser",
                "--recovery-file", str(bootstrap), cwd=repository,
            )
            self.assertEqual(2, loser.returncode)
            lock_object = next(
                line.split(":", 1)[1]
                for line in bootstrap.read_text(encoding="utf-8").splitlines()
                if line.startswith("lockObject:")
            )
            self.assertIn("outcome:pending", bootstrap.read_text(encoding="utf-8"))
            bootstrap.write_bytes(
                RUNTIME._lock_record_payload(
                    str(bare), ref, "loser", "tester@loser", lock_object,
                )
            )
            bootstrap.chmod(0o600)
            rejected_normalized = run_runtime(
                "cleanup", "--root", str(root), "--state", str(root / "codex-loop-state.json"),
                "--marker", str(marker), "--internal", str(root / ".codex-review-loop.cleanup"),
                "--external", str(root.with_name(root.name + ".cleanup")),
                "--remote", str(bare), "--ref", ref, "--lock-object", lock_object,
                "--loop-id", "loser", "--owner", "tester@loser", "--pre-lock-only",
                cwd=repository,
            )
            self.assertEqual(1, rejected_normalized.returncode)
            self.assertTrue(root.exists())
            bootstrap.write_bytes(
                RUNTIME._lock_record_payload(
                    str(bare), ref, "loser", "tester@loser", lock_object, outcome="pending",
                )
            )
            bootstrap.chmod(0o600)
            cleaned = run_runtime(
                "cleanup", "--root", str(root), "--state", str(root / "codex-loop-state.json"),
                "--marker", str(marker), "--internal", str(root / ".codex-review-loop.cleanup"),
                "--external", str(root.with_name(root.name + ".cleanup")),
                "--remote", str(bare), "--ref", ref, "--lock-object", lock_object,
                "--loop-id", "loser", "--owner", "tester@loser", "--pre-lock-only",
                cwd=repository,
            )
            self.assertEqual(0, cleaned.returncode, cleaned.stderr)
            self.assertFalse(root.exists())
            self.assertEqual(
                incumbent.stdout.strip(),
                subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-parse", ref],
                    check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip(),
            )

    def test_lock_acquire_timeout_preserves_recovery_record_and_returns_124(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recovery = Path(temporary) / "lock-bootstrap"
            candidate = "a" * 40
            empty_remote = subprocess.CompletedProcess(["git"], 0, "", "")
            timed_out_push = subprocess.CompletedProcess(["git"], 124, "", "timed out")
            arguments = RUNTIME.argparse.Namespace(
                loop_id="loop-timeout",
                owner="tester@timeout",
                recovery_file=str(recovery),
                remote="origin",
                ref="refs/heads/codex-review-locks/mr-560",
            )
            with mock.patch.object(RUNTIME, "_lock_commit", return_value=candidate), mock.patch.object(
                RUNTIME,
                "_git",
                side_effect=[empty_remote, timed_out_push],
            ):
                with self.assertRaises(SystemExit) as raised:
                    RUNTIME.acquire_lock(arguments)
            self.assertEqual(124, raised.exception.code)
            self.assertTrue(recovery.exists())
            self.assertIn(f"lockObject:{candidate}\n", recovery.read_text(encoding="utf-8"))
            self.assertIn("outcome:indeterminate\n", recovery.read_text(encoding="utf-8"))

    def test_reconcile_adopts_delayed_lock_success_and_clears_indeterminate_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repo"
            bare = base / "remote.git"
            recovery = base / "lock-bootstrap"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "loop-delayed", "--owner", "tester@delayed",
                "--recovery-file", str(recovery), cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            lock_object = acquired.stdout.strip()
            recovery.write_text(recovery.read_text(encoding="utf-8") + "outcome:indeterminate\n", encoding="utf-8")
            recovery.chmod(0o600)
            reconciled = run_runtime(
                "lock", "reconcile-acquire", "--remote", str(bare), "--ref", ref,
                "--lock-object", lock_object, "--loop-id", "loop-delayed",
                "--owner", "tester@delayed", "--recovery-file", str(recovery), cwd=repository,
            )
            self.assertEqual(0, reconciled.returncode, reconciled.stderr)
            self.assertEqual(lock_object, reconciled.stdout.strip())
            self.assertNotIn("outcome:", recovery.read_text(encoding="utf-8"))

    def test_reconcile_rejects_malformed_recovery_before_remote_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repo"
            bare = base / "remote.git"
            recovery = base / "lock-bootstrap"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "loop-malformed", "--owner", "tester@malformed",
                "--recovery-file", str(recovery), cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            lock_object = acquired.stdout.strip()
            subprocess.run(["git", "--git-dir", str(bare), "update-ref", "-d", ref], check=True)
            recovery.write_text(
                recovery.read_text(encoding="utf-8").replace(
                    "owner:tester@malformed", "owner:tester@forged",
                ),
                encoding="utf-8",
            )
            recovery.chmod(0o600)

            rejected = run_runtime(
                "lock", "reconcile-acquire", "--remote", str(bare), "--ref", ref,
                "--lock-object", lock_object, "--loop-id", "loop-malformed",
                "--owner", "tester@malformed", "--recovery-file", str(recovery),
                cwd=repository,
            )
            self.assertNotEqual(0, rejected.returncode)
            self.assertNotEqual(
                0,
                subprocess.run(
                    ["git", "--git-dir", str(bare), "show-ref", "--verify", "--quiet", ref],
                ).returncode,
            )
            omitted = run_runtime(
                "lock", "reconcile-acquire", "--remote", str(bare), "--ref", ref,
                "--lock-object", lock_object, "--loop-id", "loop-malformed",
                "--owner", "tester@malformed", cwd=repository,
            )
            self.assertNotEqual(0, omitted.returncode)
            self.assertNotEqual(
                0,
                subprocess.run(
                    ["git", "--git-dir", str(bare), "show-ref", "--verify", "--quiet", ref],
                ).returncode,
            )

    def test_lock_release_is_compare_and_delete_and_missing_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            bare = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "loop-a", "--owner", "tester@a", cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            owner_a = acquired.stdout.strip()

            victim_ref = "refs/heads/not-a-review-loop-lock"
            subprocess.run(
                ["git", "push", str(bare), f"{owner_a}:{victim_ref}"],
                cwd=repository, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            rejected_non_lock_release = run_runtime(
                "lock", "release", "--remote", str(bare), "--ref", victim_ref,
                "--lock-object", owner_a, "--loop-id", "loop-a", "--owner", "tester@a",
                cwd=repository,
            )
            self.assertNotEqual(0, rejected_non_lock_release.returncode)
            self.assertEqual(
                owner_a,
                subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-parse", victim_ref],
                    check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip(),
            )

            # Use the local object database to create a replacement commit and
            # mutate only the bare test ref, simulating a new owner winning a
            # lease race after the stale owner observed the old object.
            empty_tree = subprocess.run(
                ["git", "mktree"], cwd=repository, input="", text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            replacement = subprocess.run(
                ["git", "commit-tree", empty_tree],
                cwd=repository,
                input="replacement lock\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "test",
                    "GIT_AUTHOR_EMAIL": "test@localhost",
                    "GIT_COMMITTER_NAME": "test",
                    "GIT_COMMITTER_EMAIL": "test@localhost",
                },
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "push", str(bare), f"{replacement}:refs/heads/replacement"],
                cwd=repository, check=True, stdout=subprocess.PIPE,
            )
            subprocess.run(["git", "--git-dir", str(bare), "update-ref", ref, replacement], check=True)

            stale_release = run_runtime(
                "lock", "release", "--remote", str(bare), "--ref", ref,
                "--lock-object", owner_a, "--loop-id", "loop-a", "--owner", "tester@a",
                cwd=repository,
            )
            self.assertEqual(3, stale_release.returncode)
            current = subprocess.run(
                ["git", "--git-dir", str(bare), "rev-parse", ref],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(replacement, current)

            subprocess.run(["git", "--git-dir", str(bare), "update-ref", "-d", ref], check=True)
            already_released = run_runtime(
                "lock", "release", "--remote", str(bare), "--ref", ref,
                "--lock-object", replacement, "--loop-id", "loop-a", "--owner", "tester@a",
                cwd=repository,
            )
            self.assertEqual(0, already_released.returncode, already_released.stderr)

    def test_terminal_release_accepts_replacement_after_failed_lease_only(self) -> None:
        candidate = "a" * 40
        replacement = "b" * 40
        arguments = RUNTIME.argparse.Namespace(
            remote="origin",
            ref="refs/heads/codex-review-locks/mr-560",
            lock_object=candidate,
            loop_id="loop-old",
            owner="tester@old",
            allow_replaced=True,
        )
        failed_lease = subprocess.CompletedProcess(["git", "push"], 1, "", "lease failed")
        with mock.patch.object(
            RUNTIME, "_remote_lock_sha", side_effect=[candidate, replacement]
        ), mock.patch.object(RUNTIME, "_validate_lock_payload"), mock.patch.object(
            RUNTIME, "_git", return_value=failed_lease
        ):
            self.assertEqual("replaced", RUNTIME.release_lock(arguments))
        arguments.allow_replaced = False
        with mock.patch.object(RUNTIME, "_remote_lock_sha", return_value=replacement):
            with self.assertRaises(SystemExit) as raised:
                RUNTIME.release_lock(arguments)
        self.assertEqual(3, raised.exception.code)

    def test_strict_release_rejects_replacement_after_failed_lease(self) -> None:
        candidate = "a" * 40
        replacement = "b" * 40
        arguments = RUNTIME.argparse.Namespace(
            remote="origin",
            ref="refs/heads/codex-review-locks/mr-560",
            lock_object=candidate,
            loop_id="loop-old",
            owner="tester@old",
            allow_replaced=False,
        )
        failed_lease = subprocess.CompletedProcess(["git", "push"], 1, "", "lease failed")
        with mock.patch.object(
            RUNTIME, "_remote_lock_sha", side_effect=[candidate, replacement]
        ), mock.patch.object(RUNTIME, "_validate_lock_payload"), mock.patch.object(
            RUNTIME, "_git", return_value=failed_lease
        ):
            with self.assertRaises(SystemExit) as raised:
                RUNTIME.release_lock(arguments)
        self.assertEqual(3, raised.exception.code)

    def test_lock_assert_rejects_replaced_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = self.make_state(base / "codex-review-loop.test")
            repository = base / "repo"
            bare = base / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "loop-writer-fence", "--owner", "tester@writer-fence",
                cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            lock_object = acquired.stdout.strip()
            self.set_lock_identity(
                state,
                remote=bare,
                ref=ref,
                lock_object=lock_object,
                loop_id="loop-writer-fence",
                owner="tester@writer-fence",
            )

            initial_assert = run_runtime(
                "lock", "assert", "--remote", str(bare), "--ref", ref,
                "--lock-object", lock_object, "--loop-id", "loop-writer-fence",
                "--owner", "tester@writer-fence", "--state", str(state),
                "--writer-id", "watcher-1", "--expected-generation", "0", cwd=repository,
            )
            self.assertEqual(0, initial_assert.returncode, initial_assert.stderr)

            replaced = run_runtime(
                "state-update", "--state", str(state), "--operation", "writer",
                "--expected-generation", "0", "--expected-writer", "watcher-1",
                "--caller-writer-id", "watcher-1", "--writer-id", "watcher-2",
            )
            self.assertEqual(0, replaced.returncode, replaced.stderr)

            stale_assert = run_runtime(
                "lock", "assert", "--remote", str(bare), "--ref", ref,
                "--lock-object", lock_object, "--loop-id", "loop-writer-fence",
                "--owner", "tester@writer-fence", "--state", str(state),
                "--writer-id", "watcher-1", "--expected-generation", "1", cwd=repository,
            )
            self.assertEqual(3, stale_assert.returncode, stale_assert.stderr)
            current_assert = run_runtime(
                "lock", "assert", "--remote", str(bare), "--ref", ref,
                "--lock-object", lock_object, "--loop-id", "loop-writer-fence",
                "--owner", "tester@writer-fence", "--state", str(state),
                "--writer-id", "watcher-2", "--expected-generation", "1", cwd=repository,
            )
            self.assertEqual(0, current_assert.returncode, current_assert.stderr)

    def test_guarded_exec_holds_writer_fence_through_external_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = self.make_state(base / "codex-review-loop.test")
            repository = base / "repo"
            bare = base / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            loop_id = "loop-guarded-effect"
            owner = "tester@guarded-effect"
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", loop_id, "--owner", owner, cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            lock_object = acquired.stdout.strip()
            self.set_lock_identity(
                state,
                remote=bare,
                ref=ref,
                lock_object=lock_object,
                loop_id=loop_id,
                owner=owner,
            )
            started = base / "guarded-started"
            release = base / "guarded-release"
            effect = base / "guarded-effect"
            effect_code = (
                "import pathlib,sys,time; "
                "started,release,effect=map(pathlib.Path,sys.argv[1:]); started.touch(); "
                "deadline=time.monotonic()+5; "
                "exec('while not release.exists():\\n if time.monotonic() >= deadline: raise SystemExit(9)\\n time.sleep(0.01)'); "
                "effect.touch()"
            )
            guarded = subprocess.Popen(
                [
                    sys.executable, str(SCRIPT), "guarded-exec",
                    "--state", str(state), "--writer-id", "watcher-1",
                    "--expected-generation", "0", "--remote", str(bare),
                    "--ref", ref, "--lock-object", lock_object,
                    "--loop-id", loop_id, "--owner", owner,
                    "--timeout", "10", "--", sys.executable, "-c", effect_code,
                    str(started), str(release), str(effect),
                ],
                cwd=repository,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(started.exists(), "guarded command did not start")

            replacement = subprocess.Popen(
                [
                    sys.executable, str(SCRIPT), "state-update", "--state", str(state),
                    "--operation", "writer", "--expected-generation", "0",
                    "--expected-writer", "watcher-1", "--caller-writer-id", "watcher-1",
                    "--writer-id", "watcher-2",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.1)
            self.assertIsNone(replacement.poll(), "writer replacement escaped the guarded critical section")
            release.touch()
            guarded_stdout, guarded_stderr = guarded.communicate(timeout=5)
            self.assertEqual(0, guarded.returncode, guarded_stderr or guarded_stdout)
            self.assertTrue(effect.exists())
            replacement_stdout, replacement_stderr = replacement.communicate(timeout=5)
            self.assertEqual(0, replacement.returncode, replacement_stderr or replacement_stdout)
            self.assertEqual("watcher-2", json.loads(state.read_text(encoding="utf-8"))["writerId"])

    def test_guarded_exec_detects_remote_lock_loss_during_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = self.make_state(base / "codex-review-loop.test")
            repository = base / "repo"
            bare = base / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            loop_id = "loop-remote-loss"
            owner = "tester@remote-loss"
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", loop_id, "--owner", owner, cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            lock_object = acquired.stdout.strip()
            self.set_lock_identity(
                state, remote=bare, ref=ref, lock_object=lock_object,
                loop_id=loop_id, owner=owner,
            )
            started = base / "effect-started"
            release = base / "effect-release"
            effect = base / "effect-finished"
            effect_code = (
                "import pathlib,sys,time; "
                "started,release,effect=map(pathlib.Path,sys.argv[1:]); started.touch(); "
                "exec('while not release.exists(): time.sleep(0.01)'); effect.touch()"
            )
            guarded = subprocess.Popen(
                [
                    sys.executable, str(SCRIPT), "guarded-exec", "--state", str(state),
                    "--writer-id", "watcher-1", "--expected-generation", "0",
                    "--remote", str(bare), "--ref", ref, "--lock-object", lock_object,
                    "--loop-id", loop_id, "--owner", owner, "--timeout", "10", "--",
                    sys.executable, "-c", effect_code, str(started), str(release), str(effect),
                ],
                cwd=repository,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(started.exists())
            empty_tree = subprocess.run(
                ["git", "mktree"], cwd=repository, input="", text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            replacement = subprocess.run(
                ["git", "commit-tree", empty_tree], cwd=repository, input="replacement\n",
                text=True, stdout=subprocess.PIPE, check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@localhost",
                    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@localhost",
                },
            ).stdout.strip()
            subprocess.run(
                ["git", "push", str(bare), f"{replacement}:refs/heads/replacement"],
                cwd=repository, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            subprocess.run(["git", "--git-dir", str(bare), "update-ref", ref, replacement], check=True)
            release.touch()
            guarded_stdout, guarded_stderr = guarded.communicate(timeout=5)
            self.assertEqual(3, guarded.returncode, guarded_stderr or guarded_stdout)
            self.assertTrue(effect.exists())

    def test_run_timeout_kills_the_entire_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "child-survived-timeout"
            child_code = (
                "import os,pathlib,signal,sys,time; os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(2); pathlib.Path(sys.argv[1]).touch()"
            )
            parent_code = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
                "time.sleep(10)"
            )
            started = time.monotonic()
            timed_out = run_runtime(
                "run", "--timeout", "0.1", "--", sys.executable, "-c", parent_code,
                child_code, str(marker),
            )
            self.assertEqual(124, timed_out.returncode, timed_out.stderr)
            self.assertLess(time.monotonic() - started, 3.0)
            time.sleep(max(0.0, started + 2.5 - time.monotonic()))
            self.assertFalse(marker.exists())

    def test_run_rejects_success_when_detached_descendant_outlives_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "detached-descendant-effect"
            ready = Path(temporary) / "detached-descendant-ready"
            grandchild_code = "import pathlib,sys,time; time.sleep(1); pathlib.Path(sys.argv[1]).touch()"
            child_code = (
                "import os,pathlib,signal,subprocess,sys,time; os.setsid(); "
                "effect,ready,grandchild=sys.argv[1:]; "
                "signal.signal(signal.SIGTERM, lambda *_: (subprocess.Popen([sys.executable, '-c', grandchild, effect], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), time.sleep(1))); "
                "pathlib.Path(ready).touch(); time.sleep(10)"
            )
            parent_code = (
                "import pathlib,subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[2:]], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "ready=pathlib.Path(sys.argv[3]); "
                "exec('while not ready.exists(): time.sleep(0.01)')"
            )
            started = time.monotonic()
            result = run_runtime(
                "run", "--timeout", "5", "--", sys.executable, "-c", parent_code,
                child_code, str(marker), str(ready), grandchild_code,
            )
            self.assertEqual(125, result.returncode, result.stderr)
            self.assertLess(time.monotonic() - started, 3.0)
            time.sleep(max(0.0, started + 2.5 - time.monotonic()))
            self.assertFalse(marker.exists())

    def test_run_reaps_exited_adopted_descendant_without_rejecting_success(self) -> None:
        parent_code = (
            "import os,time; pid=os.fork(); "
            "os._exit(0) if pid == 0 else time.sleep(0.1)"
        )
        result = run_runtime(
            "run", "--timeout", "2", "--", sys.executable, "-c", parent_code,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_run_forwards_command_output(self) -> None:
        result = run_runtime("run", "--timeout", "1", "--", sys.executable, "-c", "print('visible')")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("visible\n", result.stdout)

    def test_external_command_runner_has_no_descendant_containment_bypass(self) -> None:
        self.assertNotIn("strict_descendants", inspect.signature(RUNTIME._run_command).parameters)
        self.assertIn("gc.auto=0", RUNTIME._git_command("status"))
        self.assertIn("maintenance.autoDetach=false", RUNTIME._git_command("status"))
        completed = subprocess.CompletedProcess(["git", "status"], 0, "", "")
        with mock.patch.object(RUNTIME, "_run_command", return_value=completed) as run_command:
            result = RUNTIME._git("status")
        self.assertIs(completed, result)
        run_command.assert_called_once_with(
            RUNTIME._git_command("status"),
            input_text=None,
            timeout=RUNTIME.GIT_COMMAND_TIMEOUT_SECONDS,
        )

    def test_shell_guard_requires_caller_writer_instead_of_reacquiring_authority(self) -> None:
        skill = SKILL.read_text(encoding="utf-8")
        guard = skill.split("assert_remote_lock() {", 1)[1].split("assert_private_artifact() {", 1)[0]
        self.assertNotIn(".writerId", guard)
        self.assertIn('local caller_writer="$1" generation', guard)
        self.assertIn('local stdin_mode="$1" caller_writer="$2" timeout="$3" generation', guard)
        self.assertIn('--writer-id "$caller_writer"', guard)
        self.assertIn('writer_id="$(claim_writer "$previous_writer" "$caller_writer")"', skill)
        self.assertIn('CODEX_REVIEW_LOOP_CALLER_WRITER=$writer_id', skill)
        self.assertIn("classify-cleanup-authority", skill)
        self.assertIn('recovery_remote_handled=0', skill)
        self.assertIn('[ "$recovery_remote_handled" -eq 0 ] && [ -f "$recovery_bootstrap" ]', skill)
        resume_block = skill.split('if [ "$resume_cleanup_only" -eq 1 ]; then', 1)[1]
        self.assertLess(
            resume_block.index("classify-cleanup-authority"),
            resume_block.index("lock reconcile-acquire"),
        )
        self.assertIn('resume_publish_temp_present=1', skill)
        self.assertIn('resume_marker_only=1', skill)
        self.assertIn("phase:pre-lock", skill)
        self.assertIn("--pre-lock-only", skill)
        self.assertIn("grep -Eq '^outcome:(pending|indeterminate)$'", skill)
        self.assertIn("recovery_pre_lock=1", skill)
        self.assertIn("remove-fetch-temps", skill)
        self.assertNotIn('rm -f -- "$mr_pages"', skill)
        self.assertNotIn('rm -f -- "$lock_bootstrap" "$loop_marker"', skill)
        self.assertNotIn("recovery_bootstrap\" ] && grep -Fxq", skill)
        self.assertNotIn('rm -f -- "$recovery_bootstrap"', skill)
        self.assertIn("initial lock candidate no longer owns", skill)

    def test_cleanup_authority_classification_precedes_bootstrap_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            state = self.make_state(root)
            external = root.with_name(root.name + ".cleanup")
            remote = "origin"
            ref = "refs/heads/codex-review-locks/mr-560"
            lock_object = "a" * 40
            loop_id = "loop-terminal"
            owner = "tester@terminal"
            state_data = json.loads(state.read_text(encoding="utf-8"))
            state_data.update(
                {
                    "status": "converged",
                    "runnersStopped": True,
                    "cleanupPhase": "lock_released",
                    "lockRemote": remote,
                    "lockRef": ref,
                    "lockObject": lock_object,
                    "loopId": loop_id,
                    "owner": owner,
                }
            )
            state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
            state.chmod(0o600)
            arguments = (
                "classify-cleanup-authority", "--root", str(root),
                "--state", str(state), "--external", str(external),
                "--remote", remote, "--ref", ref, "--lock-object", lock_object,
                "--loop-id", loop_id, "--owner", owner,
            )
            external.write_bytes(
                RUNTIME._token_payload(root, remote, ref, lock_object, loop_id, owner)
            )
            external.chmod(0o600)
            state_before_token_upgrade = run_runtime(*arguments)
            self.assertEqual(0, state_before_token_upgrade.returncode, state_before_token_upgrade.stderr)

            external.write_bytes(
                RUNTIME._lock_released_token_payload(
                    root, remote, ref, lock_object, loop_id, owner,
                )
            )
            external.chmod(0o600)
            state.unlink()
            state_less_released = run_runtime(*arguments)
            self.assertEqual(0, state_less_released.returncode, state_less_released.stderr)

            active_state = self.make_state(root.with_name("codex-review-loop.active"))
            active_root = active_state.parent
            active_external = active_root.with_name(active_root.name + ".cleanup")
            active_data = json.loads(active_state.read_text(encoding="utf-8"))
            active_data.update(
                {
                    "lockRemote": remote,
                    "lockRef": ref,
                    "lockObject": lock_object,
                    "loopId": loop_id,
                    "owner": owner,
                }
            )
            active_state.write_text(json.dumps(active_data) + "\n", encoding="utf-8")
            active_state.chmod(0o600)
            active_external.write_bytes(
                RUNTIME._lock_released_token_payload(
                    active_root, remote, ref, lock_object, loop_id, owner,
                )
            )
            active_external.chmod(0o600)
            active_released = run_runtime(
                "classify-cleanup-authority", "--root", str(active_root),
                "--state", str(active_state), "--external", str(active_external),
                "--remote", remote, "--ref", ref, "--lock-object", lock_object,
                "--loop-id", loop_id, "--owner", owner,
            )
            self.assertNotEqual(0, active_released.returncode)

    def test_resume_classification_prefers_phase_token_over_empty_root_shape(self) -> None:
        skill_lines = SKILL.read_text(encoding="utf-8").splitlines()
        condition = next(
            line.strip()
            for line in skill_lines
            if line.strip().startswith('if { [ "$resume_empty_root" -eq 1 ]')
        )
        self.assertIn('[ ! -e "$recovery_root.cleanup" ]', condition)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            root.mkdir()
            external = root.with_name(root.name + ".cleanup")
            external.write_text(
                f"codex-review-loop:{root}\n"
                "lockRemote:origin\n"
                "lockRef:refs/heads/codex-review-locks/mr-560\n"
                f"lockObject:{'a' * 40}\n"
                "loopId:loop-old\n"
                "owner:tester@old\n"
                "phase:lock-released\n",
                encoding="utf-8",
            )
            external.chmod(0o600)
            script = "\n".join(
                [
                    'recovery_root="$1"',
                    "resume_empty_root=1",
                    "resume_marker_only=0",
                    "resume_publish_temp_present=0",
                    "recovery_pre_lock=0",
                    condition,
                    "  recovery_pre_lock=1",
                    "fi",
                    'printf "%s" "$recovery_pre_lock"',
                ]
            )
            classified = subprocess.run(
                ["bash", "-c", script, "resume-classifier", str(root)],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertEqual("0", classified.stdout)
            external.unlink()
            pre_lock = subprocess.run(
                ["bash", "-c", script, "resume-classifier", str(root)],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            self.assertEqual("1", pre_lock.stdout)

    def test_bootstrap_identity_failure_does_not_poison_external_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "codex-review-loop.test"
            root.mkdir()
            root.chmod(0o700)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            state = root / "codex-loop-state.json"
            bootstrap = root / ".codex-review-loop.lock-bootstrap"
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            bare = base / "remote.git"
            repository = base / "repo"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            loop_id = "loop-bootstrap"
            owner = "tester@bootstrap"
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", loop_id, "--owner", owner, cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            lock_object = acquired.stdout.strip()
            bootstrap.write_bytes(
                RUNTIME._lock_record_payload(str(bare), ref, loop_id, owner, lock_object)
            )
            bootstrap.chmod(0o600)
            common = (
                "cleanup", "--root", str(root), "--state", str(state),
                "--marker", str(marker), "--internal", str(internal), "--external", str(external),
                "--remote", str(bare), "--ref", ref, "--lock-object", lock_object,
                "--loop-id", loop_id,
            )
            rejected = run_runtime(*common, "--owner", "tester@wrong", cwd=repository)
            self.assertNotEqual(0, rejected.returncode)
            self.assertTrue(root.exists())
            self.assertFalse(external.exists())
            recovered = run_runtime(*common, "--owner", owner, cwd=repository)
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertFalse(root.exists())
            self.assertFalse(external.exists())

    def test_terminal_cleanup_rejects_wrong_loop_identity_before_remote_state(self) -> None:
        for remote_state in ("missing", "replaced"):
            with self.subTest(remote_state=remote_state), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "codex-review-loop.test"
                state = self.make_state(root)
                marker = root / ".codex-review-loop.marker"
                marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
                marker.chmod(0o600)
                internal = root / ".codex-review-loop.cleanup"
                external = root.with_name(root.name + ".cleanup")
                bare = base / "remote.git"
                repository = base / "repo"
                subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
                subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
                ref = "refs/heads/codex-review-locks/mr-560"
                candidate = "a" * 40
                state_data = json.loads(state.read_text(encoding="utf-8"))
                state_data.update(
                    {
                        "status": "converged",
                        "runnersStopped": True,
                        "lockRemote": str(bare),
                        "lockRef": ref,
                        "lockObject": candidate,
                        "loopId": "loop-terminal",
                        "owner": "tester@terminal",
                    }
                )
                state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
                state.chmod(0o600)
                if remote_state == "replaced":
                    replacement = run_runtime(
                        "lock", "acquire", "--remote", str(bare), "--ref", ref,
                        "--loop-id", "loop-new", "--owner", "tester@new", cwd=repository,
                    )
                    self.assertEqual(0, replacement.returncode, replacement.stderr)
                rejected = run_runtime(
                    "cleanup", "--root", str(root), "--state", str(state),
                    "--marker", str(marker), "--internal", str(internal), "--external", str(external),
                    "--remote", str(bare), "--ref", ref, "--lock-object", candidate,
                    "--loop-id", "loop-wrong", "--owner", "tester@wrong", cwd=repository,
                )
                self.assertNotEqual(0, rejected.returncode)
                self.assertTrue(root.exists())
                self.assertTrue(state.exists())
                self.assertFalse(external.exists())

    def test_typed_round_updates_and_terminal_require_current_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = self.make_state(base / "codex-review-loop.test")
            repository = base / "repo"
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            subprocess.run(
                ["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "--allow-empty", "-m", "one"],
                check=True,
                stdout=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "--allow-empty", "-m", "two"],
                check=True,
                stdout=subprocess.PIPE,
            )
            second_sha = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            external_review_sha = "A" * 40
            self.assertNotEqual(
                0,
                subprocess.run(
                    ["git", "-C", str(repository), "cat-file", "-e", external_review_sha],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).returncode,
            )

            def update(*extra: str) -> subprocess.CompletedProcess[str]:
                generation = str(json.loads(state.read_text(encoding="utf-8"))["generation"])
                return run_runtime(
                    "state-update", "--state", str(state), "--operation", "round",
                    "--writer-id", "watcher-1", "--expected-generation", generation, *extra,
                    cwd=repository,
                )

            legacy_state = json.loads(state.read_text(encoding="utf-8"))
            legacy_state["processedReviewShas"] = [external_review_sha[:8].lower()]
            state.write_text(json.dumps(legacy_state) + "\n", encoding="utf-8")
            state.chmod(0o600)
            rejected_short_review = update("--processed-review-sha", "abc1234")
            self.assertNotEqual(0, rejected_short_review.returncode)
            processed = update("--processed-review-sha", external_review_sha)
            self.assertEqual(0, processed.returncode, processed.stderr)
            after_processed = json.loads(state.read_text(encoding="utf-8"))
            repeated_processed = update("--processed-review-sha", external_review_sha.lower())
            self.assertEqual(0, repeated_processed.returncode, repeated_processed.stderr)
            repeated_processed_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(after_processed["generation"], repeated_processed_state["generation"])
            pushed = update("--awaiting-review-sha", second_sha[:8])
            self.assertEqual(0, pushed.returncode, pushed.stderr)
            after_push = json.loads(state.read_text(encoding="utf-8"))
            after_push["awaitingReviewForSha"] = second_sha[:8]
            state.write_text(json.dumps(after_push) + "\n", encoding="utf-8")
            state.chmod(0o600)
            repeated_push = update("--awaiting-review-sha", second_sha)
            self.assertEqual(0, repeated_push.returncode, repeated_push.stderr)
            repeated_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(after_push["generation"] + 1, repeated_state["generation"])
            self.assertEqual(1, repeated_state["fixRoundsDone"])
            exact_repeat = update("--awaiting-review-sha", second_sha)
            self.assertEqual(0, exact_repeat.returncode, exact_repeat.stderr)
            exact_repeat_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(repeated_state["generation"], exact_repeat_state["generation"])
            rebutted = update(
                "--rebuttal-key", "lock-race",
                "--rebuttal-evidence", "CAS 테스트로 stale owner 삭제를 거부함",
                "--increment-rebuttal-only",
            )
            self.assertEqual(0, rebutted.returncode, rebutted.stderr)

            terminal = run_runtime(
                "state-update", "--state", str(state), "--operation", "terminal",
                "--status", "converged", "--writer-id", "watcher-1",
                "--expected-generation", str(json.loads(state.read_text(encoding="utf-8"))["generation"]),
            )
            self.assertEqual(0, terminal.returncode, terminal.stderr)
            state_data = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual([external_review_sha.lower()], state_data["processedReviewShas"])
            self.assertEqual(second_sha, state_data["awaitingReviewForSha"])
            self.assertEqual(1, state_data["fixRoundsDone"])
            self.assertEqual({"lock-race": "CAS 테스트로 stale owner 삭제를 거부함"}, state_data["rebuttals"])
            self.assertEqual(1, state_data["rebuttalOnlyStreak"])
            self.assertTrue(state_data["runnersStopped"])

            after_terminal = update("--processed-review-sha", "deadbeef")
            self.assertNotEqual(0, after_terminal.returncode)

            after_terminal_writer = run_runtime(
                "state-update", "--state", str(state), "--operation", "writer",
                "--expected-generation", str(state_data["generation"]),
                "--expected-writer", "watcher-1", "--caller-writer-id", "watcher-1",
                "--writer-id", "late-writer",
            )
            self.assertNotEqual(0, after_terminal_writer.returncode)

            state_data["cleanupPhase"] = "lock_released"
            state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
            state.chmod(0o600)
            reversed_cleanup = run_runtime(
                "state-update", "--state", str(state), "--operation", "cleanup",
                "--phase", "ready", "--writer-id", "watcher-1",
                "--expected-generation", str(state_data["generation"]),
            )
            self.assertNotEqual(0, reversed_cleanup.returncode)

    def test_cleanup_recovery_without_state_releases_exact_initialization_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "codex-review-loop.test"
            root.mkdir()
            root.chmod(0o700)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            bootstrap = root / ".codex-review-loop.lock-bootstrap"
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            bare = base / "remote.git"
            repository = base / "repo"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "loop-init", "--owner", "tester@init",
                "--recovery-file", str(bootstrap), cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            lock_object = acquired.stdout.strip()
            recovered = run_runtime(
                "cleanup", "--root", str(root), "--state", str(root / "codex-loop-state.json"),
                "--marker", str(marker), "--internal", str(internal), "--external", str(external),
                "--remote", str(bare), "--ref", ref, "--lock-object", lock_object,
                "--loop-id", "loop-init", "--owner", "tester@init", cwd=repository,
            )
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertFalse(root.exists())
            self.assertFalse(external.exists())

    def test_state_less_cleanup_preserves_recovery_after_failed_lease_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            root.mkdir()
            root.chmod(0o700)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            state = root / "codex-loop-state.json"
            bootstrap = root / ".codex-review-loop.lock-bootstrap"
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            candidate = "a" * 40
            replacement = "b" * 40
            remote = "origin"
            ref = "refs/heads/codex-review-locks/mr-560"
            loop_id = "loop-init"
            owner = "tester@init"
            bootstrap.write_bytes(
                RUNTIME._lock_record_payload(remote, ref, loop_id, owner, candidate)
            )
            bootstrap.chmod(0o600)
            arguments = RUNTIME.argparse.Namespace(
                root=str(root),
                state=str(state),
                marker=str(marker),
                internal=str(internal),
                external=str(external),
                remote=remote,
                ref=ref,
                lock_object=candidate,
                loop_id=loop_id,
                owner=owner,
                pre_lock_only=False,
            )
            failed_lease = subprocess.CompletedProcess(["git", "push"], 1, "", "lease failed")
            with mock.patch.object(
                RUNTIME, "_remote_lock_sha", side_effect=[candidate, replacement]
            ), mock.patch.object(RUNTIME, "_validate_lock_payload"), mock.patch.object(
                RUNTIME, "_git", return_value=failed_lease
            ):
                with self.assertRaises(SystemExit) as raised:
                    RUNTIME.cleanup(arguments)
            self.assertEqual(3, raised.exception.code)
            self.assertTrue(root.exists())
            self.assertTrue(marker.exists())
            self.assertTrue(bootstrap.exists())
            self.assertTrue(external.exists())
            self.assertEqual(
                RUNTIME._token_payload(root, remote, ref, candidate, loop_id, owner),
                external.read_bytes(),
            )
            external.write_bytes(
                RUNTIME._lock_released_token_payload(
                    root, remote, ref, candidate, loop_id, "tester@forged",
                )
            )
            external.chmod(0o600)
            with mock.patch.object(RUNTIME, "_remote_lock_sha") as remote_lookup:
                with self.assertRaises(SystemExit) as forged:
                    RUNTIME.cleanup(arguments)
            self.assertEqual(1, forged.exception.code)
            remote_lookup.assert_not_called()

    def test_terminal_cleanup_converges_after_failed_lease_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            state = self.make_state(root)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            candidate = "a" * 40
            replacement = "b" * 40
            remote = "origin"
            ref = "refs/heads/codex-review-locks/mr-560"
            loop_id = "loop-terminal"
            owner = "tester@terminal"
            state_data = json.loads(state.read_text(encoding="utf-8"))
            state_data.update(
                {
                    "status": "converged",
                    "runnersStopped": True,
                    "lockRemote": remote,
                    "lockRef": ref,
                    "lockObject": candidate,
                    "loopId": loop_id,
                    "owner": owner,
                }
            )
            state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
            state.chmod(0o600)
            arguments = RUNTIME.argparse.Namespace(
                root=str(root),
                state=str(state),
                marker=str(marker),
                internal=str(internal),
                external=str(external),
                remote=remote,
                ref=ref,
                lock_object=candidate,
                loop_id=loop_id,
                owner=owner,
                pre_lock_only=False,
            )
            failed_lease = subprocess.CompletedProcess(["git", "push"], 1, "", "lease failed")
            with mock.patch.object(
                RUNTIME, "_remote_lock_sha", side_effect=[candidate, replacement]
            ), mock.patch.object(RUNTIME, "_validate_lock_payload"), mock.patch.object(
                RUNTIME, "_git", return_value=failed_lease
            ):
                RUNTIME.cleanup(arguments)
            self.assertFalse(root.exists())
            self.assertFalse(external.exists())

    def test_cleanup_recovery_accepts_initial_state_left_with_bootstrap_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "codex-review-loop.test"
            root.mkdir()
            root.chmod(0o700)
            marker = root / ".codex-review-loop.marker"
            bootstrap = root / ".codex-review-loop.lock-bootstrap"
            writer_recovery = root / ".codex-review-loop.writer-recovery"
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            state = root / "codex-loop-state.json"
            bare = base / "remote.git"
            repository = base / "repo"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            marker_result = run_runtime(
                "init-marker", "--root", str(root), "--marker", str(marker), cwd=repository,
            )
            self.assertEqual(0, marker_result.returncode, marker_result.stderr)
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "loop-init-state", "--owner", "tester@init-state",
                "--recovery-file", str(bootstrap), cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            lock_object = acquired.stdout.strip()
            bootstrap_payload = bootstrap.read_bytes()
            initialized = run_runtime(
                "init-state", "--root", str(root), "--state", str(state), "--marker", str(marker),
                "--bootstrap", str(bootstrap), "--writer-recovery", str(writer_recovery),
                "--mr", "560", "--max-rounds", "8",
                "--loop-id", "loop-init-state", "--owner", "tester@init-state",
                "--remote", str(bare), "--ref", ref, "--lock-object", lock_object,
                cwd=repository,
            )
            self.assertEqual(0, initialized.returncode, initialized.stderr)
            self.assertFalse(bootstrap.exists())
            bootstrap.write_bytes(bootstrap_payload)
            bootstrap.chmod(0o600)

            external.write_bytes(
                RUNTIME._lock_released_token_payload(
                    root, str(bare), ref, lock_object, "loop-init-state", "tester@init-state",
                )
            )
            external.chmod(0o600)
            rejected_phase = run_runtime(
                "cleanup", "--root", str(root), "--state", str(state), "--marker", str(marker),
                "--internal", str(internal), "--external", str(external), "--remote", str(bare),
                "--ref", ref, "--lock-object", lock_object, "--loop-id", "loop-init-state",
                "--owner", "tester@init-state", cwd=repository,
            )
            self.assertNotEqual(0, rejected_phase.returncode)
            self.assertTrue(state.exists())
            self.assertTrue(bootstrap.exists())
            external.unlink()

            interrupted = run_runtime(
                "cleanup", "--root", str(root), "--state", str(state), "--marker", str(marker),
                "--internal", str(internal), "--external", str(external), "--remote", str(bare),
                "--ref", ref, "--lock-object", lock_object, "--loop-id", "loop-init-state",
                "--owner", "tester@init-state", cwd=repository,
                env={**os.environ, "CODEX_REVIEW_LOOP_FAIL_AFTER_LOCK_RELEASE": "1"},
            )
            self.assertEqual(77, interrupted.returncode, interrupted.stderr)
            self.assertTrue(bootstrap.exists())
            self.assertNotEqual(
                0,
                subprocess.run(
                    ["git", "--git-dir", str(bare), "show-ref", "--verify", "--quiet", ref],
                ).returncode,
            )

            recovered = run_runtime(
                "cleanup", "--root", str(root), "--state", str(state), "--marker", str(marker),
                "--internal", str(internal), "--external", str(external), "--remote", str(bare),
                "--ref", ref, "--lock-object", lock_object, "--loop-id", "loop-init-state",
                "--owner", "tester@init-state", cwd=repository,
            )
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertFalse(root.exists())
            self.assertFalse(external.exists())

    def test_cleanup_failpoint_leaves_external_recovery_token_and_retry_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            state = self.make_state(root)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            payload = root / "mr-disc.json"
            payload.write_text("[]", encoding="utf-8")
            payload.chmod(0o600)
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            state_data = json.loads(state.read_text(encoding="utf-8"))
            state_data.update({"status": "converged", "runnersStopped": True})
            state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
            state.chmod(0o600)

            failed = run_runtime(
                "cleanup",
                "--root",
                str(root),
                "--state",
                str(state),
                "--marker",
                str(marker),
                "--internal",
                str(internal),
                "--external",
                str(external),
                env={**os.environ, "CODEX_REVIEW_LOOP_FAIL_AFTER_UNLINK": "1"},
            )
            self.assertEqual(77, failed.returncode, failed.stderr)
            self.assertTrue(root.exists())
            self.assertTrue(external.exists())

            retried = run_runtime(
                "cleanup",
                "--root",
                str(root),
                "--state",
                str(state),
                "--marker",
                str(marker),
                "--internal",
                str(internal),
                "--external",
                str(external),
            )
            self.assertEqual(0, retried.returncode, retried.stderr)
            self.assertFalse(root.exists())
            self.assertFalse(external.exists())

    def test_cleanup_retry_recovers_after_marker_unlink_cut_point(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            state = self.make_state(root)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            state_data = json.loads(state.read_text(encoding="utf-8"))
            state_data.update({"status": "converged", "runnersStopped": True})
            state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
            state.chmod(0o600)

            failed = run_runtime(
                "cleanup",
                "--root",
                str(root),
                "--state",
                str(state),
                "--marker",
                str(marker),
                "--internal",
                str(internal),
                "--external",
                str(external),
                env={**os.environ, "CODEX_REVIEW_LOOP_FAIL_AFTER_UNLINK": "5"},
            )
            self.assertEqual(77, failed.returncode, failed.stderr)
            self.assertTrue(root.exists())
            self.assertFalse(marker.exists())
            self.assertTrue(external.exists())

            retried = run_runtime(
                "cleanup",
                "--root",
                str(root),
                "--state",
                str(state),
                "--marker",
                str(marker),
                "--internal",
                str(internal),
                "--external",
                str(external),
            )
            self.assertEqual(0, retried.returncode, retried.stderr)
            self.assertFalse(root.exists())
            self.assertFalse(external.exists())

    def test_cleanup_retry_accepts_lock_release_crash_after_cas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "codex-review-loop.test"
            state = self.make_state(root)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            bare = base / "remote.git"
            repository = base / "repo"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "loop-cleanup", "--owner", "tester@cleanup", cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            lock_object = acquired.stdout.strip()
            state_data = json.loads(state.read_text(encoding="utf-8"))
            state_data.update(
                {
                    "status": "converged",
                    "runnersStopped": True,
                    "loopId": "loop-cleanup",
                    "owner": "tester@cleanup",
                    "lockRemote": str(bare),
                    "lockRef": ref,
                    "lockObject": lock_object,
                }
            )
            state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
            state.chmod(0o600)

            failed = run_runtime(
                "cleanup", "--root", str(root), "--state", str(state),
                "--marker", str(marker), "--internal", str(internal), "--external", str(external),
                "--remote", str(bare), "--ref", ref, "--lock-object", lock_object,
                "--loop-id", "loop-cleanup", "--owner", "tester@cleanup",
                cwd=repository,
                env={**os.environ, "CODEX_REVIEW_LOOP_FAIL_AFTER_LOCK_RELEASE": "1"},
            )
            self.assertEqual(77, failed.returncode, failed.stderr)
            self.assertTrue(root.exists())
            self.assertTrue(external.exists())
            self.assertEqual("tombstone", json.loads(state.read_text(encoding="utf-8"))["cleanupPhase"])
            self.assertEqual("", subprocess.run(
                ["git", "--git-dir", str(bare), "ls-remote", str(bare), ref],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.strip())

            replacement = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "loop-new", "--owner", "tester@new", cwd=repository,
            )
            self.assertEqual(0, replacement.returncode, replacement.stderr)
            retried = run_runtime(
                "cleanup", "--root", str(root), "--state", str(state),
                "--marker", str(marker), "--internal", str(internal), "--external", str(external),
                "--remote", str(bare), "--ref", ref, "--lock-object", lock_object,
                "--loop-id", "loop-cleanup", "--owner", "tester@cleanup", cwd=repository,
                env={**os.environ, "CODEX_REVIEW_LOOP_FAIL_BEFORE_EXTERNAL_UNLINK": "1"},
            )
            self.assertEqual(77, retried.returncode, retried.stderr)
            self.assertFalse(root.exists())
            self.assertTrue(external.exists())
            recovered = run_runtime(
                "cleanup", "--root", str(root), "--state", str(state),
                "--marker", str(marker), "--internal", str(internal), "--external", str(external),
                "--remote", str(bare), "--ref", ref, "--lock-object", lock_object,
                "--loop-id", "loop-cleanup", "--owner", "tester@cleanup", cwd=repository,
            )
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertFalse(external.exists())
            self.assertEqual(
                replacement.stdout.strip(),
                subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-parse", ref],
                    check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip(),
            )

    def test_cleanup_retry_after_state_unlink_preserves_replacement_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "codex-review-loop.test"
            state = self.make_state(root)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            bare = base / "remote.git"
            repository = base / "repo"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE)
            ref = "refs/heads/codex-review-locks/mr-560"
            loop_id = "loop-state-unlink"
            owner = "tester@state-unlink"
            acquired = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", loop_id, "--owner", owner, cwd=repository,
            )
            self.assertEqual(0, acquired.returncode, acquired.stderr)
            lock_object = acquired.stdout.strip()
            state_data = json.loads(state.read_text(encoding="utf-8"))
            state_data.update(
                {
                    "status": "converged",
                    "runnersStopped": True,
                    "loopId": loop_id,
                    "owner": owner,
                    "lockRemote": str(bare),
                    "lockRef": ref,
                    "lockObject": lock_object,
                }
            )
            state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
            state.chmod(0o600)
            cleanup_arguments = (
                "cleanup", "--root", str(root), "--state", str(state),
                "--marker", str(marker), "--internal", str(internal), "--external", str(external),
                "--remote", str(bare), "--ref", ref, "--lock-object", lock_object,
                "--loop-id", loop_id, "--owner", owner,
            )

            interrupted = run_runtime(
                *cleanup_arguments,
                cwd=repository,
                env={**os.environ, "CODEX_REVIEW_LOOP_FAIL_AFTER_UNLINK": "1"},
            )
            self.assertEqual(77, interrupted.returncode, interrupted.stderr)
            self.assertFalse(state.exists())
            self.assertTrue(root.exists())
            self.assertEqual(
                RUNTIME._lock_released_token_payload(
                    root, str(bare), ref, lock_object, loop_id, owner,
                ),
                external.read_bytes(),
            )

            replacement = run_runtime(
                "lock", "acquire", "--remote", str(bare), "--ref", ref,
                "--loop-id", "loop-new", "--owner", "tester@new", cwd=repository,
            )
            self.assertEqual(0, replacement.returncode, replacement.stderr)
            recovered = run_runtime(*cleanup_arguments, cwd=repository)
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertFalse(root.exists())
            self.assertFalse(external.exists())
            self.assertEqual(
                replacement.stdout.strip(),
                subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-parse", ref],
                    check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip(),
            )

    def test_recovery_token_survives_after_directory_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "codex-review-loop.test"
            state = self.make_state(root)
            marker = root / ".codex-review-loop.marker"
            marker.write_text(f"codex-review-loop:{root}", encoding="utf-8")
            marker.chmod(0o600)
            internal = root / ".codex-review-loop.cleanup"
            external = root.with_name(root.name + ".cleanup")
            state_data = json.loads(state.read_text(encoding="utf-8"))
            state_data.update({"status": "converged", "runnersStopped": True})
            state.write_text(json.dumps(state_data) + "\n", encoding="utf-8")
            state.chmod(0o600)

            stopped = run_runtime(
                "cleanup",
                "--root",
                str(root),
                "--state",
                str(state),
                "--marker",
                str(marker),
                "--internal",
                str(internal),
                "--external",
                str(external),
                env={**os.environ, "CODEX_REVIEW_LOOP_FAIL_BEFORE_EXTERNAL_UNLINK": "1"},
            )
            self.assertEqual(77, stopped.returncode, stopped.stderr)
            self.assertFalse(root.exists())
            self.assertTrue(external.exists())

            retried = run_runtime(
                "cleanup",
                "--root",
                str(root),
                "--state",
                str(state),
                "--marker",
                str(marker),
                "--internal",
                str(internal),
                "--external",
                str(external),
            )
            self.assertEqual(0, retried.returncode, retried.stderr)
            self.assertFalse(external.exists())

    def test_iso_parser_is_timezone_aware_and_rejects_bad_input(self) -> None:
        parsed = run_runtime("parse-iso", "2026-07-13T12:00:00.000+09:00")
        self.assertEqual(0, parsed.returncode, parsed.stderr)
        self.assertTrue(parsed.stdout.strip().isdigit())
        invalid = run_runtime("parse-iso", "not-a-timestamp")
        self.assertNotEqual(0, invalid.returncode)


if __name__ == "__main__":
    unittest.main()
