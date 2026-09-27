# Stage 4 - Final Package and Mixed-Operation Stress Harness

## Scope and deliverables

Stage 4 is the final packaged service under `stage-4/`, starting from Stage 3 without weakening any prior API, schema, transaction, time-zone, idempotency, cancellation, or four-process guarantee.

Required artifacts are:

- `stage-4/src/` - production FastAPI/SQLite service.
- `stage-4/requirements.txt` - complete pinned-or-bounded runtime and test dependencies installable before network isolation.
- `stage-4/Dockerfile` - self-contained image containing the service and test assets.
- `stage-4/tests/` - every Stage 3 test carried forward as a Stage 4 regression, plus Stage 4 tests.
- `stage-4/tests/stress_harness.py` - directly executable real-HTTP mixed-operation harness.

The normal harness must complete at least 200 HTTP operations released as one concurrent mixed workload against four independent Uvicorn OS processes. It prints exactly one machine-readable JSON summary as its final stdout line and exits `0` only when all HTTP accounting and database audits pass.

## Numbered invariants

1. **I1 - No double booking.** No two active slot claims may share `(restaurant_id, table_id, slot_start_utc)`; the production schema retains that composite primary key.
2. **I2 - Capacity.** Every reservation's `party_size` is positive and no greater than its assigned table's `seats`.
3. **I3 - Idempotent creation.** One `Idempotency-Key` and canonical request identify at most one reservation and one stored `201` response; identical retries return that exact response.
4. **I4 - Complete cancellation.** A cancelled reservation has zero slot claims; repeated/concurrent cancellation returns the original persisted `200` body.
5. **I5 - Transactional writes.** Booking, keyed-response storage, and cancellation keep the Stage 3 `BEGIN IMMEDIATE` transaction boundaries and rollback guarantees.
6. **I6 - Mixed-load safety.** Concurrent bookings, keyed retries, cancellations, rebooks, and availability reads across four processes produce no `500`, `503`, malformed response, transport failure, hang, or invariant violation in the normal harness.
7. **I7 - Auditable accounting.** Every measured HTTP attempt is classified exactly once as success, conflict, or error: `attempts = successes + conflicts + errors`.
8. **I8 - Process isolation.** Four distinct Uvicorn PIDs share one absolute SQLite database path and listen on four distinct OS-assigned loopback ports.
9. **I9 - Portable cleanup.** Success, assertion failure, timeout, and keyboard interruption all terminate and reap every child process, close logs/sockets/database handles, and release temporary files on Windows and Linux.
10. **I10 - Mutation sensitivity.** With only the slot-claim primary key disabled in an isolated mutant database, a controlled overlapping-claim probe is accepted by SQLite and the harness audit reports a violation instead of a false clean result.

## Data schema

The production schema is unchanged from Stage 3:

- `restaurants` and `restaurant_opening_hours` define an IANA-zone restaurant and local weekly hours.
- `restaurant_tables` has composite primary key `(restaurant_id, table_id)` and positive `seats`.
- `reservations` contains canonical UTC/local audit fields, positive party size, `status` in `confirmed|cancelled`, and the status/cancellation-timestamp consistency check.
- `reservation_slot_claims` has composite primary key `(restaurant_id, table_id, slot_start_utc)`, unique `reservation_id`, and foreign keys/triggers tying each claim to the same confirmed reservation.
- `idempotency_records` has primary key `idempotency_key`, the canonical request, unique reservation reference, stored `201` status/body, and canonical UTC creation time.

Capacity and claim-consistency triggers remain mandatory. Every connection enables foreign keys and a busy timeout of at least 30 seconds; initialization enables WAL. All four servers receive the identical absolute `DATABASE_PATH` and never use in-memory or per-process databases.

The mutation database is temporary and isolated. Mutation mode may rebuild only `reservation_slot_claims` without its composite primary key while preserving its columns and other constraints. It must never alter the production schema file, a developer database, or a normal harness run.

## HTTP API contract

Stage 3 endpoints and status codes are unchanged:

| Method | Path | Success | Expected client/contention errors | Storage errors |
|---|---|---|---|---|
| `POST` | `/restaurants` | `201` | `422` invalid input | sanitized `500`; bounded `503` |
| `POST` | `/restaurants/{restaurant_id}/tables` | `201` | `404`, `409`, `422` | sanitized `500`; bounded `503` |
| `GET` | `/restaurants/{restaurant_id}/availability` | `200` | `404`, `422` | sanitized `500` |
| `POST` | `/restaurants/{restaurant_id}/reservations` | `201` including stored keyed replays | `404`, `409`, `422` | sanitized `500`; bounded `503` |
| `POST` | `/reservations/{reservation_id}/cancel` | replayable `200` | `404` | sanitized `500`; bounded `503` |

