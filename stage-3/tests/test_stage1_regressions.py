from __future__ import annotations

import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src import database
from src.database import BUSY_TIMEOUT_MS, connect, initialize_database
from src.main import ReservationCreate, app, create_reservation


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "tablekeeper.db"))
    if os.environ.get("AUDIT_MUTANT") == "remove_slot_claim_pk":
        monkeypatch.setattr(
            database,
            "SCHEMA",
            database.SCHEMA.replace(
                "    PRIMARY KEY (restaurant_id, table_id, slot_start_utc),\n",
                "",
            ),
        )
    with TestClient(app) as test_client:
        yield test_client


def create_restaurant(
    client: TestClient,
    *,
    name: str = "Cafe Example",
    time_zone: str = "Asia/Kolkata",
    opening_hours: list[dict] | None = None,
) -> str:
    response = client.post(
        "/restaurants",
        json={
            "name": name,
            "time_zone": time_zone,
            "opening_hours": opening_hours
            if opening_hours is not None
            else [{"weekday": 0, "opens": "09:00", "closes": "22:00"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def add_table(client: TestClient, restaurant_id: str, table_id: str, seats: int):
    return client.post(
        f"/restaurants/{restaurant_id}/tables",
        json={"id": table_id, "seats": seats},
    )


def insert_restaurant_and_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO restaurants VALUES ('r1', 'R', 'UTC', '2030-01-01T00:00:00Z')"
    )
    connection.execute(
        "INSERT INTO restaurants VALUES ('r2', 'R2', 'UTC', '2030-01-01T00:00:00Z')"
    )
    connection.execute("INSERT INTO restaurant_tables VALUES ('r1', 't1', 2)")
    connection.execute("INSERT INTO restaurant_tables VALUES ('r2', 't1', 4)")


@pytest.mark.parametrize(
    "patch",
    [
        {"name": ""},
        {"name": " padded "},
        {"time_zone": "Mars/Olympus_Mons"},
        {"opening_hours": [{"weekday": 0, "opens": "09:01", "closes": "22:00"}]},
        {"opening_hours": [{"weekday": 0, "opens": "22:00", "closes": "09:00"}]},
        {"opening_hours": [{"weekday": 0, "opens": "09:00", "closes": "09:00"}]},
        {
            "opening_hours": [
                {"weekday": 0, "opens": "09:00", "closes": "10:00"},
                {"weekday": 0, "opens": "11:00", "closes": "12:00"},
            ]
        },
        {"unexpected": True},
    ],
)
def test_restaurant_rejects_invalid_payloads(client: TestClient, patch: dict) -> None:
    payload = {
        "name": "Cafe Example",
        "time_zone": "Asia/Kolkata",
        "opening_hours": [{"weekday": 0, "opens": "09:00", "closes": "22:00"}],
    }
    payload.update(patch)
    response = client.post("/restaurants", json=payload)
    assert response.status_code == 422


def test_tables_enforce_boundaries_conflicts_and_restaurant_scope(client: TestClient) -> None:
    first = create_restaurant(client)
    second = create_restaurant(client, name="Other")
    assert add_table(client, first, "T1", 4).status_code == 201
    assert add_table(client, second, "T1", 4).status_code == 201
    assert add_table(client, first, "T1", 4).status_code == 409
    assert add_table(client, "missing", "T1", 4).status_code == 404
    for payload in (
        {"id": "", "seats": 4},
        {"id": " padded ", "seats": 4},
        {"id": "x" * 65, "seats": 4},
        {"id": "T2", "seats": 0},
        {"id": "T2", "seats": -1},
        {"id": "T2", "seats": 2.5},
        {"id": "T2", "seats": 2, "unknown": 1},
    ):
        assert client.post(f"/restaurants/{first}/tables", json=payload).status_code == 422


def test_table_unexpected_integrity_failure_returns_sanitized_500(client: TestClient) -> None:
    restaurant_id = create_restaurant(client)
    with connect() as connection:
        connection.execute(
            "CREATE TRIGGER force_table_failure BEFORE INSERT ON restaurant_tables "
            "BEGIN SELECT RAISE(ABORT, 'forced_table_failure'); END"
        )
    response = add_table(client, restaurant_id, "T1", 4)
    assert response.status_code == 500
    assert response.json() == {"detail": "internal_error"}
    assert "forced_table_failure" not in response.text
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM restaurant_tables").fetchone()[0] == 0


def test_availability_orders_filters_and_honours_open_boundaries(client: TestClient) -> None:
    restaurant_id = create_restaurant(client)
    for table_id, seats in (("B", 4), ("A", 4), ("C", 2), ("D", 6)):
        assert add_table(client, restaurant_id, table_id, seats).status_code == 201

    opening = client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "09:00", "party_size": 3},
    )
    assert opening.status_code == 200
    assert opening.json()["available_tables"] == [
        {"id": "A", "seats": 4},
        {"id": "B", "seats": 4},
        {"id": "D", "seats": 6},
    ]
    assert client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "21:45", "party_size": 1},
    ).status_code == 200
    assert client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "22:00", "party_size": 1},
    ).status_code == 422


