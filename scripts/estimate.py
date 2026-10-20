#!/usr/bin/env python3
"""
Monte Carlo lead-time estimator for AI-assisted development tickets (schema v2: people and calendar).

The unit inside the model is the team's WORKING DAY. Every duration is converted with the team's calendar,
so nothing mixes wall-clock hours with working hours:

  author effort (hours)   -> days = hours / (author_hours_per_day * author_availability)
  reviewer effort (hours) -> days = hours / (reviewer_hours_per_day * reviewer_availability)
  unattended agent run    -> wall-clock hours; days = hours / 24 * days_per_week / 7   (agents run overnight)
  release wait            -> Uniform(0, release_interval_days) + deploy time
  decision wait           -> Gamma(K, decision_latency_days)

Lead time T = T_spec + T_dev + T_rev + T_rel, reported in working days, calendar days and wall-clock hours.

  T_spec  Each open question blocks the ticket with probability (1 - spec_clarity): K ~ Poisson(open_questions *
          (1 - spec_clarity)) blocking decisions, each an Exp wait with mean decision_latency_days (decision owners
          are not the authors: this wait does not use author time).
  T_dev   Attempts N ~ Geometric(p), p = sigmoid(clarity, test coverage, complexity, tacit knowledge). Each attempt runs
          the agent for scope_hours / agent_speed (lognormal) + ci_hours. The author must be present for
          agent_attended_fraction of that run (Claude Code used interactively: 1.0; background runs: 0), plus steer_h
          per attempt and triage_h per failed attempt. After max_agent_attempts a person takes over (scope_hours).
  T_rev   Review is an M/M/c queue (Erlang-C): c = reviewers, each serving mu PRs per working day from
          reviewer_hours_per_day * reviewer_availability; PR arrivals lambda = authors * author_hours_per_day *
          author_availability / mean_author_hours_per_pr (or the measured pr_arrivals_per_day). More authors therefore
          mean more review load. Each pass adds reviewer effort (diff_lines / lines_per_reviewer_hour). Rework passes
          are Geometric(1 - q); each costs rework_fix_h of author time. reviewers = 0 means PRs merge without
          independent review.
  T_rel   Release batching Uniform(0, release_interval_days) plus deploy time.

Every team parameter carries a provenance (team["provenance"][key]["source"] = user | measured | prior). The output
lists the required parameters still at a prior, and --sensitivity ranks the prior parameters by how far they move the
P80, so the reader sees how much of the answer is assumption.

Usage:
  python3 estimate.py --ticket ticket.json [--team team.json] [--n 20000] [--seed 0] [--levers] [--sensitivity]
                      [--require-complete]
  python3 estimate.py --write-default-team team.json
"""
import argparse
import copy
import json
import math
import sys

import numpy as np

SCHEMA_VERSION = 2

