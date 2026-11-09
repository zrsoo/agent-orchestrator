import json
from dataclasses import asdict
from pathlib import Path
import sys
import pytest
from agent_orchestrator.backends.base import Bucket, Quota, Outcome, TransportError
from agent_orchestrator.backends.codex import normalize_quota
from agent_orchestrator.database import Database
from agent_orchestrator.models import graph
from agent_orchestrator.quota import admission, wake_delay
from agent_orchestrator.scheduler import Scheduler
from agent_orchestrator import validation
from polling import complete_objective


class AlternativeBackend:
    """No Codex types/fields; drives the unchanged scheduler."""
    def __init__(self, config=None):
        self.workspace = None
        self.calls = []
        self.limits = Quota()
        self.script = []
        self.hook = None
        self.sessions = []

    def quota(self):
        return self.limits

    def session(self, workspace, session_id=None):
        self.workspace = Path(workspace)
        sid = session_id or f'session-{len(self.sessions)}'
        self.sessions.append((sid, session_id))
        return sid

    def run(self, session_id, prompt, emit, control):
        self.calls.append((session_id, prompt))
        emit('turn_identity', {'session': session_id, 'turn': f'turn-{len(self.calls)}'})
        if self.hook:
            self.hook(control)
        if self.script:
            result = self.script.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if 'Do not implement it yet' in prompt:
            return Outcome('completed', json.dumps({'tasks': [spec(self.workspace, 'a'), spec(self.workspace, 'b', ['a'])]}))
        name = 'b' if '"id": "b"' in prompt else 'a'
        (self.workspace / f'{name}.txt').write_text(name)
        emit('item/completed', {'item': {'type': 'commandExecution', 'status': 'completed', 'command': f'write {name}'}})
        return done(name)

    def close(self):
        pass


def spec(workspace, name='a', deps=None):
    return {'id': name, 'workspace': str(workspace), 'objective': f'write {name}',
            'acceptance': [f'{name}.txt contains {name}'], 'depends_on': deps or []}


def done(name='a', fail=False):
    code = 'raise SystemExit(1)' if fail else f"from pathlib import Path; assert Path('{name}.txt').read_text() == '{name}'"
    return Outcome('completed', json.dumps({'status': 'done', 'validators': [
        {'command': ['{python}', '-c', code], 'criteria': [0]}]}))


@pytest.fixture
def system(tmp_path):
    db = Database(tmp_path / 'state.db')
    backend = AlternativeBackend()
    key = db.submit('write a then b', tmp_path, 'alternative', {'model': 'anything'})
    scheduler = Scheduler(db, {'alternative': lambda config: backend})
    yield db, backend, key, scheduler
    scheduler.close()
    db.close()


def finish(scheduler, db, key):
    for _ in range(12):
        scheduler.tick()
        if db.get('objective', key)['state'] in ('COMPLETED', 'BLOCKED', 'STOPPED'):
            return db.get('objective', key)['state']
    return db.get('objective', key)['state']


def test_alternative_submission_execution_graph_and_durable_ids(system):
    db, backend, key, scheduler = system
    assert finish(scheduler, db, key) == 'COMPLETED'
    assert len(backend.calls) == 3
    assert all(t['provider_turn'] and t['session'] for t in db.report()['turn'])
    assert len(db.report()['dependency']) == 1
    assert len([a for a in db.report()['artifact'] if a['kind'] == 'validation']) == 2
    scheduler.tick()
    assert len(backend.calls) == 3
    for action in ('pause','resume','stop'):
        with pytest.raises(ValueError,match='terminal'):
            db.control(key,action)


@pytest.mark.parametrize('mutate', [
    lambda x: x[0].update(model='bad'),
    lambda x: x[0].update(acceptance=[]),
    lambda x: x.append(dict(x[0])),
    lambda x: x[0].update(depends_on=['missing']),
    lambda x: x[0].update(depends_on=['a']),
    lambda x: x[0].update(workspace='relative'),
])
def test_graph_rejects_invalid(tmp_path, mutate):
    raw = [spec(tmp_path)]
    mutate(raw)
    with pytest.raises(ValueError):
        graph(raw)


@pytest.mark.parametrize('minutes,remaining,expected', [(300,1.9,False),(300,2,True),(10080,.9,False),(10080,1,True),(60,0,False),(60,.1,True)])
def test_all_reported_quota_windows(minutes, remaining, expected):
    raw = {'rateLimits': {'primary': {'usedPercent': 100-remaining, 'windowDurationMins': minutes, 'resetsAt': 100}}}
    assert admission(normalize_quota(raw)) is expected
    assert admission(Quota())
    assert not admission(Quota(allowed=False))


