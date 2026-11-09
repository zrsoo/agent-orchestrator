# Agent orchestrator extraction engineering report

The reusable orchestrator is now `C:/hustle/agent-orchestrator`, a standalone
Python repository on `main`. Roblox Factory is its consumer and contains no
duplicate orchestrator implementation. Production state is preserved and the
19 production Python modules are byte-for-byte identical to the source baseline.
No live Codex inference was performed during this migration.

## Provenance and scope

- Source: `C:/hustle/roblox-factory`, branch `main`, revision
  `51b7e53a1bb5e0de6d79df36f251127bac949d26`.
- Initial Git status was clean; there were no uncommitted user changes to exclude
  or overwrite. SHA-256, size and path were recorded for all 4,298 tracked files.
- Complete baseline inventory and original Git refs/history are preserved outside
  Git in `C:/hustle/orchestrator-migration-evidence/source-inventory.json` and
  `source.bundle`. The full original orchestrator directory is preserved there as
  `original-orchestrator/`, including ignored local artifacts, outside both repos.
- Exact migration hashes and dispositions are in `docs/migration-manifest.json`
  in the standalone repo and `reports/orchestrator-extraction/manifest.json` in
  Factory. They account for every original package file plus both generic runners.
- 44 files migrated; 2 Roblox test modules stayed in Factory. Of these 46 entries,
  43 are byte-identical. The README and two runner paths were adapted.
- Generic operational assertions from the original M1 documentation validator
  moved into `tests/test_documentation.py`. The complete original snapshot
  validator remains byte-identical as
  `planning/M1/historical-acceptance/test_m1_documentation.py.txt` in Factory.

## Compatibility and dependencies

There are no intentional library, scheduling, persistence, CLI, provider or
recovery behavior changes. The package remains `agent-orchestrator` 0.1.0,
import package `agent_orchestrator`, entry point
`orchestrator = agent_orchestrator.cli:main`. Every production source hash is
unchanged. Commands and flags, provider/config pinning, DAGs, retries, quotas,
validators, controls, prompt limits and provider session resumption are retained.

Dependencies remain Python >=3.11, setuptools >=68, PyYAML >=6, and pytest >=8 for
the test extra. Git and authenticated Codex on PATH are operational prerequisites
for real work. Fixture providers use the same unchanged adapter over real local
JSON-RPC subprocesses; no external inference is needed for deterministic tests.
No environment, cache, credential, SQLite database, provider session, service log
or machine executable was added to either Git repository.

The new root uses the existing conventional `src/agent_orchestrator`, `tests`,
`examples`, and `pyproject.toml` layout. `pip install -e ".[test]"` works at the
root. New `.gitignore` rules exclude runtime/private/build outputs; `.gitattributes`
preserves extracted bytes across platform checkouts.

Defaults remain relative to the invocation's working directory:
`.orchestrator/state.db`, optional `factory.yaml`, and the default submission
workspace. Consumers supply configuration and absolute external workspace paths.
`--db`, `--config`, and `--workspace` override defaults without hardcoded Factory
paths in library source. `examples/factory.yaml` demonstrates the original generic
configuration. Provider configuration remains pinned in each existing objective.

Factory setup now accepts `-OrchestratorPath` or `ORCHESTRATOR_ROOT`, defaulting
to the sibling checkout, and installs that external package into its environment.
Factory test tooling resolves external generic fixtures through
`scripts/orchestrator_paths.py`; runtime imports use the installed distribution.
Historical CLI acceptance entry points delegate to the external runners. The old
`orchestrator/src` PYTHONPATH injection is removed from Factory operational code.
Roblox publication and monetization tests remain under `tests/orchestrator` with
their assertions and bytes unchanged.

## Runtime state, backup and recovery

Process inspection found no scheduler or active provider objective. A live owner
probe returned no owner and the exclusive OS `ServiceLock` was acquired before
the source SQLite backup. Production contained six completed objectives, one
blocked objective and one stopped objective; no running turns. Its 40 tasks were
38 completed, one blocked and one stopped. Workspaces include Factory and UGC.

