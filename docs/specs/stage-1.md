# Stage 1 — Restaurant Reservation Service

## Scope and assumptions

Python 3.11, FastAPI, and SQLite in WAL mode. All application artifacts belong under `stage-1/`; this specification is the only Stage 1 artifact outside that directory. A reservation starts on a 15-minute boundary and occupies exactly one 15-minute slot. Dates and times supplied by clients are restaurant-local wall times; persisted slot instants are UTC. Opening hours contain at most one interval per weekday and cannot cross midnight. Stage 1 verification uses the repository `.venv`; a `stage-1/Dockerfile` is required as a delivery artifact but Docker build/run verification is deferred to the final stage.

## Numbered invariants

1. **I1 — Exclusive slot claim.** At most one reservation may claim a `(restaurant_id, table_id, slot_start_utc)` tuple. SQLite enforces this with the composite primary key of `reservation_slot_claims`; application checks are not the authority.
2. **I2 — Capacity.** Every reservation has `party_size > 0`, and its party size is less than or equal to the assigned table's `seats`. SQLite `BEFORE INSERT` and relevant `BEFORE UPDATE` triggers on `reservations` enforce the cross-table capacity rule.
3. **I3 — Referential integrity.** Every table belongs to an existing restaurant; every reservation and slot claim refer to the same existing restaurant/table pair; every claim refers to an existing reservation. `PRAGMA foreign_keys=ON` is required on every connection.
4. **I4 — Time validity.** Reservation starts are valid, unambiguous local times aligned to `:00`, `:15`, `:30`, or `:45`, and the full 15-minute interval is within that weekday's opening interval. Closed-day, nonexistent DST, and ambiguous DST wall times are rejected.
5. **I5 — Deterministic assignment.** When multiple tables are free, reservation creation chooses the table with the fewest seats that fits the party, then the lexicographically smallest table ID.
6. **I6 — Durable atomic creation.** A successful response means the reservation row and its slot claim committed together. Neither may exist without the other.

## Data schema

