"""Bounded recovery context. Full evidence remains in SQLite and the workspace."""
import json
from .backends.base import InputSizeError


# One limit for the complete outgoing text, below App Server's 1,048,576 characters.
MAX_PROMPT_CHARS = 900_000


def check_prompt(prompt):
    if len(prompt) > MAX_PROMPT_CHARS:
        raise InputSizeError(
            f'Outgoing prompt has {len(prompt):,} characters; limit is {MAX_PROMPT_CHARS:,}. '
            'Required task/objective text, acceptance criteria, instructions and recovery IDs '
            'were not truncated. Reduce required input or move supporting evidence to referenced '
            'SQLite records/files, then recheck the existing blocked task (or objective). '
            'Do not resubmit the objective or create a new session.')


def preview(value, size=400):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return text if len(text) <= size else text[:size] + f' [preview; full length {len(text)}]'


def names(values):
    values = sorted(set(values))
    return {'sample': [preview(v, 240) for v in values[:40]], 'total': len(values)}


def recovery_summary(db, obj, task):
    key = task['key']
    checkpoints = db.rows(
        "SELECT id,at,data FROM artifact WHERE owner=? AND kind='checkpoint' ORDER BY id DESC LIMIT 4", (key,))
    completed = db.rows(
        "SELECT id,turn_id,data FROM event WHERE owner=? AND kind='item/completed' ORDER BY id DESC LIMIT 20", (key,))
    failures = db.rows(
        "SELECT id,data FROM event WHERE owner=? AND kind='infrastructure_failure' ORDER BY id DESC LIMIT 3", (key,))
    turns = db.rows(
        'SELECT id,session,provider_turn,kind,status FROM turn WHERE owner=? ORDER BY id DESC LIMIT 1', (key,))
    provider_turn = db.rows(
        'SELECT id,session,provider_turn,status FROM turn WHERE owner=? AND provider_turn IS NOT NULL ORDER BY id DESC LIMIT 1', (key,))
    summary = {
        'objective': obj['id'], 'task': key, 'session': task['session'],
        'workspace': json.loads(task['spec'])['workspace'], 'database': str(db.path),
        'latest_turn': turns, 'latest_provider_turn': provider_turn,
        'infra_retries': task['infra_retries'],
        'event_counts': db.rows(
            "SELECT kind,count(*) AS records FROM event WHERE owner=? AND kind IN ('item/completed','infrastructure_failure') GROUP BY kind ORDER BY kind", (key,)),
        'checkpoint_count': db.rows(
            "SELECT count(*) AS records FROM artifact WHERE owner=? AND kind='checkpoint'", (key,))[0]['records'],
        'checkpoints': [], 'completed_operations': [], 'infrastructure_failures': [],
    }
    for row in checkpoints:
        data = json.loads(row['data'])
        # Status includes tracked changes still dirty across identical checkpoints.
        # `changed` alone is empty when no files changed between two snapshots.
        dirty = data.get('dirty_files')
        if dirty is None:
            dirty = [line[3:] for line in (data.get('status') or '').splitlines() if len(line) > 3]
        summary['checkpoints'].append({
            'artifact_id': row['id'], 'at': row['at'], 'stage': data.get('stage'),
            'revision': data.get('revision'), 'file_count': len(data.get('files', {})),
            'changed_files': names(data.get('changed', [])), 'dirty_files': names(dirty),
            'diff_chars': len(data.get('diff') or ''), 'full_evidence': f'artifact:{row["id"]}',
        })
    for row in completed:
        data = json.loads(row['data'])
        item = data.get('item', {})
        operation = {
            'event_id': row['id'], 'turn_id': row['turn_id'],
            'provider_turn': data.get('turnId'), 'item_id': item.get('id'),
            'type': item.get('type'), 'status': item.get('status'),
            'full_evidence': f'event:{row["id"]}', 'record_chars': len(row['data']),
        }
        for field in ('command', 'cwd', 'server', 'tool', 'name', 'exitCode', 'phase'):
            if field in item:
                operation[field] = preview(item[field])
        if item.get('changes'):
            operation['changed_files'] = names(c['path'] for c in item['changes'] if 'path' in c)
        # Outputs, diffs, arguments and messages may be megabytes or contain secrets.
        # Never copy these into a recovery prompt; the row and session retain them.
        summary['completed_operations'].append(operation)
    for row in failures:
        data = json.loads(row['data'])
        summary['infrastructure_failures'].append({
            'event_id': row['id'], 'retry': data.get('retry'),
            'error_preview': preview(data.get('error', '')),
            'full_evidence': f'event:{row["id"]}',
        })
    return (
        '\nYou were cut off or transport/supervisor crashed. Reconcile in this SAME session. '
        'Inspect files and prior completed tool operations. Avoid repeating completed side effects. '
        'Verify uncertain external operations against their actual destination before retrying them. '
        'Finish interrupted work, then run validators. Recovery summary (samples/previews only):\n'
        + json.dumps(summary, sort_keys=True) +
        '\nFull evidence is preserved in the original workspace and SQLite database above. '
        'Open SQLite read-only with sqlite3.connect(Path(database).as_uri()+"?mode=ro", uri=True). '
        'For artifact:N, use SELECT data FROM artifact WHERE id=? AND owner=?; '
        'for event:N, use SELECT data FROM event WHERE id=? AND owner=?. '
        'Bind N and the full task key. JSON-decode data; inspect checkpoint diff/files/status/changed '
        'and completed item command/arguments/result/aggregatedOutput/changes as needed. '
        'Page large values instead of dumping them. Enumerate older rows with WHERE owner=? ORDER BY id. '
        'Consult prior session turns and inspect the workspace with git status/diff; '
        'an empty checkpoint changed list does not mean the workspace is clean. '
        'Do not infer success or repeat external side effects from a summary alone.')
