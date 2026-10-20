#!/usr/bin/env python3
"""
Build a backlog from a story spec: validate it, estimate every story and the whole plan, and write
  <name>.md              the detailed document: stories, Gherkin, tasks with technical background, P50/P80/P90 and more
  <name>_ado.csv         an Azure DevOps import file (standard fields only)
  <name>_estimates.csv   every number per story and task
  <name>.spec.json       the normalised spec (the source of truth for the next update)

Hours, as in estimate.py: focused hours come from the story's size and the team's size scale (or from focused_hours
given by the person); author hours, reviewer hours and the P50/P80/P90 lead times come from the model with the team in
team.json. Nothing is estimated from a number the tool invented: ticket inputs are scored from the story text, measured
from the code the story names, or assumed at 0.5 and flagged.

Usage:
  python3 us_build.py --spec backlog.spec.json --team .ticket-estimator/team.json --repo . --out-dir docs/backlog
                      [--name NAME] [--ado-process agile|scrum|cmmi|basic] [--story-effort-field FIELD]
                      [--task-fields "Original Estimate,Remaining Work"] [--extra-columns] [--plain-text]
                      [--reps 400] [--n 20000] [--sweep authors=1,2,3,4,6] [--no-sweep] [--no-levers]
                      [--no-sensitivity] [--skip-fact-check] [--allow-errors]
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import statistics
import sys

sys.dont_write_bytecode = True  # keep the skill folder free of __pycache__
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codebase_features as CB  # noqa: E402
import estimate as E  # noqa: E402
import plan as P  # noqa: E402
import us_spec as U  # noqa: E402

WAIT = {"dev": "author and agent time", "release": "the release batch", "review": "the review queue", "spec": "decisions"}
PROCESSES = {
    "agile": {"story": "User Story", "effort": None, "task_fields": ["Original Estimate", "Remaining Work"], "ac": True},
    "scrum": {"story": "Product Backlog Item", "effort": "Effort", "task_fields": ["Remaining Work"], "ac": True},
    "cmmi": {"story": "Requirement", "effort": "Size", "task_fields": ["Original Estimate", "Remaining Work"], "ac": True},
    "basic": {"story": "Issue", "effort": "Effort", "task_fields": ["Remaining Work"], "ac": False},
}
n1 = lambda v: f"{v:,.1f}"


# ------------------------------------------------------------------ estimation
def focused_hours(story, team):
    if story.get("focused_hours"):
        return float(story["focused_hours"]), "given for this story"
    scale = team.get("size_scale") or E.DEFAULT_TEAM["size_scale"]
    table = (scale.get("by_type") or {}).get(story.get("type")) or scale["default"]
    return float(table[story["size"]]), ("by type " + story["type"] if (scale.get("by_type") or {}).get(story.get("type")) else "default scale")


def effort_only(ticket, team, n, seed):
    """Author, reviewer and agent hours without the review queue (used when the queue is unstable)."""
    rng = np.random.default_rng(seed)
    comp = E.sample_components(ticket, team, n, rng)
    return {"author_h": float(np.mean(comp["author_h_pre"] + comp["author_h_fix"])),
            "reviewer_h": float(np.mean(comp["passes"]) * comp["rev_eff_h"]), "agent_run_h": float(np.mean(comp["agent_h"])),
            "p_first": comp["p_first_attempt"], "rework": comp["rework_prob"]}


def compute_stories(spec, team, repo, n, seed, notes):
    cb_files = CB.list_repo_files(os.path.abspath(repo)) if repo else None
    doc_cache = {}
    queue_ok = E.review_queue(team)["stable"]
    out = {}
    for s in spec["stories"]:
        sid = s["id"]
        fh, fh_how = focused_hours(s, team)
        # codebase measures from the paths the story names
        existing = [p for p in s.get("paths", []) if not str(p).startswith("new:")]
        measured, cb_note = None, ""
        if repo and existing:
            m = CB.measure(repo, existing, all_files=cb_files, doc_cache=doc_cache)
            if m["scope"]["source_files"] > 0:
                measured = m
            else:
                cb_note = "the named paths hold no source files"
        elif not existing:
            cb_note = "the story names no existing paths"
        ov = s.get("ticket_overrides") or {}
        if measured:
            tf = measured["ticket_fields"]
            code_inputs = {"test_coverage": (tf["test_coverage"], measured["provenance"]["test_coverage"]),
                           "complexity": (tf["complexity"], "proxy"), "tacit": (tf["tacit"], "proxy")}
        else:
            code_inputs = {k: (0.5, "assumed") for k in ("test_coverage", "complexity", "tacit")}
            notes.append(f"{sid}: test coverage, complexity and tacit knowledge assumed at 0.5 ({cb_note}); name the code it touches in 'paths' to measure them")
        for k in ("test_coverage", "complexity", "tacit"):
            if k in ov:
                code_inputs[k] = (float(ov[k]), "user")
        score, crit, ev = U.score_clarity(s)
        clarity, clarity_src = (float(ov["spec_clarity"]), "user (" + str(ov.get("reason", "no reason given")) + ")") if "spec_clarity" in ov else (score, "scored from the story text")
        prs = int(s.get("prs") or max(1, len(s.get("tasks", []))))
        diff = float(s.get("diff_lines") or prs * team["typical_pr_lines"])
        ticket = {"spec_clarity": clarity, "open_questions": float(len(s.get("open_questions", []))), "scope_hours": 1.0, "diff_lines": diff,
                  "test_coverage": code_inputs["test_coverage"][0], "complexity": code_inputs["complexity"][0], "tacit": code_inputs["tacit"][0]}
        # scope: human-equivalent hours so that one attempt costs the focused hours (divided by expected attempts when
        # the focused hours already contain retries)
        p1 = E.sample_components({**ticket, "scope_hours": 1.0}, team, 1, np.random.default_rng(seed))["p_first_attempt"]
        scope = fh * team["agent_speed"] * (p1 if team["focused_includes_retries"] >= 0.5 else 1.0)
        ticket["scope_hours"] = round(max(scope, 0.1), 3)
        res = E.estimate(ticket, team, n, seed) if queue_ok else {"stable": False}
        eff = ({"author_h": res["effort_hours_mean"]["author_h"], "reviewer_h": res["effort_hours_mean"]["reviewer_h"],
                "agent_run_h": res["effort_hours_mean"]["agent_run_h"], "p_first": res["p_first_attempt"], "rework": res["rework_probability"]}
               if res.get("stable") else effort_only(ticket, team, n, seed))
        out[sid] = {"focused_h": fh, "focused_how": fh_how, "ticket": ticket, "clarity": clarity, "clarity_src": clarity_src, "criteria": crit, "evidence": ev,
                    "code_src": {k: v[1] for k, v in code_inputs.items()}, "prs": prs, "estimate": res, "effort": eff,
                    "paths_measured": existing if measured else []}
    return out


def allocate_tasks(story, comp):
    """Split the story's hours across its tasks by weight; the last task takes the rounding remainder."""
    tasks = story.get("tasks", [])
    if not tasks:
        return []
    w = [float(t.get("weight", 1) or 1) for t in tasks]
    tot = sum(w)
    rows, used_a, used_f = [], 0.0, 0.0
    for i, (t, wi) in enumerate(zip(tasks, w)):
        last = i == len(tasks) - 1
        a = round(comp["effort"]["author_h"] - used_a, 1) if last else round(comp["effort"]["author_h"] * wi / tot, 1)
        f = round(comp["focused_h"] - used_f, 1) if last else round(comp["focused_h"] * wi / tot, 1)
        used_a += a
        used_f += f
        rows.append({"task": t, "author_h": a, "focused_h": f})
    return rows


