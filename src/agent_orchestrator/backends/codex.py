"""Codex App Server JSON-RPC over stdio. No SDK or API key required."""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from .base import Bucket, Quota, Outcome, InputSizeError, TransportError, QuotaExhausted
from ..processes import ProcessTree
from ..prompts import check_prompt


def check_input_error(error):
    normalized = ''.join(c for c in json.dumps(error).lower() if c.isalnum())
    if 'inputtoolarge' in normalized:
        raise InputSizeError(
            'Codex App Server rejected input_too_large; this input is not transport-retryable. '
            'Reduce required prompt content or recovery summary, then recheck the existing '
            'blocked task (or objective), preserving its session and evidence.')


def normalize_quota(raw):
    limits = raw.get('rateLimitsByLimitId') or {'codex': raw.get('rateLimits', {})}
    buckets = []
    allowed = raw.get('ordinaryUsageAllowed', True)
    for limit_id, limit in limits.items():
        if not limit:
            continue
        allowed = allowed and not limit.get('spendControlReached', False)
        for name in ('primary', 'secondary', 'individualLimit'):
            b = limit.get(name)
            if not isinstance(b, dict):
                continue
            minutes = b.get('windowDurationMins')
            floor = 2 if minutes == 300 else 1 if minutes == 10080 else 0
            used = b.get('usedPercent')
            buckets.append(Bucket(f'{limit_id}/{name}', None if used is None else 100-used,
                                  minutes, b.get('resetsAt'), floor))
    return Quota(buckets, bool(allowed))


class CodexBackend:
    def __init__(self, config=None):
        self.config = config or {}
        if self.config.get('execution_mode', 'yolo') != 'yolo':
            raise ValueError('Codex M-1 supports execution_mode: yolo')
        executable = self.config.get('executable') or shutil.which('codex.cmd' if os.name == 'nt' else 'codex')
        if not executable:
            raise TransportError('codex executable not found')
        command = executable if isinstance(executable, list) else [executable]
        self.proc = subprocess.Popen([*command, 'app-server', '--listen', 'stdio://'],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding='utf-8', errors='replace', bufsize=1,
                                     start_new_session=os.name != 'nt')
        try:
            self.tree = ProcessTree(self.proc)
        except OSError:
            self.proc.kill()
            self.proc.wait()
            raise
        self.inbox = queue.Queue()
        self.pending = []
        self.sequence = 0
        self.emit = lambda *_: None
        self.latest_quota = None
        self.stderr = []
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._errors, daemon=True).start()
        try:
            self._rpc('initialize', {'clientInfo': {'name': 'agent_orchestrator', 'version': '0.1.0'},
                                     'capabilities': {'experimentalApi': True}})
            self._send({'method': 'initialized'})
        except BaseException:
            self.close()
            raise

    def _reader(self):
        try:
            for line in self.proc.stdout:
                try:
                    self.inbox.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.inbox.put(None)

    def _errors(self):
        for line in self.proc.stderr:
            self.stderr.append(line.rstrip())
            self.stderr[:] = self.stderr[-30:]

    def _send(self, message):
        try:
            self.proc.stdin.write(json.dumps(message) + '\n')
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise TransportError(str(exc)) from exc

    def _receive(self, timeout):
        try:
            msg = self.inbox.get(timeout=timeout)
        except queue.Empty:
            return {}
        if msg is None:
            raise TransportError('App Server exited: ' + '\n'.join(self.stderr[-5:]))
        if 'method' in msg:
            self.emit(msg['method'], msg.get('params', {}))
            if msg['method'] == 'account/rateLimits/updated':
                self.latest_quota = normalize_quota(msg.get('params', {}))
            if 'id' in msg:
                # YOLO should not request approval. Do not silently hang on elicitation.
                self._send({'id': msg['id'], 'error': {'code': -32601, 'message': 'No interactive owner in orchestrator'}})
        return msg

    def _rpc(self, method, params):
        self.sequence += 1
        seq = self.sequence
        self._send({'id': seq, 'method': method, 'params': params})
        deadline = time.monotonic() + self.config.get('rpc_timeout', 60)
        while time.monotonic() < deadline:
            msg = self._receive(.25)
            if msg.get('id') == seq and 'method' not in msg:
                if 'error' in msg:
                    check_input_error(msg['error'])
                    if any(s in json.dumps(msg['error']).lower() for s in ('usagelimitexceeded', 'usage limit', 'quota')):
                        raise QuotaExhausted(str(msg['error']))
                    raise TransportError(f'{method}: {msg["error"]}')
                return msg.get('result', {})
            if msg.get('method') in ('turn/completed', 'item/completed'):
                self.pending.append(msg)
        raise TransportError(f'{method}: timed out')

    def quota(self):
        self.latest_quota = normalize_quota(self._rpc('account/rateLimits/read', {}))
        return self.latest_quota

    def wait_updates(self, timeout):
        """Notifications wake the supervisor early; admission still requires a read."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self._receive(min(.25, max(.001, deadline-time.monotonic())))
            if msg.get('method') == 'account/rateLimits/updated':
                return

    def session(self, workspace, session_id=None):
        params = {'cwd': workspace, 'approvalPolicy': 'never', 'sandbox': 'danger-full-access'}
        model = self.config.get('model', 'default')
        if model != 'default':
            params['model'] = model
        if session_id:
            params['threadId'] = session_id
            result = self._rpc('thread/resume', params)
        else:
            params['experimentalRawEvents'] = False
            result = self._rpc('thread/start', params)
        return result['thread']['id']

    def run(self, session_id, prompt, emit, control):
        check_prompt(prompt)
        self.emit = emit
        self.pending.clear()
        result = self._rpc('turn/start', {'threadId': session_id,
                            'input': [{'type': 'text', 'text': prompt}],
                            'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'dangerFullAccess'}})
        turn_id = result['turn']['id']
        emit('turn_identity', {'session': session_id, 'turn': turn_id})
        text, interrupted = [], False
        deadline = time.monotonic() + self.config.get('turn_timeout', 7200)
        while time.monotonic() < deadline:
            if control() == 'stop' and not interrupted:
                interrupted = True
                self.interrupt(session_id, turn_id)
            msg = self.pending.pop(0) if self.pending else self._receive(.25)
            params = msg.get('params', {})
            if params.get('threadId', session_id) != session_id:
                continue
            if msg.get('method') == 'item/completed':
                item = params.get('item', {})
                if item.get('type') == 'agentMessage' and item.get('phase') != 'commentary':
                    text.append(item.get('text', ''))
            if msg.get('method') == 'turn/completed' and params['turn']['id'] == turn_id:
                turn = params['turn']
                error = turn.get('error') or {}
                check_input_error(error)
                diagnostic = json.dumps(error) if error else ''
                code = error.get('codexErrorInfo')
                exhausted = 'usageLimitExceeded' in str(code) or 'usage limit' in diagnostic.lower() or 'quota' in diagnostic.lower()
                status = 'quota' if exhausted else turn['status']
                return Outcome(status, '\n'.join(text), diagnostic)
        self.interrupt(session_id, turn_id)
        raise TransportError('turn timeout; reconciliation required')

    def interrupt(self, session_id, turn_id):
        self._rpc('turn/interrupt', {'threadId': session_id, 'turnId': turn_id})

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.tree.close()
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()
