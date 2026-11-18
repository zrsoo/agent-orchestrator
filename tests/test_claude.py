"""Claude Code adapter over a real stream-json subprocess fixture."""
import json
from pathlib import Path
import sys
import time
import pytest
import yaml
from agent_orchestrator.backends import claude
from agent_orchestrator.backends.base import InputSizeError, TransportError
from agent_orchestrator.backends.claude import ClaudeBackend
from agent_orchestrator.cli import main
from agent_orchestrator.database import Database
from agent_orchestrator.quota import admission
from agent_orchestrator.scheduler import Scheduler
from polling import complete_objective

FIXTURE = Path(__file__).parent / 'fixtures' / 'claude_cli.py'
PINNED = 'claude-opus-5-5'


def config(**extra):
    return {'executable': [sys.executable, str(FIXTURE)], 'model': PINNED, 'effort': 'high',
            'execution_mode': 'yolo', **extra}


def invocations(workspace):
    return [json.loads(line) for line in (workspace / 'claude-invocations.jsonl').read_text().splitlines()]


def run(backend, session, prompt='implement', control=lambda: None):
    events = []
    outcome = backend.run(session, prompt, lambda *e: events.append(e), control)
    return outcome, events


def test_wire_session_resume_pinning_events_and_quota(tmp_path, monkeypatch):
    for name, value in (('CLAUDECODE', '1'), ('CLAUDE_CODE_ENTRYPOINT', 'host'), ('CLAUDE_EFFORT', 'low'),
                        ('ANTHROPIC_API_KEY', 'host-key'), ('ANTHROPIC_BASE_URL', 'http://proxy'),
                        ('AI_AGENT', 'host'), ('CLAUDE_CONFIG_DIR', str(tmp_path / 'cfg'))):
        monkeypatch.setenv(name, value)
    backend = ClaudeBackend(config(env={'FIXTURE_EXTRA': 'x'}))
    try:
        assert backend.quota().buckets == []
        session = backend.session(str(tmp_path))
        outcome, events = run(backend, session)
        assert outcome.status == 'completed'
        assert json.loads(outcome.text.strip('`').removeprefix('json'))['status'] == 'done'
        kinds = [e[0] for e in events]
        assert kinds.index('claude/init') < kinds.index('turn_identity') < kinds.index('claude/result')
        init = next(e[1] for e in events if e[0] == 'claude/init')
        assert init['model'] == PINNED and init['session_id'] == session
        items = [e[1]['item'] for e in events if e[0] == 'item/completed']
        tool = next(i for i in items if i['type'] == 'toolCall')
        assert tool['tool'] == 'Bash' and tool['command'] == 'write output.txt' and tool['status'] == 'completed'
        assert any(i['type'] == 'agentMessage' and i['subagent'] == 'toolu_9' for i in items)
        five, week = backend.quota().buckets
        assert (five.name, five.window_minutes, week.name, week.window_minutes) == (
            'claude/five_hour', 300, 'claude/seven_day', 10080)
        assert abs(five.remaining - 90) < 1e-9 and abs(week.remaining - 75) < 1e-9
        assert admission(backend.latest_quota)
        result = next(e[1] for e in events if e[0] == 'claude/result')
        assert result['observed_effort'] == ['high']

        assert backend.session(str(tmp_path), session) == session
        assert run(backend, session)[0].status == 'completed'
        first, second = invocations(tmp_path)
        assert first['argv'][first['argv'].index('--session-id') + 1] == session
        assert second['argv'][second['argv'].index('--resume') + 1] == session
        for argv in (first['argv'], second['argv']):
            assert argv[argv.index('--model') + 1] == PINNED
            assert argv[argv.index('--effort') + 1] == 'high'
            assert '--dangerously-skip-permissions' in argv and '-p' in argv
        assert first['env'] == {'CLAUDE_CONFIG_DIR': str(tmp_path / 'cfg'),
                                'CLAUDE_CODE_SUBAGENT_MODEL': PINNED, 'FIXTURE_EXTRA': 'x'}
        assert (tmp_path / 'last-prompt.txt').read_text(encoding='utf-8') == 'implement'
    finally:
        backend.close()


