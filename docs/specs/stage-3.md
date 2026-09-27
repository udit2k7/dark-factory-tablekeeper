# Stage 3 - Idempotency, Cancellation, and Zoned Time

## Scope and assumptions

Stage 3 starts from Stage 2 under `stage-3/` and preserves its API, SQLite WAL configuration, 30-second-or-greater busy timeout, four-process contention behavior, validation, and error sanitization except where this specification extends them.

A reservation still occupies exactly one 15-minute interval `[start, start + 15 minutes)`. The API accepts a restaurant-local `date` and `time`; the database stores canonical UTC instants as `YYYY-MM-DDTHH:MM:SSZ`. The local and UTC dates may differ, and the UTC interval may end on the following UTC day. Opening-hours decisions are made against the restaurant-local interval, never against the UTC calendar date.

`Idempotency-Key` is optional on reservation creation to preserve the Stage 2 API. When present, it governs the rules below. A successful keyed response is persisted atomically with the reservation and is replayed even after that reservation is later cancelled.

## Numbered invariants

1. **I1 - Exclusive active claim.** At most one confirmed reservation may claim `(restaurant_id, table_id, slot_start_utc)`. The composite primary key of `reservation_slot_claims` is authoritative across processes.
2. **I2 - Capacity.** A confirmed reservation's positive party size cannot exceed its table's seats; SQLite triggers enforce this rule.
3. **I3 - Keyed creation atomicity.** For a successful keyed request, the reservation, all slot claims, and the stored idempotency response commit in one transaction or all roll back.
4. **I4 - Same-key replay.** Reusing a key with the same canonical request returns the original `201` status and byte-equivalent JSON response body without creating or reclaiming anything.
5. **I5 - Key conflict.** Reusing a key with a different canonical request returns `422 idempotency_key_reused` and changes no reservation, claim, or idempotency row.
6. **I6 - Atomic cancellation.** Cancelling a confirmed reservation changes it to cancelled and deletes all of its slot claims in one transaction. No committed state may show a cancelled reservation with claims or a confirmed reservation partially freed.
7. **I7 - Idempotent cancellation.** Repeated or concurrent cancellation of the same reservation returns the same `200` body, including the original cancellation timestamp, and performs no additional state change.
8. **I8 - Zoned-time validity.** Local starts in a DST gap or fold are rejected with `422`; clients cannot select a fold. The entire local 15-minute interval must lie inside that weekday's opening interval.
9. **I9 - Canonical UTC.** Every persisted creation, cancellation, reservation start, and claim start timestamp is an aware UTC instant serialized exactly as `YYYY-MM-DDTHH:MM:SSZ`.
10. **I10 - Multi-process safety.** Reservation creation and cancellation execute `BEGIN IMMEDIATE` before reading mutable booking/idempotency state. Stage 2's unkeyed 50-request result remains exactly one `201` and 49 `409`.

## Data schema

Stage 2 tables and triggers remain, with these Stage 3 changes.

### `reservations` additions

| Column | Type | Constraints |
|---|---|---|
| `status` | TEXT | not null; check in `('confirmed','cancelled')`; default `confirmed` |
| `cancelled_at_utc` | TEXT | nullable; null iff confirmed, non-null iff cancelled |

A table-level check enforces the `status`/`cancelled_at_utc` relationship. Existing columns remain: `id`, `restaurant_id`, `table_id`, `party_size`, `slot_start_utc`, `local_date`, `local_time`, and `created_at_utc`. Capacity triggers continue to protect inserts and relevant updates.

### `reservation_slot_claims`

The composite primary key remains `(restaurant_id, table_id, slot_start_utc)`, and `reservation_id` remains a foreign key to `reservations(id)` with `ON DELETE CASCADE`. Claim-consistency triggers must additionally require the referenced reservation to have `status='confirmed'`.

### `idempotency_records`

