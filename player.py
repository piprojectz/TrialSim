"""
TrialSim - Animated trial player
--------------------------------
Turns a run's transcript into a self-contained HTML page that replays the trial with
paper-cutout characters: speech, private thoughts, objections, the judge's temper,
juror leanings, and the jury room.

Usage:
    python player.py                    # newest run
    python player.py runs\\<folder>      # a specific run
    python player.py --open             # open it in the browser when done
    python player.py --out D:\\x.html    # custom output path
Voice clips made by voices.py (runs/<folder>/audio/) are used automatically.
Output:
    runs/<folder>/player.html
"""
import argparse
import json
import re
import webbrowser
from pathlib import Path

BASE = Path(__file__).parent
RUNS_DIR = BASE / "runs"
CASES_DIR = BASE / "cases"

# must match trial.py so the judge's patience can be rebuilt turn by turn
WORD_LIMITS = {"statement": 150, "question": 30, "answer": 60, "closing": 280, "rebuttal": 130}
TURN_DRAIN, SUSTAINED_DRAIN, OVERRULED_DRAIN, FAKE_EXHIBIT_DRAIN, MAX_DRAIN = 2, 10, 15, 20, 35

ROLE_KEY = {"judge": "judge", "prosecutor": "pros", "defense attorney": "def",
            "witness": "witness", "defendant": "defendant"}
PHASES = ["opening", "testimony", "closing", "instructions", "deliberation", "verdict"]
PHASE_LABEL = {"opening": "Openings", "testimony": "Testimony", "closing": "Closings",
               "instructions": "Instructions", "deliberation": "Deliberation", "verdict": "Verdict"}

POS = ("landed", "effective", "strong", "solid", "nailed", "hit home", "hit 'em", "stuck",
       "powerful", "resonat", "cut through", "pinned", "got the point", "well-placed",
       "managed to", "convinc", "sunk in", "home run", "in my pocket")
NEG = ("not planted", "didn't land", "did not land", "setback", "backfire", "weak", "hurt",
       "damag", "fell flat", "poorly", "sustained", "didn't fully", "did not fully",
       "struggl", "undermin", "worr", "concern", "frustrat")


def find_run(arg):
    if arg:
        return Path(arg)
    runs = sorted((p for p in RUNS_DIR.iterdir() if (p / "transcript.jsonl").exists()),
                  key=lambda p: p.stat().st_mtime)
    if not runs:
        raise SystemExit("No runs found.")
    return runs[-1]


def load_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_case(run_dir, result):
    cid = (result or {}).get("case_id") or re.match(r"(case_\d{8}_\d{6})", run_dir.name)
    cid = cid if isinstance(cid, str) else (cid.group(1) if cid else None)
    return load_json(CASES_DIR / f"{cid}.json") if cid else None


def s(v):
    return "" if v is None else str(v).strip()


def nothing(v):
    return s(v).lower().rstrip(".") in ("", "nothing", "none", "n/a", "no")


def thought_text(kind, d):
    d = d or {}
    if kind == "pre_thought":
        real = next((v for k, v in d.items() if k.startswith("what_i_really")), "")
        out = s(real) or s(d.get("plan"))
        if not nothing(d.get("spin_or_hide")):
            out += f"\n\nPlanning to spin or hide: {s(d['spin_or_hide'])}"
        return out
    if kind == "objection_thought":
        return s(d.get("private_reason")) or s(d.get("what_was_misstated"))
    if "reaction" in d:                                   # verdict reaction
        out = s(d.get("reaction"))
        if not nothing(d.get("lines_i_crossed")):
            out += f"\n\nLines I crossed: {s(d['lines_i_crossed'])}"
        return out
    out = s(d.get("how_it_landed"))
    if s(d.get("what_worries_me")):
        out += f"\n\nWorried about: {s(d['what_worries_me'])}"
    if not nothing(d.get("honesty_check")):
        out += f"\n\nHonesty check: {s(d['honesty_check'])}"
    return out


def sentiment(text):
    t = text.lower()
    return sum(w in t for w in POS) - sum(w in t for w in NEG)


def judge_mood(p):
    return "neutral" if p > 70 else "annoyed" if p > 40 else "angry" if p > 20 else "furious"


def lean_score(state):
    vals = [(c if l == "guilty" else -c if l == "not guilty" else 0) for l, c, _ in state]
    return sum(vals) / max(1, len(vals))


