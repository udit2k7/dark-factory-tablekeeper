from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


TESTS_ROOT = Path(__file__).resolve().parent
STAGE_ROOT = TESTS_ROOT.parent
HARNESS_PATH = TESTS_ROOT / "stress_harness.py"
SPEC = importlib.util.spec_from_file_location("stage4_stress_harness", HARNESS_PATH)
assert SPEC is not None and SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def assert_reaped(cluster) -> None:
    assert cluster.processes
    assert all(process.poll() is not None for process in cluster.processes)
    assert all(handle.closed for handle in cluster.handles)
    assert all(harness.port_closed(port) for port in cluster.ports)


def test_startup_failure_reaps_every_child_and_port(tmp_path: Path) -> None:
    cluster = harness.Cluster(
        tmp_path / "failure.db",
        tmp_path,
        app_target="tests.missing_application:app",
        startup_timeout=2,
    )
    with pytest.raises(RuntimeError, match="exited during startup"):
        cluster.__enter__()
    assert_reaped(cluster)


def test_startup_timeout_reaps_every_child_and_port(tmp_path: Path) -> None:
    cluster = harness.Cluster(
        tmp_path / "timeout.db",
        tmp_path,
        app_target="tests.cleanup_apps:hanging_app",
        startup_timeout=0.75,
    )
    with pytest.raises(RuntimeError, match="startup timeout"):
        cluster.__enter__()
    assert_reaped(cluster)


def test_keyboard_interrupt_reaps_every_child_and_port(tmp_path: Path) -> None:
    cluster = harness.Cluster(tmp_path / "interrupt.db", tmp_path)
    with pytest.raises(KeyboardInterrupt):
        with cluster:
            raise KeyboardInterrupt
    assert_reaped(cluster)


def run_harness(seed: int):
    completed = subprocess.run(
        [sys.executable, str(HARNESS_PATH), "--seed", str(seed)],
        cwd=STAGE_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["seed"] == seed
    assert summary["attempts"] == 240
    assert summary["attempts"] == summary["successes"] + summary["conflicts"] + summary["errors"]
    assert summary["errors"] == 0
    assert all(value == 0 for value in summary["database_audit"].values())
    return summary


@pytest.mark.parametrize("seed", [7, 99])
def test_multiple_seeded_schedules_are_clean(seed: int) -> None:
    run_harness(seed)


def mutant_root(tmp_path: Path) -> Path:
    root = tmp_path / "stage-4-mutant"
    shutil.copytree(STAGE_ROOT / "src", root / "src")
    (root / "tests").mkdir()
    shutil.copy2(TESTS_ROOT / "conftest.py", root / "tests" / "conftest.py")
    shutil.copy2(TESTS_ROOT / "test_stage3.py", root / "tests" / "test_stage3.py")
    return root


def run_mutant(root: Path, test_name: str):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    return subprocess.run(
        [sys.executable, "-m", "pytest", f"tests/test_stage3.py::{test_name}", "-q"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1
    path.write_text(source.replace(old, new), encoding="utf-8")


def test_keyed_response_transaction_mutant_is_killed(tmp_path: Path) -> None:
    root = mutant_root(tmp_path)
    main = root / "src" / "main.py"
    replace_once(
        main,
        "            if idempotency_key is not None:\n                connection.execute(\n",
        "            if idempotency_key is not None:\n                connection.commit()\n                connection.execute(\n",
    )
    completed = run_mutant(root, "test_idempotency_insert_failure_rolls_back_all_three_rows")
    assert completed.returncode == 1
    assert "2 failed" in completed.stdout


def test_cancellation_claim_deletion_mutant_is_killed(tmp_path: Path) -> None:
    root = mutant_root(tmp_path)
    main = root / "src" / "main.py"
    replace_once(
        main,
        '        connection.execute(\n            "DELETE FROM reservation_slot_claims WHERE reservation_id = ?",\n            (reservation_id,),\n        )\n',
        '        connection.execute("SELECT 1")\n',
    )
    completed = run_mutant(root, "test_cancel_releases_claim_replays_exactly_and_creation_replay_does_not_reclaim")
    assert completed.returncode == 1
    assert "1 failed" in completed.stdout