| Column | Type | Constraints |
|---|---|---|
| `idempotency_key` | TEXT | primary key; exact case-sensitive client value |
| `canonical_request` | TEXT | not null; canonical JSON described below |
| `reservation_id` | TEXT | not null, unique; FK to `reservations(id)` |
| `response_status` | INTEGER | not null; check equals `201` |
| `response_body` | TEXT | not null; canonical JSON response stored before commit |
| `created_at_utc` | TEXT | not null; canonical UTC instant |

Keys are globally scoped to this service, 1-255 visible ASCII characters after no trimming; leading/trailing whitespace and control/non-ASCII characters are invalid. The canonical request contains the path restaurant ID plus the validated semantic fields `date`, `time`, and integer `party_size`, serialized with sorted keys and fixed compact separators. JSON member order and insignificant request whitespace therefore do not change identity. The implementation compares canonical request text, not a hash alone.

Only successful `201` creation responses are stored. Validation failures, unavailable-table `409` responses, lock timeouts, and internal failures do not consume a key.

### Required database rules

Every connection executes `PRAGMA foreign_keys=ON` and `PRAGMA busy_timeout>=30000`; initialization executes `PRAGMA journal_mode=WAL`. All absolute times use the canonical UTC representation. The shared database path rules from Stage 2 remain unchanged.

## HTTP API contract

All bodies are JSON, unknown fields are rejected, and errors expose no SQL, trigger text, database path, or stack trace. Stage 2 endpoints and status codes remain unless amended below.

### `POST /restaurants`

Creates a restaurant using any IANA zone key accepted by `zoneinfo`, including backward-compatible aliases.

Responses: `201`; `422` for invalid input; sanitized `500 internal_error`; `503 database_busy` after genuine lock-timeout exhaustion.

### `POST /restaurants/{restaurant_id}/tables`

Responses: `201`; `404 restaurant_not_found`; `409 table_already_exists` only for the table primary-key conflict; `422` for invalid input; sanitized `500 internal_error`; `503 database_busy` after genuine lock-timeout exhaustion.

### `GET /restaurants/{restaurant_id}/availability`

Accepts local `date`, local 15-minute-aligned `time`, and positive integer `party_size`. Cancelled reservations have no claims and therefore do not reduce availability.

Responses: `200`; `404 restaurant_not_found`; `422` for invalid input, DST gap/fold, closed day, or an interval not fully inside opening hours; sanitized `500 internal_error` for unexpected storage failure.

### `POST /restaurants/{restaurant_id}/reservations`

Optional header: `Idempotency-Key`. Body: `{"date":"YYYY-MM-DD","time":"HH:MM","party_size":N}`.

First successful request returns `201` with `{id,restaurant_id,table_id,party_size,date,time,slot_start_utc}`. An identical keyed replay returns the stored `201` and exactly the stored body, whether the reservation is still confirmed or has since been cancelled.

Other responses: `404 restaurant_not_found`; `409 no_table_available`; `422 idempotency_key_reused` for same key/different canonical request; `422` for invalid key, body, local time, DST gap/fold, or opening hours; `503 database_busy`; sanitized `500 internal_error` for unrelated failures.

### `POST /reservations/{reservation_id}/cancel`

No request body. The first call atomically releases all claims and marks the reservation cancelled. Response `200`:

```json
{"id":"reservation-uuid","status":"cancelled","cancelled_at_utc":"2030-05-20T12:34:56Z"}
```

Every later or concurrent call returns the identical persisted fields and `200`. Unknown reservation returns `404 reservation_not_found`. Genuine lock-timeout exhaustion returns `503 database_busy`; unexpected storage failure returns sanitized `500 internal_error`.

## Transaction and concurrency strategy

### Keyed reservation creation

1. Validate header shape and request body without writing.
2. Build the canonical request including `restaurant_id`.
3. Open one connection and execute `BEGIN IMMEDIATE` before reading `idempotency_records`, availability, or claims.
4. If the key exists and the canonical request matches, return its stored status/body without selecting a table or writing. If it differs, return `422 idempotency_key_reused` without writing.
5. If the key is absent, validate zoned time/opening hours, select the deterministic free table, insert the confirmed reservation and claim, serialize the final response, then insert the idempotency record.
6. Commit all three records before returning `201`. Any failure rolls back all three.

