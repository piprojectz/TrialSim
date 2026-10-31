"""
TrialSim - Summarize results across all trials.

Reads every runs/<run>/result.json (plus the transcript for lie/objection counts) and prints a
scoreboard: verdicts vs. the hidden truth, accuracy, wrongful convictions / acquittals, who lies
most, and how the jury moved. Also writes runs/summary.html with links to each run's report.

Usage:
    python summarize.py                 # all completed trials
    python summarize.py --last 5        # only the 5 most recent
    python summarize.py --all           # include incomplete runs (no verdict) in the table
    python summarize.py --open          # open summary.html in the browser
"""
import argparse
import html
import json
import os
import webbrowser
from collections import Counter, defaultdict
from pathlib import Path

import report  # reuse transcript parsing + lie flags

BASE = Path(__file__).parent
RUNS_DIR = BASE / "runs"
esc = html.escape

os.system("")
BOLD, DIM, RESET, RED, GREEN, CYAN = "\033[1m", "\033[2m", "\033[0m", "\033[31m", "\033[32m", "\033[36m"


def load_run(run_dir: Path):
    tf = run_dir / "transcript.jsonl"
    if not tf.exists():
        return None
    recs = [json.loads(l) for l in tf.read_text(encoding="utf-8").splitlines() if l.strip()]
    rf = run_dir / "result.json"
    result = json.loads(rf.read_text(encoding="utf-8")) if rf.exists() else None
    case = report.load_case(run_dir)
    turns, leanings = report.build_turns(recs)

    # lies / flags by role
    flags = defaultdict(Counter)
    for t in turns:
        for f in t["flags"]:
            flags[t["role"]][f] += 1

    # jury movement: first leaning vs final ballot per juror
    by_juror = defaultdict(list)
    for r in leanings:
        by_juror[r["speaker"]].append(report.s(report.data_of(r).get("leaning")).lower())
    def flipped(v):  # first decided position vs final position (undecided -> X isn't a flip)
        decided = [x for x in v if x in ("guilty", "not guilty")]
        return len(decided) > 1 and decided[0] != decided[-1]
    flips = sum(1 for v in by_juror.values() if flipped(v))

    # who persuaded whom (final ballot)
    influence = Counter()
    for r in leanings:
        if r.get("kind") == "ballot":
            who = report.s(report.data_of(r).get("who_influenced_me")).strip()
            if who and who.lower() not in ("nobody", "none", "no one", "n/a"):
                influence[who] += 1

    # ---- individual lies / stretches, for the HTML "Lies" section
    gt = (case or {}).get("ground_truth", {})
    planted = {w["name"] for w in gt.get("witnesses", [])
               if w.get("will_lie") is True or str(w.get("will_lie")).lower() in ("true", "yes", "1")}
    knows = {w["name"]: w.get("knows", "") for w in gt.get("witnesses", [])}
    lie_list = []
    for t in turns:
        if t["flags"] == ["clean"] or t["role"] in ("juror",):
            continue
        pre, post = report.data_of(t["pre"]), report.data_of(t["post"])
        planned = pre.get("spin_or_hide", "")
        admitted = post.get("honesty_check", "")
        crossed = post.get("lines_i_crossed", "")
        if t["role"] == "judge" and "fake" not in t["flags"]:
            continue                                   # the judge's "spin" is just trial management
        lie_list.append({
            "speaker": t["speaker"], "role": t["role"], "phase": t["phase"],
            "flags": t["flags"],
            "planned": "" if report.is_nothing(planned) else report.s(planned),
            "said": (t["stmt"] or {}).get("text", ""),
            "admitted": "" if report.is_nothing(admitted) else report.s(admitted),
            "crossed": "" if report.is_nothing(crossed) else report.s(crossed),
            "fake": (t["stmt"] or {}).get("invalid_exhibits") or [],
            "planted": t["speaker"] in planted,
            "truth_known": knows.get(t["speaker"], ""),
        })

    count = lambda k: sum(r.get("kind") == k for r in recs)
    return {
        "lies": lie_list, "planted": sorted(planted),
        "run": run_dir.name, "dir": run_dir, "result": result, "case": case,
        "title": (case or {}).get("public", {}).get("title", run_dir.name),
        "crime": (case or {}).get("ground_truth", {}).get("crime", "?"),
        "truth": (case or {}).get("guilty"),
        "flags": flags, "flips": flips, "jurors": len(by_juror), "influence": influence,
        "objections": count("objection"),
        "sustained": sum(r.get("kind") == "ruling" and bool(r.get("sustained")) for r in recs),
        "outbursts": count("outburst"), "contempt": count("contempt_warning"),
        "has_report": (run_dir / "report.html").exists(),
    }


