# Stage 2 Independent Verification Report

## Verdict

**APPROVED**

Stage 2 passes all 44 tests in the required real-process `.venv` suite and the complete Docker suite with external networking disabled. The count includes a byte-for-byte copy of all 26 Stage 1 tests, collected against the Stage 2 application. Three fresh-database acceptance iterations each used four independent Uvicorn OS processes and 50 simultaneous real TCP reservation requests. Every iteration produced exactly one `201`, 49 `409 no_table_available`, zero `500`, zero `503`, and one durable reservation/claim pair.

## Scope

- Specification: `docs/specs/stage-2.md`
- Application inspected: `stage-2/src/`, `stage-2/Dockerfile`, `stage-2/requirements.txt`
- Tests added: `stage-2/tests/conftest.py`, `stage-2/tests/test_multiprocess.py`, `stage-2/tests/test_regressions.py`, `stage-2/tests/test_stage1_regressions.py`
- Application code changed by auditor: none

## Acceptance and attack coverage

- I1/I3: direct duplicate-claim attack, one reservation/claim join, and no grouped overlap after each race.
- I2/I8: database capacity trigger, foreign-key, claim-match, IANA alias, DST gap/fold, opening boundary, unknown-field, and sanitized-error regressions.
- I4: source-order guard proves `BEGIN IMMEDIATE` precedes validation/selection; a controlled 300 ms external write lock forces all contenders to wait.
- I5: each of three iterations asserts the exact response multiset `1 x 201 + 49 x 409`; every conflict body is exact and every client has an explicit timeout.
- I6: four distinct live PIDs receive one absolute `DATABASE_PATH`; all four servers observe the committed claim over real HTTP.
- I7: smallest fitting table and lexical tie-breaking remain deterministic.
- Restart/cancellation: iteration zero kills and restarts one server after setup. Cross-platform cleanup kills the entire process tree, reaps every root process, then polls every allocated port until closed with a 10-second bound on both success and assertion failure.
- Transport: each iteration allocates four distinct free ephemeral ports on numeric `127.0.0.1`; no fixed port is reused across iterations, and no TestClient or ASGI transport participates in the contention test.
- Full regression inheritance: `test_stage1_regressions.py` is hash-identical to the 26-test Stage 1 suite and imports only Stage 2's `src` package through the Stage 2 test path.

## Commands and real results

Five consecutive exact Windows virtual-environment suites:

```text
.venv\Scripts\python -m pytest stage-2/tests -q
............................................ [100%]
run 1: 44 passed, 1 warning in 72.31s; exit 0
run 2: 44 passed, 1 warning in 71.04s; exit 0
run 3: 44 passed, 1 warning in 68.78s; exit 0
run 4: 44 passed, 1 warning in 71.84s; exit 0
run 5: 44 passed, 1 warning in 70.68s; exit 0
```

Image build:

```text
docker build -t tablekeeper-stage2-stable-teardown:latest stage-2
exit 0
```

Required isolated suite:

```text
docker run --rm --network none tablekeeper-stage2-stable-teardown:latest python -m pytest -q
............................................ [100%]
44 passed, 1 warning in 51.12s
exit 0
```

The single warning in both suites is Starlette's library-level deprecation warning for the `anyio.abc.BlockingPortal` alias.

## Protection-removal sensitivity

`BEGIN DEFERRED` scratch mutant:

```text
docker run --rm --network none -e AUDIT_MUTANT=begin_deferred tablekeeper-stage2-audit:latest python -m pytest -q "tests/test_multiprocess.py::test_four_process_exact_contention[0]"
FAILED tests/test_multiprocess.py::test_four_process_exact_contention[0]
observed Counter({503: 50}); expected Counter({409: 49, 201: 1})
1 failed in 5.01s
exit 1
```

The test creates the mutant only in its temporary directory; workspace application code is untouched.

Composite slot-claim primary-key removal:

```text
docker run --rm --network none -e AUDIT_MUTANT=remove_slot_claim_pk tablekeeper-stage2-audit:latest python -m pytest -q tests/test_regressions.py::test_database_capacity_foreign_keys_claim_match_and_slot_pk
FAILED tests/test_regressions.py::test_database_capacity_foreign_keys_claim_match_and_slot_pk
Failed: DID NOT RAISE <class 'sqlite3.IntegrityError'>
1 failed, 1 warning in 0.46s
exit 1
```

Both key protection removals are therefore detected by the suite. No blocking defect remains in the Stage 2 scope.
