"""Batch comparison against greedy, using the official generator and scorer.

python3 analysis/benchmark_search.py --levels L1,L2,L3,L4 --seeds 0-4 --workers 4
python3 analysis/benchmark_search.py --seconds 5 --cases L1:0 L2:7 L3:1 L4:0

Parallel workers share CPU; use --workers 1 for representative timing/quality.
Scenario generation and external scoring are outside each planner's wall time.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import csv
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

import _kit
from fleetkit.generator import generate
from fleetkit.model import Scenario, load_json, save_json
from fleetkit.scorer import evaluate

ROOT = Path(__file__).resolve().parents[1]
LEVELS = ('L1', 'L2', 'L3', 'L4')
# Run greedy in its own bounded process, just like the submitted planner.
BASELINE_CODE = '''import sys
from fleetkit.baseline import greedy_plan
from fleetkit.model import Scenario, load_json, save_json
save_json(greedy_plan(Scenario.from_dict(load_json(sys.argv[1]))), sys.argv[2])
'''


def parse_seeds(text):
    seeds = []
    for item in text.split(','):
        match = re.fullmatch(r'\s*(-?\d+)(?:-(-?\d+))?\s*', item)
        if not match:
            raise ValueError(f'invalid seed or range: {item!r}')
        lo = int(match[1])
        hi = int(match[2]) if match[2] is not None else lo
        if hi < lo:
            raise ValueError(f'descending seed range: {item!r}')
        if hi-lo > 10000:
            raise ValueError('a seed range may contain at most 10001 seeds')
        seeds.extend(range(lo, hi+1))
    return list(dict.fromkeys(seeds))


def select_cases(cases=None, levels=None, seeds=None):
    if cases and (levels is not None or seeds is not None):
        raise ValueError('use either --cases or --levels/--seeds')
    if cases:
        selected = []
        for case in cases:
            parts = case.split(':')
            if len(parts) != 2 or parts[0] not in LEVELS:
                raise ValueError(f'invalid case {case!r}; expected LEVEL:SEED, e.g. L2:7')
            try:
                seed = int(parts[1])
            except ValueError:
                raise ValueError(f'invalid seed in case {case!r}') from None
            selected.append((parts[0], seed))
    else:
        chosen_levels = (levels or ','.join(LEVELS)).split(',')
        if not chosen_levels or any(level not in LEVELS for level in chosen_levels):
            raise ValueError('levels must be a comma-separated selection of L1,L2,L3,L4')
        selected = [(level, seed) for level in chosen_levels for seed in parse_seeds(seeds or '0-2')]
    return list(dict.fromkeys(selected))


def run_planner(command, scenario, plan_path, log_path, timeout, env):
    start = time.monotonic()
    status, note, returncode = 'ok', '', None
    with log_path.open('w', encoding='utf-8') as log:
        try:
            proc = subprocess.run(command, cwd=plan_path.parent, env=env,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            returncode = proc.returncode
            if returncode:
                status, note = 'crash', f'exit code {returncode}; see {log_path.name}'
        except subprocess.TimeoutExpired:
            status, note = 'timeout', f'exceeded {timeout:g} seconds'
        except OSError as exc:
            status, note = 'crash', str(exc)
    elapsed = time.monotonic()-start
    score, served = 0.0, []
    if status == 'ok':
        if not plan_path.exists():
            status, note = 'no_plan', 'no output plan was written'
        else:
            try:
                plan = load_json(plan_path)
            except (ValueError, OSError) as exc:
                status, note = 'bad_json', str(exc)
            else:
                try:
                    result = evaluate(scenario, plan)
                    if not result.valid:
                        status = 'invalid'
                        note = ' | '.join(str(v) for v in result.violations[:3])
                    elif not math.isfinite(result.score):
                        status, note = 'invalid', 'non-finite score'
                    else:
                        score, served = result.score, result.served
                except Exception as exc:
                    status, note = 'invalid', f'scorer rejected output: {exc}'
    return dict(status=status, score=score, seconds=elapsed, served=served,
                note=note, returncode=returncode)


DIAGNOSTIC_KEYS = ('passes', 'candidate_checks', 'rejections', 'unserved', 'unserved_points')


def read_diagnostics(log_path):
    """The planner's final stderr JSON line: passes, rejections, unserved reasons."""
    try:
        lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        if line.startswith('{'):
            try:
                data = json.loads(line)
            except ValueError:
                return {}
            return {key: data[key] for key in DIAGNOSTIC_KEYS if key in data}
    return {}


