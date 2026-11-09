import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest
import yaml
from agent_orchestrator.backends.codex import CodexBackend
from agent_orchestrator.backends.base import TransportError
from agent_orchestrator.cli import ServiceLock, main
from agent_orchestrator.database import Database
from agent_orchestrator.scheduler import Scheduler
from agent_orchestrator.quota import admission

FIXTURE = Path(__file__).parent / 'fixtures' / 'app_server.py'


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('condition timed out')


def config():
    return {'executable':[sys.executable,str(FIXTURE)],'rpc_timeout':5}


def test_wire_protocol_commentary_notification_and_resume(tmp_path):
    backend = CodexBackend(config())
    try:
        assert backend.quota().buckets[0].remaining == 90
        session = backend.session(str(tmp_path))
        events = []
        outcome = backend.run(session, 'Do not implement it yet', lambda *x: events.append(x), lambda: None)
        assert outcome.status == 'completed'
        assert json.loads(outcome.text)['tasks']
        assert any(x[0] == 'account/rateLimits/updated' for x in events)
        assert backend.latest_quota.buckets[0].remaining == 90
        assert backend.session(str(tmp_path),session) == session
    finally:
        backend.close()


def test_recorded_real_codex_events_select_final_answer():
    events = json.loads((FIXTURE.parent/'codex_events.json').read_text(encoding='utf-8'))
    backend = CodexBackend.__new__(CodexBackend)
    backend.pending = []
    backend.config = {}
    backend._rpc = lambda *args: {'turn':{'id':'recorded-turn'}}
    backend._receive = lambda timeout: events.pop(0)
    outcome = backend.run('recorded-session','test',lambda *args: None,lambda:None)
    assert outcome.status == 'completed'
    assert json.loads(outcome.text)['status'] == 'done'


def test_killed_stdio_worker_reconnects_reconciles(tmp_path):
    db = Database(tmp_path/'state.db')
    key = db.submit('write file',tmp_path,'codex',config())
    scheduler = Scheduler(db)
    scheduler.tick()
    (tmp_path/'hold').touch()
    backend = scheduler.backends[key]
    def kill():
        wait_for(lambda:(tmp_path/'output.txt').exists())
        backend.proc.kill()
    killer = threading.Thread(target=kill)
    killer.start()
    scheduler.tick()
    killer.join(5)
    assert db.get('task',key+'/task')['state'] == 'RECONCILING'
    assert db.get('task',key+'/task')['infra_retries'] == 1
    assert db.get('task',key+'/task')['attempts'] == 0
    (tmp_path/'hold').unlink()
    db.execute("UPDATE event SET data=json_set(data,'$.retry_at',0) WHERE kind='infrastructure_failure'")
    scheduler.tick()
    scheduler.tick()
    assert db.get('objective',key)['state'] == 'COMPLETED'
    assert (tmp_path/'output.txt').read_text() == 'done'
    assert db.rows("SELECT * FROM turn WHERE status='uncertain'")
    scheduler.close()
    db.close()


