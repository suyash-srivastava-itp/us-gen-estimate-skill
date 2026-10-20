#!/usr/bin/env python3
"""
Plan a whole backlog: when does it finish with N authors, M reviewers and these calendars?

estimate.py forecasts one ticket alone. plan.py runs a discrete-event simulation over the dependency graph, so the
things a single ticket cannot show appear by themselves: authors being busy, work waiting on dependencies, review
queues that grow because the authors are productive, and release batches.

Resources (from team.json, all in working days):
  authors     `authors` servers. A story needs one author for its human effort (steering, attended agent time, triage,
              fallback), then runs unattended agent time without holding the author, then waits in the review queue.
              At most authors x parallel_sessions stories are between "author starts" and "PR opened" at once.
  reviewers   `reviewers` servers (0 = PRs merge when opened). One reviewer-effort block per pass; a rework pass sends the
              story back to an author first (rework has priority over starting new stories).
  release     merged stories are deployed at the next batch boundary (every release_interval_days, random phase) + deploy.
  decisions   a story's open questions delay it by a Gamma wait (decision_latency_days) once its dependencies are done.

Each replication draws every story's outcome with estimate.sample_components (the same distributions as the
single-ticket model), then schedules. Stories are picked by longest remaining path first.

Backlog JSON: {"stories": [{"id": "S1.1", "deps": [], "ticket": {spec_clarity, open_questions, scope_hours, ...}}, ...]}

Usage:
  python3 plan.py --backlog backlog.json --team team.json [--reps 400] [--seed 0]
                  [--set authors=3] [--sweep authors=1,2,3,4] [--dep-gate merge|release] [--out plan.json]
"""
import argparse
import heapq
import itertools
import json
import math
import os
import sys
sys.dont_write_bytecode = True  # keep the skill folder free of __pycache__
from collections import deque

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import estimate as E  # noqa: E402


def topo_check(ids, deps):
    seen, stack = {}, []

    def visit(i):
        if seen.get(i) == 2:
            return
        if seen.get(i) == 1:
            raise ValueError("dependency cycle through " + " -> ".join(stack + [i]))
        seen[i] = 1
        stack.append(i)
        for d in deps[i]:
            visit(d)
        stack.pop()
        seen[i] = 2

    for i in ids:
        visit(i)


def prepare(stories, team, reps, seed):
    ids = [s["id"] for s in stories]
    index = {s["id"]: k for k, s in enumerate(stories)}
    deps = [[index[d] for d in s.get("deps", [])] for s in stories]
    for s in stories:
        for d in s.get("deps", []):
            if d not in index:
                raise ValueError(f"{s['id']}: unknown dependency {d}")
    rng = np.random.default_rng(seed)
    comps = [E.sample_components(E.validate_ticket(s["ticket"])[0], team, reps, rng) for s in stories]
    return ids, index, deps, comps


def priorities(deps, comps, team):
    cv = E.conversions(team)
    n = len(deps)
    children = [[] for _ in range(n)]
    for i, ds in enumerate(deps):
        for d in ds:
            children[d].append(i)
    # Mean time a story keeps its successors waiting even with idle people: author effort, unattended agent time,
    # reviewer service per pass, the decision wait, and half a release interval plus the deploy.
    release_wd = 0.5 * team["release_interval_days"] + team["deploy_h"] * cv["wall_to_wd"]
    own = [float(np.mean(c["author_h_pre"] + c["author_h_fix"])) / cv["a_rate"] + float(np.mean(c["unattended_days"]))
           + float(np.mean(c["passes"])) * c["rev_eff_h"] / cv["r_rate"] + float(np.mean(c["spec_days"])) + release_wd
           for c in comps]
    memo = {}

    def longest(i):
        if i not in memo:
            memo[i] = own[i] + max((longest(c) for c in children[i]), default=0.0)
        return memo[i]

    return children, own, [longest(i) for i in range(n)]


