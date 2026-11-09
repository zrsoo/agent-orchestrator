# Migration and operations

## Installation

From the standalone repository root:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[test]"
.venv/Scripts/python -m pip check
.venv/Scripts/orchestrator --help
.venv/Scripts/python -m pytest tests -q -rs
.venv/Scripts/python scripts/test_m1_integrated_cli.py
.venv/Scripts/python scripts/test_m1_recheck_startup.py
```

Prefer foreground `orchestrator run` for the dashboard; use a second terminal for
status and controls. Redirected/non-interactive operation uses plain summaries.
Use the same `--db` for submit, run, status and controls. A recheck startup
failure retains the queued request; its exit code is 2. It is safe to retry that request
with its existing ID after addressing the startup failure.

The consumer owns its configuration and workspaces. No installation command starts
a scheduler. `factory.yaml` is optional and relative to the current directory;
`--config` can select an explicit consumer configuration. See [configuration and
execution](../README.md#configuration-and-execution) for the unchanged lifecycle.

## Production state

The migration transfers the original SQLite database with SQLite's backup API,
after checking the process list, live ownership probe and exclusive OS service
lock. Production objectives are never executed as a migration experiment.

The production database is `C:/hustle/agent-orchestrator/.orchestrator/state.db`.
The original `C:/hustle/roblox-factory/.orchestrator` path is a Windows junction to
that runtime directory, preserving historical paths and a single canonical lock
identity. Both paths must resolve to the same database. Never run independent
writable copies of this database; they would have independent ownership locks.

Recoverable snapshots and original runtime files are outside Git in
`C:/hustle/orchestrator-migration-evidence/`. The [engineering report](migration-report.md) records the
verified backup, full table hashes/counts, transfer checks and original revision.
Provider sessions stay in the existing user Codex profile; they are not copied
into this repository or its Git history. Preserve that profile and every external
workspace as well as SQLite. A database alone cannot restore untracked workspace
contents or provider-side session history.

Inspect production without starting work:

```powershell
.venv/Scripts/orchestrator --db C:/hustle/agent-orchestrator/.orchestrator/state.db status
```

To continue existing work, explicitly start `run` with this database and use
existing objective/task IDs. Completed tasks are not resubmitted. A blocked task
needs its original blocker addressed followed by `recheck`, which starts/reuses
the scheduler. Recheck may execute real provider work; do not use it as a status
probe. Default `run` from this repository also selects the production database.

## Backup and restore

1. Establish quiescence: inspect processes, probe the owner, and acquire
   `ServiceLock` for the canonical database. An absent PID record is insufficient.
2. Use `sqlite3.Connection.backup` to a distinct offline path. Check
   `PRAGMA integrity_check` and `foreign_key_check`, then compare all rows of every
   table, including events, artifacts, validators, sessions and retry/control fields.
3. Preserve workspace files and the authenticated user's provider session store.
   Never put credentials, sessions, databases or logs in Git.
4. To restore, stop all services first and keep the current database as another
   backup. Restore the verified SQLite snapshot to the one canonical runtime path.
   Do not restore `.owner.json`, `.lock`, `-wal` or `-shm` files as live ownership
   evidence. SQLite recreates its sidecars; the OS service lock and live nonce
   probe establish ownership.
5. Inspect status before starting `run`. Recovery reconciles unfinished work in
   its existing sessions; completed work stays complete. Recovery is semantic,
   so uncertain external side effects still require inspection.

## Validation and historical material

The full default suite uses fixture providers, independent subprocess validators,
concurrency/crash/restart injections and real Windows console APIs. The integrated
runner records evidence under ignored `reports/m1/integrated-cli/`; override with
`ORCHESTRATOR_M1_EVIDENCE`. Live inference requires `ORCHESTRATOR_LIVE=1` explicitly.
`ORCHESTRATOR_RECOVERY_DB` enables a historical incident-specific, read-only replay;
it requires that incident's objective ID and is not a generic state migration test.

Roblox publication/monetization integration tests remain with Roblox Factory.
Milestone plans, historical reports and snapshot-specific acceptance scripts also
remain there. Their original byte/scope gates describe historical revisions,
whereas the migration report accounts for approved file moves and modifications.
