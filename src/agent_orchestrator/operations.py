"""Opt-in operational tasks: durable repair, quota-free checks and owned launches.

Consumer checks decide operational health. This module never interprets consumer
transactions or retries an uncertain external write.
"""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from . import validation
from .models import Task
from .prompts import preview


def row(db, key):
    db.execute('INSERT OR IGNORE INTO operation(task_key) VALUES(?)', (key,))
    return db.rows('SELECT * FROM operation WHERE task_key=?', (key,))[0]


def retry(db, key, reason, *, assessment=False, delay=2):
    row(db, key)
    db.execute('''UPDATE operation SET pending=?,retry_at=?,healthy=0,last_mono=NULL,
                  assessment=CASE WHEN ? THEN ? ELSE assessment END WHERE task_key=?''',
               (reason, time.time() + delay, int(assessment), reason, key))
    db.event(key, 'operational_recovery', {'reason': reason, 'assessment': assessment})
    db.transition('task', key, 'FIXING_PERSISTENT_FAILURE', reason)


def plan(db, obj):
    task = Task('operate', obj['workspace'], obj['text'], [
        'Original objective fulfilled, fixes regression-tested and committed/pushed; '
        'retained genuine operational evidence independently validates all required behavior.',
        f'At least {obj["healthy_seconds"]:g} consecutive monitored healthy seconds '
        'in the final runtime generation; leave the required healthy service running.'
    ], inputs=[obj['source_file']] if obj['source_file'] else [])
    db.save_graph(obj['id'], [task])
    row(db, obj['id'] + '/operate')


def monitor_spec(value):
    if not isinstance(value, dict) or set(value) - {'check', 'stop', 'interval', 'startup_seconds'}:
        raise ValueError('monitor requires check, stop, interval')
    check = validation.check_spec(value['check'])
    stop = validation.check_spec(value['stop'])
    interval = value.get('interval', 30)
    startup = value.get('startup_seconds', 90)
    if (type(interval) not in (int, float) or not math.isfinite(interval)
            or not 5 <= interval <= 300 or check['timeout'] > 60 or stop['timeout'] > 60):
        raise ValueError('monitor interval must be 5..300; check/stop timeout <=60 seconds')
    if type(startup) not in (int, float) or not math.isfinite(startup) or not 0 <= startup <= 300:
        raise ValueError('startup_seconds must be 0..300')
    return {'check': check, 'stop': stop, 'interval': interval, 'startup_seconds': startup}


def stop(db, key, workspace, spec, reason):
    result = validation.command_result(spec['stop']['command'], workspace, spec['stop']['timeout'])
    db.event(key, 'operational_stop', {'reason': reason, 'result': result})
    return result


def launch(db, key, workspace, service):
    """Launch from scheduler, outside the ephemeral provider process/job tree.

    Intent precedes spawn. A crash in between is uncertain, never auto-replayed.
    Consumers must use an exclusive runtime lock and independently reconcile it.
    """
    if not isinstance(service, dict) or set(service) != {'id', 'command'}:
        raise ValueError('service requires unique id and public command argv')
    identity = service['id']
    if not isinstance(identity, str) or not identity or len(identity) > 128:
        raise ValueError('invalid service launch id')
    command = validation.check_spec({'command': service['command']})['command']
    db.execute('''CREATE TABLE IF NOT EXISTS operational_launch (
        task_key TEXT REFERENCES task(key), id TEXT, state TEXT, pid INTEGER,
        log TEXT, PRIMARY KEY(task_key,id))''')
    if db.rows('SELECT * FROM operational_launch WHERE task_key=? AND id=?', (key, identity)):
        db.event(key, 'operational_launch_reconciled', {'id': identity, 'replayed': False})
        return
    log = db.path.parent / ('consumer-' + uuid.uuid4().hex[:12] + '.log')
    db.execute('INSERT INTO operational_launch VALUES(?,?,?,?,?)',
               (key, identity, 'intent', None, str(log)))
    db.event(key, 'operational_launch_intent', service)
    argv = [sys.executable if x == '{python}' else x for x in command]
    flags = ({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
              | subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True})
    try:
        with log.open('ab') as stream:
            child = subprocess.Popen(argv, cwd=workspace, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=stream, **flags)
        db.execute('UPDATE operational_launch SET state=?,pid=? WHERE task_key=? AND id=?',
                   ('spawned', child.pid, key, identity))
        db.event(key, 'operational_launch', {'id': identity, 'pid': child.pid, 'log': str(log)})
        return child
    except OSError:
        db.execute('UPDATE operational_launch SET state=? WHERE task_key=? AND id=?',
                   ('uncertain', key, identity))
        raise


