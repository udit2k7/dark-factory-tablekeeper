from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx


HOST = "127.0.0.1"
STAGE_ROOT = Path(__file__).resolve().parents[1]
PORT_COUNT = 4
TARGETS = ("10:00", "11:00", "12:00", "13:00", "14:00", "15:00")


@contextlib.contextmanager
def temporary_workspace():
    root = Path(tempfile.mkdtemp(prefix="tablekeeper-stage4-"))
    try:
        yield root
    finally:
        deadline = time.monotonic() + 10
        while root.exists():
            try:
                shutil.rmtree(root)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)


def free_ports() -> tuple[int, ...]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(PORT_COUNT):
            sock = socket.socket()
            sock.bind((HOST, 0))
            sockets.append(sock)
        return tuple(sock.getsockname()[1] for sock in sockets)
    finally:
        for sock in sockets:
            sock.close()


def port_closed(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.1)
        return sock.connect_ex((HOST, port)) != 0


class Cluster:
    def __init__(
        self,
        database: Path,
        logs: Path,
        *,
        app_target: str = "src.main:app",
        startup_timeout: float = 25,
    ):
        self.database = database.resolve()
        self.logs = logs
        self.app_target = app_target
        self.startup_timeout = startup_timeout
        self.ports = free_ports()
        self.processes: list[subprocess.Popen] = []
        self.handles = []

    def __enter__(self):
        try:
            env = os.environ.copy()
            env["DATABASE_PATH"] = str(self.database)
            env["PYTHONPATH"] = str(STAGE_ROOT)
            for port in self.ports:
                handle = (self.logs / f"uvicorn-{port}.log").open("w", encoding="utf-8")
                self.handles.append(handle)
                process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", self.app_target, "--host", HOST, "--port", str(port)],
                    cwd=STAGE_ROOT,
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    shell=False,
                )
                self.processes.append(process)
            deadline = time.monotonic() + self.startup_timeout
            pending = set(self.ports)
            with httpx.Client(timeout=1, trust_env=False) as client:
                while pending and time.monotonic() < deadline:
                    for port in tuple(pending):
                        if self.processes[self.ports.index(port)].poll() is not None:
                            raise RuntimeError(f"server on {port} exited during startup")
                        try:
                            if client.get(f"http://{HOST}:{port}/openapi.json").status_code == 200:
                                pending.remove(port)
                        except httpx.HTTPError:
                            pass
                    if pending:
                        time.sleep(0.05)
            if pending:
                raise RuntimeError(f"startup timeout: {sorted(pending)}")
            if len({process.pid for process in self.processes}) != PORT_COUNT:
                raise RuntimeError("servers did not have four distinct PIDs")
            return self
        except BaseException:
            try:
                self._cleanup()
            except Exception as cleanup_error:
                print(f"startup cleanup also failed: {cleanup_error}", file=sys.stderr)
            raise

    def _cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for handle in self.handles:
            handle.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not all(port_closed(port) for port in self.ports):
            time.sleep(0.05)
        if not all(port_closed(port) for port in self.ports):
            raise RuntimeError("listener leaked after teardown")

    def __exit__(self, *_):
        self._cleanup()


def post(client: httpx.Client, port: int, path: str, *, body: dict | None = None, key: str | None = None):
    headers = {"Idempotency-Key": key} if key else None
    return client.post(f"http://{HOST}:{port}{path}", json=body, headers=headers)


def seed(cluster: Cluster):
    port = cluster.ports[0]
    with httpx.Client(timeout=20, trust_env=False) as client:
        response = post(
            client,
            port,
            "/restaurants",
            body={
                "name": "Stage 4 Stress",
                "time_zone": "UTC",
                "opening_hours": [{"weekday": 0, "opens": "09:00", "closes": "18:00"}],
            },
        )
        response.raise_for_status()
        restaurant_id = response.json()["id"]
        for index in range(8):
            response = post(
                client,
                port,
                f"/restaurants/{restaurant_id}/tables",
                body={"id": f"T{index + 1}", "seats": 2 + (index % 3) * 2},
            )
            response.raise_for_status()

        replay_seeds = []
        for index in range(5):
            payload = {"date": "2030-05-20", "time": TARGETS[index], "party_size": 2}
            key = f"seed-replay-{index}"
            response = post(client, port, f"/restaurants/{restaurant_id}/reservations", body=payload, key=key)
            response.raise_for_status()
            replay_seeds.append((payload, key, response.content))

        cancel_seeds = []
        for index in range(10):
            payload = {
                "date": "2030-05-20",
                "time": TARGETS[index % len(TARGETS)],
                "party_size": 4 if index >= 8 else 2,
            }
            response = post(
                client,
                port,
                f"/restaurants/{restaurant_id}/reservations",
                body=payload,
                key=f"seed-cancel-{index}",
            )
            response.raise_for_status()
            cancel_seeds.append((response.json()["id"], payload))
    return restaurant_id, replay_seeds, cancel_seeds


