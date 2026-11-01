"""
TrialSim - Step 1b: Cast Generator (jury + witness personalities)
-----------------------------------------------------------------
Runs after case_gen.py. Adds two things to the case file:
  "jury":            6 generated jurors (name, age, job, background, predisposition,
                     how they reason, how they talk). Jurors only see public case info.
  witness "voice":   personality / speech style for each existing witness, so they
                     don't all sound alike. Their facts, bias and lies are untouched.

trial.py uses these if present, otherwise it falls back to the fixed juror personas.

Usage:
    python cast_gen.py                   # newest case in cases/
    python cast_gen.py cases/case_X.json # specific case
    python cast_gen.py --redraw          # replace an existing jury / voices
    python cast_gen.py --jury-only       # skip witness voices
    python cast_gen.py --show            # print the cast and exit
"""
import argparse
import json
import random
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gpt-oss:20b"
CASES_DIR = Path(__file__).parent / "cases"
NUM_JURORS = 6
MAX_RETRIES = 5

SYSTEM = ("You are a casting director for a courtroom simulation. You write fictional, "
          "varied, believable ordinary people. You output ONLY valid JSON.")

JURY_SCHEMA = """{
  "jurors": [
    {"name": "", "age": 0, "occupation": "", "background": "1-2 sentences of life story",
     "predisposition": "pro-prosecution | pro-defense | neutral",
     "why": "the life experience behind that predisposition",
     "reasoning_style": "how they weigh evidence (e.g. trusts documents, goes with gut)",
     "speech": "how they talk in the jury room (vocabulary, tone, quirks)"}
  ]
}"""

VOICE_SCHEMA = """{
  "voices": [
    {"number": 1, "name": "witness name", "demeanor": "how they come across on the stand",
     "speech": "how they talk (vocabulary, pace, quirks, regional flavor)"}
  ]
}"""


def ask(prompt, temperature=1.0):
    body = {"model": MODEL, "stream": False, "format": "json",
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}],
            "options": {"temperature": temperature, "seed": random.randint(1, 2**31 - 1),
                        "num_ctx": 16384, "num_predict": 6000}}
    if "gpt-oss" in MODEL:
        body["think"] = "low"
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())["message"]["content"]


def parse(raw):
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b > a:
            return json.loads(text[a:b + 1])
        raise


def case_people(case):
    """Everyone already in the case, so jurors can't share a name with them."""
    gt = case["ground_truth"]
    names = [case["public"]["defendant"], case["public"]["victim"],
             str(gt.get("actual_perpetrator", ""))]
    names += [w["name"] for w in gt.get("witnesses", [])]
    return [n for n in names if n]


def jury_prompt(case, feedback=""):
    pub = case["public"]
    p = f"""Cast a jury of {NUM_JURORS} for this trial. They are ordinary residents of the area.

CASE: {pub['title']}
CHARGE: {pub['charge']}
LOCATION: {pub['location']}

Requirements:
- Exactly {NUM_JURORS} jurors with clearly different ages (21-75), jobs, backgrounds and voices.
- Mix of predispositions: at least one pro-prosecution, at least one pro-defense, the rest neutral.
- Each predisposition must come from a concrete life experience, not a stereotype.
- They passed jury selection: none of them knows anyone in the case.
- Do NOT use any of these names: {', '.join(case_people(case))}
- Use realistic, varied names. Nobody is a lawyer, judge or police officer.

Return JSON in exactly this shape:
{JURY_SCHEMA}
"""
    if feedback:
        p += f"\nYour previous attempt had problems, fix them:\n{feedback}\n"
    return p


def check_jury(data, case):
    jurors = data.get("jurors", []) if isinstance(data, dict) else []
    errs = []
    if len(jurors) != NUM_JURORS:
        errs.append(f"need exactly {NUM_JURORS} jurors, got {len(jurors)}")
    names = [str(j.get("name", "")).strip().lower() for j in jurors]
    if len(set(names)) != len(names) or "" in names:
        errs.append("every juror needs a unique name")
    taken = {n.lower().split()[-1] for n in case_people(case) if n.split()}
    clash = [j["name"] for j in jurors if str(j.get("name", "")).lower().split()[-1:] and
             str(j["name"]).lower().split()[-1] in taken]
    if clash:
        errs.append(f"jurors share a last name with someone in the case: {', '.join(clash)}")
    leans = [str(j.get("predisposition", "")).lower() for j in jurors]
    if not any("prosecution" in l for l in leans) or not any("defense" in l for l in leans):
        errs.append("need at least one pro-prosecution AND one pro-defense juror")
    return jurors, errs