def simulate_rep(r, deps, children, comps, team, cv, prio, dep_gate, rng):
    n = len(deps)
    A, C = int(team["authors"]), int(team["reviewers"])
    cap_wip = A * int(team["parallel_sessions"])
    interval = team["release_interval_days"]
    deploy_wd = team["deploy_h"] * cv["wall_to_wd"]
    phase = rng.uniform(0.0, interval) if interval > 0 else 0.0
    events, seq = [], itertools.count()
    push = lambda t, kind, i, p=0: heapq.heappush(events, (t, next(seq), kind, i, p))
    remaining = [len(d) for d in deps]
    gate_t = [0.0] * n
    ready_at = [0.0] * n
    start_q, rework_q, rev_q = [], [], deque()
    st = {"a_free": A, "r_free": C, "wip": 0, "busy_a": 0.0, "busy_r": 0.0, "a_wait": 0.0, "r_wait": 0.0}
    merged, released = [None] * n, [None] * n

    def spec(i):
        return float(comps[i]["spec_days"][r])

    def try_authors(t):
        while st["a_free"] > 0:
            if rework_q:
                _, _, i, p = heapq.heappop(rework_q)
                dur = float(comps[i]["author_h_fix"][r]) / max(int(comps[i]["passes"][r]) - 1, 1) / cv["a_rate"]
                st["a_free"] -= 1
                st["busy_a"] += dur
                push(t + dur, "fix_done", i, p)
            elif start_q and st["wip"] < cap_wip:
                _, _, i = heapq.heappop(start_q)
                dur = float(comps[i]["author_h_pre"][r]) / cv["a_rate"]
                st["a_free"] -= 1
                st["wip"] += 1
                st["a_wait"] += t - ready_at[i]
                st["busy_a"] += dur
                push(t + dur, "start_done", i)
            else:
                break

    def try_reviewers(t):
        while st["r_free"] > 0 and rev_q:
            i, p, qt = rev_q.popleft()
            dur = comps[i]["rev_eff_h"] / cv["r_rate"]
            st["r_free"] -= 1
            st["r_wait"] += t - qt
            st["busy_r"] += dur
            push(t + dur, "review_done", i, p)

    def merge(t, i):
        merged[i] = t
        rel = (math.ceil((t - phase) / interval - 1e-9) * interval + phase if interval > 0 else t) + deploy_wd
        released[i] = rel
        gate = t if dep_gate == "merge" else rel
        for c in children[i]:
            remaining[c] -= 1
            gate_t[c] = max(gate_t[c], gate)
            if remaining[c] == 0:
                push(max(gate_t[c], t) + spec(c), "eligible", c)

    for i in range(n):
        if not deps[i]:
            push(spec(i), "eligible", i)
    while events:
        t, _, kind, i, p = heapq.heappop(events)
        if kind == "eligible":
            ready_at[i] = t
            heapq.heappush(start_q, (-prio[i], next(seq), i))
            try_authors(t)
        elif kind == "start_done":
            st["a_free"] += 1
            try_authors(t)
            push(t + float(comps[i]["unattended_days"][r]), "pr_open", i)
        elif kind == "pr_open":
            st["wip"] -= 1
            try_authors(t)
            if C == 0:
                merge(t, i)
            else:
                rev_q.append((i, 1, t))
                try_reviewers(t)
        elif kind == "review_done":
            st["r_free"] += 1
            try_reviewers(t)
            if p < int(comps[i]["passes"][r]):
                heapq.heappush(rework_q, (-prio[i] - 1e6, next(seq), i, p))
                try_authors(t)
            else:
                merge(t, i)
        elif kind == "fix_done":
            st["a_free"] += 1
            try_authors(t)
            rev_q.append((i, p + 1, t))
            try_reviewers(t)
    if any(x is None for x in released):
        raise RuntimeError("some stories never finished: check dependencies")
    makespan = max(released)
    return {"released": released, "makespan": makespan,
            "a_util": st["busy_a"] / (A * makespan) if makespan > 0 else 0.0,
            "r_util": st["busy_r"] / (C * makespan) if C and makespan > 0 else 0.0,
            "a_wait": st["a_wait"], "r_wait": st["r_wait"]}


