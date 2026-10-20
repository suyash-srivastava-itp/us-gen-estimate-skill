#!/usr/bin/env python3
"""
Self-test for the lead-time skill. Checks the maths against known values and the model against properties it must keep.
Run after any change to the scripts:  python3 selftest.py
"""
import json
import os
import subprocess
import sys
sys.dont_write_bytecode = True  # keep the skill folder free of __pycache__
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import estimate as E  # noqa: E402
import plan as P  # noqa: E402

TICKET = {"spec_clarity": 0.6, "open_questions": 0, "scope_hours": 8, "diff_lines": 400,
          "test_coverage": 0.5, "complexity": 0.5, "tacit": 0.5}
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


# --- Erlang-C against textbook values
check("erlang_c M/M/1 equals rho", abs(E.erlang_c(1, 0.6) - 0.6) < 1e-9)
check("erlang_c M/M/2 a=1 is 1/3", abs(E.erlang_c(2, 1.0) - 1 / 3) < 1e-9)
check("erlang_c M/M/3 a=2 is 4/9", abs(E.erlang_c(3, 2.0) - 4 / 9) < 1e-9)

# --- units: with no waits, lead time x author rate is author effort, so halving availability doubles the time
quiet = {"reviewers": 0, "release_interval_days": 0, "deploy_h": 0, "agent_attended_fraction": 1.0}
hi = E.simulate(dict(E.validate_ticket(TICKET)[0]), E.merge_team({**quiet, "author_availability": 1.0}), 40000, 1)[0]
lo = E.simulate(dict(E.validate_ticket(TICKET)[0]), E.merge_team({**quiet, "author_availability": 0.5}), 40000, 1)[0]
ratio = sum(lo.values()).mean() / sum(hi.values()).mean()
check("author availability 0.5 doubles elapsed author time", abs(ratio - 2.0) < 0.05, f"ratio {ratio:.3f}")

# --- more authors than the reviewers can absorb must be reported as unstable, not forecast
check("overloaded review queue is unstable", not E.estimate(TICKET, {"authors": 6, "reviewers": 1}, 2000)["stable"])
check("no reviewers means no review stage", E.estimate(TICKET, {"reviewers": 0}, 4000)["stage_share"]["review"] == 0.0)
r1 = E.estimate(TICKET, {"reviewers": 2}, 20000, 3)["lead_time_calendar_days"]["P80"]
r2 = E.estimate(TICKET, {"reviewers": 4}, 20000, 3)["lead_time_calendar_days"]["P80"]
check("more reviewers never slows a ticket", r2 <= r1 + 0.05, f"{r1} -> {r2}")
c5 = E.estimate(TICKET, {"days_per_week": 5}, 20000, 3)["lead_time_calendar_days"]["P50"]
c7 = E.estimate(TICKET, {"days_per_week": 7}, 20000, 3)["lead_time_calendar_days"]["P50"]
check("a 7-day week finishes in fewer calendar days than a 5-day week", c7 < c5, f"{c5} vs {c7}")

# --- legacy v1 team file still loads
check("schema v1 team file maps to v2", E.estimate(TICKET, {"productive_hours_per_day": 5.0}, 2000)["team_used"]["author_hours_per_day"] == 5.0)

# --- provenance and completeness
t = E.merge_team({})
check("defaults report every required parameter as a prior", E.required_at_prior(t) == E.REQUIRED_TEAM)
t2 = E.merge_team({"provenance": {"authors": {"source": "user"}}})
check("a user-sourced parameter leaves the prior list", "authors" not in E.required_at_prior(t2))

# --- plan: a chain cannot finish faster than its parts; two independent stories use two authors
def story(i, deps=()):
    return {"id": i, "deps": list(deps), "ticket": dict(TICKET)}

