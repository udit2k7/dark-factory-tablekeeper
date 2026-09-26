from __future__ import annotations

import os
import sqlite3
from pathlib import Path


BUSY_TIMEOUT_MS = 5_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (name = trim(name) AND length(name) BETWEEN 1 AND 200),
    time_zone TEXT NOT NULL CHECK (length(time_zone) > 0),
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restaurant_opening_hours (
    restaurant_id TEXT NOT NULL,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    opens_local TEXT NOT NULL CHECK (
        length(opens_local) = 5
        AND opens_local GLOB '[0-2][0-9]:[0-5][0-9]'
        AND CAST(substr(opens_local, 1, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(opens_local, 4, 2) IN ('00', '15', '30', '45')
    ),
    closes_local TEXT NOT NULL CHECK (
        length(closes_local) = 5
        AND closes_local GLOB '[0-2][0-9]:[0-5][0-9]'
        AND CAST(substr(closes_local, 1, 2) AS INTEGER) BETWEEN 0 AND 23
        AND substr(closes_local, 4, 2) IN ('00', '15', '30', '45')
    ),
    PRIMARY KEY (restaurant_id, weekday),
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE CASCADE,
    CHECK (opens_local < closes_local)
);

CREATE TABLE IF NOT EXISTS restaurant_tables (
    restaurant_id TEXT NOT NULL,
    table_id TEXT NOT NULL CHECK (
        table_id = trim(table_id) AND length(table_id) BETWEEN 1 AND 64
    ),
    seats INTEGER NOT NULL CHECK (typeof(seats) = 'integer' AND seats > 0),
    PRIMARY KEY (restaurant_id, table_id),
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    restaurant_id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    party_size INTEGER NOT NULL CHECK (typeof(party_size) = 'integer' AND party_size > 0),
    slot_start_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    local_time TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (restaurant_id, table_id)
        REFERENCES restaurant_tables(restaurant_id, table_id)
);

CREATE TABLE IF NOT EXISTS reservation_slot_claims (
    restaurant_id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    slot_start_utc TEXT NOT NULL,
    reservation_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY (restaurant_id, table_id, slot_start_utc),
    FOREIGN KEY (restaurant_id, table_id)
        REFERENCES restaurant_tables(restaurant_id, table_id),
    FOREIGN KEY (reservation_id) REFERENCES reservations(id) ON DELETE CASCADE
);

CREATE TRIGGER IF NOT EXISTS reservations_capacity_insert
BEFORE INSERT ON reservations
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM restaurant_tables AS t
    WHERE t.restaurant_id = NEW.restaurant_id
      AND t.table_id = NEW.table_id
      AND t.seats >= NEW.party_size
)
BEGIN
    SELECT RAISE(ABORT, 'capacity_exceeded');
END;

CREATE TRIGGER IF NOT EXISTS reservations_capacity_update
BEFORE UPDATE OF restaurant_id, table_id, party_size ON reservations
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM restaurant_tables AS t
    WHERE t.restaurant_id = NEW.restaurant_id
      AND t.table_id = NEW.table_id
      AND t.seats >= NEW.party_size
)
BEGIN
    SELECT RAISE(ABORT, 'capacity_exceeded');
END;

CREATE TRIGGER IF NOT EXISTS slot_claim_matches_reservation_insert
BEFORE INSERT ON reservation_slot_claims
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM reservations AS r
    WHERE r.id = NEW.reservation_id
      AND r.restaurant_id = NEW.restaurant_id
      AND r.table_id = NEW.table_id
      AND r.slot_start_utc = NEW.slot_start_utc
)
BEGIN
    SELECT RAISE(ABORT, 'slot_claim_mismatch');
END;

CREATE TRIGGER IF NOT EXISTS slot_claim_matches_reservation_update
BEFORE UPDATE OF restaurant_id, table_id, slot_start_utc, reservation_id
ON reservation_slot_claims
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM reservations AS r
    WHERE r.id = NEW.reservation_id
      AND r.restaurant_id = NEW.restaurant_id
      AND r.table_id = NEW.table_id
      AND r.slot_start_utc = NEW.slot_start_utc
)
BEGIN
    SELECT RAISE(ABORT, 'slot_claim_mismatch');
END;
"""


def database_path() -> Path:
    return Path(os.environ.get("DATABASE_PATH", "tablekeeper.db"))


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1_000,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return connection


def initialize_database() -> None:
    with connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(SCHEMA)