SQLite's backup API produced a recoverable, integrity-checked snapshot, then the
production transfer. No live database was blindly copied. All seven tables were
streamed and compared in rowid order, including every column, before/after transfer.
Integrity checks returned `ok`; foreign-key checks returned no violations.

- Canonical production DB: `C:/hustle/agent-orchestrator/.orchestrator/state.db`.
- Compatibility path: `C:/hustle/roblox-factory/.orchestrator` is a junction to the
  standalone runtime directory. Both paths resolve to the same DB and lock.
  Holding the new-path lock was verified to exclude acquisition via the old path.
- Verified offline backup:
  `C:/hustle/orchestrator-migration-evidence/production-state.backup.db`.
- Backup SHA-256:
  `a6af0b5a79c9a236e8f285cfb507366d57d208dfa41b6000d0594d5ac1dbd042`.
- Entire original runtime directory is retained as
  `C:/hustle/orchestrator-migration-evidence/original-runtime/`.
  Its transient lock/SQLite sidecars are archival, not live ownership evidence.
- All 48 referenced Codex session files remain in `C:/Users/zneam/.codex` and
  their bytes match the initial private inventory. No credentials or session
  contents were copied into Git. Preserve this profile and the original external
  workspaces alongside SQLite; untracked workspace data is not restored by SQLite.
- An isolated SQLite backup was opened with the standalone `Database` and
  `Scheduler.recover()`/`tick()`, with any provider creation forbidden. No row
  changed, no objective was duplicated, and no completed task was repeated.
  Bounded recovery context was generated and checked for all 40 existing tasks,
  preserving their original task/session references. Synthetic restart tests
  separately cover interrupted active work, same-session reconciliation, controls,
  queue survival, checkpoints, retries and independent validation.
- Final audit confirmed unchanged production rows, unchanged backup hash,
  unchanged session files, no live production owner and no stale owner record.
  Detailed state evidence stays outside Git in `state-baseline.json`,
  `state-transfer.json` and `final-audit.json` under the evidence directory.

| Table | Rows preserved | SHA-256 of all original rows |
| --- | ---: | --- |
| artifact | 234 | `437a7172a190e52e1db50d3c5905762137dc88bcf4d48350539428701981a90b` |
| dependency | 32 | `7fc87f08b804a7df930cf01b3bb72e918e4bf81d9634267da17d91b7f175ad21` |
| event | 358900 | `c3cdc9909746aa1da989e44ce5271461bc00d16c23211fcb056247d673ebc1c8` |
| objective | 8 | `3ff81c94f8557f578bbb5f6e66e07c34b86172d14ee0bb93deabb018e3130b44` |
| task | 40 | `845c8b5ac68c65c686e1e31b0dc9b6e6d4ca93b520ae7fcd1f16f2be051d432d` |
| turn | 59 | `436a9cef85a0126b1c989dcf8ca1eb1a6a5312c3942ff66861b571c2b947b5ef` |
| validator | 77 | `6a962e8a89b54386cd3046dc1dfce492398f8c5695058a236540cf1a42478e06` |

To restore: establish quiescence and acquire the canonical service lock; preserve
the current database as another backup; restore the verified SQLite snapshot to
the single canonical runtime location. Do not restore stale `.owner.json`, `.lock`,
`-wal` or `-shm` files as running-service evidence. Inspect status before starting
the scheduler, retain original IDs, and never submit existing work again to resume it.
Recheck can launch actual inference; it is not a read-only status probe.

## Validation evidence

All logs, exact command records and JUnit XML are in
`C:/hustle/orchestrator-migration-evidence/`; evidence was not written over
historical Factory reports. A publication fixture test's regenerated report was
copied there and the historical original restored and verified before commits.

