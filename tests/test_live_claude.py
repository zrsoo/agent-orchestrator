"""Opt-in real Claude Code inference. Evidence remains in ORCHESTRATOR_LIVE_ROOT."""
import json
import os
from pathlib import Path
import uuid
import pytest
from agent_orchestrator.backends.claude import ClaudeBackend
from agent_orchestrator.cli import DEFAULT_PROVIDERS
from agent_orchestrator.database import Database
from agent_orchestrator.scheduler import Scheduler
from polling import complete_objective

pytestmark = [pytest.mark.live, pytest.mark.skipif(
    os.getenv('ORCHESTRATOR_LIVE_CLAUDE') != '1', reason='set ORCHESTRATOR_LIVE_CLAUDE=1 to use Claude quota')]


def test_live_claude_objective_pinned_model_and_session_resume():
    path = Path(os.getenv('ORCHESTRATOR_LIVE_ROOT', '.m1-progress/live-acceptance')).resolve() / ('claude-' + uuid.uuid4().hex[:8])
    workspace = path / 'workspace'
    workspace.mkdir(parents=True)
    config = DEFAULT_PROVIDERS['claude']
    db = Database(path / 'state.db')
    scheduler = Scheduler(db)
    try:
        key = db.submit('Use exactly one task: create message.txt containing exactly "hello claude" followed by a '
                        'newline, with an independent Python artifact check.', workspace, 'claude', config)
        complete_objective(scheduler, db, key, timeout=1800)
        assert (workspace / 'message.txt').read_bytes().replace(b'\r\n', b'\n') == b'hello claude\n'
        inits = [json.loads(r['data']) for r in db.rows("SELECT data FROM event WHERE kind='claude/init'")]
        assert inits and all(i['model'] == config['model'] for i in inits), inits
        assert all(i.get('permissionMode') == 'bypassPermissions' for i in inits), inits
        results = [json.loads(r['data']) for r in db.rows("SELECT data FROM event WHERE kind='claude/result'")]
        assert results and all(r['observed_effort'] == [config['effort']] for r in results), results
        task = db.rows('SELECT * FROM task WHERE objective_id=?', (key,))[0]
        (path / 'inits.json').write_text(json.dumps(inits, indent=2))
        (path / 'rate_limits.json').write_text(json.dumps(
            [json.loads(r['data']) for r in db.rows("SELECT data FROM event WHERE kind='claude/rate_limit'")], indent=2))
    finally:
        scheduler.close()
        db.close()
    # A new process resumes the saved worker session with its context.
    backend = ClaudeBackend(config)
    try:
        session = backend.session(str(workspace), task['session'])
        outcome = backend.run(session, 'Reply with only the file name you created in this session.', lambda *e: None, lambda: None)
        assert outcome.status == 'completed' and 'message.txt' in outcome.text, outcome
    finally:
        backend.close()
