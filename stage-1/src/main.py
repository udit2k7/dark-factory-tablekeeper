from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time as Time, timedelta, timezone
from typing import Annotated
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .database import connect, initialize_database


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpeningHours(StrictModel):
    weekday: StrictInt
    opens: str
    closes: str

    @field_validator("weekday")
    @classmethod
    def valid_weekday(cls, value: int) -> int:
        if not 0 <= value <= 6:
            raise ValueError("weekday must be between 0 and 6")
        return value

    @field_validator("opens", "closes")
    @classmethod
    def valid_quarter_hour(cls, value: str) -> str:
        parsed = parse_time(value)
        if parsed.minute % 15:
            raise ValueError("time must align to a 15-minute boundary")
        return value

    @model_validator(mode="after")
    def closes_after_open(self) -> "OpeningHours":
        if parse_time(self.closes) <= parse_time(self.opens):
            raise ValueError("closing time must be later than opening time")
        return self


class RestaurantCreate(StrictModel):
    name: str
    time_zone: str
    opening_hours: list[OpeningHours]

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if value != value.strip() or not 1 <= len(value) <= 200:
            raise ValueError("name must be trimmed and contain 1 to 200 characters")
        return value

    @field_validator("time_zone")
    @classmethod
    def valid_time_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("invalid IANA time zone") from None
        return value

    @model_validator(mode="after")
    def weekdays_are_unique(self) -> "RestaurantCreate":
        weekdays = [hours.weekday for hours in self.opening_hours]
        if len(weekdays) != len(set(weekdays)):
            raise ValueError("opening hours contain a duplicate weekday")
        return self


class TableCreate(StrictModel):
    id: str
    seats: StrictInt

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if value != value.strip() or not 1 <= len(value) <= 64:
            raise ValueError("id must be trimmed and contain 1 to 64 characters")
        return value

    @field_validator("seats")
    @classmethod
    def valid_seats(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("seats must be positive")
        return value


class ReservationCreate(StrictModel):
    date: str
    time: str
    party_size: StrictInt

    @field_validator("party_size")
    @classmethod
    def valid_party_size(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("party_size must be positive")
        return value

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        parse_date(value)
        return value

    @field_validator("time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        parsed = parse_time(value)
        if parsed.minute % 15:
            raise ValueError("time must align to a 15-minute boundary")
        return value


def parse_date(value: str) -> Date:
    try:
        parsed = Date.fromisoformat(value)
    except ValueError:
        raise ValueError("date must use YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD")
    return parsed


def parse_time(value: str) -> Time:
    try:
        parsed = Time.fromisoformat(value)
    except ValueError:
        raise ValueError("time must use HH:MM") from None
    if len(value) != 5 or parsed.second or parsed.microsecond or parsed.isoformat(timespec="minutes") != value:
        raise ValueError("time must use HH:MM")
    return parsed


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_slot_to_utc(local_date: str, local_time: str, zone_name: str) -> str:
    day = parse_date(local_date)
    wall_time = parse_time(local_time)
    if wall_time.minute % 15:
        raise ValueError("time must align to a 15-minute boundary")

    naive = datetime.combine(day, wall_time)
    zone = ZoneInfo(zone_name)
    candidates: list[datetime] = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        round_trip = aware.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive and round_trip.fold == fold:
            candidates.append(aware)

    offsets = {candidate.utcoffset() for candidate in candidates}
    if not candidates:
        raise ValueError("local time does not exist in the restaurant time zone")
    if len(offsets) > 1:
        raise ValueError("local time is ambiguous in the restaurant time zone")

    return candidates[0].astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def minutes(value: str) -> int:
    parsed = parse_time(value)
    return parsed.hour * 60 + parsed.minute


def restaurant_and_slot(connection: sqlite3.Connection, restaurant_id: str, local_date: str, local_time: str) -> tuple[sqlite3.Row, str]:
    restaurant = connection.execute(
        "SELECT id, name, time_zone FROM restaurants WHERE id = ?",
        (restaurant_id,),
    ).fetchone()
    if restaurant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="restaurant_not_found")

    try:
        day = parse_date(local_date)
        parsed_time = parse_time(local_time)
        if parsed_time.minute % 15:
            raise ValueError("time must align to a 15-minute boundary")
        slot_start_utc = local_slot_to_utc(local_date, local_time, restaurant["time_zone"])
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from None

    opening = connection.execute(
        """
        SELECT opens_local, closes_local
        FROM restaurant_opening_hours
        WHERE restaurant_id = ? AND weekday = ?
        """,
        (restaurant_id, day.weekday()),
    ).fetchone()
    start_minute = parsed_time.hour * 60 + parsed_time.minute
    if (
        opening is None
        or start_minute < minutes(opening["opens_local"])
        or start_minute + 15 > minutes(opening["closes_local"])
    ):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="slot_outside_opening_hours")

    return restaurant, slot_start_utc


def rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def storage_error(error: sqlite3.Error) -> HTTPException:
    message = str(error).lower()
    if isinstance(error, sqlite3.OperationalError) and ("locked" in message or "busy" in message):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_busy")
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="internal_error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="TableKeeper Stage 1", lifespan=lifespan)


@app.post("/restaurants", status_code=status.HTTP_201_CREATED)
def create_restaurant(payload: RestaurantCreate) -> dict:
    restaurant_id = str(uuid4())
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO restaurants (id, name, time_zone, created_at_utc) VALUES (?, ?, ?, ?)",
            (restaurant_id, payload.name, payload.time_zone, utc_now()),
        )
        connection.executemany(
            """
            INSERT INTO restaurant_opening_hours
                (restaurant_id, weekday, opens_local, closes_local)
            VALUES (?, ?, ?, ?)
            """,
            [(restaurant_id, item.weekday, item.opens, item.closes) for item in payload.opening_hours],
        )
        connection.commit()
    except sqlite3.Error as error:
        rollback(connection)
        raise storage_error(error) from None
    finally:
        connection.close()
    return {
        "id": restaurant_id,
        "name": payload.name,
        "time_zone": payload.time_zone,
        "opening_hours": [item.model_dump() for item in payload.opening_hours],
    }