def lies(run, role):
    f = run["flags"].get(role, Counter())
    return f["spin"] + f["admitted"] + f["fake"]


def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "-"


def verdict_of(run):
    return run["result"]["verdict"] if run["result"] else "incomplete"


def tokens_of(run):
    tm = ((run["result"] or {}).get("tokens") or {}).get("by_model", {})
    return sum(t["in"] + t["out"] for t in tm.values())


def outcome(run):
    r = run["result"]
    if not r:
        return "-"
    if r["verdict"] == "hung":
        return "HUNG"
    return "RIGHT" if r["correct"] else "WRONG"


def main():
    ap = argparse.ArgumentParser(description="Summarize TrialSim results.")
    ap.add_argument("--last", type=int, help="only the N most recent runs")
    ap.add_argument("--all", action="store_true", help="include incomplete runs in the table")
    ap.add_argument("--open", action="store_true", help="open summary.html when done")
    a = ap.parse_args()

    if not RUNS_DIR.exists():
        raise SystemExit("No runs yet. Run trial.py first.")
    dirs = sorted((d for d in RUNS_DIR.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    runs = [r for r in (load_run(d) for d in dirs) if r]
    if a.last:
        runs = runs[-a.last:]
    done = [r for r in runs if r["result"]]
    shown = runs if a.all else done
    if not shown:
        raise SystemExit(f"No completed trials yet ({len(runs)} incomplete run(s)). "
                         "Finish a trial or use --all.")

    # ---------------- aggregate
    n = len(done)
    verdicts = Counter(r["result"]["verdict"] for r in done)
    decided = [r for r in done if r["result"]["verdict"] != "hung"]
    right = sum(1 for r in decided if r["result"]["correct"])
    guilty_cases = [r for r in decided if r["truth"]]
    innocent_cases = [r for r in decided if r["truth"] is False]
    wrongful_conv = sum(1 for r in innocent_cases if r["result"]["verdict"] == "guilty")
    wrongful_acq = sum(1 for r in guilty_cases if r["result"]["verdict"] == "not guilty")
    rounds = [r["result"]["rounds"] for r in done]
    mins = [r["result"].get("minutes", 0) for r in done]
    pros_lies = sum(lies(r, "prosecutor") for r in done)
    def_lies = sum(lies(r, "defense attorney") for r in done)
    wit_lies = sum(lies(r, "witness") for r in done)
    contempt_by = Counter()
    sustained_against = Counter()
    for r in done:
        recs = [json.loads(l) for l in (r["dir"] / "transcript.jsonl").read_text(
            encoding="utf-8").splitlines() if l.strip()]
        for x in recs:
            if x.get("kind") == "contempt_warning":
                contempt_by[x.get("target", "?")] += 1
            if x.get("kind") == "ruling" and x.get("sustained"):
                sustained_against[x.get("against", "?")] += 1

    # ---------------- console
    print(f"\n{BOLD}TRIALSIM SUMMARY{RESET}  {n} completed trial(s)"
          f"{f', {len(runs) - n} incomplete' if len(runs) > n else ''}\n")
    hdr = f"{'run':<28}{'case':<28}{'truth':<10}{'verdict':<12}{'result':<8}{'rnd':>4}{'obj':>6}{'flip':>6}"
    print(BOLD + hdr + RESET)
    for r in shown:
        res = r["result"]
        truth = "guilty" if r["truth"] else "innocent" if r["truth"] is False else "?"
        o = outcome(r)
        col = GREEN if o == "RIGHT" else RED if o == "WRONG" else DIM
        print(f"{r['run'][:27]:<28}{r['title'][:27]:<28}{truth:<10}{verdict_of(r):<12}"
              f"{col}{o:<8}{RESET}{(res['rounds'] if res else '-'):>4}"
              f"{str(r['objections']) + '/' + str(r['sustained']):>6}{r['flips']:>6}")
    if n:
        print(f"\n{BOLD}Accuracy{RESET}         {right}/{len(decided)} decided trials ({pct(right, len(decided))})"
              f"   hung: {verdicts['hung']}")
        print(f"{BOLD}Verdicts{RESET}         guilty {verdicts['guilty']}, not guilty {verdicts['not guilty']}, "
              f"hung {verdicts['hung']}   (prosecution win rate {pct(verdicts['guilty'], n)})")
        print(f"{BOLD}Wrongful{RESET}         convictions {wrongful_conv}/{len(innocent_cases)} innocent, "
              f"acquittals {wrongful_acq}/{len(guilty_cases)} guilty")
        print(f"{BOLD}Lies flagged{RESET}     prosecutor {pros_lies}, defense {def_lies}, witnesses {wit_lies}"
              f"  (spin + admitted + fake exhibit)")
        liars = Counter()
        for r in done:
            for l in r["lies"]:
                liars[f"{l['speaker']} ({l['role']})"] += 1
        print(f"{BOLD}Biggest liars{RESET}    " + (", ".join(f"{k} {v}" for k, v in liars.most_common(5))
                                                 or "none"))
        print(f"{BOLD}Caught{RESET}           sustained objections against: "
              + (", ".join(f"{k} {v}" for k, v in sustained_against.most_common()) or "nobody"))
        print(f"{BOLD}Judge{RESET}            contempt warnings: "
              + (", ".join(f"{k} {v}" for k, v in contempt_by.most_common()) or "none")
              + f"   outbursts {sum(r['outbursts'] for r in done)}")
        print(f"{BOLD}Deliberation{RESET}     avg {sum(rounds) / n:.1f} ballot rounds,"
              f" avg {sum(r['flips'] for r in done) / n:.1f} jurors flipped per trial")
        print(f"{BOLD}Run time{RESET}         avg {sum(mins) / n:.0f} min, total {sum(mins) / 60:.1f} h")
        tok = [tokens_of(r) for r in done if tokens_of(r)]
        if tok:
            by_model = Counter()
            for r in done:
                for m, t in (r["result"].get("tokens") or {}).get("by_model", {}).items():
                    by_model[m] += t["in"] + t["out"]
            print(f"{BOLD}Tokens{RESET}           avg {sum(tok) / len(tok):,.0f} per trial, total {sum(tok):,}  ("
                  + ", ".join(f"{m} {v:,}" for m, v in by_model.most_common()) + ")")

    # ---------------- html
    rows = []
    for r in shown:
        res = r["result"]
        o = outcome(r)
        rep = r["dir"] / "report.html"
        link = (f"<a href='{esc(r['run'])}/report.html'>report</a>" if rep.exists()
                else f"<span class='mute'>run report.py {esc(r['run'])}</span>")
        t = (res or {}).get("final_tally", {})
        rows.append(
            f"<tr class='{o.lower()}'><td>{esc(r['title'])}<div class='mute'>{esc(r['run'])}</div></td>"
            f"<td>{esc(str(r['crime']))}</td>"
            f"<td>{'guilty' if r['truth'] else 'innocent' if r['truth'] is False else '?'}</td>"
            f"<td><b>{esc(verdict_of(r))}</b><div class='mute'>{t.get('guilty', '-')}G / "
            f"{t.get('not guilty', '-')}NG</div></td>"
            f"<td class='o'>{o}</td><td>{res['rounds'] if res else '-'}</td>"
            f"<td>{lies(r, 'prosecutor')}</td><td>{lies(r, 'defense attorney')}</td>"
            f"<td>{lies(r, 'witness')}</td><td>{r['objections']}/{r['sustained']}</td>"
            f"<td>{r['contempt']}</td><td>{r['flips']}</td>"
            f"<td>{(res or {}).get('minutes', '-')}</td>"
            f"<td>{f'{tokens_of(r):,}' if tokens_of(r) else '-'}</td><td>{link}</td></tr>")

    # ---------------- lies section
    BADGE = {"fake": ("FAKE EXHIBIT", "#e5484d"), "spin": ("PLANNED SPIN", "#f5a524"),
             "admitted": ("ADMITTED", "#d6409f"), "caught": ("CAUGHT", "#b42318")}
    cards, trials_opt, speakers = [], [], set()
    for ti, r in enumerate(done):
        trials_opt.append(f"<option value='t{ti}'>{esc(r['title'])} ({esc(r['run'][-6:])})</option>")
        for l in r["lies"]:
            speakers.add(l["role"])
            flags = [f for f in l["flags"] if f in BADGE]
            badges = "".join(f"<span class='badge' style='--b:{BADGE[f][1]}'>{BADGE[f][0]}</span>"
                             for f in flags)
            if l["planted"]:
                badges += "<span class='badge' style='--b:#6e56cf'>PLANTED LIAR</span>"
            said = esc(l["said"])
            short = said if len(said) <= 320 else said[:320] + "&hellip;"
            said_html = (f"<p>{short}</p>" if len(said) <= 320 else
                         f"<details><summary>{short}</summary><p>{said}</p></details>")
            rows_ = []
            if l["planned"]:
                rows_.append(f"<div class='kv hl'><span class='k'>planned (private)</span>"
                             f"<span class='v'>{esc(l['planned'])}</span></div>")
            if l["said"]:
                rows_.append(f"<div class='kv'><span class='k'>said ({'jury room' if l['phase'] == 'deliberation' else 'in court'})</span>"
                             f"<span class='v'>{said_html}</span></div>")
            if l["fake"]:
                rows_.append(f"<div class='kv'><span class='k'>fake exhibits</span>"
                             f"<span class='v warn'>{esc(', '.join(l['fake']))}</span></div>")
            if l["admitted"]:
                rows_.append(f"<div class='kv hl'><span class='k'>admitted (private)</span>"
                             f"<span class='v'>{esc(l['admitted'])}</span></div>")
            if l["crossed"]:
                rows_.append(f"<div class='kv hl'><span class='k'>lines crossed</span>"
                             f"<span class='v'>{esc(l['crossed'])}</span></div>")
            if l["truth_known"]:
                rows_.append(f"<div class='kv'><span class='k'>what they really knew</span>"
                             f"<span class='v mute'>{esc(l['truth_known'])}</span></div>")
            color = report.ROLE_COLORS.get(l["role"], "#9aa0a6")
            cards.append(
                f"<article class='turn lie' style='--c:{color}' data-trial='t{ti}' "
                f"data-role='{esc(l['role'])}' data-flags='{' '.join(flags)}' "
                f"data-planted='{int(l['planted'])}'><header>"
                f"<span class='who'>{esc(l['speaker'])}</span><span class='role'>{esc(l['role'])}</span>"
                f"<span class='phase'>{esc(l['phase'])} &middot; {esc(r['title'])}</span>{badges}</header>"
                f"<section>{''.join(rows_)}</section></article>")
    role_opt = "".join(f"<option>{esc(x)}</option>" for x in sorted(speakers))
    lies_html = f"""<h2 id='lies'>Lies &amp; stretches ({len(cards)})</h2>
<div class='filters'>
<select id='fTrial'><option value=''>All trials</option>{''.join(trials_opt)}</select>
<select id='fRole'><option value=''>All roles</option>{role_opt}</select>
<select id='fFlag'><option value=''>Any flag</option><option value='fake'>Fake exhibit</option>
<option value='spin'>Planned spin</option><option value='admitted'>Admitted</option>
<option value='caught'>Caught by objection</option></select>
<label><input type='checkbox' id='fPlanted'> Planted liars only</label>
<span id='fCount' class='mute'></span></div>
{''.join(cards) or "<p class='sub'>No lies flagged yet.</p>"}
<script>
(function(){{
  var ids=['fTrial','fRole','fFlag','fPlanted'];
  function apply(){{
    var t=document.getElementById('fTrial').value, r=document.getElementById('fRole').value,
        f=document.getElementById('fFlag').value, p=document.getElementById('fPlanted').checked, n=0;
    document.querySelectorAll('article.lie').forEach(function(el){{
      var ok=(!t||el.dataset.trial===t)&&(!r||el.dataset.role===r)&&
             (!f||(' '+el.dataset.flags+' ').indexOf(' '+f+' ')>=0)&&(!p||el.dataset.planted==='1');
      el.style.display=ok?'':'none'; if(ok)n++;
    }});
    document.getElementById('fCount').textContent=n+' shown';
  }}
  ids.forEach(function(i){{document.getElementById(i).addEventListener('change',apply);}});
  apply();
}})();
</script>"""

    stat = lambda big, small: f"<div class='stat'><b>{big}</b>{small}</div>"
    page = f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>TrialSim Summary</title>
<style>{report.CSS}
.wrap{{overflow-x:auto}} table.sum{{border-collapse:collapse;width:100%;background:var(--card);font-size:14px}}
.sum th,.sum td{{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top;white-space:nowrap}}
.sum th{{font-size:12px;color:var(--mute)}} .mute{{color:var(--mute);font-size:12px}}
tr.right td.o{{color:#30a46c;font-weight:700}} tr.wrong td.o{{color:#e5484d;font-weight:700}}
tr.hung td.o{{color:#9aa0a6;font-weight:700}}
.filters{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0 12px}}
.filters select{{padding:6px 8px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg)}}
.lie details summary{{font-weight:400;cursor:pointer}} .lie details{{border:0;padding:0;margin:0;background:none}}
</style></head><body><main>
<h1>TrialSim Summary</h1><p class='sub'>{n} completed trial(s)</p>
<div class='stats'>
{stat(pct(right, len(decided)), 'jury accuracy')}
{stat(verdicts['guilty'], 'guilty')}{stat(verdicts['not guilty'], 'not guilty')}{stat(verdicts['hung'], 'hung')}
{stat(f"{wrongful_conv}/{len(innocent_cases)}", 'wrongful convictions')}
{stat(f"{wrongful_acq}/{len(guilty_cases)}", 'wrongful acquittals')}
{stat(pros_lies, 'prosecutor lies')}{stat(def_lies, 'defense lies')}{stat(wit_lies, 'witness lies')}
{stat(sum(r['contempt'] for r in done), 'contempt warnings')}
</div>
<div class='wrap'><table class='sum'><tr><th>Case</th><th>Crime</th><th>Truth</th><th>Verdict</th>
<th>Result</th><th>Rounds</th><th>Pros lies</th><th>Def lies</th><th>Wit lies</th><th>Obj/Sust</th>
<th>Contempt</th><th>Jurors flipped</th><th>Min</th><th>Tokens</th><th></th></tr>{''.join(rows)}</table></div>
<p class='sub'>"Lies" = turns flagged as planned spin, admitted stretch, or fake exhibit (self-reported).
<a href='#lies'>Jump to every lie &darr;</a></p>
{lies_html}
</main></body></html>"""
    out = RUNS_DIR / "summary.html"
    out.write_text(page, encoding="utf-8")
    print(f"\nSaved: {out}")
    if a.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
