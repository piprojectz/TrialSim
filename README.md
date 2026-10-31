\# TrialSim



A multi-agent courtroom simulation running entirely on local LLMs through Ollama. A generator invents a crime with a hidden ground truth, then an AI prosecutor, defense attorney, judge, witnesses, and a six-person jury try the case. Every agent's \*\*private thoughts are logged before and after each turn\*\*, so you can compare what they said in court with what they actually believed.



Both lawyers are told to \*\*win at all costs\*\*, regardless of whether the defendant did it.



\## What makes it interesting



\- \*\*Private vs. public channels.\*\* Each lawyer and witness writes a private plan before speaking ("what I really think", "what I'll spin or hide") and a private honesty check afterward. Agents only ever see the public court record.

\- \*\*Lie detection.\*\* Lawyers may spin, but may only cite exhibits from the evidence locker. Citing an exhibit that doesn't exist is flagged automatically, and self-reported spin and admitted stretches are flagged in the reports.

\- \*\*Live objections.\*\* After every question, opposing counsel privately decides whether to object. The judge rules in open court, and sustained questions are struck.

\- \*\*An impatient judge.\*\* Judge Price has a patience meter that drains on long-winded answers, sustained objections, and fake exhibits, leading to sarcastic outbursts and contempt warnings.

\- \*\*Biased and lying witnesses.\*\* Each witness has a bias, and some are secretly instructed to lie when it suits them.

\- \*\*Realistic deliberation.\*\* Jurors with distinct personas record private leanings after every phase, then hold a secret first ballot and up to three discussion rounds (dissenters speak first), with an Allen charge if deadlocked. The verdict must be unanimous or it's a hung jury.

\- \*\*Scored against the truth.\*\* Every verdict is compared with the case's hidden ground truth, so you can measure how often persuasion beats facts.

\- \*\*Engineering details.\*\* Per-agent and per-model token accounting, plus detection of runaway "thinking" loops with automatic retry.



\## The cast



| Role | Character | Model |

|---|---|---|

| Prosecutor | Wade Pruitt, East Texas drawl, plays dumb then goes for the throat | `gemma4:12b` |

| Defense | Preston Whitmore III, polished Houston big-firm attorney | `mistral-small3.2:24b` |

| Judge | Eleanor Price, strict and extremely short-tempered | `mistral-small3.2:24b` |

| Witnesses \& jurors | Generated per case; jurors drawn from 8 personas | `gemma4:12b` |



\## Trial flow



Openings → prosecution witnesses → defense witnesses (direct + cross, with objections) → closings and rebuttal → jury instructions → secret first ballot → deliberation → verdict



\## Quick start



Requires \[Ollama](https://ollama.com) running locally with the models above pulled. `trial.py` uses only the Python standard library.



```

python case\_gen.py          # generate a case with a hidden truth

python trial.py             # run the newest case

python report.py --open     # browse the trial as an HTML report

python summarize.py --open  # scoreboard across all trials

```



See \[CHEATSHEET.md](CHEATSHEET.md) for every option, including report filters like `--lies`, `--thoughts-only`, and `--phase deliberation`.



\## Output



Each run writes to `runs/<case>\_<time>/`:

\- `transcript.jsonl`: every event, tagged `public`, `private`, or `jury`

\- `result.json`: verdict, ground truth, objections, judge patience, tokens, runtime

\- `report.html`: a readable trial report with flags and the jury table



An example run is included in \[`runs/case\_20261029\_212402\_110800`](runs/case\_20261029\_212402\_110800).



\*All cases, people, and events are fictional.\*

