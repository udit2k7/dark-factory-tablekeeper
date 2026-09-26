# Stage 1 Independent Verification Report

## Verdict

**BLOCKED**

The image builds and 24 of 25 contract tests pass under Docker with networking disabled. One HTTP error-classification defect remains: an injected, non-uniqueness SQLite integrity failure between reservation insertion and slot-claim insertion is returned as `409 no_table_available`; the specification requires unexpected storage failures to return `500 internal_error`.

## Scope

- Specification: `docs/specs/stage-1.md`
- Application inspected: `stage-1/src/`, `stage-1/Dockerfile`, `stage-1/requirements.txt`
- Tests added: `stage-1/tests/test_stage1.py`
- Application code changed by auditor: none

## Test coverage

- I1 exclusive claim: eight-way concurrent last-table race plus direct duplicate slot-claim attack.
- I2 capacity: API boundary validation and direct insert/update attacks against the SQLite capacity triggers.
- I3 referential integrity: foreign-key pragma check, orphan table/claim attempts, and cross-restaurant claim mismatch.
- I4 time validity: opening/closing boundaries, closed days, malformed and unaligned values, DST gap, and DST fold.
- I5 deterministic assignment: capacity filtering and ordering by seats then table ID.
- I6 durable atomic creation: injected claim failure verifies both reservation and claim counts remain zero.
- Operational behavior: WAL, foreign keys, busy timeout, sanitized `503 database_busy`, unknown JSON fields, and no external network dependency.

## Commands and real results

Image build:

```text
docker build -t tablekeeper-stage1-audit:latest stage-1
exit 0
```

Required isolated suite:

```text
docker run --rm --network none tablekeeper-stage1-audit:latest python -m pytest -q
......................F.. [100%]
FAILED tests/test_stage1.py::test_claim_failure_rolls_back_reservation
1 failed, 24 passed, 1 warning in 7.50s
exit 1
```

The failing test first verifies that both database row counts are zero, so atomic rollback succeeds. It then observes `409` where the contract requires `500`.

Protection-removal sensitivity run:

```text
docker run --rm --network none -e AUDIT_MUTANT=drop_capacity_insert tablekeeper-stage1-audit:latest python -m pytest -q tests/test_stage1.py::test_database_rejects_capacity_insert_and_update
FAILED tests/test_stage1.py::test_database_rejects_capacity_insert_and_update
Failed: DID NOT RAISE <class 'sqlite3.IntegrityError'>
1 failed, 1 warning in 0.53s
exit 1
```

This mutation probe drops only the capacity insert trigger in the temporary test database. The invariant test fails immediately, demonstrating that it detects removal of the database protection.

## Blocking defect

`stage-1/src/main.py:396` catches every `sqlite3.IntegrityError` raised by either reservation insertion or claim insertion and maps it to `409 no_table_available`. That mapping is broader than the specified uniqueness-conflict case. The injected `forced_claim_failure` trigger therefore produces a false conflict response instead of the required sanitized `500 internal_error`.

Acceptance requires distinguishing slot-claim uniqueness conflicts from other integrity failures, preserving the verified rollback behavior, and rerunning the same network-disabled suite to 25 passed.
