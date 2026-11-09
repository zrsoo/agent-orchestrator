"""Gated M1 wire fixture. Operates only in the supplied temporary workspace."""
import json
import os
from pathlib import Path
import sys
import time

workspace = Path(sys.argv[1])
session_number = 0
turn_number = 0


def send(value):
    print(json.dumps(value), flush=True)


def gate(name):
    (workspace / ('entered-' + name)).touch()
    deadline = time.monotonic() + 45
    while not (workspace / ('release-' + name)).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(name)
        time.sleep(.02)


def job_policy():
    if os.name != 'nt':
        return {'platform': os.name}
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                    ('min_working', c.c_size_t), ('max_working', c.c_size_t),
                    ('active', w.DWORD), ('affinity', c.c_size_t),
                    ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class Counters(c.Structure):
        _fields_ = [(name, c.c_uint64) for name in
                    ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
    class Extended(c.Structure):
        _fields_ = [('basic', Basic), ('io', Counters), ('process_memory', c.c_size_t),
                    ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
    info = Extended()
    if not kernel.QueryInformationJobObject(None, 9, c.byref(info), c.sizeof(info), None):
        raise c.WinError(c.get_last_error())
    return {'flags': info.basic.flags,
            'limits': {name: getattr(info.basic, name) for name, _ in Basic._fields_ if name != 'flags'},
            'process_memory': info.process_memory, 'job_memory': info.job_memory}


for line in sys.stdin:
    message = json.loads(line)
    with (workspace / 'wire.jsonl').open('a', encoding='utf-8') as trace:
        trace.write(json.dumps(dict(message, fixture_pid=os.getpid())) + '\n')
    if 'id' not in message:
        continue
    method, params = message['method'], message['params']
    result = {}
    if method == 'initialize':
        (workspace / 'provider-policy.json').write_text(json.dumps(job_policy()))
    elif method == 'account/rateLimits/read':
        path = workspace / 'quota.json'
        result = json.loads(path.read_text()) if path.exists() else {
            'rateLimits': {'primary': {'usedPercent': 10,
                                     'windowDurationMins': 300, 'resetsAt': 2000000000}}}
    elif method in ('thread/start', 'thread/resume'):
        session_number += 1
        result = {'thread': {'id': params.get('threadId', f'm1-session-{session_number}')}}
    elif method == 'turn/start':
        turn_number += 1
        turn = f'm1-turn-{turn_number}'
        send({'id': message['id'], 'result': {'turn': {'id': turn, 'status': 'inProgress'}}})
        prompt = params['input'][0]['text']
        if 'Do not implement it yet' in prompt:
            gate('planning')
            output = {'tasks': [
                {'id': name, 'workspace': str(workspace), 'objective': f'write {name} once',
                 'acceptance': [f'{name}.txt contains {name} exactly once'],
                 'depends_on': ['a'] if name == 'b' else []}
                for name in ('a', 'b')]}
        else:
            name = 'b' if '"id": "b"' in prompt else 'a'
            gate('worker-' + name)
            marker = workspace / (name + '.txt')
            if not marker.exists():
                marker.write_text(name)
                with (workspace / 'effects.txt').open('a') as effects:
                    effects.write(name + '\n')
            output = {'status': 'done', 'validators': [{
                'command': ['{python}', 'validate.py', name], 'criteria': [0], 'timeout': 40}]}
        send({'method': 'item/completed', 'params': {
            'threadId': params['threadId'], 'turnId': turn,
            'item': {'type': 'agentMessage', 'phase': 'final_answer', 'text': json.dumps(output)}}})
        send({'method': 'turn/completed', 'params': {'threadId': params['threadId'],
              'turn': {'id': turn, 'status': 'completed', 'error': None}}})
        continue
    send({'id': message['id'], 'result': result})
