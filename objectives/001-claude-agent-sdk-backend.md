# Objective 001 — Claude Agent SDK backend

Status: assigned implementation objective
Workspace: this repository (agent-orchestrator)
Implement using: existing Codex backend
Date: 2026-11-17

## Goal

Implement Claude as a first-class selectable agent provider, alongside Codex, without changing the objective/task contract. The same objective must support planning, sequential task execution, validator handoff, SQLite evidence, pause/stop/recheck, quota handling and crash/restart recovery through either provider. This is an **implementation** objective, not a feasibility report.

Use Anthropic's official Python Claude Agent SDK, which includes the Claude Code coding-agent loop and tools. Do not substitute raw Messages API calls or build a second scheduler.

## Current integration points

- Backend protocol and normalized outcomes: [backends/base.py](../src/agent_orchestrator/backends/base.py).
- Existing reference adapter: [backends/codex.py](../src/agent_orchestrator/backends/codex.py).
- Scheduler owns provider factories, planning, turns, validation and persisted objective state: [scheduler.py](../src/agent_orchestrator/scheduler.py).
- CLI currently rejects providers other than Codex: [cli.py](../src/agent_orchestrator/cli.py).
- Current provider-quota admission expects fresh reads: [quota.py](../src/agent_orchestrator/quota.py).
- Recovery summary reads Codex-specific completed-item events: [prompts.py](../src/agent_orchestrator/prompts.py).
- Existing task schema, operational mode and verification requirements: [task contract](../docs/task-contract.md), [AGENTS.md](../AGENTS.md) and [README](../README.md).

Preserve provider configuration snapshots at submission, legacy databases and objective IDs, default Codex operation, injected Scheduler factories, independent validators, durable SQLite state, sequential scheduling and existing external-workspace behavior.

## Required implementation

### A. Provider package, CLI and configuration

1. Add a ClaudeBackend implementation under src/agent_orchestrator/backends/claude.py conforming to AgentBackend: quota(), session(workspace, session_id), run(session_id, prompt, emit, control), interrupt(session_id, turn_id), close(). Extend shared interfaces minimally and only where necessary.
2. Register Claude in the normal Scheduler construction and CLI submission paths through a consistent provider registry. Preserve custom factories in tests and the Codex default. Invalid provider/config errors must be explicit.
3. Make claude-agent-sdk an optional install extra (e.g. pip install -e ".[claude,test]"); Codex-only installations must not require the SDK. Select and document a verified compatible SDK release range; work with Windows and Python >=3.11, keeping POSIX functional.
4. Accept provider selection through existing factory.yaml, for example:

   ~~~yaml
   provider: claude
   providers:
     claude:
       model: sonnet
       execution_mode: yolo
   ~~~

   Map yolo to the SDK's bypassPermissions mode and enable normal coding tools including shell/filesystem. Verify actual supported model aliases and SDK permission behavior; never hang waiting for interactive approval. The configured provider/model/permissions remain frozen per submitted objective. Do not add provider or model fields to tasks.
5. Use documented SDK API-key authentication through ANTHROPIC_API_KEY inherited from the process environment. Never persist credentials in SQLite, events, config snapshots, logs, prompts, checkpoints or Git. Report missing/invalid credentials, unsupported model, SDK installation errors and billing failure as actionable non-transient blockers, not infinite infrastructure retry. Do not assume a claude.ai subscription/login provides SDK API credentials.

### B. Sessions, turns, events and output

1. Bridge the SDK's asynchronous client into the existing synchronous AgentBackend safely. Own event loop, clients, subprocesses and shutdown deterministically. Session instances must not leak context across objectives/tasks/workspaces.
2. Generate/persist an SDK-supported stable session identifier **before any side-effecting tool runs**. Prefer the SDK's preassigned session_id option where validated; otherwise implement a safe pre-side-effect handshake. On later turns, human recheck or process restart, resume the exact existing session by ID with the same working directory. Do not silently start a new session if the transcript is missing or corrupted.
3. Emit durable, bounded events for meaningful tool start/end/error, outputs, interruptions and provider status. Store actual SDK session IDs; give turn correlation a precise, documented meaning (never pretend a local UUID is a native Claude turn ID). Record enough identity/evidence to reconcile in-flight or ambiguous tool side effects. Do not reshape Claude events into misleading Codex-native event names.
4. Normalize SDK completion/result messages into Outcome. Extract final result/structured output, not streamed commentary or tool output. Planning must hand back the existing tasks JSON; workers must hand back the existing status/diagnostic/validators JSON (plus optional blocker_check). Keep existing graph validation and independent validator execution authoritative. If choosing SDK structured-output schemas, pass the planning/worker schema explicitly through a minimal provider-neutral mechanism; do not guess schema by parsing prompts.
5. Extend bounded recovery_summary() to recognize Claude tool/evidence events while preserving all Codex and historical recovery behavior. Reuse existing Git/file checkpoints, validation records, attempts and retries. On uncertain completion, inspect existing state and operation destination before replaying an external side effect.

### C. Lifecycle and process ownership

1. Map active stop to actual SDK interrupt; let pause finish the active turn/validation as currently documented. Recheck must reuse the same session and original acceptance criteria. Preserve autonomous operational objectives and their control semantics.
2. Ensure close(), service Ctrl+C, timeout, adapter failure and abrupt Windows supervisor exit clean up SDK/Claude Code child/tool processes without orphaning the service or database lock. Integrate with the existing processes.py/ProcessTree mechanism, adapting the SDK subprocess ownership safely. Test real process-tree behavior where supported.
3. Classify connection faults, timeout, invalid input/oversized prompt, aborted turns, missing transcript, quota rejection, auth/billing failure and other SDK errors appropriately: completed/blocked/interrupted/quota outcomes or existing typed exceptions. Permanent errors must not become unbounded transport retry; uncertain errors enter reconciliation with evidence.
4. Preserve status/dashboard representation and artifact integrity. Sanitize secrets and bound large tool outputs.

