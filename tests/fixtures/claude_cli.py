"""Deterministic Claude Code stream-json CLI; runs as a real child process.

Workspace marker files select faults. Sessions persist under .claude-fixture/.
"""
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid

args = sys.argv[1:]


def option(name):
    return args[args.index(name) + 1] if name in args else None


workspace = Path.cwd()
sessions = workspace / '.claude-fixture'
sessions.mkdir(exist_ok=True)
fresh, resume = option('--session-id'), option('--resume')
session = fresh or resume
with (workspace / 'claude-invocations.jsonl').open('a', encoding='utf-8') as log:
    log.write(json.dumps({'argv': args, 'env': {k: os.environ[k] for k in os.environ
                          if k.upper().startswith(('CLAUDE', 'ANTHROPIC_', 'AI_AGENT', 'FIXTURE_'))}}) + '\n')
if resume and not (sessions / resume).exists():
    print(f'No conversation found with session ID: {resume}', file=sys.stderr, flush=True)
    sys.exit(1)
if fresh and (sessions / fresh).exists():
    print(f'Error: Session ID {fresh} is already in use.', file=sys.stderr, flush=True)
    sys.exit(1)
model = option('--model') or 'claude-fixture-default'
if (workspace / 'wrong-model').exists():
    model = 'claude-other-model'
effort = 'low' if (workspace / 'wrong-effort').exists() else option('--effort')
stream_effort = None if (workspace / 'no-stream-effort').exists() else effort
transcript = None
if os.environ.get('CLAUDE_CONFIG_DIR'):
    # The real CLI names this folder from the cwd; the adapter finds it by session ID.
    transcript = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects' / 'fixture-cwd' / f'{session}.jsonl'
    transcript.parent.mkdir(parents=True, exist_ok=True)

inbox = queue.Queue()


def read():
    for line in sys.stdin:
        inbox.put(json.loads(line))
    inbox.put(None)


threading.Thread(target=read, daemon=True).start()


def send(message):
    if transcript and message.get('type') == 'assistant':
        entry = dict(message, effort='medium' if (workspace / 'transcript-wrong-effort').exists() else effort,
                     isSidechain=message.get('parent_tool_use_id') is not None)
        with transcript.open('a', encoding='utf-8') as record:
            record.write(json.dumps(entry) + '\n')
    print(json.dumps(message), flush=True)


def marker(name):
    return (workspace / name).exists()


def finish(result, **extra):
    send({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': result,
          'session_id': session, 'num_turns': 1, **extra})


def wait_interrupt(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            msg = inbox.get(timeout=.05)
        except queue.Empty:
            continue
        if msg and msg.get('type') == 'control_request' and msg['request']['subtype'] == 'interrupt':
            if marker('ignore-interrupt'):
                continue
            send({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': msg['request_id']}})
            return True
    return False


prompt = inbox.get()
assert prompt['type'] == 'user', prompt
text = prompt['message']['content'][0]['text']
(sessions / session).write_text(json.dumps({'turns': 1}))
(workspace / 'last-prompt.txt').write_text(text, encoding='utf-8')
send({'type': 'system', 'subtype': 'init', 'session_id': session, 'model': model, 'uuid': uuid.uuid4().hex,
      'permissionMode': 'bypassPermissions', 'apiKeySource': 'none', 'claude_code_version': 'fixture',
      'cwd': str(workspace)})
# Shape recorded from Claude Code 2.1.327 (values changed).
send({'type': 'rate_limit_event', 'rate_limit_info': {
      'status': 'allowed', 'resetsAt': int(time.time()) + 100, 'rateLimitType': 'five_hour',
      'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled', 'isUsingOverage': False,
      'unifiedWindows': {'five_hour': {'utilization': .1, 'resetsAt': int(time.time()) + 100, 'status': 'allowed'},
                         'seven_day': {'utilization': .25, 'resetsAt': int(time.time()) + 1000, 'status': 'allowed'}}}})
if marker('quota-once'):
    reset = float((workspace / 'quota-once').read_text() or time.time() + 100)
    (workspace / 'quota-once').unlink()
    send({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', 'rateLimitType': 'seven_day',
          'resetsAt': reset, 'isUsingOverage': False, 'unifiedWindows': {
              'five_hour': {'utilization': .5, 'resetsAt': int(time.time()) + 100, 'status': 'allowed'},
              'seven_day': {'utilization': 1.0, 'resetsAt': reset, 'status': 'rejected'}}}})
    send({'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': session,
          'result': f'Claude AI usage limit reached|{int(reset)}', 'api_error_status': 429})
elif marker('limit-text'):
    send({'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': session,
          'result': f'Claude AI usage limit reached|{int(time.time()) + 50}'})
elif marker('overage'):
    send({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'rateLimitType': 'five_hour',
          'resetsAt': int(time.time()) + 200, 'isUsingOverage': True}})
    wait_interrupt(10)
    send({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'session_id': session})
elif marker('too-long'):
    send({'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': session,
          'result': 'Prompt is too long'})
elif marker('overloaded'):
    send({'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': session,
          'result': 'API Error: 529 Overloaded', 'api_error_status': 529})
elif marker('crash'):
    sys.exit(3)
elif marker('permission-request'):
    send({'type': 'control_request', 'request_id': 'cli-1', 'request': {'subtype': 'can_use_tool', 'tool_name': 'Bash'}})
    reply = inbox.get(timeout=5)
    (workspace / 'control-reply.json').write_text(json.dumps(reply))
    finish('{"status":"blocked","diagnostic":"permission denied"}')
elif 'Do not implement it yet' in text:
    send({'type': 'assistant', 'parent_tool_use_id': None, 'effort': stream_effort, 'message': {'model': model, 'content': [
        {'type': 'text', 'text': json.dumps({'tasks': [{'id': 'task', 'workspace': str(workspace),
         'objective': 'write output.txt once', 'acceptance': ['output.txt contains done exactly once']}]})}]}})
    finish(json.dumps({'tasks': [{'id': 'task', 'workspace': str(workspace), 'objective': 'write output.txt once',
                                  'acceptance': ['output.txt contains done exactly once']}]}))
else:
    send({'type': 'assistant', 'parent_tool_use_id': None, 'effort': stream_effort, 'message': {'model': model, 'content': [
        {'type': 'thinking', 'thinking': 'plan'}, {'type': 'text', 'text': 'Writing output.'},
        {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Bash', 'input': {'command': 'write output.txt'}}]}})
    out = workspace / 'output.txt'
    if not out.exists():
        out.write_text('done')
    send({'type': 'user', 'parent_tool_use_id': None, 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'ok', 'is_error': False}]}})
    send({'type': 'assistant', 'parent_tool_use_id': 'toolu_9', 'message': {'model': 'claude-subagent-model',
          'content': [{'type': 'text', 'text': 'subagent work'}]}})
    if marker('hold'):
        interrupted = wait_interrupt(60)
        send({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'session_id': session,
              'interrupted': interrupted})
    else:
        output = {'status': 'done', 'diagnostic': 'fixture', 'validators': [{'command': ['{python}', '-c',
                  "from pathlib import Path; assert Path('output.txt').read_text() == 'done'"], 'criteria': [0]}]}
        finish('```json\n' + json.dumps(output) + '\n```')
# Real CLI exits after stdin closes.
while inbox.get() is not None:
    pass