team = {"reviewers": 0, "release_interval_days": 0, "deploy_h": 0}
chain = [story("A"), story("B", ["A"]), story("C", ["B"])]
p1 = P.plan(chain, {**team, "authors": 1}, 200, 0)
p3 = P.plan(chain, {**team, "authors": 3}, 200, 0)
check("a dependency chain gains nothing from more authors", abs(p1["completion_working_days"]["P50"] - p3["completion_working_days"]["P50"]) < 0.01)
pair = [story("A"), story("B")]
a1 = P.plan(pair, {**team, "authors": 1}, 300, 0)["completion_working_days"]["P50"]
a2 = P.plan(pair, {**team, "authors": 2}, 300, 0)["completion_working_days"]["P50"]
check("two independent stories finish sooner with two authors", a2 < a1, f"{a1} -> {a2}")
check("plan mean completion is at or above its largest lower bound",
      p1["completion_working_days"]["mean"] >= max(v for k, v in p1["lower_bounds_working_days"].items() if k != "note") - 0.1,
      f"mean {p1['completion_working_days']['mean']}")

# --- team_setup: answers become provenance, bad answers are refused
with tempfile.TemporaryDirectory() as d:
    ans, out = os.path.join(d, "a.json"), os.path.join(d, "team.json")
    json.dump({"authors": 3, "author_availability": 0.7}, open(ans, "w"))
    setup = os.path.join(os.path.dirname(os.path.abspath(__file__)), "team_setup.py")
    rc = subprocess.run([sys.executable, setup, "build", "--answers", ans, "--out", out], capture_output=True, text=True).returncode
    built = json.load(open(out))
    check("team_setup records user provenance", rc == 0 and built["provenance"]["authors"]["source"] == "user" and built["complete"] is False)
    json.dump({"author_availability": 1.7}, open(ans, "w"))
    rc = subprocess.run([sys.executable, setup, "build", "--answers", ans, "--out", out], capture_output=True, text=True).returncode
    check("team_setup refuses an out-of-range answer", rc != 0)

# =====================================================================================================================
# Story generation: spec validation, facts, size scale, build outputs, ADO file, import round trip, scope suggestion
# =====================================================================================================================
import copy  # noqa: E402
import csv  # noqa: E402

import suggest_scope as SS  # noqa: E402
import team_setup as TS  # noqa: E402
import us_build as UB  # noqa: E402
import us_import as UI  # noqa: E402
import us_spec as U  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE_DIR = os.path.join(HERE, "..", "assets", "example")
EXAMPLE_REPO = os.path.join(EXAMPLE_DIR, "repo")
EXAMPLE = U.load_spec(os.path.join(EXAMPLE_DIR, "spec.json"))


def fresh():
    s = copy.deepcopy(EXAMPLE)
    U.normalize_spec(s)
    return s


v = U.validate_spec(fresh(), EXAMPLE_REPO)
check("the worked example validates against its own project with no errors", not v["errors"], "; ".join(v["errors"][:2]))
check("the example's file facts are verified against the example project", v["facts"]["verified"] >= 8 and v["facts"]["failed"] == 0, str(v["facts"]))

s = fresh(); s["stories"][0]["scenarios"] = s["stories"][0]["scenarios"][:2]
check("fewer than 3 scenarios is an error", any("3 to 5" in e for e in U.validate_spec(s, None)["errors"]))
s = fresh(); s["stories"][0]["scenarios"] += copy.deepcopy(s["stories"][0]["scenarios"]) + copy.deepcopy(s["stories"][0]["scenarios"][:1])
check("more than 5 scenarios is an error", any("3 to 5" in e for e in U.validate_spec(s, None)["errors"]))
s = fresh(); s["stories"][0]["want"] = "something TBD later"
check("placeholder text is an error", any("placeholder" in e for e in U.validate_spec(s, None)["errors"]))
s = fresh(); del s["stories"][2]["tasks"][0]["technical_background"]["approach"]
check("a task without technical background is an error", any("approach" in e for e in U.validate_spec(s, None)["errors"]))
s = fresh(); s["stories"][2]["tasks"] = []
check("tasks_policy 'large' demands tasks on an L story", any("no tasks" in e for e in U.validate_spec(s, None)["errors"]))
s = fresh(); s["stories"][1]["deps"] = ["S9.9"]
check("an unknown dependency is an error", any("unknown dependency" in e for e in U.validate_spec(s, None)["errors"]))
s = fresh(); s["stories"][0]["deps"] = ["S1.2"]
check("a dependency cycle is an error", any("cycle" in e for e in U.validate_spec(s, None)["errors"]))
s = fresh(); s["stories"][0]["role"] = "As a release manager"; s["stories"][0]["want"] = "I want a thing"; s["stories"][0]["so"] = "so that it works"
notes = U.normalize_spec(s)
check("leading As / I want / so that are stripped with a note", s["stories"][0]["role"] == "a release manager" and s["stories"][0]["want"] == "a thing" and len(notes) == 3)