@app.post("/restaurants/{restaurant_id}/tables", status_code=status.HTTP_201_CREATED)
def create_table(restaurant_id: str, payload: TableCreate) -> dict:
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute("SELECT 1 FROM restaurants WHERE id = ?", (restaurant_id,)).fetchone()
        if exists is None:
            rollback(connection)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="restaurant_not_found")
        try:
            connection.execute(
                "INSERT INTO restaurant_tables (restaurant_id, table_id, seats) VALUES (?, ?, ?)",
                (restaurant_id, payload.id, payload.seats),
            )
        except sqlite3.IntegrityError:
            rollback(connection)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="table_already_exists") from None
        connection.commit()
    except HTTPException:
        raise
    except sqlite3.Error as error:
        rollback(connection)
        raise storage_error(error) from None
    finally:
        connection.close()
    return {"id": payload.id, "seats": payload.seats}


@app.get("/restaurants/{restaurant_id}/availability")
def availability(
    restaurant_id: str,
    date: Annotated[str, Query()],
    time: Annotated[str, Query()],
    party_size: Annotated[int, Query(gt=0)],
) -> dict:
    connection = connect()
    try:
        _, slot_start_utc = restaurant_and_slot(connection, restaurant_id, date, time)
        rows = connection.execute(
            """
            SELECT t.table_id, t.seats
            FROM restaurant_tables AS t
            WHERE t.restaurant_id = ?
              AND t.seats >= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM reservation_slot_claims AS c
                  WHERE c.restaurant_id = t.restaurant_id
                    AND c.table_id = t.table_id
                    AND c.slot_start_utc = ?
              )
            ORDER BY t.seats, t.table_id
            """,
            (restaurant_id, party_size, slot_start_utc),
        ).fetchall()
    except HTTPException:
        raise
    except sqlite3.Error as error:
        raise storage_error(error) from None
    finally:
        connection.close()
    return {
        "date": date,
        "time": time,
        "party_size": party_size,
        "available_tables": [{"id": row["table_id"], "seats": row["seats"]} for row in rows],
    }


@app.post("/restaurants/{restaurant_id}/reservations", status_code=status.HTTP_201_CREATED)
def create_reservation(restaurant_id: str, payload: ReservationCreate) -> dict:
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _, slot_start_utc = restaurant_and_slot(
            connection,
            restaurant_id,
            payload.date,
            payload.time,
        )
        table = connection.execute(
            """
            SELECT t.table_id, t.seats
            FROM restaurant_tables AS t
            WHERE t.restaurant_id = ?
              AND t.seats >= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM reservation_slot_claims AS c
                  WHERE c.restaurant_id = t.restaurant_id
                    AND c.table_id = t.table_id
                    AND c.slot_start_utc = ?
              )
            ORDER BY t.seats, t.table_id
            LIMIT 1
            """,
            (restaurant_id, payload.party_size, slot_start_utc),
        ).fetchone()
        if table is None:
            rollback(connection)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no_table_available")

        reservation_id = str(uuid4())
        created_at = utc_now()
        try:
            connection.execute(
                """
                INSERT INTO reservations (
                    id, restaurant_id, table_id, party_size, slot_start_utc,
                    local_date, local_time, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reservation_id,
                    restaurant_id,
                    table["table_id"],
                    payload.party_size,
                    slot_start_utc,
                    payload.date,
                    payload.time,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO reservation_slot_claims (
                    restaurant_id, table_id, slot_start_utc, reservation_id
                ) VALUES (?, ?, ?, ?)
                """,
                (restaurant_id, table["table_id"], slot_start_utc, reservation_id),
            )
        except sqlite3.IntegrityError:
            rollback(connection)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no_table_available") from None
        connection.commit()
    except HTTPException:
        rollback(connection)
        raise
    except sqlite3.Error as error:
        rollback(connection)
        raise storage_error(error) from None
    finally:
        connection.close()

    return {
        "id": reservation_id,
        "restaurant_id": restaurant_id,
        "table_id": table["table_id"],
        "party_size": payload.party_size,
        "date": payload.date,
        "time": payload.time,
        "slot_start_utc": slot_start_utc,
    }
