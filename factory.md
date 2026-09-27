# Dark Factory Workflow

This repository was built by three Codex agents connected through Band Desktop. The room is the coordination surface: briefs, handoffs, blockers, and verdicts are short messages, while durable evidence lives in workspace files.

## Roles and write permissions

| Agent | Purpose | May write | Must not write |
|---|---|---|---|
| Architect | Convert briefs into testable contracts and resolve specification ambiguity | `docs/specs/stage-N.md`; owner-requested governance documentation | Application source, dependencies, packaging, tests, or audit reports |
| Builder | Implement the current specification | `stage-N/src/`, `stage-N/Dockerfile`, `stage-N/requirements.txt` | Tests, audit reports, or specifications |
| Auditor | Independently attack and verify the implementation | `stage-N/tests/`, `docs/reports/stage-N.md` | Application source, dependencies, packaging, or specifications |

The detailed generic mandates are in `mandates/architect.md`, `mandates/builder.md`, and `mandates/auditor.md`. Direct owner requests may authorize a specific governance-document update, but they do not blur production/test ownership.

## Handoff flow

```text
Brief
  -> Architect: write the specification and attack list
  -> Builder: implement only the specified production artifacts
  -> Auditor: create independent tests, run required commands, publish verdict
       -> APPROVED: stage is complete
       -> BLOCKED: return evidence to Architect
            -> Architect: identify root cause, clarify the contract if needed,
               and give Builder one focused fix
            -> Builder: apply the fix without touching tests
            -> Auditor: rerun independently and issue a new verdict
```

Every handoff names one next agent, includes exact file paths, and summarizes the behavior in a few lines. No agent claims a command ran without running it. Builder never approves its own work; Auditor never repairs production code; Architect never implements the application or tests.

## What each artifact proves

- A specification defines invariants, schema, interfaces, status codes, concurrency behavior, and attacks before implementation.
- Production artifacts show only what Builder changed, not whether it is correct.
- Tests and reports provide independent evidence, including exact commands, outcomes, and protection-removal sensitivity.
- An `APPROVED` verdict means required evidence passed. A `BLOCKED` verdict names the observed mismatch and sends one focused issue back through the loop.

## Real BLOCKED -> fix -> APPROVED example

Stage 1 exposed the value of the separation of duties:

1. **Auditor blocked the stage.** The network-isolated Docker suite returned `1 failed, 24 passed`. An injected non-uniqueness claim failure correctly rolled back both database rows, but the API returned `409 no_table_available` instead of sanitized `500 internal_error`.
2. **Architect found the contract ambiguity.** `docs/specs/stage-1.md` had said a uniqueness violation returned `409`, which was broad enough to encourage catching every `sqlite3.IntegrityError`. Architect clarified that only a proven composite slot-claim primary-key race maps to `409`; triggers, foreign-key failures, unrelated uniqueness failures, and other integrity failures map to sanitized `500`.
3. **Builder made one focused fix.** The implementation narrowed error classification while preserving the already-correct transaction rollback.
4. **Auditor retested independently.** The same injected failure returned sanitized `500`, left zero booking and claim rows, and the Docker suite passed `25/25`. A later analogous table-error regression brought the maintained Stage 1 suite to `26/26`, as recorded in `docs/reports/stage-1.md`.

The loop did not change tests to fit the code, and it did not weaken the invariant. It converted a broad implementation shortcut into a precise contract, a minimal fix, and repeatable evidence.
