"""Failure episodes are distinct from quota/transport retries."""
import json


def disrupted(db, key, diagnostic, blocked=False):
    row = db.get('task', key)
    attempts = row['attempts'] + 1
    db.execute('UPDATE task SET attempts=? WHERE key=?', (attempts, key))
    checkpoints = db.rows("SELECT data FROM artifact WHERE owner=? AND kind='checkpoint' ORDER BY id DESC LIMIT 1", (key,))
    files = json.loads(checkpoints[0]['data'])['files'] if checkpoints else {}
    episodes = db.rows("SELECT data FROM event WHERE owner=? AND kind='failure_episode' ORDER BY id DESC LIMIT 1", (key,))
    previous = json.loads(episodes[0]['data']) if episodes else None
    no_progress = previous is not None and previous['files'] == files
    db.event(key, 'failure_episode', {'attempt': attempts, 'files': files, 'diagnostic': diagnostic})
    state = 'BLOCKED' if blocked or (attempts >= 3 and no_progress) else 'FIXING_PERSISTENT_FAILURE' if attempts >= 2 else 'RUNNING'
    db.transition('task', key, state, diagnostic)
    return state