| Gate / evidence | Result |
| --- | --- |
| Original complete suite, `baseline.xml` | 219 passed, 4 skipped; 180.51 s |
| Original historical M1 document/plan scripts, `historical-baseline.xml` | 8 passed, 2 failed, 26 subtests passed; both failures were stale milestone source/scope snapshots already present at baseline |
| Original setup tests, `setup-baseline.log` | 26 passed, 0 failed |
| First standalone integration attempt | 185 tests passed; evidence gate correctly rejected it because additional migration tests were added during the run; superseded by fixed-source independence run |
| Independence integration, original source directory unavailable, `independence-integration/results.json` | 189 passed, 1 optional incident replay skipped, 3 live tests deselected, 3 subtests passed; 188.46 s; source/evidence hashes and acceptance assertions passed |
| Post-cleanup full standalone suite, `standalone-final.xml` | 189 passed, 4 expected skips, 3 subtests passed; 188.03 s |
| Factory retained integration/permissions/M1 plan/M2 foundation, `factory-final.xml` | 72 passed, no skips, 79 subtests passed |
| Publication fixtures, `publication-fixtures-final.xml` | 14 passed, 1 explicitly deselected local-build artifact check |
| Retained M1 plan/M2 scope checks, `retained-scope-final.xml` | 7 passed, 23 subtests passed |
| Final foundation/final protected-source gates, `protection-final.xml` | 2 passed |
| Game source checks plus protected-source gate, `game-source-final.xml` | 4 passed |
| Factory operational documentation delegate, `documentation-consumer.log` | 2 passed, 3 subtests passed |
| Final setup tests, `setup-final.log` | 26 passed, 0 failed |
| Cross-repository fixtures, `external-precleanup.xml`, `external-final.xml` | 2 passed on each run; read-only objective targets actual Factory; another objective executes in an isolated temporary Git repo; controls, recheck, restart, session continuity, completed-task preservation and workspace isolation checked |
| Fresh editable install | Declared dependencies installed in new standalone `.venv`; `-I` import resolves standalone source; CLI and `pip check` pass; exact root `pip install -e ".[test]"` also verified |
| Wheel build/install, `wheel-smoke.json` | Fresh independent environment imports from `site-packages`; all 19 packaged modules match baseline; external fixture objective completed with 2 turns and 1 independent validator; five fresh `run --once` processes repeated no completed task; no PYTHONPATH |
| Runtime audit, `final-audit.json` | SQLite integrity/FK/all-row parity, backup hash, all 48 session hashes, alias lock exclusion and no active owner passed |

The baseline's 219 passes partition into 185 generic passes and 34 retained Roblox
integration passes. The standalone suite adds two documentation contracts and two
cross-workspace tests: 185 + 4 = 189 passes. The retained 34 Roblox cases are in
the Factory regression run. No existing generic test or assertion was removed or
weakened. The historical documentation script's two stale snapshot assertions
remain archived unchanged; its reusable operational checks run in the new suite.
The current M1 plan checks all pass. M2 historical byte gates explicitly verify
approved relocated/current files and reconstruct the exact original Git checkout
bytes, including proven CRLF conversion, before checking the historical hashes;
unrelated protected files still use their current bytes.

The four default-suite skips are exactly three opt-in live Codex tests and the
opt-in historical incident replay. That replay targets a specific old incident
objective absent from the current production DB; it is not a generic migration
test. Existing state was validated through all-table parity and isolated recovery
instead. Windows console visibility, real Ctrl+C cleanup/restoration, input/resize
and pseudoconsole transport tests ran on this Windows host; none were skipped.

## Independence and cleanup

During the full independence integration run, Factory's original `orchestrator`
directory was temporarily renamed so the old source path did not exist. The
runner cleared inherited PYTHONPATH and the new environment passed all required
acceptance checks. The directory was restored before the controlled cleanup,
then archived outside Git after every migration gate passed. The wheel test
further verified packaged operation from an unrelated directory without an
editable source import or old virtual environment.