DEFAULT_TEAM = {
    "schema_version": SCHEMA_VERSION,
    "calibrated": False,
    # ---- people and calendar: ask the team (assets/team_questions.json); never guess these silently
    "authors": 2,                    # people producing PRs with an agent
    "author_hours_per_day": 6.0,     # focused hours per working day on this work
    "author_availability": 0.8,      # fraction of those hours free for THIS backlog (other lanes, meetings, leave)
    "reviewers": 2,                  # people who review PRs; 0 = PRs merge without independent review
    "reviewer_hours_per_day": 6.0,
    "reviewer_availability": 0.3,    # fraction of their day spent reviewing for this team
    "days_per_week": 5.0,
    "parallel_sessions": 3,          # plan.py: stories one author can have in flight at once
    "agent_attended_fraction": 1.0,  # share of an agent run the author must stay for (1 = interactive)
    # ---- work flow
    "mean_author_hours_per_pr": 6.0,  # author effort per PR in steady state; sets PR arrivals
    "pr_arrivals_per_day": None,      # measured PRs per working day, team-wide; overrides the derived rate
    "steer_h": 0.25,                  # author hours per attempt reading and steering the output
    # First-attempt success: p = sigmoid(b0 + b_clarity*c + b_tests*tests - b_complex*complexity - b_tacit*tacit)
    "b0": -0.5, "b_clarity": 2.0, "b_tests": 1.5, "b_complex": 1.2, "b_tacit": 1.0,
    "prob_concentration": 30.0,
    # Agent
    "agent_speed": 4.0,              # human-equivalent hours completed per agent run hour
    "ci_hours": 0.25,                # verification latency per attempt (wall clock)
    "triage_h": 0.5,                 # author hours per failed attempt
    "max_agent_attempts": 5,
    # Decisions
    "decision_latency_days": 1.0,    # mean working days a blocking decision waits
    # Review
    "lines_per_reviewer_hour": 200.0,
    "mean_pr_lines": 300.0,
    # Rework: q = sigmoid(r0 + r_size*log1p(diff/100) - r_tests*tests - r_clarity*c)
    "r0": -1.5, "r_size": 1.0, "r_tests": 1.5, "r_clarity": 1.0,
    "rework_fix_h": 0.5,             # author hours per rework pass
    # Release
    "release_interval_days": 1.0,    # working days between release batches; 0 = continuous deploy
    "deploy_h": 0.5,
    # ---- story sizing (used by us_build.py; asked with team_setup.py questions --stories)
    # Focused hours per size: the hours one person works with the agent from branch start to a PR ready for review.
    "size_scale": {"default": {"S": 1, "M": 3, "L": 8, "XL": 16}, "by_type": {}},
    "focused_includes_retries": 1.0,  # 1 = the focused hours already contain retries and steering, 0 = one attempt only
    "typical_pr_lines": 400.0,        # median changed lines of one PR; story diff size = PRs x this when not given
}

# The parameters a person must supply (or that must be mined from history) before a forecast is trustworthy.
REQUIRED_TEAM = ["authors", "author_hours_per_day", "author_availability", "reviewers", "reviewer_hours_per_day",
                 "reviewer_availability", "days_per_week", "agent_attended_fraction", "release_interval_days",
                 "decision_latency_days"]

TEAM_RANGES = {
    "authors": (1, 100), "author_hours_per_day": (0.5, 14), "author_availability": (0.05, 1.0),
    "reviewers": (0, 100), "reviewer_hours_per_day": (0.5, 14), "reviewer_availability": (0.02, 1.0),
    "days_per_week": (1, 7), "parallel_sessions": (1, 20), "agent_attended_fraction": (0.0, 1.0),
    "mean_author_hours_per_pr": (0.25, 80), "steer_h": (0.0, 8), "agent_speed": (0.2, 50),
    "ci_hours": (0.0, 48), "triage_h": (0.0, 24), "max_agent_attempts": (1, 20),
    "decision_latency_days": (0.0, 60), "lines_per_reviewer_hour": (10, 50000), "mean_pr_lines": (10, 100000),
    "rework_fix_h": (0.0, 24), "release_interval_days": (0.0, 60), "deploy_h": (0.0, 48),
    "focused_includes_retries": (0.0, 1.0), "typical_pr_lines": (5, 100000),
}

TICKET_RANGES = {
    "spec_clarity": (0.0, 1.0),
    "open_questions": (0.0, 50.0),
    "scope_hours": (0.1, 500.0),
    "diff_lines": (1.0, 50000.0),
    "test_coverage": (0.0, 1.0),
    "complexity": (0.0, 1.0),
    "tacit": (0.0, 1.0),
}
REQUIRED_FIELDS = ["spec_clarity", "open_questions", "scope_hours", "diff_lines"]
ASSUMED_DEFAULTS = {"test_coverage": 0.5, "complexity": 0.5, "tacit": 0.5}

# Parameters whose value moves the forecast, for the prior-sensitivity ranking.
SENSITIVITY_PARAMS = ["authors", "author_hours_per_day", "author_availability", "reviewers", "reviewer_hours_per_day",
                      "reviewer_availability", "days_per_week", "agent_attended_fraction", "mean_author_hours_per_pr",
                      "steer_h", "agent_speed", "ci_hours", "triage_h", "decision_latency_days",
                      "lines_per_reviewer_hour", "mean_pr_lines", "release_interval_days", "deploy_h", "rework_fix_h"]