def test_session_flag_recovers_after_supervisor_crash(tmp_path):
    first = ClaudeBackend(config())
    session = first.session(str(tmp_path))  # Persisted by the scheduler, never sent before a crash.
    first.close()
    second = ClaudeBackend(config())
    assert run(second, second.session(str(tmp_path), session))[0].status == 'completed'
    # A new process can believe an existing session is fresh; it must resume instead.
    third = ClaudeBackend(config())
    third.session(str(tmp_path), session)
    third.fresh.add(session)
    assert run(third, session)[0].status == 'completed'
    flags = [next(f for f in ('--session-id', '--resume') if f in i['argv']) for i in invocations(tmp_path)]
    assert flags == ['--resume', '--session-id', '--session-id', '--resume']


def test_rejected_limit_blocks_until_reset_then_unknown(tmp_path):
    backend = ClaudeBackend(config())
    try:
        session = backend.session(str(tmp_path))
        reset = time.time() + .5
        (tmp_path / 'quota-once').write_text(str(reset))
        outcome, _ = run(backend, session)
        assert outcome.status == 'quota' and 'usage limit' in outcome.diagnostic
        quota = backend.quota()
        week = next(b for b in quota.buckets if b.name == 'claude/seven_day')
        five = next(b for b in quota.buckets if b.name == 'claude/five_hour')
        assert week.remaining == 0 and week.resets_at == reset and week.minimum == 0
        assert abs(five.remaining - 50) < 1e-9
        assert not admission(quota)
        time.sleep(.6)
        assert next(b for b in backend.quota().buckets if b.name == 'claude/seven_day').remaining is None
        assert admission(backend.latest_quota)
        (tmp_path / 'limit-text').touch()
        assert run(backend, session)[0].status == 'quota'
        plan = next(b for b in backend.quota().buckets if b.name == 'claude/plan')
        assert plan.remaining == 0 and plan.resets_at > time.time()
    finally:
        backend.close()


def test_overage_is_interrupted_and_refused(tmp_path):
    backend = ClaudeBackend(config())
    try:
        (tmp_path / 'overage').touch()
        outcome, events = run(backend, backend.session(str(tmp_path)))
        assert outcome.status == 'quota' and 'overage refused' in outcome.diagnostic
        assert any(e[0] == 'claude/control_response' for e in events)
        assert not admission(backend.quota())
    finally:
        backend.close()


def test_stop_control_interrupts_turn(tmp_path):
    backend = ClaudeBackend(config())
    (tmp_path / 'hold').touch()
    calls = []
    def control():
        calls.append(1)
        return 'stop' if (tmp_path / 'output.txt').exists() else None
    try:
        outcome, events = run(backend, backend.session(str(tmp_path)), control=control)
        assert outcome.status == 'interrupted'
        assert any(e[0] == 'claude/control_response' for e in events)
        assert backend.proc is None
    finally:
        backend.close()


def test_unacknowledged_interrupt_and_timeout_close_process(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, 'EXIT_SECONDS', .5)
    (tmp_path / 'hold').touch()
    (tmp_path / 'ignore-interrupt').touch()
    backend = ClaudeBackend(config())
    try:
        outcome, _ = run(backend, backend.session(str(tmp_path)), control=lambda: 'stop')
        assert outcome.status == 'interrupted' and 'not acknowledged' in outcome.diagnostic
        assert backend.proc is None
        backend.config['turn_timeout'] = 1
        with pytest.raises(TransportError, match='turn timeout'):
            run(backend, backend.session(str(tmp_path)))
        assert backend.proc is None
    finally:
        backend.close()


@pytest.mark.parametrize('marker,error,match', [
    ('too-long', InputSizeError, 'too long'),
    ('overloaded', TransportError, 'unavailable'),
    ('crash', TransportError, 'without a result'),
    ('wrong-model', ValueError, 'pins claude-opus-5-5'),
    ('wrong-effort', ValueError, 'pins high'),
])
def test_error_classification(tmp_path, marker, error, match):
    (tmp_path / marker).touch()
    backend = ClaudeBackend(config())
    try:
        with pytest.raises(error, match=match):
            run(backend, backend.session(str(tmp_path)))
        assert backend.proc is None
    finally:
        backend.close()


