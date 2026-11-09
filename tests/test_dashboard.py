"""Deterministic display/refresh checks; all state lives in isolated SQLite fixtures."""
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from agent_orchestrator import dashboard, validation
from agent_orchestrator.backends.base import Bucket, Quota
from agent_orchestrator.cli import ServiceLock, main
from agent_orchestrator.database import Database
from agent_orchestrator.models import STATES, graph
from agent_orchestrator.scheduler import Scheduler
from test_lifecycle import AlternativeBackend, spec


@pytest.fixture
def state(tmp_path):
    db = Database(tmp_path / 'state.db')
    key = db.submit('Build a useful dashboard', tmp_path, 'alternative', {})
    db.execute('UPDATE objective SET created=1000')
    yield db, key
    db.close()


def persist_dag(db, key):
    db.save_graph(key, graph([spec(db.path.parent, 'a'), spec(db.path.parent, 'b', ['a'])]))


def strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', text)


def test_planning_then_every_persisted_task_in_order_and_progress(state):
    db, key = state
    before = dashboard.render(dashboard.read_snapshot(db.path), now=1010)
    assert f'{key} [.] PENDING | completed=0/0 | elapsed=00:00:10' in before
    assert '[P] PLANNING | DAG not yet persisted' in before
    assert 'Build a useful dashboard' in before
    db.transition('objective', key, 'RUNNING')
    db.execute("INSERT INTO turn(owner,kind,status,started) VALUES(?,'planning','running',1002)", (key,))
    planning = dashboard.render(dashboard.read_snapshot(db.path), now=1010)
    assert f'Active: {key} (planning) | elapsed=00:00:08' in planning
    assert '[P] PLANNING' in planning
    db.execute("UPDATE turn SET status='completed',ended=1005")
    # Preserve insertion order even when local IDs would sort differently.
    tasks = graph([spec(db.path.parent, 'a'), spec(db.path.parent, 'b', ['a']),
                   spec(db.path.parent, 'z'), spec(db.path.parent, 'c', ['b'])])
    db.save_graph(key, tasks)
    db.transition('task', key+'/a', 'COMPLETED')
    db.transition('task', key+'/b', 'RUNNING')
    db.execute("INSERT INTO turn(owner,kind,status,started) VALUES(?,'RUNNING','running',1005)", (key+'/b',))
    snapshot = dashboard.read_snapshot(db.path)
    before_report = db.report()
    rendered = dashboard.render(snapshot, now=1012, color=True)
    text = strip_ansi(rendered)
    assert '[P] PLANNING' not in text
    assert 'completed=1/4' in text
    assert f'Active: {key}/b (RUNNING) | elapsed=00:00:07' in text
    for name, state_name, deps in [('a','COMPLETED','none'), ('b','RUNNING',key+'/a'),
                                   ('z','PENDING','none'), ('c','PENDING',key+'/b')]:
        line = next(line for line in text.splitlines() if line.startswith(f'  {key}/{name} '))
        assert state_name in line and f'write {name}' in line
        assert f'depends_on: {deps}' in line
    assert [text.index(f'  {key}/{name} ') for name in ('a','b','z','c')] == sorted(
        text.index(f'  {key}/{name} ') for name in ('a','b','z','c'))
    assert '\x1b[32m[+] COMPLETED\x1b[0m' in rendered
    assert db.report() == before_report


@pytest.mark.parametrize('state_name', sorted(STATES))
def test_all_states_have_color_independent_symbols_labels_and_actions(state, state_name):
    db, key = state
    persist_dag(db, key)
    db.transition('task', key+'/a', state_name, 'Persisted diagnostic: inspect evidence')
    snapshot = dashboard.read_snapshot(db.path)
    plain = dashboard.render(snapshot, now=1020)
    colored = dashboard.render(snapshot, now=1020, color=True)
    assert strip_ansi(colored) == plain
    assert '\x1b' not in plain
    symbol, _ = dashboard.STYLES[state_name]
    assert f'{key}/a {symbol} {state_name}' in plain
    assert 'Persisted diagnostic: inspect evidence' in plain
    if state_name in ('PAUSED','BLOCKED'):
        action = 'resume' if state_name == 'PAUSED' else 'recheck'
        assert f'orchestrator {action} {key}/a' in plain
    elif state_name in ('WAITING_QUOTA','VALIDATING','RECONCILING','RETRYING','FIXING_PERSISTENT_FAILURE'):
        assert '    Next:' in plain
    if state_name == 'VALIDATING':
        assert f'Active: {key}/a (VALIDATING)' in plain