The library's imports, defaults and runtime paths contain no Factory source
dependency. Consumer-specific paths occur only in documentation and explicit
acceptance inputs. New objectives can target arbitrary absolute workspaces with
separate state databases. Factory imports now resolve to the standalone checkout.
No orchestrator implementation remains in Factory. Historical milestone plans,
reports, source history, consumer config and Roblox code remain. Historical
Markdown links and active improvement references were repaired; recorded historical
commands/revision paths remain explicitly labelled historical. Original game,
research, integration, skill, toolchain and historical report diffs are empty.

## Remaining limitations and failed exploratory checks

- No live-provider inference or real remote session-resume handshake was performed.
  Adapter initialization, protocol, interruption and session resumption were tested
  with real fixture subprocesses plus the existing sanitized recorded Codex events.
- Native game `manage.py lint` fails on pre-existing source formatting; the exact
  baseline launcher reproduces the failure. `manage.py test` passes its 3 Python
  source checks but Lune cannot launch (`3221225781`); exact baseline replay also
  reproduces this. Game/toolchain files and tools were not changed by extraction.
  Full native Roblox/Luau execution remains unverified on this host.
- An exploratory publication-artifact check fails because local ignored
  `games/m0-token-trail/build/result.json` is absent. Its fixture-only regression
  run explicitly deselects that one artifact-dependent case; no assertion was
  changed. No publishing, cloud API calls, purchases or production objective work
  was initiated to manufacture that evidence.
- Initial local migration verification issues (runner source-change detection,
  SQLite Row serialization in the audit helper, and Git-normalized-vs-original
  CRLF byte hashes) were diagnosed and corrected; final acceptance records above
  pass. Their initial logs remain available and are not represented as passing.
- State recovery across a different machine still requires the provider account's
  session store and workspace files. The compatibility junction is a local Windows
  filesystem object; on another host use one explicit canonical `--db` path and
  recreate the alias only if needed. Never operate writable duplicate state copies.

## Install and use

From a fresh standalone checkout with Python and Git installed:

```powershell
cd C:/hustle/agent-orchestrator
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[test]"
.venv/Scripts/python -m pip check
.venv/Scripts/python -m pytest tests -q -rs
.venv/Scripts/orchestrator --help
```

Install/authenticate the Codex CLI separately before real inference. The package
does not contain account credentials. For existing production history:

```powershell
.venv/Scripts/orchestrator --db C:/hustle/agent-orchestrator/.orchestrator/state.db status
# Explicitly starting the scheduler can resume real work:
.venv/Scripts/orchestrator --db C:/hustle/agent-orchestrator/.orchestrator/state.db run
```

To submit new work against Factory, using a new independent state database:

```powershell
.venv/Scripts/orchestrator --db C:/State/new-work.db --config C:/hustle/roblox-factory/factory.yaml submit --text 'Implement the requested change' --workspace C:/hustle/roblox-factory
.venv/Scripts/orchestrator --db C:/State/new-work.db status
.venv/Scripts/orchestrator --db C:/State/new-work.db run
```

Use another absolute `--workspace` to target any other repository. Global options
precede commands. For POSIX use `.venv/bin/python` and `.venv/bin/orchestrator`.

## Exact migrated and retained inventory

Destination paths are relative to `C:/hustle/`. Source paths are relative to the
original Factory root. SHA-256 values, original sizes in the full baseline, and
approved changes are available in the migration manifest and external inventory.

