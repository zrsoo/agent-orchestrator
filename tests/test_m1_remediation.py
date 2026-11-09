"""Review regressions: real failure mechanisms, isolated databases and outputs."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agent_orchestrator.dashboard import Dashboard, clean, read_snapshot
from agent_orchestrator.database import Database
from agent_orchestrator.scheduler import Scheduler
from agent_orchestrator.viewport import Viewport
from agent_orchestrator.backends.base import Bucket, Quota
from test_lifecycle import system, finish
from test_recheck import block


def test_redirected_input_uses_complete_plain_summary(tmp_path, monkeypatch):
    monkeypatch.setattr('agent_orchestrator.dashboard.terminal_mode', lambda stream: True)
    monkeypatch.setattr(sys, 'stdin', io.StringIO())
    display = Dashboard(tmp_path/'unused.db', io.StringIO())
    assert not display.interactive


def test_recheck_committed_after_scheduler_snapshot_is_not_stranded(system, monkeypatch):
    db, backend, key, scheduler = system
    task = block(system, completed_first=True)
    db.transition('objective', key, 'RUNNING')
    other = Database(db.path)
    original = db.rows
    triggered = []

    def rows(sql, args=()):
        result = original(sql, args)
        if sql == 'SELECT * FROM task WHERE objective_id=? ORDER BY rowid' and not triggered:
            triggered.append(True)
            other.recheck(key)
        return result

    monkeypatch.setattr(db, 'rows', rows)
    try:
        scheduler.tick()
        assert triggered and db.get('objective', key)['state'] != 'BLOCKED'
        assert finish(scheduler, db, key) == 'COMPLETED'
        assert db.get('task', task)['recheck'] is None
        assert len(original("SELECT * FROM event WHERE kind='recheck_requested'")) == 1
    finally:
        other.close()


def test_block_before_recheck_is_reactivated_and_guard_rolls_back(system, monkeypatch):
    db, backend, key, scheduler = system
    task = block(system)
    db.transition('objective', key, 'RUNNING')
    event = db.event
    monkeypatch.setattr(db, 'event', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('event failed')))
    with pytest.raises(RuntimeError, match='event failed'):
        db.block_objective_if_unrecoverable(key)
    assert db.get('objective', key)['state'] == 'RUNNING'
    monkeypatch.setattr(db, 'event', event)
    assert db.block_objective_if_unrecoverable(key)
    assert not db.block_objective_if_unrecoverable(key)
    assert db.recheck(key) == [task]
    assert db.get('objective', key)['state'] == 'PENDING'


@pytest.mark.parametrize('control', ['pause', 'stop'])
def test_block_guard_preserves_controls(system, control):
    db, backend, key, scheduler = system
    block(system)
    db.transition('objective', key, 'RUNNING')
    db.control(key, control)
    before = db.get('objective', key)
    assert not db.block_objective_if_unrecoverable(key)
    assert db.get('objective', key) == before


def test_snapshot_indexes_upgrade_and_unused_turn_payload_is_excluded(tmp_path):
    path = tmp_path/'state.db'
    db = Database(path)
    key = db.submit('keep original', tmp_path, 'alternative', {})
    db.execute('DROP INDEX event_owner_kind_id')
    db.execute('DROP INDEX turn_owner_id')
    db.close()
    db = Database(path)
    try:
        assert db.get('objective', key)['text'] == 'keep original'
        db.event(key, 'quota', {'buckets': []})
        db.event(key, 'quota', {'buckets': [], 'latest': True})
        db.execute('INSERT INTO turn(owner,kind,status,started,result) VALUES(?,?,?,?,?)',
                   (key, 'planning', 'completed', 0, 'x'*2_000_000))
        snap = read_snapshot(path)
        assert 'result' not in snap['turn'][0]
        assert len(json.dumps(snap)) < 5000
        assert json.loads(snap['event'][0]['data'])['latest']
        for table, index, condition in [('event', 'event_owner_kind_id', "owner=? AND kind='quota'"),
                                         ('turn', 'turn_owner_id', 'owner=?')]:
            plan = db.rows(f'EXPLAIN QUERY PLAN SELECT id FROM {table} WHERE {condition} ORDER BY id DESC LIMIT 1', (key,))
            assert any(index in row['detail'] and 'SEARCH' in row['detail'] for row in plan)
    finally:
        db.close()


def test_actual_snapshot_queries_seek_past_irrelevant_history(tmp_path, monkeypatch):
    from agent_orchestrator import dashboard
    db = Database(tmp_path/'state.db')
    key = db.submit('snapshot query fixture', tmp_path, 'alternative', {})
    try:
        db.execute('BEGIN')
        db.conn.executemany('INSERT INTO event(owner,at,kind,data) VALUES(?,0,?,?)',
                            [(key, 'item/completed', '{}')]*20000)
        db.execute('COMMIT')
        db.event(key, 'quota', {'buckets': []})
        connect = dashboard.sqlite3.connect
        queries = []
        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(queries.append)
            return conn
        monkeypatch.setattr(dashboard.sqlite3, 'connect', traced)
        snap = read_snapshot(db.path)
        assert len(snap['event']) == 1
        for table, index in [('event', 'event_owner_kind_id'), ('turn', 'turn_owner_id')]:
            query = next(q for q in queries if f'JOIN {table} ' in q)
            plan = db.rows('EXPLAIN QUERY PLAN '+query)
            assert any(index in row['detail'] and 'SEARCH' in row['detail'] for row in plan)
    finally:
        db.close()


def test_dashboard_ascii_is_cp1252_safe_and_does_not_change_persisted_text(tmp_path):
    original = 'Caf\u00e9 \u2014 \u4e2d\u6587 \U0001f3ae \u2192 next'
    db = Database(tmp_path/'state.db')
    try:
        key = db.submit(original, tmp_path, 'alternative', {})
        db.transition('objective', key, 'PAUSED', original+'\x1b[31m')
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding='cp1252', errors='strict')
        display = Dashboard(db.path, stream, interactive=False)
        display.refresh()
        stream.flush()
        text = raw.getvalue().decode('ascii')
        assert 'Cafe - ?? ? -> next' in text and '\x1b' not in text
        assert db.get('objective', key)['text'] == original
        assert clean(original).isascii()
    finally:
        db.close()


def test_scheduler_closes_all_backends_even_after_failure(tmp_path):
    db = Database(tmp_path/'state.db')
    scheduler = Scheduler(db)
    closed = []
    class Backend:
        def __init__(self, name):
            self.name = name
        def close(self):
            closed.append(self.name)
            if self.name == 'first':
                raise OSError('close failed')
    scheduler.backends = {name: Backend(name) for name in ['first', 'second']}
    try:
        with pytest.raises(OSError, match='close failed'):
            scheduler.close()
        assert closed == ['first', 'second']
        scheduler.close()
        assert closed == ['first', 'second']
    finally:
        db.close()


def test_cli_close_failure_still_releases_owner_and_database(tmp_path, monkeypatch, capsys):
    from agent_orchestrator import cli, service
    connections = []
    class TrackedDatabase(Database):
        def __init__(self, path):
            super().__init__(path)
            self.closed = False
            connections.append(self)
        def close(self):
            super().close()
            self.closed = True
    class FailingScheduler:
        def __init__(self, db, **kwargs):
            self.db = db
        def tick(self):
            raise KeyboardInterrupt()
        def close(self):
            assert not service.lock_available(self.db.path)
            assert not service.owner_path(self.db.path).exists()
            raise OSError('fixture close failure')
    monkeypatch.setattr(cli, 'Database', TrackedDatabase)
    monkeypatch.setattr(cli, 'Scheduler', FailingScheduler)
    assert cli.main(['--db', str(tmp_path/'state.db'), 'run']) == 2
    assert 'fixture close failure' in capsys.readouterr().err
    assert connections[0].closed and service.lock_available(connections[0].path)


@pytest.mark.parametrize('initial', [False, True])
def test_real_output_backpressure_cannot_prevent_cleanup(tmp_path, initial):
    if initial:
        (tmp_path/'initial').touch()
    script = Path(__file__).parent/'fixtures/m1_blocked_display.py'
    with (tmp_path/'stderr.txt').open('wb') as errors:
        proc = subprocess.Popen([sys.executable, str(script), str(tmp_path)],
                                stdout=subprocess.PIPE, stderr=errors)
        try:
            assert proc.wait(timeout=12) == 130, (tmp_path/'stderr.txt').read_text()
            facts = json.loads((tmp_path/'result.json').read_text())
            assert facts['notice_returned'] and facts['provider_closed']
            assert facts['lock_held_during_close'] and facts['lock_released']
            assert facts['display_threads'] == []
        finally:
            if proc.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=10)
                else:
                    proc.kill()
            proc.wait(timeout=5)
            proc.stdout.close()


def section(key='obj-a', count=80, active=None, state='RUNNING'):
    return {'key': key, 'state': state, 'active': active,
            'header': [key+' RUNNING', 'Active: '+str(active)],
            'body': [(f'{key}/task{i}:0', f'{key}/task{i} task description {i}') for i in range(count)]}


@pytest.mark.parametrize('size', [(1, 1), (3, 2), (20, 5), (80, 24)])
def test_viewport_never_overflows_and_end_is_reachable(size):
    view = Viewport()
    data = [section()]
    first = view.render(data, size)
    assert len(first.splitlines()) <= size[1]
    assert all(len(line) <= max(1, size[0]-1) for line in first.splitlines())
    view.handle('end')
    last = view.render(data, size)
    assert len(last.splitlines()) <= size[1]
    if size[0] >= 20:
        assert 'task79' in last
    view.handle('home')
    assert view.render(data, size) == first


def test_every_task_is_reachable_and_anchor_survives_updates_and_resize():
    view = Viewport()
    data = [section()]
    seen = set()
    for _ in range(100):
        view.render(data, (80, 12))
        seen.update(row[0] for row in view.rows[view.offset:view.offset+view.page])
        view.handle('down')
    assert seen == {key for key, _ in data[0]['body']}
    anchor = view.anchor
    view.render(data, (80, 12))
    anchor = view.anchor
    data[0]['body'].insert(0, ('new-detail', 'new detail above existing task'))
    view.render(data, (80, 12))
    assert view.anchor == anchor
    view.render(data, (35, 8))
    assert view.anchor[0] == anchor[0]


def test_objective_navigation_is_pinned_and_can_jump_to_active_task():
    view = Viewport()
    data = [section('obj-a'), section('obj-b', active='obj-b/task60')]
    view.render(data, (80, 10))
    assert view.selected == 'obj-b'
    view.handle('previous')
    view.render(data, (80, 10))
    assert view.selected == 'obj-a'
    view.render(data, (80, 10))
    assert view.selected == 'obj-a'
    view.handle('active')
    frame = view.render(data, (80, 10))
    assert view.selected == 'obj-b' and 'obj-b/task60' in frame


def test_closed_display_stream_does_not_escape_into_durable_work(tmp_path):
    db = Database(tmp_path/'state.db')
    stream = io.StringIO()
    stream.close()
    try:
        with Dashboard(db.path, stream, interactive=False) as display:
            assert display.failed
            display.notice('notice after failure')
        assert not display.thread.is_alive()
    finally:
        db.close()


def test_failed_display_notice_cannot_block_a_quota_wait(system):
    db, backend, key, scheduler = system
    stream = io.StringIO()
    stream.close()
    with Dashboard(db.path, stream, interactive=False) as display:
        scheduler.output = display.notice
        backend.limits = Quota([Bucket('short', 0, 300, None, 2)])
        scheduler.tick()
        assert display.failed
        assert db.get('objective', key)['state'] == 'WAITING_QUOTA'
        assert not backend.calls


def test_render_failure_restores_screen_when_output_is_healthy(tmp_path, monkeypatch):
    from agent_orchestrator import dashboard
    db = Database(tmp_path/'state.db')
    stream = io.StringIO()
    def fail(*args, **kwargs):
        raise ValueError('bad display data')
    monkeypatch.setattr(dashboard, 'render_sections', fail)
    try:
        with Dashboard(db.path, stream, interactive=True) as display:
            assert display.failed and not display.terminal.output_failed
        assert stream.getvalue().endswith('\x1b[0m\x1b[?25h\x1b[?1049l')
    finally:
        db.close()


def test_input_redraws_do_not_increase_database_polling(tmp_path, monkeypatch):
    from agent_orchestrator import dashboard
    db = Database(tmp_path/'state.db')
    db.submit('Input cadence fixture', tmp_path, 'alternative', {})
    stream = io.StringIO()
    reads, inputs = [], []
    reader = dashboard.read_snapshot
    def read(path):
        reads.append(time.monotonic())
        return reader(path)
    monkeypatch.setattr(dashboard, 'read_snapshot', read)
    display = Dashboard(db.path, stream, interval=30, interactive=True)
    def poll():
        inputs.append(True)
        return [('down', 1)]
    monkeypatch.setattr(display.terminal, 'poll', poll)
    try:
        with display:
            deadline = time.monotonic()+2
            while len(inputs) < 4 and time.monotonic() < deadline:
                time.sleep(.01)
            assert len(inputs) >= 4 and len(reads) == 1
    finally:
        db.close()


@pytest.mark.skipif(os.name != 'nt', reason='real Windows console input/resize')
def test_native_scroll_wheel_keyboard_resize_and_mode_restoration(tmp_path):
    fixtures = Path(__file__).parent/'fixtures'
    env = os.environ.copy()
    env.pop('TERM', None)
    env.pop('NO_COLOR', None)
    proc = subprocess.Popen([sys.executable, str(fixtures/'m1_scroll_console.py'), str(tmp_path)],
                            creationflags=subprocess.CREATE_NEW_CONSOLE, env=env)
    def wait(predicate):
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.03)
        raise AssertionError('console did not reach expected state')
    def screen():
        result = subprocess.run([sys.executable, str(fixtures/'console_probe.py'), str(facts['pid']),
                                 str(tmp_path/'screen.json')], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, env=env, timeout=5)
        assert result.returncode == 0, result.stderr
        return json.loads((tmp_path/'screen.json').read_text())['text']
    def send(action):
        result = subprocess.run([sys.executable, str(fixtures/'m1_console_input.py'),
                                 str(facts['pid']), action], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, env=env, timeout=5)
        assert result.returncode == 0, result.stderr
    try:
        wait(lambda: (tmp_path/'ready.json').exists())
        facts = json.loads((tmp_path/'ready.json').read_text())
        assert facts['during_modes'][0] & 1  # Ctrl+C stays processed.
        assert not facts['during_modes'][0] & 0x40  # Quick Edit cannot freeze output.
        wait(lambda: facts['key']+'/task000' in screen())
        send('end')
        wait(lambda: facts['key']+'/task079' in screen())
        assert 'Orchestrator dashboard' in screen()
        send('home')
        wait(lambda: 'Rows 1-' in screen())
        send('wheel')
        wait(lambda: 'Rows 4-' in screen())
        send('active')
        wait(lambda: '/task055 [>] RUNNING' in screen())
        send('resize')
        send('end')
        wait(lambda: '/task079' in screen() and 'Orchestrator dashboard' in screen())
        send('next')
        wait(lambda: facts['other'] in screen() and 'Second objective' in screen())
        (tmp_path/'release').touch()
        assert proc.wait(timeout=8) == 0
        result = json.loads((tmp_path/'result.json').read_text())
        assert result['after_modes'] == facts['before_modes']
        assert not result['failed']
        assert 'orchestrator-dashboard' not in result['threads']
    finally:
        (tmp_path/'release').touch()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=10)
            proc.wait(timeout=5)


@pytest.mark.skipif(os.name != 'nt', reason='real Windows pseudoconsole VT transport')
def test_conpty_vt_mouse_keyboard_resize_and_restoration(tmp_path, monkeypatch):
    fixtures = Path(__file__).parent/'fixtures'
    monkeypatch.syspath_prepend(str(fixtures))
    # The ConPTY fixture launches the base Python interpreter, which does not
    # inherit pytest's sys.path or the virtual environment's editable install.
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1] / 'src'), prepend=os.pathsep)
    monkeypatch.delenv('TERM', raising=False)
    monkeypatch.delenv('NO_COLOR', raising=False)
    from conpty import PseudoConsole
    console = PseudoConsole([sys._base_executable, fixtures/'m1_scroll_console.py', tmp_path, '--conpty'])
    def wait(predicate):
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.03)
        raise AssertionError(console.frame())
    try:
        wait(lambda: (tmp_path/'ready.json').exists())
        wait(lambda: '/task000' in console.frame())
        console.send('\x1b[F')
        wait(lambda: '/task079' in console.frame())
        console.send('\x1b[H')
        wait(lambda: 'Rows 1-' in console.frame())
        console.send('\x1b[<65;5;10M')  # xterm SGR wheel-down report.
        wait(lambda: 'Rows 4-' in console.frame())
        console.resize(60, 15)
        console.send('\x1b[F')
        wait(lambda: '/task079' in console.frame() and 'Orchestrator dashboard' in console.frame())
        console.send('\t')
        wait(lambda: 'Second objective' in console.frame())
        (tmp_path/'release').touch()
        assert console.wait() == 0
        before = json.loads((tmp_path/'ready.json').read_text())['before_modes']
        after = json.loads((tmp_path/'result.json').read_text())
        assert before == after['after_modes'] and not after['failed']
    finally:
        (tmp_path/'release').touch()
        console.close()
