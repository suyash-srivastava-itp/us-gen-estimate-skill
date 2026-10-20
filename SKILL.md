---
name: us-gen-estimate
description: Create or update user stories from facts and the person's inputs, in the team's standard format (As a / I want / So that, Gherkin acceptance criteria, and tasks with technical background when the person wants them), estimate every story and the whole plan with the team's real authors, reviewers, availability and calendar (focused hours, author hours, P50/P80/P90), and write an Azure DevOps CSV plus a detailed markdown. First asks how many features and stories are expected and suggests values. Also forecasts a single ticket or a backlog's finish date, ranks what limits it, answers "how many people", and calibrates the model. Use when asked to write, split, update or estimate user stories, epics, features or a backlog, to produce an ADO import file, how long a story or backlog will take, how many people are needed, or to calibrate estimates.
---

# us-gen-estimate

Writes user stories from facts and from what the person tells you, estimates them with a model of the real team, and produces two things: an **Azure DevOps CSV** that imports without field errors, and a **detailed markdown** with every story, its Gherkin, its tasks, and its P50/P80/P90 numbers. The same model forecasts one ticket or a whole backlog.

The rules that make it trustworthy:

- **Facts first.** A story states only what the person said or what you read in the repository or the documents, each with a source. A fact that names a file is checked against the repository by the build. What nobody has decided becomes an open question with an owner, never a guess.
- **The person decides the inputs.** The number of features and stories, whether to create tasks, the size scale, the team's people and calendar: you ask (human in the loop), you suggest values, you record what they chose, and the output labels each value **user**, **measured** or **prior**.
- **The estimate is a forecast range**, never a commitment, and it says when it is not calibrated.

## Setup: Python and paths (once per session)

The scripts need Python 3.8 or newer and numpy. Which command starts Python differs between Windows, macOS and Linux, so do not guess one. Run the finder for your system:

- **Windows (PowerShell):** `powershell -NoProfile -ExecutionPolicy Bypass -File "<skill>/scripts/find_python.ps1"`
- **macOS, Linux or Git Bash:** `sh "<skill>/scripts/find_python.sh"`

`<skill>` is this skill's folder: the base directory Claude Code reports when it loads this skill (for example `.claude/skills/US-gen-estimate` in the project, or the same folder under `~/.claude/skills/`). The finder prints `PYTHON=`, `SKILL_DIR=` and `NUMPY=`, and exits with one of three codes:

- **0**: ready. Use the `PYTHON=` value as `<python>` and the `SKILL_DIR=` value as `<skill>` in every command below.
- **2**: Python is found but numpy is missing. Show the person the `INSTALL=` line and ask before you run it, since it installs packages. Then run the finder again.
- **1**: no Python 3.8 or newer. Tell the person the `HINT=` line and stop. Do not run the scripts.

If the person uses a virtual environment, pass its interpreter to the finder: `sh "<skill>/scripts/find_python.sh" "<venv>/bin/python"` or `-Python "<venv>\Scripts\python.exe"` on Windows.

**Paths.** There are two kinds, and each has one rule:

- **Skill files** (`scripts/`, `assets/`, `references/`, `requirements.txt`) are always `<skill>/...`, the absolute path the finder printed. The scripts find their own assets from their own location, so the only path you write for them is the script's.
- **Project files** (`--spec`, `--team .ticket-estimator/team.json`, `--repo .`, `--out-dir`, `--out .ticket-estimator/mined.json`) are relative to the project root, which is the working directory. Run every command from the project root. Never `cd` into the skill folder. Quote any path that contains a space.

Install the packages with `<python> -m pip install -r "<skill>/requirements.txt"` only after the person agrees.

## Files

- `scripts/find_python.ps1`, `scripts/find_python.sh`, `scripts/python_probe.py`: find a Python 3.8+ interpreter with numpy, and print the skill folder's absolute path. Run first; see Setup.
- `scripts/suggest_scope.py`: suggests how many features and stories, from the material, an existing backlog or defaults.
- `scripts/us_build.py`: validates a story spec, estimates every story and the plan, writes `<name>.md`, `<name>_ado.csv`, `<name>_estimates.csv` and `<name>.spec.json`.
- `scripts/us_spec.py`: the spec schema, validation, fact checks, spec-clarity scoring and Gherkin rendering.
- `scripts/us_import.py`: turns an existing backlog markdown in this format back into a spec (to update it).
- `scripts/team_setup.py`, `assets/team_questions.json`, `assets/size_scales.json`: the human-in-the-loop questions for the team and the size scale.
- `scripts/mine_history.py`: measures what git and `gh` can tell (authors, PR rate and size, reviews, CI time, focused hours, release cadence).
- `scripts/estimate.py`, `scripts/plan.py`, `scripts/calibrate.py`, `scripts/codebase_features.py`: the model for one ticket, the whole-backlog simulation, calibration, and the code measures.
- `references/story_format.md`: the story format, the Gherkin rules, the task technical background and the ADO columns. **Read it before writing stories.**
- `references/rubric.md`: how the spec-clarity inputs are scored.
- `assets/example/spec.json`: a small worked spec (two features, four stories, tasks with technical background) written from real files in `assets/example/repo`.
- `scripts/selftest.py`: run it after any change to the scripts.

