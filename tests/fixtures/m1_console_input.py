"""Send real console input records / resize to an isolated fixture console."""
import ctypes as c
from ctypes import wintypes as w
import sys
import threading

from agent_orchestrator.terminal import Terminal
from console_probe import kernel, Coord, Rect

kernel.FreeConsole()
assert kernel.AttachConsole(int(sys.argv[1]))
try:
    term = Terminal(sys.stdout, False, threading.Event())
    term.open()
    k = term.kernel
    handle = kernel.CreateFileW('CONIN$', 0xC0000000, 3, None, 3, 0, None)
    assert handle != w.HANDLE(-1).value
    try:
        action = sys.argv[2]
        if action == 'resize':
            output = kernel.CreateFileW('CONOUT$', 0xC0000000, 3, None, 3, 0, None)
            try:
                kernel.SetConsoleWindowInfo.argtypes = [w.HANDLE, w.BOOL, c.POINTER(Rect)]
                rect = Rect(0, 0, 59, 14)
                assert kernel.SetConsoleWindowInfo(output, True, c.byref(rect)), c.get_last_error()
            finally:
                kernel.CloseHandle(output)
        else:
            record = term.Record()
            if action == 'wheel':
                record.kind = 2
                record.event.mouse.flags = 4
                record.event.mouse.buttons = (-120 & 0xffff) << 16
                record.event.mouse.position.x = 5
                record.event.mouse.position.y = 10
            else:
                record.kind = 1
                record.event.key.down = True
                record.event.key.repeat = 1
                record.event.key.vk = {'home': 0x24, 'end': 0x23, 'page_down': 0x22,
                                      'next': 9, 'active': 65}[action]
                if action == 'active':
                    record.event.key.char = 'a'
            k.WriteConsoleInputW.argtypes = [w.HANDLE, c.POINTER(term.Record), w.DWORD, c.POINTER(w.DWORD)]
            count = w.DWORD()
            assert k.WriteConsoleInputW(handle, c.byref(record), 1, c.byref(count))
            assert count.value == 1
    finally:
        kernel.CloseHandle(handle)
finally:
    kernel.FreeConsole()
