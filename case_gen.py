"""
TrialSim - Step 1: Case Generator
---------------------------------
Creates a hidden ground truth (did the defendant do it?), an evidence locker
the lawyers are allowed to cite, and private witness files.

Usage:
    python case_gen.py              # random guilty/innocent
    python case_gen.py --guilty     # force guilty
    python case_gen.py --innocent   # force innocent
    python case_gen.py --reveal     # print the secret truth after generating
    python case_gen.py --clear      # delete all old cases first
    python case_gen.py --list       # list existing cases and exit
    python case_gen.py --crime "art theft"   # choose the crime
"""
import argparse
import json
import random
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gpt-oss:20b"          # case builder: strongest model, runs once per trial
CASES_DIR = Path(__file__).parent / "cases"
MAX_RETRIES = 5

CRIME_TYPES = [
    "burglary", "arson", "embezzlement", "hit-and-run", "assault",
    "art theft", "insurance fraud", "poisoning", "extortion", "vandalism",
    "jewelry heist", "identity theft", "blackmail", "manslaughter",
    "pharmacy robbery", "sabotage", "forgery", "kidnapping", "bribery", "cyber theft",
]

SETTINGS = [
    "a small-town bakery", "a hospital", "a university chemistry lab", "a marina",
    "a ski resort", "a tech startup office", "a vineyard", "a casino", "a farm co-op",
    "a city museum", "an apartment complex", "a high school", "a law firm", "a church",
    "a trucking depot", "a TV station", "a retirement home", "a construction site",
    "a gym", "a music venue", "a dental office", "a public library", "a fishing boat",
]

OCCUPATIONS = [
    "pastry chef", "night-shift nurse", "graduate student", "accountant", "dock worker",
    "real-estate agent", "youth pastor", "IT administrator", "veterinarian", "bartender",
    "museum curator", "retired firefighter", "personal trainer", "pharmacist",
    "city council member", "wedding photographer", "electrician", "high school coach",
]

SYSTEM = (
    "You are a case designer for a courtroom simulation. You write fictional, "
    "internally consistent criminal cases. You output ONLY valid JSON."
)

SCHEMA = """{
  "title": "short case name, e.g. 'State v. Lena Marsh'",
  "crime": "the crime type",
  "location": "town and specific place",
  "charge": "formal charge read in court",
  "defendant": {"name": "", "age": 0, "occupation": "", "background": ""},
  "victim": {"name": "", "description": ""},
  "what_really_happened": "3-6 sentence TRUE account of the events",
  "actual_perpetrator": "full name of who really did it (or 'accident' / 'no crime')",
  "timeline": [{"time": "", "event": ""}],
  "evidence": [
    {"name": "", "description": "what investigators found, stated neutrally",
     "points_toward": "guilt | innocence | ambiguous",
     "truth_note": "why this evidence exists given what REALLY happened"}
  ],
  "witnesses": [
    {"name": "", "role": "relationship to case", "knows": "what they actually saw/know",
     "bias": "their motive or slant", "will_lie": false, "lie_reason": "",
     "called_by": "prosecution | defense (the side whose case this witness helps)"}
  ]
}"""


def ask(prompt: str, temperature: float = 1.0) -> str:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature, "seed": random.randint(1, 2**31 - 1),
                    "num_ctx": 16384, "num_predict": 6000},
    }
    if "gpt-oss" in MODEL:
        body["think"] = "low"          # keep reasoning short so the JSON isn't cut off
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())["message"]["content"]


def past_cases() -> list[str]:
    out = []
    for f in sorted(CASES_DIR.glob("case_*.json"))[-15:]:
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
            out.append(f"{c['public']['title']} ({c['ground_truth'].get('crime', '')})")
        except Exception:
            pass
    return out


def used_names() -> set[str]:
    """Every person's name from ALL earlier cases (defendant, victim, witnesses, culprit)."""
    names = set()
    for f in CASES_DIR.glob("case_*.json"):
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        gt = c.get("ground_truth", {})
        for d in (gt.get("defendant"), gt.get("victim")):
            if isinstance(d, dict) and d.get("name"):
                names.add(str(d["name"]).strip())
        names.update(str(w["name"]).strip() for w in gt.get("witnesses", []) if w.get("name"))
        names.update(str(j["name"]).strip() for j in c.get("jury", []) if j.get("name"))
    return {n for n in names if n and len(n.split()) <= 3}   # skip org names like "X Care Home"