@pytest.mark.parametrize(
    ("date", "time"),
    [
        ("2030-05-21", "10:00"),
        ("2030-05-20", "08:45"),
        ("2030-05-20", "21:50"),
        ("2030-02-30", "10:00"),
        ("2030-05-20", "10:01"),
        ("2030-05-20", "bad"),
    ],
)
def test_availability_rejects_closed_malformed_or_unaligned_slots(
    client: TestClient, date: str, time: str
) -> None:
    restaurant_id = create_restaurant(client)
    response = client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": date, "time": time, "party_size": 1},
    )
    assert response.status_code == 422


def test_dst_gap_and_fold_are_rejected(client: TestClient) -> None:
    restaurant_id = create_restaurant(
        client,
        time_zone="America/New_York",
        opening_hours=[{"weekday": 6, "opens": "00:00", "closes": "04:00"}],
    )
    for date, time in (("2030-03-10", "02:00"), ("2030-11-03", "01:30")):
        availability = client.get(
            f"/restaurants/{restaurant_id}/availability",
            params={"date": date, "time": time, "party_size": 1},
        )
        reservation = client.post(
            f"/restaurants/{restaurant_id}/reservations",
            json={"date": date, "time": time, "party_size": 1},
        )
        assert availability.status_code == 422
        assert reservation.status_code == 422


def test_reservation_uses_deterministic_table_and_only_claims_one_slot(client: TestClient) -> None:
    restaurant_id = create_restaurant(client)
    for table_id, seats in (("B", 4), ("A", 4), ("small", 2), ("large", 8)):
        assert add_table(client, restaurant_id, table_id, seats).status_code == 201

    response = client.post(
        f"/restaurants/{restaurant_id}/reservations",
        json={"date": "2030-05-20", "time": "19:15", "party_size": 3},
    )
    assert response.status_code == 201
    assert response.json()["table_id"] == "A"
    same_slot = client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "19:15", "party_size": 3},
    )
    adjacent = client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "19:30", "party_size": 3},
    )
    assert {table["id"] for table in same_slot.json()["available_tables"]} == {"B", "large"}
    assert {table["id"] for table in adjacent.json()["available_tables"]} == {"A", "B", "large"}


def test_concurrent_last_table_claim_has_exactly_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "race.db"))
    initialize_database()
    with TestClient(app) as setup_client:
        restaurant_id = create_restaurant(setup_client)
        assert add_table(setup_client, restaurant_id, "only", 2).status_code == 201

    barrier = threading.Barrier(8)

    def attempt() -> int:
        barrier.wait()
        try:
            create_reservation(
                restaurant_id,
                ReservationCreate(date="2030-05-20", time="19:15", party_size=2),
            )
            return 201
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: attempt(), range(8)))
    assert statuses.count(201) == 1
    assert set(statuses) <= {201, 409, 503}
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM reservation_slot_claims").fetchone()[0] == 1


def test_database_pragmas_are_enabled(client: TestClient) -> None:
    with connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS


