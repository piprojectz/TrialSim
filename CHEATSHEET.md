# TrialSim Cheatsheet

All commands run from `D:\Simulations\TrialSim`.

Typical flow: `case_gen.py` → `cast_gen.py` → `trial.py` → `report.py --open` → `player.py --open` (optional: `voices.py` first)

## Make a case
| Command | What it does |
|---|---|
| `python case_gen.py` | New case, random guilty/innocent |
| `python case_gen.py --guilty` | Force guilty |
| `python case_gen.py --innocent` | Force innocent |
| `python case_gen.py --reveal` | Also print the secret truth |
| `python case_gen.py --clear` | Delete all old cases, then make a new one |
| `python case_gen.py --list` | List existing cases (no generation) |
| `python case_gen.py --crime "art theft"` | Choose the crime (any text works, e.g. `"murder"`) |

Validation retries up to 5 times if a case breaks the rules: people reused from earlier cases (exact
name match), descriptions in name fields ("the victim's daughter"), or the defendant listed as a witness.
Ollama errors also count as a retry instead of crashing.

## Cast the jury + witness voices
Run after `case_gen.py`. Saves into the case file; `trial.py` uses it automatically
(no jury in the case = the 8 fixed juror personas).

| Command | What it does |
|---|---|
| `python cast_gen.py` | 6 generated jurors + a speaking style for each witness (newest case) |
| `python cast_gen.py cases\case_XXXX.json` | A specific case |
| `python cast_gen.py --redraw` | Replace an existing jury and voices |
| `python cast_gen.py --jury-only` | Jury only, skip witness voices |
| `python cast_gen.py --show` | Print the cast and exit |

Each jury has at least one pro-prosecution and one pro-defense juror. The jury is saved as soon as it's
generated, so a later failure doesn't lose it.

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

## Watch a running trial
Run in a second PowerShell window. Ctrl+C stops watching, not the trial.

```
$t = (Get-ChildItem runs -Directory | Sort LastWriteTime | Select -Last 1).FullName + "\transcript.jsonl"
Get-Content $t -Tail 5 | ConvertFrom-Json | Select phase, kind, speaker            # last 5 events
Get-Content $t -Wait -Tail 1 | ConvertFrom-Json | Select phase, kind, speaker      # follow live
```

Phase `deliberation` = jury is voting. `result.json` appears in the run folder when it's done.

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
Every report opens with a spoiler-free case summary: crime, charge, location, defendant, victim, both
lawyers and their models, the case generator model, the evidence locker, and the witnesses.
Whether the jury got it right only appears with `--reveal`.

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
| `python report.py --no-summary` | Hide the case summary card at the top |
| `python report.py runs\<folder>` | A specific run instead of the newest |
| `python report.py --out D:\x.html` | Custom output path |

Filters stack: `python report.py --lies --role defense --reveal --open`

## Animated player
Builds `player.html` in the run folder: the trial replayed as a paper-cutout courtroom.

| Command | What it does |
|---|---|
| `python player.py --open` | Build the newest run's player and open it |
| `python player.py runs\<folder>` | A specific run |
| `python player.py --out D:\x.html` | Custom output path |

In the player: Space = play/pause, ← → = step, phase buttons jump, "Private thoughts" toggles thought
bubbles, "Voices" toggles audio, hover a juror for their private reasoning. Ring color = leaning
(red guilty, blue not guilty, grey undecided), ring fill = confidence.

## Voices (optional)
Generates one audio clip per line into `runs\<folder>\audio\`; `player.py` uses them automatically.
Kokoro for most of the cast, Chatterbox (cloned from `voices\wade.wav`) for Wade. Voice choices are at the top of `voices.py`.

| Command | What it does |
|---|---|
| `python voices.py` | Clips for the newest run (only new/changed lines) |
| `python voices.py --only pros` | One character: pros, def, judge, witness, J1..J6 |
| `python voices.py --no-thoughts` | Skip private thoughts (much faster) |
| `python voices.py --redo` | Regenerate everything |
| `python voices.py --device cpu` | Force CPU (default: GPU if it works) |

Then rerun `python player.py --open`.
Setup: `pip install kokoro soundfile chatterbox-tts "setuptools<81"`. RTX 50-series GPUs need
`pip install torch==2.7.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cu128`.

## Flags in the report
| Flag | Meaning |
|---|---|
| FAKE EXHIBIT | Cited an exhibit ID not in the locker |
| PLANNED SPIN | Pre-thought `spin_or_hide` was not "nothing" |
| ADMITTED STRETCH | Post-thought `honesty_check` was not "nothing" |
| OBJECTION SUSTAINED | Judge struck this question after an objection |
| CLEAN | None of the above (self-reported; not a fact-check) |

Jury table: red = guilty, blue = not guilty, grey = undecided. Hover a cell for the juror's reasoning.

## Publishing the showcase run
After changing `report.py`, regenerate the showcase report and push; GitHub Pages updates in a minute or two.
```
python report.py runs\case_20261029_212402_110800 --reveal
git pull
git add report.py runs
git commit -m "..."
git push
```
Edited anything on github.com (e.g. LICENSE)? `git pull` first, or the push gets rejected.
