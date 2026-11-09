"""Bounded terminal input and cancellable output; no scheduling or database access."""
import os
import re
import select
import sys
import time


class Terminal:
    def __init__(self, stream, interactive, stop, input_stream=None):
        self.stream, self.interactive, self.stop = stream, interactive, stop
        self.input = sys.stdin if input_stream is None else input_stream
        self.modes = []
        self.input_handle = None
        self.input_fd = None
        self.output_fd = None
        self.output_blocking = None
        self.input_bytes = b''
        self.kernel = None
        self.output_failed = False

    def open(self):
        if os.name == 'nt':
            self._windows_open()
        else:
            self._posix_open()

    def _windows_open(self):
        import ctypes as c
        from ctypes import wintypes as w
        import msvcrt
        self.kernel = k = c.WinDLL('kernel32', use_last_error=True)
        k.GetConsoleMode.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        k.SetConsoleMode.argtypes = [w.HANDLE, w.DWORD]
        k.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenThread.restype = w.HANDLE
        k.CancelSynchronousIo.argtypes = [w.HANDLE]
        k.CloseHandle.argtypes = [w.HANDLE]
        k.GetNumberOfConsoleInputEvents.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]

        class Coord(c.Structure):
            _fields_ = [('x', c.c_short), ('y', c.c_short)]
        class Rect(c.Structure):
            _fields_ = [(n, c.c_short) for n in ('left', 'top', 'right', 'bottom')]
        class Info(c.Structure):
            _fields_ = [('size', Coord), ('cursor', Coord), ('attributes', w.WORD),
                        ('window', Rect), ('maximum', Coord)]
        class Key(c.Structure):
            _fields_ = [('down', w.BOOL), ('repeat', w.WORD), ('vk', w.WORD),
                        ('scan', w.WORD), ('char', w.WCHAR), ('control', w.DWORD)]
        class Mouse(c.Structure):
            _fields_ = [('position', Coord), ('buttons', w.DWORD),
                        ('control', w.DWORD), ('flags', w.DWORD)]
        class Event(c.Union):
            _fields_ = [('key', Key), ('mouse', Mouse), ('size', Coord)]
        class Record(c.Structure):
            _fields_ = [('kind', w.WORD), ('event', Event)]
        self.Info, self.Record = Info, Record
        k.GetConsoleScreenBufferInfo.argtypes = [w.HANDLE, c.POINTER(Info)]
        k.ReadConsoleInputW.argtypes = [w.HANDLE, c.POINTER(Record), w.DWORD, c.POINTER(w.DWORD)]

        self.output_handle = None
        if not self.interactive:
            return
        try:
            handle = msvcrt.get_osfhandle(self.stream.fileno())
            mode = w.DWORD()
            if k.GetConsoleMode(handle, c.byref(mode)) and k.SetConsoleMode(handle, mode.value | 4):
                self.modes.append((handle, mode.value))
                self.output_handle = handle
            if self.input.isatty():
                handle = msvcrt.get_osfhandle(self.input.fileno())
                if k.GetConsoleMode(handle, c.byref(mode)):
                    # Keep processed Ctrl+C; disable echo/line input/Quick Edit/VT input.
                    flags = (mode.value | 0x1 | 0x8 | 0x10 | 0x80) & ~(0x2 | 0x4 | 0x40 | 0x200)
                    if k.SetConsoleMode(handle, flags):
                        self.modes.append((handle, mode.value))
                        self.input_handle = handle
        except (OSError, ValueError, AttributeError):
            pass  # Injected streams and unavailable console input still render safely.

    def _posix_open(self):
        try:
            self.output_fd = os.dup(self.stream.fileno())
            self.output_blocking = os.get_blocking(self.output_fd)
            os.set_blocking(self.output_fd, False)
        except (OSError, ValueError, AttributeError):
            if self.output_fd is not None:
                os.close(self.output_fd)
            self.output_fd = None
        if self.interactive and self.input.isatty():
            import termios
            import tty
            self.input_fd = self.input.fileno()
            self.saved_input = termios.tcgetattr(self.input_fd)
            tty.setcbreak(self.input_fd)  # ISIG remains enabled for Ctrl+C.

    def size(self):
        if os.name == 'nt' and self.kernel and self.output_handle is not None:
            import ctypes as c
            info = self.Info()
            if self.kernel.GetConsoleScreenBufferInfo(self.output_handle, c.byref(info)):
                return max(1, info.window.right-info.window.left+1), max(1, info.window.bottom-info.window.top+1)
        try:
            size = os.get_terminal_size(self.stream.fileno())
            return max(1, size.columns), max(1, size.lines)
        except (OSError, ValueError, AttributeError):
            return 100, 30

    def write(self, text, *, final=False):
        try:
            self._write(text, final=final)
        except Exception:
            self.output_failed = True
            raise

    def _write(self, text, *, final=False):
        if self.output_fd is None:
            self.stream.write(text)
            self.stream.flush()
            return
        data = text.encode('ascii')
        deadline = time.monotonic() + (0.3 if final else 3600)
        while data:
            if self.stop.is_set() and not final:
                return
            if time.monotonic() >= deadline:
                raise OSError('display output stopped')
            try:
                written = os.write(self.output_fd, data)
                if not written:
                    raise OSError('display output closed')
                data = data[written:]
            except BlockingIOError:
                select.select([], [self.output_fd], [], .05)

    def cancel_output(self, thread):
        if os.name == 'nt' and self.kernel and thread.native_id is not None:
            handle = self.kernel.OpenThread(1, False, thread.native_id)
            if handle:
                try:
                    self.kernel.CancelSynchronousIo(handle)
                finally:
                    self.kernel.CloseHandle(handle)

    def poll(self):
        if not self.interactive:
            return []
        if os.name == 'nt':
            return self._windows_poll()
        if self.input_fd is None or not select.select([self.input_fd], [], [], 0)[0]:
            return []
        self.input_bytes += os.read(self.input_fd, 4096)
        events = []
        keys = {b'\x1b[A': 'up', b'\x1b[B': 'down', b'\x1b[5~': 'page_up',
                b'\x1b[6~': 'page_down', b'\x1b[H': 'home', b'\x1b[F': 'end',
                b'\x1b[1~': 'home', b'\x1b[4~': 'end', b'\x1b[Z': 'previous',
                b'\t': 'next', b'a': 'active', b'A': 'active'}
        while self.input_bytes:
            mouse = re.match(rb'\x1b\[<(\d+);\d+;\d+[Mm]', self.input_bytes)
            if mouse:
                button = int(mouse[1])
                if button & 64:
                    events.append(('scroll', 3 if button & 1 else -3))
                self.input_bytes = self.input_bytes[mouse.end():]
                continue
            match = next((key for key in keys if self.input_bytes.startswith(key)), None)
            if match:
                events.append((keys[match], 1))
                self.input_bytes = self.input_bytes[len(match):]
            elif (any(key.startswith(self.input_bytes) for key in keys)
                  or self.input_bytes.startswith(b'\x1b[<') and len(self.input_bytes) < 64):
                break
            else:
                self.input_bytes = self.input_bytes[1:]
        return events

    def _windows_poll(self):
        if self.input_handle is None:
            return []
        import ctypes as c
        from ctypes import wintypes as w
        count = w.DWORD()
        if not self.kernel.GetNumberOfConsoleInputEvents(self.input_handle, c.byref(count)) or not count.value:
            return []
        records = (self.Record * min(count.value, 64))()
        if not self.kernel.ReadConsoleInputW(self.input_handle, records, len(records), c.byref(count)):
            return []
        keys = {0x26: 'up', 0x28: 'down', 0x21: 'page_up', 0x22: 'page_down',
                0x24: 'home', 0x23: 'end'}
        events = []
        for r in records[:count.value]:
            if r.kind == 1 and r.event.key.down:
                key = r.event.key
                action = ('previous' if key.control & 0x10 else 'next') if key.vk == 9 else keys.get(key.vk)
                if key.char.lower() == 'a':
                    action = 'active'
                if action:
                    events.append((action, max(1, min(key.repeat, 20))))
            elif r.kind == 2 and r.event.mouse.flags & 4:
                delta = c.c_short(r.event.mouse.buttons >> 16).value
                if delta:
                    events.append(('scroll', -3 * (int(delta/120) or (1 if delta > 0 else -1))))
            elif r.kind == 4:
                events.append(('resize', 1))
        return events

    def close(self):
        for handle, mode in reversed(self.modes):
            self.kernel.SetConsoleMode(handle, mode)
        self.modes.clear()
        if self.input_fd is not None:
            import termios
            termios.tcsetattr(self.input_fd, termios.TCSANOW, self.saved_input)
            self.input_fd = None
        if self.output_fd is not None:
            try:
                os.set_blocking(self.output_fd, self.output_blocking)
            finally:
                os.close(self.output_fd)
                self.output_fd = None