def build_operations(restaurant_id: str, replay_seeds, cancel_seeds):
    operations = []
    for index in range(60):
        operations.append((
            "booking",
            "POST",
            f"/restaurants/{restaurant_id}/reservations",
            {"date": "2030-05-20", "time": TARGETS[index % 6], "party_size": (2, 4, 6)[index % 3]},
            f"fresh-{index}",
            None,
        ))
    for index in range(50):
        payload, key, expected = replay_seeds[index % len(replay_seeds)]
        operations.append(("retry", "POST", f"/restaurants/{restaurant_id}/reservations", payload, key, expected))
    for index in range(40):
        reservation_id, _ = cancel_seeds[index % len(cancel_seeds)]
        operations.append(("cancel", "POST", f"/reservations/{reservation_id}/cancel", None, None, reservation_id))
    for index in range(40):
        _, payload = cancel_seeds[index % len(cancel_seeds)]
        operations.append(("rebook", "POST", f"/restaurants/{restaurant_id}/reservations", payload, f"rebook-{index}", None))
    for index in range(50):
        operations.append((
            "availability",
            "GET",
            f"/restaurants/{restaurant_id}/availability?date=2030-05-20&time={TARGETS[index % 6]}&party_size={(2, 4, 6)[index % 3]}",
            None,
            None,
            None,
        ))
    return operations


def run_workload(cluster: Cluster, operations, seed: int):
    release = threading.Event()
    clients = {
        port: httpx.Client(timeout=30, trust_env=False, limits=httpx.Limits(max_connections=80))
        for port in cluster.ports
    }
    cancellation_bodies: dict[str, list[bytes]] = defaultdict(list)

    def run(index: int, operation):
        kind, method, path, body, key, expected = operation
        port = cluster.ports[index % PORT_COUNT]
        release.wait(timeout=10)
        time.sleep(random.Random((seed << 16) + index).random() * 0.01)
        try:
            if method == "GET":
                response = clients[port].get(f"http://{HOST}:{port}{path}")
            else:
                response = post(clients[port], port, path, body=body, key=key)
            parsed = response.json()
            if kind == "retry" and (response.status_code != 201 or response.content != expected):
                return "error", kind, port, "keyed replay mismatch"
            if kind == "cancel" and response.status_code == 200:
                cancellation_bodies[expected].append(response.content)
            if response.status_code in (200, 201):
                return "success", kind, port, parsed
            if response.status_code == 409 and parsed == {"detail": "no_table_available"}:
                return "conflict", kind, port, parsed
            return "error", kind, port, f"unexpected {response.status_code}: {response.text}"
        except Exception as exc:
            return "error", kind, port, f"{type(exc).__name__}: {exc}"

    results = []
    try:
        with ThreadPoolExecutor(max_workers=len(operations)) as executor:
            futures = [executor.submit(run, index, operation) for index, operation in enumerate(operations)]
            release.set()
            deadline = time.monotonic() + 75
            for future in as_completed(futures, timeout=max(1, deadline - time.monotonic())):
                results.append(future.result())
    finally:
        for client in clients.values():
            client.close()
    if len(results) != len(operations):
        raise RuntimeError("not every measured operation completed")
    for reservation_id, bodies in cancellation_bodies.items():
        if len(bodies) != 4 or len(set(bodies)) != 1:
            results.append(("error", "cancel", -1, f"cancellation replay mismatch for {reservation_id}"))
    if {port for _, _, port, _ in results if port != -1} != set(cluster.ports):
        results.append(("error", "topology", -1, "not every port received traffic"))
    if any(process.poll() is not None for process in cluster.processes):
        results.append(("error", "topology", -1, "server exited during workload"))
    return results


