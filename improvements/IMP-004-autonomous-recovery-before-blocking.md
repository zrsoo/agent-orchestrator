# IMP-004 - Assess autonomous recovery before escalating BLOCKED

Status: done (opt-in autonomous objectives; legacy policy unchanged)
Priority: P1
Component: scheduler / recovery / worker contract / persistence / dashboard
Target: implemented following explicit user approval on 2026-11-17
Depends on: none
Source: [scheduler](../src/agent_orchestrator/scheduler.py), [failure handling](../src/agent_orchestrator/recovery.py), and user discussion on 2026-11-16

## Problem

The scheduler immediately accepts a worker's `status: blocked` through
`disrupted(..., blocked=True)`. No recovery assessment establishes that authorized
agent actions are exhausted or that a concrete user action is necessary.
Separately, repeated failure episodes can block when checkpoint file inventories
are unchanged, even when diagnosis has progressed through new evidence.

Observed example: an FC consumer stopped account writes after an ambiguous actual
bid, repaired a native identity-retention defect, then escalated the missing
historical mapping as a user prerequisite. The operator had to request further
investigation despite already authorizing ordinary diagnosis and repair. Stopping
unsafe writes was appropriate; requiring human intervention was not established
merely by the unsuccessful lookup. Consumer reference: task
`obj-da182a175410/003-04`, incident `00304-20261115T222121Z-missing-actual-bid-identity`,
reported in the FC repository at
`docs/research/003/003-04/production-02/incident-identity-01/REPORT.md`.
This is contextual evidence, not a required repository or runtime dependency.

## Proposed change

1. Treat a worker's proposed BLOCKED result as requiring a sequential recovery
   assessment in the existing task/session before terminal escalation. Use
   existing reconciliation/fixing states where possible; no parallel controller.
2. Define versioned structured escalation evidence: failure/unknowns, safe state,
   attempted actions and results, remaining authorized actions, external
   dependency, and exact requested user action when one exists. Preserve support
   for old worker results through a documented compatibility path.
3. If a viable authorized next action exists, continue diagnosis, repair, testing
   and verification automatically. A safe pause in consumer side effects must not
   itself terminate agent investigation. Generic scheduler policy must not import
   consumer-specific trading/reconciliation logic.
4. Accept final BLOCKED only for evidenced missing authorization, unavailable
   human/external prerequisites, or an exhausted safe recovery path. Do not demand
   a fictional user action when the restriction is genuinely external. Explain
   what would change the situation and why the agent cannot make that change.
5. Track diagnostic progress and repeated hypotheses/actions/results in durable
   events, alongside implementation checkpoints. Prevent unbounded repetition of
   the same failed approach; unchanged source files alone do not prove stagnation.
6. Preserve explicit stop/await-user instructions, exhausted authorizations,
   quotas, pacing, command identity and no-replay safeguards. Recovery assessment
   never grants permission to repeat an ambiguous write or expand task scope.
7. Expose active investigation/recovery separately from waiting for a user in
   dashboard/status output, with the next action or exact prerequisite. Keep
   human `recheck` for actual intervention; ordinary recovery should not need it.

## Impacted files

- [scheduler.py](../src/agent_orchestrator/scheduler.py): prompts, provisional escalation and sequential continuation.
- [recovery.py](../src/agent_orchestrator/recovery.py): progress/no-progress classification and recovery episodes.
- [database.py](../src/agent_orchestrator/database.py): additive durable recovery metadata/events if needed.
- [models.py](../src/agent_orchestrator/models.py): result validation or state definitions only if necessary.
- [dashboard.py](../src/agent_orchestrator/dashboard.py): investigation versus human-wait reporting.
- [task contract](../docs/task-contract.md) and [README](../README.md): escalation schema, compatibility and operational semantics.
- [tests](../tests/): recovery prompts, lifecycle, persistence, controls, recheck, quota and dashboard regressions.

