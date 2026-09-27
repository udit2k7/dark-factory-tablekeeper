from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import database
from src.database import BUSY_TIMEOUT_MS, connect
from src.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_PATH", str((tmp_path / "stage2.db").resolve()))
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


def restaurant(
    client: TestClient,
    *,
    zone: str = "Asia/Kolkata",
    hours: list[dict] | None = None,
) -> str:
    response = client.post(
        "/restaurants",
        json={
            "name": "Stage 2 Cafe",
            "time_zone": zone,
            "opening_hours": hours
            if hours is not None
            else [{"weekday": 0, "opens": "09:00", "closes": "22:00"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def table(client: TestClient, restaurant_id: str, table_id: str, seats: int):
    return client.post(
        f"/restaurants/{restaurant_id}/tables",
        json={"id": table_id, "seats": seats},
    )


def test_pragmas_and_shared_path_are_stage2_strengthened(client: TestClient) -> None:
    with connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000
    assert BUSY_TIMEOUT_MS >= 30_000


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": " padded "},
        {"time_zone": "Not/AZone"},
        {"opening_hours": [{"weekday": 0, "opens": "09:01", "closes": "10:00"}]},
        {"opening_hours": [{"weekday": 0, "opens": "10:00", "closes": "09:00"}]},
        {
            "opening_hours": [
                {"weekday": 0, "opens": "09:00", "closes": "10:00"},
                {"weekday": 0, "opens": "10:00", "closes": "11:00"},
            ]
        },
        {"unknown": "rejected"},
    ],
)
def test_restaurant_validation_and_unknown_fields(client: TestClient, payload: dict) -> None:
    body = {
        "name": "Cafe",
        "time_zone": "UTC",
        "opening_hours": [{"weekday": 0, "opens": "09:00", "closes": "22:00"}],
    }
    body.update(payload)
    assert client.post("/restaurants", json=body).status_code == 422


def test_iana_alias_is_accepted(client: TestClient) -> None:
    response = client.post(
        "/restaurants",
        json={"name": "Alias", "time_zone": "US/Eastern", "opening_hours": []},
    )
    assert response.status_code == 201
    assert response.json()["time_zone"] == "US/Eastern"


def test_tables_validate_scope_conflict_and_sanitize_unexpected_integrity(client: TestClient) -> None:
    restaurant_id = restaurant(client)
    assert table(client, restaurant_id, "T1", 4).status_code == 201
    assert table(client, restaurant_id, "T1", 4).status_code == 409
    assert table(client, "missing", "T1", 4).status_code == 404
    assert table(client, restaurant_id, "bad", 0).status_code == 422
    with connect() as connection:
        connection.execute(
            "CREATE TRIGGER force_table_failure BEFORE INSERT ON restaurant_tables "
            "BEGIN SELECT RAISE(ABORT, 'secret_table_failure'); END"
        )
    failed = table(client, restaurant_id, "T2", 4)
    assert failed.status_code == 500
    assert failed.json() == {"detail": "internal_error"}
    assert "secret" not in failed.text


def test_deterministic_assignment_boundaries_and_adjacent_slot(client: TestClient) -> None:
    restaurant_id = restaurant(client)
    for table_id, seats in (("B", 4), ("A", 4), ("small", 2), ("large", 8)):
        assert table(client, restaurant_id, table_id, seats).status_code == 201
    at_open = client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "09:00", "party_size": 3},
    )
    assert at_open.json()["available_tables"] == [
        {"id": "A", "seats": 4},
        {"id": "B", "seats": 4},
        {"id": "large", "seats": 8},
    ]
    created = client.post(
        f"/restaurants/{restaurant_id}/reservations",
        json={"date": "2030-05-20", "time": "19:15", "party_size": 3},
    )
    assert created.status_code == 201
    assert created.json()["table_id"] == "A"
    same = client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "19:15", "party_size": 3},
    )
    adjacent = client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "19:30", "party_size": 3},
    )
    assert {item["id"] for item in same.json()["available_tables"]} == {"B", "large"}
    assert {item["id"] for item in adjacent.json()["available_tables"]} == {"A", "B", "large"}
    assert client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "21:45", "party_size": 1},
    ).status_code == 200
    assert client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "22:00", "party_size": 1},
    ).status_code == 422


def test_dst_gap_fold_unaligned_and_closed_day_are_rejected(client: TestClient) -> None:
    restaurant_id = restaurant(
        client,
        zone="America/New_York",
        hours=[{"weekday": 6, "opens": "00:00", "closes": "04:00"}],
    )
    for day, local_time in (
        ("2030-03-10", "02:00"),
        ("2030-11-03", "01:30"),
        ("2030-03-10", "00:01"),
        ("2030-03-11", "01:00"),
    ):
        response = client.post(
            f"/restaurants/{restaurant_id}/reservations",
            json={"date": day, "time": local_time, "party_size": 1},
        )
        assert response.status_code == 422


def test_database_capacity_foreign_keys_claim_match_and_slot_pk(client: TestClient) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO restaurants VALUES ('r1', 'R1', 'UTC', '2030-01-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO restaurants VALUES ('r2', 'R2', 'UTC', '2030-01-01T00:00:00Z')"
        )
        connection.execute("INSERT INTO restaurant_tables VALUES ('r1', 't1', 2)")
        connection.execute("INSERT INTO restaurant_tables VALUES ('r2', 't1', 4)")
        with pytest.raises(sqlite3.IntegrityError, match="capacity_exceeded"):
            connection.execute(
                "INSERT INTO reservations (id, restaurant_id, table_id, party_size, slot_start_utc, local_date, local_time, created_at_utc) VALUES ('bad', 'r1', 't1', 3, '2030-01-01T10:00:00Z', '2030-01-01', '10:00', '2030-01-01T00:00:00Z')"
            )
        for reservation_id in ("a", "b"):
            connection.execute(
                "INSERT INTO reservations (id, restaurant_id, table_id, party_size, slot_start_utc, local_date, local_time, created_at_utc) VALUES (?, 'r1', 't1', 2, '2030-01-01T10:00:00Z', '2030-01-01', '10:00', '2030-01-01T00:00:00Z')",
                (reservation_id,),
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
                "INSERT INTO reservation_slot_claims VALUES ('r2', 't1', '2030-01-01T10:15:00Z', 'b')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO restaurant_tables VALUES ('missing', 'x', 2)")


def test_nonunique_claim_failure_rolls_back_and_returns_sanitized_500(client: TestClient) -> None:
    restaurant_id = restaurant(client)
    assert table(client, restaurant_id, "T1", 4).status_code == 201
    with connect() as connection:
        connection.execute(
            "CREATE TRIGGER force_claim_failure BEFORE INSERT ON reservation_slot_claims "
            "BEGIN SELECT RAISE(ABORT, 'secret_claim_failure'); END"
        )
    response = client.post(
        f"/restaurants/{restaurant_id}/reservations",
        json={"date": "2030-05-20", "time": "19:15", "party_size": 2},
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "internal_error"}
    assert "secret" not in response.text
    with connect() as connection:
        assert connection.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM reservation_slot_claims").fetchone()[0] == 0
