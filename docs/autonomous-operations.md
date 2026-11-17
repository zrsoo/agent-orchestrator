# Autonomous operational objectives

Opt in per new objective; existing objectives and their task/result contracts
retain their original behavior. No consumer imports or fixed workspace paths.

```powershell
.venv/Scripts/orchestrator --db C:/State/work.db submit objective.md --workspace C:/Projects/app --autonomous --healthy-seconds 14400
.venv/Scripts/orchestrator --db C:/State/work.db run --detach
```

Submission pins original text, configuration and minimum healthy duration. The
scheduler creates one durable `operate` task. The worker maintains its plan,
hypotheses/results and progress in the source objective file, preserving original
requirements. Edits do not replace the saved acceptance contract or create a new
DAG/session. Independent validators still cover every acceptance index.

## Worker handoffs

Normal `done`/validators still apply. Additional final JSON results:

```json
{"status":"continue","diagnostic":"Completed stage; next specific action"}
```

```json
{
  "status":"monitoring",
  "monitor": {
    "check":{"command":["{python}","health.py"],"timeout":30},
    "stop":{"command":["{python}","stop.py"],"timeout":30},
    "interval":30,"startup_seconds":90
  },
  "service":{"id":"unique-launch-intent","command":["{python}","watchdog.py"]}
}
```

`continue` can include the same monitor/service fields to launch, then resume
browser attachment/startup work in the next worker turn. `service` is optional
when the runtime already exists. Use a unique ID for each
deliberately reconciled new launch. Scheduler persists intent before spawning,
never replays the same launch ID (including uncertain intent after a crash), and
logs the child outside the provider process/job tree. The consumer must provide
an exclusive runtime lock and ownership-checked idempotent stop; a PID alone is
insufficient. Commands contain public arguments only. Stop/pause explicitly stop
the owned consumer when the running scheduler observes the control. Keep the
scheduler alive for controls and monitoring. Scheduler shutdown leaves consumers
running; independent consumer watchdogs remain responsible for immediate safety.

Check interval is 5–300 seconds; check/stop timeouts are at most 60 seconds. Check
stdout must be exactly JSON, exit 0:

```json
{"status":"healthy","generation":"unique-runtime-id","diagnostic":"...","evidence":[]}
```

Other statuses are `repair` and `starting`. Starting is allowed only within
`startup_seconds` (default 90, maximum 300) after an actual new scheduler-owned
launch. It never counts toward health; repeated handoffs/consumed launch IDs
cannot extend the grace. Nonzero exit, timeout, malformed output or
missing generation also fail health. Checks must verify semantic operation,
freshness and error streaks; process life alone is inadequate. Checks must not
repair/restart or perform account mutations. The stop command must acknowledge
the runtime stopped without touching another owner's services.

Health checking runs without provider/quota admission, including through the
worker control callback during a turn. A fault stops the consumer and queues
same-session repair. Persistent failures, worker `unresolved`, malformed output
and proposed `blocked` continue investigation automatically. Fresh quota denial
preserves pending work; authoritative quota recovery resumes it without recheck.
Offline service processes keep running during quota waits. A stopped service
cannot be repaired while AI quota is unavailable.

Consecutive health uses a monotonic clock between valid checks in the same
generation and scheduler epoch. A fault, generation change, scheduler restart or
gap exceeding `2 * interval + check_timeout` resets it. A fresh check and normal
independent acceptance validation are required before completion. The duration
does not prove consumer-specific success: retained operational evidence must
also meet the objective. Final completion leaves the healthy service running.
After completion monitoring by this objective ends; the consumer watchdog stays.

## External escalation

A proposed blocker requires a separate recovery assessment in the saved session.
An assessed `blocked` result must contain:

```json
{
  "status":"blocked",
  "diagnostic":"Precise external limitation",
  "escalation":{
    "category":"human_challenge",
    "attempted_actions":["Action and observed result"],
    "remaining_safe_actions":[],
    "evidence_files":["existing-sanitized-evidence.json"],
    "required_change":"Exact unavailable external prerequisite",
    "safe_state":"Owned process stopped; unknown effects protected"
  }
}
```

Other categories: `account_restriction`, `external_dependency`. Evidence files
must exist; safe actions remaining means continue repair. First proposals and
legacy blocked results never directly block. The worker must substantiate the
barrier; schema validation is not an independent proof of an external claim.
Explicit controls, authorization limits and transaction no-replay rules remain.

`operation` stores pending repair/assessment, next check/retry, generation,
monotonic interval and epoch. `operational_launch` stores consumed launch IDs.
Events retain health reports, stops, launch intents and recovery assessments.
Read these narrowly with SQLite `mode=ro`; do not dump whole provider histories.
Dashboard `MONITORING` distinguishes quota-free checks from active repair and
`WAITING_QUOTA`. Use normal status/recheck for actual external intervention.

## Verification

```powershell
.venv/Scripts/python -m pytest tests/test_operations.py -q
.venv/Scripts/python -m pytest tests -q -m "not live"
.venv/Scripts/python scripts/test_m1_integrated_cli.py
.venv/Scripts/python scripts/test_m1_recheck_startup.py
```

Fixtures exercise real health/stop subprocesses and detached consumer survival;
no production account or live model is used. Before deploying additive schema
changes, follow [backup and quiescence](migration.md#backup-and-restore).
