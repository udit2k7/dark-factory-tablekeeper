# TableKeeper

TableKeeper is a staged FastAPI service that demonstrates database-enforced booking correctness under real multi-process contention. It uses Python 3.11 and SQLite in WAL mode. The project progresses from a core service to four-process contention, durable idempotency and cancellation, then a packaged mixed-operation stress harness.

## Stages

| Stage | Focus |
|---|---|
| `stage-1` | Core API, tables, availability, atomic booking, and database constraints |
| `stage-2` | Four independent Uvicorn processes sharing one SQLite file; exact contention outcome |
| `stage-3` | Idempotency keys, atomic cancellation, IANA-zone conversion, DST rejection, and UTC persistence |
| `stage-4` | Final package, inherited regressions, 240-operation real-HTTP stress harness, database audit, and mutation mode |

Specifications are in `docs/specs/`; independent verification reports are in `docs/reports/`.

## Core invariants

1. **I1 - No double booking.** An active table/UTC-slot tuple can have at most one claim. SQLite enforces this with the composite primary key on `reservation_slot_claims(restaurant_id, table_id, slot_start_utc)`.
2. **I2 - Capacity.** A party must be positive and cannot exceed its assigned table's seats. SQLite insert and update triggers enforce the cross-table rule.
3. **I3 - Atomic writes.** Selection begins after `BEGIN IMMEDIATE`; the booking row, slot claim, and keyed response commit together or all roll back. All server processes share one database file.
4. **I4 - Idempotent creation.** The same `Idempotency-Key` plus canonical request returns the original byte-identical `201` response and never creates a second booking. Reusing the key for different input returns `422` without changing state.
5. **I5 - Idempotent cancellation.** The first cancellation marks the booking cancelled and deletes its claims in one transaction. Repeated or concurrent cancellation returns the original `200` body and timestamp.
6. **I6 - Correct time semantics.** Clients submit local wall time in the configured IANA zone; DST gaps and folds are rejected. Opening hours are checked locally, while stored instants use canonical UTC `YYYY-MM-DDTHH:MM:SSZ`, including UTC-midnight crossings.

## How double booking is prevented

Correctness does not depend on an application-level availability check. Each occupied 15-minute interval is represented by a row in `reservation_slot_claims`. Its composite primary key makes a second claim for the same restaurant, table, and UTC slot impossible even if application logic changes or separate processes race.

The booking transaction follows this order:

1. Open a request-scoped SQLite connection and execute `BEGIN IMMEDIATE`.
2. Check availability and select the smallest fitting free table inside that transaction.
3. Insert the booking and its slot claim on the same connection.
4. Commit before returning `201`; roll back both rows on any failure.

`BEGIN IMMEDIATE` serializes competing writers before selection. The slot-claim primary key remains the final database guard. WAL supports concurrent readers, `busy_timeout` lets local contenders wait, foreign keys prevent orphan relationships, and cancellation deletes claims in the same transaction that records the cancelled state.

The Stage 4 mutation mode removes only the slot primary key in a temporary database, injects an overlap, and proves the unchanged audit detects it.

## Prerequisites

- Python 3.11
- A platform toolchain capable of creating a Python virtual environment
- Docker Desktop or Docker Engine only for container verification

Run commands from the repository root. Each stage has its own requirements file and test directory.

## Windows PowerShell

Create the virtual environment once:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
```

Install and test every stage:

```powershell
.venv\Scripts\python -m pip install -r stage-1\requirements.txt
.venv\Scripts\python -m pytest stage-1\tests -q

.venv\Scripts\python -m pip install -r stage-2\requirements.txt
.venv\Scripts\python -m pytest stage-2\tests -q

.venv\Scripts\python -m pip install -r stage-3\requirements.txt
.venv\Scripts\python -m pytest stage-3\tests -q

.venv\Scripts\python -m pip install -r stage-4\requirements.txt
.venv\Scripts\python -m pytest stage-4\tests -q
.venv\Scripts\python stage-4\tests\stress_harness.py
```

Run one stage as a service; change `stage-1` to `stage-2`, `stage-3`, or `stage-4` as needed:

```powershell
$stage = "stage-1"
$env:DATABASE_PATH = Join-Path (Get-Location) "$stage\tablekeeper.db"
.venv\Scripts\python -m uvicorn src.main:app --app-dir $stage --host 127.0.0.1 --port 8000
```

## macOS

Create the environment, then install and test each stage:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip

.venv/bin/python -m pip install -r stage-1/requirements.txt
.venv/bin/python -m pytest stage-1/tests -q
.venv/bin/python -m pip install -r stage-2/requirements.txt
.venv/bin/python -m pytest stage-2/tests -q
.venv/bin/python -m pip install -r stage-3/requirements.txt
.venv/bin/python -m pytest stage-3/tests -q
.venv/bin/python -m pip install -r stage-4/requirements.txt
.venv/bin/python -m pytest stage-4/tests -q
.venv/bin/python stage-4/tests/stress_harness.py
```

Run any stage by changing `stage-1` below:

```bash
stage=stage-1
DATABASE_PATH="$(pwd)/$stage/tablekeeper.db" .venv/bin/python -m uvicorn src.main:app --app-dir "$stage" --host 127.0.0.1 --port 8000
```

## Linux

The Linux commands are the same as macOS:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip

.venv/bin/python -m pip install -r stage-1/requirements.txt
.venv/bin/python -m pytest stage-1/tests -q
.venv/bin/python -m pip install -r stage-2/requirements.txt
.venv/bin/python -m pytest stage-2/tests -q
.venv/bin/python -m pip install -r stage-3/requirements.txt
.venv/bin/python -m pytest stage-3/tests -q
.venv/bin/python -m pip install -r stage-4/requirements.txt
.venv/bin/python -m pytest stage-4/tests -q
.venv/bin/python stage-4/tests/stress_harness.py
```

Run any stage by changing `stage-1` below:

```bash
stage=stage-1
DATABASE_PATH="$(pwd)/$stage/tablekeeper.db" .venv/bin/python -m uvicorn src.main:app --app-dir "$stage" --host 127.0.0.1 --port 8000
```

## Docker verification

Build and test a stage by replacing `4` with `1`, `2`, or `3`. The final package also runs its harness without external networking:

```text
docker build -t tablekeeper-stage-4 stage-4
docker run --rm --network none tablekeeper-stage-4 python -m pytest -q
docker run --rm --network none tablekeeper-stage-4 python tests/stress_harness.py
```

Mutation mode is expected to print a detected double booking and exit `2`:

```text
docker run --rm --network none tablekeeper-stage-4 python tests/stress_harness.py --mutation-disable-slot-pk
```

## Verified results

Independent reports record these approved full-suite baselines:

| Stage | Windows `.venv` | Docker `--network none` |
|---|---:|---:|
| 1 | 26 passed in 8.14s | 26 passed in 7.81s |
| 2 | 44 passed; five consecutive runs | 44 passed in 51.12s |
| 3 | 72 passed; three consecutive runs | 72 passed in 86.27s |
| 4 | 74 passed in 108.40s | 74 passed in 62.25s |

The final fresh Windows rerun on 2026-09-27 produced:

| Command | Result | Exit |
|---|---|---:|
| Normal stress harness | 240 attempts, 181 successes, 59 expected conflicts, 0 errors; all four database audits `0` | 0 |
| Slot-PK mutation harness | Same HTTP accounting; `double_bookings: 1`, other audits `0` | 2 (expected detection) |
| Focused harness tests | 2 passed in 10.13s | 0 |

The single suite warning reported throughout is a Starlette library deprecation warning for `anyio.abc.BlockingPortal`.
