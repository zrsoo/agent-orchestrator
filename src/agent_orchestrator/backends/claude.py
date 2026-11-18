"""Claude Code headless CLI over stream-json stdio. Uses the CLI's own login.

One CLI process per turn; the saved Claude Code session carries context between
turns. Quota is observed only from rate-limit events and errors within turns.
"""
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from .base import Bucket, Quota, Outcome, InputSizeError, TransportError
from ..processes import ProcessTree
from ..prompts import check_prompt

EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
CONFIG_KEYS = {'model', 'effort', 'execution_mode', 'executable', 'turn_timeout', 'env'}
WINDOWS = {'five_hour': 300, 'seven_day': 10080, 'seven_day_opus': 10080, 'seven_day_sonnet': 10080}
REJECTED_RETRY_SECONDS = 300  # Fallback block when a rejection reports no reset time.
EXIT_SECONDS = 30
KEEP_ENV = {'CLAUDE_CONFIG_DIR'}
LIMIT_TEXT = re.compile(r'usage limit|hit your (usage )?limit|rate limit|limit reached', re.I)
LIMIT_RESET = re.compile(r'limit reached\|(\d{9,})')
TRANSIENT_STATUS = {408, 409, 500, 502, 503, 504, 529}


class _SessionMismatch(Exception):
    """--resume found no conversation, or --session-id already exists."""


