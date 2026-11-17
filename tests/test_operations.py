"""Operational lifecycle uses isolated fixtures, real child checks, no live inference."""
import json
import subprocess
import sys
import time

import pytest

from agent_orchestrator import operations
from agent_orchestrator.backends.base import Outcome, Quota
from agent_orchestrator.database import Database
from agent_orchestrator.scheduler import Scheduler
from test_lifecycle import AlternativeBackend


def outcome(status, **fields):
    return Outcome('completed', json.dumps({'status': status, **fields}))


@pytest.fixture
def runtime(tmp_path):
    db = Database(tmp_path / 'state.db')
    backend = AlternativeBackend()
    oid = db.submit('Repair faults and run genuine healthy work', tmp_path, 'fixture', {},
                    autonomous=True, source_file=str(tmp_path / 'objective.md'), healthy_seconds=40)
    scheduler = Scheduler(db, {'fixture': lambda config: backend})
    clock = [100.0]
    scheduler.monotonic = lambda: clock[0]
    scheduler.tick()  # Fixed operational task, no inference planning.
    key = oid + '/operate'
    state = tmp_path / 'health.json'
    state.write_text(json.dumps({'status': 'healthy', 'generation': 'run1'}))
    check = [sys.executable, '-c', "from pathlib import Path; print(Path('health.json').read_text())"]
    stop = [sys.executable, '-c', "from pathlib import Path; Path('stopped').write_text('confirmed')"]
    monitor = {'check': {'command': check, 'timeout': 2},
               'stop': {'command': stop, 'timeout': 2}, 'interval': 10}
    yield db, backend, scheduler, oid, key, state, monitor, clock
    scheduler.close()
    db.close()


def due(db, key):
    db.execute('UPDATE operation SET next_at=0,retry_at=0 WHERE task_key=?', (key,))


def start(r):
    db, backend, scheduler, oid, key, state, monitor, clock = r
    backend.script.append(outcome('monitoring', monitor=monitor))
    scheduler.tick()
    assert db.get('task', key)['state'] == 'MONITORING'


def test_monitor_checks_without_provider_or_quota(runtime):
    start(runtime)
    db, backend, scheduler, oid, key, state, monitor, clock = runtime
    backend.limits = Quota(allowed=False)
    for _ in range(3):
        clock[0] += 10
        due(db, key)
        scheduler.tick()
    assert len(backend.calls) == 1
    assert operations.row(db, key)['healthy'] == 30
    assert not (state.parent / 'stopped').exists()


def test_failed_health_stops_then_quota_automatically_resumes_same_session(runtime):
    start(runtime)
    db, backend, scheduler, oid, key, state, monitor, clock = runtime
    original_session = db.get('task', key)['session']
    state.write_text(json.dumps({'status': 'repair', 'generation': 'run1', 'diagnostic': '401 streak'}))
    backend.limits = Quota(allowed=False)
    due(db, key)
    scheduler.tick()
    assert (state.parent / 'stopped').read_text() == 'confirmed'
    assert operations.row(db, key)['healthy'] == 0
    due(db, key)
    scheduler.tick()
    assert db.get('task', key)['state'] == 'WAITING_QUOTA'
    assert len(backend.calls) == 1
    backend.limits = Quota()
    backend.script.append(outcome('continue', diagnostic='Fixed auth; reconcile before restart'))
    due(db, key)
    scheduler.tick()
    assert len(backend.calls) == 2
    assert db.get('task', key)['session'] == original_session
    assert 'never replay writes' in backend.calls[-1][1]


def test_ordinary_failures_do_not_block_after_three_unchanged_checkpoints(runtime):
    db, backend, scheduler, oid, key, *_ = runtime
    for status in ['blocked', 'unresolved', 'blocked', 'continue']:
        backend.script.append(outcome(status, diagnostic='Investigate a different safe hypothesis'))
        due(db, key)
        scheduler.tick()
        assert db.get('task', key)['state'] == 'FIXING_PERSISTENT_FAILURE'
    assert len({session for session, _ in backend.calls}) == 1
    assert operations.row(db, key)['assessment']


def test_genuine_external_barrier_requires_separate_assessment_and_evidence(runtime):
    db, backend, scheduler, oid, key, state, *_ = runtime
    evidence = state.parent / 'challenge.txt'
    evidence.write_text('Official EA UI requires security challenge; no authorized bypass')
    result = dict(diagnostic='Human security challenge', escalation={
        'category': 'human_challenge', 'attempted_actions': ['Normal saved login reached challenge'],
        'remaining_safe_actions': [], 'evidence_files': [str(evidence)],
        'required_change': 'User completes EA challenge', 'safe_state': 'Consumer stopped; no unknown write replay'})
    backend.script.append(outcome('blocked', **result))
    scheduler.tick()
    assert db.get('task', key)['state'] != 'BLOCKED'
    backend.script.append(outcome('blocked', **result))
    due(db, key)
    scheduler.tick()
    assert db.get('task', key)['state'] == 'BLOCKED'
    assert db.rows("SELECT * FROM event WHERE owner=? AND kind='recovery_assessment'", (key,))