SQLite must initialize with `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, and a nonzero `busy_timeout` (recommended: 5000 ms).

### `restaurants`

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | primary key; server-generated UUID |
| `name` | TEXT | not null; trimmed; length 1–200 |
| `time_zone` | TEXT | not null; canonical IANA zone accepted by `zoneinfo.ZoneInfo` |
| `created_at_utc` | TEXT | not null; RFC 3339 UTC instant |

### `restaurant_opening_hours`

| Column | Type | Constraints |
|---|---|---|
| `restaurant_id` | TEXT | FK to `restaurants(id)` on delete cascade |
| `weekday` | INTEGER | 0=Monday through 6=Sunday; check 0–6 |
| `opens_local` | TEXT | `HH:MM`; 15-minute aligned |
| `closes_local` | TEXT | `HH:MM`; 15-minute aligned; strictly later than open |

Primary key: `(restaurant_id, weekday)`. A missing weekday means closed.

### `restaurant_tables`

| Column | Type | Constraints |
|---|---|---|
| `restaurant_id` | TEXT | FK to `restaurants(id)` on delete cascade |
| `table_id` | TEXT | trimmed client identifier; length 1–64 |
| `seats` | INTEGER | not null; check `seats > 0` |

Primary key: `(restaurant_id, table_id)`.

### `reservations`

| Column | Type | Constraints |
|---|---|---|
| `id` | TEXT | primary key; server-generated UUID |
| `restaurant_id` | TEXT | not null |
| `table_id` | TEXT | not null |
| `party_size` | INTEGER | not null; check `party_size > 0` |
| `slot_start_utc` | TEXT | not null; canonical RFC 3339 UTC instant |
| `local_date` | TEXT | not null; `YYYY-MM-DD` audit value |
| `local_time` | TEXT | not null; `HH:MM` audit value |
| `created_at_utc` | TEXT | not null; RFC 3339 UTC instant |

Composite FK `(restaurant_id, table_id)` references `restaurant_tables`. A trigger aborts insertion or changes to restaurant, table, or party size when the assigned table has fewer seats.

### `reservation_slot_claims`

| Column | Type | Constraints |
|---|---|---|
| `restaurant_id` | TEXT | not null |
| `table_id` | TEXT | not null |
| `slot_start_utc` | TEXT | not null |
| `reservation_id` | TEXT | unique; FK to `reservations(id)` on delete cascade |

Primary key: `(restaurant_id, table_id, slot_start_utc)`. Composite FK `(restaurant_id, table_id)` references `restaurant_tables`. A trigger must reject a claim whose restaurant, table, or slot differs from its reservation.

## HTTP API contract

All request and response bodies are JSON. Validation errors use FastAPI's `422` response shape. IDs are opaque strings. Unknown JSON fields are rejected. Internal lock exhaustion or unexpected storage errors return `503` or `500` respectively without stack traces.

### `POST /restaurants`

Request:

```json
{
  "name": "Cafe Example",
  "time_zone": "Asia/Kolkata",
  "opening_hours": [
    {"weekday": 0, "opens": "09:00", "closes": "22:00"}
  ]
}
```

Responses: `201` with `{id,name,time_zone,opening_hours}`; `422` for an invalid/unknown time zone, duplicate weekday, malformed/non-aligned time, overnight interval, or invalid name; `500` for unexpected failure.

### `POST /restaurants/{restaurant_id}/tables`

Request: `{"id":"T1","seats":4}`.

Responses: `201` with `{id,seats}`; `404` if the restaurant does not exist; `409` if that table ID already exists at this restaurant; `422` for invalid ID or seats; `500` for unexpected failure.

### `GET /restaurants/{restaurant_id}/availability`

Query parameters: `date=YYYY-MM-DD`, `time=HH:MM`, and positive integer `party_size`. Availability is evaluated for `[time, time + 15 minutes)` in the restaurant's IANA zone.

Responses: `200` with `{"date":"...","time":"...","party_size":2,"available_tables":[{"id":"T1","seats":4}]}` ordered by seats then table ID; `404` if the restaurant does not exist; `422` for invalid parameters, non-aligned time, invalid/ambiguous DST wall time, or a slot outside opening hours; `500` for unexpected failure. This response is advisory; only reservation creation claims capacity.

### `POST /restaurants/{restaurant_id}/reservations`

Request: `{"date":"2030-05-20","time":"19:15","party_size":2}`. The server assigns a table according to I5.

Responses: `201` with `{id,restaurant_id,table_id,party_size,date,time,slot_start_utc}`; `404` if the restaurant does not exist; `409` with `{"detail":"no_table_available"}` when no fitting unclaimed table can be committed; `422` for invalid party size/date/time, non-aligned time, invalid/ambiguous DST wall time, or a slot outside opening hours; `503` with `{"detail":"database_busy"}` after the bounded SQLite busy timeout; `500` for unexpected failure.

## Concurrency strategy

Reservation creation uses one database connection and one explicit transaction:

1. Execute `BEGIN IMMEDIATE` so competing writers serialize before table selection.
2. Validate the restaurant, local time, opening hours, and candidate capacity.
3. Select the first fitting unclaimed table ordered by `seats, table_id`.
4. Insert the reservation, then its slot claim, and commit.
5. On a uniqueness violation, roll back the whole transaction and return `409`; on busy/locked exhaustion, roll back and return `503`.

The composite primary key remains the final protection even if selection logic changes or multiple processes race. Availability reads need no write lock and may become stale immediately. Connections are request-scoped and never shared concurrently across threads. Restaurant and table writes also use explicit transactions.

## Auditor test and attack list

1. Create a restaurant with valid IANA zone and hours; reject fake zones, duplicate weekdays, unaligned times, empty names, equal/reversed hours, and overnight hours.
2. Add tables; reject zero/negative/non-integer seats, blank/oversized IDs, duplicate IDs within one restaurant, and unknown restaurants; permit the same table ID at different restaurants.
3. Query availability on open/closed boundaries and verify deterministic ordering and capacity filtering.
4. Reject reservation and availability times not aligned to 15 minutes, outside hours, spanning close, malformed dates, DST gaps, and DST folds.
5. Create a reservation, verify the assigned smallest fitting table, verify it disappears for the claimed slot, and verify it remains available at adjacent slots.
6. Fire concurrent reservation requests for the same last available table/slot from separate connections/processes; assert exactly one `201`, all others `409` (or bounded `503` under lock pressure), one reservation, and one claim.
7. Attempt direct duplicate slot-claim insertion; assert the composite primary key rejects it.
8. Attempt direct reservation insertion or update with `party_size > seats`; assert the database trigger rejects it and no orphan claim remains.
9. Attempt cross-restaurant table/claim mismatches and orphan foreign keys with `foreign_keys=ON`; assert rejection.
10. Force a failure between reservation and claim insertion; assert rollback leaves neither row.
11. Hold a write lock beyond `busy_timeout`; assert the API returns `503` without leaking SQL or stack details.
12. Install `stage-1/requirements.txt` into the repository `.venv`, then run `.venv\Scripts\python -m pytest stage-1/tests -q` with no internet access; assert no test depends on DNS, package downloads, external databases, or network services. Confirm the FastAPI server boots from the same `.venv`. Do not build or run `stage-1/Dockerfile` during Stage 1; Docker verification is deferred to the final stage.