def check_config(config):
    """Validate pinned objective configuration before submission or launch."""
    if not isinstance(config, dict) or set(config) - CONFIG_KEYS:
        raise ValueError(f'claude config keys: {", ".join(sorted(CONFIG_KEYS))}')
    if config.get('execution_mode', 'yolo') != 'yolo':
        raise ValueError('claude supports execution_mode: yolo')
    if not isinstance(config.get('model', 'default'), str) or not config.get('model', 'default'):
        raise ValueError('claude model must be a nonempty string')
    if config.get('effort') is not None and config['effort'] not in EFFORTS:
        raise ValueError(f'claude effort must be one of {", ".join(EFFORTS)}')
    env = config.get('env', {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        raise ValueError('claude env must map strings to strings')
    return config


def pinned_model(config):
    model = config.get('model', 'default')
    return model if model.startswith('claude-') else None


def child_env(config):
    """Never inherit a host Claude session, proxy or API key; the CLI login authenticates."""
    env = {k: v for k, v in os.environ.items() if k.upper() in KEEP_ENV or not (
        k.upper().startswith(('CLAUDE', 'ANTHROPIC_')) or k.upper() == 'AI_AGENT')}
    if pinned_model(config):
        env['CLAUDE_CODE_SUBAGENT_MODEL'] = config['model']
    env.update(config.get('env', {}))
    return env


def base_model(name):
    return (name or '').split('[', 1)[0]


class ClaudeBackend:
    def __init__(self, config=None):
        self.config = check_config(dict(config or {}))
        executable = self.config.get('executable') or shutil.which('claude.cmd' if os.name == 'nt' else 'claude')
        if not executable:
            raise TransportError('claude executable not found')
        self.command = executable if isinstance(executable, list) else [executable]
        self.limits = {}
        self.fresh = set()
        self.workspaces = {}
        self.proc = None
        self.tree = None
        self.requests = 0
        self.emit = lambda *_: None
        self.latest_quota = Quota()

    # Quota -----------------------------------------------------------------
    def observe_limit(self, info, force_rejected=False):
        """Record per-window state; returns True when a plan window is rejected."""
        if not isinstance(info, dict):
            return False
        now = time.time()
        fields = ('status', 'resetsAt', 'utilization')
        windows = info.get('unifiedWindows')
        for name, window in (windows.items() if isinstance(windows, dict) else ()):
            if isinstance(window, dict):
                self.limits[name] = {**{k: window[k] for k in fields if k in window}, 'observed': now}
        kind = info.get('rateLimitType') or 'plan'
        if kind != 'overage' or force_rejected:  # Overage availability alone is not a plan window.
            key = 'plan' if kind == 'overage' else kind
            state = {**self.limits.get(key, {}), **{k: info[k] for k in fields if k in info}, 'observed': now}
            if force_rejected:
                state['status'] = 'rejected'
            self.limits[key] = state
        return any(v.get('status') == 'rejected' and v['observed'] == now for v in self.limits.values())

    def quota(self):
        """Last observed limits. Without a rejection in force, quota is unknown, not granted."""
        now = time.time()
        buckets = []
        for kind, info in sorted(self.limits.items()):
            resets = info.get('resetsAt')
            remaining = None
            if info.get('status') == 'rejected':
                resets = resets or info['observed'] + REJECTED_RETRY_SECONDS
                remaining = 0 if resets > now else None
            elif isinstance(info.get('utilization'), (int, float)):
                used = info['utilization'] * 100  # Claude reports a 0..1 fraction.
                remaining = 100 - used if used < 100 else None  # Only a rejection blocks.
            buckets.append(Bucket(f'claude/{kind}', remaining, WINDOWS.get(kind), resets, 0))
        self.latest_quota = Quota(buckets, True)
        return self.latest_quota

    # Sessions --------------------------------------------------------------
    def session(self, workspace, session_id=None):
        if not session_id:
            session_id = str(uuid.uuid4())
            self.fresh.add(session_id)
        self.workspaces[session_id] = workspace
        return session_id

    def run(self, session_id, prompt, emit, control):
        check_prompt(prompt)
        self.emit = emit
        for attempt in range(2):
            try:
                return self._turn(session_id, prompt, emit, control)
            except _SessionMismatch as exc:
                if attempt:
                    raise TransportError(f'claude session {session_id}: {exc}') from exc
                # A crash can separate session creation from the scheduler's record of it.
                if session_id in self.fresh:
                    self.fresh.discard(session_id)
                else:
                    self.fresh.add(session_id)

    # Process ---------------------------------------------------------------
    def _argv(self, session_id):
        args = [*self.command, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
                '--verbose', '--dangerously-skip-permissions',
                '--session-id' if session_id in self.fresh else '--resume', session_id]
        if self.config.get('model', 'default') != 'default':
            args += ['--model', self.config['model']]
        if self.config.get('effort'):
            args += ['--effort', self.config['effort']]
        return args

    def _send(self, message):
        try:
            self.proc.stdin.write(json.dumps(message) + '\n')
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as exc:
            raise TransportError(str(exc)) from exc

    def _start(self, session_id):
        cwd = self.workspaces.get(session_id)
        if not cwd:
            raise TransportError(f'unknown claude session workspace: {session_id}')
        try:
            self.proc = subprocess.Popen(self._argv(session_id), cwd=cwd, env=child_env(self.config),
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, encoding='utf-8', errors='replace', bufsize=1,
                                         start_new_session=os.name != 'nt')
        except OSError as exc:
            raise TransportError(f'claude launch failed: {exc}') from exc
        try:
            self.tree = ProcessTree(self.proc)
        except OSError:
            self.proc.kill()
            self.proc.wait()
            raise
        inbox, stderr = queue.Queue(), []

        def read():
            try:
                for line in self.proc.stdout:
                    try:
                        inbox.put(json.loads(line))
                    except ValueError:
                        stderr.append(line.rstrip())
            finally:
                inbox.put(None)

        def errors():
            for line in self.proc.stderr:
                stderr.append(line.rstrip())
                stderr[:] = stderr[-30:]

        threading.Thread(target=read, daemon=True).start()
        self.stderr_reader = threading.Thread(target=errors, daemon=True)
        self.stderr_reader.start()
        return inbox, stderr

    def _stop_process(self, graceful):
        proc, tree = self.proc, self.tree
        self.proc = self.tree = None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        if graceful:
            try:
                proc.wait(timeout=EXIT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        tree.close()  # Closes tool/MCP children with the CLI.
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdout, proc.stderr):
            stream.close()

    def interrupt(self, session_id=None, turn_id=None):
        if self.proc and self.proc.poll() is None:
            self.requests += 1
            self._send({'type': 'control_request', 'request_id': f'orchestrator-{self.requests}',
                        'request': {'subtype': 'interrupt'}})

    def close(self):
        self._stop_process(False)

    # Turn ------------------------------------------------------------------
    def _verify_model(self, model, where):
        expected = pinned_model(self.config)
        if expected and model and model != '<synthetic>' and base_model(model) != expected:
            raise ValueError(f'Claude Code used model {model} in {where}; objective pins {expected}. '
                             'Install a Claude Code version/account that serves the pinned model.')

    def _verify_effort(self, effort, observed):
        if effort is None:
            return
        observed.add(effort)
        if self.config.get('effort') and effort != self.config['effort']:
            raise ValueError(f'Claude Code used effort {effort}; objective pins {self.config["effort"]}.')

    def _transcript(self, session_id):
        """The CLI's own session record; the stream does not report effort."""
        root = (self.config.get('env', {}).get('CLAUDE_CONFIG_DIR') or os.environ.get('CLAUDE_CONFIG_DIR')
                or Path.home() / '.claude')
        matches = sorted((Path(root) / 'projects').glob(f'*/{session_id}.jsonl'))
        return matches[0] if matches else None

    def _check_transcript(self, session_id, before, efforts):
        path = self._transcript(session_id)
        if not path:
            return
        with path.open('rb') as handle:
            handle.seek(before[1] if before[0] == path else 0)
            data = handle.read()
        for line in data.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get('type') == 'assistant' and not entry.get('isSidechain'):
                self._verify_model((entry.get('message') or {}).get('model'), 'session transcript')
                self._verify_effort(entry.get('effort'), efforts)

    def _turn(self, session_id, prompt, emit, control):
        path = self._transcript(session_id)
        before = (path, path.stat().st_size if path else 0)
        inbox, stderr = self._start(session_id)
        tools, seen, interrupted, overage, rejected, efforts = {}, False, None, False, None, set()
        deadline = time.monotonic() + self.config.get('turn_timeout', 7200)
        try:
            self._send({'type': 'user', 'session_id': session_id, 'parent_tool_use_id': None,
                        'message': {'role': 'user', 'content': [{'type': 'text', 'text': prompt}]}})
            while True:
                now = time.monotonic()
                if now >= deadline:
                    raise TransportError('turn timeout; reconciliation required')
                if interrupted is None and control() == 'stop':
                    interrupted = now + EXIT_SECONDS
                    self.interrupt()
                if interrupted is not None and now > interrupted:
                    self._stop_process(False)
                    return Outcome('quota' if overage else 'interrupted',
                                   diagnostic='interrupt not acknowledged; process closed')
                try:
                    msg = inbox.get(timeout=.25)
                except queue.Empty:
                    continue
                if msg is None:
                    self.stderr_reader.join(5)  # Exit reason may trail stdout EOF.
                    tail = '\n'.join(stderr[-5:])
                    if not seen and re.search(r'no conversation found|already in use', tail, re.I):
                        raise _SessionMismatch(tail)
                    raise TransportError('Claude Code exited without a result: ' + tail)
                kind = msg.get('type')
                if kind == 'system' and msg.get('subtype') == 'init':
                    seen = True
                    self.fresh.discard(session_id)
                    if msg.get('session_id', session_id) != session_id:
                        raise TransportError(f'Claude Code changed session {session_id} to {msg["session_id"]}')
                    emit('claude/init', {k: msg.get(k) for k in (
                        'session_id', 'model', 'permissionMode', 'apiKeySource', 'claude_code_version',
                        'cwd', 'effort', 'uuid') if k in msg})
                    emit('turn_identity', {'session': session_id, 'turn': msg.get('uuid') or uuid.uuid4().hex})
                    self._verify_model(msg.get('model'), 'session init')
                elif kind == 'rate_limit_event':
                    info = msg.get('rate_limit_info') or {}
                    emit('claude/rate_limit', info)
                    if info.get('isUsingOverage') and interrupted is None:
                        overage = True  # Never purchase extra usage; stop at the plan limit.
                        interrupted = time.monotonic() + EXIT_SECONDS
                        self.interrupt()
                    if self.observe_limit(info, force_rejected=bool(info.get('isUsingOverage'))):
                        rejected = info
                elif kind == 'assistant':
                    message = msg.get('message') or {}
                    if msg.get('parent_tool_use_id') is None:
                        self._verify_model(message.get('model'), 'assistant response')
                        self._verify_effort(msg.get('effort'), efforts)
                    for block in message.get('content') or []:
                        if block.get('type') == 'tool_use':
                            tools[block.get('id')] = block
                        elif block.get('type') == 'text' and block.get('text'):
                            emit('item/completed', {'turnId': session_id, 'item': {
                                'type': 'agentMessage', 'text': block['text'],
                                'subagent': msg.get('parent_tool_use_id')}})
                elif kind == 'user':
                    content = (msg.get('message') or {}).get('content')
                    for block in content if isinstance(content, list) else []:
                        if block.get('type') != 'tool_result':
                            continue
                        call = tools.pop(block.get('tool_use_id'), {})
                        arguments = call.get('input') or {}
                        item = {'id': block.get('tool_use_id'), 'type': 'toolCall', 'tool': call.get('name'),
                                'arguments': arguments, 'result': block.get('content'),
                                'status': 'failed' if block.get('is_error') else 'completed'}
                        for field in ('command', 'file_path', 'cwd'):
                            if isinstance(arguments.get(field), str):
                                item['command' if field == 'command' else field] = arguments[field]
                        if isinstance(arguments.get('file_path'), str) and call.get('name') in ('Write', 'Edit', 'MultiEdit', 'NotebookEdit'):
                            item['changes'] = [{'path': arguments['file_path']}]
                        emit('item/completed', {'turnId': session_id, 'item': item})
                elif kind == 'control_request':
                    # No interactive owner: never hang on a permission or hook callback.
                    self._send({'type': 'control_response', 'response': {
                        'subtype': 'error', 'request_id': msg.get('request_id'),
                        'error': 'No interactive owner in orchestrator'}})
                elif kind == 'control_response':
                    emit('claude/control_response', msg.get('response') or {})
                elif kind == 'result':
                    break
            self._stop_process(True)  # The transcript is complete once the CLI exits.
            self._check_transcript(session_id, before, efforts)
            emit('claude/result', {**{k: v for k, v in msg.items() if k != 'result'},
                                   'observed_effort': sorted(efforts)})
            return self._outcome(msg, interrupted is not None, overage, rejected)
        except BaseException:
            self._stop_process(False)
            raise
        finally:
            if self.proc is not None:
                self._stop_process(True)

    def _outcome(self, msg, interrupted, overage, rejected):
        text = msg.get('result') if isinstance(msg.get('result'), str) else ''
        errors = msg.get('errors') or []
        detail = ' '.join([text, *map(str, errors)])
        diagnostic = json.dumps({k: msg.get(k) for k in (
            'subtype', 'is_error', 'api_error_status', 'errors', 'stop_reason', 'terminal_reason') if k in msg})
        if overage:
            return Outcome('quota', text, 'Paid overage refused; waiting for the plan limit reset. ' + diagnostic)
        if msg.get('is_error') or msg.get('subtype') != 'success':
            reset = LIMIT_RESET.search(detail)
            if rejected or msg.get('api_error_status') == 429 and LIMIT_TEXT.search(detail) or LIMIT_TEXT.search(text):
                if not rejected:
                    self.observe_limit({'status': 'rejected', 'rateLimitType': 'plan',
                                        'resetsAt': int(reset.group(1)) if reset else None})
                return Outcome('quota', text, detail.strip() or diagnostic)
            if interrupted:
                return Outcome('interrupted', text, diagnostic)
            if re.search(r'prompt is too long|input.{0,20}too (long|large)', detail, re.I):
                raise InputSizeError(
                    'Claude rejected the input as too long; this input is not transport-retryable. '
                    'Reduce required prompt content or recovery summary, then recheck the existing '
                    'blocked task (or objective), preserving its session and evidence.')
            if msg.get('api_error_status') in TRANSIENT_STATUS or re.search(r'overloaded|api error: 5\d\d', detail, re.I):
                raise TransportError('Claude API unavailable: ' + (detail.strip() or diagnostic))
            return Outcome('failed', text, detail.strip() or diagnostic)
        if interrupted:
            return Outcome('interrupted', text, diagnostic)
        return Outcome('completed', text)