def test_additional_limit_ids_and_explicit_denial():
    normal = {'primary':{'usedPercent':10,'windowDurationMins':300}}
    exhausted = {'primary':{'usedPercent':100,'windowDurationMins':60}}
    assert not admission(normalize_quota({'rateLimitsByLimitId':{'codex':normal,'other':exhausted}}))
    assert not admission(normalize_quota({'ordinaryUsageAllowed':False,'rateLimits':normal}))
    assert not admission(normalize_quota({'rateLimits':{'spendControlReached':True}}))


@pytest.mark.parametrize('minutes,floor', [(300,2),(10080,1)])
def test_quota_pause_reset_reread(system, minutes, floor):
    db, backend, key, scheduler = system
    backend.limits = Quota([Bucket('window', floor-.1, minutes, 101, floor)])
    scheduler.tick()
    assert db.get('objective', key)['state'] == 'WAITING_QUOTA'
    assert not backend.calls
    # Clock passing reset does not grant permission; reread remains authoritative.
    assert wake_delay(backend.limits, now=102) == 60
    scheduler.tick()
    assert not backend.calls
    backend.limits = Quota([Bucket('window', 100, minutes, 200, floor)])
    assert finish(scheduler, db, key) == 'COMPLETED'


@pytest.mark.parametrize('reset,poll,expected', [
    (None, 60, 60), (999, 60, 60), (1020, 60, 20),
    (9999, 60, 60), (None, 10, 10), (None, 120, 60),
])
@pytest.mark.parametrize('notifications', [False, True])
def test_quota_wait_interval(system, monkeypatch, reset, poll, expected, notifications):
    db, backend, key, scheduler = system
    monkeypatch.setattr('agent_orchestrator.quota.time.time', lambda: 1000)
    quota = Quota([Bucket('weekly', 0, 10080, reset, 1)])
    db.event(key, 'quota', asdict(quota))
    delays = []
    if notifications:
        backend.wait_updates = delays.append
        scheduler.backends[key] = backend
    else:
        monkeypatch.setattr('agent_orchestrator.scheduler.time.sleep', delays.append)
    scheduler.wait(poll)
    assert delays == [expected]
    assert not backend.calls


def test_quota_wait_pause_restart_resume_preserves_progress(system):
    db, backend, key, scheduler = system
    scheduler.tick()  # Plan.
    scheduler.tick()  # Complete a.
    backend.limits = Quota([Bucket('weekly', 0, 10080, None, 1)])
    scheduler.tick()  # b cannot start.
    assert db.get('objective', key)['state'] == 'WAITING_QUOTA'
    assert db.get('task', key+'/b')['state'] == 'WAITING_QUOTA'
    completed = db.get('task', key+'/a')
    dependencies = db.rows('SELECT * FROM dependency')
    turns = db.rows('SELECT * FROM turn')
    db.control(key, 'pause')
    scheduler.close()
    reopened = Database(db.path)
    restarted = Scheduler(reopened, {'alternative': lambda config: backend})
    try:
        restarted.tick()
        assert reopened.get('objective', key)['state'] == 'PAUSED'
        assert reopened.rows('SELECT * FROM turn') == turns
        assert reopened.get('task', key+'/a') == completed
        backend.limits = Quota()
        reopened.control(key, 'resume')
        assert finish(restarted, reopened, key) == 'COMPLETED'
        assert reopened.get('task', key+'/a') == completed
        assert reopened.rows('SELECT * FROM dependency') == dependencies
        assert len(backend.calls) == 3  # Plan, a, b; completed work did not rerun.
    finally:
        restarted.close()
        reopened.close()


def test_active_turn_finishes_at_threshold(system):
    db, backend, key, scheduler = system
    scheduler.tick()
    backend.hook = lambda control: setattr(backend, 'limits', Quota([Bucket('5h', 1,300,9999999999,2)]))
    scheduler.tick()
    assert db.get('task', key+'/a')['state'] == 'COMPLETED'
    scheduler.tick()
    assert db.get('task', key+'/b')['state'] == 'WAITING_QUOTA'
    assert len(backend.calls) == 2


def test_cutoff_preserves_files_reconciles_same_session(system):
    db, backend, key, scheduler = system
    scheduler.tick()
    path = Path(db.get('objective', key)['workspace']) / 'side-effect.txt'
    path.write_text('already done')
    backend.script = [Outcome('quota', diagnostic='cut off')]
    scheduler.tick()
    sid = db.get('task', key+'/a')['session']
    assert db.get('task', key+'/a')['state'] == 'WAITING_QUOTA'
    scheduler.tick()
    assert db.get('task', key+'/a')['state'] == 'COMPLETED'
    assert backend.calls[-1][0] == sid
    assert 'Avoid repeating completed side effects' in backend.calls[-1][1]
    assert path.read_text() == 'already done'
    assert db.get('task', key+'/a')['attempts'] == 0


