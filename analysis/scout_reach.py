"""usage: python3 analysis/scout_reach.py BENCHMARK_DIR

Counts targets that no scout could observe in time even when flying straight from base at
full horizontal speed, climbing to 70 m, ignoring zones and other tasks. It is a lower bound
on unreachable targets: these cannot be served by any plan, the rest might be.
Hostiles banned by R7 (collateral) are excluded.
"""
import json
import math
from pathlib import Path
import sys


def unreachable(scenario):
    rules = scenario['rules']
    scouts = [a for a in scenario['aircraft'] if a['type'] == 'scout']
    protected = [c for c in scenario['contacts'] if c['class'] in ('friendly', 'civilian')]
    targets, missed, points = 0, 0, 0
    for c in scenario['contacts']:
        if c['class'] not in ('hostile', 'unknown') or not c.get('window_s'):
            continue
        if c['class'] == 'hostile' and any(math.dist(c['pos'][:2], p['pos'][:2]) < rules['collateral_radius_m']
                                           for p in protected):
            continue
        targets += 1
        lo, hi = c['window_s']
        earliest = min(70 / a['v_up_mps']
                       + max(0.0, math.dist(a['home'][:2], c['pos'][:2]) - rules['R_obs_m']) / a['v_h_mps']
                       for a in scouts)
        if max(earliest, lo) + rules['obs_dwell_s'] > hi:
            missed += 1
            points += c['points']
    return targets, missed, points


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for case in sorted(p for p in Path(sys.argv[1]).iterdir() if (p / 'scenario.json').exists()):
        targets, missed, points = unreachable(json.loads((case / 'scenario.json').read_text()))
        print(f'{case.name}: {missed} of {targets} non-banned targets unreachable by any scout ({points:g} pts)')


if __name__ == '__main__':
    main()