INTEGER_PARAMS = {"authors", "reviewers", "parallel_sessions", "max_agent_attempts"}


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_ticket(raw):
    missing = [k for k in REQUIRED_FIELDS if k not in raw]
    if missing:
        raise ValueError(f"ticket is missing required fields: {missing}")
    t = dict(ASSUMED_DEFAULTS)
    t.update({k: raw[k] for k in raw if k in TICKET_RANGES})
    assumed = [k for k in ASSUMED_DEFAULTS if k not in raw]
    for k, (lo, hi) in TICKET_RANGES.items():
        v = float(t[k])
        if not lo <= v <= hi:
            raise ValueError(f"{k}={v} is outside the allowed range [{lo}, {hi}]")
        t[k] = v
    return t, assumed


def _legacy(overrides, team):
    """Map schema-v1 team files onto v2 so an old team.json still loads."""
    o = dict(overrides)
    if o.get("schema_version", 1) >= 2:
        return o
    hpd = o.get("productive_hours_per_day")
    if hpd is not None and "author_hours_per_day" not in o:
        o["author_hours_per_day"] = hpd
        o.setdefault("reviewer_hours_per_day", hpd)
    ah = o.get("author_hours_per_day", team["author_hours_per_day"])
    if "decision_latency_h" in o and "decision_latency_days" not in o:
        o["decision_latency_days"] = o["decision_latency_h"] / max(ah, 1e-9)
    if "release_interval_h" in o and "release_interval_days" not in o:
        o["release_interval_days"] = o["release_interval_h"] / max(ah, 1e-9)
    if "pr_arrivals_per_h" in o and "pr_arrivals_per_day" not in o:
        o["pr_arrivals_per_day"] = o["pr_arrivals_per_h"] * ah
    for k in ("productive_hours_per_day", "decision_latency_h", "release_interval_h", "pr_arrivals_per_h"):
        o.pop(k, None)
    return o


def merge_team(overrides):
    team = copy.deepcopy(DEFAULT_TEAM)
    team["provenance"] = {}
    o = _legacy(overrides or {}, team)
    team.update(o)
    team["schema_version"] = SCHEMA_VERSION
    for k, (lo, hi) in TEAM_RANGES.items():
        v = team.get(k)
        if v is not None and not lo <= float(v) <= hi:
            raise ValueError(f"team parameter {k}={v} is outside the allowed range [{lo}, {hi}]")
    return team


def source_of(team, key):
    prov = (team.get("provenance") or {}).get(key)
    if prov:
        return prov.get("source", "prior")
    if team.get("calibrated") and key in (team.get("fitted_params") or []):
        return "measured"
    if key in (team.get("user_set_params") or []):
        return "user"
    return "prior"


def required_at_prior(team):
    """Required parameters nobody supplied. With no reviewers the reviewer time parameters do not matter."""
    skip = {"reviewer_hours_per_day", "reviewer_availability"} if team.get("reviewers") == 0 else set()
    return [k for k in REQUIRED_TEAM if k not in skip and source_of(team, k) == "prior"]


def conversions(team):
    a_rate = team["author_hours_per_day"] * team["author_availability"]       # author hours per working day
    r_rate = team["reviewer_hours_per_day"] * team["reviewer_availability"]   # reviewer hours per working day
    return {
        "a_rate": a_rate,
        "r_rate": r_rate,
        "wall_to_wd": (team["days_per_week"] / 7.0) / 24.0,                    # working days per wall-clock hour
        "cal_per_wd": 7.0 / team["days_per_week"],
    }


def erlang_c(c, a):
    """Probability an arrival waits in an M/M/c queue with offered load a = lambda/mu (requires a < c)."""
    rho = a / c
    s = sum(a ** k / math.factorial(k) for k in range(c))
    top = a ** c / math.factorial(c) / (1.0 - rho)
    return top / (s + top)