def test_optional_quota_latest_reports_retry_diagnostics_and_controls(state):
    db, key = state
    persist_dag(db, key)
    assert 'Quota: unknown (not reported)' in dashboard.render(dashboard.read_snapshot(db.path), 1020)
    db.event(key, 'quota', asdict(Quota()))
    unknown = dashboard.render(dashboard.read_snapshot(db.path), 1020)
    assert 'Quota: unknown (no reported buckets)' in unknown
    assert '% remaining' not in unknown and 'reset=' not in unknown
    db.event(key, 'quota', asdict(Quota([Bucket('short', 1.5, 300, 2000, 2), Bucket('unknown')])))
    db.transition('task', key+'/a', 'WAITING_QUOTA')
    frame = dashboard.render(dashboard.read_snapshot(db.path), 9999)
    assert 'Quota short: 1.5% remaining | window=300m | reset=1970-01-01 00:33:20 UTC' in frame
    assert 'Quota unknown: remaining unknown' in frame
    assert f'{key}/a [~] WAITING_QUOTA' in frame  # Passing reset never grants quota.
    db.event(key+'/a', 'account/rateLimits/updated', {'rateLimits': {
        'primary': {'usedPercent': 40, 'windowDurationMins': 300, 'resetsAt': 3000}}})
    frame = dashboard.render(dashboard.read_snapshot(db.path), 9999)
    assert '60% remaining' in frame and 'reset=1970-01-01 00:50:00 UTC' in frame
    assert '1.5% remaining' not in frame
    db.event(key+'/a', 'quota', asdict(Quota([Bucket('reset-only', resets_at=4000)], allowed=False)))
    frame = dashboard.render(dashboard.read_snapshot(db.path), 9999)
    assert 'provider reports admission denied' in frame
    assert 'reset-only: remaining unknown | reset=1970-01-01 01:06:40 UTC' in frame
    db.execute('UPDATE task SET infra_retries=3,attempts=2,recheck=\'verify\' WHERE key=?', (key+'/a',))
    db.transition('task', key+'/a', 'RECONCILING', 'Transport disconnected; inspect service.log')
    db.event(key+'/a', 'infrastructure_failure', {'retry': 3, 'error': 'worker killed', 'retry_at': 2050})
    frame = dashboard.render(dashboard.read_snapshot(db.path), 9999)
    assert 'Transport disconnected; inspect service.log' in frame
    assert 'Retries: infrastructure=3 disruptions=2' in frame
    assert 'Retry diagnostic: worker killed' in frame
    assert 'Retry at: 1970-01-01 00:34:10 UTC (automatic)' in frame
    assert 'Recheck: verify (queued/in progress)' in frame
    db.transition('objective', key, 'RUNNING')
    db.control(key, 'pause')
    assert 'Pause requested; active work will finish before pausing.' in dashboard.render(dashboard.read_snapshot(db.path), 9999)


def test_objective_scoping_terminal_progress_elapsed_and_short_descriptions(state):
    db, key = state
    persist_dag(db, key)
    other = db.submit('Second objective', db.path.parent, 'alternative', {})
    db.save_graph(other, graph([spec(db.path.parent, 'a')]))
    db.event(other, 'quota', asdict(Quota([Bucket('other-account', 12)])))
    db.transition('task', key+'/a', 'COMPLETED')
    db.transition('task', key+'/b', 'COMPLETED')
    db.transition('objective', key, 'COMPLETED')
    db.execute("UPDATE event SET at=1100 WHERE owner=? AND kind='transition'", (key,))
    long_spec = spec(db.path.parent)
    long_spec['objective'] = 'Description ' + 'x'*200
    db.execute('UPDATE task SET spec=? WHERE key=?', (json.dumps(long_spec), other+'/a'))
    snapshot = dashboard.read_snapshot(db.path)
    frame = dashboard.render(snapshot, now=2000)
    first, second = frame.split(other+' [.] PENDING', 1)
    assert f'{key} [+] COMPLETED | completed=2/2 | elapsed=00:01:40' in first
    assert 'other-account' not in first and 'Quota: unknown (not reported)' in first
    assert 'completed=0/1' in second and 'Quota other-account: 12% remaining' in second
    assert f'{key}/a [+] COMPLETED' in first and f'{other}/a [.] PENDING' in second
    assert 'Description ' + 'x'*200 not in second
    assert '...' in second