@pytest.mark.parametrize('change', ['generation', 'restart', 'gap', 'fault'])
def test_consecutive_health_resets_on_actual_discontinuity(runtime, change):
    start(runtime)
    db, backend, scheduler, oid, key, state, monitor, clock = runtime
    clock[0] += 10
    due(db, key)
    scheduler.tick()
    assert operations.row(db, key)['healthy'] == 10
    if change == 'generation':
        state.write_text(json.dumps({'status': 'healthy', 'generation': 'run2'}))
    elif change == 'restart':
        scheduler.close()
        scheduler = Scheduler(db, {'fixture': lambda config: backend})
        scheduler.monotonic = lambda: clock[0]
    elif change == 'gap':
        clock[0] += 100
    else:
        state.write_text(json.dumps({'status': 'repair', 'generation': 'run1'}))
    clock[0] += 10
    due(db, key)
    scheduler.tick()
    assert operations.row(db, key)['healthy'] == 0
    if change == 'restart':
        scheduler.close()


@pytest.mark.parametrize('output', ['broken-json', '{}', '[]', '{"status":"healthy"}',
                                   '{"status":"PASS","generation":"x"}'])
def test_malformed_health_cannot_count_as_success(runtime, output):
    start(runtime)
    db, backend, scheduler, oid, key, state, *_ = runtime
    state.write_text(output)
    due(db, key)
    scheduler.tick()
    assert (state.parent / 'stopped').exists()
    assert operations.row(db, key)['healthy'] == 0
    assert operations.row(db, key)['pending']


def test_premature_done_cannot_bypass_required_interval(runtime):
    db, backend, scheduler, oid, key, *_ = runtime
    backend.script.append(outcome('done', validators=[]))
    scheduler.tick()
    assert db.get('task', key)['state'] == 'FIXING_PERSISTENT_FAILURE'
    assert 'Premature completion' in db.get('task', key)['diagnostic']


def test_interval_finalizes_only_after_independent_validation(runtime):
    start(runtime)
    db, backend, scheduler, oid, key, state, monitor, clock = runtime
    for _ in range(4):
        clock[0] += 10
        due(db, key)
        if clock[0] == 140:
            backend.script.append(outcome('done', validators=[{
                'command': [sys.executable, '-c', "import json; assert json.load(open('health.json'))['status']=='healthy'"],
                'criteria': [0, 1], 'timeout': 2}]))
        scheduler.tick()
    assert db.get('task', key)['state'] == 'COMPLETED'
    scheduler.tick()
    assert db.get('objective', oid)['state'] == 'COMPLETED'
    assert not (state.parent / 'stopped').exists()


@pytest.mark.parametrize('action', ['pause', 'stop'])
def test_explicit_controls_stop_only_owned_runtime(runtime, action):
    start(runtime)
    db, backend, scheduler, oid, key, state, *_ = runtime
    db.control(oid, action)
    scheduler.tick()
    assert (state.parent / 'stopped').exists()
    assert len(backend.calls) == 1


def test_scheduler_owned_launch_survives_backend_close_and_never_duplicates(runtime):
    db, backend, scheduler, oid, key, state, *_ = runtime
    service = {'id': 'launch1', 'command': [sys.executable, '-c',
        "import time; from pathlib import Path; Path('started').write_text('yes'); "
        "\nwhile not Path('exit').exists(): time.sleep(.05)"]}
    child = operations.launch(db, key, str(state.parent), service)
    try:
        deadline = time.monotonic() + 5
        while not (state.parent / 'started').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (state.parent / 'started').exists()
        scheduler.backends[oid] = backend
        scheduler.close()
        assert child.poll() is None
        assert operations.launch(db, key, str(state.parent), service) is None
        assert db.rows('SELECT count(*) AS n FROM operational_launch')[0]['n'] == 1
    finally:
        (state.parent / 'exit').write_text('stop')
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def test_unknown_launch_intent_is_not_replayed(runtime):
    db, _, _, _, key, state, *_ = runtime
    operations.launch(db, key, str(state.parent), {'id': 'seed', 'command': [sys.executable, '-c', 'pass']}).wait()
    db.execute('INSERT INTO operational_launch VALUES(?,?,?,?,?)', (key, 'uncertain', 'intent', None, 'x'))
    assert operations.launch(db, key, str(state.parent),
                             {'id': 'uncertain', 'command': ['nonexistent-command']}) is None