def review_queue(team):
    """Return the M/M/c description of the review queue, in PRs per working day."""
    cv = conversions(team)
    c = int(team["reviewers"])
    lam = team["pr_arrivals_per_day"]
    derived = lam is None
    if derived:
        lam = team["authors"] * cv["a_rate"] / team["mean_author_hours_per_pr"]
    out = {"lambda_per_day": lam, "lambda_derived": derived, "reviewers": c}
    if c == 0:
        out.update(mu_per_reviewer=None, utilization=0.0, wait_prob=0.0, stable=True)
        return out
    mu = cv["r_rate"] * team["lines_per_reviewer_hour"] / team["mean_pr_lines"]
    rho = lam / (c * mu)
    out.update(mu_per_reviewer=mu, utilization=rho, stable=rho < 1.0)
    if rho < 1.0:
        out.update(wait_prob=erlang_c(c, lam / mu), wait_rate=c * mu - lam)
    return out


def sample_components(t, team, n, rng):
    """Draw n independent outcomes for one ticket. Shared by estimate() and plan.py. All times in working days."""
    cv = conversions(team)
    c = t["spec_clarity"]
    k = rng.poisson(t["open_questions"] * (1.0 - c), n)
    spec_days = np.where(k > 0, rng.gamma(np.maximum(k, 1), max(team["decision_latency_days"], 1e-9)), 0.0)

    logit = (team["b0"] + team["b_clarity"] * c + team["b_tests"] * t["test_coverage"]
             - team["b_complex"] * t["complexity"] - team["b_tacit"] * t["tacit"])
    p_bar = float(np.clip(sigmoid(logit), 0.01, 0.99))
    kappa = team["prob_concentration"]
    p = np.clip(rng.beta(p_bar * kappa, (1.0 - p_bar) * kappa, n), 0.01, 1.0)
    attempts = rng.geometric(p)
    fallback = attempts > team["max_agent_attempts"]
    used = np.minimum(attempts, team["max_agent_attempts"])
    run_h = (t["scope_hours"] / team["agent_speed"]) * rng.lognormal(0.0, 0.3, n) + team["ci_hours"]
    agent_h = used * run_h
    attended = team["agent_attended_fraction"]
    author_h_pre = (used * team["steer_h"] + (used - 1) * team["triage_h"] + fallback * t["scope_hours"]
                    + attended * agent_h)
    unattended_days = (1.0 - attended) * agent_h * cv["wall_to_wd"]

    q = float(np.clip(sigmoid(team["r0"] + team["r_size"] * np.log1p(t["diff_lines"] / 100.0)
                              - team["r_tests"] * t["test_coverage"] - team["r_clarity"] * c), 0.0, 0.95))
    if team["reviewers"] == 0:
        passes = np.ones(n, dtype=int)
        rev_eff_h = 0.0
    else:
        passes = np.minimum(rng.geometric(1.0 - q, n), 20)
        rev_eff_h = t["diff_lines"] / team["lines_per_reviewer_hour"]
    author_h_fix = (passes - 1) * team["rework_fix_h"]
    return {
        "spec_days": spec_days, "agent_h": agent_h, "author_h_pre": author_h_pre, "unattended_days": unattended_days,
        "author_h_fix": author_h_fix, "passes": passes, "rev_eff_h": rev_eff_h,
        "p_first_attempt": p_bar, "rework_prob": q,
    }


def simulate(t, team, n, seed):
    rng = np.random.default_rng(seed)
    cv = conversions(team)
    comp = sample_components(t, team, n, rng)
    rq = review_queue(team)

    t_dev = (comp["author_h_pre"] + comp["author_h_fix"]) / cv["a_rate"] + comp["unattended_days"]
    if team["reviewers"] == 0:
        t_rev = np.zeros(n)
    else:
        # Each pass waits with probability wait_prob (Erlang-C), the wait given waiting being Exp(c*mu - lambda).
        # The number of waiting passes is Binomial; the sum of that many Exp waits is Gamma.
        n_wait = rng.binomial(comp["passes"], rq["wait_prob"])
        waits = np.where(n_wait > 0, rng.gamma(np.maximum(n_wait, 1), 1.0 / rq["wait_rate"]), 0.0)
        t_rev = waits + comp["passes"] * comp["rev_eff_h"] / cv["r_rate"]
    interval = team["release_interval_days"]
    t_rel = (rng.uniform(0.0, interval, n) if interval > 0 else np.zeros(n)) + team["deploy_h"] * cv["wall_to_wd"]
    parts = {"spec": comp["spec_days"], "dev": t_dev, "review": t_rev, "release": t_rel}
    effort = {"author_h": float(np.mean(comp["author_h_pre"] + comp["author_h_fix"])),
              "reviewer_h": float(np.mean(comp["passes"]) * comp["rev_eff_h"]),
              "agent_run_h": float(np.mean(comp["agent_h"]))}
    diag = {"p_first_attempt": comp["p_first_attempt"], "rework_prob": comp["rework_prob"], "effort": effort, "queue": rq}
    return parts, diag


