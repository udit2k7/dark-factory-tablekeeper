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

## Real feedback cycles from this project

### 1. Stage 1 booking error over-mapping: BLOCKED -> APPROVED

Auditor's network-isolated suite returned `1 failed, 24 passed`. An injected non-uniqueness claim failure rolled back correctly but was mapped to `409 no_table_available` instead of sanitized `500 internal_error`. Architect clarified that only a proven composite slot-claim primary-key collision is a `409`. Builder narrowed the `sqlite3.IntegrityError` mapping without touching tests. Auditor reran the same attack, observed zero partial rows and the correct `500`, and approved the stage.

### 2. Stage 1 table-creation mismatch found by Architect

During a spec-to-code comparison, Architect found the same broad catch pattern in table creation: every integrity failure became `409 table_already_exists`, although the contract reserved `409` for the table composite-key conflict. Builder added precise conflict recognition. Auditor added an unrelated-trigger regression and confirmed sanitized `500`, no leaked trigger text, zero table rows, and a fully passing 26-test suite.

### 3. Human-found Windows port-leak flake in Stage 2

After an apparently successful audit, the human found an intermittent Windows port-leak flake. The suspected root cause was teardown timing around reused ports and child processes. The harness moved to OS-assigned ephemeral ports, terminated the complete Uvicorn process tree, waited for children, and polled ports until closed. Five consecutive Windows suites passed `44/44` after the fix, confirming stable behavior, and the same suite passed in network-isolated Docker. This cycle shows that human observation remains part of the factory's evidence loop.

### 4. Missing Stage 1 regressions in Stage 2

Stage 2 initially focused on the new four-process race but did not carry the complete Stage 1 contract forward. The human spotted the test-count drop from 26 Stage 1 tests to only 18 Stage 2 tests. The gap was corrected by copying the Stage 1 regression suite byte-for-byte and collecting it against Stage 2's application. The Stage 2 report verifies hash-identical inheritance and a final total of 44 passing tests, so new concurrency work could not silently regress earlier behavior.

### 5. Refusal to fabricate a nonexistent blocker

The human later asked the agents to fix the latest `BLOCKED` report. Architect inspected every `docs/reports/` verdict, report history, and the clean reports/specs worktree; Auditor independently checked the same files. The newest report and all earlier reports were `APPROVED`. No spec was changed and no Builder fix was invented. The agents reported the contradictory premise and requested an exact artifact path instead.

Across these cycles, the rule stayed constant: preserve failing evidence, fix the narrow root cause, rerun independently, and never manufacture work merely to satisfy the shape of a request.
