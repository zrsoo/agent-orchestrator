"""Inspect a fixture scheduler's real Windows console from a separate process."""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import sys

kernel = c.WinDLL('kernel32', use_last_error=True)
user = c.WinDLL('user32', use_last_error=True)
kernel.AttachConsole.argtypes = [w.DWORD]
kernel.GetConsoleWindow.restype = w.HWND
user.IsWindowVisible.argtypes = [w.HWND]
kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
kernel.CreateFileW.restype = w.HANDLE
kernel.CloseHandle.argtypes = [w.HANDLE]


class Coord(c.Structure):
    _fields_ = [('x', c.c_short), ('y', c.c_short)]


class Rect(c.Structure):
    _fields_ = [(name, c.c_short) for name in ('left', 'top', 'right', 'bottom')]


class Info(c.Structure):
    _fields_ = [('size', Coord), ('cursor', Coord), ('attributes', w.WORD),
                ('window', Rect), ('maximum', Coord)]


kernel.GetConsoleScreenBufferInfo.argtypes = [w.HANDLE, c.POINTER(Info)]
kernel.ReadConsoleOutputCharacterW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, Coord, c.POINTER(w.DWORD)]


def inspect(pid, interrupt=False):
    kernel.FreeConsole()
    if not kernel.AttachConsole(pid):
        raise c.WinError(c.get_last_error())
    try:
        if interrupt:
            # Ignore Ctrl+C in this helper; deliver a real console event to the
            # fixture scheduler and let its Python KeyboardInterrupt unwind.
            if not kernel.SetConsoleCtrlHandler(None, True):
                raise c.WinError(c.get_last_error())
            if not kernel.GenerateConsoleCtrlEvent(0, 0):
                raise c.WinError(c.get_last_error())
            return {'ctrl_c_sent': True}
        hwnd = kernel.GetConsoleWindow()
        pids = (w.DWORD * 64)()
        count = kernel.GetConsoleProcessList(pids, len(pids))
        handle = kernel.CreateFileW('CONOUT$', 0x80000000, 3, None, 3, 0, None)
        if handle == w.HANDLE(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            info = Info()
            if not kernel.GetConsoleScreenBufferInfo(handle, c.byref(info)):
                raise c.WinError(c.get_last_error())
            size = min(info.size.x * info.size.y, 200000)
            buffer = c.create_unicode_buffer(size+1)
            read = w.DWORD()
            if not kernel.ReadConsoleOutputCharacterW(handle, buffer, size, Coord(0, 0), c.byref(read)):
                raise c.WinError(c.get_last_error())
            return {'hwnd': hwnd, 'visible': bool(user.IsWindowVisible(hwnd)),
                    'pids': list(pids)[:count], 'text': buffer[:read.value]}
        finally:
            kernel.CloseHandle(handle)
    finally:
        kernel.FreeConsole()


if __name__ == '__main__':
    Path(sys.argv[2]).write_text(json.dumps(inspect(int(sys.argv[1]), '--ctrl-c' in sys.argv)), encoding='utf-8')
