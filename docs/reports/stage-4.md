# Stage 4 Independent Verification Report

## Verdict

**APPROVED**

Stage 4 carries all 72 Stage 3 regressions and adds nine harness/attack-verification tests. The expanded 81-test suite passes on Windows and in Docker with external networking disabled. The standalone mixed-operation harness passes in both environments, and isolated mutation modes detect removed protections. The auditor changed only `stage-4/tests/` and this report; application code was not modified.

## Coverage

- Carried regressions: all Stage 3 endpoint, database, capacity, no-double-booking, idempotency, cancellation, UTC/DST, rollback, and four-process tests.
- Real topology: four distinct `sys.executable -m uvicorn` processes, one absolute SQLite path, four numeric-loopback ephemeral ports, real TCP only, and no shell or worker manager.
- Mixed load: 240 requests released as one concurrent workload: 60 bookings, 50 identical keyed retries, 40 cancellations, 40 rebooks, and 50 availability reads. Every port receives traffic.
- Replay checks: keyed retries must match their stored `201` bytes; four concurrent cancellations per seeded reservation must return one byte-identical `200` body.
- Cleanup: bounded startup/workload deadlines, `try/finally`, portable `terminate`/`wait`/`kill`/`wait`, closed log/client handles, 10-second closed-port polling, and bounded retry of temporary-directory removal for Windows handle-release latency.
- Audit: independently queried duplicate active claims, over-capacity reservations, duplicate idempotency relationships, and claims left on cancelled reservations.
- Mutation: a temporary claim table is rebuilt without only the composite slot PK; a controlled overlapping-claim probe is accepted, the unchanged audit reports a double booking, and the process exits `2`. Production source/schema files are untouched.

## Commands and results

Windows full regression and harness suite:

```text
.venv\Scripts\python -m pytest stage-4/tests -q
74 passed, 1 warning in 108.40s (0:01:48)
exit 0
```

Windows normal harness:

```text
.venv\Scripts\python stage-4/tests/stress_harness.py
{"attempts":240,"conflicts":59,"database_audit":{"double_bookings":0,"duplicate_idempotent_bookings":0,"leftover_slots_from_cancelled_bookings":0,"over_capacity":0},"errors":0,"operation_counts":{"availability":50,"booking":60,"cancel":40,"rebook":40,"retry":50},"process_count":4,"seed":404,"successes":181}
exit 0
```

Windows mutation harness:

```text
.venv\Scripts\python stage-4/tests/stress_harness.py --mutation-disable-slot-pk
{"attempts":240,"conflicts":59,"database_audit":{"double_bookings":1,"duplicate_idempotent_bookings":0,"leftover_slots_from_cancelled_bookings":0,"over_capacity":0},"errors":0,"operation_counts":{"availability":50,"booking":60,"cancel":40,"rebook":40,"retry":50},"process_count":4,"seed":404,"successes":181}
exit 2
```

Image build and isolated full suite:

```text
docker build -t tablekeeper-stage-4:latest stage-4
exit 0

docker run --rm --network none tablekeeper-stage-4:latest python -m pytest -q
74 passed, 1 warning in 62.25s (0:01:02)
exit 0
```

Docker normal and mutation harnesses:

```text
docker run --rm --network none tablekeeper-stage-4:latest python tests/stress_harness.py
{"attempts":240,"conflicts":59,"database_audit":{"double_bookings":0,"duplicate_idempotent_bookings":0,"leftover_slots_from_cancelled_bookings":0,"over_capacity":0},"errors":0,"operation_counts":{"availability":50,"booking":60,"cancel":40,"rebook":40,"retry":50},"process_count":4,"seed":404,"successes":181}
exit 0

docker run --rm --network none tablekeeper-stage-4:latest python tests/stress_harness.py --mutation-disable-slot-pk
{"attempts":240,"conflicts":59,"database_audit":{"double_bookings":1,"duplicate_idempotent_bookings":0,"leftover_slots_from_cancelled_bookings":0,"over_capacity":0},"errors":0,"operation_counts":{"availability":50,"booking":60,"cancel":40,"rebook":40,"retry":50},"process_count":4,"seed":404,"successes":181}
exit 2
```

