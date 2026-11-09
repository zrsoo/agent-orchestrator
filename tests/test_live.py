"""Opt-in real inference. Evidence remains in ORCHESTRATOR_LIVE_ROOT."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
import pytest
from agent_orchestrator.database import Database
from agent_orchestrator.models import graph
from agent_orchestrator.scheduler import Scheduler
from agent_orchestrator.backends.codex import CodexBackend
from agent_orchestrator.backends.base import Outcome, Quota, Bucket
from polling import complete_objective

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.getenv('ORCHESTRATOR_LIVE') != '1', reason='set ORCHESTRATOR_LIVE=1 to use Codex quota')]


def wait_for(predicate, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.25)
    raise AssertionError('live acceptance timed out')


def root(name):
    path = Path(os.getenv('ORCHESTRATOR_LIVE_ROOT', '.m1-progress/live-acceptance')).resolve() / (name+'-'+uuid.uuid4().hex[:8])
    path.mkdir(parents=True)
    return path


def kill_tree(pid):
    if os.name == 'nt':
        subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],capture_output=True)
    else:
        import signal
        os.kill(pid,signal.SIGKILL)


def test_live_detached_planning_dag_pause_resume(tmp_path):
    path = root('dag')
    workspace = path/'workspace'
    workspace.mkdir()
    db_path = path/'state.db'
    def cli(*args):
        p = subprocess.run([sys.executable,'-m','agent_orchestrator','--db',str(db_path),*args],capture_output=True,text=True,timeout=30)
        assert p.returncode == 0, p.stderr
        return p.stdout.strip()
    key = cli('submit','--text','Use exactly two tasks. First create a.txt containing alpha. Second read a.txt and create b.txt containing alpha-beta; it depends on the first. Generate Python artifact checks for each task.','--workspace',str(workspace))
    # Submission and service-launch processes exit; detached Python carries the work.
    output = cli('run','--detach','--poll','.2')
    pid = int(output.split('pid=')[1].split()[0])
    db = Database(db_path)
    try:
        wait_for(lambda:len(db.rows('SELECT * FROM turn')) >= 1)
        cli('pause',key)
        wait_for(lambda:db.get('objective',key)['state']=='PAUSED')
        assert not (workspace/'a.txt').exists()
        cli('resume',key)
        wait_for(lambda:db.get('objective',key)['state'] in ('COMPLETED','BLOCKED'))
        assert db.get('objective',key)['state']=='COMPLETED',db.report()
        assert (workspace/'a.txt').read_text().strip()=='alpha'
        assert (workspace/'b.txt').read_text().strip()=='alpha-beta'
        assert len(db.rows('SELECT * FROM task'))==2
        assert len(db.rows('SELECT * FROM dependency'))==1
        assert len(db.rows('SELECT * FROM turn'))==3
        assert all(t['provider_turn'] for t in db.rows('SELECT * FROM turn'))
        cli('status','--json','--output',str(path/'report.json'))
    finally:
        kill_tree(pid)
        db.close()


def test_live_killed_worker_same_session_reconciliation():
    path = root('worker-kill')
    workspace = path/'workspace'
    workspace.mkdir()
    db = Database(path/'state.db')
    key = db.submit('cutoff proof',workspace,'codex',{'model':'default','execution_mode':'yolo',
                                                        'rpc_timeout':30,'turn_timeout':120})
    objective = ('Create progress.txt containing exactly once, only if it does not exist. '
                 'When newly creating it, in that SAME shell command flush the file and then sleep for 45 seconds. '
                 'If progress.txt already exists, preserve it and skip the sleep. '
                 'Then create a Python validator that asserts its text equals once. Do not append or duplicate the side effect.')
    db.save_graph(key, graph([{'id':'recover','workspace':str(workspace),'objective':objective,
                             'acceptance':['progress.txt contains exactly once; independent Python validator passes']}]))
    scheduler = Scheduler(db)
    failures = []
    def killer():
        try:
            wait_for(lambda:(workspace/'progress.txt').exists(),timeout=180)
            kill_tree(scheduler.backends[key].proc.pid)
        except Exception as exc:
            failures.append(exc)
    thread = threading.Thread(target=killer,daemon=True)
    thread.start()
    try:
        scheduler.tick()
        thread.join(5)
        assert not failures
        task = db.get('task',key+'/recover')
        assert task['state']=='RECONCILING',task
        session = task['session']
        assert task['infra_retries']==1 and task['attempts']==0
        assert not thread.is_alive(), 'worker killer did not finish'
        assert session is not None
        complete_objective(scheduler, db, key)
        assert db.get('task',key+'/recover')['state']=='COMPLETED'
        assert db.get('task',key+'/recover')['session']==session
        assert (workspace/'progress.txt').read_text()=='once'
        assert len(db.rows("SELECT * FROM turn WHERE status='uncertain'"))==1
    finally:
        (path/'report.json').write_text(json.dumps(db.report(),indent=2))
        scheduler.close()
        db.close()


def test_live_injected_quota_cutoff_wait_and_reconcile():
    path = root('quota-cutoff')
    workspace = path/'workspace'
    workspace.mkdir()
    marker = workspace/'progress.txt'

    class CutoffBackend(CodexBackend):
        cutoff = False
        reset = False

        def quota(self):
            reading = super().quota()  # Real mandatory read, even during injected exhaustion.
            if self.cutoff and not self.reset:
                return Quota([Bucket('injected-five-hour',0,300,time.time()+30,2)])
            return reading

        def run(self, session_id, prompt, emit, control):
            def inject_control():
                return 'stop' if marker.exists() and not self.cutoff else control()
            outcome = super().run(session_id,prompt,emit,inject_control)
            if outcome.status == 'interrupted' and not self.cutoff:
                self.cutoff = True
                return Outcome('quota',diagnostic='O6 injected quota cutoff via real turn/interrupt')
            return outcome

    db = Database(path/'state.db')
    key = db.submit('quota proof',workspace,'codex',{'model':'default','execution_mode':'yolo'})
    db.save_graph(key, graph([{'id':'cutoff','workspace':str(workspace),
        'objective':'Create progress.txt with exactly once only if missing, flush it, and sleep 30 seconds in that same shell command ONLY on first creation. If file exists skip sleep and preserve it. Then create a Python artifact validator.',
        'acceptance':['progress.txt contains exactly once; Python artifact validator passes']}]))
    scheduler = Scheduler(db,{'codex':CutoffBackend})
    try:
        scheduler.tick()
        task = db.get('task',key+'/cutoff')
        assert task['state']=='WAITING_QUOTA',task
        session = task['session']
        scheduler.tick()
        assert len(db.rows('SELECT * FROM turn'))==1
        scheduler.backends[key].reset = True
        scheduler.tick()
        scheduler.tick()
        assert db.get('objective',key)['state']=='COMPLETED',db.report()
        assert db.get('task',key+'/cutoff')['session']==session
        assert marker.read_text()=='once'
        assert len(db.rows('SELECT * FROM turn'))==2
        assert db.get('task',key+'/cutoff')['attempts']==0
        (path/'report.json').write_text(json.dumps(db.report(),indent=2))
    finally:
        scheduler.close()
        db.close()