### D. Quota model adaptation (critical)

The SDK exposes RateLimitEvent status, optional utilization, windows and reset times, but does **not** document the same standalone authoritative account/rateLimits/read RPC Codex currently uses. Do not assume SDK events are a complete/cheap preflight quota read.

1. Add the minimum provider-neutral admission capability needed to preserve Codex's authoritative pre-turn rereads and support Claude's event/rejection-based telemetry. Missing Claude percentages/buckets remain **unknown**, never 100% available. Keep Codex's 300-minute/10080-minute safety thresholds and all existing quota tests unchanged.
2. Translate SDK rate-limit events to durable known status and reset/cooldown evidence; distinguish 429/quota from billing/auth and local budget caps. Persist known rejection across scheduler restart. Avoid polling with paid inference merely to sample quota.
3. After a known reset/cooldown, a controlled real attempt may probe availability; elapsed time alone must not be labeled a verified quota restoration. Prevent perpetual WAITING_QUOTA and rapid retry loops. Surface uncertainty honestly in status/events.
4. No automatic fallback or switching between providers on quota exhaustion.

### E. Verification, docs and acceptance

Implement deterministic mock/fixture tests without network access or credentials, including:

- CLI selects Claude and Codex; provider configuration snapshots; invalid/absent SDK, key and model errors; default Codex-only compatibility.
- Claude plan -> validated task DAG -> sequential worker JSON -> independent validators -> completion in isolated temp database/workspace.
- Session creation, subsequent turns, same-session recheck, interrupted turns, distinct owner session isolation, restart resuming exact session and missing transcript.
- Tool operation start/completion and uncertain/crash interruption; persistent evidence and no duplicate side effects; serializable bounded events and redaction.
- Pause/resume/stop/interrupt, timeout, error classification, close and child-process cleanup (including Windows test).
- Allowed/warning/rejected rate-limit events; missing utilization; reset/cooldown and restart; controlled probe; authentication/billing failure; unchanged Codex quota thresholds and reread behavior.
- Existing autonomous operational mode, status/dashboard, service ownership and legacy database regressions.

Run and record results of these existing verification commands:

~~~powershell
.venv/Scripts/python -m pytest tests -q -m "not live"
.venv/Scripts/python scripts/test_m1_recheck_startup.py
.venv/Scripts/python scripts/test_m1_integrated_cli.py
~~~

Add a **separate opt-in** live Claude integration test exercising a small real file edit, independent validation and a subsequent same-session turn; require an operator-provided ANTHROPIC_API_KEY and bound its model/turn budget. Do not attempt live tests by default. If no key is provided, deterministic acceptance still runs, and live status must be explicitly reported as NOT RUN (not falsely passed). Never mutate production objective state or use a consumer repo as a test fixture.

Update README/install/config/verification instructions, example configuration and affected provider/quota/recovery docs. Remove obsolete documentation claiming the CLI can only use Codex. Include API billing prerequisites and local session transcript persistence requirements. Do not add unnecessary docs or duplicate detailed scopes.

## Acceptance checklist

- [ ] Public CLI and Scheduler can run the same schema-compliant objective on Claude or Codex, with persistent provider identity/config and no regression to default Codex mode.
- [ ] Claude can perform file/shell tool calls without approval deadlock in documented yolo mode and produce parseable planning and worker handoffs.
- [ ] Existing validator requirements, DAG checks, recovery and operational workflows continue to work; no task schema changes or parallel supervisor.
- [ ] Durable SDK session IDs and turn/tool evidence enable same-session continuation after restart, human recheck and uncertain operations without automatic side-effect replay.
- [ ] Stop/pause/resume/timeout, error classification, Windows subprocess-tree closure, and lock ownership are exercised with deterministic tests.
- [ ] Missing quota is not fabricated; known rejection persists across restarts; retry/probe logic is bounded, billing/auth errors are correctly classified; Codex quota logic remains intact.
- [ ] Claude dependency is optional; credentials are neither committed nor persisted; failures provide actionable diagnostics.
- [ ] Non-live test suite and integrated CLI runners pass (or genuine failures are fixed without weakening assertions); optional live test status is accurately reported.
- [ ] Documentation contains exact installation, provider selection, authentication, execution and verification commands; no unfinished implementation stubs.

## Dependencies and risks

- **API account/key**: SDK's documented authentication uses ANTHROPIC_API_KEY with API billing. A Claude Pro/Max subscription is not an interchangeable API credit balance. User-provided key is necessary for optional live verification, not deterministic development.
- **Quota visibility**: SDK RateLimitEvent telemetry is not a reliable pre-turn usage query. A small, carefully tested change to provider-neutral admission is expected.
- **Session durability**: Claude resumes from its local transcript storage; SQLite metadata alone does not reconstruct deleted/moved transcripts. Document and test local-storage dependence.
- **Windows**: SDK starts/owns subprocesses; job cleanup and prompt interruption need empirical verification.

Official references:
- https://code.claude.com/docs/en/agent-sdk/overview
- https://code.claude.com/docs/en/agent-sdk/quickstart
- https://code.claude.com/docs/en/agent-sdk/python
- https://code.claude.com/docs/en/agent-sdk/sessions