# ------------------------------------------------------------------ markdown
def inline(t):
    return str(t)


def render_md(spec, team, comp, plan, sweep, levers, sens, val, name, args, build_notes):
    L = []
    P_ = lambda *a: L.extend(a)
    prov = team.get("provenance", {})
    stories = spec["stories"]
    features = spec["features"]
    ok = [c for c in comp.values() if c["estimate"].get("stable")]
    tot_f = sum(c["focused_h"] for c in comp.values())
    tot_a = sum(c["effort"]["author_h"] for c in comp.values())
    tot_r = sum(c["effort"]["reviewer_h"] for c in comp.values())
    tot_g = sum(c["effort"]["agent_run_h"] for c in comp.values())
    n_sc = sum(len(s["scenarios"]) for s in stories)
    n_tasks = sum(len(s.get("tasks", [])) for s in stories)
    med = lambda key, unit: statistics.median(c["estimate"][unit][key] for c in ok) if ok else None
    epic = spec["epic"]
    scope = spec.get("scope", {})

    P_(f"# {epic.get('title', name)}", "")
    P_(f"**Generated** {dt.date.today().isoformat()} by the US-gen-estimate skill from `{name}.spec.json`. Every hour and duration is a forecast range, not a commitment.", "")
    if epic.get("goal"):
        P_(f"**Goal.** {epic['goal']}", "")
    P_("## 1. Summary", "")
    P_(f"- **Size:** {len(features)} features, {len(stories)} stories, {n_sc} Gherkin scenarios, {n_tasks} tasks"
       + (f" (you expected {scope.get('features_expected', '?')} features and about {scope.get('stories_expected', '?')} stories)" if scope.get("features_expected") or scope.get("stories_expected") else "") + ".")
    P_(f"- **Hours:** focused {n1(tot_f)} h (the sizes), author effort {n1(tot_a)} h (the model), reviewer effort {n1(tot_r)} h, agent run time {n1(tot_g)} h. Author effort is {tot_a / tot_f:.2f} times the focused hours." if tot_f else "- **Hours:** none.")
    if ok:
        P_(f"- **Median story:** lead time P50 {med('P50', 'lead_time_calendar_days'):.1f}, P80 {med('P80', 'lead_time_calendar_days'):.1f}, P90 {med('P90', 'lead_time_calendar_days'):.1f} calendar days from ready to deployed "
           f"({med('P50', 'lead_time_working_days'):.1f} / {med('P80', 'lead_time_working_days'):.1f} / {med('P90', 'lead_time_working_days'):.1f} working days). Do not add story lead times: stories overlap and share the team.")
    else:
        P_("- **Lead time:** unavailable. The review queue is unstable with this team (see section 2); author and reviewer hours are still shown.")
    if plan:
        cd = plan["completion_calendar_days"]
        P_(f"- **Whole backlog with {team['authors']} authors:** finishes in P50 {cd['P50']:.0f}, P80 {cd['P80']:.0f}, P90 {cd['P90']:.0f} calendar days ({plan['completion_calendar_weeks_P80']:.1f} weeks at P80). Limited by {plan['limited_by']}; authors {plan['author_utilization']:.0%} busy, reviewers {plan['reviewer_utilization']:.0%}.")
    if val["errors"]:
        P_(f"- **Validation:** {len(val['errors'])} error(s) were allowed through; see section 8.")
    P_("")

    P_("## 2. Inputs and where they came from", "")
    P_("| Team parameter | Value | Source |", "| --- | --- | --- |")
    for k in E.REQUIRED_TEAM + ["lines_per_reviewer_hour", "typical_pr_lines", "focused_includes_retries", "agent_speed", "steer_h", "triage_h", "rework_fix_h", "ci_hours", "deploy_h"]:
        if k in team:
            P_(f"| `{k}` | {team[k]} | {E.source_of(team, k)} |")
    sc = team.get("size_scale", {})
    P_("", "**Size scale (focused hours per size)** " + f"({E.source_of(team, 'size_scale')}"
       + (f", preset {sc['preset']}" if sc.get("preset") else "") + ")", "", "| Type | S | M | L | XL |", "| --- | --- | --- | --- | --- |")
    P_(f"| default | {sc['default']['S']:g} | {sc['default']['M']:g} | {sc['default']['L']:g} | {sc['default']['XL']:g} |")
    for t, tb in (sc.get("by_type") or {}).items():
        P_(f"| {t} | {tb['S']:g} | {tb['M']:g} | {tb['L']:g} | {tb['XL']:g} |")
    pend = E.required_at_prior(team)
    P_("", "**What is assumption, not fact**", "")
    P_(f"- The model is {'calibrated on completed tickets' if team.get('calibrated') else '**not calibrated** on completed tickets'}; the success and rework coefficients are priors." )
    if pend:
        P_(f"- Required team parameters still at a prior: {', '.join(pend)}.")
    sizing_prior = [k for k in ("size_scale", "focused_includes_retries", "typical_pr_lines") if E.source_of(team, k) == "prior"]
    if sizing_prior:
        P_(f"- Story sizing inputs still at the default, not chosen by a person: {', '.join(sizing_prior)}. Run team_setup.py questions --stories to set them.")
    P_("- Spec clarity is scored by rules over the story text (breakdown with quotes under each story); test coverage, complexity and documentation signals are proxies unless a coverage report was supplied.")
    P_("- Focused hours are the person's size scale applied to each story's size; they are inputs, not measurements." + (" They are treated as already containing retries and steering." if team["focused_includes_retries"] >= 0.5 else " The model adds its own retries on top."))
    for nt in build_notes[:12]:
        P_(f"- {nt}")
    if len(build_notes) > 12:
        P_(f"- ... and {len(build_notes) - 12} more input notes (see `{name}_estimates.csv`).")
    P_("")

    P_("## 3. Features at a glance", "", "| Feature | Stories | Focused h | Author h | Median P50 / P80 / P90 (calendar days) | Goal |", "| --- | --- | --- | --- | --- | --- |")
    for f in features:
        ss = [s for s in stories if s["feature"] == f["id"]]
        cs = [comp[s["id"]] for s in ss if comp[s["id"]]["estimate"].get("stable")]
        lt = " / ".join(f"{statistics.median(c['estimate']['lead_time_calendar_days'][k] for c in cs):.1f}" for k in ("P50", "P80", "P90")) if cs else "n/a"
        P_(f"| **{f['id']}** {f['title']} | {len(ss)} | {n1(sum(comp[s['id']]['focused_h'] for s in ss))} | {n1(sum(comp[s['id']]['effort']['author_h'] for s in ss))} | {lt} | {f.get('goal', '')} |")
    P_(f"| **Total** | {len(stories)} | {n1(tot_f)} | {n1(tot_a)} | | |", "")

    P_("## 4. Estimates for every story", "",
       "Working days are the team's days; calendar days include non-working days; wall hours are calendar days x 24. Stage shares are the mean share of the lead time.", "",
       "| ID | Story | Type / size | Focused h | Author h | Reviewer h | Agent run h | P50 / P80 / P90 working days | P50 / P80 / P90 calendar days | Authors + agent | Review | Release | Decisions | First attempt | Rework | Main wait |",
       "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for s in stories:
        c = comp[s["id"]]
        e = c["estimate"]
        if e.get("stable"):
            wd, cd = e["lead_time_working_days"], e["lead_time_calendar_days"]
            sh = e["stage_share"]
            lead = f"{wd['P50']:.1f} / {wd['P80']:.1f} / {wd['P90']:.1f} | {cd['P50']:.1f} / {cd['P80']:.1f} / {cd['P90']:.1f} | {sh['dev']:.0%} | {sh['review']:.0%} | {sh['release']:.0%} | {sh['spec']:.0%}"
            wait = WAIT.get(e["bottleneck"], e["bottleneck"])
        else:
            lead, wait = "n/a | n/a | | | | ", "review queue unstable"
        P_(f"| {s['id']} | {s['title']} | {s['type']} {s.get('size') or '-'} | {n1(c['focused_h'])} | {n1(c['effort']['author_h'])} | {c['effort']['reviewer_h']:.2f} | {n1(c['effort']['agent_run_h'])} | {lead} | {c['effort']['p_first']:.0%} | {c['effort']['rework']:.0%} | {wait} |")
    P_("")

    P_("## 5. The whole backlog", "")
    if plan:
        cd, wd = plan["completion_calendar_days"], plan["completion_working_days"]
        P_(f"A simulation of {plan['reps']} runs over the dependency graph with the team in section 2 (authors take stories by longest remaining path, reviewers form a queue, merged work waits for the next release batch).", "",
           "| Measure | P50 | P80 | P90 |", "| --- | --- | --- | --- |",
           f"| Completion, calendar days | {cd['P50']:.0f} | {cd['P80']:.0f} | {cd['P90']:.0f} |",
           f"| Completion, working days | {wd['P50']:.0f} | {wd['P80']:.0f} | {wd['P90']:.0f} |", "",
           f"- **Limited by:** {plan['limited_by']}. Author utilization {plan['author_utilization']:.0%}, reviewer utilization {plan['reviewer_utilization']:.0%}.",
           "- **Lower bounds in working days (means):** " + ", ".join(f"{k.replace('_mean', '').replace('_', ' ')} {v}" for k, v in plan["lower_bounds_working_days"].items() if k != "note") + ". The simulated mean completion " + f"({wd['mean']:.0f} working days) must sit at or above all of them.", "")
    if sweep:
        P_(f"**What changes with {sweep['sweep']}** (everything else as in section 2):", "", f"| {sweep['sweep']} | P50 | P80 | P90 | P80 weeks | Author util. | Reviewer util. | Limited by |", "| --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in sweep["rows"]:
            P_(f"| {r[sweep['sweep']]}{' (current)' if float(r[sweep['sweep']]) == float(team[sweep['sweep']]) else ''} | {r['completion_calendar_days']['P50']:.0f} d | {r['completion_calendar_days']['P80']:.0f} d | {r['completion_calendar_days']['P90']:.0f} d | {r['completion_calendar_weeks_P80']:.1f} | {r['author_utilization']:.0%} | {r['reviewer_utilization']:.0%} | {r['limited_by']} |")
        P_("")

    P_("## 6. Suggestions from the model", "")
    if levers:
        P_("What would shorten the P80 lead time of a story, averaged over the stories where the change applies (negative is faster). The model does not know what a change costs.", "",
           "| Lever | Stories | Mean change in P80 (calendar days) |", "| --- | --- | --- |")
        for k, d in sorted(levers.items(), key=lambda kv: statistics.mean(kv[1]) if kv[1] else 0):
            if d:
                P_(f"| `{k}` | {len(d)} | {statistics.mean(d):.2f} |")
        P_("")
    if sens:
        P_("Parameters still at a prior, ranked by how far they move a story's P80 when halved or raised by half (mean over stories). A large swing on a prior means the answer rests on a guess. `agent_speed` is left out: it is paired with each story's scope.", "",
           "| Parameter | Mean swing in P80 (calendar days) |", "| --- | --- |")
        for k, v in sorted(sens.items(), key=lambda kv: -statistics.mean(kv[1])):
            P_(f"| `{k}` | {statistics.mean(v):.2f} |")
        P_("")
    sug = []
    nq = sum(1 for s in stories if s.get("open_questions"))
    if nq:
        sug.append(f"Answer the open questions early: {nq} stories carry some (section 7). Each is charged {team['decision_latency_days']} working day in the forecast.")
    nc = sum(1 for c in comp.values() if c["code_src"]["test_coverage"] != "measured")
    if nc:
        sug.append(f"Measure real test coverage for the code behind the {nc} stories whose coverage is a proxy or assumed (run the project's coverage tool and pass the report to codebase_features.py), then rebuild.")
    if not team.get("calibrated"):
        sug.append("Calibrate when completed stories exist: record their actual lead times and run calibrate.py with --base team.json.")
    big = [s["id"] for s in stories if s.get("size") == "XL"]
    if big:
        sug.append(f"Split the XL stories before planning: {', '.join(big)}.")
    for i, t in enumerate(sug, 1):
        P_(f"{i}. {t}")
    P_("")

    P_("## 7. Stories", "")
    for f in features:
        P_(f"### {f['id']}. {f['title']}", "")
        if f.get("goal"):
            P_(f"**Goal.** {f['goal']}", "")
        if f.get("why"):
            P_(f"**Why.** {f['why']}", "")
        if f.get("exit_criteria"):
            P_("**Feature exit criteria**", "")
            for x in f["exit_criteria"]:
                P_(f"- [ ] {x}")
            P_("")
        for s in [x for x in stories if x["feature"] == f["id"]]:
            c = comp[s["id"]]
            e = c["estimate"]
            P_(f"#### {s['id']}. {s['title']}", "", f"> **As** {s['role']}, **I want** {s['want']}, **so that** {s['so']}.", "")
            P_(f"**Type** {s['type']} · **Horizon** {s.get('horizon', '-')} · **Depends on:** {', '.join(s['deps']) or 'none'}" + (f" · **Also applies:** {', '.join(s['templates'])} (appendix)" if s["templates"] else ""), "")
            P_("| Estimate | Value |", "| --- | --- |", f"| Size / focused hours | {s.get('size') or '-'} · {n1(c['focused_h'])} h ({c['focused_how']}) |",
               f"| Author effort | {n1(c['effort']['author_h'])} h |", f"| Reviewer effort | {c['effort']['reviewer_h']:.2f} h |", f"| Agent run time | {n1(c['effort']['agent_run_h'])} h |")
            if e.get("stable"):
                wd, cd, wh = e["lead_time_working_days"], e["lead_time_calendar_days"], e["lead_time_hours"]
                sh = e["stage_share"]
                P_(f"| Lead time P50 / P80 / P90 | {wd['P50']:.1f} / {wd['P80']:.1f} / {wd['P90']:.1f} working days · {cd['P50']:.1f} / {cd['P80']:.1f} / {cd['P90']:.1f} calendar days · {wh['P50']:.0f} / {wh['P80']:.0f} / {wh['P90']:.0f} wall hours |",
                   f"| Where the time goes | authors and agent {sh['dev']:.0%} · review {sh['review']:.0%} · release batch {sh['release']:.0%} · decisions {sh['spec']:.0%} (main wait: {WAIT.get(e['bottleneck'], e['bottleneck'])}) |")
            else:
                P_("| Lead time | unavailable: the review queue is unstable with this team |")
            P_(f"| First attempt succeeds / rework needed | {c['effort']['p_first']:.0%} / {c['effort']['rework']:.0%} |", "")
            t = c["ticket"]
            P_(f"**Inputs.** Spec clarity {c['clarity']:.2f} ({c['clarity_src']}) · open questions {int(t['open_questions'])} · test coverage {t['test_coverage']:.2f} ({c['code_src']['test_coverage']}) · complexity {t['complexity']:.2f} ({c['code_src']['complexity']}) · tacit {t['tacit']:.2f} ({c['code_src']['tacit']}) · {c['prs']} PR(s), {t['diff_lines']:.0f} changed lines · scope {t['scope_hours']:.2f} human-equivalent hours.", "")
            P_("<details><summary>Spec clarity breakdown</summary>", "")
            for nm, v in zip(U.CRITERIA_NAMES, c["criteria"]):
                P_(f"- {nm}: {v:g} ({c['evidence'][nm]})")
            P_("", "</details>", "")
            if s.get("open_questions"):
                P_("**Open questions**", "")
                for q in s["open_questions"]:
                    P_(f"- {q['question']}" + (f" (owner: {q['owner']})" if q.get("owner") else ""))
                P_("")
            if s.get("context"):
                P_(f"**Context.** {s['context']}", "")
            if s.get("facts"):
                P_("**Facts and sources**", "")
                for fa in s["facts"]:
                    st, _ = U.verify_fact(fa, args.repo if not args.skip_fact_check else None)
                    P_(f"- {fa.get('claim')} ({fa.get('path') or fa.get('command') or fa.get('source')}{':' + str(fa['line']) if fa.get('line') else ''}; {'verified' if st == 'verified' else 'not verified by the tool'})")
                P_("")
            P_("**Acceptance criteria**", "", "```gherkin", U.gherkin(s["scenarios"], f"{s['id']} {s['title']}"), "```", "")
            tasks = allocate_tasks(s, c)
            if tasks:
                P_("**Tasks**", "")
                for i, tk in enumerate(tasks, 1):
                    t0, tb = tk["task"], tk["task"].get("technical_background", {})
                    P_(f"{i}. **{t0['title']}** ({tk['author_h']} author h, {tk['focused_h']} focused h)", "")
                    if not tb.get("approach"):
                        P_("   - *No technical background yet.* Add the current state, evidence, approach, touched paths, test plan and risks.", "")
                        continue
                    P_(f"   - *Current state.* {tb.get('current_state', '')}")
                    for fa in tb.get("evidence", []):
                        P_(f"   - *Evidence.* {fa.get('claim', fa.get('finding', ''))} (`{fa.get('path') or fa.get('command') or fa.get('source')}{':' + str(fa['line']) if fa.get('line') else ''}`)")
                    P_(f"   - *Approach.* {tb['approach']}", f"   - *Touches.* {', '.join('`' + x + '`' for x in tb.get('touches', []))}")
                    if tb.get("interfaces"):
                        P_(f"   - *Interfaces.* {', '.join(tb['interfaces'])}")
                    P_(f"   - *Test plan.* {tb.get('test_plan', '')}", f"   - *Risks and rollback.* {tb.get('risks', '')}", "")
            P_("")

    P_("## 8. Open questions, validation and what was not verified", "")
    oqs = [(s["id"], q) for s in stories for q in s.get("open_questions", [])]
    if oqs:
        P_("| Story | Open question | Owner |", "| --- | --- | --- |")
        for sid, q in oqs:
            P_(f"| {sid} | {q['question']} | {q.get('owner') or ''} |")
        P_("")
    if val["errors"]:
        P_("**Validation errors (allowed through)**", "")
        for x in val["errors"]:
            P_(f"- {x}")
        P_("")
    if val["warnings"]:
        P_("**Validation warnings**", "")
        for x in val["warnings"]:
            P_(f"- {x}")
        P_("")
    P_(f"**Facts checked against the repository:** {val['facts']['verified']} verified, {val['facts']['failed']} failed, {val['facts']['unverified']} not verifiable by the tool.", "")
    if val["not_verified"]:
        P_("**Not verified (rest on a command or a document the tool cannot re-run)**", "")
        for x in val["not_verified"]:
            P_(f"- {x}")
        P_("")

    if spec.get("templates"):
        P_("## Appendix. Shared scenario templates", "", "A story that lists a template inherits every scenario of it in addition to its own 3 to 5.", "")
        for k, t in spec["templates"].items():
            P_(f"**{t.get('title', k)}**", "", "```gherkin", U.gherkin(t["scenarios"], f"{k} template"), "```", "")
    return "\n".join(L)


# ------------------------------------------------------------------ ado csv
def md_to_html(text):
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", U.html_escape(text))


def render_ado(spec, comp, team, process, story_effort_field, task_fields, extra, plain, args):
    pr = PROCESSES[process]
    effort_field = pr["effort"] if story_effort_field is None else (story_effort_field or None)
    tfields = pr["task_fields"] if task_fields is None else task_fields
    cols = ["Work Item Type", "Title 1", "Title 2", "Title 3", "Title 4", "Description", "Acceptance Criteria"]
    if effort_field:
        cols.append(effort_field)
    cols += tfields + ["Tags"]
    extra_cols = ["Focused Hours", "Author Hours", "Reviewer Hours", "Lead Time P50 Days", "Lead Time P80 Days", "Lead Time P90 Days"]
    if extra:
        cols += extra_cols
    rows = []

    def row(**kw):
        r = {c: "" for c in cols}
        r.update({k: v for k, v in kw.items() if k in r})
        rows.append(r)

    def desc_text(s, c):
        parts = [U.user_story_line(s)]
        if s.get("context"):
            parts.append("Context: " + s["context"])
        if s.get("facts"):
            parts.append("Facts: " + "; ".join(f"{f.get('claim')} ({f.get('path') or f.get('command') or f.get('source')})" for f in s["facts"]))
        e = c["estimate"]
        est = f"Estimate: size {s.get('size') or '-'}, focused {n1(c['focused_h'])} h, author effort {n1(c['effort']['author_h'])} h"
        if e.get("stable"):
            lt = e["lead_time_calendar_days"]
            est += f", lead time P50 {lt['P50']:.1f} / P80 {lt['P80']:.1f} / P90 {lt['P90']:.1f} calendar days"
        parts.append(est)
        parts.append("Depends on: " + (", ".join(s["deps"]) or "none"))
        if s.get("open_questions"):
            parts.append("Open questions: " + "; ".join(q["question"] for q in s["open_questions"]))
        return parts

    def html_desc(s, c):
        h = [f"<p><b>As</b> {md_to_html(s['role'])}, <b>I want</b> {md_to_html(s['want'])}, <b>so that</b> {md_to_html(s['so'])}.</p>"]
        for p in desc_text(s, c)[1:]:
            k, _, v = p.partition(": ")
            h.append(f"<p><b>{md_to_html(k)}.</b> {md_to_html(v)}</p>")
        return "".join(h)

    def ac_text(s):
        blocks = [U.gherkin(s["scenarios"])]
        for k in s["templates"]:
            t = spec["templates"][k]
            blocks.append(f"Shared template {k}:\n" + U.gherkin(t["scenarios"]))
        return "\n\n".join(blocks)

    def html_ac(s):
        out = f"<pre>{U.html_escape(U.gherkin(s['scenarios']))}</pre>"
        for k in s["templates"]:
            out += f"<p><b>Shared template {U.html_escape(k)}</b></p><pre>{U.html_escape(U.gherkin(spec['templates'][k]['scenarios']))}</pre>"
        return out

    def task_desc(tk):
        tb = tk["task"].get("technical_background", {})
        ev = [f"{f.get('claim', f.get('finding', ''))} ({f.get('path') or f.get('command') or f.get('source')}{':' + str(f['line']) if f.get('line') else ''})" for f in tb.get("evidence", [])]
        items = [("Current state", tb.get("current_state", "")), ("Evidence", "; ".join(ev)), ("Approach", tb.get("approach", "")), ("Touches", ", ".join(tb.get("touches", []))),
                 ("Interfaces", ", ".join(tb.get("interfaces", []))), ("Test plan", tb.get("test_plan", "")), ("Risks and rollback", tb.get("risks", ""))]
        items = [(k, v) for k, v in items if v]
        if plain:
            return "\n".join(f"{k}: {v}" for k, v in items)
        return "".join(f"<p><b>{k}.</b> {md_to_html(v)}</p>" for k, v in items)

    row(**{"Work Item Type": "Epic", "Title 1": spec["epic"].get("title", spec["name"]), "Description": spec["epic"].get("goal", "") if plain else f"<p>{md_to_html(spec['epic'].get('goal', ''))}</p>", "Tags": spec["name"]})
    for f in spec["features"]:
        goal = f.get("goal", "") + (" Exit: " + " | ".join(f["exit_criteria"]) if f.get("exit_criteria") else "")
        row(**{"Work Item Type": "Feature", "Title 2": f"{f['id']}. {f['title']}", "Description": goal if plain else f"<p>{md_to_html(goal)}</p>", "Tags": f"{spec['name']};{f['id']}"})
        for s in [x for x in spec["stories"] if x["feature"] == f["id"]]:
            c = comp[s["id"]]
            e = c["estimate"]
            d = "\n".join(desc_text(s, c)) if plain else html_desc(s, c)
            ac = ac_text(s) if plain else html_ac(s)
            kw = {"Work Item Type": pr["story"], "Title 3": f"{s['id']}. {s['title']}", "Description": d, "Tags": f"{f['id']};{s['type']};{s.get('horizon', '')};{s.get('size') or ''}".replace(";;", ";").strip(";")}
            if pr["ac"]:
                kw["Acceptance Criteria"] = ac
            else:
                kw["Description"] = d + ("\n\n" if plain else "") + ("Acceptance criteria:\n" + ac if plain else "<p><b>Acceptance criteria</b></p>" + ac)
            if effort_field:
                kw[effort_field] = round(c["effort"]["author_h"], 1)
            if extra:
                lt = e["lead_time_calendar_days"] if e.get("stable") else {}
                kw.update({"Focused Hours": round(c["focused_h"], 1), "Author Hours": round(c["effort"]["author_h"], 1), "Reviewer Hours": round(c["effort"]["reviewer_h"], 2),
                           "Lead Time P50 Days": lt.get("P50", ""), "Lead Time P80 Days": lt.get("P80", ""), "Lead Time P90 Days": lt.get("P90", "")})
            row(**kw)
            for tk in allocate_tasks(s, c):
                kw = {"Work Item Type": "Task", "Title 4": f"{s['id']} - {tk['task']['title']}", "Description": task_desc(tk), "Tags": f"{f['id']};{s['id']}"}
                for tf in tfields:
                    kw[tf] = tk["author_h"]
                if extra:
                    kw.update({"Focused Hours": tk["focused_h"], "Author Hours": tk["author_h"]})
                row(**kw)
    return cols, rows


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])


