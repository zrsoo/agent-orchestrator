"""m1-04 acceptance entry: fresh pytest process, isolated fixtures, exact evidence."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get('ORCHESTRATOR_M1_EVIDENCE', str(ROOT / 'reports/m1/integrated-cli'))).resolve()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    paths = [*sorted((ROOT / 'src').rglob('*.py')), *sorted((ROOT / 'tests').rglob('*.py')), Path(__file__).resolve(),
             ROOT / 'pyproject.toml']
    return {str(path.relative_to(ROOT)).replace('\\', '/'): digest(path) for path in paths
            if '__pycache__' not in path.parts}


def validate(record, cases):
    assert record['returncode'] == 0, 'regressions failed; see pytest.log'
    assert record['source_sha256'] == source_hashes(), 'sources changed while tests were running'
    assert not [case for case in cases if case.find('failure') is not None or case.find('error') is not None]
    passed = {case.attrib['name'] for case in cases if case.find('skipped') is None}
    assert record['passed'] >= 180, 'regression suite was not fully collected'
    # Fail if key acceptance tests disappear or are skipped rather than passing.
    required = {
        'test_fresh_planning_long_worker_validation_quota_and_provider_contract',
        'test_fresh_recheck_failure_queue_survives_and_once_recovers_original_session',
        'test_real_recheck_start_ack_is_async_and_keeps_original_work',
        'test_concurrent_fresh_rechecks_report_one_actual_owner',
        'test_crashed_owner_stale_record_replaced_on_next_recheck',
        'test_detached_service_and_abrupt_python_restart',
        'test_validator_coverage_timeout_and_missing',
        'test_refresh_during_blocked_planning_worker_validation_and_quota_wait',
        'test_fresh_cli_once_status_json_output_and_lock_compatibility',
    }
    if os.name == 'nt':
        required |= {'test_new_windows_console_is_visible_and_contains_dashboard',
                     'test_windows_real_ctrl_c_restores_cursor_buffer_threads_and_ownership'}
    assert required <= passed, f'missing acceptance checks: {required - passed}'
    allowed_skips = {'test_existing_incident_session_and_previous_turns_read_only'}
    if os.name != 'nt':
        allowed_skips |= {'test_new_windows_console_is_visible_and_contains_dashboard',
                         'test_windows_real_ctrl_c_restores_cursor_buffer_threads_and_ownership'}
    assert set(record['skipped']) <= allowed_skips, record['skipped']
    for filename, sha in record['evidence_sha256'].items():
        assert digest(OUT / filename) == sha, filename
    facts = json.loads((OUT / 'cli/status.json').read_text(encoding='utf-8'))
    assert facts['objective'][0]['state'] == 'COMPLETED'
    assert all(task['state'] == 'COMPLETED' for task in facts['task'])
    assert len(facts['task']) == len(facts['validator']) == 2
    assert (OUT / 'cli/effects.txt').read_text().splitlines() == ['a', 'b']
    failure = json.loads((OUT / 'startup-failure/status.json').read_text(encoding='utf-8'))
    assert failure['failure']['returncode'] == 2 and 'unresponsive' in failure['failure']['stderr']
    assert failure['queued']['task'][1]['recheck'] == 'verify'
    assert failure['recovered']['objective'][0]['state'] == 'COMPLETED'
    if os.name == 'nt':
        terminal = json.loads((OUT / 'windows-terminal/terminal-result.json').read_text(encoding='utf-8'))
        assert terminal['returncode'] == 130 and terminal['isatty'] and terminal['cursor_visible']
        assert 'M1 original console buffer' in terminal['screen']


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)  # Require the installed standalone distribution.
    env['ORCHESTRATOR_M1_EVIDENCE'] = str(OUT)
    # This deterministic gate never uses the production incident DB or live quota.
    env.pop('ORCHESTRATOR_RECOVERY_DB', None)
    env.pop('ORCHESTRATOR_LIVE', None)
    command = [sys.executable, '-m', 'pytest', 'tests', '-q', '-rs',
               '-m', 'not live', '--junitxml', str(OUT / 'pytest.xml')]
    record = {'task': 'm1-04', 'utc': datetime.now(timezone.utc).isoformat(),
              'platform': platform.platform(), 'python': sys.version, 'command': command,
              'cwd': str(ROOT), 'env': {name: env[name] for name in ('ORCHESTRATOR_M1_EVIDENCE',)},
              'source_sha256': source_hashes()}
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', timeout=260)
    (OUT / 'pytest.log').write_text(result.stdout, encoding='utf-8')
    print(result.stdout, end='')
    record.update(returncode=result.returncode, elapsed_seconds=round(time.monotonic() - started, 3))
    cases = list(ET.parse(OUT / 'pytest.xml').getroot().iter('testcase'))
    record['passed'] = sum(all(case.find(tag) is None for tag in ('failure', 'error', 'skipped')) for case in cases)
    record['skipped'] = {case.attrib['name']: case.find('skipped').attrib.get('message', '')
                         for case in cases if case.find('skipped') is not None}
    record['host_checks'] = {
        'real_visible_windows_console': 'passed' if os.name == 'nt' and any(
            c.attrib['name'] == 'test_new_windows_console_is_visible_and_contains_dashboard'
            and all(c.find(t) is None for t in ('failure', 'error', 'skipped')) for c in cases) else 'not_run',
        'real_windows_ctrl_c_terminal_restoration': 'passed' if os.name == 'nt' and any(
            c.attrib['name'] == 'test_windows_real_ctrl_c_restores_cursor_buffer_threads_and_ownership'
            and all(c.find(t) is None for t in ('failure', 'error', 'skipped')) for c in cases) else 'not_run',
        'human_visual_review_and_windows_terminal_resize': 'not_run; no human/UI inspection',
        'live_codex_and_optional_production_incident_replay': 'not_run; deterministic fixture gate',
    }
    record['evidence_sha256'] = {str(p.relative_to(OUT)).replace('\\', '/'): digest(p)
                                  for p in sorted(OUT.rglob('*')) if p.is_file()
                                  and p.name != 'results.json'}
    record['status'] = 'unresolved'
    try:
        validate(record, cases)
        record['status'] = 'done'
    finally:
        (OUT / 'results.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(f"m1-04: {record['passed']} passed; evidence: {OUT / 'results.json'}")
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
