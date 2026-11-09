"""Read-only foreground display; no scheduler or provider calls on the refresh thread."""
from dataclasses import asdict
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unicodedata
from .terminal import Terminal
from .viewport import Viewport

from .backends.codex import normalize_quota


REFRESH_SECONDS = .25
STYLES = {
    'PENDING': ('[.]', 37), 'RUNNING': ('[>]', 36),
    'VALIDATING': ('[?]', 35), 'WAITING_QUOTA': ('[~]', 33),
    'RECONCILING': ('[<>]', 36), 'RETRYING': ('[R]', 33),
    'FIXING_PERSISTENT_FAILURE': ('[!]', 33), 'PAUSED': ('[||]', 33),
    'BLOCKED': ('[!]', 31), 'COMPLETED': ('[+]', 32), 'STOPPED': ('[x]', 31),
}


def read_snapshot(db_path):
    """Copy a coherent committed view, without migrations or large evidence payloads."""
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro',
                                uri=True, timeout=.1)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('BEGIN')
            snapshot = {table: [dict(row) for row in conn.execute(
                f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('objective', 'task', 'dependency')}
            owners = 'SELECT id AS owner FROM objective UNION ALL SELECT key FROM task'
            snapshot['turn'] = [dict(row) for row in conn.execute(f'''
                WITH owners AS ({owners})
                SELECT t.id,t.owner,t.kind,t.status,t.started,t.ended
                FROM owners o JOIN turn t ON t.id=(
                    SELECT x.id FROM turn x WHERE x.owner=o.owner ORDER BY x.id DESC LIMIT 1)
                ORDER BY t.id''')]
            snapshot['event'] = [dict(row) for row in conn.execute('''
                WITH owners(owner) AS (SELECT id FROM objective UNION ALL SELECT key FROM task),
                kinds(kind) AS (VALUES ('quota'),('account/rateLimits/updated'),
                                      ('infrastructure_failure'),('transition'))
                SELECT e.id,e.owner,e.at,e.kind,e.data
                FROM owners o CROSS JOIN kinds k JOIN event e ON e.id=(
                    SELECT x.id FROM event x WHERE x.owner=o.owner AND x.kind=k.kind
                    ORDER BY x.id DESC LIMIT 1)
                ORDER BY e.id''')]
            return snapshot
        finally:
            conn.rollback()


def clean(value, limit=None):
    punctuation = str.maketrans({'\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
                                 '\u2013': '-', '\u2014': '-', '\u2026': '...', '\u2192': '->'})
    source = unicodedata.normalize('NFKD', str(value).translate(punctuation))
    source = ''.join(c for c in source if not unicodedata.combining(c))
    source = source.encode('ascii', errors='replace').decode('ascii')
    text = ' '.join(''.join(c if 32 <= ord(c) < 127 else ' ' for c in source).split())
    return text if limit is None or len(text) <= limit else text[:limit-3] + '...'


def state_label(state, color):
    symbol, code = STYLES.get(state, ('[?]', 37))
    label = f'{symbol} {state}'
    return f'\x1b[{code}m{label}\x1b[0m' if color else label


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}'


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def next_step(row):
    key, state = row.get('key', row.get('id')), row['state']
    if row.get('control') == 'pause' and state != 'PAUSED':
        return 'Pause requested; active work will finish before pausing.'
    return {
        'BLOCKED': f'Resolve the diagnostic, then run: orchestrator recheck {key}',
        'PAUSED': f'To continue, run: orchestrator resume {key}',
        'WAITING_QUOTA': 'Waiting for an authoritative quota reread; recovery is automatic.',
        'VALIDATING': 'Independent acceptance checks are running; wait for results.',
        'RECONCILING': 'Automatic recovery in the saved session; inspect uncertain side effects.',
        'RETRYING': 'Automatic infrastructure retry; inspect the diagnostic if failures persist.',
        'FIXING_PERSISTENT_FAILURE': 'Worker is diagnosing repeated failure and trying an alternative approach.',
    }.get(state)


def render_sections(snapshot, now, color=False, *, timers=True):
    """Structured rows share the same factual presentation in live and plain modes."""
    sections = []
    lines = []
    tasks = snapshot['task']
    turns = {t['owner']: t for t in snapshot['turn']}
    retries = {e['owner']: json.loads(e['data']) for e in snapshot['event']
               if e['kind'] == 'infrastructure_failure'}
    transitions = {e['owner']: e for e in snapshot['event'] if e['kind'] == 'transition'}
    dependencies = {}
    for dep in snapshot['dependency']:
        dependencies.setdefault(dep['task_key'], []).append(dep['depends_key'])

    def details(row, action=None):
        key = row.get('key', row.get('id'))
        if row['diagnostic']:
            lines.append(f'    Diagnostic: {clean(row["diagnostic"])}')
        if row['infra_retries'] or row.get('attempts'):
            lines.append(f'    Retries: infrastructure={row["infra_retries"]} disruptions={row.get("attempts", 0)}')
        retry = retries.get(key)
        if retry and row['state'] in ('RECONCILING', 'RETRYING'):
            lines.append(f'    Retry diagnostic: {clean(retry.get("error", ""))}')
            if retry.get('retry_at') is not None:
                lines.append(f'    Retry at: {timestamp(retry["retry_at"])} (automatic)')
        if row.get('recheck'):
            lines.append(f'    Recheck: {row["recheck"]} (queued/in progress)')
        action = action or next_step(row)
        if action:
            lines.append(f'    Next: {action}')

    for obj in snapshot['objective']:
        lines = []
        owned = [t for t in tasks if t['objective_id'] == obj['id']]
        owners = {obj['id'], *(t['key'] for t in owned)}
        completed = sum(t['state'] == 'COMPLETED' for t in owned)
        ended = transitions.get(obj['id'], {}).get('at', now) if obj['state'] in ('COMPLETED', 'STOPPED') else now
        elapsed = f' | elapsed={duration(ended-obj["created"])}' if timers else ''
        lines.append(f'{obj["id"]} {state_label(obj["state"], color)} | completed={completed}/{len(owned)}{elapsed}')
        lines.append(f'  Objective: {clean(obj["text"], 100)}')
        active = [t for owner, t in turns.items() if owner in owners and t['status'] == 'running']
        if active:
            for turn in active:
                elapsed = f' | elapsed={duration(now-turn["started"])}' if timers else ''
                lines.append(f'  Active: {turn["owner"]} ({clean(turn["kind"])}){elapsed}')
        working = [t for t in owned if t['state'] in
                   ('RUNNING', 'VALIDATING', 'RECONCILING', 'RETRYING', 'FIXING_PERSISTENT_FAILURE')
                   and t['key'] not in {turn['owner'] for turn in active}]
        for task in working:
            started = transitions.get(task['key'], {}).get('at')
            elapsed = f' | elapsed={duration(now-started)}' if timers and started is not None else ''
            lines.append(f'  Active: {task["key"]} ({task["state"]}){elapsed}')
        if not active and not working:
            lines.append('  Active: none')
        if not owned:
            lines.append('  [P] PLANNING | DAG not yet persisted')
        quota_events = [e for e in snapshot['event'] if e['owner'] in owners
                        and e['kind'] in ('quota', 'account/rateLimits/updated')]
        if quota_events:
            event = max(quota_events, key=lambda e: e['id'])
            quota = json.loads(event['data'])
            if event['kind'] == 'account/rateLimits/updated':
                quota = asdict(normalize_quota(quota))
            if quota.get('allowed') is False:
                lines.append('  Quota: provider reports admission denied')
            if not quota.get('buckets'):
                lines.append('  Quota: unknown (no reported buckets)')
            for bucket in quota.get('buckets', []):
                remaining = bucket.get('remaining')
                fields = [f'{remaining:g}% remaining' if remaining is not None else 'remaining unknown']
                if bucket.get('window_minutes') is not None:
                    fields.append(f'window={bucket["window_minutes"]}m')
                if bucket.get('resets_at') is not None:
                    fields.append(f'reset={timestamp(bucket["resets_at"])}')
                lines.append(f'  Quota {clean(bucket["name"])}: ' + ' | '.join(fields))
        else:
            lines.append('  Quota: unknown (not reported)')
        action = None
        if obj['state'] == 'BLOCKED':
            if not owned:
                action = 'Planning is blocked; resolve the diagnostic and submit a corrected objective.'
            elif any(t['state'] == 'STOPPED' for t in owned):
                action = 'A required task was stopped permanently; submit a revised objective to continue.'
        header, lines = lines, []
        details(obj, action)
        body = [(f'{obj["id"]}:detail:{i}', line) for i, line in enumerate(lines)]
        for task in owned:
            lines = []
            spec = json.loads(task['spec'])
            deps = ', '.join(dependencies.get(task['key'], [])) or 'none'
            lines.append(f'  {task["key"]} {state_label(task["state"], color)} | {clean(spec["objective"], 72)} | depends_on: {deps}')
            details(task)
            body.extend((f'{task["key"]}:{i}', line) for i, line in enumerate(lines))
        active_key = next((t['owner'] for t in active if t['owner'] != obj['id']), None)
        if active_key is None and working:
            active_key = working[0]['key']
        sections.append({'key': obj['id'], 'state': obj['state'], 'header': header,
                         'body': body, 'active': active_key or (obj['id'] if active else None)})
    return sections


def render(snapshot, now, color=False, *, timers=True):
    """Complete plain/report presentation; timers=False gives stable log summaries."""
    sections = render_sections(snapshot, now, color, timers=timers)
    lines = ['Orchestrator dashboard']
    for section in sections:
        lines.extend(section['header'])
        lines.extend(text for _, text in section['body'])
    if not sections:
        lines.append('No objectives; waiting for submissions.')
    return '\n'.join(lines)


def terminal_mode(stream):
    """Detect console capabilities without mutating the caller's terminal modes."""
    if not stream.isatty() or os.environ.get('TERM') == 'dumb':
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        try:
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            mode = wintypes.DWORD()
            kernel = ctypes.windll.kernel32
            if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
        except (OSError, ValueError):
            return False
    return True


class Dashboard:
    def __init__(self, db_path, stream=None, *, interval=REFRESH_SECONDS,
                 interactive=None, color=None, clock=time.time):
        self.db_path = db_path
        self.stream = sys.stdout if stream is None else stream
        self.interactive = (terminal_mode(self.stream) and sys.stdin is not None and sys.stdin.isatty()
                            if interactive is None else interactive)
        self.color = self.interactive and ('NO_COLOR' not in os.environ if color is None else color)
        self.interval = interval
        self.clock = clock
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.previous = None
        self.snapshot = None
        self.error = None
        self.ready = threading.Event()
        self.wake = threading.Event()
        self.pending = []
        self.thread = None
        self.failed = False
        self.terminal = Terminal(self.stream, self.interactive, self.stop)
        self.view = Viewport()

    def refresh(self, *, read=True, final=False):
        if read:
            try:
                self.snapshot = read_snapshot(self.db_path)
                self.error = None
            except sqlite3.Error as exc:
                self.error = clean(exc)
        if self.interactive and self.snapshot is not None:
            sections = render_sections(self.snapshot, self.clock())
            if self.error and sections:
                sections[0]['body'].insert(0, ('read-error', f'Display read error (retrying): {self.error}'))
            def paint(line):
                if self.color:
                    for state in STYLES:
                        line = line.replace(state_label(state, False), state_label(state, True))
                return line
            frame = self.view.render(sections, self.terminal.size(), paint)
        else:
            frame = render(self.snapshot, self.clock(), timers=False) if self.snapshot else 'Orchestrator dashboard'
            if self.error:
                frame += f'\nDisplay read error (retrying): {self.error}'
        with self.lock:
            pending, self.pending = self.pending, []
        if self.interactive or frame != self.previous:
            self.terminal.write(('\x1b[H\x1b[J' if self.interactive else '') + frame +
                                ('' if self.interactive else '\n'), final=final)
            self.previous = frame
        for text in pending:
            self.terminal.write(text+'\n', final=final)

    def notice(self, text, **kwargs):
        # Interactive quota transitions are already represented by committed rows.
        if not self.interactive:
            with self.lock:
                self.pending[:] = [clean(text)]  # State is persisted; coalesce duplicate notices.
            self.wake.set()
            if self.thread is None:
                self.refresh(read=False)  # Explicit standalone rendering used by callers/tests.

    def _run(self):
        try:
            if self.interactive:
                self.terminal.write('\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h')
            self.refresh()
            self.ready.set()
            next_read = time.monotonic()+self.interval
            size = self.terminal.size()
            while not self.stop.is_set():
                events = self.terminal.poll()
                for action, amount in events:
                    self.view.handle(action, amount)
                now = time.monotonic()
                new_size = self.terminal.size()
                if now >= next_read:
                    self.refresh()
                    next_read = time.monotonic()+self.interval
                elif events or new_size != size or self.wake.is_set():
                    self.refresh(read=False)
                size = new_size
                self.wake.wait(max(.001, min(.05, next_read-time.monotonic())))
                self.wake.clear()
            self.refresh(final=True)
        except Exception as exc:
            self.failed = True
            self.error = clean(exc)
            self.stop.set()  # Display failures never become lifecycle failures.
        finally:
            try:
                if self.interactive and not self.terminal.output_failed:
                    self.terminal.write('\x1b[?1000l\x1b[?1006l\x1b[0m\x1b[?25h\x1b[?1049l', final=True)
            except (OSError, ValueError):
                self.failed = True
            self.ready.set()

    def __enter__(self):
        try:
            self.terminal.open()
            self.thread = threading.Thread(target=self._run, name='orchestrator-dashboard', daemon=True)
            self.thread.start()
            if not self.ready.wait(1):
                self.__exit__()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.wake.set()
        try:
            if self.thread is not None and self.thread.ident is not None:
                self.thread.join(.2)
                deadline = time.monotonic()+1
                while self.thread.is_alive() and time.monotonic() < deadline:
                    self.terminal.cancel_output(self.thread)
                    self.thread.join(.05)
        finally:
            self.terminal.close()
