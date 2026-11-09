# IMP-002 - Git task attribution and regression traceability

Status: proposed
Priority: P0
Component: checkpoints / scheduling / persistence / validation
Target: next assigned provenance objective, after verifying current checks accept legitimate changes
Depends on: assess any active historical verification coupling before scheduling; no missing local improvement dependency
Source: migrated IMP-002 proposal and the existing [checkpoint implementation](../src/agent_orchestrator/executor.py)

## Problem

The orchestrator captures revision, Git status/diff, dirty files and workspace hashes at checkpoints, but does not maintain an explicit, reliable task to commits to validator-result history. When several agent turns modify a shared worktree, identifying the task that introduced a regression requires manual reconstruction.

## Proposed change

1. Define a task provenance record linking objective ID, task key, attempt/turn ID, starting and finishing HEAD, tracked/untracked changes, commit list (if any), and validator command/results. Persist it in existing SQLite events/artifacts and expose it in `status --json`.
2. Detect commits created during a task with Git ancestry/range checks. Attribute them only when ownership is unambiguous; record a dirty or externally modified worktree as unattributed, never fabricate a task commit.
3. Establish focused task-linked commits with objective/task IDs and validator evidence. Prefer observing commits before introducing automated commit creation.
4. Any later auto-commit proposal requires explicit opt-in, a known initial worktree, scoped staging, sensitive-file exclusions, and safe interrupted/failed-task handling. Do not auto-commit by default.
5. Keep non-Git workspaces functional with an explicit `git_unavailable` provenance state.

## Impacted files

- [executor.py](../src/agent_orchestrator/executor.py): Git checkpoint/provenance capture.
- [scheduler.py](../src/agent_orchestrator/scheduler.py): correlate task/turn lifecycle, commits and validation.
- [database.py](../src/agent_orchestrator/database.py): additive durable state and reporting.
- [validation.py](../src/agent_orchestrator/validation.py): associate validator results with task revisions.
- [tests](../tests/): deterministic Git fixtures, restart and recovery coverage.
- [README](../README.md) and [agent instructions](../AGENTS.md): commit conventions and troubleshooting.

## Acceptance criteria

- [ ] Each completed task has a traceable start/end revision and associated validator results; missing commits are explicitly reported.
- [ ] A regression maps to the first task-owned commit or is correctly marked unattributed when edits overlap.
- [ ] Restart/recheck/quota recovery retains task provenance without duplicate attribution or lost validators.
- [ ] Existing tasks/databases migrate additively; non-Git workspaces still execute.
- [ ] No unrequested commits, staging, history rewrites, or uploads occur.

## Verification

Use temporary Git repositories for zero, one and multiple commits; dirty initial state; concurrent external commit; failed/retried tasks; and restart recovery. Run `.venv/Scripts/python -m pytest tests -q -m "not live"` and record test-to-revision attribution.

## Notes and boundaries

Preserves the migrated proposal's ID, proposed status, priority and acceptance scope; no feature is implemented by recording it. The original prerequisite concerned consumer-owned historical snapshot gates. Check the current standalone gates locally before scheduling; do not require another repository's plan or implement its cleanup implicitly. Attribution does not replace validators or evidence hashes. Multi-worker isolation, PR orchestration and mandatory auto-commit are outside scope.