def test_database_rejects_capacity_insert_and_update(client: TestClient) -> None:
    with connect() as connection:
        insert_restaurant_and_tables(connection)
        if os.environ.get("AUDIT_MUTANT") == "drop_capacity_insert":
            connection.execute("DROP TRIGGER reservations_capacity_insert")
        with pytest.raises(sqlite3.IntegrityError, match="capacity_exceeded"):
            connection.execute(
                "INSERT INTO reservations (id, restaurant_id, table_id, party_size, slot_start_utc, local_date, local_time, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("bad", "r1", "t1", 3, "2030-01-01T10:00:00Z", "2030-01-01", "10:00", "2030-01-01T00:00:00Z"),
            )
        connection.execute(
            "INSERT INTO reservations (id, restaurant_id, table_id, party_size, slot_start_utc, local_date, local_time, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("ok", "r1", "t1", 2, "2030-01-01T10:00:00Z", "2030-01-01", "10:00", "2030-01-01T00:00:00Z"),
        )
        if os.environ.get("AUDIT_MUTANT") == "drop_capacity_update":
            connection.execute("DROP TRIGGER reservations_capacity_update")
        with pytest.raises(sqlite3.IntegrityError, match="capacity_exceeded"):
            connection.execute("UPDATE reservations SET party_size = 3 WHERE id = 'ok'")


def test_database_rejects_duplicate_mismatched_and_orphan_claims(client: TestClient) -> None:
    with connect() as connection:
        insert_restaurant_and_tables(connection)
        for reservation_id, restaurant_id, table_id, slot in (
            ("a", "r1", "t1", "2030-01-01T10:00:00Z"),
            ("b", "r1", "t1", "2030-01-01T10:00:00Z"),
        ):
            connection.execute(
                "INSERT INTO reservations (id, restaurant_id, table_id, party_size, slot_start_utc, local_date, local_time, created_at_utc) VALUES (?, ?, ?, 2, ?, '2030-01-01', '10:00', '2030-01-01T00:00:00Z')",
                (reservation_id, restaurant_id, table_id, slot),
            )
        connection.execute(
            "INSERT INTO reservation_slot_claims VALUES ('r1', 't1', '2030-01-01T10:00:00Z', 'a')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO reservation_slot_claims VALUES ('r1', 't1', '2030-01-01T10:00:00Z', 'b')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="slot_claim_mismatch"):
            connection.execute(
                "INSERT INTO reservation_slot_claims VALUES ('r2', 't1', '2030-01-01T10:00:00Z', 'b')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO reservation_slot_claims VALUES ('r1', 't1', '2030-01-01T10:15:00Z', 'missing')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO restaurant_tables VALUES ('missing', 'x', 2)")


def test_claim_failure_rolls_back_reservation(client: TestClient) -> None:
    restaurant_id = create_restaurant(client)
    assert add_table(client, restaurant_id, "T1", 4).status_code == 201
    with connect() as connection:
        connection.execute(
            "CREATE TRIGGER force_claim_failure BEFORE INSERT ON reservation_slot_claims "
            "BEGIN SELECT RAISE(ABORT, 'forced_claim_failure'); END"
        )
    response = client.post(
        f"/restaurants/{restaurant_id}/reservations",
        json={"date": "2030-05-20", "time": "19:15", "party_size": 2},
    )
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM reservation_slot_claims").fetchone()[0] == 0
    assert response.status_code == 500


def test_write_lock_exhaustion_returns_sanitized_503(client: TestClient) -> None:
    lock_holder = connect()
    lock_holder.execute("BEGIN IMMEDIATE")
    try:
        response = client.post(
            "/restaurants",
            json={
                "name": "Locked",
                "time_zone": "UTC",
                "opening_hours": [],
            },
        )
    finally:
        lock_holder.rollback()
        lock_holder.close()
    assert response.status_code == 503
    assert response.json() == {"detail": "database_busy"}
    assert "sqlite" not in response.text.lower()
    assert "traceback" not in response.text.lower()


def test_capacity_protection_mutation_probe(client: TestClient) -> None:
    """Prove the capacity attack succeeds when the insert trigger is removed."""
    with connect() as connection:
        insert_restaurant_and_tables(connection)
        connection.execute("DROP TRIGGER reservations_capacity_insert")
        connection.execute(
            "INSERT INTO reservations (id, restaurant_id, table_id, party_size, slot_start_utc, local_date, local_time, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("mutant", "r1", "t1", 99, "2030-01-01T10:00:00Z", "2030-01-01", "10:00", "2030-01-01T00:00:00Z"),
        )
        assert connection.execute(
            "SELECT party_size FROM reservations WHERE id = 'mutant'"
        ).fetchone()[0] == 99