| Original file | Destination | Byte parity |
| --- | --- | --- |
| `orchestrator/README.md` | `agent-orchestrator/README.md` | adapted |
| `orchestrator/examples/objective.txt` | `agent-orchestrator/examples/objective.txt` | identical |
| `orchestrator/examples/task.yaml` | `agent-orchestrator/examples/task.yaml` | identical |
| `orchestrator/pyproject.toml` | `agent-orchestrator/pyproject.toml` | identical |
| `orchestrator/src/agent_orchestrator/__init__.py` | `agent-orchestrator/src/agent_orchestrator/__init__.py` | identical |
| `orchestrator/src/agent_orchestrator/__main__.py` | `agent-orchestrator/src/agent_orchestrator/__main__.py` | identical |
| `orchestrator/src/agent_orchestrator/backends/__init__.py` | `agent-orchestrator/src/agent_orchestrator/backends/__init__.py` | identical |
| `orchestrator/src/agent_orchestrator/backends/base.py` | `agent-orchestrator/src/agent_orchestrator/backends/base.py` | identical |
| `orchestrator/src/agent_orchestrator/backends/codex.py` | `agent-orchestrator/src/agent_orchestrator/backends/codex.py` | identical |
| `orchestrator/src/agent_orchestrator/cli.py` | `agent-orchestrator/src/agent_orchestrator/cli.py` | identical |
| `orchestrator/src/agent_orchestrator/dashboard.py` | `agent-orchestrator/src/agent_orchestrator/dashboard.py` | identical |
| `orchestrator/src/agent_orchestrator/database.py` | `agent-orchestrator/src/agent_orchestrator/database.py` | identical |
| `orchestrator/src/agent_orchestrator/executor.py` | `agent-orchestrator/src/agent_orchestrator/executor.py` | identical |
| `orchestrator/src/agent_orchestrator/models.py` | `agent-orchestrator/src/agent_orchestrator/models.py` | identical |
| `orchestrator/src/agent_orchestrator/processes.py` | `agent-orchestrator/src/agent_orchestrator/processes.py` | identical |
| `orchestrator/src/agent_orchestrator/prompts.py` | `agent-orchestrator/src/agent_orchestrator/prompts.py` | identical |
| `orchestrator/src/agent_orchestrator/quota.py` | `agent-orchestrator/src/agent_orchestrator/quota.py` | identical |
| `orchestrator/src/agent_orchestrator/recovery.py` | `agent-orchestrator/src/agent_orchestrator/recovery.py` | identical |
| `orchestrator/src/agent_orchestrator/scheduler.py` | `agent-orchestrator/src/agent_orchestrator/scheduler.py` | identical |
| `orchestrator/src/agent_orchestrator/service.py` | `agent-orchestrator/src/agent_orchestrator/service.py` | identical |
| `orchestrator/src/agent_orchestrator/terminal.py` | `agent-orchestrator/src/agent_orchestrator/terminal.py` | identical |
| `orchestrator/src/agent_orchestrator/validation.py` | `agent-orchestrator/src/agent_orchestrator/validation.py` | identical |
| `orchestrator/src/agent_orchestrator/viewport.py` | `agent-orchestrator/src/agent_orchestrator/viewport.py` | identical |
| `orchestrator/tests/fixtures/app_server.py` | `agent-orchestrator/tests/fixtures/app_server.py` | identical |
| `orchestrator/tests/fixtures/codex_events.json` | `agent-orchestrator/tests/fixtures/codex_events.json` | identical |
| `orchestrator/tests/fixtures/conpty.py` | `agent-orchestrator/tests/fixtures/conpty.py` | identical |
| `orchestrator/tests/fixtures/console_probe.py` | `agent-orchestrator/tests/fixtures/console_probe.py` | identical |
| `orchestrator/tests/fixtures/m1_app_server.py` | `agent-orchestrator/tests/fixtures/m1_app_server.py` | identical |
| `orchestrator/tests/fixtures/m1_blocked_display.py` | `agent-orchestrator/tests/fixtures/m1_blocked_display.py` | identical |
| `orchestrator/tests/fixtures/m1_console.py` | `agent-orchestrator/tests/fixtures/m1_console.py` | identical |
| `orchestrator/tests/fixtures/m1_console_input.py` | `agent-orchestrator/tests/fixtures/m1_console_input.py` | identical |
| `orchestrator/tests/fixtures/m1_scroll_console.py` | `agent-orchestrator/tests/fixtures/m1_scroll_console.py` | identical |
| `orchestrator/tests/polling.py` | `agent-orchestrator/tests/polling.py` | identical |
| `orchestrator/tests/test_dashboard.py` | `agent-orchestrator/tests/test_dashboard.py` | identical |
| `orchestrator/tests/test_lifecycle.py` | `agent-orchestrator/tests/test_lifecycle.py` | identical |
| `orchestrator/tests/test_live.py` | `agent-orchestrator/tests/test_live.py` | identical |
| `orchestrator/tests/test_m0_publication.py` | `roblox-factory/tests/orchestrator/test_m0_publication.py` | identical |
| `orchestrator/tests/test_m0_recovery.py` | `roblox-factory/tests/orchestrator/test_m0_recovery.py` | identical |
| `orchestrator/tests/test_m1_cli.py` | `agent-orchestrator/tests/test_m1_cli.py` | identical |
| `orchestrator/tests/test_m1_remediation.py` | `agent-orchestrator/tests/test_m1_remediation.py` | identical |
| `orchestrator/tests/test_process.py` | `agent-orchestrator/tests/test_process.py` | identical |
| `orchestrator/tests/test_recheck.py` | `agent-orchestrator/tests/test_recheck.py` | identical |
| `orchestrator/tests/test_recovery_prompt.py` | `agent-orchestrator/tests/test_recovery_prompt.py` | identical |
| `orchestrator/tests/test_service.py` | `agent-orchestrator/tests/test_service.py` | identical |
| `scripts/test_m1_integrated_cli.py` | `agent-orchestrator/scripts/test_m1_integrated_cli.py` | adapted |
| `scripts/test_m1_recheck_startup.py` | `agent-orchestrator/scripts/test_m1_recheck_startup.py` | adapted |