The normal harness sends only valid input. It treats `409 no_table_available` as an expected conflict. Every other non-2xx response, including `422`, `500`, and `503`, is an error for harness accounting.

## Stress harness interface

Normal invocation:

```text
python stage-4/tests/stress_harness.py
```

Mutation invocation:

```text
python stage-4/tests/stress_harness.py --mutation-disable-slot-pk
```

The harness may accept optional deterministic seed, operation-count, and timeout arguments, but defaults must satisfy this specification without environment-specific configuration. The default operation count is at least 240 and never below 200.

### Topology and startup

1. Create one temporary directory and one absolute shared SQLite path.
2. Obtain four distinct ephemeral ports by binding numeric loopback `127.0.0.1` with port `0`; do not hard-code 8001-8004. Close each selection socket immediately before its corresponding spawn and retry the select/spawn sequence if Uvicorn reports an address collision.
3. Start four independent commands using `sys.executable -m uvicorn`, without `shell=True`, reloaders, or worker managers. Each process receives the same `DATABASE_PATH`, its assigned port, and isolated log destination.
4. Poll a real HTTP endpoint such as `/openapi.json` on numeric loopback until every server is ready or the bounded startup deadline expires. Assert four distinct live PIDs.
5. Seed through real HTTP (or before servers start through a closed setup connection) one open restaurant, at least eight tables with varied capacities, at least six local target times, and enough confirmed reservations to support cancellation/rebook races. Setup calls are not included in measured counters.

The test must use real TCP requests. FastAPI/Starlette `TestClient`, ASGI transports, direct route calls, and in-process Uvicorn are forbidden.

### Mixed workload

Construct at least 240 measured operations before release, including no fewer than:

- 60 fresh keyed or unkeyed booking attempts spread across multiple tables, party sizes, and at least six times.
- 50 identical retries reusing successful or concurrently submitted keys.
- 40 cancellation attempts, including repeated/concurrent cancellation IDs.
- 40 fresh-key rebooking attempts targeting slots being cancelled.
- 50 availability reads spread across party sizes and times.

Some logical operations may intentionally target the same capacity, but all request bodies/headers are valid. Distribute every category across the four ports and ensure every port receives traffic. Submit all measured operations to a client executor, hold them behind one barrier/event, then release them together. Per-request and overall deadlines are mandatory.

Because cancellations and rebooks race, a rebook may validly return `201` or `409`. Fresh oversubscribed bookings may also return `409`. Keyed identical retries must return byte-identical `201` bodies; cancellation retries must return byte-identical `200` bodies. Availability calls return `200`.

### Status accounting

- `attempts`: number of measured HTTP requests completed or attempted after the release barrier.
- `successes`: responses with status `200` or `201` that also satisfy their body/replay assertions.
- `conflicts`: responses with status `409` and exact body `{"detail":"no_table_available"}`.
- `errors`: transport/timeouts, process exits, invalid JSON, body-contract failures, unexpected statuses, or internal harness exceptions attributable to measured work.

Each request increments exactly one terminal category. Normal mode requires `attempts >= 200`, the accounting equality in I7, and `errors == 0`.

## Final JSON summary contract

The final stdout line is one compact JSON object with at least this stable shape; diagnostic logs go to stderr or per-process files:

```json
{
  "attempts": 240,
  "successes": 180,
  "conflicts": 60,
  "errors": 0,
  "database_audit": {
    "double_bookings": 0,
    "over_capacity": 0,
    "duplicate_idempotent_bookings": 0,
    "leftover_slots_from_cancelled_bookings": 0
  }
}
```

Counts shown are illustrative except for required zeros. Extra deterministic fields such as seed, elapsed time, operation counts, process count, or mutation flag are allowed.

### Database audit definitions

Run the audit after all client futures complete and after opening a new direct connection to the shared file:

- `double_bookings`: number of `(restaurant_id, table_id, slot_start_utc)` groups in active claims with `COUNT(*) > 1`.
- `over_capacity`: number of reservations whose stored `party_size` exceeds the joined table's `seats`.
- `duplicate_idempotent_bookings`: number of idempotency keys for which observed successful replay bodies name more than one reservation ID, cross-checked so those IDs exist in the database; also include duplicate key/relationship groups discoverable in storage.
- `leftover_slots_from_cancelled_bookings`: number of claims joined to reservations with `status='cancelled'`.