# facts are checked against the repository
with tempfile.TemporaryDirectory() as repo:
    os.makedirs(os.path.join(repo, "pkg"))
    open(os.path.join(repo, "pkg", "mod.py"), "w", encoding="utf-8").write("def hello():\n    return 'world'\n")
    ok = U.verify_fact({"claim": "hello returns world", "path": "pkg/mod.py", "quote": "return 'world'"}, repo)[0]
    bad_quote = U.verify_fact({"claim": "x", "path": "pkg/mod.py", "quote": "return 'mars'"}, repo)[0]
    bad_path = U.verify_fact({"claim": "x", "path": "pkg/nope.py"}, repo)[0]
    cmd = U.verify_fact({"claim": "x", "command": "git grep hello"}, repo)[0]
    check("a fact with a real path and quote is verified", ok == "verified")
    check("a fact whose quote is not in the file fails", bad_quote == "failed")
    check("a fact whose file does not exist fails", bad_path == "failed")
    check("a fact resting on a command is reported as not verified", cmd == "unverified")
    check("a task may touch a new file but not a missing one", U.check_touches(["pkg/mod.py", "new:pkg/new.py"], repo) == [] and U.check_touches(["pkg/gone.py"], repo) == ["pkg/gone.py"])

# size scale: presets and per-type tables
sc = TS.resolve_size_scale("agent_assisted_refactor")
check("a size-scale preset resolves with per-type overrides", sc["default"]["M"] == 3 and sc["by_type"]["MIGRATE"]["L"] == 16)
try:
    TS.resolve_size_scale({"default": {"S": 5, "M": 3, "L": 8, "XL": 16}})
    shrink_ok = False
except ValueError:
    shrink_ok = True
check("a size scale that shrinks from S to XL is refused", shrink_ok)
team = E.merge_team({"size_scale": sc})
check("focused hours come from the type's own scale", UB.focused_hours({"type": "MIGRATE", "size": "L"}, team)[0] == 16 and UB.focused_hours({"type": "FEATURE", "size": "L"}, team)[0] == 8)
check("focused_hours given on a story wins over the scale", UB.focused_hours({"type": "FEATURE", "size": "L", "focused_hours": 5}, team)[0] == 5)

