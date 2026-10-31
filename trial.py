r"""
TrialSim - Courtroom engine: openings, witnesses, objections, closings, deliberation, verdict
------------------------------------------------------------------------
Lawyer turns:   PRE-THOUGHT (private) -> STATEMENT/QUESTIONS (public) -> POST-THOUGHT (private)
Witnesses:      PRE-THOUGHT before testifying -> ANSWERS -> POST-THOUGHT after cross
Objections:     after every question the other lawyer privately decides whether to object;
                the judge rules in open court. Sustained = question struck, jury told to disregard.
Jurors:         private leaning after openings, each witness and closings.
Deliberation:   secret first vote, then jury-room rounds (dissenters speak first) + secret ballots.
                Unanimous = verdict. Deadlocked after MAX_DELIB_ROUNDS = hung jury / mistrial.
Result:         runs/<run>/result.json (read by summarize.py)
Agents only ever see the PUBLIC record.

Usage:
    python trial.py                          # newest case in cases/
    python trial.py cases/case_XXXX.json     # specific case
    python trial.py --random                 # random case from cases/
Output:
    runs/<case_id>_<time>/transcript.jsonl   # channel = public | private
"""
import argparse
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/chat"
PROS_MODEL = "gemma4:12b"        # East Texas voice holds up best on gemma4
DEFENSE_MODEL = "mistral-small3.2:24b"
JUDGE_MODEL = "mistral-small3.2:24b"
CAST_MODEL = "gemma4:12b"        # witnesses, jurors
NUM_JURORS = 6
QUESTIONS_PER_EXAM = 2           # questions per direct / cross
NUM_CTX = 32768                  # context window per model; "max" = model's full window (lots of VRAM)
                                 # (big windows use a lot of VRAM; lower this if runs get slow)
RECORD_CHARS = None              # None = agents see the whole court record; or a char limit
JUDGE_OUTBURST_MAX = 0.25        # outburst chance when patience is empty (scales down when calm)
JUDGE_OUTBURST_MIN = 0.03        # outburst chance when patience is full
JUDGE_MAX_DRAIN = 35             # most patience one turn can cost
WORD_LIMITS = {"statement": 150, "question": 30, "answer": 60,   # words before extra drain
               "closing": 280, "rebuttal": 130}
TURN_DRAIN = 2                   # every turn wears on her a little
SUSTAINED_DRAIN = 10             # lawyer caught misstating / improper question
FAKE_EXHIBIT_DRAIN = 20          # lawyer cites an exhibit that doesn't exist
JUDGE = None                     # set in main()

MAX_DELIB_ROUNDS = 3             # deliberation rounds before a hung jury
JURY_ROOM_CHARS = None           # None = jurors see the whole jury-room discussion

BASE = Path(__file__).parent
CASES_DIR = BASE / "cases"
RUNS_DIR = BASE / "runs"

os.system("")  # enable ANSI colors in Windows consoles
DIM, BOLD, RESET, YELLOW, CYAN, RED = ("\033[2m", "\033[1m", "\033[0m",
                                       "\033[33m", "\033[36m", "\033[31m")


JUROR_PERSONAS = [
    "a retired high school teacher who values careful reasoning",
    "a blunt auto mechanic who distrusts lawyers",
    "an ER nurse who has seen plenty of people lie about injuries",
    "a small business owner who was once scammed",
    "a 20-year-old college student who distrusts police",
    "an accountant who only trusts documents and numbers",
    "a church volunteer who believes in second chances",
    "a long-haul trucker who goes with his gut",
]

PROS_PERSONA = """PERSONALITY: You are an East Texas county prosecutor, born and raised in the
Piney Woods. You talk slow and plain with a thick East Texas drawl and folksy expressions,
and you explain everything with down-home comparisons: hunting, fishing, cattle, church
suppers, Friday night football, pickup trucks. You play the simple country boy so people
underestimate you, then you go for the throat. You're warm with the jury like they're your
neighbors, and you treat that big-city defense lawyer like a fancy fella who's lost his way.
Stay in this voice at all times, even in your private thoughts."""

DEFENSE_PERSONA = """PERSONALITY: You are Preston Whitmore III, a polished big-firm defense attorney
from Houston. Expensive suit, precise vocabulary, measured and surgical. You are quietly
condescending toward the prosecutor, whom you consider a folksy bumpkin performing for the
jury. You dismantle arguments with cold logic, technicalities and reasonable doubt, and
you never raise your voice."""

LAWYER_RULES = """RULES OF THIS COURT:
- You may ONLY cite exhibits from the evidence locker, by ID (e.g. "Exhibit E3").
  You cannot invent new evidence, documents, or witnesses.
- Everything else is fair game: spin, cherry-picking, emotional appeals, attacking credibility,
  framing ambiguous evidence in your favor, overstating what an exhibit proves.
- Overstating is a gamble: opposing counsel can object and the judge can sustain it
  in front of the jury.
- Read the evidence locker carefully. Exploit any exhibit your opponent misrepresents."""


# ---------------------------------------------------------------- LLM plumbing
NO_THINK_PARAM = set()           # models that reject the "think" switch (learned at runtime)
SPEAKER = None                   # set by Agent.system() so token counts know who is talking
TOKENS = {"by_agent": {}, "by_model": {}}


CTX_CACHE = {}