Unkeyed creation follows the Stage 2 `BEGIN IMMEDIATE` flow. A uniqueness check performed outside the transaction, a response cached after commit, or process-local idempotency memory is forbidden.

### Cancellation

1. Open one connection and execute `BEGIN IMMEDIATE`.
2. Load the reservation. Return `404` if absent.
3. If already cancelled, return its persisted cancellation response unchanged.
4. Otherwise generate one canonical UTC cancellation timestamp, delete every claim for the reservation, update status and timestamp, then commit.
5. Return `200` only after commit. Roll back all changes on any error.

`BEGIN IMMEDIATE` serializes cancellation against creation and other cancellations across all Uvicorn processes. Idempotency records are retained permanently when their reservation is cancelled, so a retry cannot create a replacement booking.

## Time conversion rules

1. Parse `date` strictly as `YYYY-MM-DD` and `time` strictly as `HH:MM`, aligned to 15 minutes.
2. Combine them as a naive local wall time and evaluate both PEP 495 `fold` values in the restaurant's `zoneinfo.ZoneInfo` zone.
3. Round-trip each candidate through UTC. No valid round-trip means a DST gap; two candidates with distinct UTC offsets means an ambiguous fold. Reject either with `422`.
4. Validate `[local_start, local_start + 15 minutes)` against the opening interval for the local weekday. Opening intervals remain non-overnight.
5. Convert the unique valid start to UTC and serialize with seconds and trailing `Z`. Compute slot identity from that UTC instant, without assuming its UTC date equals the local date.

Example: a valid local late-evening booking in `America/Los_Angeles` may have a next-day UTC date. A UTC `23:45` slot ends at `00:00` on the next UTC date; it remains one valid interval and one claim.

## Auditor test and attack list

1. Send the same valid key/body concurrently through four real Uvicorn processes; assert every response is the same stored `201` body and the database contains one reservation, one claim, and one idempotency row.
2. Reuse a committed key with a different party size, date, time, or restaurant path; assert `422 idempotency_key_reused` and no state change.
3. Prove JSON member order/whitespace do not affect canonical request identity; prove key case does affect identity.
4. Reject missing-value, empty, whitespace-edge, control-character, non-ASCII, and over-255-character keys when the header is supplied. Confirm omission remains valid.
5. Inject failure after reservation/claim insertion but before idempotency insertion; assert all three roll back. Inject failure after idempotency insertion but before commit with the same result.
6. Cancel a confirmed reservation and directly verify status/timestamp persisted and all its claims disappeared; availability must immediately include the table again.
7. Cancel the same reservation sequentially and concurrently through all four processes; assert all calls return byte-equivalent `200` bodies with one unchanged timestamp and no claims.
8. Replay the original creation key after cancellation; assert the original `201` body is returned, no claim is recreated, and no second reservation appears.
9. Race cancellation of the winner against new booking requests for the same slot; assert serializable outcomes, at most one active claim, and no `500`/orphan state.
10. Test representative DST spring gaps and autumn folds in at least one changing IANA zone; both reservation and availability endpoints must return `422`.
11. Test fixed-offset behavior through a non-DST IANA zone and an accepted backward-compatible alias.
12. Test local-to-UTC conversion where dates differ and a 15-minute interval whose UTC end crosses midnight; assert canonical `Z` storage, correct claim identity, and opening-hours decisions based on local time.
13. Test opening and closing boundaries, including rejection when any part of the 15-minute interval falls outside hours.
14. Re-run Stage 2's three four-process unkeyed 50-request races; each must remain exactly one `201`, 49 `409`, one durable confirmed reservation, and one claim.
15. Run the complete Stage 3 suite using `.venv\Scripts\python -m pytest stage-3/tests -q` and in the built Stage 3 image with `docker run --rm --network none <stage-3-image> python -m pytest -q`.