def test_health_poll_continues_during_worker_turn(runtime):
    start(runtime)
    db, backend, scheduler, oid, key, state, monitor, clock = runtime
    operations.retry(db, key, 'Inspection needed', delay=0)
    def during_turn(control):
        state.write_text(json.dumps({'status': 'repair', 'generation': 'run1'}))
        due(db, key)
        assert control() is None
        assert (state.parent / 'stopped').exists()
    backend.hook = during_turn
    backend.script.append(outcome('continue', diagnostic='Diagnosing fault'))
    scheduler.tick()
    assert operations.row(db, key)['healthy'] == 0


def test_explicit_pause_during_turn_prevents_service_dispatch(runtime):
    db, backend, scheduler, oid, key, state, monitor, _ = runtime
    backend.hook = lambda control: db.control(oid, 'pause')
    backend.script.append(outcome('monitoring', monitor=monitor, service={
        'id': 'must-not-start', 'command': [sys.executable, '-c', "open('unexpected','w').close()"]}))
    scheduler.tick()
    assert db.get('task', key)['state'] == 'PAUSED'
    assert not (state.parent / 'unexpected').exists()


def test_restart_preserves_fault_session_and_auto_quota_recovery(runtime):
    db, backend, scheduler, oid, key, state, *_ = runtime
    backend.script.append(Outcome('quota', diagnostic='usage limit'))
    scheduler.tick()
    saved = db.get('task', key)['session']
    scheduler.close()
    resumed = Scheduler(db, {'fixture': lambda config: backend})
    backend.script.append(outcome('continue', diagnostic='Resumed without human recheck'))
    resumed.tick()
    assert db.get('task', key)['session'] == saved
    assert db.get('task', key)['state'] == 'FIXING_PERSISTENT_FAILURE'
    assert 'Reconcile in this SAME session' in backend.calls[-1][1]
    resumed.close()


def test_final_validator_failure_keeps_repairing(runtime):
    start(runtime)
    db, backend, scheduler, oid, key, state, monitor, clock = runtime
    for _ in range(4):
        clock[0] += 10
        due(db, key)
        if clock[0] == 140:
            backend.script.append(outcome('done', validators=[{
                'command': [sys.executable, '-c', 'raise SystemExit(1)'], 'criteria': [0, 1]}]))
        scheduler.tick()
    assert db.get('task', key)['state'] == 'FIXING_PERSISTENT_FAILURE'
    assert db.get('objective', oid)['state'] != 'COMPLETED'


def test_continue_launch_allows_browser_attachment_without_counting_startup(runtime):
    db, backend, scheduler, oid, key, state, monitor, clock = runtime
    state.write_text(json.dumps({'status': 'starting', 'generation': 'run1'}))
    backend.script.append(outcome('continue', diagnostic='Attach browser to newly started service',
        monitor=monitor, service={'id': 'startup1', 'command': [sys.executable, '-c', 'pass']}))
    scheduler.tick()
    assert operations.row(db, key)['healthy'] == 0
    assert not (state.parent / 'stopped').exists()
    assert db.get('task', key)['state'] != 'BLOCKED'
    state.write_text(json.dumps({'status': 'healthy', 'generation': 'run1'}))
    backend.script.append(outcome('monitoring', monitor=monitor))
    clock[0] += 10
    scheduler.tick()
    assert db.get('task', key)['state'] == 'MONITORING'
    assert operations.row(db, key)['healthy'] == 0
    assert len(backend.calls) == 2
    for child in scheduler.consumers:
        child.wait(timeout=5)


def test_startup_cannot_be_extended_by_repeated_handoff_or_consumed_launch(runtime):
    db, backend, scheduler, oid, key, state, monitor, clock = runtime
    state.write_text(json.dumps({'status': 'starting', 'generation': 'run1'}))
    service = {'id': 'startup1', 'command': [sys.executable, '-c', 'pass']}
    backend.script.append(outcome('monitoring', monitor=monitor, service=service))
    scheduler.tick()
    db.execute('UPDATE operation SET startup_until=0 WHERE task_key=?', (key,))
    backend.script.append(outcome('monitoring', monitor=monitor, service=service))
    operations.retry(db, key, 'Inspect startup', delay=0)
    scheduler.tick()
    assert (state.parent / 'stopped').exists()
    assert operations.row(db, key)['healthy'] == 0
    assert len(scheduler.consumers) == 1
    for child in scheduler.consumers:
        child.wait(timeout=5)


def test_nonzero_health_exit_cannot_claim_healthy(runtime):
    db, backend, scheduler, oid, key, state, monitor, _ = runtime
    monitor['check']['command'] = [sys.executable, '-c',
        "import json; print(json.dumps({'status':'healthy','generation':'run1'})); raise SystemExit(1)"]
    backend.script.append(outcome('monitoring', monitor=monitor))
    scheduler.tick()
    assert (state.parent / 'stopped').exists()
    assert operations.row(db, key)['healthy'] == 0
