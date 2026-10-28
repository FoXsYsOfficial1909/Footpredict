#!/usr/bin/env python3
"""Bounded local job supervisor and timed report writer. No chat notifications."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
from zoneinfo import ZoneInfo


def utcnow():
    return datetime.now(timezone.utc)


def parsed(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def atomic_json(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w') as target:
            json.dump(data, target, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked_state(root):
    root = Path(root)
    with (root / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads((root / 'state.json').read_text())
        yield state
        atomic_json(root / 'state.json', state)


def stamp():
    return utcnow().isoformat()


def render_report(state, phase, now=None):
    now = now or utcnow()
    msk = now.astimezone(ZoneInfo('Europe/Moscow'))
    lines = [f'# Footpredict — отчёт {msk:%d.%m.%Y %H:%M} МСК', '',
             f'Статус отчёта: **{phase}**.', '',
             'Отчёт в 07:50 МСК сохраняется локальным планировщиком. Отложенная отправка в чат не настроена.', '',
             '## Выполненные действия', '']
    updates = state.get('updates', [])
    lines.extend('- ' + entry['message'] for entry in updates)
    if not updates:
        lines.append('- Подготовка начата; подтверждённые результаты ещё не внесены.')
    lines += ['', '## Проверки', '']
    for check in state.get('tests', []):
        lines.append(f"- {check.get('name', 'Проверка')}: **{check.get('status', 'unknown')}**. {check.get('evidence', '')}")
    if not state.get('tests'):
        lines.append('- Итоги текущего запуска ещё не зарегистрированы.')
    lines += ['', '## Фоновые задачи', '', '| Задача | Статус | Успешные запуски | Последний код выхода |',
              '|---|---|---:|---:|']
    for job in state.get('jobs', []):
        lines.append(f"| {job['id']} | {job.get('status', 'queued')} | {job.get('successes', 0)} | {job.get('returncode', '—')} |")
    lines += ['', '## Ограничения и незавершённое', '']
    lines.extend('- ' + text for text in state.get('blockers', []))
    if not state.get('blockers'):
        lines.append('- Ограничения пока не зарегистрированы. Это не является заявлением об отсутствии проблем.')
    lines += ['', '## Артефакты', '']
    lines.extend(f"- {item}" for item in state.get('artifacts', []))
    if state.get('monitor_summary'):
        lines += ['', '## Последний проход сбора данных', '', '```json',
                  json.dumps(state['monitor_summary'],ensure_ascii=False,indent=2),'```']
    if state.get('prospective_summary'):
        lines += ['', '## Новая серия прогнозов до матчей', '', '```json',
                  json.dumps(state['prospective_summary'], ensure_ascii=False, indent=2), '```']
    lines += ['', '## Границы работы', '',
              f"- Начало: {state['started_at']}; часовой пояс отчёта — Europe/Moscow.",
              f"- Отчёт: {state['report_at']}; остановка собственных фоновых процессов: {state['stop_at']}.",
              '- Рабочие секреты не включаются в отчёт. Сохранённая серия прогнозов v1 не переписывается.',
              '- Исходы испытаний отражаются как прошедшие, проваленные, пропущенные или ещё не выполненные.', '']
    return '\n'.join(lines)


def save_report(root, phase, filename):
    with locked_state(root) as state:
        body = render_report(state, phase)
    path = Path(root) / 'reports' / filename
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    with os.fdopen(fd, 'w') as target:
        target.write(body)
    os.replace(temporary, path)
    return path


def supervise(root):
    root = Path(root).resolve()
    lease = (root / 'supervisor.lock').open('a')
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    active = {}
    with locked_state(root) as state:
        state['supervisor_pid'] = os.getpid()
        state['status'] = 'active'
        already_reported = bool(state.get('report_written_at'))
    if not already_reported:
        save_report(root, 'Черновик: работа продолжается', 'REPORT-0750-MSK.md')
    while True:
        now = utcnow()
        with locked_state(root) as state:
            report_at, stop_at = parsed(state['report_at']), parsed(state['stop_at'])
            for job in state['jobs']:
                process_info = active.get(job['id'])
                if not process_info:
                    continue
                process, log, started = process_info
                expired = (now - started).total_seconds() > job.get('timeout_seconds', 1800)
                if (expired or now >= stop_at) and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                    job['status'] = 'stopped_at_deadline' if now >= stop_at else 'timed_out'
                code = process.poll()
                if code is not None:
                    log.close()
                    job['returncode'] = code
                    job['attempts'] = job.get('attempts', 0) + 1
                    job['finished_at'] = now.isoformat()
                    job['successes'] = job.get('successes', 0) + int(code == 0)
                    if job.get('status') == 'running':
                        job['status'] = 'passed' if code == 0 else 'failed'
                    interval = job.get('interval_seconds', 0)
                    can_repeat = not job.get('max_attempts') or job['attempts'] < job['max_attempts']
                    job['next_run_at'] = (now.timestamp() + interval) if interval and now < stop_at and can_repeat else None
                    del active[job['id']]
            if now < stop_at:
                for job in state['jobs']:
                    if len(active) >= 2:
                        break
                    due = job.get('next_run_at')
                    if job['id'] in active or due is None or now.timestamp() < due:
                        continue
                    if (stop_at - now).total_seconds() < state.get('start_margin_seconds', 60):
                        continue
                    command = job['command']
                    if not isinstance(command, list) or not command or any(not isinstance(v, str) for v in command):
                        job.update(status='invalid_command', next_run_at=None)
                        continue
                    log = (root / 'logs' / (job['id'] + '.log')).open('a')
                    environment = os.environ.copy()
                    environment.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
                    process = subprocess.Popen(command, cwd=state['project'], env=environment,
                                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    active[job['id']] = (process, log, now)
                    job.update(status='running', pid=process.pid, started_at=now.isoformat(), next_run_at=None)
            report_due = now >= report_at and not state.get('report_written_at')
            if report_due:
                state['report_written_at'] = now.isoformat()
            stopping = now >= stop_at
            if stopping:
                state['status'] = 'stopped'
                state['stopped_at'] = now.isoformat()
        if report_due:
            save_report(root, 'Срез на 07:50 МСК', 'REPORT-0750-MSK.md')
        if stopping:
            save_report(root, 'Рабочие процессы остановлены к 08:00 МСК', 'FINAL-0800-MSK.md')
            return
        seconds_to_event = min((report_at - now).total_seconds() if now < report_at else 30,
                               (stop_at - now).total_seconds())
        time.sleep(max(0.25, min(10, seconds_to_event)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('supervise')
    note = sub.add_parser('note')
    note.add_argument('message')
    test = sub.add_parser('test')
    test.add_argument('name')
    test.add_argument('status', choices=['passed', 'failed', 'skipped', 'unrun'])
    test.add_argument('evidence')
    block = sub.add_parser('blocker')
    block.add_argument('message')
    artifact = sub.add_parser('artifact')
    artifact.add_argument('path')
    job = sub.add_parser('job')
    job.add_argument('id')
    job.add_argument('--interval', type=int, default=0)
    job.add_argument('--timeout', type=int, default=1800)
    job.add_argument('--start-at', help='Timezone-aware ISO time; defaults to now')
    job.add_argument('--max-attempts', type=int, default=0, help='Zero repeats until the session deadline')
    job.add_argument('command', nargs=argparse.REMAINDER)
    sub.add_parser('draft')
    args = parser.parse_args()
    if args.action == 'supervise':
        supervise(args.run_dir)
        return
    if args.action == 'draft':
        print(save_report(args.run_dir, 'Черновик: работа продолжается', 'PROGRESS.md'))
        return
    with locked_state(args.run_dir) as state:
        if args.action == 'note':
            state['updates'].append({'at': stamp(), 'message': args.message})
        elif args.action == 'test':
            state['tests'].append({'at': stamp(), 'name': args.name, 'status': args.status, 'evidence': args.evidence})
        elif args.action == 'blocker':
            if args.message not in state['blockers']:
                state['blockers'].append(args.message)
        elif args.action == 'artifact':
            if args.path not in state.setdefault('artifacts', []):
                state['artifacts'].append(args.path)
        elif args.action == 'job':
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', args.id):
                raise SystemExit('Job ID must be a safe log filename')
            if args.timeout <= 0 or args.interval < 0 or args.max_attempts < 0:
                raise SystemExit('Timeout must be positive; interval and attempt limit cannot be negative')
            if any(job['id'] == args.id for job in state['jobs']):
                raise SystemExit('Job ID already registered')
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            if not command:
                raise SystemExit('A command is required')
            due = parsed(args.start_at).timestamp() if args.start_at else time.time()
            if args.start_at and parsed(args.start_at).tzinfo is None:
                raise SystemExit('Start time must include a timezone')
            if due >= parsed(state['stop_at']).timestamp():
                raise SystemExit('The job would start after the session deadline')
            state['jobs'].append({'id': args.id, 'command': command, 'status': 'queued', 'next_run_at': due,
                                  'timeout_seconds': args.timeout, 'interval_seconds': args.interval,
                                  'max_attempts': args.max_attempts})


if __name__ == '__main__':
    main()
