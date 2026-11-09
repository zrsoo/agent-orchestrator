"""Single-worker durable lifecycle. Backend instances never own scheduling state."""
from dataclasses import asdict
import json
from pathlib import Path
import time
from .backends.base import InputSizeError, TransportError, QuotaExhausted
from .backends.codex import CodexBackend
from .executor import checkpoint
from .models import graph, TERMINAL
from .quota import DEFAULT_POLL_SECONDS, admission, wake_delay
from .backends.base import Bucket, Quota
from .recovery import disrupted
from .prompts import check_prompt, recovery_summary
from . import validation


def decode(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    return json.loads(text)


class Scheduler:
    def __init__(self, db, factories=None, *, output=print):
        self.db = db
        self.factories = factories or {'codex': CodexBackend}
        self.backends = {}
        self.recovered = False
        self.output = output

    def backend(self, obj):
        # Configuration is pinned at submission, including model/execution settings.
        key = obj['id']
        if key not in self.backends:
            if obj['provider'] not in self.factories:
                raise ValueError(f'backend unavailable: {obj["provider"]}')
            self.backends[key] = self.factories[obj['provider']](json.loads(obj['config']))
        return self.backends[key]

    def recover(self):
        for table in ('objective', 'task'):
            for row in self.db.rows(f"SELECT * FROM {table} WHERE state IN ('RUNNING','VALIDATING','RECONCILING','RETRYING','FIXING_PERSISTENT_FAILURE')"):
                key = row['key'] if table == 'task' else row['id']
                # Keep the original blocker/failure diagnostic available to the next worker.
                self.db.transition(table, key, 'RECONCILING')
        self.db.execute("UPDATE turn SET status='uncertain',ended=? WHERE status='running'", (time.time(),))
        self.recovered = True

    def controls(self, obj_id, task_key=None):
        obj = self.db.get('objective', obj_id)
        if obj['state'] == 'STOPPED' or obj['control'] == 'stop':
            return 'stop'
        if task_key:
            task = self.db.get('task', task_key)
            if task['state'] == 'STOPPED' or task['control'] == 'stop':
                return 'stop'
            if task['control'] == 'pause' or task['state'] == 'PAUSED':
                return 'pause'
        return 'pause' if obj['control'] == 'pause' or obj['state'] == 'PAUSED' else None

    def quota_ready(self, backend, table, key):
        quota = backend.quota()
        latest = self.db.rows("SELECT data FROM event WHERE owner=? AND kind='quota' ORDER BY id DESC LIMIT 1", (key,))
        if not latest or json.loads(latest[0]['data']) != asdict(quota):
            self.db.event(key, 'quota', asdict(quota))
        if not admission(quota):
            if self.db.get(table, key)['state'] != 'WAITING_QUOTA':
                self.db.transition(table, key, 'WAITING_QUOTA')
                self.output(f'{key}: WAITING_QUOTA (automatic quota reread)', flush=True)
            return False
        return True

    def turn(self, obj, table, key, workspace, prompt, kind):
        check_prompt(prompt)
        backend = self.backend(obj)
        owner = self.db.get(table, key)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        backend.emit = lambda method, data: self.db.event(key, method, data)
        session = backend.session(workspace, owner['session'])
        self.db.execute(f'UPDATE {table} SET session=? WHERE {"key" if table == "task" else "id"}=?', (session, key))
        turn = self.db.execute('INSERT INTO turn(owner,session,kind,status,started) VALUES(?,?,?,?,?)',
                               (key, session, kind, 'running', time.time())).lastrowid
        checkpoint(self.db, key, workspace, 'before_turn')

        def emit(method, data):
            self.db.event(key, method, data, turn)
            if method == 'turn_identity':
                self.db.execute('UPDATE turn SET provider_turn=? WHERE id=?', (data['turn'], turn))

        try:
            outcome = backend.run(session, prompt, emit,
                                  lambda: self.controls(obj['id'], key if table == 'task' else None))
        except QuotaExhausted as exc:
            from .backends.base import Outcome
            self.db.execute("UPDATE turn SET status='quota',ended=?,result=? WHERE id=?", (time.time(), str(exc), turn))
            checkpoint(self.db, key, workspace, 'quota')
            return Outcome('quota', diagnostic=str(exc))
        except InputSizeError as exc:
            self.db.execute("UPDATE turn SET status='rejected',ended=?,result=? WHERE id=?", (time.time(), str(exc), turn))
            raise
        except BaseException as exc:
            self.db.execute("UPDATE turn SET status='uncertain',ended=?,result=? WHERE id=?", (time.time(), str(exc), turn))
            checkpoint(self.db, key, workspace, 'uncertain')
            raise
        self.db.execute('UPDATE turn SET status=?,ended=?,result=? WHERE id=?',
                        (outcome.status, time.time(), json.dumps(asdict(outcome)), turn))
        checkpoint(self.db, key, workspace, outcome.status)
        return outcome

    def infra_failure(self, obj, table, key, exc):
        row = self.db.get(table, key)
        retries = row['infra_retries'] + 1
        self.db.execute(f'UPDATE {table} SET infra_retries=? WHERE {"key" if table == "task" else "id"}=?', (retries, key))
        self.db.event(key, 'infrastructure_failure', {'retry': retries, 'error': str(exc),
                                                    'retry_at': time.time() + min(60, 2 ** min(retries, 6))})
        self.db.transition(table, key, 'RECONCILING', str(exc))
        backend = self.backends.pop(obj['id'], None)
        if backend:
            backend.close()

    def plan(self, obj):
        key = obj['id']
        backend = self.backend(obj)
        if not self.quota_ready(backend, 'objective', key):
            return
        self.db.transition('objective', key, 'RUNNING')
        prompt = ('Plan this objective into a small sequential task DAG. Do not implement it yet. '
                  'Return ONLY JSON {"tasks":[...]}. Each task has exactly id, workspace, objective, '
                  'acceptance (nonempty list of strings), optional inputs (list of strings), '
                  'optional depends_on (list of IDs). Workspace must be absolute. No provider/model fields. '
                  'Use workspace ' + obj['workspace'] + '. Objective:\n' + obj['text'])
        if obj['session']:
            prompt += '\nPrior planning was interrupted or invalid. Inspect previous messages; provide the complete graph again.'
        outcome = self.turn(obj, 'objective', key, obj['workspace'], prompt, 'planning')
        if outcome.status == 'quota':
            self.db.transition('objective', key, 'WAITING_QUOTA', outcome.diagnostic)
            return
        if self.controls(key) == 'stop':
            return
        if outcome.status != 'completed':
            self.db.transition('objective', key, 'BLOCKED', outcome.diagnostic or outcome.text)
            return
        try:
            tasks = graph(decode(outcome.text)['tasks'])
        except (ValueError, KeyError, TypeError) as exc:
            self.db.transition('objective', key, 'BLOCKED', f'Invalid planning output: {exc}')
            return
        self.db.save_graph(key, tasks)
        self.db.transition('objective', key, 'PAUSED' if self.controls(key) == 'pause' else 'PENDING')

    def execute_task(self, obj, task):
        key = task['key']
        spec = json.loads(task['spec'])
        if task['recheck'] == 'verify' and task['blocker_check']:
            self.db.transition('task', key, 'RECONCILING')
            ok, evidence = validation.run_check(self.db, key, spec['workspace'], json.loads(task['blocker_check']))
            if self.controls(obj['id'], key) == 'stop':
                return
            if not ok:
                self.db.transition('task', key, 'BLOCKED', evidence)
                return
            self.db.execute("UPDATE task SET recheck='continue',attempts=0 WHERE key=?", (key,))
            self.db.event(key, 'blocker_resolved', {'evidence': evidence})
            task = self.db.get('task', key)
            if self.controls(obj['id'], key) == 'pause':
                return
        if task['accepted_stage'] == 'validated':
            # Pause after a completed turn must not cause another worker turn.
            self.db.transition('task', key, 'VALIDATING')
            ok, evidence = validation.run(self.db, key, spec['workspace'])
            if ok:
                self.db.transition('task', key, 'COMPLETED')
                return
            self.db.execute('UPDATE task SET accepted_stage=NULL WHERE key=?', (key,))
            disrupted(self.db, key, evidence)
            return
        backend = self.backend(obj)
        if not self.quota_ready(backend, 'task', key):
            return
        last_turn = self.db.rows('SELECT status FROM turn WHERE owner=? ORDER BY id DESC LIMIT 1', (key,))
        interrupted = last_turn and last_turn[0]['status'] in ('quota', 'uncertain', 'interrupted')
        state = 'RECONCILING' if task['session'] and (interrupted or task['state'] in ('WAITING_QUOTA', 'RECONCILING', 'RETRYING')) else 'FIXING_PERSISTENT_FAILURE' if task['attempts'] >= 2 else 'RUNNING'
        self.db.transition('task', key, state)
        prompt = ('Implement and verify this task in its workspace. Create meaningful tests/artifact checks for ALL acceptance criteria. '
                  'Return ONLY a final JSON object: {"status":"done"|"unresolved"|"blocked", "diagnostic":"...", '
                  '"validators":[{"command":["{python}","test_file.py"],"criteria":[0],"timeout":120}]}. '
                  'Commands are argv, run independently from the task workspace; {python} means supervisor Python. '
                  'criteria indexes are zero-based; cover all criteria. Do not merely assert success. '
                  'If blocked, optionally include "blocker_check":{"command":["{python}","check.py"],"timeout":120} '
                  'for an existing deterministic, repeatable verification of human-intervention prerequisites only. '
                  'Do not use full task acceptance checks or require outputs the worker must generate after resuming. '
                  'Keep full acceptance validation in validators after the remaining work. '
                  'The blocker check must report nonzero exit or '
                  'JSON status BLOCKED while unresolved. '
                  'Task:\n' + json.dumps(spec))
        if task['recheck']:
            prompt += ('\nHuman intervention recheck in this SAME session. Preserve completed work, checkpoints and the original '
                       'acceptance criteria; do not regenerate the plan or repeat completed side effects. '
                       'If it remains, return blocked with the specific reason. If resolved, continue only the remaining '
                       'task work and return done with validators covering the original acceptance criteria. '
                       'A resolved blocker alone does not complete the task. Original blocker:\n' + task['diagnostic'])
            if task['recheck'] == 'verify':
                prompt += ('\nFirst verify the original blocker, running any existing deterministic blocker-verification command '
                           'before other work. A zero process exit does not override JSON status BLOCKED.')
            else:
                prompt += '\nThe blocker check already succeeded; continue remaining work without repeating that check unless new evidence requires it.'
        if state == 'RECONCILING':
            prompt += recovery_summary(self.db, obj, task)
        if task['attempts']:
            prompt += '\nUnresolved failure evidence:\n' + task['diagnostic']
        if task['attempts'] >= 2:
            prompt += '\nFIXING_PERSISTENT_FAILURE: diagnose the root cause and use an alternative approach in this same worker. If no viable next action or owner input is required, return blocked.'
        outcome = self.turn(obj, 'task', key, spec['workspace'], prompt, state)
        if self.controls(obj['id'], key) == 'stop':
            return
        if outcome.status in ('quota', 'interrupted'):
            self.db.transition('task', key, 'WAITING_QUOTA' if outcome.status == 'quota' else 'RECONCILING', outcome.diagnostic)
            return
        if outcome.status != 'completed':
            disrupted(self.db, key, outcome.diagnostic or outcome.text, outcome.status == 'blocked')
            return
        try:
            result = decode(outcome.text)
            if result['status'] == 'blocked':
                if result.get('blocker_check') is not None:
                    check = validation.check_spec(result['blocker_check'])
                    self.db.execute('UPDATE task SET blocker_check=? WHERE key=?', (json.dumps(check), key))
                disrupted(self.db, key, result.get('diagnostic', 'Unresolved blocker'), True)
                return
            if result['status'] != 'done':
                disrupted(self.db, key, result.get('diagnostic', 'Unresolved handoff'), result['status'] == 'blocked')
                return
            validation.register(self.db, key, result.get('validators'), spec['acceptance'])
            if task['recheck'] == 'verify':
                self.db.execute("UPDATE task SET recheck='continue',attempts=0 WHERE key=? AND recheck='verify'", (key,))
                self.db.event(key, 'blocker_resolved', {'source': 'provider', 'diagnostic': result.get('diagnostic', '')})
        except (ValueError, KeyError, TypeError) as exc:
            disrupted(self.db, key, f'Invalid worker result: {exc}; output: {outcome.text}')
            return
        self.db.transition('task', key, 'VALIDATING')
        ok, evidence = validation.run(self.db, key, spec['workspace'])
        checkpoint(self.db, key, spec['workspace'], 'validated' if ok else 'validation_failed')
        if ok:
            self.db.execute("UPDATE task SET attempts=0,diagnostic='',accepted_stage='validated' WHERE key=?", (key,))
            self.db.transition('task', key, 'PAUSED' if self.controls(obj['id'], key) == 'pause' else 'COMPLETED')
        else:
            disrupted(self.db, key, evidence)

    def tick(self):
        if not self.recovered:
            self.recover()
        for obj in self.db.rows("SELECT * FROM objective WHERE state NOT IN ('COMPLETED','BLOCKED','STOPPED') ORDER BY created"):
            key = obj['id']
            control = self.controls(key)
            if control:
                if control == 'pause' and obj['state'] != 'PAUSED':
                    self.db.transition('objective', key, 'PAUSED')
                continue
            tasks = self.db.rows('SELECT * FROM task WHERE objective_id=? ORDER BY rowid', (key,))
            table, owner = 'objective', key
            try:
                if not tasks:
                    retries = self.db.rows("SELECT data FROM event WHERE owner=? AND kind='infrastructure_failure' ORDER BY id DESC LIMIT 1", (key,))
                    if obj['state'] == 'RECONCILING' and retries and json.loads(retries[0]['data']).get('retry_at', 0) > time.time():
                        continue
                    self.plan(obj)
                    return True
                if all(t['state'] == 'COMPLETED' for t in tasks):
                    self.db.transition('objective', key, 'COMPLETED')
                    continue
                if (any(t['state'] in ('BLOCKED', 'STOPPED') for t in tasks)
                        and self.db.block_objective_if_unrecoverable(key)):
                    continue
                states = {t['key']: t['state'] for t in tasks}
                for task in sorted(tasks, key=lambda t: not bool(t['recheck'])):
                    if task['state'] in TERMINAL or self.controls(key, task['key']):
                        continue
                    deps = self.db.rows('SELECT depends_key FROM dependency WHERE task_key=?', (task['key'],))
                    if any(states[d['depends_key']] != 'COMPLETED' for d in deps):
                        continue
                    retries = self.db.rows("SELECT data FROM event WHERE owner=? AND kind='infrastructure_failure' ORDER BY id DESC LIMIT 1", (task['key'],))
                    if task['state'] == 'RECONCILING' and retries and json.loads(retries[0]['data']).get('retry_at', 0) > time.time():
                        continue
                    table, owner = 'task', task['key']
                    self.db.transition('objective', key, 'RUNNING')
                    self.execute_task(obj, task)
                    new_state = self.db.get('task', owner)['state']
                    if self.controls(key, owner) == 'pause' and new_state not in TERMINAL:
                        self.db.transition('task', owner, 'PAUSED')
                    if self.controls(key) == 'pause':
                        self.db.transition('objective', key, 'PAUSED')
                    elif new_state == 'WAITING_QUOTA':
                        self.db.transition('objective', key, 'WAITING_QUOTA')
                    return True
            except QuotaExhausted as exc:
                self.db.transition(table, owner, 'WAITING_QUOTA', str(exc))
                return True
            except InputSizeError as exc:
                self.db.event(owner, 'input_rejected', {'error': str(exc), 'retryable': False,
                                                       'prior_diagnostic': self.db.get(table, owner)['diagnostic']})
                self.db.transition(table, owner, 'BLOCKED', str(exc))
                return True
            except TransportError as exc:
                self.infra_failure(obj, table, owner, exc)
                return True
            except (ValueError, OSError) as exc:
                self.db.transition(table, owner, 'BLOCKED', str(exc))
                return True
        return False

    def wait(self, seconds):
        delay = min(seconds, DEFAULT_POLL_SECONDS)
        waiting = self.db.rows("SELECT data FROM event WHERE kind='quota' ORDER BY id DESC LIMIT 1")
        if waiting:
            raw = json.loads(waiting[0]['data'])
            delay = min(delay, wake_delay(Quota([Bucket(**b) for b in raw['buckets']], raw['allowed'])))
        for backend in list(self.backends.values()):
            wait = getattr(backend, 'wait_updates', None)
            if wait:
                try:
                    wait(delay)
                except TransportError:
                    pass  # Next tick reconnects and records the infrastructure failure.
                return
        time.sleep(delay)

    def close(self):
        backends = list(self.backends.values())
        self.backends.clear()
        failure = None
        for backend in backends:
            try:
                backend.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure
