#!/usr/bin/env python3
"""
Fit team parameters from completed tickets, then backtest the estimator.

Input: a CSV with one row per completed ticket.

  Required columns:
    spec_clarity, open_questions, scope_hours, diff_lines,
    test_coverage, complexity, tacit   (same meaning as in estimate.py)
    first_pass    1 if the agent's first attempt passed CI and merged without substantial rework, else 0

  Optional columns (each enables one parameter, or the backtest):
    rework            1 if review required substantial rework, else 0 (enables rework parameters)
    decision_wait_h   wall-clock hours the ticket was blocked waiting on decisions (fits decision_latency_days)
    review_effort_h   reviewer hours spent on this ticket's PR(s)
    lead_time_h       elapsed WALL-CLOCK hours from ready to deployed (enables the backtests; the estimator's
                      lead_time_hours is the same unit, so nights and weekends count)

Usage:
  python3 calibrate.py history.csv --out .ticket-estimator/team.json \
      [--set reviewers=3 --set pr_arrivals_per_h=0.9 --set release_interval_h=24]

--set overrides any team parameter that cannot be read from the CSV. --base team.json (from team_setup.py) keeps the
people, calendar and provenance a person supplied and fits only the rest. Two backtests are reported: in-sample (flatters
the model) and k-fold out-of-sample (the one to trust).
"""
import argparse
import csv
import json
import sys

sys.dont_write_bytecode = True  # keep the skill folder free of __pycache__
import numpy as np

import datetime as dt

from estimate import DEFAULT_TEAM, INTEGER_PARAMS, estimate, merge_team, sigmoid, source_of

TICKET_FIELDS = ["spec_clarity", "open_questions", "scope_hours", "diff_lines",
                 "test_coverage", "complexity", "tacit"]
REQUIRED_COLUMNS = TICKET_FIELDS + ["first_pass"]


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError("history file is empty")
    missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing:
        raise ValueError(f"history file is missing columns: {missing}")
    return rows


def has_all(rows, name):
    return all(str(r.get(name, "")).strip() != "" for r in rows)


def col(rows, name):
    return np.array([float(r[name]) for r in rows], dtype=float)


def fit_logistic(X, y, ridge=1e-3, iters=100):
    """Newton-Raphson logistic regression with a small ridge penalty. Returns [intercept, coefs...]."""
    X = np.column_stack([np.ones(len(X)), X])
    beta = np.zeros(X.shape[1])
    for _ in range(iters):
        p = sigmoid(X @ beta)
        grad = X.T @ (y - p) - ridge * beta
        hess = (X * (p * (1.0 - p))[:, None]).T @ X + ridge * np.eye(len(beta))
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if np.max(np.abs(step)) < 1e-8:
            break
    return beta


def calibrate(rows, overrides, base=None):
    if len(rows) < 10:
        raise ValueError(f"need at least 10 tickets, got {len(rows)}; 30 or more gives usable estimates")

    c = col(rows, "spec_clarity")
    oq = col(rows, "open_questions")
    diff = col(rows, "diff_lines")
    tests = col(rows, "test_coverage")
    cx = col(rows, "complexity")
    tacit = col(rows, "tacit")
    y_fp = col(rows, "first_pass")

    team = merge_team(base)          # start from the HITL team file (people, calendar, provenance) when given
    prov = dict(team.get("provenance") or {})
    today = dt.date.today().isoformat()
    fitted, warnings = [], []

    # First-attempt success model. Columns: [intercept, clarity, tests, complexity, tacit].
    beta = fit_logistic(np.column_stack([c, tests, cx, tacit]), y_fp)
    team.update(b0=float(beta[0]), b_clarity=float(beta[1]), b_tests=float(beta[2]),
                b_complex=float(-beta[3]), b_tacit=float(-beta[4]))
    fitted += ["b0", "b_clarity", "b_tests", "b_complex", "b_tacit"]

    # Rework model. Columns: [intercept, log-size, tests, clarity].
    if has_all(rows, "rework"):
        beta_r = fit_logistic(np.column_stack([np.log1p(diff / 100.0), tests, c]), col(rows, "rework"))
        team.update(r0=float(beta_r[0]), r_size=float(beta_r[1]),
                    r_tests=float(-beta_r[2]), r_clarity=float(-beta_r[3]))
        fitted += ["r0", "r_size", "r_tests", "r_clarity"]

    # Decision latency: total blocked hours divided by expected blocking events.
    if has_all(rows, "decision_wait_h"):
        waited = col(rows, "decision_wait_h")
        expected = oq * (1.0 - c)
        if expected.sum() > 0:
            # decision_wait_h is wall-clock hours blocked; the model's unit is the team's working day
            team["decision_latency_days"] = float(waited.sum() / expected.sum()) / 24.0 * team["days_per_week"] / 7.0
            fitted.append("decision_latency_days")
    else:
        warnings.append("No decision_wait_h column: decision_latency_days kept at its current value (prior unless a person set it).")

    # Review throughput: lines reviewed per reviewer-hour.
    if has_all(rows, "review_effort_h"):
        eff = col(rows, "review_effort_h")
        if eff.sum() > 0:
            team["lines_per_reviewer_hour"] = float(diff.sum() / eff.sum())
            fitted.append("lines_per_reviewer_hour")
    else:
        warnings.append("No review_effort_h column: lines_per_reviewer_hour kept at prior.")

    if not has_all(rows, "rework"):
        warnings.append("No rework column: rework parameters kept at priors.")

    for k in fitted:
        prov[k] = {"source": "measured", "evidence": f"fitted on {len(rows)} completed tickets", "at": today}
    for k, v in overrides.items():
        team[k] = int(v) if k in INTEGER_PARAMS else float(v)
        prov[k] = {"source": "user", "at": today}

    team["provenance"] = prov
    team["calibrated"] = True
    team["fitted_params"] = fitted
    team["user_set_params"] = sorted(k for k, v in prov.items() if v.get("source") == "user")
    team["priors_kept"] = sorted(k for k in DEFAULT_TEAM
                                 if k not in ("calibrated", "schema_version") and source_of(team, k) == "prior")

    # How well the first-attempt model explains the history.
    logit = (team["b0"] + team["b_clarity"] * c + team["b_tests"] * tests
             - team["b_complex"] * cx - team["b_tacit"] * tacit)
    p_pred = sigmoid(logit)
    report = {
        "tickets": len(rows),
        "first_pass_base_rate": round(float(y_fp.mean()), 3),
        "first_pass_fit": {
            "accuracy": round(float(np.mean((p_pred > 0.5) == (y_fp > 0.5))), 3),
            "brier": round(float(np.mean((p_pred - y_fp) ** 2)), 4),
        },
        "fitted_params": fitted,
        "user_set_params": sorted(overrides),
        "priors_kept": team["priors_kept"],
        "warnings": warnings,
    }
    return team, report


