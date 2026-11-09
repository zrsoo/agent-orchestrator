"""Recovery payload regressions; no real provider turns or production DB writes."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from agent_orchestrator.backends.base import InputSizeError
from agent_orchestrator.backends.codex import CodexBackend
from agent_orchestrator.database import Database
from agent_orchestrator.executor import checkpoint
from agent_orchestrator.models import graph
from agent_orchestrator.prompts import MAX_PROMPT_CHARS, check_prompt, recovery_summary
from agent_orchestrator.scheduler import Scheduler
from test_lifecycle import AlternativeBackend, spec


@pytest.fixture
def recovery(tmp_path):
    db = Database(tmp_path / 'state.db')
    backend = AlternativeBackend()
    obj = db.submit('preserve the objective', tmp_path, 'alternative', {})
    db.save_graph(obj, graph([spec(tmp_path)]))
    key = obj + '/a'
    db.execute("UPDATE task SET state='RECONCILING',session='existing-session',infra_retries=4 WHERE key=?", (key,))
    db.execute('INSERT INTO turn(owner,session,provider_turn,kind,status,started) VALUES(?,?,?,?,?,?)',
               (key, 'existing-session', 'previous-provider-turn', 'RUNNING', 'interrupted', time.time()))
    scheduler = Scheduler(db, {'alternative': lambda c: backend})
    yield db, backend, obj, key, scheduler
    scheduler.close()
    db.close()


def artifact(db, key, data):
    return db.execute('INSERT INTO artifact(owner,at,kind,data) VALUES(?,?,?,?)',
                      (key, time.time(), 'checkpoint', json.dumps(data))).lastrowid


def test_multimegabyte_evidence_stays_referenced_and_side_effect_is_preserved(recovery, tmp_path):
    db, backend, obj, key, scheduler = recovery
    diff = '+large-diff-evidence\n' * 200_000
    output = 'large-tool-output\n' * 200_000
    data = {'stage': 'uncertain', 'revision': 'original-git-revision',
            'files': {'side-effect.txt': 'hash'}, 'changed': [],
            'status': ' M tracked.txt\n?? side-effect.txt\n', 'diff': diff}
    aid = artifact(db, key, data)
    db.event(key, 'item/completed', {'threadId': 'existing-session', 'turnId': 'previous-provider-turn',
             'item': {'id': 'completed-command', 'type': 'commandExecution', 'status': 'completed',
                      'command': 'publish-once --existing-artifact', 'exitCode': 0, 'aggregatedOutput': output}})
    event = db.rows("SELECT * FROM event WHERE kind='item/completed'")[0]
    marker = tmp_path / 'side-effect.txt'
    marker.write_text('already published')
    for i in range(100):
        db.event(key, 'infrastructure_failure', {'retry': i, 'retry_at': 0, 'error': 'transport failed\n' * 10_000})
    before = recovery_summary(db, db.get('objective', obj), db.get('task', key))
    assert before == recovery_summary(db, db.get('objective', obj), db.get('task', key))
    def preserve_side_effect(control):
        assert marker.read_text() == 'already published'
    backend.hook = preserve_side_effect
    scheduler.tick()
    prompt = backend.calls[0][1]
    assert len(prompt) < 25_000 < MAX_PROMPT_CHARS
    assert diff not in prompt and output not in prompt
    assert 'large-tool-output' not in prompt and 'large-diff-evidence' not in prompt
    for needed in (obj, key, 'existing-session', 'previous-provider-turn', json.dumps(str(db.path)),
                   'original-git-revision', 'tracked.txt', 'side-effect.txt',
                   'publish-once --existing-artifact', f'artifact:{aid}', f'event:{event["id"]}',
                   'Avoid repeating completed side effects', 'actual destination', '?mode=ro'):
        assert needed in prompt
    assert '"records": 100' in prompt
    assert backend.sessions == [('existing-session', 'existing-session')]
    assert marker.read_text() == 'already published'
    assert db.get('task', key)['state'] == 'COMPLETED'
    assert json.loads(db.rows('SELECT data FROM artifact WHERE id=?', (aid,))[0]['data'])['diff'] == diff
    assert db.rows('SELECT * FROM event WHERE id=?', (event['id'],))[0] == event
    assert len(db.rows("SELECT id FROM event WHERE kind='infrastructure_failure'")) == 100


def test_large_operation_fields_are_explicit_previews_with_full_row_reference(recovery):
    db, backend, obj, key, scheduler = recovery
    for kind, extra in (
        ('commandExecution', {'command': 'do-once ' * 200_000, 'aggregatedOutput': 'output' * 300_000}),
        ('fileChange', {'changes': [{'path': 'changed.txt', 'diff': 'patch' * 300_000}]}),
        ('mcpToolCall', {'server': 'studio', 'tool': 'publish', 'arguments': {'payload': 'argument' * 200_000},
                         'result': {'content': 'result' * 300_000}}),
    ):
        db.event(key, 'item/completed', {'item': {'type': kind, 'status': 'completed', **extra}})
    scheduler.tick()
    prompt = backend.calls[0][1]
    assert len(prompt) < 15_000
    assert '[preview; full length' in prompt
    assert 'changed.txt' in prompt and 'studio' in prompt and 'publish' in prompt
    assert 'argumentargument' not in prompt and 'resultresult' not in prompt


@pytest.mark.parametrize('field', ['objective', 'acceptance', 'inputs', 'diagnostic'])
def test_required_content_is_never_truncated_or_transport_retried(recovery, field):
    db, backend, obj, key, scheduler = recovery
    task = db.get('task', key)
    raw = json.loads(task['spec'])
    required = 'REQUIRED-' + 'x' * MAX_PROMPT_CHARS
    if field == 'diagnostic':
        db.execute('UPDATE task SET attempts=1,diagnostic=? WHERE key=?', (required, key))
    else:
        raw[field] = [required] if field in ('acceptance', 'inputs') else required
        db.execute('UPDATE task SET spec=? WHERE key=?', (json.dumps(raw), key))
    previous_turns = db.rows('SELECT * FROM turn')
    scheduler.tick()
    task = db.get('task', key)
    assert task['state'] == 'BLOCKED'
    assert 'Required task/objective text' in task['diagnostic']
    assert 'recheck the existing blocked task' in task['diagnostic']
    assert task['session'] == 'existing-session' and task['infra_retries'] == 4
    if field != 'diagnostic':
        assert json.loads(task['spec']) == raw
    else:
        rejected = db.rows("SELECT data FROM event WHERE kind='input_rejected'")
        assert json.loads(rejected[0]['data'])['prior_diagnostic'] == required
    assert not backend.calls and not backend.sessions
    assert db.rows('SELECT * FROM turn') == previous_turns
    assert not db.rows("SELECT * FROM event WHERE kind='infrastructure_failure'")
    for _ in range(3):
        scheduler.tick()
    assert not backend.calls
    assert len(db.rows("SELECT * FROM event WHERE kind='input_rejected'")) == 1


def test_limit_covers_combined_sections_and_exact_boundary(recovery):
    db, backend, obj, key, scheduler = recovery
    check_prompt('x' * MAX_PROMPT_CHARS)
    with pytest.raises(InputSizeError):
        check_prompt('x' * (MAX_PROMPT_CHARS + 1))
    raw = json.loads(db.get('task', key)['spec'])
    raw['objective'] = 'x' * (MAX_PROMPT_CHARS // 2)
    db.execute('UPDATE task SET spec=?,attempts=1,diagnostic=? WHERE key=?',
               (json.dumps(raw), 'd' * (MAX_PROMPT_CHARS // 2), key))
    scheduler.tick()
    assert db.get('task', key)['state'] == 'BLOCKED'
    assert not backend.calls


def test_planning_limit_does_not_create_session_or_loop(recovery):
    db, backend, obj, key, scheduler = recovery
    planning = db.submit('p' * MAX_PROMPT_CHARS, db.path.parent, 'alternative', {})
    scheduler.tick()  # finish the existing recovery task first
    scheduler.tick()
    assert db.get('objective', planning)['state'] == 'BLOCKED'
    assert db.get('objective', planning)['session'] is None
    assert len(backend.calls) == 1


@pytest.mark.parametrize('error', [
    {'code': -32600, 'message': 'input_too_large: max 1048576 characters, got 4625433'},
    {'code': -32600, 'message': 'Invalid request: input too large'},
    {'code': -32600, 'data': {'codexErrorInfo': 'inputTooLarge'}},
])
def test_rpc_size_rejection_is_non_transient(error):
    backend = CodexBackend.__new__(CodexBackend)
    backend.config = {}
    backend.sequence = 0
    backend._send = lambda msg: None
    backend._receive = lambda timeout: {'id': 1, 'error': error}
    with pytest.raises(InputSizeError, match='not transport-retryable'):
        backend._rpc('turn/start', {})


def test_backend_guard_precedes_rpc_and_async_rejection_is_non_transient():
    backend = CodexBackend.__new__(CodexBackend)
    backend.pending = []
    backend.config = {}
    calls = []
    backend._rpc = lambda *args: calls.append(args) or {'turn': {'id': 'turn'}}
    with pytest.raises(InputSizeError):
        backend.run('existing', 'x' * (MAX_PROMPT_CHARS + 1), lambda *a: None, lambda: None)
    assert not calls
    backend._receive = lambda timeout: {'method': 'turn/completed', 'params': {
        'turn': {'id': 'turn', 'status': 'failed', 'error': {'codexErrorInfo': 'inputTooLarge'}}}}
    with pytest.raises(InputSizeError):
        backend.run('existing', 'small', lambda *a: None, lambda: None)


def test_server_rejection_records_rejected_turn_without_infra_retry(recovery):
    db, backend, obj, key, scheduler = recovery
    backend.script = [InputSizeError('input_too_large; reduce input and recheck existing task')]
    scheduler.tick()
    assert db.get('task', key)['state'] == 'BLOCKED'
    assert db.get('task', key)['infra_retries'] == 4
    assert db.get('task', key)['attempts'] == 0
    assert db.rows('SELECT status FROM turn ORDER BY id DESC LIMIT 1')[0]['status'] == 'rejected'
    for _ in range(3):
        scheduler.tick()
    assert len(backend.calls) == 1


def test_wire_size_rejection_blocks_once_in_same_resumed_session(tmp_path):
    fixture = Path(__file__).parent / 'fixtures' / 'app_server.py'
    db = Database(tmp_path / 'state.db')
    obj = db.submit('recover existing work', tmp_path, 'codex',
                    {'executable': [sys.executable, str(fixture)], 'rpc_timeout': 5})
    db.save_graph(obj, graph([spec(tmp_path)]))
    key = obj + '/a'
    db.execute("UPDATE task SET state='RECONCILING',session='saved-codex-session' WHERE key=?", (key,))
    (tmp_path / 'reject-input').touch()
    scheduler = Scheduler(db)
    try:
        scheduler.tick()
        rejected = json.loads((tmp_path / 'rejected-input.json').read_text())
        assert rejected['threadId'] == 'saved-codex-session'
        assert rejected['prompt_chars'] < MAX_PROMPT_CHARS
        assert db.get('task', key)['state'] == 'BLOCKED'
        assert db.get('task', key)['infra_retries'] == 0
        assert db.rows('SELECT status FROM turn')[0]['status'] == 'rejected'
        for _ in range(3):
            scheduler.tick()
        assert json.loads((tmp_path / 'rejected-input.json').read_text())['requests'] == 1
        assert not (tmp_path / 'output.txt').exists()
    finally:
        scheduler.close()
        db.close()


def test_checkpoint_keeps_large_diff_and_dirty_names_across_identical_snapshots(tmp_path):
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    git('init')
    (tmp_path / '.gitignore').write_text('.orchestrator/\n')
    (tmp_path / 'tracked.txt').write_text('base\n')
    git('add', 'tracked.txt', '.gitignore')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'base')
    (tmp_path / 'tracked.txt').write_text('diff\n' * 700_000)
    (tmp_path / 'untracked.txt').write_text('exists')
    db = Database(tmp_path / '.orchestrator' / 'state.db')
    try:
        first = checkpoint(db, 'task', tmp_path, 'uncertain')
        second = checkpoint(db, 'task', tmp_path, 'before_turn')
        assert len(first['diff']) > 3_000_000
        assert second['diff'] == first['diff']
        assert second['changed'] == []
        assert second['dirty_files'] == ['tracked.txt', 'untracked.txt']
        assert first['revision'] == git('rev-parse', 'HEAD').stdout.decode()
    finally:
        db.close()


def test_existing_incident_session_and_previous_turns_read_only(monkeypatch):
    path = os.environ.get('ORCHESTRATOR_RECOVERY_DB')
    if not path:
        pytest.skip('set ORCHESTRATOR_RECOVERY_DB to replay persisted incident without provider calls')
    db = Database.__new__(Database)
    db.path = Path(path).resolve()
    db.conn = sqlite3.connect(db.path.as_uri() + '?mode=ro', uri=True)
    db.conn.row_factory = sqlite3.Row
    key = 'obj-43726a1e659f/m0_private_publication'
    try:
        db.execute('BEGIN')  # Consistent read snapshot while the existing service remains untouched.
        task = db.get('task', key)
        obj = db.get('objective', task['objective_id'])
        previous = db.rows('SELECT * FROM turn WHERE owner=? ORDER BY id', (key,))
        assert len(previous) > 1 and task['session']
        assert any(t['provider_turn'] and t['session'] == task['session'] for t in previous)
        history = db.rows('SELECT * FROM task WHERE objective_id=? ORDER BY key', (obj['id'],))
        dependencies = db.rows('SELECT * FROM dependency ORDER BY task_key,depends_key')
        scheduler = Scheduler(db)
        monkeypatch.setattr(scheduler, 'backend', lambda obj: None)
        monkeypatch.setattr(scheduler, 'quota_ready', lambda *a: True)
        monkeypatch.setattr(db, 'transition', lambda *a: None)
        captured = []
        class Captured(Exception):
            pass
        def capture(obj, table, owner, workspace, prompt, kind):
            check_prompt(prompt)
            captured.append(prompt)
            raise Captured
        monkeypatch.setattr(scheduler, 'turn', capture)
        with pytest.raises(Captured):
            scheduler.execute_task(obj, task)
        prompt = captured[0]
        assert len(prompt) < 40_000
        assert json.dumps(json.loads(task['spec'])) in prompt
        assert task['session'] in prompt and key in prompt
        assert 'Avoid repeating completed side effects' in prompt
        assert json.dumps(str(db.path)) in prompt
        assert json.loads(task['spec'])['workspace'] in prompt.replace('\\\\', '\\')
        assert db.rows('SELECT * FROM turn WHERE owner=? ORDER BY id', (key,)) == previous
        assert db.rows('SELECT * FROM task WHERE objective_id=? ORDER BY key', (obj['id'],)) == history
        assert db.rows('SELECT * FROM dependency ORDER BY task_key,depends_key') == dependencies
        print(f'Existing-session recovery prompt: {len(prompt):,} characters; limit {MAX_PROMPT_CHARS:,}; prior turns {len(previous)}')
    finally:
        db.close()
