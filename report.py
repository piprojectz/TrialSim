"""
TrialSim - Report: turn a run's transcript into an HTML page.

Each turn is shown as  BEFORE (private)  ->  SAID IN COURT (public)  ->  AFTER (private),
with flags:
    FAKE      cited an exhibit that doesn't exist in the locker
    SPIN      pre-thought admitted planning to exaggerate / downplay / hide
    ADMITTED  post-thought honesty_check confessed to stretching the truth
    CLEAN     none of the above

The page opens with a case summary (charge, defendant, victim, evidence locker,
witnesses). With --reveal, a collapsed spoiler panel adds the ground truth.

Run `python report.py -h` for all options. See CHEATSHEET.md.
"""
import argparse
import html
import json
import re
import webbrowser
from pathlib import Path

BASE = Path(__file__).parent
RUNS_DIR = BASE / "runs"
CASES_DIR = BASE / "cases"

ROLE_COLORS = {
    "prosecutor": "#e5484d", "defense attorney": "#3e8ef7", "judge": "#a78bfa",
    "juror": "#9aa0a6", "defendant": "#f5a524", "witness": "#30a46c",
}
FLAG_STYLE = {
    "fake": ("FAKE EXHIBIT", "#e5484d"),
    "spin": ("PLANNED SPIN", "#f5a524"),
    "admitted": ("ADMITTED STRETCH", "#d6409f"),
    "caught": ("OBJECTION SUSTAINED", "#b42318"),
    "clean": ("CLEAN", "#30a46c"),
}
HIGHLIGHT_KEYS = {"spin_or_hide", "honesty_check"}

esc = html.escape


