# Stage 2 - Multi-Process Reservation Contention

## Amendment - 2026-09-27

The original fixed-port examples are superseded. Every acceptance iteration allocates four distinct free ephemeral ports on numeric loopback, starts one Uvicorn process per port, and distributes requests across all four. Teardown must terminate and reap each complete server process tree, then poll every allocated port until it is confirmed closed before the iteration returns. This amendment is normative wherever older text referred to ports 8001-8004.

## Scope and acceptance outcome

Stage 2 starts as a behavioral copy of Stage 1 under `stage-2/` and preserves every Stage 1 API, validation rule, database invariant, and error-sanitization rule unless this specification strengthens it. The stack remains Python 3.11, FastAPI, Uvicorn, and SQLite in WAL mode.

The defining acceptance case is 50 simultaneous HTTP reservation requests for the same last available table and 15-minute slot, distributed across four independent Uvicorn OS processes on four distinct free ephemeral ports on `127.0.0.1`, all using one SQLite database file. The exact result must be one `201 Created`, 49 `409 Conflict` responses with `{"detail":"no_table_available"}`, zero `500` responses, zero `503` responses, and exactly one committed reservation/slot claim.

Both of these commands must pass without external network access:

- `.venv\Scripts\python -m pytest stage-2/tests -q`
- `docker run --rm --network none <stage-2-image> python -m pytest -q`

Inside the network-disabled container, tests use only numeric loopback addresses; they must not require DNS, published host ports, downloads, or any external service.

## Numbered invariants

1. **I1 - Exclusive slot claim.** At most one reservation may claim `(restaurant_id, table_id, slot_start_utc)`. The composite primary key of `reservation_slot_claims` is the final authority across threads and processes.
2. **I2 - Capacity.** A reservation has a positive party size no greater than the assigned table's seats. SQLite insert/update triggers enforce the cross-table rule.
3. **I3 - Atomic reservation.** A reservation row and its slot claim commit together or both roll back.
4. **I4 - Serialized selection.** Reservation creation obtains a SQLite write reservation with `BEGIN IMMEDIATE` before checking availability or selecting a table. Selection and both inserts occur on the same connection inside that transaction.
5. **I5 - Exact contention outcome.** For the defined 50-request/one-table workload, exactly one request returns `201`; the other 49 return `409 no_table_available`. The workload may not produce `500`, `503`, transport failures, or timeouts.
6. **I6 - Cross-process durability.** All four Uvicorn processes use the same resolved database path. A success returned by any process is immediately observable by transactions in every other process after commit.
7. **I7 - Deterministic assignment.** A transaction chooses the free fitting table with the fewest seats, then lexicographically smallest table ID.
8. **I8 - Referential and time integrity.** Stage 1 foreign-key, IANA-zone, DST, opening-hours, 15-minute alignment, and sanitized error rules remain in force.

## Data schema

The Stage 1 schema is retained in the shared SQLite file:

- `restaurants(id PK, name, time_zone, created_at_utc)`
- `restaurant_opening_hours(restaurant_id FK, weekday, opens_local, closes_local, PK(restaurant_id, weekday))`
- `restaurant_tables(restaurant_id FK, table_id, seats, PK(restaurant_id, table_id))`
- `reservations(id PK, restaurant_id, table_id, party_size, slot_start_utc, local_date, local_time, created_at_utc)` with composite FK to `restaurant_tables`
- `reservation_slot_claims(restaurant_id, table_id, slot_start_utc, reservation_id UNIQUE, PK(restaurant_id, table_id, slot_start_utc))` with table and reservation FKs

Capacity triggers on reservation insert/update and claim/reservation consistency triggers on claim insert/update are required. Every connection executes `PRAGMA foreign_keys=ON` and a nonzero `busy_timeout`. Database initialization executes `PRAGMA journal_mode=WAL` before servers accept traffic.

For Stage 2, `busy_timeout` must be long enough for all 50 local contenders to wait for serialized short transactions; use at least 30,000 ms. The database path is supplied as an absolute path through `DATABASE_PATH`; every server process must receive the identical value. No process may copy the database, use `:memory:`, or derive a per-process file.

## HTTP API contract

All bodies are JSON, unknown fields are rejected, and storage error bodies reveal no SQL, trigger text, database paths, or stack traces.

### `POST /restaurants`

Creates a restaurant with name, any IANA zone key accepted by `zoneinfo` (including aliases), and zero or one non-overnight opening interval per weekday.

Responses: `201`; `422` for invalid input; sanitized `500` for unexpected storage failure; `503 database_busy` only for genuine lock-timeout exhaustion outside the acceptance workload.

### `POST /restaurants/{restaurant_id}/tables`

Creates a positive-seat table scoped to a restaurant.

Responses: `201`; `404` for unknown restaurant; `409 table_already_exists` only for the table composite-primary-key conflict; `422` for invalid input; sanitized `500` for unrelated integrity/storage failure; `503 database_busy` only for genuine lock-timeout exhaustion outside the acceptance workload.

### `GET /restaurants/{restaurant_id}/availability`

Queries a restaurant-local `date`, 15-minute-aligned `time`, and positive integer `party_size`; results are ordered by seats then table ID. The result is advisory.

