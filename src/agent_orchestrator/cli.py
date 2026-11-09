"""Submission and management require no live provider process."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import yaml
from .database import Database
from .dashboard import Dashboard
from .scheduler import Scheduler
from .quota import DEFAULT_POLL_SECONDS
from .service import OwnerProbe, ServiceLock, StartupError, ensure_running


def parser():
    p = argparse.ArgumentParser(prog='orchestrator')
    p.add_argument('--db', default='.orchestrator/state.db')
    p.add_argument('--config', default='factory.yaml')
    sub = p.add_subparsers(dest='command', required=True)
    submit = sub.add_parser('submit')
    submit.add_argument('file', nargs='?')
    submit.add_argument('--text')
    submit.add_argument('--workspace', default=str(Path.cwd()))
    run = sub.add_parser('run')
    mode = run.add_mutually_exclusive_group()
    mode.add_argument('--detach', action='store_true')
    mode.add_argument('--once', action='store_true')
    run.add_argument('--poll', type=float, default=DEFAULT_POLL_SECONDS)
    run.add_argument('--startup-nonce', help=argparse.SUPPRESS)
    status = sub.add_parser('status')
    status.add_argument('--json', action='store_true')
    status.add_argument('--output')
    for cmd in ('pause', 'resume', 'stop', 'recheck'):
        sub.add_parser(cmd).add_argument('id')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    db = Database(args.db)
    scheduler = None
    try:
        if args.command == 'submit':
            if bool(args.file) == bool(args.text):
                raise ValueError('supply exactly one objective file or --text')
            text = Path(args.file).read_text(encoding='utf-8-sig') if args.file else args.text
            if not text.strip():
                raise ValueError('objective is empty')
            path = Path(args.config)
            config = yaml.safe_load(path.read_text(encoding='utf-8-sig')) if path.exists() else {'provider': 'codex', 'providers': {'codex': {'model': 'default', 'execution_mode': 'yolo'}}}
            provider = config['provider']
            if provider != 'codex':
                raise ValueError('only codex is installed; other adapters can be registered through Scheduler factories')
            workspace = Path(args.workspace).resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            print(db.submit(text, workspace, provider, config['providers'][provider]))
        elif args.command == 'recheck':
            db.recheck(args.id)  # Commit before discovery/spawn; failures retain this request.
            try:
                owner = ensure_running(db.path)
            except (StartupError, OSError) as exc:
                print(f'Recheck queued, but scheduler startup failed: {exc}', file=sys.stderr)
                return 2
            if owner.started:
                suffix = '; dashboard opened.' if os.name == 'nt' else '.'
                print(f'Recheck queued; scheduler started (PID {owner.pid}){suffix}')
            else:
                print(f'Recheck queued; scheduler already running (PID {owner.pid}).')
        elif args.command in ('pause', 'resume', 'stop'):
            db.control(args.id, args.command)
            print(f'{args.id}: {db.get("task" if "/" in args.id else "objective", args.id)["state"]}')
        elif args.command == 'status':
            report = db.report()
            if args.json:
                output = json.dumps(report, indent=2)
            else:
                lines = []
                for obj in report['objective']:
                    lines.append(f'{obj["id"]} {obj["state"]} provider={obj["provider"]} {obj["diagnostic"]}')
                    for task in report['task']:
                        if task['objective_id'] == obj['id']:
                            lines.append(f'  {task["key"]} {task["state"]} attempts={task["attempts"]} infra={task["infra_retries"]} {task["diagnostic"]}')
                for turn in report['turn']:
                    duration = (turn['ended'] or time.time()) - turn['started']
                    lines.append(f'turn {turn["id"]} {turn["owner"]} {turn["status"]} {duration:.1f}s session={turn["session"]} provider_turn={turn["provider_turn"]}')
                for dep in report['dependency']:
                    lines.append(f'{dep["task_key"]} depends_on {dep["depends_key"]}')
                for event in report['event']:
                    if event['kind'] in ('quota', 'validation', 'thread/tokenUsage/updated'):
                        lines.append(f'{event["owner"]} {event["kind"]}: {event["data"]}')
                output = '\n'.join(lines) or 'No objectives'
            if args.output:
                Path(args.output).write_text(output, encoding='utf-8')
            else:
                print(output)
        elif args.command == 'run':
            if args.poll <= 0:
                raise ValueError('--poll must be positive')
            if args.detach:
                log = db.path.parent / 'service.log'
                owner = ensure_running(db.path, poll=args.poll, detached=True)
                if not owner.started:
                    raise ValueError(f'another orchestrator service owns this database (PID {owner.pid})')
                print(f'pid={owner.pid} log={log}')
                return 0
            with ServiceLock(db.path) as lock:
                if args.once:
                    scheduler = Scheduler(db)
                    try:
                        scheduler.tick()
                    finally:
                        closing, scheduler = scheduler, None
                        closing.close()
                else:
                    with Dashboard(db.path) as display:
                        scheduler = Scheduler(db, output=display.notice)
                        try:
                            with OwnerProbe(db.path, lock, args.startup_nonce):
                                while True:
                                    scheduler.tick()
                                    scheduler.wait(args.poll)
                        finally:
                            closing, scheduler = scheduler, None
                            closing.close()  # Own the DB until all provider trees are closed.
        return 0
    except (ValueError, KeyError, IndexError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            if scheduler:
                scheduler.close()
        finally:
            db.close()
