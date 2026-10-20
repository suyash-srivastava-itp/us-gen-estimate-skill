# The story format

Every story the skill writes has the same shape. It is the standard the team chose: a user story with three clauses and Gherkin acceptance criteria.

## 1. The three clauses

> **As** [a specific role], **I want** [capability or action], **so that** [benefit or outcome].

- **Role** names the user or persona, specific enough that a reader knows who benefits ("a release manager landing a long-lived branch", not "a user"). For refactoring and platform work the role is the engineer, operator or reviewer who is affected.
- **Want** states what the role needs to do or have, not how the code will do it. Implementation belongs in the context, the design note or the tasks.
- **So that** gives the value: the outcome the role gets or the harm that stops. If the benefit cannot be stated, the story is probably a task.

Store `role`, `want` and `so` without the leading words ("a release manager", not "As a release manager"); the renderers add them. A leading "As", "I want" or "so that" is removed with a note.

## 2. Acceptance criteria: 3 to 5 Gherkin scenarios

```gherkin
Scenario: A missing Redis turns the cache off without failing
  Given `REDIS_URL` is unset
  When I call get and set through the port
  Then get returns `None` and set returns `False`
  And the caller computes the value and no error is raised
```

- **3 to 5 scenarios per story.** Fewer than 3 usually means the failure cases are missing; more than 5 means the story is too big (split it) or two scenarios are really one.
- **Given** the context, **When** the action, **Then** the observable outcome. More steps of the same kind continue with "And". In the spec each scenario holds arrays `given`, `when`, `then`; the first item of each array is the keyword step and the rest are "And" steps.
- **Make outcomes checkable.** "The export works" fails; "the CSV contains every row matching the active filter, at most 50,000" passes. A command with its expected output, a number, a named file or an exact message is checkable.
- **Include a failure or refusal** in at least one scenario (a missing input, a denied request, a rollback). The validator warns when none of the scenarios describes one.
- **Use a Scenario Outline with an `examples` table** when the same behaviour holds for several inputs (`headers` plus `rows`). It raises the story's "concrete example" score.
- **Shared scenarios.** A type of work that always needs the same proof (every code move, every deletion) can be written once in `templates` and attached to stories with `"templates": ["MOVE"]`. They add to the story's own 3 to 5.

## 3. Facts, not guesses

A story is written from facts and from what the person said, nothing else.

- Anything the story states about the current system (a count, a file, a behaviour, a date) goes into `facts`, each with a `source`.
- A fact that names a file gets `path` (and `line`, `quote` where useful). The build checks that the file exists and that the quote appears in it; a fact that fails is an error.
- A fact that rests on a command or a document gets `command` or `source`. The build cannot re-run it, so it is listed under "Not verified" in the output.
- If the person has not decided something, do not decide it in the story. Add it to `open_questions` with an owner. Each open question is charged a decision wait in the estimate and lowers the "design decided" score.
- No placeholders: `TBD`, `TODO`, `XXX`, `lorem ipsum`, `to be decided` in any story or task are validation errors.

## 4. Tasks, only when the person wants them

Ask the person whether to create tasks: for every story, only for large ones (size L or XL, or 8 or more focused hours), or not at all (`scope.tasks_policy`: `all`, `large`, `none`).

A task is a unit of technical work. It always carries a **technical background**, because a task without one cannot be picked up:

| Field | What it holds |
| --- | --- |
| `current_state` | What the code does today, in facts (not what it should do) |
| `evidence` | At least one fact with `path`, `line` or `quote` (or a `command`) that shows the current state |
| `approach` | How to do the work: the design, the order of steps, the pattern followed |
| `touches` | The files or modules changed. Existing paths must exist; a file to be created is written `new:path/to/file.py` |
| `interfaces` | Routes, schemas, contracts or settings that change (optional but listed when there are any) |
| `test_plan` | How the change is proved: which tests, fixtures, goldens or commands |
| `risks` | What can go wrong and the rollback |

`weight` (default 1) says how the story's hours are split: each task receives the story's author and focused hours in proportion to its weight.

## 5. Sizes, hours and lead times

- **Size** (S, M, L, XL) is the person's call, or give `focused_hours` for the story directly. The size scale (hours per size) is chosen with the person and stored in `team.json`; a type can have its own scale (`by_type`).
- **Focused h** is the size's hours: one person working with the agent from starting the branch to a pull request ready for review, with no waiting.
- **Author h** is the model's estimate of the author's time on the story (steering, the attended part of agent runs, triage, rework), from the team's people and calendar. **Reviewer h** and **agent run h** come from the same model.
- **P50, P80 and P90** are lead times from ready to deployed (decisions, review queue, release batch included), in working days, calendar days and wall-clock hours.
- Stories do not add up in time; the whole-backlog simulation gives the completion range for the team.

## 6. What the ADO file contains

`<name>_ado.csv` uses only standard Azure DevOps columns, so it imports without field errors:

`Work Item Type, Title 1, Title 2, Title 3, Title 4, Description, Acceptance Criteria, [story effort field], [task hour fields], Tags`

- Epic in Title 1, Feature in Title 2, story in Title 3, task in Title 4.
- Description holds the user story, context, facts, estimate and dependencies; Acceptance Criteria holds the Gherkin (in a `<pre>` block); a task's Description holds its technical background. Text is HTML unless `--plain-text` is given.
- Task hours go into the task fields of the chosen process (`--ado-process`): agile and cmmi use Original Estimate and Remaining Work, scrum and basic use Remaining Work. The story's author hours go into the process's effort field when it has a suitable one (scrum: Effort; cmmi: Size) and into the Description otherwise (agile's field is Story Points, which are not hours); override with `--story-effort-field`.
- `--extra-columns` adds non-ADO analysis columns (focused, author and reviewer hours, P50/P80/P90); remove them before importing into a project that rejects unknown fields. The full numbers are always in `<name>_estimates.csv` and `<name>.md`.
- Field names differ between ADO processes and customisations; check them against your project and override with `--story-effort-field` and `--task-fields`.