def test_detached_service_and_abrupt_python_restart(tmp_path, capsys):
    db_path = tmp_path/'state.db'
    conf = tmp_path/'factory.yaml'
    conf.write_text(yaml.safe_dump({'provider':'codex','providers':{'codex':config()}}))
    objective = tmp_path/'objective.txt'
    objective.write_text('write a checked output artifact')
    assert main(['--db',str(db_path),'--config',str(conf),'submit',str(objective),'--workspace',str(tmp_path)]) == 0
    key = capsys.readouterr().out.strip()
    (tmp_path/'hold').touch()
    assert main(['--db',str(db_path),'run','--detach','--poll','.05']) == 0
    pid = int(capsys.readouterr().out.split('pid=')[1].split()[0])
    db = Database(db_path)
    try:
        wait_for(lambda:(tmp_path/'output.txt').exists())
        # Kill Python while a worker is active. EOF then terminates the stdio fixture.
        if os.name == 'nt':
            subprocess.run(['taskkill','/PID',str(pid),'/F'],capture_output=True,check=True)
        else:
            import signal
            os.killpg(pid,signal.SIGKILL)
        (tmp_path/'hold').unlink()
        assert db.get('objective',key)['state'] not in ('COMPLETED','STOPPED')
        assert main(['--db',str(db_path),'run','--detach','--poll','.05']) == 0
        pid = int(capsys.readouterr().out.split('pid=')[1].split()[0])
        wait_for(lambda: db.get('objective',key)['state']=='COMPLETED')
        assert db.rows("SELECT * FROM turn WHERE status='uncertain'")
        assert (tmp_path/'output.txt').read_text() == 'done'
        assert len(db.rows("SELECT * FROM event WHERE kind='graph_persisted'")) == 1
        assert main(['--db',str(db_path),'status','--json','--output',str(tmp_path/'report.json')]) == 0
        assert json.loads((tmp_path/'report.json').read_text())['turn']
    finally:
        if os.name == 'nt':
            subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],capture_output=True)
        else:
            import signal
            try: os.killpg(pid,signal.SIGKILL)
            except ProcessLookupError: pass
        db.close()


def test_service_exclusion_lock(tmp_path):
    path = tmp_path/'state.db'
    with ServiceLock(path):
        with pytest.raises(ValueError,match='another orchestrator'):
            with ServiceLock(path):
                pass
        assert main(['--db',str(path),'run','--detach']) == 2


def test_cli_pins_provider_configuration_at_submission(tmp_path, capsys):
    db_path = tmp_path/'state.db'
    config_path = tmp_path/'factory.yaml'
    for model in ('first-model','second-model'):
        config_path.write_text(yaml.safe_dump({'provider':'codex','providers':{'codex':{'model':model,'execution_mode':'yolo'}}}))
        assert main(['--db',str(db_path),'--config',str(config_path),'submit','--text','implement objective','--workspace',str(tmp_path)])==0
    db = Database(db_path)
    try:
        rows = db.rows('SELECT * FROM objective ORDER BY rowid')
        assert [json.loads(r['config'])['model'] for r in rows]==['first-model','second-model']
        assert all(r['provider']=='codex' for r in rows)
    finally:
        db.close()


@pytest.mark.parametrize('notification', [True, False])
def test_wire_quota_cutoff_reset_and_authoritative_reread(tmp_path, notification):
    db = Database(tmp_path/'state.db')
    key = db.submit('write file',tmp_path,'codex',config())
    scheduler = Scheduler(db)
    try:
        scheduler.tick()
        (tmp_path/'cutoff-once').touch()
        scheduler.tick()
        task = db.get('task',key+'/task')
        assert task['state']=='WAITING_QUOTA'
        sid = task['session']
        quota_file = tmp_path/'quota.json'
        low = {'rateLimits':{'primary':{'usedPercent':100,'windowDurationMins':300,'resetsAt':time.time()-.1}}}
        quota_file.write_text(json.dumps(low))
        backend = scheduler.backends[key]
        high = {'rateLimits':{'primary':{'usedPercent':0,'windowDurationMins':300,'resetsAt':time.time()+100}}}
        if notification:
            backend._rpc('fixture/notify',high)
            backend.wait_updates(1)
            assert admission(backend.latest_quota)
        # Notification or elapsed reset is insufficient when mandatory read still says exhausted.
        scheduler.tick()
        assert db.get('task',key+'/task')['state']=='WAITING_QUOTA'
        assert len(db.rows('SELECT * FROM turn'))==2
        quota_file.write_text(json.dumps(high))
        scheduler.tick()
        scheduler.tick()
        assert db.get('objective',key)['state']=='COMPLETED'
        assert db.get('task',key+'/task')['session']==sid
        assert db.get('task',key+'/task')['attempts']==0
        assert (tmp_path/'output.txt').read_text()=='done'
    finally:
        scheduler.close()
        db.close()
