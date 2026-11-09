"""Reusable operational documentation contracts extracted from M1 acceptance."""
from contextlib import redirect_stderr, redirect_stdout
import inspect
import io
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import unittest
from unittest.mock import patch
from agent_orchestrator import cli, dashboard, service
from agent_orchestrator.database import Database
from agent_orchestrator.models import graph
ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / 'README.md').read_text(encoding='utf-8')
SETUP = (ROOT / 'docs/migration.md').read_text(encoding='utf-8')

class DocumentationChecks(unittest.TestCase):
    def test_criterion_0_operational_instructions(self):
        first = re.findall(r'```powershell\n(.*?)```', README, re.S)[0]
        self.assertIn('.venv/Scripts/orchestrator run\n', first)
        self.assertNotIn('--detach', first)
        commands = re.findall(r'^\.venv/Scripts/orchestrator (.+)$', README, re.M)
        self.assertGreaterEqual(len(commands), 10)
        for command in commands:
            cli.parser().parse_args(shlex.split(command))
        for phrase in ('preferred interactive workflow', 'second terminal',
                       '250 ms', 'read-only', 'committed SQLite', 'NO_COLOR',
                       'TERM=dumb', 'without elapsed-only updates',
                       'terminal escapes', 'service.log', 'Ctrl+C',
                       '`run --once`, `status`', '`status --json`'):
            self.assertIn(phrase, README)
        self.assertEqual(dashboard.REFRESH_SECONDS * 1000, 250)
        for phrase in ('Prefer foreground', 'second terminal',
                       'Redirected/non-interactive', 'same `--db`',
                       'failure retains the queued request'):
            self.assertIn(phrase, SETUP)
        # Relative documentation links must resolve to existing artifacts.
        for name, text in (('README.md', README),
                           ('docs/migration.md', SETUP)):
            for target in re.findall(r'\]\(([^)]+)\)', text):
                if '://' not in target and not target.startswith('#'):
                    self.assertTrue((ROOT / name).parent.joinpath(
                        target.split('#')[0]).is_file(), (name, target))

    def test_criterion_1_documented_startup_and_durable_recovery(self):
        self.assertEqual(inspect.signature(service.ensure_running).parameters['timeout'].default, 10)
        for phrase in ('ten-second monotonic startup', 'before the first scheduling step',
                       'does not wait for recovery', 'preserves the committed request',
                       'retry `recheck` or start `run` with the same DB',
                       'stale records are never', 'Repeated requests',
                       'Completed/stopped records cannot be rechecked'):
            self.assertIn(phrase, README)
        # Execute the documented command outcomes without launching any service.
        # The startup callback reads through another connection to prove commit
        # precedes startup and reopening proves queue survival after exit 2.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.db'
            db = Database(path)
            try:
                key = db.submit('documentation fixture', temp, 'codex', {})
                db.save_graph(key, graph([{'id': 'a', 'workspace': temp,
                    'objective': 'documentation fixture', 'acceptance': ['fixture']}]))
                task = key + '/a'
                db.transition('task', task, 'BLOCKED', 'fixture prerequisite')
                db.transition('objective', key, 'BLOCKED')
            finally:
                db.close()
            for mode in ('existing', 'started', 'failed'):
                with self.subTest(mode=mode):
                    def startup(db_path):
                        reopened = Database(db_path)
                        try:
                            self.assertEqual(reopened.get('task', task)['recheck'], 'verify')
                        finally:
                            reopened.close()
                        if mode == 'failed':
                            raise service.StartupError('fixture failure')
                        return service.Running(12345, mode == 'started')
                    out, err = io.StringIO(), io.StringIO()
                    with patch.object(cli, 'ensure_running', startup), redirect_stdout(out), redirect_stderr(err):
                        code = cli.main(['--db', str(path), 'recheck', task])
                    message = (err if mode == 'failed' else out).getvalue().strip()
                    documented = message.replace('12345', '<pid>').replace('fixture failure', '<reason>')
                    if mode == 'started' and os.name != 'nt':
                        documented = documented[:-1] + '; dashboard opened.'
                    self.assertIn('`' + documented + '`', README)
                    self.assertEqual(code, 2 if mode == 'failed' else 0)
                    self.assertEqual((out if mode == 'failed' else err).getvalue(), '')
            reopened = Database(path)
            try:
                self.assertEqual(reopened.get('task', task)['recheck'], 'verify')
                self.assertEqual(len(reopened.rows("SELECT * FROM event WHERE kind='recheck_requested'")), 1)
            finally:
                reopened.close()