def test_transport_crash_and_restart_reconcile(system):
    db, backend, key, scheduler = system
    scheduler.tick()
    backend.script = [TransportError('worker killed')]
    scheduler.tick()
    assert db.get('task', key+'/a')['infra_retries'] == 1
    assert db.get('task', key+'/a')['attempts'] == 0
    db.execute("UPDATE event SET data=json_set(data,'$.retry_at',0) WHERE kind='infrastructure_failure'")
    restarted = Scheduler(db, {'alternative': lambda c: backend})
    assert finish(restarted, db, key) == 'COMPLETED'
    assert 'SAME session' in backend.calls[2][1]


@pytest.mark.parametrize('restart', [False, True])
def test_recovery_waits_for_retry_then_validates_and_propagates(system, monkeypatch, restart):
    db, backend, key, scheduler = system
    db.save_graph(key, graph([spec(db.get('objective', key)['workspace'])]))
    now = [1000.0]
    monkeypatch.setattr('time.time', lambda: now[0])
    marker = Path(db.get('objective', key)['workspace'])/'side-effect.txt'
    effects = []
    def preserve_side_effect(control):
        if not marker.exists():
            marker.write_text('once')
            effects.append('created')
    backend.hook = preserve_side_effect
    backend.script = [TransportError('worker killed')]
    scheduler.tick()
    task_key = key+'/a'
    session = db.get('task', task_key)['session']
    if restart:
        scheduler = Scheduler(db, {'alternative': lambda c: backend})
    # Reproduce both old post-sleep ticks occurring before persisted retry eligibility.
    now[0] = 1001.99
    for _ in range(2):
        assert scheduler.tick() is False
    assert db.get('objective', key)['state'] == ('RECONCILING' if restart else 'RUNNING')
    assert db.get('task', task_key)['state'] == 'RECONCILING'
    assert len(backend.calls) == 1
    assert not db.rows("SELECT * FROM artifact WHERE kind='validation'")

    now[0] = 1002.0
    scheduler.tick()
    assert db.get('task', task_key)['state'] == 'COMPLETED'
    assert db.get('objective', key)['state'] == 'RUNNING'
    assert db.get('task', task_key)['session'] == session
    assert db.get('task', task_key)['attempts'] == 0
    assert len(backend.calls) == 2
    assert 'Avoid repeating completed side effects' in backend.calls[-1][1]
    results = db.rows("SELECT data FROM artifact WHERE kind='validation'")
    assert results and all(json.loads(r['data'])['exit_code'] == 0 for r in results)
    scheduler.tick()
    assert db.get('objective', key)['state'] == 'COMPLETED'
    scheduler.tick()
    assert len(backend.calls) == 2
    assert effects == ['created']
    assert marker.read_text() == 'once'
    assert len(db.rows("SELECT * FROM turn WHERE status='uncertain'")) == 1


@pytest.mark.parametrize('stuck', [False, True])
def test_completion_polling_delayed_retry_and_deadline(system, monkeypatch, stuck):
    db, backend, key, scheduler = system
    db.save_graph(key, graph([spec(db.get('objective', key)['workspace'])]))
    now = [1000.0]
    monkeypatch.setattr('time.time', lambda: now[0])
    monkeypatch.setattr('time.monotonic', lambda: now[0])
    monkeypatch.setattr('time.sleep', lambda seconds: now.__setitem__(0, now[0]+seconds))
    backend.script = [TransportError('worker killed')]
    scheduler.tick()
    session = db.get('task', key+'/a')['session']
    if stuck:
        backend.script = [TransportError('still broken') for _ in range(10)]
        with pytest.raises(AssertionError, match='did not complete within 5s'):
            complete_objective(scheduler, db, key, timeout=5)
        assert now[0] == 1005
        assert db.get('objective', key)['state'] != 'COMPLETED'
    else:
        complete_objective(scheduler, db, key, timeout=5)
        assert db.get('task', key+'/a')['session'] == session
        assert len(backend.calls) == 2
        transitions = [json.loads(r['data'])['to'] for r in db.rows(
            "SELECT data FROM event WHERE kind='transition' AND owner=? ORDER BY id", (key+'/a',))]
        assert transitions[-2:] == ['VALIDATING', 'COMPLETED']


def test_supervisor_uncertain_turn_no_duplicate_completed_task(system):
    db, backend, key, scheduler = system
    scheduler.tick()
    scheduler.tick()
    db.transition('task', key+'/b', 'RUNNING')
    db.execute("UPDATE task SET session='known-session' WHERE key=?", (key+'/b',))
    db.execute("INSERT INTO turn(owner,session,kind,status,started) VALUES(?,?,?,'running',0)", (key+'/b','known-session','RUNNING'))
    restarted = Scheduler(db, {'alternative': lambda c: backend})
    assert finish(restarted, db, key) == 'COMPLETED'
    assert len(backend.calls) == 3
    assert backend.calls[-1][0] == 'known-session'
    assert db.rows("SELECT * FROM turn WHERE status='uncertain'")


