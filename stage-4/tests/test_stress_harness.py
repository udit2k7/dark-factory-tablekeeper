from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


HARNESS = Path(__file__).with_name("stress_harness.py")


def test_mutation_mode_detects_removed_slot_primary_key() -> None:
    completed = subprocess.run(
        [sys.executable, str(HARNESS), "--mutation-disable-slot-pk"],
        cwd=HARNESS.parents[1],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr + completed.stdout
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    summary = json.loads(lines[-1])
    assert summary["attempts"] == 240
    assert summary["attempts"] == summary["successes"] + summary["conflicts"] + summary["errors"]
    assert summary["errors"] == 0
    assert summary["process_count"] == 4
    assert summary["database_audit"]["double_bookings"] >= 1
    assert summary["database_audit"]["over_capacity"] == 0
    assert summary["database_audit"]["duplicate_idempotent_bookings"] == 0
    assert summary["database_audit"]["leftover_slots_from_cancelled_bookings"] == 0


def test_harness_has_portable_real_process_topology() -> None:
    source = HARNESS.read_text(encoding="utf-8")
    assert 'HOST = "127.0.0.1"' in source
    assert '"-m", "uvicorn"' in source
    assert "shell=False" in source
    assert "bind((HOST, 0))" in source
    assert "TestClient" not in source
    assert "taskkill" not in source
    assert "os.killpg" not in source
    assert "stdout=subprocess.PIPE" not in source
