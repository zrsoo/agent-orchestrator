"""Worker-generated commands are registered separately from the task contract."""
import json
import subprocess
import sys
import time


def check_spec(spec):
    if not isinstance(spec, dict) or set(spec) - {'command', 'timeout'}:
        raise ValueError('blocker_check requires command argv and optional timeout')
    command = spec.get('command')
    timeout = spec.get('timeout', 120)
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
        raise ValueError('blocker_check command must be argv')
    if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
        raise ValueError('blocker_check timeout must be 0 < seconds <= 3600')
    return {'command': command, 'timeout': timeout}


def command_result(command, workspace, timeout):
    command = [sys.executable if part == '{python}' else part for part in command]
    start = time.time()
    try:
        proc = subprocess.run(command, cwd=workspace, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout)
        result = {'command': command, 'exit_code': proc.returncode,
                  'stdout': proc.stdout, 'stderr': proc.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {'command': command, 'exit_code': -1, 'stderr': str(exc)}
    result['seconds'] = time.time() - start
    return result


def passed(result):
    """A structured negative outcome overrides a successful process exit."""
    if result['exit_code'] != 0:
        return False
    output = result.get('stdout', '').strip()
    try:
        responses = [json.loads(output)]
    except ValueError:
        responses = []
        for line in output.splitlines():
            try:
                responses.append(json.loads(line))
            except ValueError:
                pass
    return not any(isinstance(r, dict) and str(r.get('status', '')).lower() in
                   ('blocked', 'unresolved', 'failed', 'fail', 'error') for r in responses)


def run_check(db, key, workspace, spec):
    spec = check_spec(spec)
    db.event(key, 'blocker_check_started', spec)
    result = command_result(spec['command'], workspace, spec['timeout'])
    db.execute('INSERT INTO artifact(owner,at,kind,data) VALUES(?,?,?,?)',
               (key, time.time(), 'blocker_check', json.dumps(result)))
    db.event(key, 'blocker_check', result)
    return passed(result), json.dumps(result)


def register(db, key, specs, criteria):
    if not isinstance(specs, list) or not specs:
        raise ValueError('at least one validator is required')
    covered = set()
    normalized = []
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) - {'command', 'criteria', 'timeout'}:
            raise ValueError('invalid validator fields')
        command = spec.get('command')
        indexes = spec.get('criteria')
        timeout = spec.get('timeout', 120)
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError('validator command must be argv')
        if not isinstance(indexes, list) or not indexes or any(type(x) is not int or x < 0 or x >= len(criteria) for x in indexes):
            raise ValueError('validator criteria must be zero-based acceptance indexes')
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            raise ValueError('validator timeout must be 0 < seconds <= 3600')
        covered.update(indexes)
        normalized.append((command, indexes, timeout))
    if covered != set(range(len(criteria))):
        raise ValueError('validators must cover every acceptance criterion')
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('UPDATE validator SET active=0 WHERE task_key=?', (key,))
        for cmd, indexes, timeout in normalized:
            db.execute('INSERT INTO validator(task_key,command,criteria,timeout) VALUES(?,?,?,?)',
                       (key, json.dumps(cmd), json.dumps(indexes), timeout))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def run(db, key, workspace):
    validators = db.rows('SELECT * FROM validator WHERE task_key=? AND active=1', (key,))
    if not validators:
        return False, 'No validators registered'
    results = []
    for v in validators:
        result = command_result(json.loads(v['command']), workspace, v['timeout'])
        result['validator'] = v['id']
        db.execute('INSERT INTO artifact(owner,at,kind,data) VALUES(?,?,?,?)',
                   (key, time.time(), 'validation', json.dumps(result)))
        db.event(key, 'validation', result)
        results.append(result)
    return all(passed(r) for r in results), json.dumps(results)
