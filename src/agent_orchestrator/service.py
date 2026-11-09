"""Exclusive service ownership and bounded, live startup acknowledgement.

The owner file locates a probe; only a nonce response while the OS lock is held
establishes readiness. Neither a PID file nor a spawned launcher is authority.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from .quota import DEFAULT_POLL_SECONDS


def canonical_db(path):
    return os.path.normcase(str(Path(path).resolve()))


class ServiceLock:
    def __init__(self, path):
        self.path = Path(canonical_db(path) + '.lock')
        self.acquired = False

    def __enter__(self):
        self.file = self.path.open('a+b')
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise ValueError('another orchestrator service owns this database') from exc
        self.acquired = True
        return self

    def __exit__(self, *args):
        self.acquired = False
        self.file.close()


def lock_available(path):
    try:
        with ServiceLock(path):
            return True
    except ValueError:
        return False


def owner_path(path):
    return Path(canonical_db(path) + '.owner.json')


def read_owner(path):
    try:
        with owner_path(path).open(encoding='utf-8') as stream:
            return json.loads(stream.read(8192))
    except (OSError, ValueError):
        return None


def receive(sock, deadline):
    data = b''
    while b'\n' not in data and len(data) < 8192:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('owner probe deadline')
        sock.settimeout(remaining)
        chunk = sock.recv(8192 - len(data))
        if not chunk:
            raise ValueError('incomplete owner probe')
        data += chunk
    return json.loads(data.split(b'\n', 1)[0])


class OwnerProbe:
    """Started after scheduler/display initialization; closed before lock release."""
    def __init__(self, path, lock, launch_nonce=None):
        self.path = path
        self.lock = lock
        self.stop = threading.Event()
        self.sock = None
        self.thread = None
        self.identity = {
            'canonical_db': canonical_db(path), 'pid': os.getpid(),
            'generation': secrets.token_hex(16), 'launch_nonce': launch_nonce,
            'token': secrets.token_hex(32),
        }

    def __enter__(self):
        if not self.lock.acquired or self.lock.path != Path(canonical_db(self.path) + '.lock'):
            raise ValueError('owner acknowledgement requires the database lock')
        temporary = owner_path(self.path).with_suffix('.' + self.identity['generation'] + '.tmp')
        try:
            self.sock = socket.socket()
            self.sock.bind(('127.0.0.1', 0))
            self.sock.listen(8)
            self.sock.settimeout(.1)
            self.identity['port'] = self.sock.getsockname()[1]
            self.thread = threading.Thread(target=self._run, name='orchestrator-owner', daemon=True)
            self.thread.start()
            temporary.write_text(json.dumps(self.identity), encoding='utf-8')
            os.replace(temporary, owner_path(self.path))
        except BaseException:
            self.__exit__()
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return self

    def _run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    request = receive(conn, time.monotonic() + .2)
                    if (self.lock.acquired and not self.stop.is_set()
                            and request.get('token') == self.identity['token']
                            and request.get('generation') == self.identity['generation']
                            and isinstance(request.get('challenge'), str)):
                        response = dict(self.identity, challenge=request['challenge'])
                        conn.sendall(json.dumps(response).encode() + b'\n')
                except (OSError, ValueError, TypeError, AttributeError):
                    pass

    def __exit__(self, *args):
        self.stop.set()
        if self.sock:
            self.sock.close()
        if self.thread and self.thread.ident is not None:
            self.thread.join(timeout=1)
        record = read_owner(self.path)
        if isinstance(record, dict) and record.get('generation') == self.identity['generation']:
            owner_path(self.path).unlink(missing_ok=True)


def probe_owner(path, deadline):
    record = read_owner(path)
    if not isinstance(record, dict) or record.get('canonical_db') != canonical_db(path):
        return None
    if time.monotonic() >= deadline or lock_available(path):
        return None
    try:
        if (type(record.get('port')) is not int or not 0 < record['port'] < 65536
                or type(record.get('pid')) is not int or record['pid'] <= 0
                or not isinstance(record.get('generation'), str)
                or not isinstance(record.get('token'), str)
                or 'launch_nonce' not in record
                or (record['launch_nonce'] is not None and not isinstance(record['launch_nonce'], str))):
            return None
        challenge = secrets.token_hex(16)
        # Each attempt is short; the caller owns the overall monotonic budget.
        attempt = min(deadline, time.monotonic() + .2)
        with socket.create_connection(('127.0.0.1', record['port']),
                                      timeout=max(.001, attempt - time.monotonic())) as conn:
            conn.settimeout(max(.001, attempt - time.monotonic()))
            conn.sendall(json.dumps({'token': record['token'], 'generation': record['generation'],
                                     'challenge': challenge}).encode() + b'\n')
            response = receive(conn, attempt)
        if (isinstance(response, dict) and response == dict(record, challenge=challenge)
                and time.monotonic() < deadline and not lock_available(path)
                and read_owner(path) == record):
            return record
    except (OSError, ValueError, TypeError, OverflowError):
        pass
    return None


class StartupError(ValueError):
    pass


@dataclass(frozen=True)
class Running:
    pid: int
    started: bool


def launch_service(path, nonce, poll, detached):
    command = [sys.executable, '-m', 'agent_orchestrator', '--db', canonical_db(path),
               'run', '--poll', str(poll), '--startup-nonce', nonce]
    if detached or os.name != 'nt':
        log = Path(path).parent / 'service.log'
        with log.open('ab') as stream:
            flags = ({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                     if os.name == 'nt' else {'start_new_session': True})
            return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, **flags)
    # Inherit console handles on Windows: the new foreground service owns its dashboard.
    return subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE)


def ensure_running(path, *, poll=DEFAULT_POLL_SECONDS, detached=False, timeout=10):
    """Return the live owner, never Popen.pid; do not wait for recovery work."""
    deadline = time.monotonic() + timeout
    nonce = secrets.token_hex(16)
    child = None
    exit_code = None
    while time.monotonic() < deadline:
        owner = probe_owner(path, deadline)
        if owner:
            return Running(owner['pid'], owner['launch_nonce'] == nonce)
        if child is not None:
            exit_code = child.poll()
            if exit_code is not None:
                # A contender can lose ownership and exit before its rival publishes.
                # A Python redirector can exit before its interpreter acquires
                # the lock. Its exit (even zero) is neither success nor failure.
                owner = probe_owner(path, deadline)
                if owner:
                    return Running(owner['pid'], owner['launch_nonce'] == nonce)
        elif lock_available(path):
            try:
                child = launch_service(path, nonce, poll, detached)
            except OSError as exc:
                owner = probe_owner(path, deadline)
                if owner:
                    return Running(owner['pid'], False)
                raise StartupError(str(exc)) from exc
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(.05, remaining))
    if exit_code is not None:
        raise StartupError(f'launcher exited with code {exit_code}; startup acknowledgement timed out; no live owner')
    raise StartupError('startup acknowledgement timed out (another orchestrator service owns this database but is unresponsive)'
                       if not lock_available(path) else 'startup acknowledgement timed out; no live owner')