def plan(stories, team_overrides=None, reps=400, seed=0, dep_gate="merge"):
    team = E.merge_team(team_overrides)
    cv = E.conversions(team)
    ids, index, deps, comps = prepare(stories, team, reps, seed)
    topo_check(list(range(len(ids))), {k: deps[k] for k in range(len(ids))})
    children, own, prio = priorities(deps, comps, team)
    rng = np.random.default_rng(seed + 1)
    res = [simulate_rep(r, deps, children, comps, team, cv, prio, dep_gate, rng) for r in range(reps)]
    mk = np.array([x["makespan"] for x in res])
    cal = cv["cal_per_wd"]
    fin = np.array([x["released"] for x in res])
    n = len(ids)
    pr = lambda a, q: float(np.percentile(a, q))
    total_author_days = sum(float(np.mean(c["author_h_pre"] + c["author_h_fix"])) / cv["a_rate"] for c in comps)
    crit_days = max(prio)
    a_util, r_util = float(np.mean([x["a_util"] for x in res])), float(np.mean([x["r_util"] for x in res]))
    a_wait, r_wait = float(np.mean([x["a_wait"] for x in res])), float(np.mean([x["r_wait"] for x in res]))
    C = int(team["reviewers"])
    rev_total_days = sum(float(np.mean(c["passes"])) * c["rev_eff_h"] for c in comps) / cv["r_rate"]
    bounds = {"critical path": crit_days, "author capacity": total_author_days / int(team["authors"])}
    if C > 0:
        bounds["reviewer capacity"] = rev_total_days / C
    limiter = max(bounds, key=bounds.get)
    return {
        "reps": reps, "stories": n, "dep_gate": dep_gate,
        "resources": {k: team[k] for k in ("authors", "author_hours_per_day", "author_availability", "reviewers",
                                           "reviewer_hours_per_day", "reviewer_availability", "days_per_week",
                                           "parallel_sessions", "agent_attended_fraction", "release_interval_days")},
        "completion_calendar_days": {f"P{q}": round(pr(mk, q) * cal, 1) for q in (50, 80, 90)},
        "completion_working_days": {**{f"P{q}": round(pr(mk, q), 1) for q in (50, 80, 90)}, "mean": round(float(mk.mean()), 1)},
        "completion_calendar_weeks_P80": round(pr(mk, 80) * cal / 7.0, 1),
        "author_utilization": round(a_util, 3), "reviewer_utilization": round(r_util, 3),
        "story_wait_working_days_summed_over_backlog": {"for_an_author": round(a_wait, 1), "for_a_reviewer": round(r_wait, 1)},
        "limited_by": limiter,
        "lower_bounds_working_days": {
            **{k.replace(" ", "_") + "_mean": round(v, 1) for k, v in bounds.items()},
            "note": "Mean working days with idle people elsewhere. critical_path is the longest dependency chain counting "
                    "author effort, unattended agent time, reviewer service, decision waits and half a release interval. "
                    "The largest bound names what limits the plan; the simulated MEAN completion (not the P50, which a skewed "
                    "finish time can place below a mean) must sit at or above all of them."},
        "per_story_finish_calendar_days": {ids[i]: {"P50": round(pr(fin[:, i], 50) * cal, 1), "P80": round(pr(fin[:, i], 80) * cal, 1)}
                                           for i in range(n)},
        "required_at_prior": E.required_at_prior(team),
    }


def parse_set(items):
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        if k not in E.DEFAULT_TEAM:
            raise ValueError(f"unknown team parameter: {k}")
        out[k] = int(v) if k in E.INTEGER_PARAMS else float(v)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Plan a backlog with N authors, M reviewers and calendars. JSON on stdout.")
    ap.add_argument("--backlog", required=True)
    ap.add_argument("--team")
    ap.add_argument("--reps", type=int, default=400)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a team parameter")
    ap.add_argument("--sweep", metavar="KEY=V1,V2,..", help="run the plan once per value of one team parameter")
    ap.add_argument("--dep-gate", choices=["merge", "release"], default="merge",
                    help="a dependency counts as done when merged (default) or when released")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    try:
        with open(args.backlog, encoding="utf-8") as f:
            stories = json.load(f)["stories"]
        team = {}
        if args.team:
            with open(args.team, encoding="utf-8") as f:
                team = json.load(f)
        team.update(parse_set(args.set))
        if args.sweep:
            key, _, vals = args.sweep.partition("=")
            if key not in E.DEFAULT_TEAM:
                raise ValueError(f"unknown team parameter: {key}")
            rows = []
            for v in vals.split(","):
                t2 = dict(team)
                t2[key] = int(v) if key in E.INTEGER_PARAMS else float(v)
                p = plan(stories, t2, args.reps, args.seed, args.dep_gate)
                rows.append({key: t2[key], "completion_calendar_days": p["completion_calendar_days"],
                             "completion_calendar_weeks_P80": p["completion_calendar_weeks_P80"],
                             "author_utilization": p["author_utilization"], "reviewer_utilization": p["reviewer_utilization"],
                             "limited_by": p["limited_by"]})
            out = {"sweep": key, "rows": rows, "required_at_prior": E.required_at_prior(E.merge_team(team))}
        else:
            out = plan(stories, team, args.reps, args.seed, args.dep_gate)
    except (ValueError, OSError, RuntimeError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    text = json.dumps(out, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