def test_blocked_planning_and_stopped_dependencies_have_actionable_next_steps(state):
    db, key = state
    db.transition('objective', key, 'BLOCKED', 'Invalid planning output')
    frame = dashboard.render(dashboard.read_snapshot(db.path), 2000)
    assert 'Invalid planning output' in frame
    assert 'resolve the diagnostic and submit a corrected objective' in frame
    assert f'orchestrator recheck {key}' not in frame  # Recheck requires persisted tasks.
    persist_dag(db, key)
    db.transition('task', key+'/a', 'STOPPED')
    frame = dashboard.render(dashboard.read_snapshot(db.path), 2000)
    assert 'stopped permanently; submit a revised objective' in frame


def test_reader_is_read_only_coherent_and_excludes_history_payloads(state, monkeypatch):
    db, key = state
    db.event(key, 'item/completed', {'large': 'x'*100000})
    db.event(key, 'quota', asdict(Quota([Bucket('old', 10)])))
    db.event(key, 'quota', asdict(Quota([Bucket('new', 20)])))
    db.execute("INSERT INTO turn(owner,kind,status,started) VALUES(?,'planning','completed',1000)", (key,))
    db.execute("INSERT INTO turn(owner,kind,status,started) VALUES(?,'planning','running',1001)", (key,))
    db.execute("INSERT INTO artifact(owner,at,kind,data) VALUES(?,1000,'checkpoint',?)", (key, 'x'*100000))
    connect = sqlite3.connect
    committed = []

    def connect_and_commit_between_reads(*args, **kwargs):
        assert args[0].endswith('?mode=ro') and kwargs['uri']
        conn = connect(*args, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            conn.execute("UPDATE objective SET diagnostic='forbidden'")
        conn.rollback()
        def trace(sql):
            if sql.startswith('SELECT * FROM task') and not committed:
                persist_dag(db, key)
                committed.append(True)
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(dashboard.sqlite3, 'connect', connect_and_commit_between_reads)
    snapshot = dashboard.read_snapshot(db.path)
    assert committed
    assert snapshot['task'] == [] and snapshot['dependency'] == []
    assert len(snapshot['turn']) == 1 and snapshot['turn'][0]['status'] == 'running'
    assert [e['kind'] for e in snapshot['event']] == ['quota']
    assert 'new' in snapshot['event'][0]['data']
    assert 'artifact' not in snapshot and len(json.dumps(snapshot)) < 5000
    monkeypatch.setattr(dashboard.sqlite3, 'connect', connect)
    assert len(dashboard.read_snapshot(db.path)['task']) == 2
    # Uncommitted scheduling/control updates never leak into the display.
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute("UPDATE objective SET diagnostic='uncommitted'")
        assert dashboard.read_snapshot(db.path)['objective'][0]['diagnostic'] == ''
    finally:
        db.execute('ROLLBACK')


class FrameStream(io.StringIO):
    """Condition-based frame rendezvous, no timing/sleep assertions."""
    def __init__(self):
        super().__init__()
        self.condition = threading.Condition()
        self.frames = []

    def write(self, text):
        with self.condition:
            result = super().write(text)
            if text.startswith('\x1b[H\x1b[J'):
                self.frames.append(text)
            self.condition.notify_all()
            return result

    def wait_frames(self, predicate, count=2):
        with self.condition:
            offset = len(self.frames)
            assert self.condition.wait_for(
                lambda: sum(predicate(f) for f in self.frames[offset:]) >= count, timeout=5), self.getvalue()
            return [f for f in self.frames[offset:] if predicate(f)]


def test_refresh_during_blocked_planning_worker_validation_and_quota_wait(state, monkeypatch):
    db, key = state
    stream = FrameStream()
    backend = AlternativeBackend()
    now = iter(range(1010, 100000))
    seen = []

    def while_blocked(label, predicate):
        # Called synchronously by tick/wait on the owning thread. Refresh must
        # advance while that same thread cannot perform another scheduling step.
        before = db.report()
        frames = stream.wait_frames(predicate)
        assert frames[0] != frames[-1], 'elapsed time must keep advancing'
        assert db.report() == before, 'display must never mutate scheduling/evidence'
        seen.append(label)

    def hook(control):
        if len(backend.calls) == 1:
            while_blocked('planning', lambda f: '[P] PLANNING' in f and f'Active: {key} (planning)' in f)
        else:
            while_blocked('worker', lambda f: f'Active: {key}/a (RUNNING)' in f)
    backend.hook = hook
    run_validation = validation.run
    def blocked_validation(*args, **kwargs):
        while_blocked('validation', lambda f: f'{key}/a' in f and '[?] VALIDATING' in f)
        return run_validation(*args, **kwargs)
    monkeypatch.setattr(validation, 'run', blocked_validation)
    with dashboard.Dashboard(db.path, stream, interval=.01, interactive=True,
                             color=False, clock=lambda: next(now)) as display:
        scheduler = Scheduler(db, {'alternative': lambda _: backend}, output=display.notice)
        try:
            scheduler.tick()
            assert len(db.report()['task']) == 2
            scheduler.tick()
            assert db.get('task', key+'/a')['state'] == 'COMPLETED'
            backend.limits = Quota([Bucket('short', 0, 300, 2000, 2)])
            scheduler.tick()
            assert db.get('task', key+'/b')['state'] == 'WAITING_QUOTA'
            backend.wait_updates = lambda seconds: while_blocked(
                'quota_wait', lambda f: f'{key}/b [~] WAITING_QUOTA' in f)
            scheduler.wait(30)
            assert len(backend.calls) == 2  # No worker admitted by the display.
            assert seen == ['planning', 'worker', 'validation', 'quota_wait']
        finally:
            scheduler.close()
    assert not display.thread.is_alive()
    output = stream.getvalue()
    assert output.startswith('\x1b[?1049h\x1b[?25l')
    assert output.endswith('\x1b[0m\x1b[?25h\x1b[?1049l')
    assert 'automatic quota reread' not in output  # Suppressed duplicate notice.
    assert '[~] WAITING_QUOTA' in output


def test_plain_output_changes_only_not_elapsed_and_sanitizes_text(state):
    db, key = state
    stream = io.StringIO()
    display = dashboard.Dashboard(db.path, stream, interactive=False)
    display.refresh()
    first = stream.getvalue()
    display.clock = lambda: 999999
    display.refresh()
    assert stream.getvalue() == first
    db.transition('objective', key, 'PAUSED', 'Inspect\npermission\x1b[31m')
    display.refresh()
    text = stream.getvalue()
    assert '[||] PAUSED' in text and f'orchestrator resume {key}' in text
    assert '\x1b' not in text and 'elapsed=' not in text
    assert text.count('Orchestrator dashboard') == 2
    display.notice('persisted quota notice', flush=True)
    assert stream.getvalue().endswith('persisted quota notice\n')


def test_read_errors_preserve_last_frame_then_recover(state, monkeypatch):
    db, key = state
    stream = io.StringIO()
    display = dashboard.Dashboard(db.path, stream, interactive=False)
    display.refresh()
    reader = dashboard.read_snapshot
    def fail(_):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(dashboard, 'read_snapshot', fail)
    display.refresh()
    assert key in stream.getvalue().split('Orchestrator dashboard')[-1]
    assert 'Display read error (retrying): database is locked' in stream.getvalue()
    monkeypatch.setattr(dashboard, 'read_snapshot', reader)
    display.refresh()
    assert display.error is None
    assert 'Display read error' not in stream.getvalue().split('Orchestrator dashboard')[-1]


@pytest.mark.parametrize('failure', [KeyboardInterrupt, RuntimeError])
def test_terminal_restored_and_thread_closed_on_interrupt_or_error(state, failure):
    db, _ = state
    stream = FrameStream()
    display = dashboard.Dashboard(db.path, stream, interactive=True)
    with pytest.raises(failure):
        with display:
            raise failure()
    assert not display.thread.is_alive()
    assert stream.getvalue().endswith('\x1b[0m\x1b[?25h\x1b[?1049l')


def test_no_color_and_dumb_or_redirected_terminal_detection(state, monkeypatch):
    db, _ = state
    monkeypatch.setenv('NO_COLOR', '')
    display = dashboard.Dashboard(db.path, io.StringIO(), interactive=True)
    assert not display.color
    assert not dashboard.terminal_mode(io.StringIO())
    monkeypatch.setenv('TERM', 'dumb')
    class Tty(io.StringIO):
        def isatty(self):
            return True
    assert not dashboard.terminal_mode(Tty())


def cli(db_path, *args):
    return subprocess.run([sys.executable, '-m', 'agent_orchestrator', '--db',
                           str(db_path), *args], capture_output=True, text=True,
                          encoding='utf-8', timeout=15)


def test_fresh_cli_once_status_json_output_and_lock_compatibility(state, tmp_path):
    db, key = state
    persist_dag(db, key)
    db.control(key, 'pause')
    before = db.report()
    result = cli(db.path, 'run', '--once')
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''  # One step still has no continuous display.
    assert db.report() == before
    result = cli(db.path, 'status')
    assert result.returncode == 0 and f'{key} PAUSED provider=alternative' in result.stdout
    assert f'{key}/a PENDING attempts=0 infra=0' in result.stdout
    assert f'{key}/b depends_on {key}/a' in result.stdout
    assert '\x1b' not in result.stdout and 'Orchestrator dashboard' not in result.stdout
    result = cli(db.path, 'status', '--json')
    assert result.returncode == 0 and json.loads(result.stdout) == before
    output = tmp_path/'report.json'
    result = cli(db.path, 'status', '--json', '--output', str(output))
    assert result.returncode == 0 and result.stdout == ''
    assert json.loads(output.read_text(encoding='utf-8')) == before
    with ServiceLock(db.path):
        for mode in ([], ['--once'], ['--detach']):
            result = cli(db.path, 'run', *mode)
            assert result.returncode == 2
            assert 'another orchestrator service owns this database' in result.stderr
            assert 'dashboard' not in result.stdout
    assert db.report() == before


def stop_process(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def test_fresh_redirected_foreground_run_plain_frames_and_no_state_changes(state):
    db, key = state
    db.control(key, 'pause')
    before = db.report()
    proc = subprocess.Popen([sys.executable, '-u', '-m', 'agent_orchestrator', '--db',
                             str(db.path), 'run', '--poll', '.05'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding='utf-8')
    received = []
    ready = threading.Event()
    def collect():
        for line in proc.stdout:
            received.append(line)
            if f'orchestrator resume {key}' in line:
                ready.set()
    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        assert ready.wait(10), ''.join(received)
        assert proc.poll() is None
        result = cli(db.path, 'run', '--once')
        assert result.returncode == 2 and 'another orchestrator' in result.stderr
        assert db.report() == before
    finally:
        stop_process(proc)
        reader.join(5)
    text = ''.join(received)
    assert 'Orchestrator dashboard' in text and '[||] PAUSED' in text
    assert '\x1b' not in text and 'elapsed=' not in text
    assert text.count('Orchestrator dashboard') == 1
    assert proc.stderr.read() == ''
    proc.stdout.close()
    proc.stderr.close()


def test_fresh_detached_launcher_keeps_pid_plain_log_and_exclusion(state):
    db, key = state
    db.control(key, 'pause')
    before = db.report()
    result = cli(db.path, 'run', '--detach', '--poll', '.05')
    assert result.returncode == 0, result.stderr
    match = re.fullmatch(r'pid=(\d+) log=(.+)\n', result.stdout)
    assert match, result.stdout
    pid = int(match.group(1))
    log = Path(match.group(2))
    assert log == db.path.parent/'service.log'
    # Probe the real child via its established service lock, with a bounded wait.
    try:
        deadline = time.monotonic() + 10
        while not log.exists() or 'Orchestrator dashboard' not in log.read_text(encoding='utf-8'):
            assert time.monotonic() < deadline, 'detached child did not write its display'
            threading.Event().wait(.02)
        result = cli(db.path, 'run', '--once')
        assert result.returncode == 2 and 'another orchestrator' in result.stderr
        text = log.read_text(encoding='utf-8')
        assert '[||] PAUSED' in text and '\x1b' not in text
        assert db.report() == before
    finally:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=15)
        else:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_cli_foreground_uses_display_and_once_bypasses_it(state, monkeypatch):
    db, key = state
    db.control(key, 'pause')
    calls = []
    class Display:
        def __init__(self, path):
            assert path == db.path
        def __enter__(self):
            calls.append('display enter')
            return self
        def notice(self, *args, **kwargs):
            pass
        def __exit__(self, *args):
            calls.append('display exit')
    class HeldScheduler:
        def __init__(self, database, *, output=None):
            self.db = database
            assert output is not None
            calls.append('scheduler')
        def tick(self):
            calls.append('tick')
        def wait(self, seconds):
            calls.append('wait')
            raise KeyboardInterrupt()
        def close(self):
            calls.append('close')
    monkeypatch.setattr('agent_orchestrator.cli.Dashboard', Display)
    monkeypatch.setattr('agent_orchestrator.cli.Scheduler', HeldScheduler)
    assert main(['--db', str(db.path), 'run']) == 130
    assert calls == ['display enter', 'scheduler', 'tick', 'wait', 'close', 'display exit']
