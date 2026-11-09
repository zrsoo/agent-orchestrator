# Improvement backlog

The canonical index for deferred engineering improvements, fixes and technical debt.
Keep one actionable specification per stable `IMP-###` ID using [TEMPLATE.md](TEMPLATE.md).
For a new item, use the next ID above the highest existing ID; do not renumber migrated items.
Use an orchestrator component and an observable scheduling condition instead of an external milestone.
Recording or prioritizing an item does **not** authorize implementation.

Statuses: `proposed`, `planned`, `in_progress`, `done`, `deferred`.
Priorities: `P0` = next required assigned work; `P1` = near-term; `P2` = optional/future;
`P3` = conditional/long horizon.

| ID | Improvement | Priority | Status | Target |
| --- | --- | --- | --- | --- |
| [IMP-002](IMP-002-git-task-regression-attribution.md) | Attribute regression evidence and Git commits to tasks | P0 | proposed | Next assigned provenance objective after verifying current checks accept legitimate changes |
| [IMP-003](IMP-003-plan-level-orchestration.md) | Plan to dependent objectives to task DAGs | P2 | deferred | When measured objective handoff overhead justifies it |

## Working agreement

- Link the canonical item from an affected local task, plan or report when scheduling context is useful; no master plan or milestone directory is required. Do not duplicate its detailed scope.
- Record source/evidence, dependencies, impacted files, measurable acceptance criteria, repeatable verification and backward-compatibility requirements. Keep unavailable external prerequisites explicit instead of making them repository dependencies.
- Close an item only when implementation and verification evidence are linked. Preserve its original issue history and distinguish proposals from completed work.
- Preserve historical verification reports; do not rewrite completed evidence to retroactively satisfy a proposal.
- Maintain sequential execution unless the assigned objective explicitly changes it. No proposal, dependency or priority implicitly authorizes implementation.