def voice_prompt(case, feedback=""):
    wits = "\n".join(f"{i}. {w['name']}: {w.get('role', '')}. Bias: {w.get('bias', '')}"
                     for i, w in enumerate(case["ground_truth"]["witnesses"], 1))
    p = f"""Give each witness in this trial a distinct personality and way of speaking on the stand.
Do not change what they know or their bias; only HOW they come across and talk.

CASE: {case['public']['title']} ({case['public']['location']})
WITNESSES:
{wits}

Return one entry per witness, in the same order, with the same number.
Make them sound clearly different from each other through temperament, vocabulary and pace. Keep it grounded. Don't guess an accent or language from someone's name or ethnicity; only give a regional accent if their role or background in the case calls for it.

Return JSON in exactly this shape:
{VOICE_SCHEMA}
"""
    if feedback:
        p += f"\nYour previous attempt had problems, fix them:\n{feedback}\n"
    return p


def check_voices(data, case):
    """Match voices to witnesses by number (names from case_gen can be messy)."""
    voices = data.get("voices", []) if isinstance(data, dict) else []
    n = len(case["ground_truth"]["witnesses"])
    by_num = {}
    for i, v in enumerate(voices, 1):
        try:
            k = int(v.get("number", i))
        except (TypeError, ValueError):
            k = i
        by_num[k] = v
    missing = [k for k in range(1, n + 1) if k not in by_num]
    if missing:
        return None, [f"need one voice per witness; missing numbers: {missing}"]
    return [by_num[k] for k in range(1, n + 1)], []


def generate(label, make_prompt, check, case):
    feedback = ""
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"  {label}: attempt {attempt}...")
        try:
            out, errs = check(parse(ask(make_prompt(case, feedback))), case)
        except json.JSONDecodeError as e:
            out, errs = None, [f"Invalid JSON: {e}. Return ONLY the JSON object."]
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"    Ollama error ({e}); retrying...")
            continue
        if not errs:
            return out
        feedback = "\n".join(f"- {e}" for e in errs)
        print(f"    failed:\n{feedback}")
    sys.exit(f"\n*** FAILED: no valid {label} after {MAX_RETRIES} attempts. Nothing saved. ***")


def show(case):
    print(f"\n=== {case['public']['title']} ===")
    for i, j in enumerate(case.get("jury", []), 1):
        print(f"  Juror {i}: {j['name']}, {j['age']}, {j['occupation']} "
              f"[{j['predisposition']}]\n           {j['why']}")
    if not case.get("jury"):
        print("  (no generated jury; trial.py will use the fixed personas)")
    for w in case["ground_truth"]["witnesses"]:
        if w.get("voice"):
            print(f"  Witness {w['name']}: {w['voice']}")


def main():
    ap = argparse.ArgumentParser(description="Generate jurors and witness voices for a case.")
    ap.add_argument("case", nargs="?", help="case file (default: newest in cases/)")
    ap.add_argument("--redraw", action="store_true", help="replace existing jury / voices")
    ap.add_argument("--jury-only", action="store_true", help="skip witness voices")
    ap.add_argument("--show", action="store_true", help="print the cast and exit")
    a = ap.parse_args()

    if a.case:
        f = Path(a.case)
    else:
        files = sorted(CASES_DIR.glob("case_*.json"))
        if not files:
            sys.exit("No cases found. Run case_gen.py first.")
        f = files[-1]
    case = json.loads(f.read_text(encoding="utf-8"))
    print(f"Case file: {f.name}")

    if a.show:
        show(case)
        return

    if case.get("jury") and not a.redraw:
        print("  jury already exists (use --redraw to replace)")
    else:
        case["jury"] = generate("jury", jury_prompt, check_jury, case)
        f.write_text(json.dumps(case, indent=2, ensure_ascii=False), encoding="utf-8")
        print("  jury saved")

    wits = case["ground_truth"]["witnesses"]
    if not a.jury_only:
        if all(w.get("voice") for w in wits) and not a.redraw:
            print("  witness voices already exist (use --redraw to replace)")
        else:
            voices = generate("witness voices", voice_prompt, check_voices, case)
            for w, v in zip(wits, voices):
                w["voice"] = f"{v.get('demeanor', '')} {v.get('speech', '')}".strip()

    case["cast_model"] = MODEL
    f.write_text(json.dumps(case, indent=2, ensure_ascii=False), encoding="utf-8")
    show(case)
    print(f"\nSaved: {f}")


if __name__ == "__main__":
    main()
