"""State-based waits for tests that drive the synchronous scheduler."""
import time


def complete_objective(scheduler, db, key, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = db.get('objective', key)['state']
        if state == 'COMPLETED':
            tasks = db.rows('SELECT state FROM task WHERE objective_id=?', (key,))
            assert tasks and all(t['state'] == 'COMPLETED' for t in tasks), db.report()
            return
        assert state not in ('BLOCKED', 'STOPPED'), db.report()
        scheduler.tick()
        # A tick can defer a retry or complete a task without completing its objective.
        time.sleep(min(.25, max(0, deadline - time.monotonic())))
    raise AssertionError(f'objective {key} did not complete within {timeout}s: {db.report()}')
