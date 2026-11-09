# IMP-003 - Plan-level orchestration above objectives

Status: deferred
Priority: P2
Component: task contracts / scheduling / persistence / CLI
Target: when measured objective handoff overhead justifies another abstraction
Depends on: demonstrate objective-by-objective submission is a material bottleneck
Source: migrated IMP-003 proposal and the existing [task contract](../docs/task-contract.md)

## Problem

The orchestrator durably executes one objective's task DAG, but a larger delivery plan still requires a human or initiating agent to submit successive dependent objectives and hand off artifacts. This may interrupt unattended workflows. There is not yet evidence that the overhead justifies another abstraction.

## Proposed change

1. Extend the model to plan to objective DAG to task DAG. A plan stores goals, objective dependencies, status and acceptance; objectives retain the existing task contract and scheduler.
2. Advance an objective only after prerequisites are validated. Persist plan status, artifact/context handoffs, blockers and decision checkpoints.
3. Recover plan-level progress after restarts, quota pauses, failed objectives and rechecks without resubmitting successful objectives or duplicate side effects.
4. Reuse existing SQLite, scheduling, session recovery and single-worker operation. Design an additive schema and minimal CLI operations before implementation. Do not create a second supervisor.
5. Keep dynamic replanning optional and human choice points explicit.

## Impacted files

- [database.py](../src/agent_orchestrator/database.py): durable plans and objective-dependency state.
- [scheduler.py](../src/agent_orchestrator/scheduler.py): plan-scoped eligibility, validation and recovery.
- [cli.py](../src/agent_orchestrator/cli.py): plan controls and handoff references.
- [prompts.py](../src/agent_orchestrator/prompts.py) and [tests](../tests/): context and acceptance/recovery coverage.
- [README](../README.md) and [task contract](../docs/task-contract.md): interface documentation.

## Acceptance criteria

- [ ] A plan with at least two dependent objectives completes in order and hands off validated artifacts.
- [ ] Restart, quota wait and recoverable failure resume the existing plan without duplicate objectives.
- [ ] Blockers and explicit human decision points prevent unsafe automatic advancement.
- [ ] Existing standalone objective execution, database migrations and single-worker quota policy remain compatible.

## Verification

Use deterministic fake backends and SQLite restart tests for dependency ordering, partial completion, artifact handoff, duplicate-submission prevention, quota waits, and blocked/rechecked objectives. Execute the existing orchestrator regression suites using [verification commands](../README.md#verification).

## Notes and boundaries

Preserves the migrated proposal's ID, deferred status, priority and acceptance scope. Recording it does not authorize implementation. Revisit only after real operations demonstrate material handoff overhead; no consumer milestone is a prerequisite. Parallel execution remains outside this proposal and the current sequential model is unchanged.