def empty_result():
    return dict(status='not_run', score=0.0, seconds=0.0, served=[], note='', returncode=None)


def run_case(level, seed, output, seconds, timeout, planner_path):
    folder = output / f'{level}-{seed}'
    folder.mkdir()
    base, search = empty_result(), empty_result()
    row = dict(case=f'{level}:{seed}', level=level, seed=seed, folder=folder.name,
               generation_status='ok', generation_seconds=0.0, generation_note='')
    try:
        start = time.monotonic()
        data = generate(level, seed)
        row['generation_seconds'] = time.monotonic()-start
        scenario = Scenario.from_dict(data)
        scenario_path = folder / 'scenario.json'
        save_json(data, scenario_path)
        env = dict(os.environ, PYTHONPATH=str(_kit.KIT), PYTHONDONTWRITEBYTECODE='1')
        base = run_planner([sys.executable, '-c', BASELINE_CODE, str(scenario_path),
                            str(folder / 'greedy.plan.json')], scenario,
                           folder / 'greedy.plan.json', folder / 'greedy.log', timeout, env)
        search = run_planner([sys.executable, str(planner_path), str(scenario_path),
                              str(folder / 'search.plan.json'), '--seconds', str(seconds)],
                             scenario, folder / 'search.plan.json', folder / 'search.log', timeout, env)
        if search['status'] == 'ok':
            row['diagnostics'] = read_diagnostics(folder / 'search.log')
    except Exception as exc:
        row['generation_status'] = 'error'
        row['generation_note'] = f'{type(exc).__name__}: {exc}'
    row.update(baseline=base, search=search,
               baseline_score=base['score'], baseline_seconds=base['seconds'],
               search_score=search['score'], search_seconds=search['seconds'],
               improvement=search['score']-base['score'], served=search['served'])
    return row


def percent(delta, baseline):
    return 100.0*delta/baseline if baseline > 0 else None


def add_counts(total, counts):
    for key, value in (counts or {}).items():
        total[key] = total.get(key, 0) + value
    return total


def summarize(rows):
    eligible = [r for r in rows if r['generation_status'] == 'ok']
    unserved, unserved_points = {}, {}
    for r in eligible:
        add_counts(unserved, r.get('diagnostics', {}).get('unserved'))
        add_counts(unserved_points, r.get('diagnostics', {}).get('unserved_points'))
    unserved_points = dict(sorted(unserved_points.items(), key=lambda kv: -kv[1]))
    baseline = sum(r['baseline_score'] for r in eligible)
    score = sum(r['search_score'] for r in eligible)
    return dict(cases=len(rows), compared=len(eligible), baseline_score=baseline,
                search_score=score, improvement=score-baseline,
                improvement_pct=percent(score-baseline, baseline),
                wins=sum(r['improvement'] > 1e-7 for r in eligible),
                ties=sum(abs(r['improvement']) <= 1e-7 for r in eligible),
                losses=sum(r['improvement'] < -1e-7 for r in eligible),
                search_valid=sum(r['search']['status'] == 'ok' for r in eligible),
                search_failures=sum(r['search']['status'] != 'ok' for r in eligible),
                baseline_failures=sum(r['baseline']['status'] != 'ok' for r in eligible),
                setup_failures=len(rows)-len(eligible),
                search_timeouts=sum(r['search']['status'] == 'timeout' for r in eligible),
                baseline_timeouts=sum(r['baseline']['status'] == 'timeout' for r in eligible),
                median_search_seconds=statistics.median([r['search_seconds'] for r in eligible])
                                      if eligible else 0.0,
                max_search_seconds=max((r['search_seconds'] for r in eligible), default=0.0),
                total_baseline_seconds=sum(r['baseline_seconds'] for r in eligible),
                total_search_seconds=sum(r['search_seconds'] for r in eligible),
                passes=[r.get('diagnostics', {}).get('passes') for r in eligible],
                unserved={key: unserved[key] for key in unserved_points},
                unserved_points=unserved_points)


