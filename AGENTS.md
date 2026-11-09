# Agent instructions

## Planning and execution

- **Plan carefully, then execute precisely.** Planning, implementation, testing, and documentation all require technical accuracy.
- Produce the **shortest complete plan**: concrete steps, dependencies, interfaces, expected artifacts, verification, and acceptance criteria. Do not omit essential details to make it shorter.
- Follow the assigned objective's accepted plan and acceptance criteria. Complete every required step; validate actual outputs. Keep the plan with the task unless a durable repository document is needed.
- Work autonomously, diagnose and fix ordinary failures, and report genuine blockers with the next needed action.
- Prefer simple, modular, testable solutions; reuse existing infrastructure. No unfinished TODOs or placeholders in completed work.
- Preserve unrelated user changes. Preserve the documented CLI, task contract, provider configuration, sequential scheduling, ownership, quotas, persistence and recovery semantics unless the assigned objective explicitly changes them. See [usage](README.md) and [task contract](docs/task-contract.md).
- Keep the package independent of consumer repositories. Consumer workspaces and configuration are inputs, not hardcoded paths, imports or required local checkouts.

## Documentation

- **Minimal, precise, implementation-focused. No fluff.**
- Include only material needed to plan, implement, run, test, debug, or maintain the system.
- Prefer exact commands, schemas, interfaces, ordered steps, and acceptance tests over narrative.
- Omit motivational prose, generic explanations, redundant caveats, and repeated policy commentary.
- Keep one canonical source for each detail; link instead of copying.
- Avoid creating new documents unless they materially help execution.
- When editing docs, cut repetition without sacrificing requirements or accuracy.
- Before finishing, remove unnecessary text and confirm every instruction is actionable and every local link resolves.
- Preserve historical reports as historical evidence; do not rewrite old results to claim current verification.

## Improvements and technical debt

- Record nonblocking technical debt, fixes and deferred enhancements in [improvements](improvements/README.md) using [the improvement template](improvements/TEMPLATE.md). Update the index and link the canonical item from an affected local plan, task or report when relevant; do not duplicate its scope.
- Preserve stable IDs and issue history. Record evidence, dependencies, impacted components/files, measurable acceptance criteria, repeatable verification and compatibility requirements. Close an item only with implementation and verification evidence.
- Recording, prioritizing or linking a backlog item does not authorize implementation. Do not implement backlog items without an assigned objective or explicit approval. Continue work already authorized by the current objective; this rule does not add a confirmation step for in-scope work.
- The backlog is self-contained; it does not require a separate master plan or milestone directory. If a local skill is needed, use `.agents/skills/<name>/SKILL.md` for a scoped procedure, with useful references or deterministic helpers beside it; do not create a second supervisor or import consumer-specific skills.

## Verification and reporting

- Run checks appropriate to the change and verify actual outputs against acceptance criteria. For runtime changes, run the relevant deterministic regression suite from [verification commands](README.md#verification), including persistence, restart/recovery, scheduling, controls, provider fixtures and supported platform checks as affected. Investigate failures and explain skips or unrun checks; do not weaken assertions to obtain a pass.
- Use isolated temporary databases/workspaces and fixture providers for tests. Live inference is opt-in. Never modify production objective state merely to test a change. Before handling production state, detect active services and establish safe quiescence; use SQLite's backup API and integrity/row-parity checks as described in [operations and recovery](docs/migration.md).
- Preserve objective/task identities, dependency graphs, checkpoints, evidence, retries and provider session/turn references across compatible changes. Resume existing work rather than submitting duplicate objectives; use the existing scheduler's recovery mechanisms.
- State what changed, what was actually tested, its results, and any blockers. Distinguish current verification, historical results and unverified live/provider behavior.
- Never claim execution, validation, or completion without evidence. Keep runtime state, credentials, provider sessions, virtual environments, caches and machine-generated outputs out of Git.