Additional standalone files are `.gitignore`, `.gitattributes`,
`examples/factory.yaml`, `docs/task-contract.md`, `docs/migration.md`,
`docs/migration-manifest.json`, `docs/migration-report.md`,
`tests/test_documentation.py` and `tests/test_external_workspaces.py`.
Factory's complete modified tracked-file inventory is below; new consumer helper,
guide, relocated integration tests/conftest, archived historical validator and
migration report/manifest accompany these changes.

- `.gitattributes`
- `improvements/M-1/IMP-002-git-task-regression-attribution.md`
- `improvements/M-1/IMP-003-plan-level-orchestration.md`
- `improvements/M8/IMP-005-parallel-portfolio-execution.md`
- `improvements/shared/IMP-001-remove-permanent-hash-gates.md`
- `planning/M-1/EXECUTION_PLAN.md`
- `planning/M1/EXECUTION_PLAN.md`
- `planning/M1/INTEGRATION_PLAN.md`
- `planning/M1/VERIFICATION.md`
- `planning/M2/EXECUTION_PLAN.md`
- `planning/MASTER_PLAN.md`
- `scripts/m0_qa.py`
- `scripts/setup.md`
- `scripts/setup.ps1`
- `scripts/test_m0_orchestrated_replays.py`
- `scripts/test_m0_private_publication.py`
- `scripts/test_m0_qa_and_skill.py`
- `scripts/test_m1_documentation.py`
- `scripts/test_m1_integrated_cli.py`
- `scripts/test_m1_integration_plan.py`
- `scripts/test_m1_recheck_startup.py`
- `scripts/test_m2_final.py`
- `scripts/test_m2_foundation.py`
- `scripts/test_setup.ps1`

## Commits

Both repositories are committed only after their verification gates. The delivered
`C:/hustle/orchestrator-migration-evidence/FINAL_REPORT.md` appends the exact commit
SHAs and final clean-status evidence after commit creation; the report inside each
commit records the verified implementation and inventories without a circular
self-referential commit hash. No remote push is performed.
