"""Human recovery uses the original graph, session and independent validators."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3

import pytest

from agent_orchestrator import validation
from agent_orchestrator.backends.base import Outcome, TransportError
from agent_orchestrator.cli import main
from agent_orchestrator.database import Database
from agent_orchestrator.scheduler import Scheduler
from agent_orchestrator.service import OwnerProbe, ServiceLock
from test_lifecycle import system, done, finish


def block(system, check=None, completed_first=False):
    db, backend, key, scheduler = system
    scheduler.tick()  # Persist the graph once.
    if completed_first:
        scheduler.tick()
    name = 'b' if completed_first else 'a'
    result = {'status': 'blocked', 'diagnostic': 'Human must create permission.txt'}
    if check:
        result['blocker_check'] = check
    backend.script = [Outcome('completed', json.dumps(result))]
    scheduler.tick()
    scheduler.tick()
    assert db.get('objective', key)['state'] == 'BLOCKED'
    return key+'/'+name


def check_command():
    return {'command': ['{python}', '-c',
        "import json; from pathlib import Path; print(json.dumps({'status': 'PASS' if Path('permission.txt').exists() else 'BLOCKED', 'reason':'permission.txt required'}))"]}


@pytest.mark.parametrize('via_task', [False, True])
def test_recheck_resolves_preserving_dag_work_and_sessions(system, via_task):
    db, backend, key, scheduler = system
    task = block(system, check_command(), completed_first=True)
    original = db.get('task', task)
    graph = db.rows('SELECT * FROM dependency')
    completed = db.get('task', key+'/a')
    prior_artifacts = db.rows('SELECT * FROM artifact')
    Path(db.get('objective', key)['workspace'], 'permission.txt').touch()
    before = len(backend.calls)
    assert db.recheck(task if via_task else key) == [task]
    assert db.recheck(key) == [task]
    assert len(db.rows("SELECT * FROM event WHERE kind='recheck_requested'")) == 1
    assert finish(scheduler, db, key) == 'COMPLETED'
    assert len(backend.calls) == before+1
    assert db.get('task', task)['session'] == original['session']
    assert db.get('task', task)['spec'] == original['spec']
    assert db.get('task', key+'/a') == completed
    assert db.rows('SELECT * FROM dependency') == graph
    assert db.rows('SELECT * FROM artifact')[:len(prior_artifacts)] == prior_artifacts
    assert len(db.rows("SELECT * FROM event WHERE kind='graph_persisted'")) == 1
    assert db.rows("SELECT * FROM artifact WHERE kind='blocker_check'")
    assert db.rows("SELECT * FROM artifact WHERE owner=? AND kind='validation'", (task,))
    scheduler.tick()
    assert len(backend.calls) == before+1


def test_zero_exit_blocked_remains_blocked_and_can_retry(system):
    db, backend, key, scheduler = system
    task = block(system, check_command())
    before = len(backend.calls)
    for _ in range(2):
        db.recheck(key)
        assert finish(scheduler, db, key) == 'BLOCKED'
        assert db.get('task', task)['recheck'] is None
        assert 'permission.txt required' in db.get('task', task)['diagnostic']
    assert len(backend.calls) == before  # No provider admission for unresolved command.
    assert db.get('task', key+'/b')['state'] == 'PENDING'
    Path(db.get('objective', key)['workspace'], 'permission.txt').touch()
    db.recheck(task)
    assert finish(scheduler, db, key) == 'COMPLETED'
    assert len(db.rows("SELECT * FROM event WHERE kind='recheck_requested'")) == 3
    assert len(backend.calls) == before+2  # a, then original dependent b.


def test_legacy_recheck_uses_same_session_and_checks_original_blocker_first(system):
    db, backend, key, scheduler = system
    task = block(system)
    session = db.get('task', task)['session']
    db.recheck(task)
    backend.script = [Outcome('completed', json.dumps({'status': 'blocked', 'diagnostic': 'Still missing permission'}))]
    assert finish(scheduler, db, key) == 'BLOCKED'
    assert backend.calls[-1][0] == session
    assert 'First verify the original blocker' in backend.calls[-1][1]
    assert 'Human must create permission.txt' in backend.calls[-1][1]
    assert db.get('task', task)['diagnostic'] == 'Still missing permission'
    db.recheck(key)
    assert finish(scheduler, db, key) == 'COMPLETED'
    assert db.get('task', task)['session'] == session


def test_resolved_check_does_not_skip_work_or_failed_acceptance(system):
    db, backend, key, scheduler = system
    task = block(system, {'command': ['{python}', '-c', 'pass']})
    db.recheck(task)
    backend.script = [done(fail=True)]
    scheduler.tick()
    assert db.get('task', task)['state'] != 'COMPLETED'
    assert db.get('task', key+'/b')['state'] == 'PENDING'
    assert db.get('task', task)['recheck'] == 'continue'
    assert db.rows("SELECT * FROM event WHERE kind='blocker_resolved'")
    results = db.rows("SELECT data FROM artifact WHERE owner=? AND kind='validation'", (task,))
    assert results and json.loads(results[-1]['data'])['exit_code'] == 1
    assert finish(scheduler, db, key) == 'COMPLETED'


def test_validator_zero_exit_cannot_override_blocked_json(system):
    db, backend, key, scheduler = system
    task = block(system)
    db.recheck(task)
    backend.script = [Outcome('completed', json.dumps({'status': 'done', 'validators': [
        {'command': ['{python}', '-c', 'print(\'{"status":"BLOCKED"}\')'], 'criteria': [0]}]}))]
    scheduler.tick()
    assert db.get('task', task)['state'] != 'COMPLETED'
    assert db.get('task', key+'/b')['state'] == 'PENDING'


def test_queued_recheck_survives_database_reopen(system):
    db, backend, key, scheduler = system
    task = block(system)
    session = db.get('task', task)['session']
    db.recheck(key)
    reopened = Database(db.path)
    restarted = Scheduler(reopened, {'alternative': lambda c: backend})
    try:
        assert finish(restarted, reopened, key) == 'COMPLETED'
        assert reopened.get('task', task)['session'] == session
    finally:
        restarted.close()
        reopened.close()


def test_interrupted_deterministic_check_is_retried_after_restart(system, monkeypatch):
    db, backend, key, scheduler = system
    task = block(system, check_command())
    Path(db.get('objective', key)['workspace'], 'permission.txt').touch()
    db.recheck(task)
    original = validation.command_result
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(validation, 'command_result', interrupt)
    with pytest.raises(KeyboardInterrupt):
        scheduler.tick()
    assert db.get('task', task)['recheck'] == 'verify'
    monkeypatch.setattr(validation, 'command_result', original)
    restarted = Scheduler(db, {'alternative': lambda c: backend})
    assert finish(restarted, db, key) == 'COMPLETED'
    assert len(db.rows("SELECT * FROM event WHERE kind='blocker_check_started'")) == 2
    assert len(db.rows("SELECT * FROM event WHERE kind='blocker_resolved'")) == 1


def test_interrupted_worker_reconciles_without_rerunning_successful_check(system):
    db, backend, key, scheduler = system
    task = block(system, {'command': ['{python}', '-c', 'pass']})
    session = db.get('task', task)['session']
    db.recheck(task)
    backend.script = [TransportError('process interrupted')]
    scheduler.tick()
    assert db.get('task', task)['recheck'] == 'continue'
    db.execute("UPDATE event SET data=json_set(data,'$.retry_at',0) WHERE kind='infrastructure_failure'")
    restarted = Scheduler(db, {'alternative': lambda c: backend})
    assert finish(restarted, db, key) == 'COMPLETED'
    assert backend.calls[-2][0] == session
    assert 'Avoid repeating completed side effects' in backend.calls[-2][1]
    assert len(db.rows("SELECT * FROM event WHERE kind='blocker_check_started'")) == 1


def test_concurrent_requests_queue_only_once(system):
    db, backend, key, scheduler = system
    task = block(system)
    def request(_):
        connection = Database(db.path)
        try:
            return connection.recheck(key)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(request, range(2))) == [[task], [task]]
    assert len(db.rows("SELECT * FROM event WHERE kind='recheck_requested'")) == 1
    assert finish(scheduler, db, key) == 'COMPLETED'


def test_recheck_cli_only_queues_and_preserves_controls(system, capsys):
    db, backend, key, scheduler = system
    task = block(system)
    before = len(backend.calls)
    with ServiceLock(db.path) as lock, OwnerProbe(db.path, lock):
        assert main(['--db', str(db.path), 'recheck', key]) == 0
    import os
    assert capsys.readouterr().out == f'Recheck queued; scheduler already running (PID {os.getpid()}).\n'
    assert len(backend.calls) == before
    db.control(key, 'pause')
    scheduler.tick()
    assert len(backend.calls) == before
    db.control(key, 'resume')
    assert finish(scheduler, db, key) == 'COMPLETED'
    assert main(['--db', str(db.path), 'recheck', task]) == 2


def test_stopped_records_remain_stopped(system):
    db, backend, key, scheduler = system
    task = block(system)
    db.recheck(task)
    db.control(key, 'stop')
    with pytest.raises(ValueError, match='stopped'):
        db.recheck(task)
    scheduler.tick()
    assert db.get('task', task)['state'] == 'STOPPED'


def test_additive_migration_preserves_old_database(tmp_path):
    path = tmp_path/'old.db'
    db = Database(path)
    key = db.submit('original', tmp_path, 'alternative', {})
    db.close()
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE task DROP COLUMN recheck')
        connection.execute('ALTER TABLE task DROP COLUMN blocker_check')
    reopened = Database(path)
    try:
        assert reopened.get('objective', key)['text'] == 'original'
        assert {'recheck', 'blocker_check'} <= {r['name'] for r in reopened.execute('PRAGMA table_info(task)')}
    finally:
        reopened.close()