def build(recs, case, result):
    pub = (case or {}).get("public", {})
    title = s(pub.get("title"))
    if not title:
        m = next((re.search(r"State v\.? ([A-Z][\w'-]+(?: [A-Z][\w'-]+)?)", r.get("text") or "")
                  for r in recs if r.get("role") == "judge" and r.get("text")), None)
        title = f"State v. {m.group(1)}" if m else "TrialSim"
    defendant = s(pub.get("defendant")) or title.replace("State v. ", "")
    jury = (case or {}).get("jury") or []

    jurors = sorted({r["speaker"] for r in recs if r.get("role") == "juror"},
                    key=lambda n: int(re.sub(r"\D", "", n) or 0))
    jlabel = {}
    for i, n in enumerate(jurors):
        first = s(jury[i].get("name")).split(" ")[0] if i < len(jury) else ""
        jlabel[n] = f"J{i + 1} · {first}" if first else f"Juror {i + 1}"
    jkey = {n: f"J{i + 1}" for i, n in enumerate(jurors)}

    witnesses = list(dict.fromkeys(r["speaker"] for r in recs if r.get("role") == "witness"))
    beats, polls = [], []
    state = [["none", 0, ""] for _ in jurors]
    polls.append([list(x) for x in state])
    patience, stand, pros_closings, last_objector = 100, None, 0, None
    pending_poll = None

    def key_of(r):
        return jkey.get(r["speaker"]) or ROLE_KEY.get(r.get("role"), "witness")

    def push(b):
        b.setdefault("moods", {})
        b.setdefault("fx", [])
        b.update(pat=patience, stand=stand,
                 standIdx=witnesses.index(stand) if stand in witnesses else 0,
                 jp=len(polls) - 1)
        b["moods"].setdefault("judge", judge_mood(patience))
        beats.append(b)

    def flush_poll():
        nonlocal pending_poll
        if not pending_poll:
            return
        before = lean_score(polls[-1])
        polls.append([list(x) for x in state])
        shift = lean_score(state) - before
        moods = {}
        if abs(shift) >= 5:
            moods = ({"pros": "smirk", "def": "worried", "defendant": "worried"} if shift > 0
                     else {"def": "smirk", "pros": "worried", "defendant": "hopeful"})
        counts = {k: sum(1 for l, _, _ in state if l == k)
                  for k in ("guilty", "not guilty", "undecided")}
        p = pending_poll
        push({"t": "poll", "phase": p["phase"], "scene": p["scene"], "kind": p["label"],
              "moods": moods, "pulse": True, "tally": counts, "ballot": p["ballot"],
              "shift": round(shift, 1)})
        pending_poll = None

    for r in recs:
        kind, ch, role, phase = r.get("kind"), r.get("channel"), r.get("role"), r.get("phase")
        if kind in ("leaning", "ballot"):
            d = r.get("data") or {}
            i = jurors.index(r["speaker"])
            lean = s(d.get("leaning") or d.get("vote")).lower()
            lean = "not guilty" if "not" in lean else "guilty" if "guilt" in lean else "undecided"
            try:
                conf = max(0, min(100, int(float(d.get("confidence", 0)))))
            except (TypeError, ValueError):
                conf = 50
            state[i] = [lean, conf, s(d.get("why"))]
            moment = s(r.get("moment"))
            if not pending_poll:
                pending_poll = {"phase": phase, "ballot": kind == "ballot",
                                "scene": "jury" if kind == "ballot" else "court",
                                "label": (f"Secret ballot: {moment}" if kind == "ballot"
                                          else f"Jurors privately update their leanings ({moment})")}
            continue
        if kind == "tally":
            continue
        flush_poll()

        if ch == "private":
            if role == "juror":
                continue
            text = thought_text(kind, r.get("data"))
            if not text:
                continue
            k = key_of(r)
            mood = "think"
            if kind == "post_thought":
                sc = sentiment(text)
                mood = "smirk" if sc > 0 else "worried" if sc < 0 else "think"
            if role == "witness":
                stand = r["speaker"]
            push({"t": "think", "key": k, "name": r["speaker"], "role": role, "phase": phase,
                  "scene": "court", "kind": "Thinks privately" if kind != "post_thought"
                  else ("Private reaction" if phase == "verdict" else "Private reflection"),
                  "text": text, "moods": {k: mood}})
            continue

        text = s(r.get("text"))
        k = key_of(r)
        if role == "witness":
            stand = r["speaker"]
        b = {"t": "say", "key": k, "name": r["speaker"], "role": role, "phase": phase,
             "scene": "jury" if ch == "jury" else "court", "text": text, "moods": {}, "fx": []}

        # judge's temper, rebuilt with trial.py's formula
        drain = None
        if role != "judge" and kind in ("statement", "question", "answer"):
            limit_kind = kind
            if phase == "closing" and kind == "statement":
                if role == "prosecutor":
                    pros_closings += 1
                    limit_kind = "closing" if pros_closings == 1 else "rebuttal"
                else:
                    limit_kind = "closing"
            over = max(0, len(text.split()) - WORD_LIMITS.get(limit_kind, 150))
            drain = TURN_DRAIN + (over // 10) * 3 + (FAKE_EXHIBIT_DRAIN if r.get("invalid_exhibits") else 0)
            if over > 0:
                b["moods"]["baba"] = "sleep"
            if r.get("invalid_exhibits"):
                b["fx"].append("fake")
        labels = {"question": f"Question to {stand}" if stand else "Question", "answer": "Answer",
                  "objection": "Objection!", "ruling": "Ruling", "outburst": "Outburst",
                  "contempt_warning": "Contempt warning", "allen_charge": "Allen charge",
                  "verdict": "Verdict", "jury_statement": "Jury room"}
        b["kind"] = labels.get(kind) or {"opening": "Opening statement", "closing": "Closing argument",
                                         "instructions": "Jury instructions"}.get(phase, "")
        if kind == "objection":
            b["fx"].append("objection")
            last_objector = k
        elif kind == "ruling":
            sustained = bool(r.get("sustained"))
            b["fx"] += ["gavel", "sustained" if sustained else "overruled"]
            if sustained:
                struck = "pros" if s(r.get("against")) == "Wade Pruitt" else "def"
                b["moods"].update({struck: "shocked", ("def" if struck == "pros" else "pros"): "smirk",
                                   "baba": "cane"})
                drain = TURN_DRAIN + SUSTAINED_DRAIN
            else:
                if last_objector:
                    b["moods"][last_objector] = "annoyed"
                    b["moods"]["pros" if last_objector == "def" else "def"] = "smirk"
                drain = TURN_DRAIN + OVERRULED_DRAIN
        elif kind == "outburst":
            b["fx"].append("shake")
            b["moods"].update({"judge": "furious", "baba": "cane"})
            if r.get("patience") is not None:
                patience = int(r["patience"])
        elif kind == "contempt_warning":
            b["fx"] += ["gavel", "shake", "contempt"]
            patience = 0
            tgt = s(r.get("target"))
            tk = "pros" if tgt == "Wade Pruitt" else "def" if tgt == "Preston Whitmore" else "witness"
            b["moods"].update({"judge": "furious", tk: "shocked", "baba": "cane"})
        elif kind == "verdict":
            v = s((r.get("data") or {}).get("verdict")) or s((result or {}).get("verdict"))
            b["fx"].append("gavel")
            b["moods"].update({"defendant": "happy" if v in ("hung", "not guilty") else "shocked",
                               "pros": "angry" if v != "guilty" else "smirk",
                               "def": "smirk" if v == "not guilty" else "worried" if v == "hung" else "angry"})
            b["verdict"] = v
        elif kind == "allen_charge":
            b["fx"].append("gavel")
        if role == "judge" and kind == "statement":
            m = re.search(r"calls (.+?) to the stand", text)
            if m:
                stand = m.group(1).strip()
            elif "rests. We will now hear closing" in text:
                stand = None

        if drain is not None:
            patience -= min(MAX_DRAIN, drain)
            if patience <= 0 and phase == "closing":
                patience = 5
            patience = max(0, patience)
            b["moods"].setdefault("judge", judge_mood(patience))
        push(b)
        if kind == "contempt_warning":
            patience = 60
    flush_poll()

    verdict = s((result or {}).get("verdict")) or next(
        (s((b.get("verdict"))) for b in beats if b.get("verdict")), "")
    tally = (result or {}).get("final_tally") or (beats and next(
        (b["tally"] for b in reversed(beats) if b.get("tally")), {})) or {}
    end = {"verdict": verdict, "tally": tally}
    if case:
        gt = case.get("ground_truth", {})
        end["truth"] = {"guilty": bool(case.get("guilty")),
                        "perpetrator": s(gt.get("actual_perpetrator")),
                        "story": s(gt.get("what_really_happened"))}
    beats.append({"t": "end", "phase": "verdict", "scene": "court", "moods": {}, "fx": [],
                  "pat": patience, "stand": None, "standIdx": 0, "jp": len(polls) - 1})

    phases = []
    for p in PHASES:
        idx = next((i for i, b in enumerate(beats) if b["phase"] == p), None)
        if idx is not None:
            phases.append({"id": p, "label": PHASE_LABEL[p], "at": idx})

    sub = " · ".join(x for x in (s(pub.get("charge")), f"Victim: {s(pub.get('victim'))}"
                                 if pub.get("victim") else "", s(pub.get("location"))) if x)
    return {"title": title, "sub": sub, "defendant": defendant, "witnesses": witnesses,
            "jurors": [jlabel[n] for n in jurors], "beats": beats, "polls": polls,
            "phases": phases, "end": end}


def main():
    ap = argparse.ArgumentParser(description="Build an animated player for a TrialSim run.")
    ap.add_argument("run", nargs="?", help="run folder (default: newest)")
    ap.add_argument("--open", action="store_true", help="open in the browser")
    ap.add_argument("--out", help="output path (default: <run>/player.html)")
    a = ap.parse_args()
    run_dir = find_run(a.run)
    recs = [json.loads(l) for l in
            (run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    result = load_json(run_dir / "result.json")
    case = load_case(run_dir, result)
    data = build(recs, case, result)
    manifest = load_json(run_dir / "audio" / "manifest.json") or {}
    data["audio"] = {k: f"audio/{v['file']}" for k, v in manifest.items()
                     if (run_dir / "audio" / v["file"]).exists()}
    if data["audio"]:
        print(f"Using {len(data['audio'])} voice clips from {run_dir / 'audio'}")
    template = (BASE / "player_template.html").read_text(encoding="utf-8")
    page = (template.replace("__TITLE__", data["title"])
            .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    out = Path(a.out) if a.out else run_dir / "player.html"
    out.write_text(page, encoding="utf-8")
    print(f"{len(data['beats'])} beats -> {out}")
    if a.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