def case_names(case: dict) -> list[str]:
    out = [str(case.get(k, {}).get("name", "")) for k in ("defendant", "victim")
           if isinstance(case.get(k), dict)]
    out += [str(w.get("name", "")) for w in case.get("witnesses", [])]
    return [n.strip() for n in out if n.strip()]


def parse_case(raw: str) -> dict:
    """Parse JSON, tolerating code fences or stray text around it."""
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def build_prompt(guilty: bool, crime: str, setting: str, occupation: str,
                 feedback: str = "") -> str:
    truth = (
        "The defendant IS GUILTY. They committed the crime."
        if guilty else
        "The defendant is INNOCENT. Someone else did it (or it was an accident), "
        "but the defendant looks suspicious enough to be charged."
    )
    avoid = past_cases()
    avoid_txt = ("\nDo NOT reuse any names, plots or situations from these earlier cases:\n- "
                 + "\n- ".join(avoid) + "\n") if avoid else ""
    taken = sorted(used_names())
    if taken:
        avoid_txt += ("\nThese people already exist in other cases. Do NOT reuse "
                      "these exact people; prefer less common names:\n" + ", ".join(taken) + "\n")
    vehicle = "" if crime == "hit-and-run" else (
        "\nThis case must NOT involve a car crash, collision or traffic incident.")
    p = f"""Design a fictional criminal case.

CRIME: {crime}
SETTING: {setting}
DEFENDANT'S OCCUPATION: {occupation}
SECRET TRUTH: {truth}{vehicle}
{avoid_txt}
Requirements:
- 5 to 8 evidence items. Include BOTH evidence pointing toward guilt AND toward innocence,
  plus at least one ambiguous item. The case must be genuinely arguable either way.
- Evidence descriptions must be neutral facts (what was found), not conclusions.
- Evidence that points AWAY from the truth must still be REAL: a misleading detail, an honest
  mistake, a coincidence, or a gap. Its truth_note must explain how it arose given what really happened.
  It must never flatly contradict what_really_happened.
- Every evidence item must be a different kind of thing (no two forensic reports about the same marks).
- 2 or 3 witnesses. At least one has a bias. At most one will_lie=true.
  Set called_by to the side their testimony helps. Ideally each side gets at least one witness.
- Everything must be consistent with what_really_happened.
- Use realistic names. Keep it grounded, no supernatural elements.

Return JSON in exactly this shape:
{SCHEMA}
"""
    if feedback:
        p += f"\nYour previous attempt had problems, fix them:\n{feedback}\n"
    return p


def validate(case: dict, guilty: bool) -> list[str]:
    errs = []
    for key in ["title", "charge", "defendant", "victim", "what_really_happened",
                "actual_perpetrator", "evidence", "witnesses"]:
        if key not in case:
            errs.append(f"missing key: {key}")
    if errs:
        return errs

    ev = case["evidence"]
    if not (5 <= len(ev) <= 8):
        errs.append(f"need 5-8 evidence items, got {len(ev)}")
    sides = " | ".join(str(e.get("points_toward", "")).lower() for e in ev)
    if "guilt" not in sides or "innoc" not in sides:
        errs.append("evidence must include items pointing to guilt AND innocence")

    wits = case["witnesses"]
    if not (2 <= len(wits) <= 3):
        errs.append(f"need 2-3 witnesses, got {len(wits)}")
    if sum(1 for w in wits if w.get("will_lie")) > 1:
        errs.append("at most one witness may lie")

    odd = [n for n in case_names(case)
           if re.search(r"[,()'\u2019]|\bthe\b", n, re.I) or not 2 <= len(n.split()) <= 4]
    if odd:
        errs.append("every person needs a plain first and last name (no descriptions like "
                    f"\"the victim's daughter\" in the name field): {', '.join(odd)}")

    taken = {n.lower() for n in used_names()}
    reused = [n for n in case_names(case) if n.lower() in taken]
    if reused:
        errs.append(f"these people already exist in earlier cases, invent new names: "
                    f"{', '.join(reused)}")
    dname = str(case["defendant"].get("name", "")).strip().lower()
    if dname and any(dname in str(w.get("name", "")).lower() for w in case["witnesses"]):
        errs.append("the defendant cannot also be listed as a witness")

    name = str(case["defendant"].get("name", "")).lower().split()
    perp = str(case["actual_perpetrator"]).lower()
    if name:
        named = name[-1] in perp
        if guilty and not named:
            errs.append("defendant is guilty but actual_perpetrator is not the defendant")
        if not guilty and named:
            errs.append("defendant is innocent but actual_perpetrator names the defendant")
    return errs