def _estimate(t, team, n, seed):
    parts, diag = simulate(t, team, n, seed)
    cv = conversions(team)
    total = sum(parts.values())
    pct_wd = {f"P{q}": float(np.percentile(total, q)) for q in (50, 80, 90)}
    stage_means = {k: float(v.mean()) for k, v in parts.items()}
    mean_total = sum(stage_means.values())
    return {
        "stable": True,
        "lead_time_working_days": {**{k: round(v, 2) for k, v in pct_wd.items()}, "mean": round(float(total.mean()), 2)},
        "lead_time_calendar_days": {k: round(v * cv["cal_per_wd"], 2) for k, v in pct_wd.items()},
        "lead_time_hours": {k: round(v * cv["cal_per_wd"] * 24.0, 1) for k, v in pct_wd.items()},
        "stage_mean_working_days": {k: round(v, 3) for k, v in stage_means.items()},
        "stage_share": {k: round(v / mean_total, 3) for k, v in stage_means.items()},
        "bottleneck": max(stage_means, key=stage_means.get),
        "effort_hours_mean": {k: round(v, 2) for k, v in diag["effort"].items()},
        "review_utilization": round(diag["queue"]["utilization"], 3),
        "p_first_attempt": round(diag["p_first_attempt"], 3),
        "rework_probability": round(diag["rework_prob"], 3),
    }


def estimate(raw_ticket, team_overrides=None, n=20000, seed=0):
    t, assumed = validate_ticket(raw_ticket)
    team = merge_team(team_overrides)
    warnings = []
    if not team["calibrated"]:
        warnings.append("Team parameters are not calibrated on completed tickets (calibrate.py has not been run). "
                        "Treat the numbers as an order-of-magnitude guess.")
    pend = required_at_prior(team)
    if pend:
        warnings.append("Required team parameters still at the estimator's prior (ask the team, or mine them with "
                        f"mine_history.py): {', '.join(pend)}.")
    if assumed:
        warnings.append(f"Not provided, assumed 0.5: {', '.join(assumed)}. Measure them with codebase_features.py.")
    kept = team.get("priors_kept") or []
    if team["calibrated"] and kept:
        warnings.append(f"Still at prior values, not measured from history: {', '.join(kept)}.")
    if t["test_coverage"] < 0.3:
        warnings.append("Test coverage in the affected area is low. Agent verification is weak, "
                        "so development estimates are likely optimistic.")
    if team["reviewers"] == 0:
        warnings.append("reviewers = 0: PRs are assumed to merge with no independent review, so there is no review "
                        "queue or rework in this forecast.")

    rq = review_queue(team)
    if not rq["stable"]:
        cv = conversions(team)
        mu = rq["mu_per_reviewer"]
        return {
            "stable": False,
            "bottleneck": "review",
            "review_utilization": round(rq["utilization"], 3),
            "message": ("Review queue is unstable: the authors open about "
                        f"{rq['lambda_per_day']:.2f} PRs per working day and {rq['reviewers']} reviewer(s) can clear "
                        f"{rq['reviewers'] * mu:.2f}. No finite lead time exists. Add reviewer time, add reviewers, "
                        "or cut PR arrivals (fewer authors or larger author effort per PR)."),
            "warnings": warnings,
        }
    if rq["utilization"] > 0.85:
        warnings.append(f"Review utilization is {rq['utilization']:.2f}. Queue waits are very sensitive to "
                        "small changes, so the P90 is unreliable.")
    out = _estimate(t, team, n, seed)
    out.update(inputs_used=t, warnings=warnings, required_at_prior=pend,
               team_used={k: team[k] for k in REQUIRED_TEAM})
    return out