def pct_label(value):
    return f'{value:+.1f}%' if value is not None else 'n/a'


def write_report(output, rows, settings, elapsed, complete):
    rows = sorted(rows, key=lambda r: (r['level'], r['seed']))
    total = summarize(rows)
    by_level = {level: summarize([r for r in rows if r['level'] == level])
                for level in LEVELS if any(r['level'] == level for r in rows)}
    summary = dict(settings=settings, complete=complete, elapsed_seconds=elapsed,
                   completed=len(rows), requested=len(settings['cases']), total=total, levels=by_level)
    save_json(summary, output / 'summary.json')
    save_json(rows, output / 'results.json')
    with (output / 'results.csv').open('w', newline='', encoding='utf-8') as handle:
        fields = ['case', 'baseline_score', 'search_score', 'improvement', 'baseline_seconds',
                  'search_seconds', 'baseline_status', 'search_status', 'generation_status', 'note']
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            record = {key: r[key] for key in fields if key in r}
            record.update(baseline_status=r['baseline']['status'], search_status=r['search']['status'],
                          note=' | '.join(filter(None, [r['generation_note'],r['baseline']['note'],r['search']['note']])))
            writer.writerow(record)
    escape = html.escape
    table = []
    for label, aggregate in [('TOTAL', total), *by_level.items()]:
        table.append(f"<tr><th>{label}</th><td>{aggregate['cases']}</td>"
                     f"<td>{aggregate['baseline_score']:.2f}</td><td>{aggregate['search_score']:.2f}</td>"
                     f"<td>{aggregate['improvement']:+.2f}</td><td>{pct_label(aggregate['improvement_pct'])}</td>"
                     f"<td>{aggregate['wins']} / {aggregate['ties']} / {aggregate['losses']}</td>"
                     f"<td>{aggregate['search_valid']} / {aggregate['compared']}</td></tr>")
    cases = []
    for r in rows:
        css = 'win' if r['improvement'] > 1e-7 else 'loss' if r['improvement'] < -1e-7 else ''
        note = ' | '.join(filter(None, [r['generation_note'],r['baseline']['note'],r['search']['note']]))
        folder = escape(r['folder'], quote=True)
        cases.append(f"<tr class='{css}'><td>{escape(r['case'])}</td><td>{r['baseline_score']:.2f}</td>"
                     f"<td>{r['search_score']:.2f}</td><td>{r['improvement']:+.2f}</td>"
                     f"<td>{pct_label(percent(r['improvement'], r['baseline_score']))}</td>"
                     f"<td>{r['baseline_seconds']:.2f}</td><td>{r['search_seconds']:.2f}</td>"
                     f"<td>{escape(r['baseline']['status'])} / {escape(r['search']['status'])}</td>"
                     f"<td><a href='{folder}/scenario.json'>scenario</a> · "
                     f"<a href='{folder}/greedy.plan.json'>greedy</a> · "
                     f"<a href='{folder}/search.plan.json'>ours</a> · "
                     f"<a href='{folder}/search.log'>log</a></td><td>{escape(note)}</td></tr>")
    reasons = list(total['unserved_points'])
    why = []
    for label, aggregate in [('TOTAL', total), *by_level.items()]:
        cells = ''.join(f"<td>{aggregate['unserved'].get(k, 0)} · {aggregate['unserved_points'].get(k, 0):.0f} pts</td>"
                        for k in reasons)
        why.append(f"<tr><th>{label}</th>{cells}</tr>")
    why_table = (f"<h2>Why contacts were not served (our plan)</h2><p>Contacts · points lost, by primary reason. "
                 f"collateral_ban can never be served (R7). never_tried and time_cap mean the search did not "
                 f"get to it; outranked means a valid serve existed in a branch that lost.</p>"
                 f"<div class='scroll'><table><thead><tr><th>Level</th>"
                 f"{''.join(f'<th>{escape(k)}</th>' for k in reasons)}</tr></thead><tbody>{''.join(why)}</tbody></table></div>"
                 if reasons else '')
    warning = ('Parallel runs share CPU and can reduce search quality and affect timing. '
               'Use one worker for a representative comparison.' if settings['workers'] > 1
               else 'Single-worker run: scenarios execute sequentially.')
    refresh = '' if complete else '<meta http-equiv="refresh" content="5">'
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8">{refresh}
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Fleet benchmark</title>
<style>body{{font:15px system-ui;background:#111827;color:#e5e7eb;margin:30px}}h1{{margin-bottom:8px}}
p{{max-width:1000px;line-height:1.5;color:#cbd5e1}}a{{color:#93c5fd}}table{{border-collapse:collapse;margin:20px 0;width:100%}}
th,td{{padding:10px;text-align:right;border-bottom:1px solid #374151;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}
thead{{background:#1f2937}}.win td:nth-child(4){{color:#86efac}}.loss td:nth-child(4){{color:#fca5a5}}
.scroll{{overflow:auto}}.notice{{padding:12px;background:#1f2937;border-left:3px solid #60a5fa}}
</style><h1>Fleet planner versus greedy</h1>
<p>{'Complete' if complete else 'Running'} · {len(rows)}/{len(settings['cases'])} cases · {elapsed:.1f} s elapsed ·
{settings['workers']} worker(s) · {settings['seconds']:g} s search budget · {settings['timeout']:g} s process limit</p>
<p class="notice">{warning} Both planners run in separate processes with the same hard timeout.
Crashes, timeouts, missing or invalid plans score zero. Scenario setup failures are excluded from score comparisons.</p>
<div class="scroll"><table><thead><tr><th>Level</th><th>Cases</th><th>Greedy score</th><th>Our score</th>
<th>Gain</th><th>Gain %</th><th>Wins / ties / losses</th><th>Our valid outputs</th></tr></thead>
<tbody>{''.join(table)}</tbody></table></div>
{why_table}
<p>Our failures: {total['search_failures']} ({total['search_timeouts']} timeouts) · Greedy failures: {total['baseline_failures']}
({total['baseline_timeouts']} timeouts) · Setup failures: {total['setup_failures']} · Slowest search: {total['max_search_seconds']:.2f} s</p>
<p>Ranking uses the sum of raw scores. Gain % is calculated from summed scores, rather than averaging percentages.
Inspect individual plans with the official flight viewer. This report refreshes every five seconds while running.</p>
<div class="scroll"><table><thead><tr><th>Scenario</th><th>Greedy</th><th>Ours</th><th>Gain</th><th>Gain %</th>
<th>Greedy s</th><th>Our s</th><th>Greedy / our status</th><th>Files</th><th>Failure details</th></tr></thead>
<tbody>{''.join(cases)}</tbody></table></div><p>Source: {escape(settings['planner'])}.
<a href="results.csv">CSV</a> · <a href="results.json">JSON</a> · <a href="summary.json">Summary and settings</a></p></html>'''
    temporary = output / 'report.html.tmp'
    temporary.write_text(page, encoding='utf-8')
    temporary.replace(output / 'report.html')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--seconds', type=float, default=55, help='search deadline, 0..55 (default: 55)')
    parser.add_argument('--cases', nargs='+', help='explicit cases, e.g. L1:0 L2:7')
    parser.add_argument('--levels', help='comma-separated levels (default: L1,L2,L3,L4)')
    parser.add_argument('--seeds', help='inclusive ranges or comma-separated seeds (default: 0-2)')
    parser.add_argument('--workers', type=int, default=1, help='concurrent scenarios (default: 1)')
    parser.add_argument('--timeout', type=float, default=60, help='hard limit per planner, 0 < seconds <= 60')
    parser.add_argument('--planner', type=Path, default=ROOT / 'planner.py', help='candidate planner entry point')
    parser.add_argument('--output', type=Path, help='new output directory (default: output/benchmarks/TIMESTAMP)')
    args = parser.parse_args(argv)
    if not 0 <= args.seconds <= 55 or not 0 < args.timeout <= 60 or args.workers < 1:
        parser.error('seconds must be 0..55; timeout must be >0 and <=60; workers must be positive')
    try:
        selected = select_cases(args.cases, args.levels, args.seeds)
    except ValueError as exc:
        parser.error(str(exc))
    planner_path = args.planner.resolve()
    if not planner_path.is_file():
        parser.error(f'planner does not exist: {planner_path}')
    output = (args.output or ROOT / 'output' / 'benchmarks' /
              datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error(f'output directory already exists; choose a new path: {output}')
    settings = dict(seconds=args.seconds, timeout=args.timeout, workers=args.workers,
                    cases=[f'{level}:{seed}' for level, seed in selected], planner=str(planner_path),
                    started_utc=datetime.now(timezone.utc).isoformat())
    save_json(settings, output / 'run.json')
    start = time.monotonic()
    rows = []
    write_report(output, rows, settings, 0, False)
    print(f'Benchmark: {len(selected)} cases, {args.workers} worker(s), {args.seconds:g}s search / {args.timeout:g}s process limit')
    if args.workers > 1:
        print('Parallel workers share CPU; use --workers 1 for representative timing and search quality.')
    print(f'Report: {output / "report.html"}', flush=True)
    print(' Done  Scenario      Greedy        Ours        Gain     Gain %   Our s  Greedy / our status', flush=True)
    with (output / 'results.jsonl').open('w', encoding='utf-8') as log:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(run_case, level, seed, output, args.seconds, args.timeout, planner_path)
                       for level, seed in selected]
            for future in as_completed(futures):
                row = future.result()
                rows.append(row)
                log.write(json.dumps(row)+'\n')
                log.flush()
                print(f"{len(rows):3}/{len(selected):<3} {row['case']:<9} {row['baseline_score']:10.2f} "
                      f"{row['search_score']:11.2f} {row['improvement']:+11.2f} "
                      f"{pct_label(percent(row['improvement'], row['baseline_score'])):>9} "
                      f"{row['search_seconds']:7.2f}  {row['baseline']['status']} / {row['search']['status']}", flush=True)
                write_report(output, rows, settings, time.monotonic()-start, False)
    summary = write_report(output, rows, settings, time.monotonic()-start, True)
    print('\n Score summary (sum of raw scores)')
    for level, s in [('TOTAL', summary['total']), *summary['levels'].items()]:
        print(f" {level:<5} greedy {s['baseline_score']:10.2f}  ours {s['search_score']:10.2f}  "
              f"gain {s['improvement']:+10.2f} ({pct_label(s['improvement_pct'])})  "
              f"W/T/L {s['wins']}/{s['ties']}/{s['losses']}  valid {s['search_valid']}/{s['compared']}")
    if summary['total']['unserved_points']:
        print('\n Unserved contacts in our plans (count, points lost)')
        for level, s in [('TOTAL', summary['total']), *summary['levels'].items()]:
            parts = [f"{k} {s['unserved'][k]} ({s['unserved_points'][k]:.0f})" for k in s['unserved_points']]
            print(f" {level:<5} " + ', '.join(parts))
    total = summary['total']
    print(f" Failures: ours {total['search_failures']}, greedy {total['baseline_failures']}, setup {total['setup_failures']}")
    print(f'Report: {output / "report.html"}')
    return int(bool(total['search_failures'] or total['baseline_failures'] or total['setup_failures']))


if __name__ == '__main__':
    sys.exit(main())
