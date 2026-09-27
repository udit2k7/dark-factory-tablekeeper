from __future__ import annotations

import os
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import httpx
import pytest


HOST = "127.0.0.1"
REQUEST_COUNT = 50


def stage_root() -> Path:
    return Path(__file__).resolve().parents[1]


def app_root_for_run(tmp_path: Path) -> Path:
    root = stage_root()
    if os.environ.get("AUDIT_MUTANT") != "begin_deferred":
        return root
    mutant = tmp_path / "begin-deferred-mutant"
    shutil.copytree(root / "src", mutant / "src")
    main_path = mutant / "src" / "main.py"
    source = main_path.read_text(encoding="utf-8")
    mutated = source.replace('connection.execute("BEGIN IMMEDIATE")', 'connection.execute("BEGIN DEFERRED")')
    assert mutated != source
    main_path.write_text(mutated, encoding="utf-8")
    return mutant


class ServerCluster:
    def __init__(self, app_root: Path, database_path: Path, log_dir: Path, ports: tuple[int, ...]):
        self.app_root = app_root
        self.database_path = database_path.resolve()
        self.log_dir = log_dir
        self.ports = ports
        self.processes: dict[int, subprocess.Popen] = {}
        self.logs: dict[int, object] = {}
        self.configured_paths: dict[int, str] = {}

    def start_one(self, port: int) -> None:
        env = os.environ.copy()
        env["DATABASE_PATH"] = str(self.database_path)
        env["PYTHONPATH"] = str(self.app_root) + os.pathsep + env.get("PYTHONPATH", "")
        log_handle = (self.log_dir / f"uvicorn-{port}-{time.time_ns()}.log").open(
            "w", encoding="utf-8"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "src.main:app",
                "--host",
                HOST,
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=self.app_root,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        self.processes[port] = process
        self.logs[port] = log_handle
        self.configured_paths[port] = env["DATABASE_PATH"]

    def start(self) -> None:
        try:
            for port in self.ports:
                self.start_one(port)
                self.wait_ready([port])
        except BaseException:
            self.stop()
            raise

    def wait_ready(self, ports: tuple[int, ...] | list[int], timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        pending = set(ports)
        with httpx.Client(timeout=0.5, trust_env=False) as client:
            while pending and time.monotonic() < deadline:
                for port in tuple(pending):
                    process = self.processes[port]
                    if process.poll() is not None:
                        pytest.fail(f"Uvicorn on {port} exited early with {process.returncode}")
                    try:
                        if client.get(f"http://{HOST}:{port}/openapi.json").status_code == 200:
                            pending.remove(port)
                    except httpx.TransportError:
                        pass
                if pending:
                    time.sleep(0.05)
        assert not pending, f"servers failed readiness: {sorted(pending)}"

    def restart(self, port: int) -> tuple[int, int]:
        old = self.processes[port]
        old_pid = old.pid
        terminate_process_tree(old)
        self.logs[port].close()
        wait_ports_closed([port])
        self.start_one(port)
        self.wait_ready([port])
        return old_pid, self.processes[port].pid

    def assert_live_distinct_and_shared(self) -> None:
        pids = [process.pid for process in self.processes.values()]
        assert len(pids) == 4
        assert len(set(pids)) == 4
        assert all(process.poll() is None for process in self.processes.values())
        assert self.database_path.is_absolute()
        assert set(self.configured_paths.values()) == {str(self.database_path)}

    def stop(self) -> None:
        for process in self.processes.values():
            terminate_process_tree(process)
        for handle in self.logs.values():
            if not handle.closed:
                handle.close()
        wait_ports_closed(self.ports)

    def __enter__(self) -> "ServerCluster":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((HOST, port)) == 0


def wait_ports_closed(ports: tuple[int, ...] | list[int], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    open_ports = [port for port in ports if port_is_open(port)]
    while open_ports and time.monotonic() < deadline:
        time.sleep(0.05)
        open_ports = [port for port in ports if port_is_open(port)]
    assert not open_ports, f"listeners still open after teardown: {open_ports}"


def terminate_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)


def allocate_ephemeral_ports(count: int = 4) -> tuple[int, ...]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((HOST, 0))
            sockets.append(sock)
        ports = tuple(sock.getsockname()[1] for sock in sockets)
        assert len(set(ports)) == count
        return ports
    finally:
        for sock in sockets:
            sock.close()


def assert_ports_free(ports: tuple[int, ...]) -> None:
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            assert sock.connect_ex((HOST, port)) != 0, f"listener leaked on {port}"


def create_single_table_setup(port: int) -> tuple[str, dict]:
    with httpx.Client(timeout=10, trust_env=False) as client:
        created = client.post(
            f"http://{HOST}:{port}/restaurants",
            json={
                "name": "Contention Cafe",
                "time_zone": "UTC",
                "opening_hours": [{"weekday": 0, "opens": "09:00", "closes": "22:00"}],
            },
        )
        assert created.status_code == 201, created.text
        restaurant_id = created.json()["id"]
        table = client.post(
            f"http://{HOST}:{port}/restaurants/{restaurant_id}/tables",
            json={"id": "only", "seats": 2},
        )
        assert table.status_code == 201, table.text
    return restaurant_id, {"date": "2030-05-20", "time": "19:15", "party_size": 2}


def run_contenders(
    database_path: Path,
    restaurant_id: str,
    payload: dict,
    ports: tuple[int, ...],
):
    barrier = threading.Barrier(REQUEST_COUNT + 1)
    clients = {
        port: httpx.Client(timeout=20, trust_env=False, limits=httpx.Limits(max_connections=60))
        for port in ports
    }
    lock_holder = sqlite3.connect(database_path, timeout=30, isolation_level=None)
    lock_holder.execute("PRAGMA busy_timeout=30000")
    lock_holder.execute("BEGIN IMMEDIATE")

    def request(index: int):
        port = ports[index % len(ports)]
        barrier.wait(timeout=10)
        time.sleep((index % 7) * 0.002)
        response = clients[port].post(
            f"http://{HOST}:{port}/restaurants/{restaurant_id}/reservations",
            json=payload,
        )
        return port, response.status_code, response.json()

    try:
        with ThreadPoolExecutor(max_workers=REQUEST_COUNT) as pool:
            futures = [pool.submit(request, index) for index in range(REQUEST_COUNT)]
            barrier.wait(timeout=10)
            time.sleep(0.30)
            lock_holder.rollback()
            done, not_done = wait(futures, timeout=35)
            assert not not_done, "contention workload exceeded overall timeout"
            return [future.result() for future in done]
    finally:
        if lock_holder.in_transaction:
            lock_holder.rollback()
        lock_holder.close()
        for client in clients.values():
            client.close()


def assert_database_outcome(database_path: Path, restaurant_id: str, winner: dict) -> None:
    connection = sqlite3.connect(database_path)
    try:
        reservation = connection.execute(
            "SELECT id, restaurant_id, table_id, party_size, slot_start_utc FROM reservations"
        ).fetchall()
        claims = connection.execute(
            "SELECT restaurant_id, table_id, slot_start_utc, reservation_id FROM reservation_slot_claims"
        ).fetchall()
        overlaps = connection.execute(
            """
            SELECT restaurant_id, table_id, slot_start_utc
            FROM reservation_slot_claims
            GROUP BY restaurant_id, table_id, slot_start_utc
            HAVING COUNT(*) > 1
            """
        ).fetchall()
    finally:
        connection.close()
    assert reservation == [
        (
            winner["id"],
            restaurant_id,
            winner["table_id"],
            winner["party_size"],
            winner["slot_start_utc"],
        )
    ]
    assert claims == [
        (restaurant_id, winner["table_id"], winner["slot_start_utc"], winner["id"])
    ]
    assert overlaps == []


def test_transaction_source_requires_immediate_before_selection() -> None:
    source = (stage_root() / "src" / "main.py").read_text(encoding="utf-8")
    reservation = source.split("def create_reservation", 1)[1]
    begin = reservation.index('connection.execute("BEGIN IMMEDIATE")')
    validation = reservation.index("restaurant_and_slot")
    selection = reservation.index("SELECT t.table_id")
    assert "BEGIN DEFERRED" not in reservation
    assert begin < validation < selection


@pytest.mark.parametrize("iteration", range(3))
def test_four_process_exact_contention(tmp_path: Path, iteration: int) -> None:
    ports = allocate_ephemeral_ports()
    assert_ports_free(ports)
    database_path = (tmp_path / f"shared-{iteration}.db").resolve()
    app_root = app_root_for_run(tmp_path)
    cluster = ServerCluster(app_root, database_path, tmp_path, ports)
    with cluster:
        cluster.assert_live_distinct_and_shared()
        restaurant_id, payload = create_single_table_setup(ports[0])
        if iteration == 0:
            old_pid, new_pid = cluster.restart(ports[0])
            assert new_pid != old_pid
            cluster.assert_live_distinct_and_shared()

        results = run_contenders(database_path, restaurant_id, payload, ports)
        assert len(results) == REQUEST_COUNT
        assert {port for port, _, _ in results} == set(ports)
        counts = Counter(status for _, status, _ in results)
        assert counts == Counter({409: 49, 201: 1})
        assert all(body == {"detail": "no_table_available"} for _, status, body in results if status == 409)
        winner = next(body for _, status, body in results if status == 201)
        assert_database_outcome(database_path, restaurant_id, winner)

        with httpx.Client(timeout=10, trust_env=False) as client:
            for port in ports:
                observed = client.get(
                    f"http://{HOST}:{port}/restaurants/{restaurant_id}/availability",
                    params={"date": payload["date"], "time": payload["time"], "party_size": 2},
                )
                assert observed.status_code == 200
                assert observed.json()["available_tables"] == []
        cluster.assert_live_distinct_and_shared()
