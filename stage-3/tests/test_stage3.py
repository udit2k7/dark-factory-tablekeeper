from __future__ import annotations

import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.database import connect
from src.main import app


UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_PATH", str((tmp_path / "stage3.db").resolve()))
    with TestClient(app) as test_client:
        yield test_client


def create_restaurant(
    client: TestClient,
    *,
    name: str = "Stage 3 Cafe",
    zone: str = "UTC",
    hours: list[dict] | None = None,
) -> str:
    response = client.post(
        "/restaurants",
        json={
            "name": name,
            "time_zone": zone,
            "opening_hours": hours
            if hours is not None
            else [{"weekday": 0, "opens": "09:00", "closes": "22:00"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def add_table(client: TestClient, restaurant_id: str, table_id: str = "T1", seats: int = 4):
    response = client.post(
        f"/restaurants/{restaurant_id}/tables",
        json={"id": table_id, "seats": seats},
    )
    assert response.status_code == 201, response.text


def reserve(
    client: TestClient,
    restaurant_id: str,
    *,
    key: str | bytes | None = None,
    date: str = "2030-05-20",
    time: str = "19:15",
    party_size: int = 2,
):
    headers = None if key is None else [(b"idempotency-key", key if isinstance(key, bytes) else key.encode("latin-1"))]
    return client.post(
        f"/restaurants/{restaurant_id}/reservations",
        json={"date": date, "time": time, "party_size": party_size},
        headers=headers,
    )


def counts() -> tuple[int, int, int]:
    with connect() as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("reservations", "reservation_slot_claims", "idempotency_records")
        )


def test_same_key_replays_byte_exact_canonical_body(client: TestClient) -> None:
    restaurant_id = create_restaurant(client)
    add_table(client, restaurant_id)
    url = f"/restaurants/{restaurant_id}/reservations"
    first = client.post(
        url,
        content=b'{ "time" : "19:15", "party_size" : 2, "date" : "2030-05-20" }',
        headers={"content-type": "application/json", "Idempotency-Key": "Replay-Key"},
    )
    second = client.post(
        url,
        content=b'{"date":"2030-05-20","time":"19:15","party_size":2}',
        headers={"content-type": "application/json", "Idempotency-Key": "Replay-Key"},
    )
    assert first.status_code == second.status_code == 201
    assert first.content == second.content
    expected = json.dumps(first.json(), sort_keys=True, separators=(",", ":")).encode()
    assert first.content == expected
    assert counts() == (1, 1, 1)
    with connect() as connection:
        stored = connection.execute(
            "SELECT canonical_request, response_status, response_body FROM idempotency_records"
        ).fetchone()
    assert json.loads(stored["canonical_request"]) == {
        "date": "2030-05-20",
        "party_size": 2,
        "restaurant_id": restaurant_id,
        "time": "19:15",
    }
    assert stored["response_status"] == 201
    assert stored["response_body"].encode() == first.content


@pytest.mark.parametrize(
    "key",
    [b"", b" leading", b"trailing ", b"bad\x00key", b"bad\x1fkey", b"bad\x7fkey", b"caf\xe9", b"x" * 256],
)
def test_invalid_supplied_idempotency_keys_are_rejected(client: TestClient, key: bytes) -> None:
    restaurant_id = create_restaurant(client)
    add_table(client, restaurant_id)
    response = reserve(client, restaurant_id, key=key)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_idempotency_key"}
    assert counts() == (0, 0, 0)


def test_omitted_key_is_valid_and_key_case_is_distinct(client: TestClient) -> None:
    restaurant_id = create_restaurant(client)
    for table_id in ("A", "B", "C"):
        add_table(client, restaurant_id, table_id)
    assert reserve(client, restaurant_id).status_code == 201
    assert reserve(client, restaurant_id, key="Case-Key").status_code == 201
    assert reserve(client, restaurant_id, key="case-key").status_code == 201
    assert counts() == (3, 3, 2)


@pytest.mark.parametrize(
    "change",
    [
        {"party_size": 3},
        {"date": "2030-05-27"},
        {"time": "19:30"},
    ],
)
def test_same_key_different_semantics_conflicts_without_state_change(
    client: TestClient, change: dict
) -> None:
    restaurant_id = create_restaurant(client)
    add_table(client, restaurant_id)
    assert reserve(client, restaurant_id, key="Conflict-Key").status_code == 201
    response = reserve(client, restaurant_id, key="Conflict-Key", **change)
    assert response.status_code == 422
    assert response.json() == {"detail": "idempotency_key_reused"}
    assert counts() == (1, 1, 1)


def test_same_key_different_restaurant_path_conflicts(client: TestClient) -> None:
    first_id = create_restaurant(client, name="First")
    second_id = create_restaurant(client, name="Second")
    add_table(client, first_id)
    add_table(client, second_id)
    assert reserve(client, first_id, key="Global-Key").status_code == 201
    response = reserve(client, second_id, key="Global-Key")
    assert response.status_code == 422
    assert response.json() == {"detail": "idempotency_key_reused"}
    assert counts() == (1, 1, 1)


def test_unsuccessful_request_does_not_consume_key(client: TestClient) -> None:
    restaurant_id = create_restaurant(client)
    unavailable = reserve(client, restaurant_id, key="Retry-Key")
    assert unavailable.status_code == 409
    assert counts() == (0, 0, 0)
    add_table(client, restaurant_id)
    succeeded = reserve(client, restaurant_id, key="Retry-Key")
    assert succeeded.status_code == 201
    assert counts() == (1, 1, 1)


@pytest.mark.parametrize("timing", ["BEFORE", "AFTER"])
def test_idempotency_insert_failure_rolls_back_all_three_rows(
    client: TestClient, timing: str
) -> None:
    restaurant_id = create_restaurant(client)
    add_table(client, restaurant_id)
    with connect() as connection:
        connection.execute(
            f"CREATE TRIGGER fail_idempotency {timing} INSERT ON idempotency_records "
            "BEGIN SELECT RAISE(ABORT, 'secret_idempotency_failure'); END"
        )
    response = reserve(client, restaurant_id, key=f"Atomic-{timing}")
    assert response.status_code == 500
    assert response.json() == {"detail": "internal_error"}
    assert "secret" not in response.text
    assert counts() == (0, 0, 0)


def test_cancel_releases_claim_replays_exactly_and_creation_replay_does_not_reclaim(
    client: TestClient,
) -> None:
    restaurant_id = create_restaurant(client)
    add_table(client, restaurant_id)
    created = reserve(client, restaurant_id, key="Cancel-Replay")
    original = created.content
    reservation_id = created.json()["id"]
    first = client.post(f"/reservations/{reservation_id}/cancel")
    second = client.post(f"/reservations/{reservation_id}/cancel")
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert first.json()["status"] == "cancelled"
    assert UTC_PATTERN.fullmatch(first.json()["cancelled_at_utc"])
    assert counts() == (1, 0, 1)
    available = client.get(
        f"/restaurants/{restaurant_id}/availability",
        params={"date": "2030-05-20", "time": "19:15", "party_size": 2},
    )
    assert available.json()["available_tables"] == [{"id": "T1", "seats": 4}]
    replay = reserve(client, restaurant_id, key="Cancel-Replay")
    assert replay.status_code == 201
    assert replay.content == original
    assert counts() == (1, 0, 1)


def test_unknown_cancel_and_cancellation_failure_are_atomic(client: TestClient) -> None:
    assert client.post("/reservations/missing/cancel").status_code == 404
    restaurant_id = create_restaurant(client)
    add_table(client, restaurant_id)
    created = reserve(client, restaurant_id)
    reservation_id = created.json()["id"]
    with connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_cancel BEFORE UPDATE OF status ON reservations "
            "BEGIN SELECT RAISE(ABORT, 'secret_cancel_failure'); END"
        )
    response = client.post(f"/reservations/{reservation_id}/cancel")
    assert response.status_code == 500
    assert response.json() == {"detail": "internal_error"}
    assert "secret" not in response.text
    with connect() as connection:
        row = connection.execute(
            "SELECT status, cancelled_at_utc FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        claims = connection.execute(
            "SELECT count(*) FROM reservation_slot_claims WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()[0]
    assert tuple(row) == ("confirmed", None)
    assert claims == 1


def test_database_blocks_cancelled_claims_revival_and_bad_utc_shapes(client: TestClient) -> None:
    restaurant_id = create_restaurant(client)
    add_table(client, restaurant_id)
    created = reserve(client, restaurant_id)
    body = created.json()
    reservation_id = body["id"]
    with connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="cancelled_reservation_has_claims"):
            connection.execute(
                "UPDATE reservations SET status='cancelled', cancelled_at_utc='2030-05-20T12:00:00Z' WHERE id=?",
                (reservation_id,),
            )
    assert client.post(f"/reservations/{reservation_id}/cancel").status_code == 200
    with connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="slot_claim_mismatch"):
            connection.execute(
                "INSERT INTO reservation_slot_claims VALUES (?, ?, ?, ?)",
                (restaurant_id, body["table_id"], body["slot_start_utc"], reservation_id),
            )
        if os.environ.get("AUDIT_MUTANT") == "remove_cancel_terminal":
            connection.execute("DROP TRIGGER reservation_cancel_is_terminal")
        with pytest.raises(sqlite3.IntegrityError, match="cancelled_reservation_is_terminal"):
            connection.execute(
                "UPDATE reservations SET status='confirmed', cancelled_at_utc=NULL WHERE id=?",
                (reservation_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE reservations SET cancelled_at_utc='2030-05-20 12:00:00' WHERE id=?",
                (reservation_id,),
            )


def test_local_date_differs_from_utc_and_slot_end_crosses_utc_midnight(client: TestClient) -> None:
    restaurant_id = create_restaurant(
        client,
        zone="Asia/Kolkata",
        hours=[{"weekday": 0, "opens": "05:00", "closes": "06:00"}],
    )
    add_table(client, restaurant_id)
    created = reserve(
        client,
        restaurant_id,
        key="Midnight-UTC",
        date="2030-05-20",
        time="05:15",
    )
    assert created.status_code == 201
    assert created.json()["slot_start_utc"] == "2030-05-19T23:45:00Z"
    with connect() as connection:
        reservation = connection.execute(
            "SELECT slot_start_utc, created_at_utc FROM reservations"
        ).fetchone()
        claim = connection.execute("SELECT slot_start_utc FROM reservation_slot_claims").fetchone()
        record = connection.execute("SELECT created_at_utc FROM idempotency_records").fetchone()
    for timestamp in (*reservation, *claim, *record):
        assert UTC_PATTERN.fullmatch(timestamp)


def test_offline_five_sequential_and_five_concurrent_replays_make_one_booking(
    client: TestClient,
) -> None:
    restaurant_id = create_restaurant(client)
    add_table(client, restaurant_id)
    sequential = [reserve(client, restaurant_id, key="Exact-5x5") for _ in range(5)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        concurrent = list(
            pool.map(lambda _: reserve(client, restaurant_id, key="Exact-5x5"), range(5))
        )
    responses = sequential + concurrent
    assert [response.status_code for response in responses] == [201] * 10
    assert len({response.content for response in responses}) == 1
    assert counts() == (1, 1, 1)
    conflict = reserve(client, restaurant_id, key="Exact-5x5", party_size=3)
    assert conflict.status_code == 422
    assert conflict.json() == {"detail": "idempotency_key_reused"}
    assert counts() == (1, 1, 1)


def test_offline_cancel_twice_then_rebook_same_slot(client: TestClient) -> None:
    restaurant_id = create_restaurant(client)
    add_table(client, restaurant_id)
    first = reserve(client, restaurant_id, key="First-Booking")
    reservation_id = first.json()["id"]
    cancelled_once = client.post(f"/reservations/{reservation_id}/cancel")
    cancelled_twice = client.post(f"/reservations/{reservation_id}/cancel")
    assert cancelled_once.status_code == cancelled_twice.status_code == 200
    assert cancelled_once.content == cancelled_twice.content
    second = reserve(client, restaurant_id, key="Second-Booking")
    assert second.status_code == 201
    assert second.json()["id"] != reservation_id
    assert counts() == (2, 1, 2)
    with connect() as connection:
        states = connection.execute(
            "SELECT status, count(*) FROM reservations GROUP BY status ORDER BY status"
        ).fetchall()
    assert [tuple(row) for row in states] == [("cancelled", 1), ("confirmed", 1)]


def test_offline_utc_midnight_dst_gap_and_outside_hours(client: TestClient) -> None:
    midnight_id = create_restaurant(
        client,
        name="Midnight",
        zone="Asia/Kolkata",
        hours=[{"weekday": 0, "opens": "05:00", "closes": "06:00"}],
    )
    add_table(client, midnight_id)
    midnight = reserve(
        client,
        midnight_id,
        key="UTC-Midnight",
        date="2030-05-20",
        time="05:15",
    )
    assert midnight.status_code == 201
    assert midnight.json()["slot_start_utc"] == "2030-05-19T23:45:00Z"

    dst_id = create_restaurant(
        client,
        name="DST",
        zone="America/New_York",
        hours=[{"weekday": 6, "opens": "00:00", "closes": "04:00"}],
    )
    add_table(client, dst_id)
    assert reserve(
        client,
        dst_id,
        key="DST-Gap",
        date="2030-03-10",
        time="02:00",
    ).status_code == 422

    outside_id = create_restaurant(client, name="Outside")
    add_table(client, outside_id)
    assert reserve(
        client,
        outside_id,
        key="Outside-Hours",
        time="22:00",
    ).status_code == 422


def test_stage3_dst_fall_back_ambiguous_local_time_returns_422(client: TestClient) -> None:
    restaurant_id = create_restaurant(
        client,
        name="DST Fold",
        zone="America/New_York",
        hours=[{"weekday": 6, "opens": "00:00", "closes": "04:00"}],
    )
    add_table(client, restaurant_id)

    response = reserve(
        client,
        restaurant_id,
        key="DST-Fold",
        date="2030-11-03",
        time="01:30",
    )

    assert response.status_code == 422
    assert counts() == (0, 0, 0)
