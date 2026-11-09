"""m1-03: live ownership, deterministic startup races, durable asynchronous recovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent_orchestrator import cli, service
from agent_orchestrator.database import Database
from agent_orchestrator.models import graph
from agent_orchestrator.scheduler import Scheduler
from test_lifecycle import system, finish
from test_recheck import block


class Clock:
    def __init__(self):
        self.now = 0
    def monotonic(self):
        return self.now
    def sleep(self, delay):
        assert 0 < delay <= .05
        self.now += delay


@pytest.fixture
def startup(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(service, 'time', clock)
    state = SimpleNamespace(owner=None, available=True, launches=[], code=None)
    monkeypatch.setattr(service, 'probe_owner', lambda path, deadline: state.owner)
    monkeypatch.setattr(service, 'lock_available', lambda path: state.available)
    def launch(path, nonce, poll, detached):
        state.launches.append((path, nonce, poll, detached))
        return SimpleNamespace(pid=99999, poll=lambda: state.code)
    monkeypatch.setattr(service, 'launch_service', launch)
    state.clock = clock
    return state


def identity(pid=1234, nonce=None):
    return {'pid': pid, 'launch_nonce': nonce}


@pytest.mark.parametrize('nonce', [None, 'old-launcher'])
def test_existing_owner_never_launches(startup, nonce):
    startup.owner = identity(nonce=nonce)
    assert service.ensure_running('db') == service.Running(1234, False)
    assert startup.launches == []
    assert startup.clock.now == 0


@pytest.mark.parametrize('rival', [False, True])
@pytest.mark.parametrize('launcher_exit', [None, 0, 2])
def test_acknowledged_owner_not_launcher_pid(startup, monkeypatch, rival, launcher_exit):
    startup.code = launcher_exit
    def probe(path, deadline):
        if startup.launches and startup.clock.now >= .15:
            nonce = 'rival' if rival else startup.launches[0][1]
            return identity(4321, nonce)
    monkeypatch.setattr(service, 'probe_owner', probe)
    assert service.ensure_running('db', timeout=.5) == service.Running(4321, not rival)
    assert len(startup.launches) == 1
    assert startup.launches[0][2] == cli.parser().parse_args(['run']).poll == 60
    assert startup.clock.now < .5


def test_rival_ack_on_final_exit_probe(startup, monkeypatch):
    startup.code = 2
    probes = []
    def probe(path, deadline):
        probes.append(1)
        return identity(77, 'rival') if len(probes) == 3 else None
    monkeypatch.setattr(service, 'probe_owner', probe)
    assert service.ensure_running('db', timeout=.5) == service.Running(77, False)
    assert len(probes) == 3


def test_spawn_error_still_discovers_rival(startup, monkeypatch):
    def launch(*args):
        startup.owner = identity(78, 'rival')
        raise OSError('launch denied')
    monkeypatch.setattr(service, 'launch_service', launch)
    assert service.ensure_running('db') == service.Running(78, False)


@pytest.mark.parametrize('mode,reason', [
    ('spawn', 'launch denied'), ('timeout', 'timed out; no live owner'),
    ('crashed', 'launcher exited with code 7'),
    ('unresponsive', 'owns this database but is unresponsive'),
])
def test_bounded_startup_failures(startup, monkeypatch, mode, reason):
    if mode == 'spawn':
        def launch(*args):
            raise OSError('launch denied')
        monkeypatch.setattr(service, 'launch_service', launch)
    if mode == 'crashed':
        startup.code = 7
    if mode == 'unresponsive':
        startup.available = False
    with pytest.raises(service.StartupError, match=reason):
        service.ensure_running('db', timeout=.25)
    assert startup.clock.now <= .25
    assert len(startup.launches) == (0 if mode in ('spawn', 'unresponsive') else 1)


def test_lock_busy_then_crash_allows_single_launch(startup, monkeypatch):
    monkeypatch.setattr(service, 'lock_available', lambda path: startup.clock.now >= .1)
    def probe(path, deadline):
        if startup.launches:
            return identity(90, startup.launches[0][1])
    monkeypatch.setattr(service, 'probe_owner', probe)
    assert service.ensure_running('db', timeout=.5) == service.Running(90, True)
    assert len(startup.launches) == 1


@pytest.mark.parametrize('mode', ['reuse', 'started', 'spawn', 'timeout', 'crashed', 'unresponsive'])
def test_cli_commits_before_startup_and_preserves_request_on_failure(system, monkeypatch, capsys, mode):
    db, backend, key, scheduler = system
    task = block(system, completed_first=True)
    before = db.report()
    calls = len(backend.calls)
    def ensure(path):
        # A separate connection must already see the committed request.
        connection = Database(path)
        try:
            assert connection.get('task', task)['recheck'] == 'verify'
            assert connection.get('task', task)['state'] == 'PENDING'
        finally:
            connection.close()
        if mode not in ('reuse', 'started'):
            raise service.StartupError(mode)
        return service.Running(42, mode == 'started')
    monkeypatch.setattr(cli, 'ensure_running', ensure)
    result = cli.main(['--db', str(db.path), 'recheck', key])
    output = capsys.readouterr()
    if mode in ('reuse', 'started'):
        assert result == 0 and output.err == ''
        message = ('Recheck queued; scheduler already running (PID 42).\n' if mode == 'reuse' else
                   'Recheck queued; scheduler started (PID 42)' +
                   ('; dashboard opened.\n' if os.name == 'nt' else '.\n'))
        assert output.out == message
    else:
        assert result == 2 and output.out == ''
        assert output.err == f'Recheck queued, but scheduler startup failed: {mode}\n'
    assert len(backend.calls) == calls  # CLI never waits for/executes recovery.
    reopened = Database(db.path)
    try:
        assert reopened.get('task', task)['recheck'] == 'verify'
        assert reopened.recheck(task) == [task]
        assert len(reopened.rows("SELECT * FROM event WHERE kind='recheck_requested'")) == 1
        assert reopened.rows('SELECT * FROM dependency') == before['dependency']
        assert reopened.rows('SELECT * FROM artifact') == before['artifact']
        assert reopened.get('task', key+'/a') == before['task'][0]
        for field in ('id', 'spec', 'session', 'diagnostic', 'blocker_check'):
            assert reopened.get('task', task)[field] == before['task'][1][field]
        for field in ('id', 'provider', 'config', 'session'):
            assert reopened.get('objective', key)[field] == before['objective'][0][field]
    finally:
        reopened.close()
    assert finish(scheduler, db, key) == 'COMPLETED'
    assert db.get('task', task)['session'] == before['task'][1]['session']


def test_queue_transaction_rolls_back_on_partial_failure(system, monkeypatch):
    db, backend, key, scheduler = system
    block(system)
    before = db.report()
    def fail(*args):
        raise OSError('event write interrupted')
    monkeypatch.setattr(db, 'event', fail)
    with pytest.raises(OSError, match='event write interrupted'):
        db.recheck(key)
    assert db.report() == before


@pytest.mark.parametrize('mode', ['spawn', 'timeout', 'crashed', 'unresponsive'])
def test_real_startup_failure_path_keeps_committed_request(system, startup, monkeypatch, capsys, mode):
    db, backend, key, scheduler = system
    task = block(system)
    session = db.get('task', task)['session']
    calls = len(backend.calls)
    monkeypatch.setattr(cli, 'ensure_running', lambda path: service.ensure_running(path, timeout=.25))
    if mode == 'spawn':
        def launch(*args):
            raise OSError('launch denied')
        monkeypatch.setattr(service, 'launch_service', launch)
    elif mode == 'crashed':
        startup.code = 9
    elif mode == 'unresponsive':
        startup.available = False
    assert cli.main(['--db', str(db.path), 'recheck', key]) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert output.err.startswith('Recheck queued, but scheduler startup failed: ')
    assert ('launch denied' if mode == 'spawn' else 'timed out') in output.err
    assert len(backend.calls) == calls
    connection = Database(db.path)
    try:
        assert connection.get('task', task)['recheck'] == 'verify'
        assert connection.get('task', task)['session'] == session
        assert len(connection.rows("SELECT * FROM event WHERE kind='recheck_requested'")) == 1
    finally:
        connection.close()


def test_invalid_recheck_does_not_launch(system, monkeypatch, capsys):
    db, backend, key, scheduler = system
    def unexpected(*args):
        pytest.fail('ineligible recheck attempted startup')
    monkeypatch.setattr(cli, 'ensure_running', unexpected)
    assert cli.main(['--db', str(db.path), 'recheck', key]) == 2
    assert 'no blocked task' in capsys.readouterr().err


@pytest.mark.parametrize('detached', [False, True])
def test_windows_launch_uses_interpreter_and_correct_console_flags(tmp_path, monkeypatch, detached):
    # Run everywhere: only platform selection is mocked; inspect the actual Popen boundary.
    calls = []
    monkeypatch.setattr(service, 'os', SimpleNamespace(name='nt', path=os.path))
    monkeypatch.setattr(service.subprocess, 'CREATE_NEW_CONSOLE', 0x10, raising=False)
    monkeypatch.setattr(service.subprocess, 'DETACHED_PROCESS', 0x8, raising=False)
    monkeypatch.setattr(service.subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, raising=False)
    monkeypatch.setattr(service.subprocess, 'Popen', lambda argv, **kw: calls.append((argv, kw)))
    service.launch_service(tmp_path/'db', 'nonce', .2, detached)
    argv, kwargs = calls[0]
    assert argv == [sys.executable, '-m', 'agent_orchestrator', '--db',
                    service.canonical_db(tmp_path/'db'), 'run', '--poll', '0.2', '--startup-nonce', 'nonce']
    assert '--detach' not in argv and '--once' not in argv
    if detached:
        assert kwargs['creationflags'] == 0x208
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['stdout'].name == str(tmp_path/'service.log')
        assert kwargs['stderr'] is kwargs['stdout']
    else:
        assert kwargs == {'creationflags': 0x10}


def test_probe_requires_lock_and_cleans_its_generation(tmp_path):
    path = tmp_path/'db'
    with pytest.raises(ValueError, match='requires the database lock'):
        with service.OwnerProbe(path, service.ServiceLock(path)):
            pass
    assert not service.owner_path(path).exists()
    with service.ServiceLock(path) as lock:
        with service.OwnerProbe(path, lock, 'launch') as owner:
            record = service.probe_owner(path, time.monotonic()+1)
            assert record == owner.identity
            assert record['pid'] == os.getpid()
            assert record['canonical_db'] == service.canonical_db(path)
            assert not service.lock_available(path)
        assert not service.owner_path(path).exists()
    assert service.lock_available(path)


def test_probe_publication_failure_releases_socket_thread_and_lock(tmp_path, monkeypatch):
    path = tmp_path/'db'
    def fail(*args):
        raise OSError('cannot publish')
    monkeypatch.setattr(service.os, 'replace', fail)
    with pytest.raises(OSError, match='cannot publish'):
        with service.ServiceLock(path) as lock:
            with service.OwnerProbe(path, lock) as owner:
                pytest.fail('acknowledged before publication')
    assert service.lock_available(path)
    assert not service.owner_path(path).exists()
    assert not list(tmp_path.glob('*.tmp'))
    assert not any(t.name == 'orchestrator-owner' for t in threading.enumerate())


@pytest.mark.parametrize('failure', ['display', 'scheduler'])
def test_cli_never_publishes_ack_before_service_initializes(tmp_path, monkeypatch, capsys, failure):
    path = tmp_path/'db'
    class Display:
        def __init__(self, *args):
            pass
        def __enter__(self):
            assert not service.owner_path(path).exists()
            if failure == 'display':
                raise OSError('display initialization failed')
            return self
        def notice(self, *args, **kwargs):
            pass
        def __exit__(self, *args):
            pass
    def scheduler(*args, **kwargs):
        assert not service.owner_path(path).exists()
        raise OSError('scheduler initialization failed')
    monkeypatch.setattr(cli, 'Dashboard', Display)
    monkeypatch.setattr(cli, 'Scheduler', scheduler)
    assert cli.main(['--db', str(path), 'run']) == 2
    assert f'{failure} initialization failed' in capsys.readouterr().err
    assert not service.owner_path(path).exists()
    assert service.lock_available(path)


def test_cli_ack_is_ready_before_first_tick_and_remains_live_during_work(tmp_path, monkeypatch):
    path = tmp_path/'db'
    calls = []
    class Display:
        def __init__(self, *args):
            pass
        def __enter__(self):
            calls.append('display')
            return self
        def notice(self, *args, **kwargs):
            pass
        def __exit__(self, *args):
            calls.append('display closed')
    class HeldScheduler:
        def __init__(self, *args, **kwargs):
            calls.append('scheduler')
        def tick(self):
            assert calls == ['display', 'scheduler']
            with ThreadPoolExecutor(max_workers=1) as pool:
                owner = pool.submit(service.probe_owner, path, time.monotonic()+1).result(timeout=2)
            assert owner['pid'] == os.getpid() and owner['launch_nonce'] == 'test'
            assert not service.lock_available(path)
            raise KeyboardInterrupt()
        def close(self):
            calls.append('scheduler closed')
    monkeypatch.setattr(cli, 'Dashboard', Display)
    monkeypatch.setattr(cli, 'Scheduler', HeldScheduler)
    assert cli.main(['--db', str(path), 'run', '--startup-nonce', 'test']) == 130
    assert calls == ['display', 'scheduler', 'scheduler closed', 'display closed']
    assert not service.owner_path(path).exists()
    assert service.lock_available(path)


def test_stale_record_or_live_probe_without_lock_is_not_authority(tmp_path):
    path = tmp_path/'db'
    with service.ServiceLock(path) as lock, service.OwnerProbe(path, lock) as owner:
        assert service.probe_owner(path, time.monotonic()+1)
        # Simulate a surviving stale listener with a released OS lock.
        lock.__exit__()
        assert service.probe_owner(path, time.monotonic()+1) is None
    record = dict(owner.identity, pid=os.getpid())  # PID exists, but no ownership.
    service.owner_path(path).write_text(json.dumps(record))
    assert service.probe_owner(path, time.monotonic()+1) is None


@pytest.mark.parametrize('fault', ['generation', 'pid', 'canonical_db', 'token', 'challenge',
                                  'malformed', 'hang', 'record_replaced'])
def test_stale_or_wrong_probe_response_cannot_ack(tmp_path, fault):
    path = tmp_path/'db'
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    record = {'canonical_db': service.canonical_db(path), 'pid': 123,
              'generation': 'current', 'launch_nonce': 'old', 'token': 'secret',
              'port': listener.getsockname()[1]}
    service.owner_path(path).write_text(json.dumps(record))
    release = threading.Event()
    def respond():
        conn, _ = listener.accept()
        with conn:
            request = service.receive(conn, time.monotonic()+1)
            response = dict(record, challenge=request['challenge'])
            if fault == 'hang':
                release.wait(2)
                return
            if fault == 'malformed':
                data = b'broken\n'
            elif fault == 'record_replaced':
                # A valid reply from an earlier generation must not hide a
                # handoff to another owner during this probe.
                service.owner_path(path).write_text(json.dumps(dict(record, generation='new-owner')))
                data = json.dumps(response).encode()+b'\n'
            else:
                response[fault] = 'wrong'
                data = json.dumps(response).encode()+b'\n'
            conn.sendall(data)
    worker = threading.Thread(target=respond, daemon=True)
    worker.start()
    try:
        with service.ServiceLock(path):
            started = time.monotonic()
            assert service.probe_owner(path, started+.1) is None
            assert time.monotonic()-started < .75
    finally:
        release.set()
        worker.join(2)
        listener.close()
    assert not worker.is_alive()


def test_owner_cleanup_does_not_remove_another_generation(tmp_path):
    path = tmp_path/'db'
    with service.ServiceLock(path) as lock:
        with service.OwnerProbe(path, lock) as owner:
            record = dict(owner.identity, generation='replacement')
            service.owner_path(path).write_text(json.dumps(record))
        assert service.read_owner(path) == record


def wait_for(predicate, timeout=10):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError('condition timed out')


def live_owner(path):
    return service.probe_owner(path, time.monotonic()+.3)


def kill_pid(pid):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=15)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


@contextmanager
def running_process(path):
    log = Path(path).parent/'foreground.log'
    with log.open('ab') as stream:
        proc = subprocess.Popen([sys.executable, '-m', 'agent_orchestrator', '--db', str(path),
                                 'run', '--poll', '.05'], stdin=subprocess.DEVNULL,
                                stdout=stream, stderr=stream)
    try:
        owner = wait_for(lambda: live_owner(path))
        yield proc, owner
    finally:
        owner = live_owner(path)
        if owner:
            kill_pid(owner['pid'])
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)


def seeded_database(tmp_path, paused=False):
    db = Database(tmp_path/'state.db')
    fixture = Path(__file__).parent/'fixtures/app_server.py'
    key = db.submit('existing recovery objective', tmp_path, 'codex',
                    {'executable': [sys.executable, str(fixture)], 'rpc_timeout': 5})
    db.save_graph(key, graph([
        {'id': 'a', 'workspace': str(tmp_path), 'objective': 'completed', 'acceptance': ['done']},
        {'id': 'b', 'workspace': str(tmp_path), 'objective': 'finish remaining work',
         'acceptance': ['output.txt contains done exactly once'], 'depends_on': ['a']}]))
    db.transition('task', key+'/a', 'COMPLETED')
    db.execute("UPDATE task SET session='original-provider-session' WHERE key=?", (key+'/b',))
    db.execute("INSERT INTO artifact(owner,at,kind,data) VALUES(?,?,?,?)",
               (key+'/b', time.time(), 'checkpoint', '{"stage":"before_block","files":{"a":"saved"}}'))
    db.transition('task', key+'/b', 'BLOCKED', 'permission.txt missing')
    db.transition('objective', key, 'BLOCKED')
    if paused:
        db.execute("UPDATE objective SET control='pause' WHERE id=?", (key,))
    return db, key


def invoke(path, *args):
    return subprocess.run([sys.executable, '-m', 'agent_orchestrator', '--db', str(path), *args],
                          capture_output=True, text=True, encoding='utf-8', timeout=20)


@pytest.mark.parametrize('detached', [False, True])
def test_fresh_cli_reuses_foreground_and_detached_owner(tmp_path, detached):
    db, key = seeded_database(tmp_path, paused=True)
    before = db.report()
    if detached:
        result = invoke(db.path, 'run', '--detach', '--poll', '.05')
        assert result.returncode == 0, result.stderr
        reported = int(re.search(r'pid=(\d+)', result.stdout)[1])
        owner = live_owner(db.path)
        assert owner and owner['pid'] == reported
        context = None
    else:
        context = running_process(db.path)
        proc, owner = context.__enter__()
    try:
        generation = owner['generation']
        for target in (key, key+'/b'):
            result = invoke(db.path, 'recheck', target)
            assert result.returncode == 0, result.stderr
            assert result.stdout == f'Recheck queued; scheduler already running (PID {owner["pid"]}).\n'
        assert live_owner(db.path)['generation'] == generation
        assert len(db.rows("SELECT * FROM event WHERE kind='recheck_requested'")) == 1
        assert db.rows('SELECT * FROM artifact') == before['artifact']
        assert db.get('task', key+'/a') == before['task'][0]
        assert db.get('task', key+'/b')['session'] == 'original-provider-session'
        assert not db.rows('SELECT * FROM turn')
    finally:
        if context:
            context.__exit__(None, None, None)
        else:
            kill_pid(owner['pid'])
        db.close()


def test_real_recheck_start_ack_is_async_and_keeps_original_work(tmp_path):
    db, key = seeded_database(tmp_path)
    before = db.report()
    (tmp_path/'hold').touch()  # Existing fault server holds the resumed turn for 60s.
    owner = None
    try:
        started = time.monotonic()
        result = invoke(db.path, 'recheck', key)
        assert result.returncode == 0, result.stderr
        assert time.monotonic()-started < 15
        owner = live_owner(db.path)
        assert owner
        suffix = '; dashboard opened.' if os.name == 'nt' else '.'
        assert result.stdout == f'Recheck queued; scheduler started (PID {owner["pid"]}){suffix}\n'
        wait_for(lambda: (tmp_path/'output.txt').exists())
        task = db.get('task', key+'/b')
        assert task['state'] == 'RUNNING' and task['recheck'] == 'verify'
        assert task['session'] == 'original-provider-session'
        assert db.rows('SELECT * FROM dependency') == before['dependency']
        assert db.rows('SELECT * FROM artifact')[:len(before['artifact'])] == before['artifact']
        assert db.get('task', key+'/a') == before['task'][0]
        assert len(db.rows("SELECT * FROM event WHERE kind='graph_persisted'")) == 1
        assert len(db.rows('SELECT * FROM turn')) == 1
        result = invoke(db.path, 'recheck', key)
        assert result.returncode == 0
        assert result.stdout == f'Recheck queued; scheduler already running (PID {owner["pid"]}).\n'
        assert len(db.rows('SELECT * FROM turn')) == 1
    finally:
        owner = owner or live_owner(db.path)
        if owner:
            kill_pid(owner['pid'])
            wait_for(lambda: service.lock_available(db.path))
        (tmp_path/'hold').unlink(missing_ok=True)
    # Abrupt service death leaves a queued/in-progress request that normal
    # reconciliation completes, in the original provider session and graph.
    restarted = Scheduler(db)
    try:
        assert finish(restarted, db, key) == 'COMPLETED'
        assert db.get('task', key+'/b')['session'] == 'original-provider-session'
        assert db.rows("SELECT * FROM artifact WHERE kind='validation'")
        assert db.get('task', key+'/a') == before['task'][0]
        assert len(db.rows("SELECT * FROM event WHERE kind='graph_persisted'")) == 1
    finally:
        restarted.close()
        db.close()


def test_concurrent_fresh_rechecks_report_one_actual_owner(tmp_path):
    db, key = seeded_database(tmp_path, paused=True)
    barrier = threading.Barrier(3)
    def recheck(_):
        barrier.wait(timeout=5)
        return invoke(db.path, 'recheck', key)
    owner = None
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(recheck, range(3)))
        owner = live_owner(db.path)
        assert owner
        started = 0
        for result in results:
            assert result.returncode == 0, result.stderr
            assert f'(PID {owner["pid"]})' in result.stdout
            assert 'scheduler already running' in result.stdout or 'scheduler started' in result.stdout
            started += 'scheduler started' in result.stdout
        assert started == 1
        assert len(db.rows("SELECT * FROM event WHERE kind='recheck_requested'")) == 1
        assert not db.rows('SELECT * FROM turn')
        assert not service.lock_available(db.path)
        result = invoke(db.path, 'run', '--once')
        assert result.returncode == 2 and 'another orchestrator' in result.stderr
        assert live_owner(db.path)['generation'] == owner['generation']
    finally:
        owner = owner or live_owner(db.path)
        if owner:
            kill_pid(owner['pid'])
        db.close()


def test_crashed_owner_stale_record_replaced_on_next_recheck(tmp_path):
    db, key = seeded_database(tmp_path, paused=True)
    first = service.ensure_running(db.path, detached=True, poll=.05)
    original = live_owner(db.path)
    kill_pid(first.pid)
    wait_for(lambda: service.lock_available(db.path))
    assert service.read_owner(db.path) == original  # Abrupt death leaves a locator, not authority.
    assert live_owner(db.path) is None
    owner = None
    try:
        result = invoke(db.path, 'recheck', key)
        assert result.returncode == 0, result.stderr
        owner = live_owner(db.path)
        assert owner and owner['generation'] != original['generation']
        assert f'scheduler started (PID {owner["pid"]})' in result.stdout
        assert db.get('task', key+'/b')['recheck'] == 'verify'
    finally:
        owner = owner or live_owner(db.path)
        if owner:
            kill_pid(owner['pid'])
        db.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows console API host verification')
def test_new_windows_console_is_visible_and_contains_dashboard(tmp_path):
    db, key = seeded_database(tmp_path, paused=True)
    owner = None
    try:
        result = invoke(db.path, 'recheck', key)
        assert result.returncode == 0, result.stderr
        owner = live_owner(db.path)
        assert owner
        assert f'scheduler started (PID {owner["pid"]}); dashboard opened.' in result.stdout
        probe = Path(__file__).parent/'fixtures/console_probe.py'
        output = tmp_path/'console.json'
        result = subprocess.run([sys.executable, str(probe), str(owner['pid']), str(output)],
                                capture_output=True, text=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr
        console = json.loads(output.read_text(encoding='utf-8'))
        assert console['hwnd'] and console['visible'], console
        assert owner['pid'] in console['pids'], console
        assert 'Orchestrator dashboard' in console['text'], console
        assert key in console['text'] and 'completed=1/2' in console['text'], console
        assert key+'/b' in console['text'] and 'Recheck: verify' in console['text'], console
        evidence = os.environ.get('ORCHESTRATOR_M1_EVIDENCE')
        if evidence:
            destination = Path(evidence)/'windows-visible'
            destination.mkdir(parents=True, exist_ok=True)
            (destination/'console.json').write_text(json.dumps(console, indent=2), encoding='utf-8')
    finally:
        owner = owner or live_owner(db.path)
        if owner:
            kill_pid(owner['pid'])
        db.close()