def poll(scheduler, obj, task, *, force=False):
    db, key = scheduler.db, task['key']
    op = row(db, key)
    if not op['monitor'] or not force and op['next_at'] > time.time():
        return op
    spec = json.loads(op['monitor'])
    workspace = json.loads(task['spec'])['workspace']
    result = validation.command_result(spec['check']['command'], workspace, spec['check']['timeout'])
    try:
        report = json.loads(result.get('stdout', ''))
        if (result['exit_code'] != 0 or not isinstance(report, dict)
                or report.get('status') not in ('healthy', 'repair', 'starting')
                or not isinstance(report.get('generation'), str) or not report['generation']):
            raise ValueError('check must return exit 0 and JSON status healthy|repair|starting with generation')
        if report['status'] == 'starting' and time.time() > op['startup_until']:
            raise ValueError('starting reported outside the scheduler-owned startup grace')
    except (ValueError, TypeError) as exc:
        report = {'status': 'repair', 'generation': op['generation'], 'diagnostic': str(exc)}
    now = scheduler.monotonic()
    healthy = 0
    if report['status'] == 'healthy':
        gap = now - op['last_mono'] if op['last_mono'] is not None else None
        if (op['epoch'] == scheduler.epoch and report['generation'] == op['generation']
                and gap is not None and 0 <= gap <= 2 * spec['interval'] + spec['check']['timeout']):
            healthy = op['healthy'] + gap
        else:
            db.event(key, 'healthy_interval_reset', {'previous': op['healthy'],
                     'generation': report['generation'], 'reason': 'first check, restart, gap or generation change'})
    evidence = {'result': result, 'report': report, 'healthy_seconds': healthy,
                'required_seconds': obj['healthy_seconds'], 'epoch': scheduler.epoch}
    db.event(key, 'operational_health', evidence)
    if report['status'] == 'healthy' and not op['pending']:
        db.execute('UPDATE task SET diagnostic=? WHERE key=?',
                   (f'Healthy {healthy:.1f}/{obj["healthy_seconds"]:g}s; generation '
                    + report['generation'] + '; ' + str(report.get('diagnostic', '')), key))
    elif report['status'] == 'starting' and not op['pending']:
        db.execute('UPDATE task SET diagnostic=? WHERE key=?',
                   ('Starting owned consumer; healthy interval has not begun', key))
    db.execute('''UPDATE operation SET healthy=?,generation=?,last_mono=?,epoch=?,next_at=?,faulted=?
                  WHERE task_key=?''', (healthy, report.get('generation'),
                                       now if report['status'] == 'healthy' else None, scheduler.epoch,
                                       time.time() + spec['interval'], int(report['status'] == 'repair'), key))
    if report['status'] == 'repair':
        if not op['faulted']:
            stopped = stop(db, key, workspace, spec, 'health check failed')
            retry(db, key, json.dumps({'incident': evidence, 'safe_stop': stopped}))
    elif report['status'] == 'healthy' and healthy >= obj['healthy_seconds'] and not op['pending']:
        db.execute('UPDATE operation SET pending=? WHERE task_key=?',
                   ('Healthy interval reached: independently validate evidence, publish report and finalize.', key))
        db.transition('task', key, 'RUNNING', 'Healthy interval reached; final evidence review')
    elif not op['pending'] and task['state'] != 'MONITORING':
        db.transition('task', key, 'MONITORING')
    return row(db, key)