def backtest(rows, team):
    """Share of actual lead times falling below the predicted P50 and P80."""
    if not has_all(rows, "lead_time_h"):
        return {"note": "No lead_time_h column: backtest skipped."}
    actual = col(rows, "lead_time_h")
    used = below50 = below80 = 0
    for r, a in zip(rows, actual):
        ticket = {k: float(r[k]) for k in TICKET_FIELDS}
        res = estimate(ticket, team, n=2000, seed=1)
        if not res["stable"]:
            continue
        used += 1
        below50 += a <= res["lead_time_hours"]["P50"]
        below80 += a <= res["lead_time_hours"]["P80"]
    if used == 0:
        return {"note": "No stable tickets to backtest; review queue is unstable under these settings."}
    s50, s80 = below50 / used, below80 / used
    if s80 < 0.7:
        verdict = "Estimates are optimistic. Widen the range or add contingency."
    elif s80 > 0.9:
        verdict = "Estimates are conservative. The P80 is looser than needed."
    else:
        verdict = "Calibration looks reasonable."
    return {
        "tickets": used,
        "share_below_P50": round(s50, 3), "target_P50": 0.5,
        "share_below_P80": round(s80, 3), "target_P80": 0.8,
        "verdict": verdict,
        "note": "Backtest uses team-level parameters, so it tests ticket-level calibration only.",
    }


def crossval(rows, overrides, base=None, k=5, seed=0):
    """k-fold, out-of-sample: fit on k-1 folds, score the held-out fold, pool the counts. The in-sample backtest
    flatters the model because it scores the same tickets it was fitted on."""
    if not has_all(rows, "lead_time_h"):
        return {"note": "No lead_time_h column: cross-validation skipped."}
    n = len(rows)
    if n < 20:
        return {"note": f"{n} tickets is too few for cross-validation (need 20 or more); only the in-sample check was run."}
    order = np.random.default_rng(seed).permutation(n)
    folds = np.array_split(order, min(k, n // 10 if n >= 20 else 2))
    used = b50 = b80 = 0
    for f in folds:
        test = [rows[i] for i in f]
        train = [rows[i] for i in order if i not in set(f.tolist())]
        if len(train) < 10:
            continue
        team, _ = calibrate(train, overrides, base)
        actual = col(test, "lead_time_h")
        for r, a in zip(test, actual):
            res = estimate({kk: float(r[kk]) for kk in TICKET_FIELDS}, team, n=2000, seed=1)
            if not res["stable"]:
                continue
            used += 1
            b50 += a <= res["lead_time_hours"]["P50"]
            b80 += a <= res["lead_time_hours"]["P80"]
    if used == 0:
        return {"note": "No stable held-out tickets; review queue is unstable under these settings."}
    s50, s80 = b50 / used, b80 / used
    return {"tickets_scored_out_of_sample": used, "folds": len(folds),
            "share_below_P50": round(s50, 3), "target_P50": 0.5, "share_below_P80": round(s80, 3), "target_P80": 0.8,
            "verdict": ("Estimates are optimistic out of sample. Widen the range or add contingency." if s80 < 0.7 else
                        "Estimates are conservative out of sample. The P80 is looser than needed." if s80 > 0.9 else
                        "Calibration holds out of sample.")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fit team parameters from ticket history and backtest.")
    ap.add_argument("history", help="CSV of completed tickets")
    ap.add_argument("--out", required=True, help="where to write team.json")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a team parameter (repeatable)")
    ap.add_argument("--base", help="team.json from team_setup.py: keep its people, calendar and provenance, fit the rest")
    args = ap.parse_args(argv)
    try:
        overrides = {}
        for item in args.set:
            k, _, v = item.partition("=")
            if k not in DEFAULT_TEAM or k == "calibrated":
                raise ValueError(f"unknown team parameter: {k}")
            overrides[k] = int(v) if k in INTEGER_PARAMS else float(v)
        base = json.load(open(args.base, encoding="utf-8")) if args.base else None
        rows = load_rows(args.history)
        team, report = calibrate(rows, overrides, base)
        report["backtest_in_sample"] = backtest(rows, team)
        report["backtest_out_of_sample"] = crossval(rows, overrides, base)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(team, f, indent=2)
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