def ctx_for(model):
    """Context window for a model: NUM_CTX if it's a number, else the model's own maximum."""
    if isinstance(NUM_CTX, int):
        return NUM_CTX
    if model not in CTX_CACHE:
        n = 32768
        try:
            req = urllib.request.Request(OLLAMA_URL.replace("/api/chat", "/api/show"),
                                         data=json.dumps({"model": model}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                info = json.loads(r.read()).get("model_info", {})
            lens = [v for k, v in info.items() if k.endswith(".context_length") and isinstance(v, int)]
            if lens:
                n = max(lens)
        except Exception as e:
            print(f"{YELLOW}  (couldn't read context size for {model}: {e}; using {n:,}){RESET}")
        CTX_CACHE[model] = n
    return CTX_CACHE[model]


THINK_CAP = None                 # max thinking tokens per call (None = unlimited; loops still caught)
LOOP_CHECK_CHARS = 200           # thinking is "looping" if its last N chars already appeared earlier


def _post(body):
    """Stream the reply so runaway thinking can be stopped. Generation is aborted (and the call
    retried with thinking off) only if the model starts repeating itself or passes THINK_CAP."""
    body = dict(body, stream=True)
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    think, content, n_think, n_out, final = [], [], 0, 0, {}
    checked_at = 0
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            if not line.strip():
                continue
            chunk = json.loads(line)
            msg = chunk.get("message", {})
            if msg.get("thinking"):
                think.append(msg["thinking"])
                n_think += 1
            if msg.get("content"):
                content.append(msg["content"])
            n_out += 1
            if chunk.get("done"):
                final = chunk
                break
            # loop / cap checks while the model is still thinking
            if msg.get("thinking") and not content:
                text = "".join(think)
                reason = None
                if THINK_CAP and n_think > THINK_CAP:
                    reason = f"passed the {THINK_CAP:,}-token thinking cap"
                elif len(text) - checked_at > 1500 and len(text) > 3000:
                    checked_at = len(text)
                    tail = text[-LOOP_CHECK_CHARS:]
                    if text[:-LOOP_CHECK_CHARS].find(tail) != -1:
                        reason = f"started looping after {n_think:,} thinking tokens"
                if reason:
                    r.close()                        # Ollama stops generating when we hang up
                    return {"message": {"content": ""}, "eval_count": n_out,
                            "prompt_eval_count": 0, "aborted": reason}
    final = dict(final)
    final["message"] = {"content": "".join(content), "thinking": "".join(think)}
    return final


def SPEAKER_LABEL(label):
    global SPEAKER
    SPEAKER = label


def count_tokens(who, model, resp):
    tin, tout = resp.get("prompt_eval_count", 0) or 0, resp.get("eval_count", 0) or 0
    secs = (resp.get("total_duration", 0) or 0) / 1e9
    for table, key in ((TOKENS["by_agent"], who), (TOKENS["by_model"], model)):
        t = table.setdefault(key, {"in": 0, "out": 0, "calls": 0, "secs": 0.0})
        t["in"] += tin
        t["out"] += tout
        t["calls"] += 1
        t["secs"] = round(t["secs"] + secs, 1)
    print(f"{DIM}  [tokens] {who}: {tin:,} in / {tout:,} out · {secs:.1f}s{RESET}")


def chat(model, system, user, json_mode=False, temperature=0.8):
    """Thinking is unlimited. If a model thinks until the context window is full and never answers,
    retry that one call with thinking off (then without strict JSON) so the trial keeps moving."""
    global SPEAKER
    who, SPEAKER = SPEAKER or model, None
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": ctx_for(model)},
    }
    # attempt plan: (thinking allowed?, strict JSON?)
    plan = [(True, json_mode), (False, json_mode), (False, False), (True, False)]
    text = ""
    for think, strict in plan:
        body.pop("think", None)
        body.pop("format", None)
        if not think and model not in NO_THINK_PARAM:
            body["think"] = False
        if strict:
            body["format"] = "json"
        try:
            resp = _post(body)
        except urllib.error.HTTPError as e:
            if "think" in e.read().decode(errors="ignore").lower():
                NO_THINK_PARAM.add(model)          # model doesn't accept the think switch
                continue
            raise
        count_tokens(who, model, resp)
        if resp.get("aborted"):
            print(f"{YELLOW}  ({who} on {model} {resp['aborted']}; retrying with thinking off){RESET}")
            continue
        text = re.sub(r"<think>.*?</think>", "", resp.get("message", {}).get("content", ""),
                      flags=re.S).strip()
        if text and text not in ("{}", "{ }"):
            break
        used = (resp.get("prompt_eval_count") or 0) + (resp.get("eval_count") or 0)
        why = (f"thought for {resp.get('eval_count', 0):,} tokens and ran out of room"
               if used >= ctx_for(model) - 64 else "returned nothing")
        print(f"{YELLOW}  ({who} on {model} {why}; retrying with thinking off){RESET}")
    return text


def trim_speech(text, limit, slack=1.2):
    """Hard limit on spoken words: cut to the last full sentence within limit*slack words."""
    words = text.split()
    if len(words) <= limit * slack:
        return text
    cut = " ".join(words[:int(limit * slack)])
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind('." '))
    return cut[:end + 1].strip() if end > len(cut) * 0.5 else cut.rstrip(",;:") + "..."


def parse_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"raw": text}


def truthy(v):
    return v is True or str(v).strip().lower() in ("true", "yes", "1")


