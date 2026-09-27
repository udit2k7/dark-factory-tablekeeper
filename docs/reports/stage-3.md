# Stage 3 Independent Verification Report

## Verdict

**APPROVED**

The current Stage 3 suite contains 72 tests: all 44 inherited Stage 1/2 tests plus 28 Stage 3 cases for durable idempotency, canonical response replay, cancellation, UTC edge cases, DST ambiguity, and multi-process races. The complete suite passed three consecutive times in the Windows `.venv` and once in Docker with external networking disabled. Application code was not modified by the auditor.

## Scope

- Specification: `docs/specs/stage-3.md`
- Application inspected: `stage-3/src/`, `stage-3/Dockerfile`, `stage-3/requirements.txt`
- Tests: `stage-3/tests/conftest.py`, `test_multiprocess.py`, `test_regressions.py`, `test_stage1_regressions.py`, `test_stage3.py`

## Invariant and attack coverage

- I1/I2: composite slot-claim primary key, capacity insert/update triggers, confirmed-only claim triggers, foreign keys, and direct duplicate/cross-restaurant attacks.
- I3: injected `BEFORE` and `AFTER` idempotency-record insert failures both prove reservation, claim, and key row roll back together.
- I4: 50 simultaneous same-key requests are distributed across four real Uvicorn OS processes on four free ephemeral ports. All return one byte-identical stored `201` body backed by exactly one reservation, one claim, and one idempotency row; a post-restart replay is identical. This exceeds the requested 20-request process test, so no duplicate test was added.
- I5: party-size, date, time, and restaurant-path conflicts return `422 idempotency_key_reused` without state changes; JSON order/whitespace are canonicalized and key case remains distinct.
- Key validation: omitted key succeeds; empty, whitespace-edge, control, DEL, non-ASCII, and 256-byte keys return `422` and consume nothing; failed `409` creation does not consume a key.
- I6/I7: cancellation deletes claims and persists one timestamp atomically; 20 concurrent cancellations across four processes return one byte-identical `200` body; injected cancellation failure restores confirmed state and its claim.
- Cancel/replay: keyed creation replay after cancellation returns the original `201` bytes without recreating a claim or reservation.
- Cancel/create race: concurrent cancellation and 20 new bookings produce no `500`/`503`, at most one confirmed replacement, one cancelled original, and claims equal confirmed rows.
- I8/I9: inherited DST gap/fold and opening-boundary cases pass. A new explicit Stage 3 test proves ambiguous `2030-11-03 01:30` in `America/New_York` returns `422` and persists no reservation, claim, or idempotency row. An `Asia/Kolkata` local `2030-05-20 05:15` persists as `2030-05-19T23:45:00Z`, whose interval ends at UTC midnight. All audited absolute timestamps match canonical `YYYY-MM-DDTHH:MM:SSZ`.
- I10: three inherited four-process unkeyed races each remain exactly `1 x 201 + 49 x 409`; selection still begins with `BEGIN IMMEDIATE`. Processes use one absolute database path, numeric loopback, ephemeral ports, bounded waits, whole-tree teardown, and closed-port polling.
- Exact offline matrix: five sequential plus five simultaneous uses of one key return ten identical `201` bodies backed by exactly one reservation, one claim, and one key row; changing the body returns `422`. Double cancellation is byte-identical, a new key can rebook the released slot, UTC-midnight conversion is canonical, and DST-gap/out-of-hours requests return `422`.

## Commands and real results

Full Windows `.venv` suite, three consecutive runs:

```text
.venv\Scripts\python -m pytest stage-3/tests -q
72 passed, 1 warning in 116.34s (0:01:56)
exit 0

.venv\Scripts\python -m pytest stage-3/tests -q
72 passed, 1 warning in 120.55s (0:02:00)
exit 0

.venv\Scripts\python -m pytest stage-3/tests -q
72 passed, 1 warning in 114.90s (0:01:54)
exit 0
```

Image build:

```text
docker build -t tablekeeper-stage3-audit:latest stage-3
exit 0
```

Required isolated suite:

```text
docker run --rm --network none tablekeeper-stage3-audit:latest python -m pytest tests -q
........................................................................ [100%]
72 passed, 1 warning in 86.27s (0:01:26)
exit 0
```

The warning is Starlette's library-level deprecation warning for the `anyio.abc.BlockingPortal` alias.

## Protection-removal sensitivity

`BEGIN DEFERRED` scratch mutant:

```text
FAILED tests/test_multiprocess.py::test_four_process_exact_contention[0]
observed Counter({503: 50}); expected Counter({409: 49, 201: 1})
1 failed in 8.39s
exit 1
```

Composite slot-claim primary-key removal:

```text
FAILED tests/test_regressions.py::test_database_capacity_foreign_keys_claim_match_and_slot_pk
Failed: DID NOT RAISE <class 'sqlite3.IntegrityError'>
1 failed, 1 warning in 0.84s
exit 1
```

Terminal-cancellation trigger removal:

```text
FAILED tests/test_stage3.py::test_database_blocks_cancelled_claims_revival_and_bad_utc_shapes
Failed: DID NOT RAISE <class 'sqlite3.IntegrityError'>
1 failed, 1 warning in 0.90s
exit 1
```

All mutants exist only in temporary test state or scratch copies; workspace application code remains untouched. No blocking Stage 3 defect remains.
