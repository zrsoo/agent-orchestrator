"""Real console viewport/input fixture, no scheduler or provider processes."""
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import sys
import threading
import time

from agent_orchestrator.dashboard import Dashboard
from agent_orchestrator.database import Database
from agent_orchestrator.models import graph
from console_probe import kernel

root = Path(sys.argv[1])
if '--conpty' in sys.argv:
    # This harness is launched by redirected pytest, not by a shell in the PTY.
    # Bind the fixture's standard streams to its attached pseudoconsole.
    import msvcrt
    sys.stdin = open('CONIN$', 'r', encoding='utf-8')
    sys.stdout = open('CONOUT$', 'w', encoding='utf-8', buffering=1)
    kernel.SetStdHandle.argtypes = [w.DWORD, w.HANDLE]
    assert kernel.SetStdHandle(-10, msvcrt.get_osfhandle(sys.stdin.fileno()))
    assert kernel.SetStdHandle(-11, msvcrt.get_osfhandle(sys.stdout.fileno()))
db = Database(root/'state.db')
key = db.submit('Scrollable DAG', root, 'codex', {})
db.save_graph(key, graph([{'id': f'task{i:03}', 'workspace': str(root),
                         'objective': f'review item {i:03}', 'acceptance': ['done']} for i in range(80)]))
db.transition('task', key+'/task055', 'RUNNING')
db.transition('objective', key, 'RUNNING')
other = db.submit('Second objective', root, 'codex', {})
db.control(other, 'pause')
kernel.GetStdHandle.argtypes = [w.DWORD]
kernel.GetStdHandle.restype = w.HANDLE
kernel.GetConsoleMode.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
handles = [kernel.GetStdHandle(-10), kernel.GetStdHandle(-11)]
def modes():
    values = []
    for handle in handles:
        mode = w.DWORD()
        assert kernel.GetConsoleMode(handle, c.byref(mode))
        values.append(mode.value)
    return values
before = modes()
sys.stdout.write('Original scroll fixture screen\n')
sys.stdout.flush()
try:
    with Dashboard(db.path) as display:
        ready = root/'ready.tmp'
        ready.write_text(json.dumps({'pid': os.getpid(), 'key': key, 'other': other,
                                     'before_modes': before, 'during_modes': modes()}))
        ready.replace(root/'ready.json')
        deadline = time.monotonic()+30
        while time.monotonic() < deadline and not (root/'release').exists():
            time.sleep(.03)
    (root/'result.json').write_text(json.dumps({'after_modes': modes(),
            'threads': [t.name for t in threading.enumerate()], 'failed': display.failed}))
finally:
    db.close()
