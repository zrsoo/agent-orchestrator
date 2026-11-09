"""Public task contract and strict graph validation."""
from dataclasses import dataclass, field, asdict
from pathlib import Path
import re

STATES = frozenset('PENDING RUNNING VALIDATING WAITING_QUOTA RECONCILING RETRYING FIXING_PERSISTENT_FAILURE PAUSED BLOCKED COMPLETED STOPPED'.split())
TERMINAL = frozenset(('COMPLETED', 'BLOCKED', 'STOPPED'))


@dataclass
class Task:
    id: str
    workspace: str
    objective: str
    acceptance: list[str]
    inputs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    def dict(self):
        return asdict(self)


def graph(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError('tasks must be a nonempty list')
    tasks = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - set(Task.__dataclass_fields__):
            raise ValueError('unknown task fields')
        try:
            t = Task(**item)
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(t.id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', t.id):
            raise ValueError('invalid task ID')
        if not isinstance(t.workspace, str) or not Path(t.workspace).is_absolute():
            raise ValueError('workspace must be absolute')
        if not isinstance(t.objective, str) or not t.objective.strip():
            raise ValueError('objective required')
        for name in ('acceptance', 'inputs', 'depends_on'):
            values = getattr(t, name)
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(f'invalid {name}')
        if not t.acceptance or len(set(t.depends_on)) != len(t.depends_on):
            raise ValueError('acceptance required; dependencies must be unique')
        tasks.append(t)
    by_id = {t.id: t for t in tasks}
    if len(by_id) != len(tasks):
        raise ValueError('duplicate task ID')
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            raise ValueError('dependency cycle')
        if key not in by_id:
            raise ValueError(f'unknown dependency: {key}')
        if key in visited:
            return
        visiting.add(key)
        for dep in by_id[key].depends_on:
            visit(dep)
        visiting.remove(key)
        visited.add(key)

    for key in by_id:
        visit(key)
    return tasks
