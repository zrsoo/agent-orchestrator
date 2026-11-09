"""Fresh CLI integration: no in-process Scheduler or patched CLI entry point."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from agent_orchestrator.database import Database
from agent_orchestrator import service
from test_service import (invoke, kill_pid, live_owner, seeded_database, wait_for)

FIXTURES = Path(__file__).parent / 'fixtures'
SOURCE = str(Path(__file__).resolve().parents[1] / 'src')


@pytest.fixture(autouse=True)
def fresh_source(monkeypatch):
    monkeypatch.setenv('PYTHONPATH', SOURCE + os.pathsep + os.environ.get('PYTHONPATH', ''))


def read(path):
    return path.read_text(encoding='utf-8') if path.exists() else ''


def until_text(path, text):
    return wait_for(lambda: text in read(path))


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.replace(path)


def record(name, paths, report=None):
    destination = os.environ.get('ORCHESTRATOR_M1_EVIDENCE')
    if not destination:
        return
    directory = Path(destination) / name
    directory.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.exists():
            (directory / path.name).write_bytes(path.read_bytes())
    if report is not None:
        (directory / 'status.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


def test_fresh_planning_long_worker_validation_quota_and_provider_contract(tmp_path):
    config = tmp_path / 'factory.yaml'
    pinned = {'execution_mode': 'yolo', 'model': 'm1-fixture-model', 'rpc_timeout': 5,
              'executable': [sys.executable, str(FIXTURES / 'm1_app_server.py'), str(tmp_path)]}
    config.write_text(yaml.safe_dump({'provider': 'codex', 'providers': {'codex': pinned}}))
    db_path = tmp_path / 'state.db'
    result = invoke(db_path, '--config', str(config), 'submit', '--text',
                    'M1 isolated integration fixture', '--workspace', str(tmp_path))
    assert result.returncode == 0, result.stderr
    key = result.stdout.strip()
    # Independent acceptance program really blocks validation and checks artifacts.
    (tmp_path / 'validate.py').write_text(
        "from pathlib import Path\nimport sys,time\n"
        "name=sys.argv[1]\nPath('entered-validation-'+name).touch()\n"
        "deadline=time.monotonic()+35\n"
        "while not Path('release-validation-'+name).exists():\n"
        "    assert time.monotonic()<deadline, 'validation gate timed out'\n"
        "    time.sleep(.02)\n"
        "assert Path(name+'.txt').read_text()==name\n"
        "assert Path('effects.txt').read_text().splitlines().count(name)==1\n")
    output, errors = tmp_path / 'foreground.log', tmp_path / 'stderr.log'
    db = Database(db_path)
    with output.open('w', encoding='utf-8') as stdout, errors.open('w') as stderr:
        proc = subprocess.Popen([sys.executable, '-m', 'agent_orchestrator', '--db', str(db_path),
                                 'run', '--poll', '.5'], stdout=stdout, stderr=stderr,
                                stdin=subprocess.DEVNULL)
    try:
        owner = wait_for(lambda: live_owner(db_path))
        assert owner['pid'] != os.getpid()
        wait_for(lambda: (tmp_path / 'entered-planning').exists())
        assert db.rows('SELECT * FROM task') == []
        until_text(output, f'Active: {key} (planning)')
        db.transition('objective', key, 'RUNNING', 'planning gate held')
        until_text(output, 'Diagnostic: planning gate held')
        (tmp_path / 'release-planning').touch()

        wait_for(lambda: (tmp_path / 'entered-worker-a').exists())
        until_text(output, f'Active: {key}/a (RUNNING)')
        assert len(db.rows('SELECT * FROM task')) == 2
        assert db.rows('SELECT * FROM dependency') == [
            {'task_key': key + '/b', 'depends_key': key + '/a'}]
        until_text(output, f'{key}/b [.] PENDING | write b once | depends_on: {key}/a')
        db.transition('task', key + '/a', 'RUNNING', 'long worker gate held')
        until_text(output, 'Diagnostic: long worker gate held')
        assert len(db.rows('SELECT * FROM turn')) == 2
        (tmp_path / 'release-worker-a').touch()

        wait_for(lambda: (tmp_path / 'entered-validation-a').exists())
        until_text(output, f'{key}/a [?] VALIDATING')
        assert db.get('task', key + '/a')['state'] == 'VALIDATING'
        db.transition('task', key + '/a', 'VALIDATING', 'independent validator gate held')
        until_text(output, 'Diagnostic: independent validator gate held')
        exhausted = {'rateLimits': {'primary': {'usedPercent': 100,
                     'windowDurationMins': 300, 'resetsAt': 2000000000}}}
        atomic_json(tmp_path / 'quota.json', exhausted)
        (tmp_path / 'release-validation-a').touch()
        wait_for(lambda: db.get('task', key + '/b')['state'] == 'WAITING_QUOTA')
        until_text(output, f'{key}/b [~] WAITING_QUOTA')
        until_text(output, 'completed=1/2')
        until_text(output, 'Quota codex/primary: 0% remaining | window=300m | reset=')
        db.transition('task', key + '/b', 'WAITING_QUOTA', 'quota wait remains automatic')
        until_text(output, 'Diagnostic: quota wait remains automatic')
        assert not (tmp_path / 'entered-worker-b').exists()
        assert len(db.rows('SELECT * FROM turn')) == 2

        # Only an authoritative reread admits b. No manual resume/recheck.
        exhausted['rateLimits']['primary']['usedPercent'] = 10
        atomic_json(tmp_path / 'quota.json', exhausted)
        wait_for(lambda: (tmp_path / 'entered-worker-b').exists())
        until_text(output, f'Active: {key}/b (RUNNING)')
        (tmp_path / 'release-worker-b').touch()
        (tmp_path / 'release-validation-b').touch()
        wait_for(lambda: db.get('objective', key)['state'] == 'COMPLETED')
        until_text(output, 'completed=2/2')
        status = invoke(db_path, 'status', '--json')
        assert status.returncode == 0, status.stderr
        report = json.loads(status.stdout)
        assert report == db.report()
        assert len(report['validator']) == 2
        assert len([a for a in report['artifact'] if a['kind'] == 'validation']) == 2
        assert len([e for e in report['event'] if e['kind'] == 'graph_persisted']) == 1
        assert len(report['turn']) == 3
        assert len({t['provider_turn'] for t in report['turn']}) == 3
        assert all(t['status'] == 'completed' and t['session'] for t in report['turn'])
        assert json.loads(report['objective'][0]['config']) == pinned
        assert read(tmp_path / 'effects.txt').splitlines() == ['a', 'b']
        wire = [json.loads(line) for line in read(tmp_path / 'wire.jsonl').splitlines()]
        assert len({r['fixture_pid'] for r in wire}) == 1
        assert wire[0]['fixture_pid'] not in (os.getpid(), owner['pid'])
        sessions = [r['params'] for r in wire if r.get('method') in ('thread/start', 'thread/resume')]
        assert len(sessions) == 3
        assert all(p['approvalPolicy'] == 'never' and p['sandbox'] == 'danger-full-access'
                   and p['model'] == 'm1-fixture-model' for p in sessions)
        turns = [r['params'] for r in wire if r.get('method') == 'turn/start']
        assert len(turns) == 3
        assert all(p['approvalPolicy'] == 'never' and p['sandboxPolicy'] == {'type': 'dangerFullAccess'}
                   and set(p) == {'threadId', 'input', 'approvalPolicy', 'sandboxPolicy'} for p in turns)
        if os.name == 'nt':
            # Inspect the actual provider's job object, not just launch arguments.
            # Native job queries can include silent breakaway (0x1000) and
            # default priority/scheduling values even when those limits are off.
            policy = json.loads(read(tmp_path / 'provider-policy.json'))
            assert policy['flags'] & 0x2000  # Existing kill-on-close cleanup.
            assert policy['flags'] & ~0x3000 == 0  # No CPU/memory/process/affinity limits.
            assert all(value == 0 for name, value in policy['limits'].items()
                       if name not in ('priority', 'scheduling'))
            assert policy['process_memory'] == policy['job_memory'] == 0
        text = read(output)
        assert text.index('[P] PLANNING') < text.index(f'{key}/a [')
        assert '\x1b' not in text and 'elapsed=' not in text
        assert len(text) < 100000, 'redirected output should contain state changes, not timer floods'
        assert read(errors) == ''
        record('cli', [output, errors, tmp_path / 'wire.jsonl', tmp_path / 'provider-policy.json',
                       tmp_path / 'effects.txt'], report)
    finally:
        live = live_owner(db_path)
        if live:
            kill_pid(live['pid'])
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)
        db.close()


def test_fresh_recheck_failure_queue_survives_and_once_recovers_original_session(tmp_path):
    db, key = seeded_database(tmp_path)
    before = db.report()
    try:
        # A real owner lock without a responsive scheduler exhausts the CLI's
        # normal 10s startup budget; no entry point or launch helper is mocked.
        with service.ServiceLock(db.path):
            result = invoke(db.path, 'recheck', key)
            assert result.returncode == 2 and result.stdout == ''
            assert result.stderr.startswith('Recheck queued, but scheduler startup failed: ')
            assert 'unresponsive' in result.stderr
        queued = json.loads(invoke(db.path, 'status', '--json').stdout)
        assert queued['task'][1]['recheck'] == 'verify'
        assert queued['task'][1]['session'] == 'original-provider-session'
        assert queued['task'][0] == before['task'][0]
        assert queued['dependency'] == before['dependency']
        assert queued['artifact'] == before['artifact']
        assert queued['turn'] == []
        assert not service.owner_path(db.path).exists()
        # A fresh process performs the already queued recovery; no resubmission.
        for _ in range(2):
            step = invoke(db.path, 'run', '--once')
            assert step.returncode == 0 and step.stdout == '' and step.stderr == ''
        report = json.loads(invoke(db.path, 'status', '--json').stdout)
        assert report['objective'][0]['state'] == 'COMPLETED'
        assert report['task'][1]['session'] == 'original-provider-session'
        assert report['task'][1]['recheck'] is None
        assert report['task'][0] == before['task'][0]
        assert report['dependency'] == before['dependency']
        assert len(report['validator']) == 1
        assert any(a['kind'] == 'validation' for a in report['artifact'])
        assert len([e for e in report['event'] if e['kind'] == 'graph_persisted']) == 1
        assert len([e for e in report['event'] if e['kind'] == 'recheck_requested']) == 1
        record('startup-failure', [], {'failure': {'returncode': result.returncode,
               'stdout': result.stdout, 'stderr': result.stderr}, 'queued': queued, 'recovered': report})
    finally:
        db.close()


@pytest.mark.skipif(os.name != 'nt', reason='requires real Windows console and Ctrl+C APIs')
def test_windows_real_ctrl_c_restores_cursor_buffer_threads_and_ownership(tmp_path, monkeypatch):
    # Explicit real-host check; no isatty, console API or CLI mock.
    monkeypatch.delenv('TERM', raising=False)
    monkeypatch.delenv('NO_COLOR', raising=False)
    db, key = seeded_database(tmp_path, paused=True)
    proc = subprocess.Popen([sys.executable, str(FIXTURES / 'm1_console.py'), str(tmp_path)],
                            creationflags=subprocess.CREATE_NEW_CONSOLE)
    try:
        owner = wait_for(lambda: live_owner(db.path))
        until_text(tmp_path / 'terminal.txt', '[!] BLOCKED')
        probe = subprocess.run([sys.executable, str(FIXTURES / 'console_probe.py'),
                                str(owner['pid']), str(tmp_path / 'ctrl-c.json'), '--ctrl-c'],
                               creationflags=subprocess.CREATE_NO_WINDOW,
                               capture_output=True, text=True, timeout=10)
        assert probe.returncode == 0, probe.stderr
        assert json.loads(read(tmp_path / 'ctrl-c.json')) == {'ctrl_c_sent': True}
        assert proc.wait(timeout=10) == 0, read(tmp_path / 'terminal-error.txt')
        facts = json.loads(read(tmp_path / 'terminal-result.json'))
        assert facts['returncode'] == 130 and facts['isatty']
        assert facts['cursor_visible']
        assert 'M1 original console buffer' in facts['screen']
        assert 'orchestrator-dashboard' not in facts['threads']
        assert 'orchestrator-owner' not in facts['threads']
        trace = read(tmp_path / 'terminal.txt')
        assert trace.startswith('\x1b[?1049h\x1b[?25l')
        assert '\x1b[31m[!] BLOCKED\x1b[0m' in trace
        assert trace.endswith('\x1b[0m\x1b[?25h\x1b[?1049l')
        assert service.lock_available(db.path)
        assert not service.owner_path(db.path).exists()
        record('windows-terminal', [tmp_path / 'terminal.txt', tmp_path / 'terminal-result.json',
                                    tmp_path / 'ctrl-c.json'])
    finally:
        live = live_owner(db.path)
        if live:
            kill_pid(live['pid'])
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)
        db.close()