# ---------------------------------------------------------------- helpers
def s(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(map(str, v))
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def is_nothing(v):
    t = s(v).strip().lower().rstrip(".!")
    return t in {"", "nothing", "none", "n/a", "na", "no"} or (
        t.startswith(("nothing", "none")) and len(t) < 40)


def data_of(rec):
    d = (rec or {}).get("data")
    return d if isinstance(d, dict) else {}


def find_run(arg):
    if arg:
        p = Path(arg)
        return p.parent if p.is_file() else p
    if not RUNS_DIR.exists():
        raise SystemExit("No runs folder yet. Run trial.py first.")
    runs = [d for d in RUNS_DIR.iterdir() if (d / "transcript.jsonl").exists()]
    if not runs:
        raise SystemExit("No runs found. Run trial.py first.")
    return max(runs, key=lambda d: d.stat().st_mtime)


def load_case(run_dir):
    case_id = run_dir.name.rsplit("_", 1)[0]
    f = CASES_DIR / f"{case_id}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def load_result(run_dir):
    f = run_dir / "result.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


# ---------------------------------------------------------------- turns
def new_turn(r):
    return {"speaker": r["speaker"], "role": r["role"], "phase": r["phase"],
            "pre": None, "stmt": None, "post": None}


def build_turns(recs):
    turns, leanings, open_turn = [], [], {}
    for r in recs:
        kind, who = r.get("kind"), r.get("speaker")
        if kind in ("leaning", "ballot"):
            leanings.append(r)
        elif kind == "tally":
            continue
        elif kind in ("pre_thought", "objection_thought"):
            t = new_turn(r)
            t["pre"] = r
            turns.append(t)
            open_turn[who] = t
        elif kind == "post_thought":
            t = None if r.get("phase") == "verdict" else next(
                (x for x in reversed(turns) if x["speaker"] == who and x["stmt"] and not x["post"]), None)
            if not t:
                t = new_turn(r)
                turns.append(t)
            t["post"] = r
        elif r.get("channel") in ("public", "jury"):   # statement, objection, ruling, jury room...
            t = open_turn.get(who)
            if not t or t["stmt"]:
                t = new_turn(r)
                turns.append(t)
            t["stmt"] = r
            if kind == "ruling" and r.get("sustained") and r.get("against"):
                tgt = next((x for x in reversed(turns)
                            if x["speaker"] == r["against"] and x["stmt"]), None)
                if tgt:
                    tgt["caught"] = True
        else:                                        # any other private note
            t = new_turn(r)
            t["pre"] = r
            turns.append(t)

    for t in turns:
        f = []
        if t["stmt"] and t["stmt"].get("invalid_exhibits"):
            f.append("fake")
        if t["pre"] and not is_nothing(data_of(t["pre"]).get("spin_or_hide")):
            f.append("spin")
        if t["post"] and not is_nothing(data_of(t["post"]).get("honesty_check")):
            f.append("admitted")
        if t.get("caught"):
            f.append("caught")
        t["flags"] = f or ["clean"]
    return turns, leanings


def keep(t, a):
    if a.speaker and a.speaker.lower() not in t["speaker"].lower():
        return False
    if a.role and a.role.lower() not in t["role"].lower():
        return False
    if a.phase and a.phase.lower() != t["phase"].lower():
        return False
    fl = t["flags"]
    if a.lies and fl == ["clean"]:
        return False
    if a.truths and fl != ["clean"]:
        return False
    for name in ("fake", "spin", "admitted", "caught"):
        if getattr(a, name) and name not in fl:
            return False
    return True


# ---------------------------------------------------------------- rendering
def render_kv(d):
    if not isinstance(d, dict):
        return f"<p>{esc(s(d))}</p>"
    rows = []
    for k, v in d.items():
        hl = " hl" if k in HIGHLIGHT_KEYS and not is_nothing(v) else ""
        rows.append(f"<div class='kv{hl}'><span class='k'>{esc(k.replace('_', ' '))}</span>"
                    f"<span class='v'>{esc(s(v))}</span></div>")
    return "".join(rows)


def badges(flags):
    return "".join(f"<span class='badge' style='--b:{FLAG_STYLE[f][1]}'>{FLAG_STYLE[f][0]}</span>"
                   for f in flags)


def render_turn(t, a):
    color = ROLE_COLORS.get(t["role"], "#9aa0a6")
    parts = [f"<article class='turn' style='--c:{color}'><header>"
             f"<span class='who'>{esc(t['speaker'])}</span>"
             f"<span class='role'>{esc(t['role'])}</span>"
             f"<span class='phase'>{esc(t['phase'])}</span>{badges(t['flags'])}</header>"]
    if t["pre"] and not a.public_only:
        parts.append(f"<section class='private'><h4>Before &middot; private</h4>"
                     f"{render_kv(t['pre'].get('data'))}</section>")
    if t["stmt"] and not a.thoughts_only:
        fake = t["stmt"].get("invalid_exhibits")
        warn = (f"<p class='warn'>Cited exhibits that don't exist: {esc(', '.join(fake))}</p>"
                if fake else "")
        text = esc(t["stmt"].get("text", ""))
        text = re.sub(r"\b(Exhibit\s+E?\d+|E\d+)\b", r"<mark>\1</mark>", text)
        where = "Said in the jury room" if t["stmt"].get("channel") == "jury" else "Said in court"
        parts.append(f"<section class='public'><h4>{where}</h4>"
                     f"<p>{text}</p>{warn}</section>")
    if t["post"] and not a.public_only:
        parts.append(f"<section class='private'><h4>After &middot; private</h4>"
                     f"{render_kv(t['post'].get('data'))}</section>")
    parts.append("</article>")
    return "".join(parts)


def r_is_ballot(juror, col_name, leanings):
    return str(col_name).startswith("ballot")


def render_summary(case, result):
    """Spoiler-free case summary: what a juror would know walking into the courtroom."""
    if not case:
        return ""
    pub, gt = case.get("public", {}), case.get("ground_truth", {})

    def row(label, value):
        return (f"<div class='kv'><span class='k'>{esc(label)}</span>"
                f"<span class='v'>{value}</span></div>") if value else ""

    d = gt.get("defendant")
    if isinstance(d, dict):
        name = s(d.get("name") or pub.get("defendant"))
        facts = ", ".join(x for x in (f"age {s(d['age'])}" if d.get("age") else "",
                                      s(d.get("occupation"))) if x)
        defendant = esc(name) + (f" &middot; {esc(facts)}" if facts else "")
        if d.get("background"):
            defendant += f"<br><span class='bg'>{esc(s(d['background']))}</span>"
    else:
        defendant = esc(s(pub.get("defendant") or d))

    v = gt.get("victim")
    if isinstance(v, dict):
        victim = esc(s(v.get("name") or pub.get("victim")))
        if v.get("description"):
            victim += f"<br><span class='bg'>{esc(s(v['description']))}</span>"
    else:
        victim = esc(s(pub.get("victim") or v))

    counsel = ""
    if result:
        p, df = result.get("prosecutor") or {}, result.get("defense") or {}
        counsel = (row("Prosecution", f"{esc(s(p.get('name')))} <span class='bg'>"
                                      f"({esc(s(p.get('model')))})</span>") +
                   row("Defense", f"{esc(s(df.get('name')))} <span class='bg'>"
                                  f"({esc(s(df.get('model')))})</span>"))

    locker = "".join(f"<li><b>{esc(s(e.get('id')))}</b> {esc(s(e.get('name')))}"
                     f"<br><span class='bg'>{esc(s(e.get('description')))}</span></li>"
                     for e in case.get("evidence_locker", []))
    wits = "".join(f"<li><b>{esc(s(w.get('name')))}</b> &middot; {esc(s(w.get('role')))}"
                   f"{' &middot; <span class=bg>called by the ' + esc(s(w.get('called_by'))) + '</span>' if w.get('called_by') else ''}"
                   f"</li>" for w in gt.get("witnesses", []))

    return ("<div class='case'><h2>The case</h2>"
            + row("Crime", esc(s(gt.get("crime"))))
            + row("Charge", esc(s(pub.get("charge"))))
            + row("Location", esc(s(pub.get("location"))))
            + row("Defendant", defendant)
            + row("Victim", victim)
            + counsel
            + (f"<h4>Evidence locker</h4><ul>{locker}</ul>" if locker else "")
            + (f"<h4>Witnesses</h4><ul>{wits}</ul>" if wits else "")
            + "</div>")


def render_verdict(result):
    if not result:
        return "<p class='sub'>No verdict yet (trial incomplete or run before deliberation existed).</p>"
    v = result["verdict"].upper()
    color = {"GUILTY": "#e5484d", "NOT GUILTY": "#3e8ef7"}.get(v, "#9aa0a6")
    t = result.get("final_tally", {})
    return (f"<div class='verdict' style='--v:{color}'><div class='vlabel'>Verdict</div>"
            f"<div class='vword'>{esc(v)}</div><div class='sub'>"
            f"{t.get('guilty', 0)} guilty &middot; {t.get('not guilty', 0)} not guilty &middot; "
            f"{result.get('rounds')} ballot round(s) &middot; {result.get('minutes')} min"
            f"</div></div>")


def render_jury(leanings, a):
    if not leanings or a.no_jury:
        return ""
    col = lambda r: r.get("moment") or r["phase"]
    phases = list(dict.fromkeys(col(r) for r in leanings))
    jurors = list(dict.fromkeys(r["speaker"] for r in leanings))
    if a.speaker:
        jurors = [j for j in jurors if a.speaker.lower() in j.lower()]
    grid = {(r["speaker"], col(r)): data_of(r) for r in leanings}

    head = "".join(f"<th>{esc(p)}</th>" for p in phases)
    rows = []
    for j in jurors:
        cells = []
        for p in phases:
            d = grid.get((j, p))
            if not d:
                cells.append("<td></td>")
                continue
            lean = s(d.get("leaning")).lower()
            cls = "g" if lean.startswith("guilty") else "ng" if "not" in lean else "u"
            if r_is_ballot(j, p, leanings):
                cls += " ballot"
            cells.append(f"<td class='{cls}' title='{esc(s(d.get('why')))}'>"
                         f"{esc(lean)}<small>{esc(s(d.get('confidence')))}%</small></td>")
        rows.append(f"<tr><th>{esc(j)}</th>{''.join(cells)}</tr>")

    details = "".join(
        f"<div class='kv'><span class='k'>{esc(r['speaker'])} &middot; {esc(col(r))}</span>"
        f"<span class='v'>{esc(s(data_of(r).get('why')))} "
        f"<em>Would change mind if: {esc(s(data_of(r).get('what_would_change_my_mind')))}</em>"
        f"</span></div>"
        for r in leanings if r["speaker"] in jurors)
    return (f"<h2>Jury, private leanings</h2><table class='jury'><tr><th></th>{head}</tr>"
            f"{''.join(rows)}</table><details><summary>Juror reasoning</summary>"
            f"{details}</details>")


def render_truth(case, result):
    gt = case["ground_truth"]
    ev = "".join(
        f"<div class='kv'><span class='k'>{esc(e.get('id', ''))} {esc(e.get('name', ''))}</span>"
        f"<span class='v'>points toward <b>{esc(s(e.get('points_toward')))}</b> &middot; "
        f"{esc(s(e.get('truth_note')))}</span></div>" for e in gt.get("evidence", []))
    wit = "".join(
        f"<div class='kv'><span class='k'>{esc(w.get('name', ''))}</span>"
        f"<span class='v'>{esc(s(w.get('knows')))} &middot; bias: {esc(s(w.get('bias')))}"
        f"{' &middot; <b>WILL LIE</b>: ' + esc(s(w.get('lie_reason'))) if w.get('will_lie') else ''}"
        f"</span></div>" for w in gt.get("witnesses", []))
    verdict = "GUILTY" if case.get("guilty") else "INNOCENT"
    right = ""
    if result:
        right = {True: "The jury got it RIGHT.", False: "The jury got it WRONG.",
                 None: "The jury hung, so no verdict to score."}.get(result.get("correct"), "")
    return (f"<details class='truth'><summary>Ground truth (spoiler)</summary>"
            f"<p><b>Defendant is {verdict}.</b> {esc(right)} Actual perpetrator: "
            f"{esc(s(gt.get('actual_perpetrator')))}</p>"
            f"<p>{esc(s(gt.get('what_really_happened')))}</p>"
            f"<h4>Evidence</h4>{ev}<h4>Witnesses</h4>{wit}</details>")


CSS = """
:root{--bg:#f6f6f4;--card:#fff;--fg:#1c1c1c;--mute:#6b6b6b;--line:#e3e3e0;--priv:#f1efe9}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1d1d1d;--fg:#ececec;--mute:#9a9a9a;--line:#2e2e2e;--priv:#232120}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,Segoe UI,sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px 80px}
h1{margin:0 0 4px;font-size:24px}h2{margin:36px 0 12px;font-size:18px}
.sub{color:var(--mute);margin:0 0 16px}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.stat b{font-size:18px;display:block}
.turn{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:8px;margin:14px 0;overflow:hidden}
.turn header{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--line)}
.who{font-weight:700}.role,.phase{color:var(--mute);font-size:13px}
.badge{font-size:11px;font-weight:700;letter-spacing:.03em;color:#fff;background:var(--b);border-radius:4px;padding:2px 6px}
section{padding:10px 14px}section h4{margin:0 0 6px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute)}
.private{background:var(--priv);border-top:1px dashed var(--line)}
.public p{margin:0;white-space:pre-wrap}
mark{background:#3e8ef733;color:inherit;border-radius:3px;padding:0 2px}
.kv{display:grid;grid-template-columns:170px 1fr;gap:10px;padding:3px 0}
.kv .k{color:var(--mute);font-size:13px}.kv.hl .v{background:#f5a52422;border-radius:4px;padding:0 4px}
.warn{color:#e5484d;font-weight:600;margin-top:6px!important}
table.jury{border-collapse:collapse;width:100%;background:var(--card);border-radius:8px;overflow:hidden}
.jury th,.jury td{border:1px solid var(--line);padding:6px 10px;text-align:left;font-size:14px}
.jury td small{color:var(--mute);margin-left:6px}
.jury td.g{background:#e5484d22}.jury td.ng{background:#3e8ef722}.jury td.u{background:#9aa0a622}
details{margin:12px 0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px}
summary{cursor:pointer;font-weight:600}
.truth{border-color:#d6409f}
.verdict{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--v);border-radius:8px;padding:14px 16px;margin:12px 0}
.vlabel{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute)}
.vword{font-size:28px;font-weight:800;color:var(--v)}
.jury td.ballot{font-weight:700}
.case{background:var(--card);border:1px solid var(--line);border-left:6px solid #a78bfa;border-radius:8px;padding:14px 16px;margin:16px 0}
.case h2{margin:0 0 10px}
.case h4{margin:16px 0 6px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute)}
.case ul{margin:0;padding-left:18px}.case li{margin:5px 0}
.bg{color:var(--mute);font-size:13px}
@media (max-width:600px){.kv{grid-template-columns:1fr;gap:0}}
"""


def main():
    ap = argparse.ArgumentParser(description="Render a TrialSim run as HTML.")
    ap.add_argument("run", nargs="?", help="run folder or transcript.jsonl (default: newest run)")
    ap.add_argument("--speaker", help="only this speaker (partial match, e.g. Hale)")
    ap.add_argument("--role", help="only this role (prosecutor, defense, judge, juror...)")
    ap.add_argument("--phase", help="only this phase (opening, testimony, closing, instructions, deliberation, verdict)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--lies", action="store_true", help="only turns with ANY flag (fake/spin/admitted)")
    g.add_argument("--truths", action="store_true", help="only CLEAN turns")
    g.add_argument("--fake", action="store_true", help="only turns citing non-existent exhibits")
    g.add_argument("--spin", action="store_true", help="only turns that planned spin beforehand")
    g.add_argument("--admitted", action="store_true", help="only turns whose honesty check confessed")
    g.add_argument("--caught", action="store_true", help="only questions struck by a sustained objection")
    v = ap.add_mutually_exclusive_group()
    v.add_argument("--thoughts-only", action="store_true", help="hide public statements")
    v.add_argument("--public-only", action="store_true", help="hide private thoughts")
    ap.add_argument("--no-jury", action="store_true", help="hide the jury leanings table")
    ap.add_argument("--no-summary", action="store_true", help="hide the case summary at the top")
    ap.add_argument("--reveal", action="store_true", help="include the ground truth (collapsed spoiler)")
    ap.add_argument("--out", help="output file (default: <run>/report[_filters].html)")
    ap.add_argument("--open", action="store_true", help="open in your browser when done")
    a = ap.parse_args()

    run_dir = find_run(a.run)
    recs = [json.loads(l) for l in
            (run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    case = load_case(run_dir)
    result = load_result(run_dir)
    turns, leanings = build_turns(recs)
    shown = [t for t in turns if keep(t, a)]

    count = lambda f: sum(f in t["flags"] for t in turns)
    title = case["public"]["title"] if case else run_dir.name
    objections = sum(r.get("kind") == "objection" for r in recs)
    sustained = sum(r.get("kind") == "ruling" and bool(r.get("sustained")) for r in recs)
    outbursts = sum(r.get("kind") == "outburst" for r in recs)
    contempt = sum(r.get("kind") == "contempt_warning" for r in recs)
    filters = [n for n in ("lies", "truths", "fake", "spin", "admitted", "caught",
                           "thoughts_only", "public_only") if getattr(a, n)]
    filters += [f"{k}={getattr(a, k)}" for k in ("speaker", "role", "phase") if getattr(a, k)]

    body = [f"<main><h1>{esc(title)}</h1>",
            f"<p class='sub'>{esc(run_dir.name)}"
            f"{' &middot; filters: ' + esc(', '.join(filters)) if filters else ''}</p>"]
    if not a.no_summary:
        body.append(render_summary(case, result))
    body.append("<div class='stats'>"
                f"<div class='stat'><b>{len(turns)}</b>turns</div>"
                f"<div class='stat'><b>{count('fake')}</b>fake exhibits</div>"
                f"<div class='stat'><b>{count('spin')}</b>planned spin</div>"
                f"<div class='stat'><b>{count('admitted')}</b>admitted stretches</div>"
                f"<div class='stat'><b>{objections}</b>objections</div>"
                f"<div class='stat'><b>{sustained}</b>sustained</div>"
                f"<div class='stat'><b>{outbursts}</b>judge outbursts</div>"
                f"<div class='stat'><b>{contempt}</b>contempt warnings</div>"
                f"<div class='stat'><b>{count('clean')}</b>clean</div></div>")
    body.append(render_verdict(result))
    if a.reveal and case:
        body.append(render_truth(case, result))
    body.append(f"<h2>Turns ({len(shown)} shown)</h2>")
    body += [render_turn(t, a) for t in shown] or ["<p class='sub'>No turns match.</p>"]
    body.append(render_jury(leanings, a))
    body.append("</main>")

    page = (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head>"
            f"<body>{''.join(body)}</body></html>")

    suffix = "".join("_" + re.sub(r"[^\w]+", "-", f) for f in filters)
    out = Path(a.out) if a.out else run_dir / f"report{suffix}.html"
    out.write_text(page, encoding="utf-8")
    print(f"{len(shown)}/{len(turns)} turns -> {out}")
    if a.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