def test_failures_second_persistent_then_blocked(system):
    db, backend, key, scheduler = system
    scheduler.tick()
    backend.script = [Outcome('completed', json.dumps({'status':'unresolved','diagnostic':'same error'})) for _ in range(3)]
    scheduler.tick()
    assert db.get('task',key+'/a')['state'] == 'RUNNING'
    scheduler.tick()
    assert db.get('task',key+'/a')['state'] == 'FIXING_PERSISTENT_FAILURE'
    scheduler.tick()
    assert db.get('task',key+'/a')['state'] == 'BLOCKED'
    assert 'alternative approach' in backend.calls[-1][1]


def test_failure_episode_resolves_and_validation_failure_never_completes(system):
    db, backend, key, scheduler = system
    scheduler.tick()
    backend.script = [done(fail=True), done(fail=True)]
    scheduler.tick()
    assert db.get('task',key+'/a')['state'] == 'RUNNING'
    scheduler.tick()
    assert db.get('task',key+'/a')['state'] == 'FIXING_PERSISTENT_FAILURE'
    assert finish(scheduler, db, key) == 'COMPLETED'
    assert db.get('task',key+'/a')['attempts'] == 0


def test_in_turn_debugging_does_not_count(system):
    db, backend, key, scheduler = system
    scheduler.tick()
    backend.hook = lambda control: db.event(key+'/a','item/completed', {'exitCode':1,'then':'fixed inside same turn'})
    scheduler.tick()
    assert db.get('task',key+'/a')['attempts'] == 0


def test_pause_resume_stop_without_backend(system):
    db, backend, key, scheduler = system
    db.control(key,'pause')
    scheduler.tick()
    assert not backend.calls
    db.control(key,'resume')
    scheduler.tick()
    db.control(key+'/a','pause')
    scheduler.tick()
    assert len(backend.calls) == 1
    db.control(key+'/a','resume')
    scheduler.tick()
    db.control(key,'stop')
    scheduler.tick()
    assert db.get('objective',key)['state'] == 'STOPPED'
    assert db.get('task',key+'/b')['state'] == 'STOPPED'
    assert len(backend.calls) == 2


def test_pause_during_turn_then_resume_does_not_rerun_worker(system):
    db, backend, key, scheduler = system
    scheduler.tick()
    backend.hook = lambda control: db.control(key+'/a','pause')
    scheduler.tick()
    assert db.get('task',key+'/a')['state'] == 'PAUSED'
    backend.hook = None
    db.control(key+'/a','resume')
    scheduler.tick()
    assert db.get('task',key+'/a')['state'] == 'COMPLETED'
    assert len(backend.calls) == 2


def test_objective_pause_during_turn_resume_releases_inherited_pause(system):
    db, backend, key, scheduler = system
    scheduler.tick()
    backend.hook = lambda control: db.control(key, 'pause')
    scheduler.tick()
    scheduler.tick()
    assert db.get('objective', key)['state'] == 'PAUSED'
    assert db.get('task', key+'/a')['state'] == 'PAUSED'
    backend.hook = None
    db.control(key, 'resume')
    assert finish(scheduler, db, key) == 'COMPLETED'
    assert len(backend.calls) == 3


@pytest.mark.parametrize('outcome',[Outcome('quota'), done(fail=True)])
def test_task_pause_after_disrupted_turn_and_resume(system, outcome):
    db, backend, key, scheduler = system
    scheduler.tick()
    backend.hook = lambda control: db.control(key+'/a','pause')
    backend.script = [outcome]
    scheduler.tick()
    assert db.get('task',key+'/a')['state']=='PAUSED'
    backend.hook = None
    db.control(key+'/a','resume')
    scheduler.tick()
    if outcome.status == 'quota':
        assert 'You were cut off' in backend.calls[-1][1]
    assert db.get('task',key+'/a')['state']=='COMPLETED'


def test_validator_coverage_timeout_and_missing(system, tmp_path):
    db, backend, key, scheduler = system
    scheduler.tick()
    with pytest.raises(ValueError):
        validation.register(db,key+'/a',[{'command':['x'],'criteria':[]}],['one'])
    assert validation.run(db,key+'/a',tmp_path)[0] is False
    validation.register(db,key+'/a',[{'command':['{python}','-c','import time; time.sleep(5)'],'criteria':[0],'timeout':.1}],['one'])
    assert validation.run(db,key+'/a',tmp_path)[0] is False