Responses: `200`; `404` for unknown restaurant; `422` for invalid, ambiguous, nonexistent, closed, or out-of-hours local slots; sanitized `500` for unexpected storage failure.

### `POST /restaurants/{restaurant_id}/reservations`

Accepts `{"date":"YYYY-MM-DD","time":"HH:MM","party_size":N}` and assigns a table inside the write transaction.

Responses: `201` with `{id,restaurant_id,table_id,party_size,date,time,slot_start_utc}`; `404` for unknown restaurant; `409` with `{"detail":"no_table_available"}` when no fitting unclaimed table remains or a proven slot-claim primary-key race occurs; `422` for invalid input or slot; sanitized `500 internal_error` for every unrelated integrity/storage failure; `503 database_busy` only after genuine lock-timeout exhaustion. Under I5's acceptance workload, only `201` and `409` are permitted.

## Concurrency strategy

Each request opens its own SQLite connection. Reservation creation performs this sequence without retrying the whole HTTP operation:

1. Execute `BEGIN IMMEDIATE` and wait up to the configured busy timeout.
2. Inside that transaction, validate the restaurant and slot.
3. Query for one fitting table with no claim at the UTC slot, ordered by `seats, table_id`.
4. If none exists, roll back and return `409 no_table_available`.
5. Insert the reservation and its slot claim, then commit before returning `201`.
6. If the slot-claim composite primary key is proven to have raced, roll back and return `409`; all other integrity failures roll back and return sanitized `500`.

`BEGIN DEFERRED`, an availability read before `BEGIN IMMEDIATE`, process-local mutexes, in-memory coordination, and check-then-insert across separate transactions are forbidden. The primary key remains mandatory even though `BEGIN IMMEDIATE` serializes writers.

The four server processes are independent `python -m uvicorn` processes, not Uvicorn workers hidden behind one test client. For each iteration, select four distinct free ports by binding numeric loopback with port `0`, then start one server per selected port; no fixed port may be assumed. Every process receives the same absolute `DATABASE_PATH`. Tests wait for all four processes to answer a real HTTP readiness probe before releasing contenders. In `finally`, they terminate and reap each full process tree, then poll all four ports until closed within a bounded deadline.

## Multi-process acceptance test contract

1. Create one temporary database, initialize one open restaurant and exactly one fitting table, and leave the target slot unclaimed.
2. Allocate four distinct free ephemeral numeric-loopback ports for this iteration, then launch four independent Uvicorn processes with the same database path and isolated logs.
3. Confirm all four PIDs are distinct and alive. Do not use FastAPI/Starlette `TestClient`, ASGI transports, monkeypatching, or direct route calls.
4. Prepare 50 identical real HTTP requests. Distribute them across all four ports (each port receives at least one), hold them behind one barrier/event, then release them together.
5. Bound every client request and the overall run with explicit timeouts so deadlocks fail the test rather than hang it.
6. Assert the complete response multiset is exactly one `201` and 49 `409`; assert every `409` body is `{"detail":"no_table_available"}` and no request has a transport error.
7. After responses complete, open the shared database directly with a new connection. Assert exactly one reservation and one slot claim exist for the target restaurant/table/slot and their IDs/fields match.
8. Run a direct overlap query grouped by `(restaurant_id, table_id, slot_start_utc)` with `HAVING COUNT(*) > 1`; assert it returns no rows.
9. Query each server after the race (or perform an equivalent real HTTP observation) to prove all processes observe the committed claim.
10. Repeat the race with a fresh database and a fresh set of four ephemeral ports enough times to expose timing-sensitive failures; a minimum of three iterations is required.
11. On every success or failure path, terminate and reap all descendants of the four server roots and poll each allocated port until closed before the next iteration begins.

The 50 clients may be concurrent threads or processes, but every request must cross a real TCP socket to one of the four independent servers.

## Auditor attack list

1. Run the exact multi-process acceptance test in `.venv` and in the built Stage 2 image with `--network none`.
2. Record the 50 status codes and fail on any count other than `1 x 201` plus `49 x 409`, including any `500`, `503`, timeout, or connection failure.
3. Prove requests reached all four ports and that four distinct live server PIDs shared one absolute database path.
4. Add scheduling jitter before transaction start and repeat at least three fresh-database races.
5. Hold the winning transaction briefly to force lock waiting; verify contenders wait and later return `409`, not `503`.
6. Mutate reservation creation to `BEGIN DEFERRED` or move selection before `BEGIN IMMEDIATE`; the contention suite must fail or expose forbidden outcomes.
7. Remove the slot-claim composite primary key in an isolated mutant; the direct overlap/claim checks must detect loss of protection.
8. Inject a non-uniqueness integrity failure during reservation or table creation; assert full rollback and sanitized `500 internal_error` with no SQL/trigger leakage.
9. Verify Stage 1 capacity, foreign-key, DST, opening-boundary, deterministic-table, and unknown-field regressions against Stage 2.
10. Kill/restart one server between setup and contention; confirm the surviving processes still use the shared durable database and no partial booking appears.
11. Confirm complete server process trees are terminated and reaped on both test success and assertion failure; bounded closed-port polling must prove that no port or database handle leaks into later tests.