The sole warning is Starlette's library-level deprecation warning for `anyio.abc.BlockingPortal`. No Stage 4 blocker remains.

## Fresh Windows rerun (2026-09-27)

The first requested rerun exposed `WinError 32` while removing a Uvicorn log after otherwise completed work. The auditor corrected only `stage-4/tests/stress_harness.py` by adding the specification-required bounded Windows cleanup retry. Fresh normal and mutation commands then produced:

```text
.venv\Scripts\python stage-4/tests/stress_harness.py
{"attempts":240,"conflicts":59,"database_audit":{"double_bookings":0,"duplicate_idempotent_bookings":0,"leftover_slots_from_cancelled_bookings":0,"over_capacity":0},"errors":0,"operation_counts":{"availability":50,"booking":60,"cancel":40,"rebook":40,"retry":50},"process_count":4,"seed":404,"successes":181}
exit 0

.venv\Scripts\python stage-4/tests/stress_harness.py --mutation-disable-slot-pk
{"attempts":240,"conflicts":59,"database_audit":{"double_bookings":1,"duplicate_idempotent_bookings":0,"leftover_slots_from_cancelled_bookings":0,"over_capacity":0},"errors":0,"operation_counts":{"availability":50,"booking":60,"cancel":40,"rebook":40,"retry":50},"process_count":4,"seed":404,"successes":181}
exit 2

.venv\Scripts\python -m pytest stage-4/tests/test_stress_harness.py -q
2 passed in 10.13s
exit 0
```

## Attack-list gap closure (2026-09-27)

### #4 — multiple seeds and scheduling jitter

The seed now deterministically shuffles the 240-operation list and adds bounded per-request release jitter. Three normal real-HTTP runs used four Uvicorn processes and ephemeral ports:

| Seed | Attempts | Successes | Conflicts | Errors | All four audits |
|---:|---:|---:|---:|---:|---|
| 7 | 240 | 181 | 59 | 0 | zero |
| 99 | 240 | 181 | 59 | 0 | zero |
| 404 | 240 | 181 | 59 | 0 | zero |

Each command exited `0`. Seeds 7 and 99 are permanent parametrized regressions in `test_attack_list.py`.

### #8 — cleanup attacks

Three deterministic Windows tests exercise distinct teardown paths:

- Invalid ASGI target forces startup failure; every spawned process is reaped, all log handles close, and all four ephemeral ports close.
- A deliberately hanging lifespan forces the bounded startup timeout; the same process/handle/port assertions pass.
- A `KeyboardInterrupt` raised inside a live four-process cluster propagates only after teardown; all process/handle/port assertions pass.

### #10 — additional isolated mutants

- Keyed-response transaction mutant: a scratch source copy commits the reservation and claim immediately before inserting the idempotency record. `test_idempotency_insert_failure_rolls_back_all_three_rows` fails both `BEFORE` and `AFTER` cases (`2 failed`), proving the regression kills the mutant.
- Cancellation claim-deletion mutant: a scratch source copy replaces claim deletion with a no-op. `test_cancel_releases_claim_replays_exactly_and_creation_replay_does_not_reclaim` fails (`1 failed`), proving the regression kills the mutant.
- Mutants are created only under pytest temporary directories; production source and schema are unchanged.

### Expanded-suite results

```text
.venv\Scripts\python -m pytest stage-4/tests -q
81 passed, 1 warning in 142.82s (0:02:22)
exit 0

docker build -t tablekeeper-stage-4:latest stage-4
exit 0

docker run --rm --network none tablekeeper-stage-4:latest python -m pytest -q
81 passed, 1 warning in 91.91s (0:01:31)
exit 0
```

## Not covered

None for the three requested attack-list gaps. Tests use a deterministic raised `KeyboardInterrupt` rather than platform-specific console signal injection; it exercises the harness's actual `BaseException` teardown path without relying on Windows console attachment state.