def prompt(db, obj, task):
    op = row(db, task['key'])
    return '''
AUTONOMOUS OPERATIONAL MODE (opt-in). Continue in this SAME task/session until the
original objective is fulfilled. Plan, investigate, fix, test and update the
working-plan/progress sections of the objective source file; never weaken the
original acceptance criteria saved in SQLite. Read that file on each resumption.
Ordinary defects, failed methods, stopped consumer processes or ambiguous writes
are NOT reasons to request manual recheck. Safely stop side effects as needed,
reconcile actual outcomes, diagnose alternatives, repair and resume. Never blindly
repeat uncertain writes. Preserve historical evidence and explicit user controls.
Maintain a concise durable hypothesis/action/result/next-action log. On repeated
failure choose a different evidenced approach rather than repeating indefinitely.

Return ONLY final JSON using one of these handoffs:
1. {"status":"continue","diagnostic":"completed work, next concrete action"}
   for a durable checkpoint between repair stages; automatic same-session resume.
   May include the same monitor/service fields below to launch a persistent
   service then continue browser attachment/startup work in the next worker turn.
2. {"status":"monitoring","diagnostic":"...","monitor":{
   "check":{"command":["{python}","health.py"],"timeout":30},
   "stop":{"command":["{python}","stop.py"],"timeout":30},"interval":30,
   "startup_seconds":90},
   "service":{"id":"unique-launch-intent","command":["{python}","watchdog.py"]}}
   Service is optional if already independently running. To start persistent
   services, use this scheduler-owned service handoff, NOT a provider/tool child:
   tool descendants can be killed on quota/backend reconnect. Public argv only.
   Exactly one runtime owner; implement exclusive lock, journal before dispatch,
   restart reconciliation, idempotent ownership-checked stop and startup grace.
   Health command is bounded, deterministic, repeatable, zero account writes,
   strict JSON {"status":"healthy"|"repair"|"starting","generation":"runtime-unique-id",
   "diagnostic":"...","evidence":[...]}, exit 0. Health MUST examine semantic
   success/freshness/failure streaks, not merely process life or heartbeat. It may
   safely stop a broken runtime, never replay writes, repair or restart it.
   Starting is allowed only within scheduler-owned startup grace (max 300s from
   a genuinely new service launch), never counts as healthy. It cannot extend
   indefinitely by returning monitoring or replaying a consumed launch id.
   Stop must only stop this task's runtime, return promptly and acknowledge it.
   Scheduler polls checks without AI quota, including during worker turns. A
   healthy service survives quota waits; stopped services await automatic repair.
   Fault/generation change, scheduler restart or an unmonitored gap resets the
   consecutive healthy interval. Report genuine operational evidence as required
   by the objective; elapsed time alone is insufficient. Do not busy-wait four
   hours inside an AI turn. Return monitoring and let deterministic checks run.
3. {"status":"done","validators":[...]} only AFTER all original criteria plus
   the required healthy interval. Independent validators must cover every task
   acceptance index. Leave healthy services running if the objective requires it.
4. {"status":"blocked","diagnostic":"...","escalation":{
   "category":"human_challenge"|"account_restriction"|"external_dependency",
   "attempted_actions":["action and actual result"],"remaining_safe_actions":[],
   "evidence_files":["existing sanitized evidence path"],
   "required_change":"precise unavailable external condition or human action",
   "safe_state":"which services stopped, uncertain effects protected"}}
   This PROPOSES escalation, never immediately blocks. The next same-session
   turn must assess it, execute any remaining safe authorized recovery, and only
   confirm the evidenced external barrier when recovery is genuinely unavailable.
   Legacy blocked/unresolved/malformed results automatically return to repair.

Keep checks/monitor/stop scripts and regression tests in the consumer workspace.
Full histories stay in SQLite events/artifacts; read them narrowly, not wholesale.
Source file: ''' + str(obj['source_file']) + '\nOperational state (bounded previews): ' + json.dumps({
        k: preview(v, 3000) if k in ('pending', 'assessment') and v else v for k, v in op.items()})


def accept_blocker(db, obj, task, result):
    op = row(db, task['key'])
    e = result.get('escalation')
    if not op['assessment'] or not isinstance(e, dict):
        return False
    if (e.get('category') not in ('human_challenge', 'account_restriction', 'external_dependency')
            or e.get('remaining_safe_actions') != [] or not e.get('attempted_actions')
            or not e.get('required_change') or not e.get('safe_state') or not e.get('evidence_files')):
        return False
    workspace = Path(json.loads(task['spec'])['workspace'])
    if not all(isinstance(p, str) and (workspace / p).is_file() for p in e['evidence_files']):
        return False
    if op['monitor']:
        stopped = stop(db, task['key'], str(workspace), json.loads(op['monitor']), 'external escalation')
        if not validation.passed(stopped):
            db.event(task['key'], 'escalation_stop_unconfirmed', stopped)
            return False
    db.event(task['key'], 'recovery_assessment', {'proposal': op['assessment'], 'confirmed': e})
    db.transition('task', task['key'], 'BLOCKED', result.get('diagnostic', '') + ' ' + json.dumps(e))
    return True


def handoff(scheduler, obj, task, result):
    """True means consumed; False continues normal independent done validation."""
    db, key = scheduler.db, task['key']
    status = result['status']
    if status == 'blocked' and accept_blocker(db, obj, task, result):
        return True
    if status == 'monitoring' or status == 'continue' and result.get('service'):
        spec = monitor_spec(result['monitor'])
        row(db, key)
        db.execute('''UPDATE operation SET monitor=?,pending=NULL,assessment=NULL,next_at=0
                      WHERE task_key=?''', (json.dumps(spec), key))
        if result.get('service'):
            child = launch(db, key, json.loads(task['spec'])['workspace'], result['service'])
            if child:
                scheduler.consumers.append(child)
                db.execute('UPDATE operation SET startup_until=? WHERE task_key=?',
                           (time.time() + spec['startup_seconds'], key))
        if status == 'continue':
            retry(db, key, result.get('diagnostic', 'Continue service attachment/startup'), delay=0)
        poll(scheduler, obj, db.get('task', key), force=True)
        return True
    if status == 'done':
        op = poll(scheduler, obj, task, force=True)
        if obj['healthy_seconds'] and (not op['monitor'] or op['healthy'] < obj['healthy_seconds']):
            retry(db, key, 'Premature completion: required consecutive healthy interval not met')
            return True
        if op['assessment']:
            retry(db, key, 'Resolve or confirm pending external recovery assessment before completing')
            return True
        return False
    retry(db, key, result.get('diagnostic', 'Continue investigation'), assessment=status == 'blocked')
    return True