def generate(guilty: bool, crime: str | None = None) -> dict:
    crime = crime or random.choice(CRIME_TYPES)
    setting, occupation = random.choice(SETTINGS), random.choice(OCCUPATIONS)
    feedback = ""
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"  attempt {attempt}: generating {crime} case at {setting}, defendant is a "
              f"{occupation} ({'guilty' if guilty else 'innocent'})...")
        try:
            raw = ask(build_prompt(guilty, crime, setting, occupation, feedback))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"    Ollama error ({e}); retrying...")
            continue
        try:
            case = parse_case(raw)
        except json.JSONDecodeError as e:
            feedback = f"Invalid JSON: {e}. Return ONLY the JSON object, complete and closed."
            CASES_DIR.mkdir(exist_ok=True)
            (CASES_DIR / "_last_bad_output.txt").write_text(raw, encoding="utf-8")
            print(f"    bad JSON ({len(raw)} chars, {e.msg} at char {e.pos}); "
                  f"raw saved to cases\\_last_bad_output.txt")
            continue
        errs = validate(case, guilty)
        if not errs:
            return case
        feedback = "\n".join(f"- {e}" for e in errs)
        print(f"    validation failed:\n{feedback}")
    sys.exit(f"\n*** FAILED: no valid case after {MAX_RETRIES} attempts. Nothing was saved. ***\n"
             f"Last problems:\n{feedback}")


def package(case: dict, guilty: bool) -> dict:
    """Split into hidden truth vs. what the court is allowed to see."""
    locker = []
    for i, e in enumerate(case["evidence"], 1):
        e["id"] = f"E{i}"
        locker.append({"id": e["id"], "name": e["name"], "description": e["description"]})

    case_id = datetime.now().strftime("case_%Y%m%d_%H%M%S")
    return {
        "case_id": case_id,
        "created": datetime.now().isoformat(timespec="seconds"),
        "model": MODEL,
        # --- SECRET: engine + defendant only ---
        "guilty": guilty,
        "ground_truth": case,
        # --- Lawyers may cite ONLY these exhibits ---
        "evidence_locker": locker,
        # --- What everyone in the courtroom knows ---
        "public": {
            "title": case["title"],
            "charge": case["charge"],
            "location": case.get("location", ""),
            "defendant": case["defendant"]["name"],
            "victim": case["victim"]["name"],
        },
    }


def list_cases():
    files = sorted(CASES_DIR.glob("case_*.json"))
    if not files:
        print("No cases yet.")
    for f in files:
        c = json.loads(f.read_text(encoding="utf-8"))
        print(f"{f.name}  {c['public']['title']}  ({c['ground_truth'].get('crime', '?')})")


def main():
    ap = argparse.ArgumentParser(description="Generate a new TrialSim case.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--guilty", action="store_true", help="force guilty")
    g.add_argument("--innocent", action="store_true", help="force innocent")
    ap.add_argument("--reveal", action="store_true", help="print the secret truth")
    ap.add_argument("--clear", action="store_true", help="delete all old cases first")
    ap.add_argument("--list", action="store_true", help="list existing cases and exit")
    ap.add_argument("--crime", help='pick the crime, e.g. --crime "art theft"')
    a = ap.parse_args()

    if a.list:
        list_cases()
        return
    if a.clear:
        old = list(CASES_DIR.glob("case_*.json"))
        for f in old:
            f.unlink()
        print(f"Cleared {len(old)} old case(s).")

    guilty = True if a.guilty else False if a.innocent else random.random() < 0.5

    record = package(generate(guilty, a.crime), guilty)
    CASES_DIR.mkdir(exist_ok=True)
    out = CASES_DIR / f"{record['case_id']}.json"
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    pub = record["public"]
    print(f"\n=== {pub['title']} ===")
    print(f"Charge:    {pub['charge']}")
    print(f"Defendant: {pub['defendant']}   Victim: {pub['victim']}")
    print(f"\nEvidence locker:")
    for e in record["evidence_locker"]:
        print(f"  [{e['id']}] {e['name']}: {e['description']}")
    print(f"\nWitnesses: {', '.join(w['name'] for w in record['ground_truth']['witnesses'])}")
    print(f"\nSaved: {out}")

    if a.reveal:
        gt = record["ground_truth"]
        print(f"\n--- SECRET TRUTH ---")
        print(f"Guilty: {guilty}   Actual perpetrator: {gt['actual_perpetrator']}")
        print(gt["what_really_happened"])


if __name__ == "__main__":
    main()
