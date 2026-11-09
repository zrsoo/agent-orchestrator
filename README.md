# Agent Orchestrator

Python owns durable scheduling; `AgentBackend` owns provider sessions, turns,
notifications, quota readings and interruption. Codex App Server is the initial
adapter. Task fields remain exactly those in
[the task contract](docs/task-contract.md).
No Roblox integration is implemented.

## Install and launch (PowerShell, repository root)

Install Python 3.11+ and Git. For the Codex provider, install and authenticate the
Codex CLI separately, keeping its account and session data in your user profile.
Roblox Studio and Roblox Factory are not dependencies.

```powershell
py -3.13 -m venv .venv
.venv/Scripts/python -m pip install -e ".[test]"
codex login
.venv/Scripts/orchestrator submit examples/objective.txt --workspace C:/Projects/orchestrator-example
.venv/Scripts/orchestrator run
```

Foreground `run` is the preferred interactive workflow. Keep its dashboard open;
use a second terminal to submit work, inspect status or issue controls:

```powershell
.venv/Scripts/orchestrator status
```

Requires Python 3.11+, Git and authenticated `codex` on PATH. Development was
verified on Windows with Python 3.13 and Codex CLI 0.166.0. The server protocol
can be inspected with `codex app-server generate-json-schema --out <directory>`.
The adapter uses the [official App Server protocol](https://developers.openai.com/codex/app-server).

`submit <file>` reads an objective or existing plan as UTF-8. Alternatively:

```powershell
.venv/Scripts/orchestrator submit --text 'Implement and test the described feature' --workspace C:/Projects/example
```

Submission persists work; `run` executes it. For background operation, use
`run --detach`; it launches an independent Python process, confirms exclusive
ownership, prints the actual scheduler PID
and logs to `.orchestrator/service.log`. Startup determination takes at most ten
seconds; a launcher PID alone does not establish readiness. The service stays
alive to process subsequent submissions and quota resets. `run` stays foreground;
Ctrl+C exits the service. `run --once` processes one scheduling step. Restart with
the same database to reconcile unfinished work; completed tasks are not rerun.
After intentionally shutting down a detached service, terminate its printed PID
(PowerShell `Stop-Process -Id <pid>`) and relaunch with the same `--db`.

Interactive foreground `run` refreshes a screen-sized dashboard every 250 ms,
including during planning, worker turns, validation and quota waits. The selected
objective's status stays above a scrollable task/details area. Wheel/Up/Down
scroll, PgUp/PgDn page, Home/End reach the bounds, Tab/Shift+Tab switch objectives,
and `a` jumps to active work. Selection and position survive updates and resizing.
It shows the planning phase before the DAG exists, then every persisted task's
scoped ID, description, dependencies and state. Objective progress, active work/elapsed
time, reported quota/reset times, diagnostics and next actions come from read-only
committed SQLite snapshots. Missing quota remains unknown; the display never
grants admission or changes scheduling. State symbols and labels work without
color; set `NO_COLOR` to disable color. Dashboard text uses ASCII-compatible
presentation; original text remains intact in SQLite and JSON output. Windows
consoles enable VT rendering; redirected input/output, unsupported consoles and
`TERM=dumb` use plain state summaries
only when something changes, without elapsed-only updates or terminal escapes.
Detached summaries stay in `service.log`. `run --once`, `status` and
`status --json` retain their existing output formats. The interactive screen and
cursor are restored on Ctrl+C or service errors when output is healthy. Display
startup/shutdown waits are bounded; broken output cannot prevent provider cleanup.
Provider processes close before scheduler ownership is released.

Global options precede the command:

```powershell
.venv/Scripts/orchestrator --db C:/State/factory.db --config factory.yaml submit objective.md --workspace C:/Projects/example
.venv/Scripts/orchestrator --db C:/State/factory.db run --detach
.venv/Scripts/orchestrator --db C:/State/factory.db status --json --output report.json
.venv/Scripts/orchestrator --db C:/State/factory.db pause obj-0123456789ab
.venv/Scripts/orchestrator --db C:/State/factory.db resume obj-0123456789ab
.venv/Scripts/orchestrator --db C:/State/factory.db recheck obj-0123456789ab
.venv/Scripts/orchestrator --db C:/State/factory.db recheck obj-0123456789ab/task-id
.venv/Scripts/orchestrator --db C:/State/factory.db stop obj-0123456789ab/task-id
```

Use the objective IDs and scoped task IDs printed by `status`. Pause lets the
active turn and validation finish; resumption rechecks successful validation
without another worker turn. Stop is terminal and interrupts an active turn.
Stopping a required task blocks its objective. `resume` still applies only to
paused work. After resolving a human blocker, `recheck` durably queues existing
blocked tasks whose dependencies are complete (or the named blocked task).
Repeated requests while queued/executing coalesce. After committing the request,
the command synchronously probes the owner of the same canonical database:

- Existing foreground/detached owner (exit 0): `Recheck queued; scheduler already running (PID <pid>).`
- New Windows foreground service (exit 0): `Recheck queued; scheduler started (PID <pid>); dashboard opened.`
- Failure on stderr (exit 2): `Recheck queued, but scheduler startup failed: <reason>`.

Startup uses the current Python interpreter and a new visible Windows console.
Acknowledgement follows exclusive lock acquisition and scheduler/dashboard
initialization, before the first scheduling step. A live nonce probe supplies
the scheduler PID, including when a Windows Python launcher has a different PID.
Concurrent callers report the winning owner. The ten-second monotonic startup
budget does not wait for recovery; work continues asynchronously. On POSIX the
independent service logs to `service.log` and reports startup without the Windows
dashboard claim. Spawn failure, child exit, an unresponsive owner or timeout
preserves the committed request; retry `recheck` or start `run` with the same DB.
An owner that becomes ready after timeout can be reused on the next invocation.
The DB-adjacent `.owner.json` is only a probe locator; stale records are never
ownership evidence. Completed/stopped records cannot be rechecked. Existing IDs,
DAG, artifacts, checkpoints and sessions remain.
Only one service can own a database. Windows job objects close provider/tool
process trees when Python exits, including an abrupt Python crash.

## Configuration and execution

The consumer supplies `factory.yaml` in the current directory, or an explicit
`--config` path, to select the provider globally. See [the example](examples/factory.yaml).
If absent, the CLI uses the existing Codex default configuration. Its provider
configuration is copied into each objective at submission; later edits apply to
new objectives. `model: default` uses the installed Codex default. Execution is
unrestricted (`execution_mode: yolo`, approval policy `never`). Run in the intended
VM. Validators and provider tools execute with that user's permissions.

The planning turn returns `{ "tasks": [...] }`; IDs, fields, acceptance and the
complete dependency DAG are checked before an atomic graph insert. Execution is
sequential. Normally each task uses one turn. The worker's final JSON is:

```json
{
  "status": "done",
  "diagnostic": "Implemented and tested",
  "validators": [
    {"command": ["{python}", "check_artifact.py"], "criteria": [0], "timeout": 120}
  ]
}
```

`unresolved` and `blocked` are other worker statuses. Validators are separate
records, not additions to task YAML. Commands are argv arrays, execute in the
task workspace, and must cover every zero-based acceptance index. `{python}`
expands to the service interpreter. Python independently reruns all registered
checks, recording output, exit status and duration before completion. A missing,
malformed, failed or timed-out validator prevents completion. Timeout maximum is
3600 seconds per validator (default 120).

A blocked worker can persist an optional repeatable verification command:
`"blocker_check":{"command":["{python}","check_blocker.py"],"timeout":120}`.
This checks only human-intervention prerequisites, excluding outputs the worker
must generate after resuming. Full task acceptance checks belong in `validators`.
On human recheck, the service runs this command before provider admission. A
nonzero exit or JSON `status` of `BLOCKED`, `unresolved`, `failed`, `fail` or
`error` (case insensitive) leaves the task blocked with command evidence; a
zero exit cannot override that status. This status rule also applies to validators.
Without a stored check, the original provider session verifies the blocker first,
using existing commands identified in its diagnostic/history. Resolving a blocker
does not complete a task: the same worker continues remaining work against the
original acceptance criteria, then independent validators must pass. Rechecks
survive restarts through normal reconciliation; interrupted deterministic checks
may rerun, so check commands must be repeatable. Unresolved rechecks can be
requested again. Pause/stop controls continue to apply.

Codex admission pauses below 2% remaining for 300-minute windows or below 1% for
10080-minute windows. Every reported bucket/limit ID is considered; other windows
block at exhaustion. Missing quota data stays unknown. Active turns finish at a
threshold. A cutoff preserves events, Git revision/diff and file hashes. Reset
notifications wake the service; `resetsAt` bounds fallback waits; every admission
requires a fresh reading. Polling defaults to 60 seconds (`run --poll <seconds>`),
capped at 60 seconds; quota notifications or an earlier reset can wake it sooner.
No extra usage is purchased.

Transport crashes use separate infrastructure retry counters and bounded
exponential delay (2–60 seconds). Interrupted work reconciles in the saved
provider session. Reconciliation receives workspace checkpoints and completed
tool operation summaries to avoid repeating side effects. This is semantic recovery, not an
exactly-once transaction over external systems; workers must inspect uncertain
side effects. Infrastructure remains retryable when the provider is unavailable.

The complete outgoing prompt is limited to **900,000 characters**, enforced by
[`MAX_PROMPT_CHARS`](src/agent_orchestrator/prompts.py) before session resumption
or turn creation and again at the Codex adapter. This leaves headroom below the
App Server's 1,048,576-character limit. Required objectives, acceptance criteria,
instructions, diagnostics and recovery identifiers are never truncated. If they
cannot fit, the owner becomes `BLOCKED` with an actionable diagnostic and an
`input_rejected` event preserving its prior diagnostic. App Server
`input_too_large` responses also block without infrastructure backoff/retries;
rejected turns are recorded as `rejected`, not uncertain transport failures.
After fixing the input, use `recheck` on the existing blocked task/objective.

Recovery context summarizes four checkpoints, twenty completed items and three
infrastructure failures, with total counts; failures do not displace completed
items. Summaries retain workspace/database paths, task/session/turn IDs, checkpoint
stage/revision, changed/dirty file-name samples, operation/status/exit-code summaries
and exact artifact/event row references. Previews and samples are explicitly
labelled. Full diffs, file hashes and tool outputs remain in SQLite; untracked
contents remain in the original workspace. The prompt explains read-only SQLite
access and paging. New checkpoints also record Git dirty-file names so identical
snapshots do not hide outstanding edits; old checkpoints require no migration.
Restart reconciliation preserves the existing blocker/failure diagnostic.

Unresolved worker handoffs and failed independent validation count as disruptions.
The first requests repair in the same worker; the second enters
`FIXING_PERSISTENT_FAILURE` and requests diagnosis plus an alternative approach.
From the third disruption, unchanged checkpoint file hashes indicate no progress
and cause `BLOCKED`; changed files allow continued repair. A successful validated
task ends the episode. In-turn debugging, quotas and crashes do not increment it.
Workers may report owner input/no viable action as `blocked` immediately.

SQLite stores objectives, tasks, dependencies, turns, events, artifacts and
validators. JSON status includes full records, provider session/turn IDs, quota
readings, reset times, usage notifications when available, retries and diagnostics.
Checkpoint hashes cover files up to 10 MB; dependency/cache/runtime directories
and the database are excluded. Git diff covers tracked changes; untracked file
contents remain in the workspace. Preserve both workspace and database to resume.

## Verification

```powershell
.venv/Scripts/python -m pytest tests -q
.venv/Scripts/python scripts/test_m1_recheck_startup.py
.venv/Scripts/python scripts/test_m1_integrated_cli.py
$env:ORCHESTRATOR_RECOVERY_DB = (Resolve-Path .orchestrator/state.db).Path
.venv/Scripts/python -m pytest tests/test_recovery_prompt.py -q -s
Remove-Item Env:ORCHESTRATOR_RECOVERY_DB
$env:ORCHESTRATOR_LIVE = '1'
.venv/Scripts/python -m pytest tests/test_live.py -q
```

The default suite uses an alternative provider, a real subprocess fault server
and sanitized [recorded Codex events](tests/fixtures/codex_events.json). It covers
DAG rejection, threshold boundaries, active-turn completion, exhaustion,
notifications and elapsed-reset fallback with authoritative rereads, same-session
reconciliation, killed worker, abrupt detached-Python restart, episode recovery,
validators, controls, service exclusion and human rechecks (including concurrent
requests, interrupted checks/workers and preserved graph/session continuity).
The integration entry point runs the suite in a fresh interpreter using the
installed distribution without inheriting `PYTHONPATH`. It records exact argv, environment, source and
evidence hashes, JUnit results, wire requests, redirected output and Windows
console/Ctrl+C facts in `reports/m1/integrated-cli/`. It uses temporary databases
and fixture providers; live inference and the optional production incident replay
are excluded. See [migration and operations](docs/migration.md) for state handling and verification evidence.
The optional `ORCHESTRATOR_RECOVERY_DB` regression replays the persisted
`obj-43726a1e659f/m0_private_publication` prompt in a read-only SQLite transaction.
It sends no provider requests and changes no objective, task, graph or evidence.
Other recovery regressions cover multi-megabyte checkpoints/tool outputs, repeated
infrastructure failures, complete-input limits and non-transient wire rejections.

Live tests consume Codex quota and save SQLite/events/reports under
`.m1-progress/live-acceptance/` (override `ORCHESTRATOR_LIVE_ROOT`). They verify
detached planning plus a two-task DAG and pause/resume; kill a real provider
process and reconcile; and inject exhaustion through a real `turn/interrupt`,
wait, then reconcile in the same session after the injected reset. Actual account
quota exhaustion and multi-hour waiting are simulated, not deliberately induced.

Deferred: additional real adapters, provider switching during a task, parallel
workers, automatic Windows boot startup and Roblox M0. Alternative adapters can
be injected with `Scheduler(db, {"provider": factory})`; no task, database or
recovery changes are needed. The CLI exposes only the installed Codex provider.

## External workspaces and state

The working directory determines the default `.orchestrator/state.db`, optional
`factory.yaml`, and default submission workspace. These defaults are unchanged.
Use absolute `--db`, `--config`, and `--workspace` paths when operating across repositories.
Commands controlling the same work must always use the same canonical database path.

```powershell
.venv/Scripts/orchestrator --db C:/State/factory.db --config C:/hustle/roblox-factory/factory.yaml submit --text 'Implement the requested change' --workspace C:/hustle/roblox-factory
.venv/Scripts/orchestrator --db C:/State/factory.db status
.venv/Scripts/orchestrator --db C:/State/factory.db run
```

Use a separate database for independent objective sets. Provider configuration is
pinned at submission. Installing this package does not start a scheduler or resume work.
Do not submit existing work again after migration: use its original IDs and database.
Production state remains at the original workspace path; see [migration](docs/migration.md).

On POSIX, use `.venv/bin/python` and `.venv/bin/orchestrator` in the same commands.
Only the existing Windows console tests require a Windows host. Live-provider tests
remain opt-in and consume quota; ordinary tests use local fixtures.
