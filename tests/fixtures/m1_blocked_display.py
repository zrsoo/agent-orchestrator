"""A real blocked output pipe, fixture worker tree, and actual CLI unwinding."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from agent_orchestrator import cli
from agent_orchestrator.database import Database
from agent_orchestrator.processes import ProcessTree
from agent_orchestrator.service import lock_available
from agent_orchestrator.terminal import Terminal

root = Path(sys.argv[1])
blocked = threading.Event()
facts = {}
write = Terminal.write


def watched_write(self, text, **kwargs):
    if len(text) > 10000:
        blocked.set()
    return write(self, text, **kwargs)


Terminal.write = watched_write


class Stream:
    def isatty(self):
        return False
    def fileno(self):
        return sys.__stdout__.fileno()
    def write(self, text):
        if len(text) > 10000:
            blocked.set()
        return sys.__stdout__.write(text)
    def flush(self):
        sys.__stdout__.flush()


class HeldScheduler:
    def __init__(self, db, output=None):
        self.db, self.output = db, output
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        self.proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], **flags)
        self.tree = ProcessTree(self.proc)
    def tick(self):
        if not (root/'initial').exists():
            key = self.db.submit('blocked output fixture', root, 'codex', {})
            self.db.execute('UPDATE objective SET diagnostic=? WHERE id=?', ('x'*200000, key))
            assert blocked.wait(5)
            time.sleep(.1)  # Let the real pipe fill before unwinding.
        self.output('notice must not block', flush=True)
        facts['notice_returned'] = True
        raise KeyboardInterrupt()
    def close(self):
        facts['lock_held_during_close'] = not lock_available(self.db.path)
        self.tree.close()
        self.proc.wait(timeout=5)
        facts['provider_closed'] = self.proc.poll() is not None


if (root/'initial').exists():
    db = Database(root/'state.db')
    key = db.submit('initial blocked output fixture', root, 'codex', {})
    db.execute('UPDATE objective SET diagnostic=? WHERE id=?', ('x'*200000, key))
    db.close()
cli.Scheduler = HeldScheduler
sys.stdout = Stream()
result = cli.main(['--db', str(root/'state.db'), 'run', '--poll', '.01'])
facts.update(returncode=result, lock_released=lock_available(root/'state.db'),
             display_threads=[t.name for t in threading.enumerate() if t.name == 'orchestrator-dashboard'])
(root/'result.json').write_text(json.dumps(facts), encoding='utf-8')
raise SystemExit(result)