def disable_slot_pk_and_probe(database: Path, restaurant_id: str):
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        triggers = [row[0] for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='reservation_slot_claims' ORDER BY name"
        )]
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            ALTER TABLE reservation_slot_claims RENAME TO reservation_slot_claims_old;
            CREATE TABLE reservation_slot_claims (
                restaurant_id TEXT NOT NULL,
                table_id TEXT NOT NULL,
                slot_start_utc TEXT NOT NULL CHECK (
                    length(slot_start_utc) = 20
                    AND slot_start_utc GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                ),
                reservation_id TEXT NOT NULL UNIQUE,
                FOREIGN KEY (restaurant_id, table_id) REFERENCES restaurant_tables(restaurant_id, table_id),
                FOREIGN KEY (reservation_id) REFERENCES reservations(id) ON DELETE CASCADE
            );
            INSERT INTO reservation_slot_claims SELECT * FROM reservation_slot_claims_old;
            DROP TABLE reservation_slot_claims_old;
            COMMIT;
            """
        )
        for sql in triggers:
            connection.execute(sql)
        table_id = connection.execute(
            "SELECT table_id FROM restaurant_tables WHERE restaurant_id=? ORDER BY table_id LIMIT 1", (restaurant_id,)
        ).fetchone()[0]
        slot = "2030-05-20T17:00:00Z"
        created = "2030-01-01T00:00:00Z"
        for suffix in ("a", "b"):
            reservation_id = f"mutation-{suffix}-{uuid.uuid4()}"
            connection.execute(
                "INSERT INTO reservations VALUES (?, ?, ?, 2, ?, '2030-05-20', '17:00', ?, 'confirmed', NULL)",
                (reservation_id, restaurant_id, table_id, slot, created),
            )
            connection.execute(
                "INSERT INTO reservation_slot_claims VALUES (?, ?, ?, ?)",
                (restaurant_id, table_id, slot, reservation_id),
            )
        connection.commit()
    finally:
        connection.close()


def audit(database: Path):
    connection = sqlite3.connect(database)
    try:
        return {
            "double_bookings": connection.execute(
                "SELECT count(*) FROM (SELECT 1 FROM reservation_slot_claims GROUP BY restaurant_id, table_id, slot_start_utc HAVING count(*) > 1)"
            ).fetchone()[0],
            "over_capacity": connection.execute(
                "SELECT count(*) FROM reservations r JOIN restaurant_tables t USING (restaurant_id, table_id) WHERE r.party_size > t.seats"
            ).fetchone()[0],
            "duplicate_idempotent_bookings": connection.execute(
                "SELECT count(*) FROM (SELECT idempotency_key FROM idempotency_records GROUP BY idempotency_key HAVING count(DISTINCT reservation_id) > 1)"
            ).fetchone()[0],
            "leftover_slots_from_cancelled_bookings": connection.execute(
                "SELECT count(*) FROM reservation_slot_claims c JOIN reservations r ON r.id=c.reservation_id WHERE r.status='cancelled'"
            ).fetchone()[0],
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutation-disable-slot-pk", action="store_true")
    parser.add_argument("--seed", type=int, default=404)
    args = parser.parse_args()
    summary = {"attempts": 0, "successes": 0, "conflicts": 0, "errors": 1, "database_audit": {}}
    exit_code = 1
    try:
        with temporary_workspace() as root:
            database = root / "shared.db"
            with Cluster(database, root) as cluster:
                restaurant_id, replay_seeds, cancel_seeds = seed(cluster)
                operations = build_operations(restaurant_id, replay_seeds, cancel_seeds)
                random.Random(args.seed).shuffle(operations)
                results = run_workload(cluster, operations, args.seed)
                counts = Counter(result[0] for result in results)
                summary.update(
                    attempts=len(operations),
                    successes=counts["success"],
                    conflicts=counts["conflict"],
                    errors=counts["error"],
                    process_count=len(cluster.processes),
                    operation_counts=dict(Counter(result[1] for result in results if result[1] != "topology")),
                    seed=args.seed,
                )
            if args.mutation_disable_slot_pk:
                disable_slot_pk_and_probe(database, restaurant_id)
            summary["database_audit"] = audit(database)
            normal_audits = all(value == 0 for value in summary["database_audit"].values())
            accounting = summary["attempts"] == summary["successes"] + summary["conflicts"] + summary["errors"]
            if args.mutation_disable_slot_pk:
                exit_code = 2 if summary["database_audit"]["double_bookings"] >= 1 else 1
            elif summary["attempts"] >= 200 and accounting and summary["errors"] == 0 and normal_audits:
                exit_code = 0
    except Exception as exc:
        summary["failure"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
