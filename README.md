# TableKeeper

TableKeeper is a staged FastAPI service that demonstrates database-enforced booking correctness under real multi-process contention. It uses Python 3.11 and SQLite in WAL mode. The project progresses from a core service to four-process contention, durable idempotency and cancellation, then a packaged mixed-operation stress harness.

## How this was built

Three Codex agents collaborated in Band Desktop with separated duties: Architect wrote testable specifications, Builder wrote production artifacts, and Auditor wrote independent tests and reports. The handoff and feedback model is documented in [`factory.md`](factory.md). TableKeeper is released under the [MIT License](LICENSE).

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

Capacity is also enforced in SQLite rather than trusted to application checks: insert and update triggers reject any booking whose party size exceeds the selected table's seats. Together, the write-locking transaction, slot-claim composite primary key, and capacity triggers protect the two central allocation invariants at the database boundary.

The Stage 4 mutation mode is an audit-sensitivity proof, not a race result. A controlled probe rebuilds a temporary claim table without the slot primary key and inserts two overlapping claims. The mutant accepts them, while the production schema rejects the second claim. The unchanged audit then reports `double_bookings=1` and exits `2`.

## Prerequisites

- Python 3.11+ (verified locally on 3.12.10; Docker images use 3.11)
- A platform toolchain capable of creating a Python virtual environment
- Docker Desktop or Docker Engine only for container verification

Run commands from the repository root. Each stage has its own requirements file and test directory.

## Windows PowerShell

Create the virtual environment once:

```powershell
py -3 -m venv .venv
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

Docker commands are the same on all three platforms. On Windows and macOS, start Docker Desktop first. On Linux, start Docker Engine and ensure the current user can access the daemon.

### Windows PowerShell

Build and test every stage with external networking disabled:

```powershell
docker build -t tablekeeper-stage-1 stage-1
docker run --rm --network none tablekeeper-stage-1 python -m pytest -q
docker build -t tablekeeper-stage-2 stage-2
docker run --rm --network none tablekeeper-stage-2 python -m pytest -q
docker build -t tablekeeper-stage-3 stage-3
docker run --rm --network none tablekeeper-stage-3 python -m pytest -q
docker build -t tablekeeper-stage-4 stage-4
docker run --rm --network none tablekeeper-stage-4 python -m pytest -q
docker run --rm --network none tablekeeper-stage-4 python tests/stress_harness.py
```

### macOS and Linux

Run the equivalent commands in `bash` or another POSIX shell:

```bash
docker build -t tablekeeper-stage-1 stage-1
docker run --rm --network none tablekeeper-stage-1 python -m pytest -q
docker build -t tablekeeper-stage-2 stage-2
docker run --rm --network none tablekeeper-stage-2 python -m pytest -q
docker build -t tablekeeper-stage-3 stage-3
docker run --rm --network none tablekeeper-stage-3 python -m pytest -q
docker build -t tablekeeper-stage-4 stage-4
docker run --rm --network none tablekeeper-stage-4 python -m pytest -q
docker run --rm --network none tablekeeper-stage-4 python tests/stress_harness.py
```

Mutation mode is expected to print a detected double booking and exit `2`:

```text
docker run --rm --network none tablekeeper-stage-4 python tests/stress_harness.py --mutation-disable-slot-pk
```

## Verified results

Independent fresh runs record these approved full-suite totals:

| Stage | Passed | Failed | Additional evidence |
|---|---:|---:|---|
| 1 | 26 | 0 | Windows `.venv` and Docker `--network none` |
| 2 | 44 | 0 | Five consecutive stable Windows runs plus Docker |
| 3 | 72 | 0 | Three consecutive Windows runs plus Docker |
| 4 | 74 | 0 | Windows `.venv` and Docker `--network none` |

The final stress evidence is:

| Run | Command suffix | Attempts | Successes | Conflicts | Errors | Database audit | Exit |
|---|---|---:|---:|---:|---:|---|---:|
| Human, seed 404 | default | 240 | 180 | 60 | 0 | All four violation counts `0` | 0 |
| Auditor, seed 404 | default | 240 | 181 | 59 | 0 | All four violation counts `0` | 0 |
| Auditor, seed 7 | `--seed 7` | 240 | 181 | 59 | 0 | All four violation counts `0` | 0 |
| Slot-PK mutation | `--mutation-disable-slot-pk` | 240 | timing-dependent | timing-dependent | 0 | `double_bookings: 1`; other counts `0` | 2 (expected detection) |

Normal runs produce about 180-181 successes and 59-60 expected conflicts depending on race timing. That split is observational, not a guarantee. The guaranteed acceptance result is `errors=0` and zeros for all four audits: double bookings, over-capacity bookings, duplicate idempotent bookings, and slots left on cancelled bookings.

Mutation mode uses a controlled probe to insert two overlapping claims into a temporary claim table with the slot PK disabled. Production rejects the second claim; the mutant accepts both; the audit reports `double_bookings=1` and exits `2`. This proves audit sensitivity and is not presented as a naturally occurring race outcome. Seed 7 was independently rerun with `.venv\Scripts\python stage-4/tests/stress_harness.py --seed 7`.

The single suite warning reported throughout is a Starlette library deprecation warning for `anyio.abc.BlockingPortal`.