def write_estimates_csv(path, spec, comp, plan_per_story):
    cols = ["id", "kind", "feature", "title", "type", "size", "horizon", "focused_h", "author_h", "reviewer_h", "agent_run_h",
            "P50_working_days", "P80_working_days", "P90_working_days", "P50_calendar_days", "P80_calendar_days", "P90_calendar_days",
            "P50_wall_hours", "P80_wall_hours", "P90_wall_hours", "stage_share_authors_agent", "stage_share_review", "stage_share_release", "stage_share_decisions",
            "bottleneck", "p_first_attempt", "rework_probability", "spec_clarity", "clarity_source", "open_questions", "test_coverage", "test_coverage_source",
            "complexity", "tacit", "prs", "diff_lines", "scope_hours_human_equivalent", "plan_finish_P50_calendar_days", "plan_finish_P80_calendar_days"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(cols)
        for s in spec["stories"]:
            c = comp[s["id"]]
            e = c["estimate"]
            t = c["ticket"]
            st = e.get("stable")
            ps = (plan_per_story or {}).get(s["id"], {})
            w.writerow([s["id"], "story", s["feature"], s["title"], s["type"], s.get("size") or "", s.get("horizon", ""), round(c["focused_h"], 2), round(c["effort"]["author_h"], 2),
                        round(c["effort"]["reviewer_h"], 3), round(c["effort"]["agent_run_h"], 2),
                        *([e["lead_time_working_days"][k] for k in ("P50", "P80", "P90")] if st else ["", "", ""]),
                        *([e["lead_time_calendar_days"][k] for k in ("P50", "P80", "P90")] if st else ["", "", ""]),
                        *([e["lead_time_hours"][k] for k in ("P50", "P80", "P90")] if st else ["", "", ""]),
                        *([e["stage_share"][k] for k in ("dev", "review", "release", "spec")] if st else ["", "", "", ""]),
                        e.get("bottleneck", "") if st else "", round(c["effort"]["p_first"], 3), round(c["effort"]["rework"], 3), c["clarity"], c["clarity_src"], int(t["open_questions"]),
                        t["test_coverage"], c["code_src"]["test_coverage"], t["complexity"], t["tacit"], c["prs"], t["diff_lines"], t["scope_hours"], ps.get("P50", ""), ps.get("P80", "")])
            for tk in allocate_tasks(s, c):
                w.writerow([f"{s['id']}-T", "task", s["feature"], tk["task"]["title"], s["type"], "", s.get("horizon", ""), tk["focused_h"], tk["author_h"]] + [""] * (len(cols) - 9))


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Estimate a story spec and write the markdown, ADO CSV and estimates CSV.")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--team", help="team.json (people, calendar, size scale); omit to use priors")
    ap.add_argument("--repo", default=".", help="repository the facts and paths are checked against")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--name")
    ap.add_argument("--ado-process", choices=sorted(PROCESSES), default="agile")
    ap.add_argument("--story-effort-field", help="ADO field for the story's author hours; '' omits it (default depends on the process)")
    ap.add_argument("--task-fields", help="comma-separated ADO task fields that receive the task's author hours")
    ap.add_argument("--extra-columns", action="store_true", help="add non-ADO analysis columns (hours, lead times) to the ADO CSV")
    ap.add_argument("--plain-text", action="store_true", help="write plain text instead of HTML into the ADO text fields")
    ap.add_argument("--reps", type=int, default=400)
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--sweep", default="authors=1,2,3,4,6")
    ap.add_argument("--no-sweep", action="store_true")
    ap.add_argument("--no-levers", action="store_true")
    ap.add_argument("--no-sensitivity", action="store_true")
    ap.add_argument("--skip-fact-check", action="store_true")
    ap.add_argument("--allow-errors", action="store_true", help="write the files even when validation finds errors")
    args = ap.parse_args(argv)
    args.repo = os.path.abspath(args.repo)

    spec = U.load_spec(args.spec)
    changes = U.normalize_spec(spec)
    name = args.name or spec.get("name") or "backlog"
    spec["name"] = name
    team_raw = json.load(open(args.team, encoding="utf-8")) if args.team else {}
    team = E.merge_team(team_raw)
    val = U.validate_spec(spec, None if args.skip_fact_check else args.repo)
    for c in changes:
        val["warnings"].append("normalised: " + c)
    if val["errors"] and not args.allow_errors:
        print(f"{len(val['errors'])} validation error(s); nothing written:", file=sys.stderr)
        for x in val["errors"]:
            print("  - " + x, file=sys.stderr)
        for x in val["warnings"][:15]:
            print("  (warning) " + x, file=sys.stderr)
        return 2

    build_notes = []
    comp = compute_stories(spec, team, args.repo, args.n, args.seed, build_notes)
    plan_stories = [{"id": s["id"], "deps": s["deps"], "ticket": comp[s["id"]]["ticket"]} for s in spec["stories"]]
    plan = sweep = None
    plan_per_story = {}
    try:
        plan = P.plan(plan_stories, team_raw, args.reps, args.seed, "merge")
        plan_per_story = plan.get("per_story_finish_calendar_days", {})
        if not args.no_sweep and args.sweep:
            key, _, vals = args.sweep.partition("=")
            rows = []
            for v in vals.split(","):
                t2 = dict(team_raw)
                t2[key] = int(v) if key in E.INTEGER_PARAMS else float(v)
                p = P.plan(plan_stories, t2, max(100, args.reps // 2), args.seed, "merge")
                rows.append({key: t2[key], "completion_calendar_days": p["completion_calendar_days"], "completion_calendar_weeks_P80": p["completion_calendar_weeks_P80"],
                             "author_utilization": p["author_utilization"], "reviewer_utilization": p["reviewer_utilization"], "limited_by": p["limited_by"]})
            sweep = {"sweep": key, "rows": rows}
    except (ValueError, RuntimeError, KeyError) as e:
        build_notes.append(f"the whole-backlog simulation could not run: {e}")

    levers, sens = {}, {}
    if E.review_queue(team)["stable"]:
        if not args.no_levers:
            for s in spec["stories"]:
                for r in E.run_levers(comp[s["id"]]["ticket"], team_raw, 3000, args.seed)["levers"]:
                    if r.get("stable") and not r.get("no_op"):
                        levers.setdefault(r["lever"], []).append(r["delta_p80_days"])
        if not args.no_sensitivity:
            for s in spec["stories"]:
                for r in E.prior_sensitivity(comp[s["id"]]["ticket"], team_raw, 2000, args.seed)["params"]:
                    # agent_speed is paired with every story's scope (scope = focused hours x agent speed), so moving it
                    # alone gives a meaningless swing; it is left out
                    if r["swing_days"] is not None and r["param"] != "agent_speed":
                        sens.setdefault(r["param"], []).append(r["swing_days"])

    n_items = 1 + len(spec["features"]) + len(spec["stories"]) + sum(len(s["tasks"]) for s in spec["stories"])
    if n_items > 1000:
        build_notes.append(f"the ADO file has {n_items} work items and Azure DevOps imports at most 1,000 per file; split the backlog (for example by feature) before importing")
    os.makedirs(args.out_dir, exist_ok=True)
    out = lambda suffix: os.path.join(args.out_dir, name + suffix)
    with open(out(".md"), "w", encoding="utf-8") as f:
        f.write(render_md(spec, team, comp, plan, sweep, levers, sens, val, name, args, build_notes))
    tf = None if args.task_fields is None else [x.strip() for x in args.task_fields.split(",") if x.strip()]
    cols, rows = render_ado(spec, comp, team, args.ado_process, args.story_effort_field, tf, args.extra_columns, args.plain_text, args)
    write_csv(out("_ado.csv"), cols, rows)
    write_estimates_csv(out("_estimates.csv"), spec, comp, plan_per_story)
    U.save_spec(spec, out(".spec.json"))

    summary = {"features": len(spec["features"]), "stories": len(spec["stories"]), "scenarios": sum(len(s["scenarios"]) for s in spec["stories"]),
               "tasks": sum(len(s["tasks"]) for s in spec["stories"]), "focused_h": round(sum(c["focused_h"] for c in comp.values()), 1),
               "author_h": round(sum(c["effort"]["author_h"] for c in comp.values()), 1), "plan_calendar_days": plan["completion_calendar_days"] if plan else None,
               "warnings": len(val["warnings"]), "errors_allowed": len(val["errors"]), "files": [out(x) for x in (".md", "_ado.csv", "_estimates.csv", ".spec.json")]}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
