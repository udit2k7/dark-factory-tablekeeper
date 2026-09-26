# Stage 1 Independent Verification Report

## Verdict

**APPROVED**

The current workspace builds successfully and all 26 contract tests pass under Docker with networking disabled. Both HTTP error-classification defects are fixed: only proven table/slot uniqueness conflicts map to `409`; injected unrelated integrity failures roll back and return sanitized `500 internal_error`.

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
docker build -t tablekeeper-stage1-table-fix:latest stage-1
exit 0
```

Required isolated suite:

```text
docker run --rm --network none tablekeeper-stage1-table-fix:latest python -m pytest -q
.......................... [100%]
26 passed, 1 warning in 7.81s
exit 0
```

The failure-injection test verifies both database row counts remain zero and the API returns the required sanitized `500`.

Exact local virtual-environment suite:

```text
.venv\Scripts\python -m pytest stage-1/tests -q
.......................... [100%]
26 passed, 1 warning in 8.14s
exit 0
```

Protection-removal sensitivity run:

```text
docker run --rm --network none -e AUDIT_MUTANT=drop_capacity_insert tablekeeper-stage1-completion:latest python -m pytest -q tests/test_stage1.py::test_database_rejects_capacity_insert_and_update
FAILED tests/test_stage1.py::test_database_rejects_capacity_insert_and_update
Failed: DID NOT RAISE <class 'sqlite3.IntegrityError'>
1 failed, 1 warning in 0.45s
exit 1
```

This mutation probe drops only the capacity insert trigger in the temporary test database. The invariant test fails immediately, demonstrating that it detects removal of the database protection.

Capacity-update trigger removal sensitivity run:

```text
docker run --rm --network none -e AUDIT_MUTANT=drop_capacity_update tablekeeper-stage1-update-mutation:latest python -m pytest -q tests/test_stage1.py::test_database_rejects_capacity_insert_and_update
FAILED tests/test_stage1.py::test_database_rejects_capacity_insert_and_update
Failed: DID NOT RAISE <class 'sqlite3.IntegrityError'>
1 failed, 1 warning in 0.44s
exit 1
```

The control run of the same focused test with the trigger intact passed (`1 passed, 1 warning in 0.51s`). The scratch mutation drops only `reservations_capacity_update`; an oversized update then succeeds and the guarding assertion fails.

Slot-claim primary-key removal sensitivity run:

```text
docker run --rm --network none -e AUDIT_MUTANT=remove_slot_claim_pk tablekeeper-stage1-completion:latest python -m pytest -q tests/test_stage1.py::test_database_rejects_duplicate_mismatched_and_orphan_claims
FAILED tests/test_stage1.py::test_database_rejects_duplicate_mismatched_and_orphan_claims
Failed: DID NOT RAISE <class 'sqlite3.IntegrityError'>
1 failed, 1 warning in 0.46s
exit 1
```

This mutation removes only `PRIMARY KEY (restaurant_id, table_id, slot_start_utc)` from the temporary test schema. The duplicate-claim invariant test fails immediately.

## Resolved defect

Reservation creation distinguishes the composite slot-claim uniqueness error from other `sqlite3.IntegrityError` instances. Table creation likewise maps only the composite restaurant/table uniqueness conflict to `409`; an injected trigger failure returns sanitized `500` and leaves no table row. Both targeted regressions and the complete isolated suite pass; no blocking defect remains in the Stage 1 scope.