@pytest.mark.parametrize('marker,observed', [('no-stream-effort', ['high']), ('transcript-wrong-effort', None)])
def test_transcript_verifies_effort_when_stream_omits_it(tmp_path, marker, observed):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'no-stream-effort').touch()
    (workspace / marker).touch()
    backend = ClaudeBackend(config(env={'CLAUDE_CONFIG_DIR': str(tmp_path / 'cfg')}))
    try:
        session = backend.session(str(workspace))
        if observed is None:
            with pytest.raises(ValueError, match='effort medium; objective pins high'):
                run(backend, session)
            return
        outcome, events = run(backend, session)
        assert outcome.status == 'completed'
        assert next(e[1] for e in events if e[0] == 'claude/result')['observed_effort'] == observed
        # Only this turn's entries are read on resume.
        outcome, events = run(backend, session)
        assert next(e[1] for e in events if e[0] == 'claude/result')['observed_effort'] == observed
    finally:
        backend.close()


def test_cli_permission_request_is_answered_not_hung(tmp_path):
    (tmp_path / 'permission-request').touch()
    backend = ClaudeBackend(config())
    try:
        assert run(backend, backend.session(str(tmp_path)))[0].status == 'completed'
        reply = json.loads((tmp_path / 'control-reply.json').read_text())
        assert reply['response']['subtype'] == 'error' and reply['response']['request_id'] == 'cli-1'
    finally:
        backend.close()


def test_missing_executable_and_invalid_config(monkeypatch):
    monkeypatch.setattr(claude.shutil, 'which', lambda name: None)
    with pytest.raises(TransportError, match='not found'):
        ClaudeBackend({})
    for bad in ({'effort': 'extreme'}, {'execution_mode': 'sandbox'}, {'provider_mode': 1}, {'env': {'A': 1}}):
        with pytest.raises(ValueError):
            claude.check_config(bad)


def test_scheduler_objective_quota_wait_same_session(tmp_path):
    db = Database(tmp_path / 'state.db')
    key = db.submit('write file', tmp_path, 'claude', config())
    scheduler = Scheduler(db)
    try:
        scheduler.tick()
        assert db.get('objective', key)['session']
        reset = time.time() + 5
        (tmp_path / 'quota-once').write_text(str(reset))
        scheduler.tick()
        task = db.get('task', key + '/task')
        assert task['state'] == 'WAITING_QUOTA' and task['attempts'] == 0
        scheduler.tick()  # The rejected bucket is still in force: no provider process starts.
        assert db.get('task', key + '/task')['state'] == 'WAITING_QUOTA'
        assert len(invocations(tmp_path)) == 2
        time.sleep(max(0, reset - time.time()) + .1)
        complete_objective(scheduler, db, key, timeout=60)
        assert db.get('task', key + '/task')['session'] == task['session']
        assert (tmp_path / 'output.txt').read_text() == 'done'
        assert db.rows("SELECT * FROM event WHERE kind='item/completed' AND owner=?", (key + '/task',))
        assert db.rows("SELECT * FROM turn WHERE owner=? AND provider_turn IS NOT NULL", (key + '/task',))
    finally:
        scheduler.close()
        db.close()


def test_cli_pins_claude_defaults_and_rejects_unknown_provider(tmp_path, capsys):
    db_path, conf = tmp_path / 'state.db', tmp_path / 'factory.yaml'
    conf.write_text(yaml.safe_dump({'provider': 'claude'}))
    assert main(['--db', str(db_path), '--config', str(conf), 'submit', '--text', 'x', '--workspace', str(tmp_path)]) == 0
    conf.write_text(yaml.safe_dump({'provider': 'claude', 'providers': {'claude': {'model': 'sonnet'}}}))
    assert main(['--db', str(db_path), '--config', str(conf), 'submit', '--text', 'x', '--workspace', str(tmp_path)]) == 0
    for bad in ({'provider': 'claude', 'providers': {'claude': {'effort': 'extreme'}}}, {'provider': 'other'}):
        conf.write_text(yaml.safe_dump(bad))
        assert main(['--db', str(db_path), '--config', str(conf), 'submit', '--text', 'x', '--workspace', str(tmp_path)]) == 2
    db = Database(db_path)
    try:
        rows = db.rows('SELECT provider,config FROM objective ORDER BY rowid')
        assert [(r['provider'], json.loads(r['config'])) for r in rows] == [
            ('claude', {'model': PINNED, 'effort': 'high', 'execution_mode': 'yolo'}),
            ('claude', {'model': 'sonnet'})]
    finally:
        db.close()