# ---------------------------------------------------------------- Transcript
class Transcript:
    def __init__(self, path: Path):
        self.path = path
        self.public = []
        self.jury = []

    def log(self, channel, agent, phase, kind, text="", data=None, **extra):
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "channel": channel,
            "speaker": agent.name,
            "role": agent.role,
            "phase": phase,
            "kind": kind,
            "text": text,
            "data": data,
            **extra,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if channel == "public":
            self.public.append(f"{agent.name} ({agent.role}): {text}")
            print(f"\n{BOLD}{agent.name} ({agent.role}){RESET}\n{text}")
        elif channel == "jury":
            self.jury.append(f"{agent.name}: {text}")
            print(f"\n{BOLD}{CYAN}[JURY ROOM] {agent.name}{RESET}\n{text}")
        else:
            print(f"{DIM}  [PRIVATE {kind}] {agent.name}: "
                  f"{json.dumps(data, ensure_ascii=False)}{RESET}")

    def jury_room(self):
        if not self.jury:
            return ""
        text = "\n\n".join(self.jury)
        if JURY_ROOM_CHARS and len(text) > JURY_ROOM_CHARS:
            text = "...(earlier discussion omitted)...\n\n" + text[-JURY_ROOM_CHARS:]
        return f"\n\nJURY ROOM DISCUSSION SO FAR (private, jurors only):\n{text}"

    def record(self):
        if not self.public:
            return "(nothing has been said yet)"
        text = "\n\n".join(self.public)
        if RECORD_CHARS and len(text) > RECORD_CHARS:
            text = "...(earlier proceedings omitted)...\n\n" + text[-RECORD_CHARS:]
        return text


# ---------------------------------------------------------------- Agents
class Agent:
    def __init__(self, name, role, model, brief):
        self.name, self.role, self.model, self.brief = name, role, model, brief
        self.plan = None

    def system(self):
        global SPEAKER
        SPEAKER = self.name
        return (f"This is a fictional courtroom simulation. You are {self.name}, "
                f"the {self.role}.\n\n{self.brief}")

    def think_before(self, record, task):
        prompt = f"""COURT RECORD SO FAR:
{record}

YOUR NEXT TASK: {task}

Before you act, write your PRIVATE thoughts. Nobody else will ever see these. Be completely candid.
Return JSON:
{{"what_i_really_think": "your honest read of the case right now",
  "plan": "exactly what you will do and say",
  "spin_or_hide": "anything you plan to exaggerate, downplay, or leave out ('nothing' if none)",
  "exhibits_to_use": ["E1"]}}"""
        return parse_json(chat(self.model, self.system(), prompt, json_mode=True))

    def act(self, record, task, plan=None, limit=200):
        plan_txt = (f"YOUR PRIVATE PLAN: {json.dumps(plan, ensure_ascii=False)}\n\n"
                    if plan else "")
        prompt = f"""COURT RECORD SO FAR:
{record}

{plan_txt}YOUR TASK NOW: {task}

Speak in character, out loud, to the court. Under {limit} words.
No stage directions, no labels, no markdown. Just your spoken words."""
        return trim_speech(chat(self.model, self.system(), prompt), limit)

    def think_after(self, record, what_you_did):
        prompt = f"""COURT RECORD SO FAR:
{record}

What you just did: {what_you_did}

Now that others have responded, write your PRIVATE reflection. Nobody else will see it.
Return JSON:
{{"how_it_landed": "",
  "what_worries_me": "",
  "next_move": "",
  "honesty_check": "anything you said that you don't actually believe, or that stretched the evidence ('nothing' if none)"}}"""
        return parse_json(chat(self.model, self.system(), prompt, json_mode=True))

    def consider_objection(self, record, question, asker, exam_label):
        prompt = f"""COURT RECORD SO FAR:
{record}

During {exam_label}, {asker} just asked: "{question}"

Should you object? Valid grounds: leading (direct examination only), misstates the evidence,
assumes facts not in evidence, calls for speculation, argumentative, hearsay, relevance.
Object only if it helps you win. A frivolous objection that gets overruled makes you look bad.
Return JSON:
{{"object": false, "grounds": "", "private_reason": "why you did or did not object"}}"""
        return parse_json(chat(self.model, self.system(), prompt, json_mode=True,
                               temperature=0.4))

    def rule(self, record, question, grounds):
        prompt = f"""COURT RECORD SO FAR:
{record}

Question asked: "{question}"
Objection grounds: {grounds}

Rule on the objection fairly. If it concerns misstating evidence, check the evidence locker.
Return JSON:
{{"ruling": "sustained | overruled", "explanation": "one short sentence, said aloud to the court"}}"""
        return parse_json(chat(self.model, self.system(), prompt, json_mode=True,
                               temperature=0.3))

    def private_leaning(self, record, moment):
        prompt = f"""COURT RECORD SO FAR:
{record}

We are just past: {moment}.
Write your PRIVATE leaning right now. No other juror sees this.
Return JSON:
{{"leaning": "guilty | not guilty | undecided",
  "confidence": "integer from 0 to 100",
  "why": "one or two sentences",
  "what_would_change_my_mind": ""}}"""
        return parse_json(chat(self.model, self.system(), prompt, json_mode=True,
                               temperature=0.7))


