"""Builds an HTML comparison page from analysis/compare_policies.py --json output.

    python analysis/linkloss_report_html.py output/linkloss_v2/final.json \
        --ablation output/linkloss_v2/ablation2.json -o output/linkloss_v2/policy_v2_comparison.html
"""
import argparse
import html
import json
from collections import defaultdict
from pathlib import Path

MU = 0.002

CHANGES = [
    ("Scouts finish observations before a pop-up zone",
     "policy.py turned a scout back as soon as any later part of its route crossed a new zone, "
     "dropping every remaining observation (12 contacts at 1000×200 in the stored collection). "
     "v2 keeps flying, completes the observations before the crossing, then turns back. Zone checks "
     "look one step ahead with a 4 m margin, so the scout can reach the zone's edge."),
    ("Unconfirmed effectors park instead of turning back",
     "Without a confirmation, policy.py held short and turned back after 15 s, flying head-on into "
     "its own strike partners coming in behind it (3 of the 6 stored separation failures). v2 parks "
     "1.5 s of route time short of the target, inside the 100 m zone where strike partners are "
     "exempt from separation, and engages if the confirmation arrives later."),
    ("Take-off check covers announced and priority peers",
     "A peer still on its pad that had announced a state was left out of the separation check, even "
     "when it would take off within the look-ahead. A lower-id peer, which may move any way, is now "
     "assumed able to take off at any moment."),
    ("Aircraft that can no longer be on time stand down",
     "Some aircraft waited minutes on the ground, then took off into airspace nobody predicted them "
     "in. v2 aborts once no remaining task can happen inside its contact's window; an aircraft "
     "still on the ground stays there."),
    ("Re-announce when a peer comes close",
     "A state message sent while out of range is lost, and the 8 s refresh came too late at 250 m "
     "range. An off-plan aircraft now re-announces when a peer it believes is within 350 m was not "
     "that close at its last announcement."),
    ("Strike partners relay the confirmation",
     "An effector that heard its target's confirmation repeats it 10 s and 5 s (route time) before "
     "engaging, so partners close behind it hear it too."),
    ("Fewer repeated observation notices",
     "Scouts repeated each observation notice every second for about 100 s (30 % of all messages). "
     "v2 sends it every second for 6 s, then every 3 s."),
]


def fmt_setting(s):
    return f"{s[0]:g} m × {s[1]:g} B/s"


def summarise(rows, policy, setting):
    sel = [r for r in rows if r["policy"] == policy and r["setting"] == list(setting)]
    n = len(sel)
    ret = sum(r["ret"] for r in sel) / n
    msgs = sum(r["msgs"] for r in sel) / n
    fails = sum(bool(r["failed"]) for r in sel)
    ms = sum(r["ms"] for r in sel) / n
    return dict(n=n, ret=ret, msgs=msgs, fails=fails, s_loss=ret - MU * msgs, ms=ms,
                meets=ret >= 0.9 and fails == 0)


def esc(x):
    return html.escape(str(x))


LABELS = {"policy.py": "policy.py (news forwarding)", "policy_news_off.py": "policy.py, news off",
          "policy_v2.py": "policy_v2.py"}
COLORS = ["var(--base)", "var(--alt)", "var(--accent)", "var(--muted)"]


