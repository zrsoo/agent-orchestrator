"""Migration acceptance against external Git workspaces, without live inference."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from agent_orchestrator.backends.base import Outcome, TransportError
from agent_orchestrator.database import Database
from agent_orchestrator.scheduler import Scheduler
from agent_orchestrator.service import OwnerProbe, ServiceLock
from test_lifecycle import AlternativeBackend, finish


def invoke(database, *args):
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-I', '-m', 'agent_orchestrator',
                             '--db', str(database), *args], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def inventory(workspace):
    # Exclude Git administrative state and files whose existence predates this test.
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others',
                                     '--exclude-standard', '-z'], cwd=workspace).decode().split('\0')
    return {name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
            for name in names if name and (workspace / name).is_file()}


class ReadOnlyBackend(AlternativeBackend):
    def run(self, session_id, prompt, emit, control):
        self.calls.append((session_id, prompt))
        emit('turn_identity', {'session': session_id, 'turn': 'external-' + str(len(self.calls))})
        if 'Do not implement it yet' in prompt:
            return Outcome('completed', json.dumps({'tasks': [{
                'id': 'inspect', 'workspace': str(self.workspace), 'objective': 'verify Git workspace',
                'acceptance': ['The task runs in the specified external Git workspace']}]}))
        check = 'import pathlib,subprocess; assert pathlib.Path.cwd().resolve() == pathlib.Path(' + repr(str(self.workspace)) + ').resolve(); subprocess.run(["git","rev-parse","--show-toplevel"],check=True)'
        return Outcome('completed', json.dumps({'status': 'done', 'validators': [
            {'command': ['{python}', '-c', check], 'criteria': [0]}]}))


def test_read_only_objective_targets_external_workspace(tmp_path):
    fallback = tmp_path / 'external'
    fallback.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(fallback)], check=True)
    workspace = Path(os.environ.get('ORCHESTRATOR_EXTERNAL_WORKSPACE', str(fallback))).resolve()
    before = inventory(workspace)
    database = tmp_path / 'state' / 'state.db'
    key = invoke(database, 'submit', '--text', 'Verify the selected Git workspace read-only',
                 '--workspace', str(workspace))
    db = Database(database)
    backend = ReadOnlyBackend()
    scheduler = Scheduler(db, {'codex': lambda config: backend})
    try:
        assert finish(scheduler, db, key) == 'COMPLETED'
        assert db.get('objective', key)['workspace'] == str(workspace)
        assert len(backend.calls) == 2
        assert len(db.rows("SELECT * FROM artifact WHERE kind='validation'")) == 1
        scheduler.close()
        scheduler = Scheduler(db, {'codex': lambda config: backend})
        scheduler.tick()
        assert len(backend.calls) == 2, 'restart repeated completed work'
        assert json.loads(invoke(database, 'status', '--json'))['objective'][0]['state'] == 'COMPLETED'
        assert inventory(workspace) == before
        assert not (workspace / '.orchestrator').exists() or workspace != fallback
    finally:
        scheduler.close()
        db.close()


def test_external_workspace_controls_recheck_and_restart(tmp_path):
    workspace = tmp_path / 'separate-repository'
    workspace.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(workspace)], check=True)
    database = tmp_path / 'state' / 'state.db'
    key = invoke(database, 'submit', '--text', 'write a then b', '--workspace', str(workspace))
    db = Database(database)
    backend = AlternativeBackend()
    scheduler = Scheduler(db, {'codex': lambda config: backend})
    try:
        invoke(database, 'pause', key)
        scheduler.tick()
        assert db.get('objective', key)['state'] == 'PAUSED' and not backend.calls
        invoke(database, 'resume', key)
        scheduler.tick()  # Persist the original DAG.
        scheduler.tick()  # Complete a exactly once.
        first = db.get('task', key + '/a')
        backend.script = [Outcome('completed', json.dumps({'status': 'blocked',
            'diagnostic': 'fixture human prerequisite', 'blocker_check': {
                'command': ['{python}', '-c', 'pass'], 'timeout': 10}}))]
        assert finish(scheduler, db, key) == 'BLOCKED'
        original = db.get('task', key + '/b')
        graph = db.rows('SELECT * FROM dependency')
        with ServiceLock(database) as lock, OwnerProbe(database, lock):
            assert 'scheduler already running' in invoke(database, 'recheck', key)
        # Interrupt the existing task after its side effect; restart reconciles
        # the same session and never calls the already completed task again.
        def interrupt(control):
            (workspace / 'side-effect.txt').write_text('once')
            raise TransportError('injected fixture transport interruption')
        backend.hook = interrupt
        scheduler.tick()
        assert db.get('task', key + '/b')['state'] == 'RECONCILING'
        scheduler.close()
        backend.hook = None
        backend.script = []
        db.execute("UPDATE event SET data=json_set(data,'$.retry_at',?) WHERE kind='infrastructure_failure'", (time.time() - 1,))
        scheduler = Scheduler(db, {'codex': lambda config: backend})
        assert finish(scheduler, db, key) == 'COMPLETED'
        assert db.get('task', key + '/a') == first
        assert db.get('task', key + '/b')['session'] == original['session']
        assert db.rows('SELECT * FROM dependency') == graph
        assert (workspace / 'side-effect.txt').read_text() == 'once'
        assert (workspace / 'a.txt').read_text() == 'a'
        assert (workspace / 'b.txt').read_text() == 'b'
        assert not (workspace / '.orchestrator').exists()
        stopped = invoke(database, 'submit', '--text', 'must never execute', '--workspace', str(workspace))
        invoke(database, 'stop', stopped)
        before_calls = len(backend.calls)
        scheduler.tick()
        assert db.get('objective', stopped)['state'] == 'STOPPED'
        assert len(backend.calls) == before_calls
    finally:
        scheduler.close()
        db.close()
