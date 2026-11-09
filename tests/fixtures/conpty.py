"""Small Windows pseudoconsole harness for real VT input/output integration tests."""
import ctypes as c
from ctypes import wintypes as w
import subprocess
import threading


class Coord(c.Structure):
    _fields_ = [('x', c.c_short), ('y', c.c_short)]


class Startup(c.Structure):
    _fields_ = [('cb', w.DWORD), ('reserved', w.LPWSTR), ('desktop', w.LPWSTR), ('title', w.LPWSTR),
                *[(name, w.DWORD) for name in ('x', 'y', 'width', 'height', 'chars_x', 'chars_y', 'fill', 'flags')],
                ('show', w.WORD), ('reserved_size', w.WORD), ('reserved_ptr', c.c_void_p),
                ('stdin', w.HANDLE), ('stdout', w.HANDLE), ('stderr', w.HANDLE)]


class StartupEx(c.Structure):
    _fields_ = [('startup', Startup), ('attributes', c.c_void_p)]


class Process(c.Structure):
    _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE), ('pid', w.DWORD), ('tid', w.DWORD)]


class PseudoConsole:
    def __init__(self, argv):
        self.k = k = c.WinDLL('kernel32', use_last_error=True)
        k.CreatePipe.argtypes = [c.POINTER(w.HANDLE), c.POINTER(w.HANDLE), c.c_void_p, w.DWORD]
        k.CreatePseudoConsole.argtypes = [Coord, w.HANDLE, w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
        k.ResizePseudoConsole.argtypes = [w.HANDLE, Coord]
        k.ClosePseudoConsole.argtypes = [w.HANDLE]
        k.CloseHandle.argtypes = [w.HANDLE]
        k.InitializeProcThreadAttributeList.argtypes = [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)]
        k.UpdateProcThreadAttribute.argtypes = [c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p]
        k.DeleteProcThreadAttributeList.argtypes = [c.c_void_p]
        k.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD,
                                    c.c_void_p, w.LPCWSTR, c.POINTER(StartupEx), c.POINTER(Process)]
        k.ReadFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
        k.WriteFile.argtypes = k.ReadFile.argtypes
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        self.pc = w.HANDLE()
        self.process = Process()
        self.input, self.output = w.HANDLE(), w.HANDLE()
        self.text = b''
        self.lock = threading.Lock()
        pipe_read, pipe_write = w.HANDLE(), w.HANDLE()
        assert k.CreatePipe(c.byref(pipe_read), c.byref(self.input), None, 0)
        assert k.CreatePipe(c.byref(self.output), c.byref(pipe_write), None, 0)
        try:
            assert k.CreatePseudoConsole(Coord(100, 25), pipe_read, pipe_write, 0, c.byref(self.pc)) == 0
        finally:
            k.CloseHandle(pipe_read)
            k.CloseHandle(pipe_write)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()  # ConPTY output must drain even during process creation/close.
        size = c.c_size_t()
        k.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
        attributes = c.create_string_buffer(size.value)
        assert k.InitializeProcThreadAttributeList(attributes, 1, 0, c.byref(size))
        try:
            assert k.UpdateProcThreadAttribute(attributes, 0, 0x20016, self.pc, c.sizeof(self.pc), None, None)
            startup = StartupEx()
            startup.startup.cb = c.sizeof(startup)
            startup.attributes = c.cast(attributes, c.c_void_p)
            command = c.create_unicode_buffer(subprocess.list2cmdline([str(a) for a in argv]))
            assert k.CreateProcessW(None, command, None, None, False, 0x80000,
                                    None, None, c.byref(startup), c.byref(self.process)), c.get_last_error()
        except BaseException:
            self.close()
            raise
        finally:
            k.DeleteProcThreadAttributeList(attributes)

    def _read(self):
        data = c.create_string_buffer(16384)
        count = w.DWORD()
        while self.k.ReadFile(self.output, data, len(data), c.byref(count), None) and count.value:
            with self.lock:
                self.text = (self.text+data.raw[:count.value])[-2_000_000:]

    def frame(self):
        with self.lock:
            return self.text.decode('utf-8', errors='replace').split('\x1b[H\x1b[J')[-1]

    def send(self, text):
        data = text.encode('ascii')
        count = w.DWORD()
        assert self.k.WriteFile(self.input, data, len(data), c.byref(count), None)
        assert count.value == len(data)

    def resize(self, width, height):
        assert self.k.ResizePseudoConsole(self.pc, Coord(width, height)) == 0

    def wait(self, timeout=5000):
        assert self.k.WaitForSingleObject(self.process.process, timeout) == 0, 'pseudoconsole process hung'
        code = w.DWORD()
        assert self.k.GetExitCodeProcess(self.process.process, c.byref(code))
        return code.value

    def close(self):
        if self.process.process:
            if self.k.WaitForSingleObject(self.process.process, 0) != 0:
                subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'], capture_output=True, timeout=10)
            self.k.CloseHandle(self.process.thread)
            self.k.CloseHandle(self.process.process)
            self.process.process = None
        if self.pc:
            self.k.ClosePseudoConsole(self.pc)
            self.pc = None
        self.reader.join(2)
        self.k.CloseHandle(self.input)
        self.k.CloseHandle(self.output)