## Acceptance criteria

- [ ] An ordinary implementation failure/proposed blocker with a safe next action continues into recovery without manual recheck.
- [ ] A proposed blocker is assessed before terminal escalation; the recorded decision distinguishes failed methods from unavailable capabilities.
- [ ] Ambiguous side effects stay protected while safe investigation continues; no duplicate dispatch, controller or consumed-window replay occurs.
- [ ] Genuine human/external barriers yield concrete evidence and an actionable or precisely explained escalation.
- [ ] Explicit stop/await-user instructions and authorization limits override automatic continuation.
- [ ] Evidence-only diagnostic progress does not become a false no-progress block; repeated ineffective actions are detected without an infinite loop.
- [ ] Restart/quota recovery preserves objective/task IDs, sessions, acceptance criteria, checkpoints, counters and recovery episodes without duplicating side effects.
- [ ] Existing databases/results and manual recheck retain documented compatibility; no mandatory consumer repository dependency is introduced.
- [ ] Dashboard/status distinguishes active recovery from waiting for intervention.

## Verification

Use isolated temporary databases/workspaces and deterministic provider fixtures.
Cover recoverable defects, structured and legacy blocked results, human barriers,
ambiguous writes, explicit stops, evidence-only progress, repeated no-progress,
quota interruption, scheduler restart and manual recheck. Run:

```text
.venv/Scripts/python -m pytest tests/test_recovery_prompt.py tests/test_lifecycle.py tests/test_recheck.py tests/test_dashboard.py tests/test_service.py -q -m "not live"
.venv/Scripts/python -m pytest tests -q -m "not live"
```

The original proposal was documentation only. Implementation was subsequently
authorized on 2026-11-17. See the implementation record below for current checks.

## Notes and boundaries

The user originally deferred implementation because remaining quota was limited,
then explicitly authorized autonomous operational recovery and quota resumption.
Existing historical objectives retain their original meaning and failure policy.

## Implementation record — 2026-11-17

[Autonomous operations](../docs/autonomous-operations.md) documents the opt-in
CLI, unchanged Task schema, one persistent operational task, structured recovery
assessment, deterministic monitoring and scheduler-owned consumer launches.
Ordinary blocked/unresolved results return to repair in the same session. Actual
external escalation needs a second assessment and existing evidence files; the
schema is not a substitute for the worker establishing the external facts.
Explicit controls and uncertain transaction safeguards remain authoritative.
Legacy objectives retain their original DAG, failure and manual recheck behavior.

`tests/test_operations.py` covers semantic check failures, malformed output,
same-session repair/quota/restart, pause/stop, premature completion, failed final
validation, healthy-interval discontinuities, checks during worker turns and
consumer survival/consumed uncertain launch intents. The consumer supplies
domain-specific health and stop logic; no FC dependency exists in the package.
Repeated worker failures are durably recorded and prompted toward alternative
hypotheses; diagnostic quality still requires real investigation by the worker.

Current verification before launch: full non-live suite 209 passed, one optional
historical replay skipped, three live tests deselected; startup/recheck suite
63 passed; expanded operations/lifecycle/recovery selection 84 passed, one
historical replay skipped. A first integrated run passed its 209 tests but its
concurrent-edit evidence consistency guard invalidated the run; repeat with the
final source frozen passed: 216 passed, one optional historical replay skipped,
three live tests deselected, three subtests passed (178.63 seconds). The complete
integrated acceptance runner succeeded. All 26 operations tests passed. Real
Windows provider-job closure left a scheduler-owned consumer alive, followed by
its confirmed cooperative exit. A migration of an isolated production backup
preserved every original column/row, integrity/FK checks and legacy opt-out.

Consumer production DB and runtime backups were taken under exclusive service
lock with no running owner and checked for integrity, foreign keys and complete
row parity. No historical task/session or control state was reset. This closes
the generic enhancement; it does not assert any consumer's production success.