def prior_sensitivity(raw_ticket, team_overrides=None, n=6000, seed=0):
    """One-at-a-time swing of the P80 (calendar days) for each prior-sourced parameter: low = 0.5x, high = 1.5x
    (integers: -1 / +1, availabilities capped at 1). Ranks how much of the answer rests on an assumption."""
    t, _ = validate_ticket(raw_ticket)
    team = merge_team(team_overrides)
    base = _estimate(t, team, n, seed) if review_queue(team)["stable"] else None
    rows = []
    for key in SENSITIVITY_PARAMS:
        if source_of(team, key) != "prior" or team.get(key) is None:
            continue
        v = team[key]
        if key in INTEGER_PARAMS:
            lo_v, hi_v = max(int(v) - 1, TEAM_RANGES[key][0]), int(v) + 1
        else:
            lo_v, hi_v = v * 0.5, v * 1.5
        lo_lim, hi_lim = TEAM_RANGES[key]
        lo_v, hi_v = max(lo_v, lo_lim), min(hi_v, hi_lim)
        res = {}
        for tag, val in (("low", lo_v), ("high", hi_v)):
            tm = dict(team); tm[key] = val
            if not review_queue(tm)["stable"]:
                res[tag] = None
            else:
                res[tag] = _estimate(t, tm, n, seed)["lead_time_calendar_days"]["P80"]
        vals = [x for x in res.values() if x is not None]
        swing = (max(vals) - min(vals)) if len(vals) == 2 else None
        rows.append({"param": key, "prior": v, "low_value": lo_v, "high_value": hi_v,
                     "p80_days_at_low": res["low"], "p80_days_at_high": res["high"],
                     "swing_days": None if swing is None else round(swing, 2),
                     "note": "unstable at one end" if swing is None else ""})
    rows.sort(key=lambda r: (r["swing_days"] is not None, -(r["swing_days"] or 0)))
    return {"base_p80_calendar_days": None if base is None else base["lead_time_calendar_days"]["P80"], "params": rows}


# Each lever returns (ticket_changes, team_changes) given the current ticket and team.
LEVERS = [
    ("resolve_open_questions", "Answer every open question before starting",
     lambda t, tm: ({"open_questions": 0.0}, {})),
    ("spec_clarity_0.8", "Bring spec clarity to 0.8 (criteria, edge cases, examples)",
     lambda t, tm: ({"spec_clarity": max(t["spec_clarity"], 0.8)}, {})),
    ("test_coverage_0.7", "Raise test coverage in the affected area to 0.7 first",
     lambda t, tm: ({"test_coverage": max(t["test_coverage"], 0.7)}, {})),
    ("split_in_half", "Split the ticket; reports P80 for one half (scope and diff halved, open questions kept)",
     lambda t, tm: ({"scope_hours": t["scope_hours"] / 2, "diff_lines": t["diff_lines"] / 2}, {})),
    ("add_reviewer", "Add one reviewer to the rotation",
     lambda t, tm: ({}, {"reviewers": tm["reviewers"] + 1})),
    ("reviewer_time_+25pct", "Give reviewers 25% more review time (availability, capped at 1)",
     lambda t, tm: ({}, {"reviewer_availability": min(1.0, tm["reviewer_availability"] * 1.25)})),
    ("review_throughput_+25pct", "Raise review throughput 25% (tooling, checklists, AI-assisted review)",
     lambda t, tm: ({}, {"lines_per_reviewer_hour": tm["lines_per_reviewer_hour"] * 1.25})),
    ("add_author", "Add one author (raises PR arrivals, so it also loads review)",
     lambda t, tm: ({}, {"authors": tm["authors"] + 1})),
    ("author_availability_0.9", "Protect authors' time: availability to 0.9",
     lambda t, tm: ({}, {"author_availability": max(tm["author_availability"], 0.9)})),
    ("halve_decision_latency", "Halve decision latency (named owners, decision SLA)",
     lambda t, tm: ({}, {"decision_latency_days": tm["decision_latency_days"] / 2})),
    ("halve_ci_time", "Halve CI and verification time per attempt",
     lambda t, tm: ({}, {"ci_hours": tm["ci_hours"] / 2})),
    ("halve_release_interval", "Release twice as often",
     lambda t, tm: ({}, {"release_interval_days": tm["release_interval_days"] / 2})),
    ("double_agent_pr_rate", "Agent halves author effort per PR (so PR arrivals double)",
     lambda t, tm: ({}, {"mean_author_hours_per_pr": tm["mean_author_hours_per_pr"] / 2,
                         "agent_speed": tm["agent_speed"] * 2})),
]