def build(main, ablation, out, base=None, new=None):
    pols = main["policies"]
    base = base or pols[0]
    new = new or pols[-1]
    rows = main["rows"]
    settings = [tuple(s) for s in main["settings"]]
    cases = sorted({r["case"] for r in rows})

    # ---------------------------------------------------------------- headline table
    def headline_table():
        out_rows = []
        for s in settings:
            b = summarise(rows, base, s)
            for i, p in enumerate(pols):
                m = summarise(rows, p, s)
                is_new = p == new
                delta = ""
                if p != base:
                    d = m["s_loss"] - b["s_loss"]
                    delta = f'<span class="delta {"up" if d > 0 else "down" if d < 0 else ""}">{d:+.3f}</span>'
                out_rows.append(
                    f'<tr class="{"new" if is_new else ""}"><td>{esc(fmt_setting(s)) if i == 0 else ""}</td>'
                    f'<td>{esc(LABELS.get(p, p))}</td>'
                    f'<td class="num strong">{m["s_loss"]:.3f} {delta}</td>'
                    f'<td class="num">{m["ret"]:.3f}</td>'
                    f'<td class="num">{m["fails"]}/{m["n"]}</td>'
                    f'<td class="num">{m["msgs"]:.1f}</td>'
                    f'<td class="num">{m["ms"]:.2f}</td></tr>')
        return "\n".join(out_rows)

    # ---------------------------------------------------------------- per level chart data
    levels = sorted({c.split("-")[0] for c in cases})
    per_level = []
    for s in settings:
        for lv in levels:
            vals = {}
            for p in pols:
                sel = [r for r in rows if r["policy"] == p and r["setting"] == list(s) and r["case"].startswith(lv + "-")]
                vals[p] = (sum(r["ret"] for r in sel) / len(sel), sum(bool(r["failed"]) for r in sel), len(sel))
            per_level.append((s, lv, vals))

    def level_chart(s):
        data = [(lv, vals) for ss, lv, vals in per_level if ss == s]
        w, h, left, bottom, top = 520, 230, 44, 34, 14
        plot_h = h - bottom - top
        group_w = (w - left - 10) / len(data)
        k = len(pols)
        bar_w = min(30, (group_w - 12) / k - 4)
        parts = []
        for v in (0, 0.25, 0.5, 0.75, 1.0):
            y = top + plot_h * (1 - v)
            parts.append(f'<line x1="{left}" x2="{w - 6}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                         f'<text x="{left - 6}" y="{y + 4:.1f}" class="tick" text-anchor="end">{v:.2f}</text>')
        for i, (lv, vals) in enumerate(data):
            cx = left + group_w * (i + 0.5)
            for j, p in enumerate(pols):
                ret, fails, n = vals[p]
                x = cx - (k * bar_w + (k - 1) * 4) / 2 + j * (bar_w + 4)
                bh = plot_h * ret
                y = top + plot_h - bh
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(bh, 0.5):.1f}" rx="2" style="fill:{COLORS[j % len(COLORS)]}">'
                             f'<title>{esc(lv)} {esc(p)}: retention {ret:.3f}, {fails}/{n} failed</title></rect>')
                parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" class="val" text-anchor="middle">{ret:.2f}</text>')
            parts.append(f'<text x="{cx:.1f}" y="{h - bottom + 18:.1f}" class="lvl" text-anchor="middle">{esc(lv)}</text>')
        return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Mean retention per level at {esc(fmt_setting(s))}">'
                + "".join(parts) + "</svg>")

    # ---------------------------------------------------------------- paired outcomes
    key = lambda r: (r["case"], tuple(r["setting"]), r["comm"])  # noqa: E731
    b = {key(r): r for r in rows if r["policy"] == base}
    n = {key(r): r for r in rows if r["policy"] == new}
    paired = defaultdict(lambda: [0, 0, 0])
    changes = []
    for k in b:
        d = n[k]["ret"] - b[k]["ret"]
        idx = 0 if d > 1e-9 else 1 if d < -1e-9 else 2
        paired[k[1]][idx] += 1
        if abs(d) > 1e-9:
            changes.append((k, b[k], n[k], d))
    changes.sort(key=lambda x: x[3])

    def change_rows(items):
        out_rows = []
        for k, rb, rn, d in items:
            why = (rn["failed"] or rb["failed"])
            who = ""
            if rn["failed"] and not rb["failed"]:
                who = "v2 fails: "
            elif rb["failed"] and not rn["failed"]:
                who = "policy.py failed: "
            elif rb["failed"] and rn["failed"]:
                who = "both fail: "
            reason = f'{who}{why[0][:110]}' if why else "valid in both, value changed"
            out_rows.append(f'<tr><td>{esc(k[0])}</td><td>{esc(fmt_setting(k[1]))}</td><td class="num">{k[2]}</td>'
                            f'<td class="num">{rb["ret"]:.3f}</td><td class="num">{rn["ret"]:.3f}</td>'
                            f'<td class="num"><span class="delta {"up" if d > 0 else "down"}">{d:+.3f}</span></td>'
                            f'<td class="why">{esc(reason)}</td></tr>')
        return "\n".join(out_rows)

    worse = [c for c in changes if c[3] < 0]
    better = [c for c in changes if c[3] > 0][::-1]

    # ---------------------------------------------------------------- ablation
    abl_html = ""
    if ablation:
        ab_rows = ablation["rows"]
        ab_pols = ablation["policies"]
        ab_settings = [tuple(s) for s in ablation["settings"]]
        ref = ab_pols[0]
        labels = {
            "v2_all.py": "v2, all changes on",
            "no_zone.py": "without: scouts finish before zone",
            "no_park.py": "without: park unconfirmed effectors",
            "no_takeoff.py": "without: take-off check fix",
            "no_hopeless.py": "without: stand down when late",
            "no_resend.py": "without: re-announce when close",
            "no_relay.py": "without: partner relay",
            "o_every_s.py": "without: fewer observation notices",
            "ghost45.py": "with: parked-peer ghost (45 s), off in v2",
        }
        cells = []
        hdr = "".join(f'<th class="num" colspan="2">{esc(fmt_setting(s))}</th>' for s in ab_settings)
        sub = "".join('<th class="num">S_loss</th><th class="num">failed</th>' for _ in ab_settings)
        for p in ab_pols:
            tds = []
            for s in ab_settings:
                m = summarise(ab_rows, p, s)
                r = summarise(ab_rows, ref, s)
                d = m["s_loss"] - r["s_loss"]
                dd = "" if p == ref else f' <span class="delta {"up" if d > 0 else "down" if d < 0 else ""}">{d:+.3f}</span>'
                tds.append(f'<td class="num">{m["s_loss"]:.3f}{dd}</td><td class="num">{m["fails"]}/{m["n"]}</td>')
            cells.append(f'<tr class="{"new" if p == ref else ""}"><td>{esc(labels.get(p, p))}</td>{"".join(tds)}</tr>')
        abl_html = f"""
<section>
  <h2>Which change does what</h2>
  <p class="lede">Each row turns one change off and reruns the same {len({r['case'] for r in ab_rows})} scenarios. A negative difference means v2 does better with that change in place. Measured earlier with {ablation['comm_seeds']} communication seeds at {esc(', '.join(fmt_setting(s) for s in ab_settings))}: differences of one or two failed runs are within the run-to-run noise.</p>
  <div class="scroll"><table>
    <thead><tr><th>Variant</th>{hdr}</tr><tr><th></th>{sub}</tr></thead>
    <tbody>{''.join(cells)}</tbody>
  </table></div>
</section>"""

    lead = next((s for s in settings if s[0] == 1000), settings[0])
    bl, nl = summarise(rows, base, lead), summarise(rows, new, lead)
    tail = min(settings)
    bt, nt = summarise(rows, base, tail), summarise(rows, new, tail)
    legend_html = "".join(f'<span><i style="background:{COLORS[j % len(COLORS)]}"></i>{esc(LABELS.get(p, p))}</span>' for j, p in enumerate(pols))
    changes_html = "".join(f"<li><h3>{esc(t)}</h3><p>{esc(d)}</p></li>" for t, d in CHANGES)
    paired_html = "".join(
        f'<div class="pair"><div class="pair-set">{esc(fmt_setting(s))}</div>'
        f'<div class="pair-bar"><span class="seg up" style="flex:{paired[s][0]}"></span>'
        f'<span class="seg same" style="flex:{paired[s][2]}"></span><span class="seg down" style="flex:{paired[s][1]}"></span></div>'
        f'<div class="pair-nums"><b class="up-t">{paired[s][0]} better</b> · {paired[s][2]} same · <b class="down-t">{paired[s][1]} worse</b></div></div>'
        for s in settings)
    charts_html = "".join(f'<figure><figcaption>{esc(fmt_setting(s))}</figcaption>{level_chart(s)}</figure>' for s in settings)

    page = f"""<title>Link-loss Policy v2</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&family=Space+Grotesk:wght@500;600&display=swap">
<style>
/* Layout: one reading column; tables scroll on their own; summary before detail. */
:root {{
  --bg: #f6f7f9; --panel: #ffffff; --fg: #17202b; --muted: #5b6676; --line: #dde2e8;
  --accent: #2557c7; --base: #9aa6b6; --alt: #c9853a; --up: #157a4a; --down: #b3261e; --same: #c9d0d9;
  --display: "Space Grotesk", "Segoe UI", system-ui, sans-serif;
  --body: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Consolas, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #0f141b; --panel: #161d27; --fg: #e4e9f0; --muted: #98a3b3; --line: #2a3442;
  --accent: #7fa6ff; --base: #5d6878; --alt: #d9a05b; --up: #4cc38a; --down: #ff7b72; --same: #3a4555; color-scheme: dark }} }}
:root[data-theme="dark"] {{
  --bg: #0f141b; --panel: #161d27; --fg: #e4e9f0; --muted: #98a3b3; --line: #2a3442;
  --accent: #7fa6ff; --base: #5d6878; --alt: #d9a05b; --up: #4cc38a; --down: #ff7b72; --same: #3a4555; color-scheme: dark }}
body {{ background: var(--bg); color: var(--fg); font-family: var(--body); font-size: 15px; line-height: 1.55; }}
main {{ max-width: 980px; margin: 0 auto; padding-block: 40px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 44px; }}
h1, h2, h3 {{ font-family: var(--display); text-wrap: balance; margin: 0; }}
h1 {{ font-size: clamp(28px, 5vw, 40px); font-weight: 600; letter-spacing: -0.01em; }}
h2 {{ font-size: 22px; font-weight: 600; }}
h3 {{ font-size: 16px; font-weight: 600; }}
p {{ margin: 0; max-width: 68ch; }}
section {{ display: grid; gap: 16px; }}
.eyebrow {{ font-family: var(--mono); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }}
.lede {{ color: var(--muted); }}
header {{ display: grid; gap: 12px; }}
.kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }}
.kpi {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; display: grid; gap: 4px; }}
.kpi .k {{ font-size: 12px; color: var(--muted); letter-spacing: 0.04em; text-transform: uppercase; }}
.kpi .v {{ font-family: var(--display); font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }}
.kpi .v small {{ font-size: 15px; color: var(--muted); font-weight: 500; }}
.scroll {{ overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ padding: 9px 12px; text-align: left; border-bottom: 1px solid var(--line); white-space: nowrap; }}
th {{ font-weight: 500; color: var(--muted); font-size: 12px; letter-spacing: 0.03em; }}
tbody tr:last-child td {{ border-bottom: 0; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; font-family: var(--mono); font-size: 13px; }}
td.strong {{ font-weight: 600; }}
td.why {{ white-space: normal; min-width: 280px; color: var(--muted); font-size: 13px; }}
tr.new td {{ background: color-mix(in srgb, var(--accent) 7%, transparent); }}
code {{ font-family: var(--mono); font-size: 13px; }}
.delta {{ font-family: var(--mono); font-size: 12px; padding: 1px 6px; border-radius: 10px; margin-left: 4px; }}
.delta.up {{ color: var(--up); background: color-mix(in srgb, var(--up) 12%, transparent); }}
.delta.down {{ color: var(--down); background: color-mix(in srgb, var(--down) 12%, transparent); }}
.charts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }}
figure {{ margin: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 12px 4px; min-width: 0; }}
figcaption {{ font-size: 13px; color: var(--muted); margin-bottom: 4px; }}
svg {{ width: 100%; height: auto; display: block; }}
svg .grid {{ stroke: var(--line); stroke-width: 1; }}
svg .tick, svg .val {{ fill: var(--muted); font: 11px var(--mono); }}
svg .lvl {{ fill: var(--fg); font: 500 13px var(--body); }}
svg .bar-base {{ fill: var(--base); }}
svg .bar-new {{ fill: var(--accent); }}
.legend {{ display: flex; gap: 16px; font-size: 13px; color: var(--muted); flex-wrap: wrap; }}
.legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }}
.pairs {{ display: grid; gap: 12px; }}
.pair {{ display: grid; grid-template-columns: 160px 1fr; gap: 6px 14px; align-items: center; }}
.pair-set {{ font-size: 13px; }}
.pair-bar {{ display: flex; height: 14px; border-radius: 7px; overflow: hidden; background: var(--same); }}
.seg.up {{ background: var(--up); }} .seg.same {{ background: var(--same); }} .seg.down {{ background: var(--down); }}
.pair-nums {{ grid-column: 2; font-size: 13px; color: var(--muted); }}
.up-t {{ color: var(--up); font-weight: 600; }} .down-t {{ color: var(--down); font-weight: 600; }}
ol.changes {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }}
ol.changes li {{ display: grid; gap: 6px; align-content: start; padding-top: 12px; border-top: 2px solid var(--line); }}
ol.changes p {{ color: var(--muted); font-size: 14px; }}
details {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
summary {{ cursor: pointer; padding: 10px 14px; font-weight: 500; }}
summary:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
details .scroll {{ border: 0; border-top: 1px solid var(--line); border-radius: 0 0 8px 8px; }}
.method {{ color: var(--muted); font-size: 14px; display: grid; gap: 8px; }}
@media (max-width: 520px) {{ .pair {{ grid-template-columns: 1fr; }} .pair-nums {{ grid-column: 1; }} }}
</style>

<main>
  <header>
    <div class="eyebrow">Link-loss challenge · policy comparison</div>
    <h1>policy_v2.py against the current policy.py</h1>
    <p class="lede">The current <code>policy.py</code> forwards news of dangers by default (store and forward). It is shown with news on and off, next to v2, which shares no news. Same scenarios, events and communication seeds for every policy: greedy plans, {len(cases)} unseen scenarios ({esc(main['levels'].replace(',', ', '))}, seeds {esc(main['seeds'])}), {main['comm_seeds']} communication seeds each.</p>
  </header>

  <section>
    <div class="kpis">
      <div class="kpi"><span class="k">S_loss at {esc(fmt_setting(lead))}</span><span class="v">{nl['s_loss']:.3f} <small>from {bl['s_loss']:.3f}</small></span></div>
      <div class="kpi"><span class="k">Failed runs at {esc(fmt_setting(lead))}</span><span class="v">{nl['fails']}/{nl['n']} <small>from {bl['fails']}/{bl['n']}</small></span></div>
      <div class="kpi"><span class="k">S_loss at {esc(fmt_setting(tail))}</span><span class="v">{nt['s_loss']:.3f} <small>from {bt['s_loss']:.3f}</small></span></div>
      <div class="kpi"><span class="k">Failed runs at {esc(fmt_setting(tail))}</span><span class="v">{nt['fails']}/{nt['n']} <small>from {bt['fails']}/{bt['n']}</small></span></div>
    </div>
  </section>

  <section>
    <h2>Results</h2>
    <p class="lede">S_loss = mean retention − 0.002 × messages per aircraft per minute. A failed run scores 0 retention. 1000 m × 200 B/s is the leaderboard setting.</p>
    <div class="scroll"><table>
      <thead><tr><th>Setting</th><th>Policy</th><th class="num">S_loss</th><th class="num">Retention</th><th class="num">Failed runs</th><th class="num">Msg / aircraft / min</th><th class="num">ms / call</th></tr></thead>
      <tbody>{headline_table()}</tbody>
    </table></div>
  </section>

  <section>
    <h2>Retention by level</h2>
    <div class="legend">{legend_html}</div>
    <div class="charts">{charts_html}</div>
  </section>

  <section>
    <h2>Run by run</h2>
    <p class="lede">Each scenario, setting and communication seed is run with both policies. Retention is compared pair by pair.</p>
    <div class="pairs">{paired_html}</div>
    <details>
      <summary>Runs where v2 is worse ({len(worse)})</summary>
      <div class="scroll"><table>
        <thead><tr><th>Case</th><th>Setting</th><th class="num">Comm seed</th><th class="num">v1</th><th class="num">v2</th><th class="num">Change</th><th>Reason</th></tr></thead>
        <tbody>{change_rows(worse)}</tbody></table></div>
    </details>
    <details>
      <summary>Runs where v2 is better ({len(better)})</summary>
      <div class="scroll"><table>
        <thead><tr><th>Case</th><th>Setting</th><th class="num">Comm seed</th><th class="num">v1</th><th class="num">v2</th><th class="num">Change</th><th>Reason</th></tr></thead>
        <tbody>{change_rows(better)}</tbody></table></div>
    </details>
  </section>
{abl_html}
  <section>
    <h2>What changed in v2</h2>
    <p class="lede">Each change comes from the stored run history in <code>history/linkloss/</code>: what each aircraft knew, what it decided and why runs lost value or failed.</p>
    <ol class="changes">{changes_html}</ol>
  </section>

  <section class="method">
    <h2>How this was measured</h2>
    <p><code>analysis/compare_policies.py {esc(' '.join(pols))} --levels {esc(main['levels'])} --seeds {esc(main['seeds'])} --settings {esc(','.join(f'{s[0]:g}x{s[1]:g}' for s in settings))} --comm-seeds {main['comm_seeds']}</code>, {main['workers']} workers, {main['seconds']:.0f} s.</p>
    <p>Plans are the kit's greedy baseline and events use seed + 1, as in <code>handin/check_policy.py</code>. These are sampled scenarios: they show measured gains and losses on these seeds, not a guarantee on the organisers' reference plans. Separation failures remain, mostly between aircraft that leave the plan near the base or at 250 m range.</p>
  </section>
</main>
"""
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(page, encoding="utf-8")
    print("wrote", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("main")
    ap.add_argument("--ablation")
    ap.add_argument("--base", help="baseline policy name (default: the first)")
    ap.add_argument("--new", help="policy compared run by run (default: the last)")
    ap.add_argument("-o", "--output", default="output/linkloss_v2/policy_v2_comparison.html")
    a = ap.parse_args()
    main_data = json.loads(Path(a.main).read_text())
    abl = json.loads(Path(a.ablation).read_text()) if a.ablation else None
    build(main_data, abl, a.output, a.base, a.new)


if __name__ == "__main__":
    main()
