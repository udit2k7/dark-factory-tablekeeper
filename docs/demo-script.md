# TableKeeper Demo Script - 2:30

## Recording setup

- Record at 1080p or higher with the Band Desktop room, PowerShell, and repository tree already open.
- Use a large terminal font and keep the final JSON line fully visible.
- Run from the repository root with the populated `.venv`.
- If a command takes longer than its allotted screen time, use an obvious jump cut or speed-up; do not imply that the omitted wait did not occur.
- Mutation mode intentionally exits `2`. Present that as successful detection, not a failed demo.

## 0:00-0:45 - Room handoffs and BLOCKED to APPROVED

### 0:00-0:10 - Establish the factory

**On screen:** Show the Band Desktop room with Architect, Builder, and Auditor visible. Briefly highlight `factory.md` in the file panel.

**Say:**

> "This is a three-agent software factory running in Band Desktop. Architect writes the contract, Builder owns production code, and Auditor owns tests and the verdict. Their write permissions stay separate, so no agent can quietly grade its own work."

### 0:10-0:23 - Show the forward handoff

**On screen:** Scroll through one Stage brief, the Architect handoff containing a `docs/specs/stage-N.md` path, then the Builder handoff to Auditor.

**Say:**

> "Each stage starts as a short brief. Architect turns it into numbered invariants, schema, status codes, concurrency rules, and attack cases. Builder implements only that specification, then hands exact paths to Auditor for independent execution."

### 0:23-0:45 - Show the real blocked cycle

**On screen:** Show the Stage 1 `BLOCKED` message, highlight `1 failed, 24 passed`, then the focused fix handoff and the later `APPROVED` message. Open `docs/reports/stage-1.md` on the final verdict.

**Say:**

> "Here is a real correction loop. Auditor found that an injected database failure rolled back correctly but was misclassified as a 409 conflict instead of a sanitized 500. Architect clarified that only a proven slot-key collision is 409. Builder narrowed one error mapping without touching tests. Auditor reran the same attack and approved the stage. The maintained suite now passes 26 tests."

## 0:45-1:45 - Stage 4 stress run and mutation catch

### 0:45-0:57 - Launch the normal harness

**On screen:** Switch to PowerShell at the repository root and run:

```powershell
.venv\Scripts\python stage-4\tests\stress_harness.py
```

**Say:**

> "The final harness starts four real Uvicorn processes on free loopback ports, all sharing one SQLite file, then releases 240 mixed operations at once."

### 0:57-1:17 - Read the clean result

**On screen:** Hold on the final JSON. Zoom or select `attempts`, `successes`, `conflicts`, `errors`, and `database_audit`.

The success/conflict split may display as `180/60` or `181/59`; keep the actual terminal result on screen. The invariant values are `"errors":0` and four zero database-audit counts.

**Say:**

> "The run completed 240 requests: about 180 successes and 60 expected conflicts - the split varies with timing; the zeros never do. We have zero errors, zero double bookings, zero over-capacity bookings, zero duplicate idempotent bookings, and zero claims left behind after cancellation."

### 1:17-1:29 - Explain the database guard

**On screen:** Open the verified DDL file `stage-4/src/database.py`, jump to line 82, and highlight the `reservation_slot_claims` table plus its composite primary key at line 90.

**Say:**

> "The key point is that exclusivity lives in SQLite. `BEGIN IMMEDIATE` serializes selection, while this composite primary key is the final guard across processes."

### 1:29-1:45 - Run and interpret the mutation

**On screen:** Return to PowerShell and run:

```powershell
.venv\Scripts\python stage-4\tests\stress_harness.py --mutation-disable-slot-pk
$LASTEXITCODE
```

Highlight `"double_bookings":1` and exit code `2`.

**Say:**

> "Now the harness rebuilds only a temporary claim table without that primary key. A controlled probe inserts two overlapping claims: production would reject the second, but the mutant accepts both. The audit catches the overlap as one double booking and exits two by design. This proves audit sensitivity; it is not a race result."

## 1:45-2:30 - Repository tour

### 1:45-1:57 - Specs and reports

**On screen:** Show the repository root, expand `docs/specs/` and `docs/reports/`, and select matching Stage 4 files.

**Say:**

> "The repository preserves the reasoning. Every stage has a testable specification and a separate verification report with exact commands, results, and mutation evidence."

### 1:57-2:10 - Production package

**On screen:** Expand `stage-4/src/`, then select `stage-4/Dockerfile` and `stage-4/requirements.txt`.

**Say:**

> "The final production package is small: FastAPI source, the SQLite schema and transactions, pinned dependencies, and a self-contained Docker image that can run with external networking disabled."

### 2:10-2:21 - Tests and stress harness

**On screen:** Expand `stage-4/tests/`; highlight the inherited regression files, `stress_harness.py`, and `test_stress_harness.py`.

**Say:**

> "Stage 4 carries all 72 earlier regressions unchanged, then adds the stress harness and its verification. The full suite passes 74 tests on Windows and in Docker with `--network none`."

### 2:21-2:30 - Close on governance and outcome

**On screen:** Select `README.md`, `factory.md`, and the three files under `mandates/`. End on the clean Stage 4 JSON summary.

**Say:**

> "README covers every platform, while factory and mandate files make ownership explicit. The result is not just a working service; it is a reproducible chain from requirement, to implementation, to independent proof."