def run_levers(raw_ticket, team_overrides=None, n=20000, seed=0):
    base = estimate(raw_ticket, team_overrides, n, seed)
    t, _ = validate_ticket(raw_ticket)
    team = merge_team(team_overrides)
    base_p80 = base["lead_time_calendar_days"]["P80"] if base["stable"] else None
    rows = []
    for name, desc, fn in LEVERS:
        t_change, team_change = fn(t, team)
        r = estimate({**t, **t_change}, {**team, **team_change}, n, seed)
        row = {"lever": name, "description": desc}
        same_t = all(abs(float(v) - float(t[k])) < 1e-9 for k, v in t_change.items())
        same_team = all(team.get(k) is not None and abs(float(v) - float(team[k])) < 1e-9 for k, v in team_change.items())
        if (t_change or team_change) and same_t and same_team:
            row["no_op"] = "already met by the current ticket or team"
        if not r["stable"]:
            row.update(stable=False, note=r["message"])
        else:
            p80 = r["lead_time_calendar_days"]["P80"]
            row.update(stable=True, p80_calendar_days=p80)
            if base_p80 is not None:
                row["delta_p80_days"] = round(p80 - base_p80, 2)
                row["delta_p80_pct"] = round(100.0 * (p80 - base_p80) / base_p80, 1) if base_p80 else 0.0
        rows.append(row)
    rows.sort(key=lambda r: (not r["stable"], r.get("delta_p80_days", 0.0)))
    return {"baseline": base, "levers": rows,
            "note": ("Deltas are changes in P80 lead time, in calendar days (negative is better). Rank by delta, then "
                     "weigh each lever's real cost, which the model does not know.")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lead-time forecast for an AI-assisted ticket. JSON on stdout.")
    ap.add_argument("--ticket", help="ticket JSON file, or - for stdin")
    ap.add_argument("--team", help="team parameters JSON (team_setup.py / calibrate.py); omit to use priors")
    ap.add_argument("--n", type=int, default=20000, help="Monte Carlo samples (default 20000)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--levers", action="store_true", help="also rank what-if levers by P80 impact")
    ap.add_argument("--sensitivity", action="store_true", help="rank prior-sourced parameters by P80 swing")
    ap.add_argument("--require-complete", action="store_true",
                    help="refuse to forecast while a required team parameter is still at a prior")
    ap.add_argument("--write-default-team", metavar="PATH", help="write the default team file and exit")
    args = ap.parse_args(argv)

    if args.write_default_team:
        with open(args.write_default_team, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in DEFAULT_TEAM.items()}, f, indent=2)
        print(f"wrote {args.write_default_team}", file=sys.stderr)
        return 0
    if not args.ticket:
        ap.error("--ticket is required")
    try:
        raw = json.load(sys.stdin) if args.ticket == "-" else load_json(args.ticket)
        team = load_json(args.team) if args.team else None
        if args.require_complete:
            pend = required_at_prior(merge_team(team))
            if pend:
                print("error: required team parameters are still at a prior: " + ", ".join(pend) +
                      ". Run team_setup.py (ask the team) first.", file=sys.stderr)
                return 3
        out = (run_levers(raw, team, args.n, args.seed) if args.levers else estimate(raw, team, args.n, args.seed))
        if args.sensitivity:
            out = {"result": out, "prior_sensitivity": prior_sensitivity(raw, team, min(args.n, 6000), args.seed)}
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
