"""Persistent planner benchmarks. Run --help for options; summary exports JSONL."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
DEFAULT_HISTORY = ROOT / 'history'


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, allow_nan=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def records(history):
    for path in sorted(history.glob('*/result.json')):
        try:
            yield json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            print(f'Cannot read {path}: {exc}', file=sys.stderr)


def comparison(current, previous):
    a, b = current['benchmark']['total'], previous['benchmark']['total']
    delta = a['search_score'] - b['search_score']
    old_time = b['total_search_seconds']
    new_time = a['total_search_seconds']
    return dict(previous_run_id=previous['run_id'], score_delta=delta,
                score_change_pct=100 * delta / abs(b['search_score']) if b['search_score'] else None,
                runtime_delta_seconds=new_time-old_time,
                runtime_reduction_pct=100*(old_time-new_time)/old_time if old_time else None,
                failures_delta=a['search_failures']-b['search_failures'])


def fingerprints(paths):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(paths))}


def run(args, bench):
    cases = bench.select_cases(args.cases, args.levels, args.seeds)
    if not 0 <= args.seconds <= 55 or not 0 < args.timeout <= 60 or args.workers < 1:
        raise ValueError('seconds must be 0..55, timeout >0 and <=60, workers >=1')
    planner = args.planner.resolve(strict=True)
    history = args.history.resolve()
    environment = dict(platform=platform.platform(), python=sys.version,
                       machine=platform.machine(), hostname=platform.node(), cpu_count=os.cpu_count())
    # Kit changes alter inputs/rules, so exclude them from like-for-like comparisons.
    kit_hashes = fingerprints(list((bench._kit.KIT / 'fleetkit').rglob('*.py')))
    settings = dict(cases=sorted(f'{level}:{seed}' for level, seed in cases),
                    seconds=args.seconds, timeout=args.timeout, workers=args.workers,
                    environment=environment, kit_sha256=kit_hashes)
    previous = [r for r in records(history) if r.get('settings') == settings
                and r.get('benchmark', {}).get('complete')
                and not r['benchmark']['total']['setup_failures']]
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '-' + uuid4().hex[:8]
    folder = history / run_id
    folder.mkdir(parents=True, exist_ok=False)
    source_paths = list(ROOT.glob('*.py')) + list((ROOT / 'analysis').glob('*.py')) + [planner]
    record = dict(schema_version=2, run_id=run_id, started_at=now(), status='running',
                  note=args.note, settings=settings, source_sha256=fingerprints(source_paths),
                  artifacts='benchmark', comparison=None)
    try:
        record['git'] = dict(commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL, timeout=5).strip(),
            status=subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT,
                text=True, stderr=subprocess.DEVNULL, timeout=5).strip())
    except (OSError, subprocess.SubprocessError):
        record['git'] = None
    command = [sys.executable, str(ROOT / 'analysis' / 'benchmark_search.py'),
               '--cases', *settings['cases'], '--seconds', str(args.seconds),
               '--timeout', str(args.timeout), '--workers', str(args.workers),
               '--planner', str(planner), '--output', str(folder / 'benchmark')]
    record['command'] = command
    save(folder / 'result.json', record)
    print(f'History: {folder}', flush=True)
    started = time.perf_counter()
    process = None
    code = 1
    try:
        # Inherit console output for live progress; per-planner logs live in benchmark/.
        process = subprocess.Popen(command, cwd=ROOT)
        code = process.wait()
        record['status'] = 'success' if code == 0 else 'failed'
    except KeyboardInterrupt:
        record['status'] = 'interrupted'
        code = 130
        if process is not None:
            # Let the bounded benchmark finish its current children and preserve artifacts.
            print('Interrupted; waiting for benchmark children to finish safely.', file=sys.stderr)
            process.wait()
    except OSError as exc:
        record.update(status='launch_error', error=str(exc))
    finally:
        record.update(finished_at=now(), elapsed_seconds=time.perf_counter()-started, exit_code=code)
        summary_path = folder / 'benchmark' / 'summary.json'
        if summary_path.exists():
            try:
                record['benchmark'] = json.loads(summary_path.read_text(encoding='utf-8'))
                result_path = folder / 'benchmark' / 'results.json'
                record['results'] = json.loads(result_path.read_text(encoding='utf-8'))
                if previous and record['benchmark']['complete'] and not record['benchmark']['total']['setup_failures']:
                    record['comparison'] = comparison(record, previous[-1])
            except (OSError, ValueError) as exc:
                record['collection_error'] = str(exc)
                code = 1
                record.update(status='failed', exit_code=code)
        save(folder / 'result.json', record)
    if record['comparison']:
        change = record['comparison']
        print(f"Versus {change['previous_run_id']}: score {change['score_delta']:+.2f}, "
              f"planner time {change['runtime_delta_seconds']:+.3f}s")
    print(f"Saved: {folder / 'result.json'}")
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['run', 'summary', 'contacts'], default='run')
    parser.add_argument('--history', type=Path, default=DEFAULT_HISTORY)
    parser.add_argument('--note', default='', help='Describe the optimisation being tested')
    parser.add_argument('--cases', nargs='+', help='e.g. L1:0 L4:0')
    parser.add_argument('--levels', help='default: L1,L2,L3,L4')
    parser.add_argument('--seeds', help='default: 0-2')
    parser.add_argument('--seconds', type=float, default=45)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--planner', type=Path, default=ROOT / 'planner.py')
    args = parser.parse_args(argv)
    if args.action == 'summary':
        for record in records(args.history):
            print(json.dumps(record, allow_nan=False))
        return 0
    sys.path.insert(0, str(ROOT / 'analysis'))
    import benchmark_search as bench
    if args.action == 'contacts':
        rows = [row for record in records(args.history) for row in record.get('results', [])]
        print(json.dumps(bench.contact_summary(rows), indent=2, allow_nan=False))
        return 0
    try:
        return run(args, bench)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    sys.exit(main())