# build the example end to end, then import its markdown back
with tempfile.TemporaryDirectory() as d:
    team_file = os.path.join(d, "team.json")
    json.dump({"authors": 2, "author_hours_per_day": 4, "author_availability": 0.85, "reviewers": 1, "reviewer_hours_per_day": 4,
               "reviewer_availability": 1.0, "days_per_week": 5, "agent_attended_fraction": 1.0, "release_interval_days": 2,
               "decision_latency_days": 0.5, "size_scale": sc}, open(team_file, "w"))
    outdir = os.path.join(d, "out")
    spec_path = os.path.join(EXAMPLE_DIR, "spec.json")
    r = subprocess.run([sys.executable, os.path.join(HERE, "us_build.py"), "--spec", spec_path, "--team", team_file, "--repo", EXAMPLE_REPO, "--out-dir", outdir,
                        "--reps", "100", "--n", "4000", "--no-levers", "--no-sensitivity", "--sweep", "authors=1,2"], capture_output=True, text=True)
    files = {k: os.path.join(outdir, "cache-port-example" + k) for k in (".md", "_ado.csv", "_estimates.csv", ".spec.json")}
    check("us_build writes the four outputs", r.returncode == 0 and all(os.path.exists(p) for p in files.values()), r.stderr[-200:])
    if r.returncode == 0:
        with open(files["_ado.csv"], encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        hdr, body = rows[0], rows[1:]
        std = {"Work Item Type", "Title 1", "Title 2", "Title 3", "Title 4", "Description", "Acceptance Criteria", "Original Estimate", "Remaining Work", "Tags", "Effort", "Size", "Story Points"}
        check("the ADO file uses only standard columns by default", set(hdr) <= std and hdr[:7] == ["Work Item Type", "Title 1", "Title 2", "Title 3", "Title 4", "Description", "Acceptance Criteria"], str(hdr))
        kinds = [x[0] for x in body]
        check("the ADO file has an epic, features, stories and tasks", kinds.count("Epic") == 1 and kinds.count("Feature") == 2 and kinds.count("User Story") == 4 and kinds.count("Task") == 2, str(kinds))
        task = [x for x in body if x[0] == "Task"][0]
        check("a task row carries hours and a technical background", float(task[hdr.index("Original Estimate")]) > 0 and "Approach" in task[hdr.index("Description")])
        story = [x for x in body if x[0] == "User Story"][0]
        check("a story row carries the user story and Gherkin", "I want" in story[hdr.index("Description")] and "Given" in story[hdr.index("Acceptance Criteria")])
        md = open(files[".md"], encoding="utf-8").read()
        check("the markdown has P50, P80 and P90 per story and for the plan", "P50 / P80 / P90" in md and "## 5. The whole backlog" in md and "Completion, calendar days" in md)
        # update path: markdown back to a spec
        imported = UI.import_md(md, "cache-port-example", "large")
        orig = U.load_spec(files[".spec.json"])
        same_ids = [s["id"] for s in imported["stories"]] == [s["id"] for s in orig["stories"]]
        same_sc = [len(s["scenarios"]) for s in imported["stories"]] == [len(s["scenarios"]) for s in orig["stories"]]
        same_deps = [s["deps"] for s in imported["stories"]] == [s["deps"] for s in orig["stories"]]
        same_tasks = [len(s["tasks"]) for s in imported["stories"]] == [len(s["tasks"]) for s in orig["stories"]]
        same_role = [s["role"] for s in imported["stories"]] == [s["role"] for s in orig["stories"]]
        check("importing the markdown recovers stories, scenarios, deps, tasks and roles", same_ids and same_sc and same_deps and same_tasks and same_role)
        check("an imported task keeps its technical background", imported["stories"][2]["tasks"][0]["technical_background"].get("approach", "").startswith("Pass the port into"))
        check("Gherkin parses back to the same steps", UI.parse_gherkin(U.gherkin(orig["stories"][0]["scenarios"], "x")) == orig["stories"][0]["scenarios"])
    # a spec with an error is refused and nothing is written
    bad = copy.deepcopy(EXAMPLE); bad["stories"][0]["scenarios"] = bad["stories"][0]["scenarios"][:1]
    bad_path = os.path.join(d, "bad.json"); json.dump(bad, open(bad_path, "w"))
    r2 = subprocess.run([sys.executable, os.path.join(HERE, "us_build.py"), "--spec", bad_path, "--team", team_file, "--repo", d, "--out-dir", os.path.join(d, "bad_out"), "--skip-fact-check"], capture_output=True, text=True)
    check("a spec with validation errors is refused and nothing is written", r2.returncode == 2 and not os.path.exists(os.path.join(d, "bad_out")))

# scope suggestion
s0 = SS.suggest_from_counts(0, 0)
check("with no material the suggestion is the generic 4 features and 24 stories", s0[0] == 4 and s0[1] == 24)
heads, items = SS.candidates("# Doc\n## Login\n- The user must be able to reset the password by email\n- The admin should see failed logins\n## Billing\n- The system must send an invoice every month\n- Customers can download an invoice as a PDF\n## Summary\nText.\n")
check("section headings and requirement-like lines are counted", len(items) == 4 and [h for h in heads[2] if not SS.BOILERPLATE.match(h)] == ["Login", "Billing"])
f_, st_, lean_, gran_, _ = SS.suggest_from_counts(2, 4)
check("stories are clamped to at least 3 per feature and the options are ordered", st_ == 6 and lean_ <= st_ <= gran_)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall checks passed")
sys.exit(1 if FAILS else 0)