Normal mode requires all four values to be zero. The audit should additionally fail on orphan claims, confirmed reservation/claim mismatches, malformed stored response JSON, or idempotency records pointing to missing/different reservations, even if those optional counts are reported separately.

## Mutation mode

`--mutation-disable-slot-pk` proves audit sensitivity deterministically; it is not a benchmark run.

1. Use a fresh temporary database and rebuild only the claim table without `PRIMARY KEY (restaurant_id, table_id, slot_start_utc)`.
2. Run the normal mixed workload.
3. In one controlled probe, create two valid confirmed reservations for the same restaurant/table/UTC slot and insert both matching claims directly. Production schema must reject the second claim; the isolated mutant accepts it.
4. Run the same final audit. Require `database_audit.double_bookings >= 1` while the other audit logic still executes.
5. Print the JSON summary and exit with code `2` to signal a detected invariant violation. Exit `1` means harness/setup failure. A mutation regression test passes only when it observes exit `2` and the reported nonzero double-booking count.

The controlled probe is necessary because `BEGIN IMMEDIATE` normally serializes API writers even when the final primary-key defense is removed. Mutation mode changes no other protection merely to manufacture a race.

## Windows-safe teardown

All startup and workload logic is enclosed in `try/finally`. Teardown must:

1. Stop submitting work, cancel pending client futures where possible, and close HTTP clients.
2. Call `terminate()` on every still-live Uvicorn `Popen` object, wait with a bounded timeout, then call `kill()` only for survivors and wait again.
3. Avoid Unix-only signals, process groups, shell commands, `taskkill`, inherited console assumptions, or deletion while child processes remain live.
4. Close parent-side log handles and every SQLite connection before temporary cleanup.
5. Retry temporary-file removal briefly for Windows handle-release latency and report cleanup failure without hiding the primary verdict.

Child output must not use unconsumed `PIPE`s that can deadlock. Redirect it to closed-on-cleanup files or a safely drained destination. No Uvicorn child may remain after any exit path.

## Packaging and verification

The Docker build must install all dependencies and copy source plus tests/harness before network isolation. Neither tests nor harness may download at runtime or contact anything except numeric loopback.

Required Windows `.venv` commands:

```text
.venv\Scripts\python -m pytest stage-4/tests -q
.venv\Scripts\python stage-4/tests/stress_harness.py
```

Required container commands:

```text
docker run --rm --network none <stage-4-image> python -m pytest -q
docker run --rm --network none <stage-4-image> python tests/stress_harness.py
```

The Stage 4 suite includes every Stage 3 test as a regression and invokes mutation mode in a bounded subprocess, asserting its expected exit `2` and detected violation. Normal harness runs must exit `0` with four zero audit counts in both environments.

## Auditor attack list

1. Verify all Stage 3 tests are present and pass from `stage-4/tests`; spot-check keyed replay, cancellation, UTC/DST, and 1x201/49x409 coverage.
2. Run the normal harness in Windows `.venv` and Docker `--network none`; parse only its final stdout line as JSON and validate types, accounting equality, minimum attempts, zero errors, and all four zero audits.
3. Confirm four distinct PIDs, distinct ephemeral ports, one absolute database path, all five operation categories, at least eight tables, and at least six target times.
4. Repeat with several seeds and scheduling jitter; reject hangs, `500`, `503`, transport errors, malformed bodies, or flaky cleanup.
5. Confirm exact body identity for same-key booking retries and repeated/concurrent cancellations across different server processes.
6. Directly recompute every database audit with independent SQL and compare it with the printed counts.
7. Run mutation mode in both environments; require exit `2` and `double_bookings >= 1`. Run the same controlled probe against production schema and require rejection.
8. Force startup failure, mid-workload assertion failure, timeout, and keyboard-style interruption; confirm all child PIDs are reaped and ports/temp files become reusable.
9. Inspect for fixed ports, `shell=True`, Unix-only teardown, unconsumed pipes, external DNS/network use, or per-process database paths.
10. Remove or bypass each major protection in isolated mutants: slot PK, capacity trigger, keyed response transaction, cancellation claim deletion, and `BEGIN IMMEDIATE`. The corresponding regression or audit must fail.
