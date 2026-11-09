"""Deterministic workspace evidence; interpretation belongs to the worker."""
import hashlib
import json
from pathlib import Path
import subprocess
import time


def checkpoint(db, owner, workspace, stage):
    root = Path(workspace)
    files = {}
    excluded = {'.git', '.venv', 'node_modules', '__pycache__', '.orchestrator', '.m1-progress', '.pytest_cache'}
    import os
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in excluded]
        for name in names:
            p = Path(folder) / name
            if str(p.resolve()) in {str(db.path) + suffix for suffix in ('', '-wal', '-shm', '.lock')}:
                continue
            if p.is_symlink():
                continue
            try:
                if p.stat().st_size <= 10_000_000:
                    files[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                continue
    def git(*args):
        try:
            p = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=30)
            return p.stdout if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    previous = db.rows("SELECT data FROM artifact WHERE owner=? AND kind='checkpoint' ORDER BY id DESC LIMIT 1", (owner,))
    old = json.loads(previous[0]['data'])['files'] if previous else {}
    dirty_files = git('diff', 'HEAD', '--name-only', '-z')
    untracked = git('ls-files', '--others', '--exclude-standard', '-z')
    dirty_names = set((dirty_files or '').split('\0') + (untracked or '').split('\0')) - {''}
    data = {'stage': stage, 'files': files, 'changed': sorted(k for k in files.keys() | old.keys() if files.get(k) != old.get(k)),
            'dirty_files': sorted(dirty_names) if dirty_files is not None else None,
            'revision': git('rev-parse', 'HEAD'), 'status': git('status', '--porcelain'), 'diff': git('diff', 'HEAD')}
    db.execute('INSERT INTO artifact(owner,at,kind,data) VALUES(?,?,?,?)',
               (owner, time.time(), 'checkpoint', json.dumps(data)))
    return data