## Workflow: create stories

### 0. Create or update?

If the person has an existing backlog, go to "Update stories" below. Otherwise continue.

### 1. How many features and stories (ask, with suggestions)

1. Ask for the material the stories come from (a requirements or design document, a brief typed in the chat, a code area to refactor). If it is typed in the chat, save it to a file first.
2. Run `<python> <skill>/scripts/suggest_scope.py --source <files> [--text "<brief>"]`. With nothing to read it returns the generic starting point (4 features, about 24 stories).
3. **Ask the person with AskUserQuestion**, suggested value first and marked "(Recommended)", the other options being the lean and granular values from the script (they can always type their own):
   - "How many features do you expect?" (options from `suggestion.features.choices`)
   - "How many stories do you expect?" (options from `suggestion.stories.choices`; lean means fewer, larger stories, granular means more, smaller ones)
   - "Should I create tasks for the stories?" Options: for every story, only for large stories (size L or XL), no tasks. Tasks always carry a technical background (below).
   Show the evidence behind the suggestion in a line (for example "7 section headings and 21 requirement-like lines were found; the counts come from counting lines, not from understanding them").
4. Record the answers in the spec: `scope.features_expected`, `scope.stories_expected`, `scope.tasks_policy` (`all`, `large`, `none`). The build warns when the spec ends up far from what was asked.

### 2. The team and the sizes (ask)

Look for `.ticket-estimator/team.json`. If it exists and is complete, show it in one line and reuse it. Otherwise follow "Team parameters" below. Then ask the **story sizing** questions, which are not part of the team questions unless you pass `--stories`:

`<python> <skill>/scripts/team_setup.py questions --stories [--mined .ticket-estimator/mined.json] [--existing .ticket-estimator/team.json]`

It lists the size scale (offer the presets from `assets/size_scales.json` plus "type your own"), whether the focused hours already include retries and steering, and the typical pull-request size. Ask them with AskUserQuestion, put the answers in an answers file (`{"size_scale": "repo_calibrated", "typical_pr_lines": 400, "focused_includes_retries": 1}`) and run `<python> <skill>/scripts/team_setup.py build --answers answers.json --existing .ticket-estimator/team.json --out .ticket-estimator/team.json`. Focused hours are the hours one person works with the agent from starting the branch to a pull request ready for review, with no waiting.

### 3. Gather the facts

Read the material and the code the stories touch (Read, Grep, `git grep`, a command whose output you can quote). For every statement the stories will make about the current system, keep the source: the file and line, or the command and when you ran it. Prefer a file with a short quote, which the build can check. Never write a number you did not read.

### 4. Outline first, then write (checkpoint)

Draft the epic, the features and the story list (id, title, role, size, type, dependencies) and **show it to the person before writing the scenarios and tasks**, especially above about 20 stories. Ask whether to proceed or what to change. Sizes are the person's call: propose, and let them correct.

### 5. Write the spec

Write `<name>.spec.json` following the schema in `scripts/us_spec.py` and the rules in `references/story_format.md`. For every story:

- `role`, `want`, `so` (without the leading "As", "I want", "so that").
- 3 to 5 Gherkin scenarios (`given`, `when`, `then` arrays), checkable outcomes, at least one failure or refusal.
- `context`, `facts` (each with a source), `paths` (the existing code it touches, so test coverage and complexity can be measured), `deps`, `type`, `horizon`, `size` (or `focused_hours`), `prs` (how many pull requests it will take).
- `open_questions` for anything undecided, each with an owner.
- Tasks, when the policy asks for them: title, `weight`, and a **technical background** (`current_state`, `evidence`, `approach`, `touches`, `interfaces`, `test_plan`, `risks`). Existing paths in `touches` must exist; a file to be created is written `new:path`.

### 6. Build and fix

```
<python> <skill>/scripts/us_build.py --spec <name>.spec.json --team .ticket-estimator/team.json --repo . --out-dir <dir> [--ado-process agile|scrum|cmmi|basic]
```

Run it from the project root. `--repo .` is the project the facts and paths are checked against.

Validation errors stop the build and are listed (a missing scenario, a placeholder, a fact whose quote is not in the file, a task without technical background). Fix them in the spec and rebuild. Useful flags: `--story-effort-field`, `--task-fields`, `--extra-columns` (non-ADO analysis columns), `--plain-text`, `--sweep authors=1,2,3,4`, `--no-levers`, `--no-sensitivity`, `--skip-fact-check`, `--allow-errors`.

### 7. Report

Give the person the four files and a short summary:

1. **Counts** against what they asked for (features, stories, scenarios, tasks).
2. **Hours**: focused hours (the sizes), author hours (the model), and how they differ.
3. **Whole backlog**: P50, P80 and P90 completion in calendar days for the team, what limits it, and the answer to "how many people" from the authors sweep.
4. **What is assumption**: parameters still at a prior, proxies, facts "not verified", open questions.
5. **Suggestions** from the model (levers, prior sensitivity).
6. Tell them the ADO field names are for the chosen process and may need `--story-effort-field` or `--task-fields` for a customised project.

Keep it short; the detail is in the markdown.

## Workflow: update stories

Prefer the `<name>.spec.json` written by the last build. If only a markdown in this format exists, run `<python> <skill>/scripts/us_import.py --md <file> --out <name>.spec.json` (it recovers stories, Gherkin, tasks and templates, not the paths, evidence quotes or overrides; say so and ask for them if the estimate needs them).

Then change the spec with Edit: add or rewrite a story, split one, add a scenario, change a size, add tasks, answer an open question. Keep story ids stable, say what changed, re-check facts that changed, and rebuild. If the person changed the team or the size scale, update `team.json` through `team_setup.py` and rebuild; the numbers move, the stories do not.

## Team parameters (human in the loop)

Look for `.ticket-estimator/team.json`. If it exists and `complete` is true, show it and use it. Otherwise:

1. **Mine what history can tell.** Ask which people work on this (GitHub logins and git names) and run `<python> <skill>/scripts/mine_history.py --repo . --since-days 60 --people <logins> --git-names <names> --out .ticket-estimator/mined.json`. Mined values are suggestions the person confirms. `mined.json` holds logins and names, so it is not committed (the project's `.gitignore` should list it).
2. `<python> <skill>/scripts/team_setup.py questions --mined .ticket-estimator/mined.json [--existing .ticket-estimator/team.json]` prints the required questions still open with their suggestions.
3. **Ask the person with AskUserQuestion**, up to four questions per round, until every required item is answered: people (authors, hours per day, availability), review (reviewers, their hours and availability, review speed), calendar and workflow (days per week, how much of an agent run the author must attend, release interval, decision latency). Offer the choices from `suggest`; when a mined value exists make it the first option with its evidence. Never answer a required question yourself and never pick "prior" for the person.
4. Write the answers (`{key: number | "mined" | "prior"}`) and run `<python> <skill>/scripts/team_setup.py build --answers answers.json --mined .ticket-estimator/mined.json [--accept-mined-optional] --out .ticket-estimator/team.json`. Show the table it prints and any required parameter still at a prior.

If the person cannot answer a required question, record `"prior"` only when they say so; the output then carries the warning. Never invent a team number.

## Workflow: one ticket, a forecast, calibration

For "how long will this ticket take", "what limits it", "what if we add a reviewer":

1. Score the ticket with `references/rubric.md`: `spec_clarity`, `open_questions`, `scope_hours` (human-equivalent), `diff_lines`; measure the code with `<python> <skill>/scripts/codebase_features.py --repo . --paths <paths>`; write `ticket.json` like `assets/ticket.example.json`.
2. `<python> <skill>/scripts/estimate.py --ticket ticket.json --team .ticket-estimator/team.json --sensitivity --levers` (`--require-complete` refuses to run while a required team parameter is a prior).
3. For a set of tickets with dependencies, `<python> <skill>/scripts/plan.py --backlog backlog.json --team .ticket-estimator/team.json [--sweep authors=1,2,3,4]`.
4. Report the range in stated units, the stage that limits it, utilization, the inputs with their provenance, and the top levers. If the review queue is unstable, say so and give no lead time.

Calibrate with completed tickets: `<python> <skill>/scripts/calibrate.py history.csv --out .ticket-estimator/team.json --base .ticket-estimator/team.json`. Trust the out-of-sample backtest; `lead_time_h` in the history is elapsed wall-clock hours from ready to deployed. Recalibrate when the team, the review policy or CI changes.

## Rules

- Never present one number as a commitment; give the range with its unit.
- Never invent a fact, a number, a file or a quote. Cite or leave an open question. Do not write placeholders.
- Never fill a required team parameter yourself, and never choose the counts, the size scale or the task policy for the person; ask, suggest, record.
- Do not adjust inputs to hit a date the person wants; sweep the levers and say what would make it reachable.
- Score spec clarity conservatively; the build quotes the text behind each criterion.
- Tasks exist only when the person wants them, and every task has a technical background with evidence.
- If the review queue is unstable, give no lead time and name the lever (reviewer time, reviewers, fewer authors).
- Keep the "not calibrated" warning unless `calibrate.py` has run on the team's history.
- Do not edit the scripts to change a result; change the inputs or recalibrate. Improving the skill itself is a separate task.
- Focused hours already contain retries when the person says so (`focused_includes_retries`); the report states which reading it used.
- Run every command from the project root with `<python>` and `<skill>` replaced by the finder's values. Never hard-code a Python command name or a home-directory path.
