"""Own the provider process tree, including tools and MCP children on Windows."""
import os
import signal


class ProcessTree:
    def __init__(self, proc):
        self.proc = proc
        self.handle = None
        if os.name != 'nt':
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                        ('min_working', c.c_size_t), ('max_working', c.c_size_t),
                        ('active', w.DWORD), ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class Counters(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

        class Extended(c.Structure):
            _fields_ = [('basic', Basic), ('io', Counters), ('process_memory', c.c_size_t),
                        ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]

        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)) or not self.kernel.AssignProcessToJobObject(self.handle, int(proc._handle)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def close(self):
        if os.name == 'nt':
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
