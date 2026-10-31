# TrialSim Cheatsheet

All commands run from `D:\Simulations\TrialSim`.

## Make a case
| Command | What it does |
|---|---|
| `python case_gen.py` | New case, random guilty/innocent |
| `python case_gen.py --guilty` | Force guilty |
| `python case_gen.py --innocent` | Force innocent |
| `python case_gen.py --reveal` | Also print the secret truth |
| `python case_gen.py --clear` | Delete all old cases, then make a new one |
| `python case_gen.py --list` | List existing cases (no generation) |
| `python case_gen.py --crime "art theft"` | Choose the crime (any text works) |

## Run a trial
| Command | What it does |
|---|---|
| `python trial.py` | Run the newest case |
| `python trial.py cases\case_XXXX.json` | Run a specific case |
| `python trial.py --random` | Run a random case from `cases\` |

Trial order: openings → witnesses (each side calls its own) → closings + rebuttal (objections allowed
for misstated evidence) → jury instructions → secret first vote → deliberation (dissenters speak first,
up to 3 rounds, must be unanimous) → verdict.
Deadlocked after 3 rounds = hung jury / mistrial. Each run saves `result.json`.

## Summarize all trials
| Command | What it does |
|---|---|
| `python summarize.py` | Scoreboard of every finished trial: verdict vs truth, accuracy, lies per side |
| `python summarize.py --open` | Same, and open `runs\summary.html` (links to each report) |
| (in summary.html) | **Lies & stretches** section: every flagged lie across all trials, filter by trial / role / flag / planted liars, shows what they planned, said, admitted, and what they really knew |
| `python summarize.py --last 5` | Only the 5 most recent trials |
| `python summarize.py --all` | Include unfinished runs in the table |

## Reports (HTML)
Default = newest run. Output lands in the run folder as `report*.html`.

| Command | Shows |
|---|---|
| `python report.py --open` | Everything, opens in browser |
| `python report.py --reveal --open` | Everything + ground truth spoiler panel |
| `python report.py --lies --open` | Only flagged turns (fake / spin / admitted) |
| `python report.py --truths --open` | Only clean turns |
| `python report.py --fake` | Cited exhibits that don't exist |
| `python report.py --spin` | Planned to exaggerate/hide beforehand |
| `python report.py --admitted` | Honesty check confessed afterward |
| `python report.py --caught` | Questions struck by a sustained objection |
| `python report.py --phase testimony` | Witness testimony only |
| `python report.py --thoughts-only` | Private thoughts only, no speeches |
| `python report.py --public-only` | Only what was said in court |
| `python report.py --role prosecutor` | One role (prosecutor, defense, judge, juror) |
| `python report.py --speaker Pruitt` | One person (partial name match) |
| `python report.py --phase opening` | One phase: opening, testimony, closing, deliberation, verdict |
| `python report.py --phase deliberation --open` | Just the jury room debate + ballots |
| `python report.py --no-jury` | Hide the jury table |
| `python report.py runs\<folder>` | A specific run instead of the newest |
| `python report.py --out D:\x.html` | Custom output path |

Filters stack: `python report.py --lies --role defense --reveal --open`

## Flags in the report
| Flag | Meaning |
|---|---|
| FAKE EXHIBIT | Cited an exhibit ID not in the locker |
| PLANNED SPIN | Pre-thought `spin_or_hide` was not "nothing" |
| ADMITTED STRETCH | Post-thought `honesty_check` was not "nothing" |
| OBJECTION SUSTAINED | Judge struck this question after an objection |
| CLEAN | None of the above (self-reported; not a fact-check) |

Jury table: red = guilty, blue = not guilty, grey = undecided. Hover a cell for the juror's reasoning.