def witness_sides(case, case_path):
    """Which side calls each witness. Uses the case's called_by tag; untagged witnesses are
    inferred once and the result is saved back into the case file (edit it there to override)."""
    sides, changed = {}, False
    defendant = case["public"]["defendant"]
    for w in case["ground_truth"]["witnesses"]:
        tag = str(w.get("called_by", "")).lower()
        if "def" in tag:
            side = "defense"
        elif "pros" in tag:
            side = "prosecution"
        else:
            SPEAKER_LABEL("(witness side check)")
            r = parse_json(chat(JUDGE_MODEL, "You are a trial-planning assistant. Output only JSON.",
                f"The defendant is {defendant}.\nA witness in the trial:\nROLE: {w.get('role', '')}\n"
                f"KNOWS: {w.get('knows', '')}\nBIAS: {w.get('bias', '')}\n\n"
                f"Does this witness's testimony HURT {defendant} (prosecution witness) or HELP "
                f"{defendant} (defense witness)? Consider whom they point suspicion at.\n"
                'Return JSON: {"reason": "one sentence", "side": "prosecution | defense"}',
                json_mode=True, temperature=0))
            side = "defense" if "def" in str(r.get("side", "")).lower() else "prosecution"
            w["called_by"] = side
            changed = True
        sides[w["name"]] = side
        print(f"{DIM}  {w['name']}: {side} witness{RESET}")
    if changed:
        case_path.write_text(json.dumps(case, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"{DIM}  (sides saved to {case_path.name}; edit \"called_by\" there to override){RESET}")
    return sides


def build_cast(case, sides):
    pub, gt = case["public"], case["ground_truth"]
    locker = "\n".join(f"[{e['id']}] {e['name']}: {e['description']}"
                       for e in case["evidence_locker"])
    wit_list = "\n".join(f"- {w['name']} ({w['role']}), {sides[w['name']]} witness"
                         for w in gt["witnesses"])
    header = (f"CASE: {pub['title']}\nCHARGE: {pub['charge']}\n"
              f"DEFENDANT: {pub['defendant']}\nVICTIM: {pub['victim']}\n"
              f"LOCATION: {pub['location']}")
    facts = f"{header}\n\nEVIDENCE LOCKER:\n{locker}\n\nWITNESSES:\n{wit_list}"

    pros = Agent("Wade Pruitt", "prosecutor", PROS_MODEL,
                 "Your ONLY goal is to WIN A CONVICTION, at all costs. You do not care "
                 "whether the defendant actually did it. Losing is the only failure.\n\n"
                 f"{PROS_PERSONA}\n\n{LAWYER_RULES}\n\n{facts}")
    defense = Agent("Preston Whitmore", "defense attorney", DEFENSE_MODEL,
                    "Your ONLY goal is to WIN AN ACQUITTAL, at all costs. You do not care "
                    "whether your client actually did it. Losing is the only failure.\n\n"
                    f"{DEFENSE_PERSONA}\n\n{LAWYER_RULES}\n\n{facts}")
    judge = Agent("Eleanor Price", "judge", JUDGE_MODEL,
                  "You are a strict judge who rules on the law fairly. You keep the trial moving, "
                  "instruct the jury, and enforce the rules: lawyers may only cite exhibits in the "
                  "evidence locker and may not misstate what an exhibit says.\n\n"
                  "PERSONALITY: You are EXTREMELY impatient and short-tempered. You hate long-winded "
                  "lawyers, repetition, theatrics and wasted time. You interrupt, snap, sigh, and make "
                  "cutting sarcastic remarks. Your temper never changes how you rule on the law.\n\n"
                  + facts)
    judge.patience = 100

    witnesses = []
    for w in gt["witnesses"]:
        lie = (f"You WILL LIE on the stand when it suits you. Your reason: {w.get('lie_reason', '')}"
               if truthy(w.get("will_lie")) else
               "You intend to tell the truth as you remember it, though your bias colors how you say it.")
        witnesses.append(Agent(w["name"], "witness", CAST_MODEL,
            f"You are testifying as: {w.get('role', '')}.\n"
            f"WHAT YOU ACTUALLY KNOW: {w.get('knows', '')}\n"
            f"YOUR BIAS: {w.get('bias', '')}\n{lie}\n\n"
            "You only know what you personally saw or heard. Answer only the question asked. "
            f"Stay in character.\n\n{header}"))

    roster = "\n".join(f"- {w['name']}: {w.get('role', '')}" for w in gt["witnesses"])
    d = pub["defendant"]
    jurors = [
        Agent(f"Juror {i + 1}", "juror", CAST_MODEL,
              f"You are {p}. You are an ordinary citizen serving on a jury. You know ONLY "
              "what has been said in open court. If the judge sustains an objection, "
              "disregard that question. You must decide whether the prosecution proved "
              f"guilt beyond a reasonable doubt.\n\n"
              f"IMPORTANT: You are a JUROR. You are NOT the defendant and NOT a witness. "
              f"The defendant is {d}; always call him or her \"{d}\" or \"the defendant\", "
              f"never \"I\" or \"me\". Keep the witnesses straight; do not confuse what a "
              f"witness did with what {d} did. Think for yourself and do not just go along "
              f"with the other jurors.\n\n{header}\n\nWITNESSES:\n{roster}")
        for i, p in enumerate(random.sample(JUROR_PERSONAS, NUM_JURORS))
    ]
    return pros, defense, judge, witnesses, jurors


# ---------------------------------------------------------------- Engine
def check_exhibits(text, case):
    valid = {e["id"] for e in case["evidence_locker"]}
    nums = re.findall(r"\bExhibit\s+E?(\d+)\b|\bE(\d+)\b", text, re.I)
    cited = {f"E{a or b}" for a, b in nums}
    return sorted(cited - valid)


def flag_fake(bad):
    if bad:
        print(f"{YELLOW}  !! cited exhibits that don't exist: {bad}{RESET}")


def judge_reacts(T, speaker, phase, text="", cost=0, kind="statement", fake=False):
    """Drain the judge's patience; maybe an outburst; contempt warning at zero."""
    if JUDGE is None or speaker is JUDGE:
        return
    words = len(text.split())
    over = max(0, words - WORD_LIMITS.get(kind, 150))
    drain = TURN_DRAIN + cost + (over // 10) * 3 + (FAKE_EXHIBIT_DRAIN if fake else 0)
    JUDGE.patience -= min(JUDGE_MAX_DRAIN, drain)
    if JUDGE.patience <= 0 and phase == "closing":
        JUDGE.patience = 5            # she fumes, but won't hold counsel in contempt for a closing
    if JUDGE.patience <= 0:
        msg = JUDGE.act(T.record(), f"Your patience has completely run out with {speaker.name}, "
                        f"who is the {speaker.role}. Address them correctly as the {speaker.role} "
                        "and give them a stern contempt-of-court warning in front of the jury. "
                        "Do not repeat earlier warnings word for word.", limit=40)
        T.log("public", JUDGE, phase, "contempt_warning", text=msg, target=speaker.name)
        print(f"{RED}  >> CONTEMPT WARNING for {speaker.name}{RESET}")
        JUDGE.patience = 60
    elif random.random() < (JUDGE_OUTBURST_MIN + (JUDGE_OUTBURST_MAX - JUDGE_OUTBURST_MIN)
                            * (1 - JUDGE.patience / 100)):
        mood = "irritated" if JUDGE.patience > 50 else "completely fed up"
        msg = JUDGE.act(T.record(), f"Blurt out a short, impatient interjection reacting to what "
                        f"{speaker.name} (the {speaker.role}) just said. You are {mood}. "
                        "Do not repeat your earlier remarks.", limit=20)
        T.log("public", JUDGE, phase, "outburst", text=msg, target=speaker.name,
              patience=JUDGE.patience)
    print(f"{DIM}  (judge patience: {JUDGE.patience}){RESET}")


def take_turn(T, case, agent, phase, task, limit=200, kind="statement"):
    pre = agent.think_before(T.record(), task)
    T.log("private", agent, phase, "pre_thought", data=pre)
    stmt = agent.act(T.record(), task, pre, limit=limit)
    bad = check_exhibits(stmt, case)
    T.log("public", agent, phase, "statement", text=stmt, invalid_exhibits=bad)
    flag_fake(bad)
    judge_reacts(T, agent, phase, stmt, kind=kind, fake=bool(bad))
    return stmt


def closing_objection(T, case, judge, speaker, opponent, speech, stats):
    """Opponent may object to a closing that misstates the evidence; judge rules in open court."""
    locker = "\n".join(f"[{e['id']}] {e['name']}: {e['description']}" for e in case["evidence_locker"])
    o = parse_json(chat(opponent.model, opponent.system(), f"""COURT RECORD SO FAR:
{T.record()}

{speaker.name} just gave this closing argument:
"{speech}"

During closings you may only object if opposing counsel MISSTATES THE EVIDENCE: says an exhibit or a
witness said something it did not, or argues facts never introduced. Compare against the locker:
{locker}
Object only if it clearly helps you win; a weak objection that gets overruled annoys the judge.
Return JSON:
{{"object": false, "what_was_misstated": "", "private_reason": ""}}""", json_mode=True, temperature=0.4))
    if not truthy(o.get("object")):
        return
    stats[opponent.name]["objections"] += 1
    what = str(o.get("what_was_misstated", "")).strip()
    T.log("private", opponent, "closing", "objection_thought", data=o)
    T.log("public", opponent, "closing", "objection",
          text=f"Objection, Your Honor. Counsel misstates the evidence: {what}")
    r = parse_json(chat(judge.model, judge.system(), f"""COURT RECORD SO FAR:
{T.record()}

During {speaker.name}'s closing, opposing counsel objected that it misstates the evidence: {what}
Check the evidence locker and the testimony. Rule fairly.
Return JSON:
{{"ruling": "sustained | overruled", "explanation": "one short sentence said aloud; if sustained, tell the jury to disregard that statement"}}""",
                        json_mode=True, temperature=0.3))
    sustained = str(r.get("ruling", "overruled")).lower().startswith("sustain")
    word = "Sustained" if sustained else "Overruled"
    T.log("public", judge, "closing", "ruling", text=f"{word}. {r.get('explanation', '')}".strip(),
          data=r, sustained=sustained, against=speaker.name if sustained else None)
    print(f"{RED if sustained else CYAN}  >> {word}{RESET}")
    if sustained:
        stats[opponent.name]["sustained"] += 1
        judge_reacts(T, speaker, "closing", cost=SUSTAINED_DRAIN)
    else:
        judge_reacts(T, opponent, "closing", cost=15)


def reflect(T, agent, phase, what_you_did):
    post = agent.think_after(T.record(), what_you_did)
    T.log("private", agent, phase, "post_thought", data=post)


def examine(T, case, judge, examiner, opponent, witness, direct, stats):
    label = "direct examination" if direct else "cross-examination"
    phase = "testimony"
    pre = examiner.think_before(
        T.record(), f"Conduct your {label} of {witness.name} ({witness.brief.splitlines()[0]}). "
                    f"You get {QUESTIONS_PER_EXAM} questions.")
    T.log("private", examiner, phase, "pre_thought", data=pre, witness=witness.name)

    last_q = ""
    for i in range(QUESTIONS_PER_EXAM):
        q = examiner.act(T.record(),
                         f"{label.capitalize()} of {witness.name}: ask question {i + 1} of "
                         f"{QUESTIONS_PER_EXAM}. Ask ONE question only.", pre, limit=60)
        bad = check_exhibits(q, case)
        T.log("public", examiner, phase, "question", text=q, invalid_exhibits=bad,
              witness=witness.name)
        flag_fake(bad)
        last_q = q
        judge_reacts(T, examiner, phase, q, kind="question", fake=bool(bad))

        obj = opponent.consider_objection(T.record(), q, examiner.name, label)
        if truthy(obj.get("object")):
            stats[opponent.name]["objections"] += 1
            grounds = str(obj.get("grounds", "")).strip()
            T.log("private", opponent, phase, "objection_thought", data=obj)
            T.log("public", opponent, phase, "objection",
                  text=f"Objection, Your Honor. {grounds.capitalize()}.")
            r = judge.rule(T.record(), q, grounds)
            sustained = str(r.get("ruling", "overruled")).lower().startswith("sustain")
            word = "Sustained" if sustained else "Overruled"
            T.log("public", judge, phase, "ruling",
                  text=f"{word}. {r.get('explanation', '')}".strip(), data=r,
                  sustained=sustained, against=examiner.name if sustained else None)
            print(f"{RED if sustained else CYAN}  >> {word}{RESET}")
            if sustained:
                stats[opponent.name]["sustained"] += 1
                judge_reacts(T, examiner, phase, cost=SUSTAINED_DRAIN)
                continue
            judge_reacts(T, opponent, phase, cost=15)   # wasted the court's time

        ans = witness.act(T.record(), f"Answer {examiner.name}'s question: \"{q}\"",
                          witness.plan, limit=100)
        T.log("public", witness, phase, "answer", text=ans, examiner=examiner.name)
        judge_reacts(T, witness, phase, ans, kind="answer")

    reflect(T, examiner, phase, f"your {label} of {witness.name}, ending with: \"{last_q}\"")


def poll_jury(T, jurors, phase, moment):
    tally = {}
    for j in jurors:
        lean = j.private_leaning(T.record(), moment)
        try:
            c = float(lean.get("confidence", 0))
            lean["confidence"] = int(round(c * 100 if 0 < c <= 1 else c))
        except (TypeError, ValueError):
            pass
        T.log("private", j, phase, "leaning", data=lean, moment=moment)
        key = str(lean.get("leaning", "?")).lower().strip()
        tally[key] = tally.get(key, 0) + 1
    print(f"\n{CYAN}Jury {moment}: {tally}{RESET}")
    return tally


def norm_vote(v):
    v = str(v).lower()
    return "not guilty" if "not" in v or "innoc" in v else "guilty" if "guilt" in v else "undecided"


def secret_ballot(T, j, label, rnd):
    ctx = T.record() + T.jury_room()
    b = parse_json(chat(j.model, j.system(), f"""{ctx}

SECRET BALLOT ({label}). No one will see how you voted. You must vote guilty or not guilty.
Remember: guilty only if the prosecution proved it beyond a reasonable doubt. You are a juror,
not the defendant. Vote what YOU actually believe, not what the others said.
Return JSON:
{{"vote": "guilty | not guilty", "confidence": "integer 0-100",
  "why": "one or two sentences", "who_influenced_me": "juror name or 'nobody'"}}""",
                        json_mode=True, temperature=0.6))
    b["leaning"] = norm_vote(b.get("vote"))
    try:
        c = float(b.get("confidence", 0))
        b["confidence"] = int(round(c * 100 if 0 < c <= 1 else c))
    except (TypeError, ValueError):
        pass
    T.log("private", j, "deliberation", "ballot", data=b, moment=label, round=rnd)
    return b["leaning"]


def deliberate(T, judge, jurors):
    """Secret first vote, then discussion rounds (dissenters first, random order) + ballots.
    Returns (verdict, rounds_of_discussion, final_tally)."""
    fore = jurors[0]
    T.log("public", judge, "deliberation", "statement",
          text=f"The jury will now retire to deliberate. {fore.name} will serve as foreperson.")

    print(f"\n{BOLD}--- First secret ballot (before any discussion) ---{RESET}")
    votes = {j.name: secret_ballot(T, j, "first vote", 0) for j in jurors}
    tally = {k: list(votes.values()).count(k) for k in ("guilty", "not guilty", "undecided")}
    print(f"{CYAN}First vote: {tally}{RESET}")
    T.log("private", judge, "deliberation", "tally", data=tally, round=0)

    for rnd in range(1, MAX_DELIB_ROUNDS + 1):
        print(f"\n{BOLD}--- Deliberation round {rnd} ---{RESET}")
        majority = max(("guilty", "not guilty"), key=lambda k: tally[k])
        order = jurors[:]
        random.shuffle(order)
        order.sort(key=lambda j: votes[j.name] == majority)      # dissenters speak first
        for j in order:
            stance = votes[j.name]
            if rnd == 1 and tally[majority] == len(jurors):
                task = (f"Everyone voted {majority} on the first ballot. Before the jury finalizes it, "
                        "play devil's advocate honestly: what is the strongest point for the OTHER side, "
                        "and does it give you any real pause?")
            elif stance != majority:
                task = (f"You are in the minority (you voted {stance}). Make your case to the others. "
                        "Hold your ground unless someone gives you a genuinely good reason.")
            else:
                task = ("Respond to the jurors who disagree. Persuade them with specific testimony "
                        "or exhibits, or say honestly if they have changed your mind.")
            said = j.act(T.record() + T.jury_room(),
                         task + " Refer only to what was said in court.", limit=90)
            T.log("jury", j, "deliberation", "jury_statement", text=said, round=rnd)

        votes = {j.name: secret_ballot(T, j, f"ballot {rnd}", rnd) for j in jurors}
        tally = {k: list(votes.values()).count(k) for k in ("guilty", "not guilty", "undecided")}
        print(f"{CYAN}Ballot {rnd}: {tally}{RESET}")
        T.log("private", judge, "deliberation", "tally", data=tally, round=rnd)
        for verdict in ("guilty", "not guilty"):
            if tally[verdict] == len(jurors):
                return verdict, rnd, tally

        if rnd == MAX_DELIB_ROUNDS - 1:
            T.log("public", judge, "deliberation", "allen_charge", text=judge.act(
                T.record(), "The jury has sent a note saying it is deadlocked. Instruct them to keep "
                "deliberating and to re-examine their views, without giving up honest convictions. "
                "Stay in character.", limit=70))
    return "hung", MAX_DELIB_ROUNDS, tally


def load_case(arg, pick_random=False):
    if arg:
        f = Path(arg)
    else:
        files = sorted(CASES_DIR.glob("case_*.json"))
        if not files:
            sys.exit("No cases found. Run case_gen.py first.")
        f = random.choice(files) if pick_random else files[-1]
    print(f"Loaded case file: {f.name}")
    return json.loads(f.read_text(encoding="utf-8")), f


def main():
    global JUDGE
    ap = argparse.ArgumentParser(description="Run a TrialSim trial.")
    ap.add_argument("case", nargs="?", help="case file (default: newest in cases/)")
    ap.add_argument("--random", action="store_true", help="pick a random case from cases/")
    a = ap.parse_args()
    case, case_path = load_case(a.case, a.random)
    run_dir = RUNS_DIR / f"{case['case_id']}_{datetime.now():%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    T = Transcript(run_dir / "transcript.jsonl")
    sides = witness_sides(case, case_path)
    pros, defense, judge, witnesses, jurors = build_cast(case, sides)
    JUDGE = judge
    stats = {a.name: {"objections": 0, "sustained": 0} for a in (pros, defense)}

    started = datetime.now()
    models = dict.fromkeys([PROS_MODEL, DEFENSE_MODEL, JUDGE_MODEL, CAST_MODEL])
    print(f"{DIM}Context windows: " + ", ".join(f"{m} {ctx_for(m):,}" for m in models) + RESET)
    print(f"{BOLD}=== {case['public']['title']} ==={RESET}")
    print(f"{DIM}Jurors: " + "; ".join(j.brief.split('.')[0][8:] for j in jurors) + RESET)

    # ---- Openings
    take_turn(T, case, judge, "opening",
              "Open the trial. Briefly introduce the case and charge to the jury, explain "
              "the burden of proof (beyond a reasonable doubt), and call on the prosecution.")
    p = take_turn(T, case, pros, "opening", "Deliver your opening statement to the jury.")
    d = take_turn(T, case, defense, "opening",
                  "Deliver your opening statement to the jury, responding to the prosecution.")
    reflect(T, pros, "opening", f"your opening statement: \"{p}\"")
    reflect(T, defense, "opening", f"your opening statement: \"{d}\"")
    poll_jury(T, jurors, "opening", "after openings")

    # ---- Witnesses: prosecution's case first, then the defense's.
    #      The calling side does direct, the other side crosses.
    order = ([w for w in witnesses if sides[w.name] == "prosecution"] +
             [w for w in witnesses if sides[w.name] == "defense"])
    rested = False
    for w in order:
        side = sides[w.name]
        if side == "defense" and not rested:
            T.log("public", judge, "testimony", "statement",
                  text="The prosecution rests. The defense may call its first witness.")
            rested = True
        caller, other = (pros, defense) if side == "prosecution" else (defense, pros)
        T.log("public", judge, "testimony", "statement",
              text=f"The {side} calls {w.name} to the stand.")
        w.plan = w.think_before(T.record(), f"You were called by the {side}. You are about to "
                                "take the stand and be questioned by both lawyers. "
                                "Decide how you will testify.")
        T.log("private", w, "testimony", "pre_thought", data=w.plan)

        examine(T, case, judge, caller, other, w, direct=True, stats=stats)
        examine(T, case, judge, other, caller, w, direct=False, stats=stats)

        reflect(T, w, "testimony", "your testimony under direct and cross-examination")
        poll_jury(T, jurors, "testimony", f"after {w.name}")

    if not rested:
        T.log("public", judge, "testimony", "statement", text="The prosecution rests.")
    T.log("public", judge, "testimony", "statement",
          text="The defense rests. We will now hear closing arguments.")

    # ---- Closings: prosecution, defense, prosecution rebuttal
    pc = take_turn(T, case, pros, "closing",
                   "Deliver your closing argument. Pull together the testimony and exhibits into "
                   "the most persuasive case for conviction.", limit=250, kind="closing")
    closing_objection(T, case, judge, pros, defense, pc, stats)
    dc = take_turn(T, case, defense, "closing",
                   "Deliver your closing argument. Tear apart the prosecution's case and leave the "
                   "jury with reasonable doubt.", limit=250, kind="closing")
    closing_objection(T, case, judge, defense, pros, dc, stats)
    pr = take_turn(T, case, pros, "closing",
                   "Deliver a short rebuttal to the defense's closing. You get the last word. "
                   "Finish your final sentence.", limit=110, kind="rebuttal")
    closing_objection(T, case, judge, pros, defense, pr, stats)
    reflect(T, pros, "closing", f"your closing and rebuttal: \"{pc}\" / \"{pr}\"")
    reflect(T, defense, "closing", f"your closing argument: \"{dc}\"")
    poll_jury(T, jurors, "closing", "after closings")

    # ---- Jury instructions + deliberation
    take_turn(T, case, judge, "instructions",
              "Give the jury its final instructions: the charge, the presumption of innocence, "
              "proof beyond a reasonable doubt, that only evidence and testimony count (not lawyer "
              "speeches or struck questions), and that the verdict must be unanimous.", limit=150)
    verdict, rounds, tally = deliberate(T, judge, jurors)

    # ---- Verdict
    if verdict == "hung":
        T.log("public", judge, "verdict", "verdict", text=judge.act(
            T.record(), f"The jury is hopelessly deadlocked ({tally['guilty']} guilty, "
            f"{tally['not guilty']} not guilty). Declare a mistrial. Stay in character.", limit=60),
            data={"verdict": "hung", "tally": tally})
    else:
        T.log("public", jurors[0], "verdict", "verdict",
              text=f"We, the jury, find the defendant, {case['public']['defendant']}, "
                   f"{verdict.upper()}.", data={"verdict": verdict, "tally": tally})
    print(f"\n{BOLD}{RED if verdict == 'guilty' else CYAN}VERDICT: {verdict.upper()}"
          f" (after {rounds} round{'s' if rounds > 1 else ''}){RESET}")

    outcome = f"The verdict was {verdict.upper()}." if verdict != "hung" else \
              "The jury deadlocked and the judge declared a mistrial."
    for lawyer in (pros, defense):
        post = parse_json(chat(lawyer.model, lawyer.system(), f"""{T.record()}

{outcome}
Write your PRIVATE reaction. Nobody will ever see it.
Return JSON:
{{"reaction": "", "what_won_or_lost_it": "",
  "lines_i_crossed": "anything you did to win that you're not proud of ('nothing' if none)",
  "honesty_check": "what you said in this trial that you knew was false or stretched ('nothing' if none)"}}""",
                                    json_mode=True))
        T.log("private", lawyer, "verdict", "post_thought", data=post)

    truth = bool(case.get("guilty"))
    result = {
        "case_id": case["case_id"], "run": run_dir.name, "title": case["public"]["title"],
        "crime": case["ground_truth"].get("crime", ""), "truth_guilty": truth,
        "verdict": verdict, "rounds": rounds, "final_tally": tally,
        "correct": None if verdict == "hung" else (verdict == "guilty") == truth,
        "prosecutor": {"name": pros.name, "model": pros.model},
        "defense": {"name": defense.name, "model": defense.model},
        "judge_patience_end": judge.patience, "objections": stats,
        "tokens": TOKENS,
        "started": started.isoformat(timespec="seconds"),
        "minutes": round((datetime.now() - started).total_seconds() / 60, 1),
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Truth: {'GUILTY' if truth else 'INNOCENT'}  ->  jury was "
          f"{'RIGHT' if result['correct'] else 'WRONG' if result['correct'] is False else 'HUNG'}")

    print(f"\n{BOLD}Objections:{RESET} " + "; ".join(
        f"{n}: {s['objections']} made, {s['sustained']} sustained" for n, s in stats.items()))
    print(f"{BOLD}Judge patience at end:{RESET} {judge.patience}")
    print(f"\n{BOLD}Tokens by character{RESET}")
    for name, t in sorted(TOKENS["by_agent"].items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"])):
        print(f"  {name:<22}{t['in']:>10,} in{t['out']:>9,} out{t['calls']:>5} calls{t['secs']:>8.0f}s")
    print(f"{BOLD}Tokens by model{RESET}")
    for name, t in TOKENS["by_model"].items():
        print(f"  {name:<22}{t['in']:>10,} in{t['out']:>9,} out{t['calls']:>5} calls{t['secs']:>8.0f}s")
    tot_in = sum(t["in"] for t in TOKENS["by_model"].values())
    tot_out = sum(t["out"] for t in TOKENS["by_model"].values())
    print(f"  {'TOTAL':<22}{tot_in:>10,} in{tot_out:>9,} out")
    print(f"Run time: {result['minutes']} min")
    print(f"Transcript saved: {T.path}")


if __name__ == "__main__":
    main()
