"""SQLite is the source of truth. Local IDs are scoped to objectives."""
import json
import sqlite3
import time
import uuid
from pathlib import Path
from .models import STATES, TERMINAL


class Database:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS objective (
          id TEXT PRIMARY KEY, text TEXT NOT NULL, workspace TEXT NOT NULL,
          provider TEXT NOT NULL, config TEXT NOT NULL, state TEXT NOT NULL,
          control TEXT, session TEXT, diagnostic TEXT NOT NULL DEFAULT '',
          infra_retries INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS task (
          key TEXT PRIMARY KEY, objective_id TEXT NOT NULL REFERENCES objective(id),
          id TEXT NOT NULL, spec TEXT NOT NULL, state TEXT NOT NULL, control TEXT,
          session TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          infra_retries INTEGER NOT NULL DEFAULT 0, diagnostic TEXT NOT NULL DEFAULT '',
          accepted_stage TEXT, UNIQUE(objective_id,id));
        CREATE TABLE IF NOT EXISTS dependency (
          task_key TEXT REFERENCES task(key), depends_key TEXT REFERENCES task(key),
          PRIMARY KEY(task_key,depends_key));
        CREATE TABLE IF NOT EXISTS turn (
          id INTEGER PRIMARY KEY, owner TEXT NOT NULL, session TEXT, provider_turn TEXT,
          kind TEXT NOT NULL, status TEXT NOT NULL, started REAL NOT NULL, ended REAL,
          result TEXT);
        CREATE TABLE IF NOT EXISTS event (
          id INTEGER PRIMARY KEY, owner TEXT NOT NULL, turn_id INTEGER,
          at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifact (
          id INTEGER PRIMARY KEY, owner TEXT NOT NULL, at REAL NOT NULL,
          kind TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS validator (
          id INTEGER PRIMARY KEY, task_key TEXT NOT NULL REFERENCES task(key),
          command TEXT NOT NULL, criteria TEXT NOT NULL, timeout REAL NOT NULL,
          active INTEGER NOT NULL DEFAULT 1);
        ''')
        # Additive migration: retain existing graphs, sessions and evidence.
        self.execute('BEGIN IMMEDIATE')
        try:
            columns = {r['name'] for r in self.execute('PRAGMA table_info(task)')}
            for name in ('recheck', 'blocker_check'):
                if name not in columns:
                    self.execute(f'ALTER TABLE task ADD COLUMN {name} TEXT')
            self.execute('CREATE INDEX IF NOT EXISTS event_owner_kind_id ON event(owner,kind,id)')
            self.execute('CREATE INDEX IF NOT EXISTS turn_owner_id ON turn(owner,id)')
            self.execute('COMMIT')
        except BaseException:
            self.execute('ROLLBACK')
            raise

    def execute(self, sql, args=()):
        return self.conn.execute(sql, args)

    def rows(self, sql, args=()):
        return [dict(r) for r in self.execute(sql, args).fetchall()]

    def get(self, table, key):
        if table not in ('task', 'objective'):
            raise ValueError(table)
        rows = self.rows(f'SELECT * FROM {table} WHERE {"key" if table == "task" else "id"}=?', (key,))
        if not rows:
            raise ValueError(f'unknown {table}: {key}')
        return rows[0]

    def event(self, owner, kind, data, turn_id=None):
        self.execute('INSERT INTO event(owner,turn_id,at,kind,data) VALUES(?,?,?,?,?)',
                     (owner, turn_id, time.time(), kind, json.dumps(data)))

    def transition(self, table, key, state, diagnostic=None):
        if state not in STATES:
            raise ValueError(state)
        self.execute('BEGIN IMMEDIATE')
        try:
            old = self.get(table, key)
            if old['state'] == 'STOPPED' and state != 'STOPPED':
                self.execute('COMMIT')
                return
            self.execute(f'UPDATE {table} SET state=?,diagnostic=? WHERE {"key" if table == "task" else "id"}=?',
                         (state, old['diagnostic'] if diagnostic is None else diagnostic, key))
            self.event(key, 'transition', {'from': old['state'], 'to': state, 'diagnostic': diagnostic})
            if table == 'task' and old['recheck'] and state in TERMINAL:
                self.execute('UPDATE task SET recheck=NULL WHERE key=?', (key,))
                self.event(key, 'recheck_finished', {'state': state, 'diagnostic': diagnostic})
            self.execute('COMMIT')
        except BaseException:
            self.execute('ROLLBACK')
            raise

    def block_objective_if_unrecoverable(self, key):
        """Serialize terminalization with recheck; never decide from cached tasks."""
        self.execute('BEGIN IMMEDIATE')
        try:
            old = self.get('objective', key)
            diagnostic = 'A required task is blocked or stopped'
            changed = self.execute('''
                UPDATE objective SET state='BLOCKED',diagnostic=?
                WHERE id=? AND state NOT IN ('COMPLETED','BLOCKED','STOPPED','PAUSED')
                  AND control IS NULL
                  AND EXISTS (SELECT 1 FROM task WHERE objective_id=?
                              AND state IN ('BLOCKED','STOPPED'))
                  AND NOT EXISTS (SELECT 1 FROM task WHERE objective_id=? AND recheck IS NOT NULL)
                ''', (diagnostic, key, key, key)).rowcount
            if changed:
                self.event(key, 'transition', {'from': old['state'], 'to': 'BLOCKED',
                                                'diagnostic': diagnostic})
            self.execute('COMMIT')
            return bool(changed)
        except BaseException:
            self.execute('ROLLBACK')
            raise

    def submit(self, text, workspace, provider, config):
        key = 'obj-' + uuid.uuid4().hex[:12]
        self.execute('INSERT INTO objective(id,text,workspace,provider,config,state,created) VALUES(?,?,?,?,?,?,?)',
                     (key, text, str(Path(workspace).resolve()), provider, json.dumps(config), 'PENDING', time.time()))
        self.event(key, 'submitted', {'provider': provider})
        return key

    def save_graph(self, objective_id, tasks):
        self.execute('BEGIN IMMEDIATE')
        try:
            for t in tasks:
                key = f'{objective_id}/{t.id}'
                self.execute('INSERT INTO task(key,objective_id,id,spec,state) VALUES(?,?,?,?,?)',
                             (key, objective_id, t.id, json.dumps(t.dict()), 'PENDING'))
                self.event(key, 'created', t.dict())
            for t in tasks:
                for dep in t.depends_on:
                    self.execute('INSERT INTO dependency VALUES(?,?)',
                                 (f'{objective_id}/{t.id}', f'{objective_id}/{dep}'))
            self.event(objective_id, 'graph_persisted', [t.dict() for t in tasks])
            self.execute('COMMIT')
        except BaseException:
            self.execute('ROLLBACK')
            raise

    def recheck(self, key):
        """Queue human intervention recovery atomically; never launch a worker here."""
        self.execute('BEGIN IMMEDIATE')
        try:
            task_key = key if '/' in key else None
            obj = self.get('objective', key.split('/')[0])
            if obj['state'] in ('COMPLETED', 'STOPPED'):
                raise ValueError('completed/stopped objectives cannot be rechecked')
            tasks = ([self.get('task', task_key)] if task_key else
                     self.rows('SELECT * FROM task WHERE objective_id=? ORDER BY rowid', (key,)))
            selected = []
            for task in tasks:
                if task['recheck']:
                    selected.append(task['key'])  # Idempotent while queued or executing.
                    continue
                if task['state'] != 'BLOCKED':
                    continue
                deps = self.rows('SELECT t.state FROM dependency d JOIN task t ON t.key=d.depends_key WHERE d.task_key=?', (task['key'],))
                if any(d['state'] != 'COMPLETED' for d in deps):
                    continue
                self.execute("UPDATE task SET recheck='verify',state='PENDING',accepted_stage=NULL WHERE key=?", (task['key'],))
                self.event(task['key'], 'recheck_requested', {'requested_via': key, 'diagnostic': task['diagnostic']})
                selected.append(task['key'])
            if not selected:
                raise ValueError('no blocked task with completed dependencies is available to recheck')
            if obj['state'] == 'BLOCKED':
                self.execute("UPDATE objective SET state='PENDING' WHERE id=?", (obj['id'],))
                self.event(obj['id'], 'transition', {'from': 'BLOCKED', 'to': 'PENDING', 'diagnostic': 'Human requested recheck'})
            self.execute('COMMIT')
            return selected
        except BaseException:
            self.execute('ROLLBACK')
            raise

    def control(self, key, action):
        if action not in ('pause', 'resume', 'stop'):
            raise ValueError(f'unknown control: {action}')
        table = 'task' if '/' in key else 'objective'
        row = self.get(table, key)
        if row['state'] in TERMINAL:
            if action == 'stop' and row['state'] == 'STOPPED':
                return
            raise ValueError('terminal records cannot be controlled; submit a new objective')
        self.execute(f'UPDATE {table} SET control=? WHERE {"key" if table == "task" else "id"}=?',
                     (None if action == 'resume' else action, key))
        self.event(key, 'control', action)
        if action == 'stop':
            self.transition(table, key, 'STOPPED')
            if table == 'objective':
                for task in self.rows('SELECT * FROM task WHERE objective_id=?', (key,)):
                    if task['state'] not in TERMINAL:
                        self.control(task['key'], 'stop')
        elif action == 'resume' and row['state'] == 'PAUSED':
            # A paused active turn may already have passed validation.
            self.transition(table, key, 'PENDING')
            if table == 'objective':
                for task in self.rows("SELECT * FROM task WHERE objective_id=? AND state='PAUSED' AND control IS NULL", (key,)):
                    self.transition('task', task['key'], 'PENDING')
        elif action == 'pause' and row['state'] not in TERMINAL and row['state'] not in ('RUNNING', 'RECONCILING', 'FIXING_PERSISTENT_FAILURE', 'VALIDATING'):
            self.transition(table, key, 'PAUSED')

    def report(self):
        return {name: self.rows(f'SELECT * FROM {name}') for name in
                ('objective', 'task', 'dependency', 'turn', 'validator', 'artifact', 'event')}

    def close(self):
        self.conn.close()
