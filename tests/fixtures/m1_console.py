"""Run the actual CLI on a Windows console and capture terminal cleanup facts."""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import sys
import threading
import traceback

from agent_orchestrator.cli import main
from console_probe import Coord, Info, kernel


class Tee:
    def __init__(self, console, trace):
        self.console, self.trace = console, trace
    def isatty(self):
        return self.console.isatty()
    def fileno(self):
        return self.console.fileno()
    def write(self, value):
        self.trace.write(value)
        return self.console.write(value)
    def flush(self):
        self.trace.flush()
        self.console.flush()


class Cursor(c.Structure):
    _fields_ = [('size', w.DWORD), ('visible', w.BOOL)]


kernel.GetConsoleCursorInfo.argtypes = [w.HANDLE, c.POINTER(Cursor)]
root = Path(sys.argv[1])
def exception_file(kind, value, tb):
    (root / 'terminal-error.txt').write_text(''.join(traceback.format_exception(kind, value, tb)))
sys.excepthook = exception_file
# Ensure Python can receive Ctrl+C even if a host inherited the ignore flag.
if not kernel.SetConsoleCtrlHandler(None, False):
    raise c.WinError(c.get_last_error())
original = sys.stdout
original.write('M1 original console buffer\n')
original.flush()
with (root / 'terminal.txt').open('w', encoding='utf-8') as trace:
    sys.stdout = Tee(original, trace)
    try:
        result = main(['--db', str(root / 'state.db'), 'run', '--poll', '.05'])
    finally:
        sys.stdout = original
handle = kernel.CreateFileW('CONOUT$', 0xC0000000, 3, None, 3, 0, None)
if handle == w.HANDLE(-1).value:
    raise c.WinError(c.get_last_error())
try:
    cursor, info = Cursor(), Info()
    if not kernel.GetConsoleCursorInfo(handle, c.byref(cursor)):
        raise c.WinError(c.get_last_error())
    if not kernel.GetConsoleScreenBufferInfo(handle, c.byref(info)):
        raise c.WinError(c.get_last_error())
    size = min(info.size.x * info.size.y, 200000)
    buffer, count = c.create_unicode_buffer(size + 1), w.DWORD()
    if not kernel.ReadConsoleOutputCharacterW(handle, buffer, size, Coord(0, 0), c.byref(count)):
        raise c.WinError(c.get_last_error())
    facts = {'returncode': result, 'isatty': original.isatty(),
             'cursor_visible': bool(cursor.visible), 'screen': buffer[:count.value],
             'threads': [t.name for t in threading.enumerate()]}
    (root / 'terminal-result.json').write_text(json.dumps(facts), encoding='utf-8')
finally:
    kernel.CloseHandle(handle)
