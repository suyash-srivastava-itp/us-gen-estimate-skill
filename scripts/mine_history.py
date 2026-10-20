#!/usr/bin/env python3
"""
Mine measured team parameters from git and the GitHub CLI, so a person only has to answer what history cannot tell.

Writes a JSON file the team_setup.py step uses as pre-filled suggestions. Nothing here is a final answer: each value
carries a confidence level and the evidence behind it, and a person confirms or overrides it (human in the loop).

What it can measure
  authors                  distinct non-bot authors of merged PRs in the window        (medium: they may not all work on this backlog)
  author_hours_per_day     median focused hours per active day, from commit sessions   (medium: commits undercount reading time)
  mean_author_hours_per_pr focused hours of those authors / merged PRs                  (medium)
  pr_arrivals_per_day      PRs opened (and merged in the window) per working day        (high)
  mean_pr_lines            mean added+deleted lines, top 5% winsorised                  (high)
  typical_pr_lines         median added+deleted lines of a merged PR                    (high)
  reviewers                distinct non-author reviewers; 0 when under 10% of PRs have a non-author review (medium)
  ci_hours                 median wall-clock duration of completed CI runs              (medium)
  release_interval_days    median gap between the last tags, as working days            (low: tags may not be deploys)

What it cannot measure (left for a person): author_availability, reviewer_hours_per_day, reviewer_availability,
days_per_week, agent_attended_fraction, decision_latency_days, lines_per_reviewer_hour, parallel_sessions.
It does print hints for some (presence of each author on working days, weekday spread of commits).

Usage:
  python3 mine_history.py --repo . [--since-days 60] [--base main] --out mined.json
"""
import argparse
import datetime as dt
import json
import statistics
import subprocess
import sys

sys.dont_write_bytecode = True  # keep the skill folder free of __pycache__
GAP_MIN = 90          # commits closer than this belong to one working session
LEAD_IN_H = 0.5       # time spent before the first commit of a session


def run(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}: {p.stderr.strip()[:200]}")
    return p.stdout


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def working_days(start, end):
    d, n = start.date(), 0
    while d <= end.date():
        n += d.weekday() < 5
        d += dt.timedelta(days=1)
    return max(n, 1)


def sessions(stamps):
    """Group sorted datetimes into sessions; return [(start, end)]."""
    out = []
    for t in sorted(stamps):
        if out and (t - out[-1][1]).total_seconds() <= GAP_MIN * 60:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


def pct(vals, q):
    vals = sorted(vals)
    if not vals:
        return None
    k = (len(vals) - 1) * q
    f, c = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Mine measured team parameters from git and gh.")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--since-days", type=int, default=60)
    ap.add_argument("--base", help="only PRs into this base branch (default: all)")
    ap.add_argument("--max-authors", type=int, default=8, help="authors counted for focused hours (most active first)")
    ap.add_argument("--people", help="comma-separated GitHub logins of the people on this work; restricts the PR mining")
    ap.add_argument("--git-names", help="comma-separated git author names of the same people; restricts the commit mining")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    people = {x.strip().lower() for x in (args.people or "").split(",") if x.strip()}
    git_names = {x.strip().lower() for x in (args.git_names or "").split(",") if x.strip()}
    PR_LIMIT = 1000

    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=args.since_days)
    values, conf, evid, hints, gaps, notes = {}, {}, {}, {}, [], []

    # ------------------------------------------------------------------ pull requests
    prs = []
    try:
        cmd = ["gh", "pr", "list", "--state", "merged", "--limit", str(PR_LIMIT), "--search", f"merged:>={start.date()}",
               "--json", "number,author,createdAt,mergedAt,additions,deletions,reviews,baseRefName"]
        prs = json.loads(run(cmd, args.repo))
        if len(prs) >= PR_LIMIT:
            oldest = min(parse_ts(p["mergedAt"]) for p in prs if p.get("mergedAt"))
            notes.append(f"The {PR_LIMIT}-PR fetch limit was reached, so the PR window starts at {oldest.date()} "
                         f"instead of {start.date()}. Rates use the shorter window.")
            start_pr = oldest
        else:
            start_pr = start
        if args.base:
            prs = [p for p in prs if p.get("baseRefName") == args.base]
    except (RuntimeError, OSError, json.JSONDecodeError) as e:
        start_pr = start
        notes.append(f"Pull-request history unavailable ({e}). Author, review and PR-rate parameters are not mined.")
    wdays = working_days(start_pr, now)
    humans = [p for p in prs if not (p.get("author") or {}).get("is_bot")
              and (not people or (p.get("author") or {}).get("login", "").lower() in people)]
    if len(humans) >= 5:
        by_author = {}
        for p in humans:
            by_author[p["author"]["login"]] = by_author.get(p["author"]["login"], 0) + 1
        values["authors"] = len(by_author)
        conf["authors"] = "medium"
        evid["authors"] = (f"{len(by_author)} distinct non-bot authors of {len(humans)} merged PRs in the last "
                           f"{args.since_days} days: " + ", ".join(f"{k} ({v})" for k, v in sorted(by_author.items(), key=lambda kv: -kv[1])[:8]))
        values["pr_arrivals_per_day"] = round(len(humans) / wdays, 2)
        conf["pr_arrivals_per_day"] = "high"
        evid["pr_arrivals_per_day"] = f"{len(humans)} merged PRs over {wdays} working days (Mon-Fri) in the window"
        lines = [p["additions"] + p["deletions"] for p in humans]
        cap = pct(lines, 0.95)
        wins = [min(x, cap) for x in lines]
        values["mean_pr_lines"] = round(statistics.mean(wins), 0)
        conf["mean_pr_lines"] = "high"
        evid["mean_pr_lines"] = (f"mean of added+deleted lines with the top 5% capped at {cap:.0f}; raw mean "
                                 f"{statistics.mean(lines):.0f}, median {statistics.median(lines):.0f}, n={len(lines)}")
        values["typical_pr_lines"] = round(statistics.median(lines), 0)
        conf["typical_pr_lines"] = "high"
        evid["typical_pr_lines"] = f"median added+deleted lines of {len(lines)} merged PRs in the window"
        # reviews by someone other than the author
        reviewed, first_review_h, reviewers = 0, [], set()
        for p in humans:
            author = p["author"]["login"]
            rv = [r for r in p.get("reviews", []) if (r.get("author") or {}).get("login") not in (None, author)
                  and not (r.get("author") or {}).get("is_bot")]
            if rv:
                reviewed += 1
                reviewers.update(r["author"]["login"] for r in rv)
                ts = [parse_ts(r["submittedAt"]) for r in rv if r.get("submittedAt")]
                if ts:
                    first_review_h.append((min(ts) - parse_ts(p["createdAt"])).total_seconds() / 3600)
        share = reviewed / len(humans)
        values["reviewers"] = len(reviewers) if share >= 0.10 else 0
        conf["reviewers"] = "medium"
        evid["reviewers"] = (f"{reviewed} of {len(humans)} merged PRs ({share:.0%}) had a review by someone other than "
                             f"the author; {len(reviewers)} distinct reviewers"
                             + (f"; median {statistics.median(first_review_h):.1f} h from open to first review" if first_review_h else "")
                             + (". Under 10% reviewed, so reviewers is suggested as 0 (PRs merge without independent review)."
                                if share < 0.10 else "."))
        merge_h = [(parse_ts(p["mergedAt"]) - parse_ts(p["createdAt"])).total_seconds() / 3600 for p in humans if p.get("mergedAt")]
        hints["open_to_merge_hours"] = {"median": round(statistics.median(merge_h), 2), "p80": round(pct(merge_h, 0.8), 2), "n": len(merge_h)}
    elif prs or not notes:
        notes.append(f"Only {len(humans)} merged PRs in the window; too few to mine PR parameters.")

    # ------------------------------------------------------------------ commit sessions (focused hours)
    try:
        raw = run(["git", "log", "--all", f"--since={args.since_days}.days.ago", "--no-merges", "--format=%aI|%an"], args.repo)
        commits = {}
        for line in raw.splitlines():
            if "|" not in line:
                continue
            ts, name = line.split("|", 1)
            if git_names and name.lower() not in git_names:
                continue
            commits.setdefault(name, []).append(dt.datetime.fromisoformat(ts))
        top = sorted(commits.items(), key=lambda kv: -len(kv[1]))[:args.max_authors]
        per_author, total_h = {}, 0.0
        for name, stamps in top:
            days = {}
            for s, e in sessions(stamps):
                hours = (e - s).total_seconds() / 3600 + LEAD_IN_H
                days[s.date()] = days.get(s.date(), 0.0) + hours
            wk = [d for d in days if d.weekday() < 5]
            per_author[name] = {"active_days": len(days), "median_hours_per_active_day": round(statistics.median(days.values()), 2),
                                "presence_on_working_days": round(len(wk) / working_days(start, now), 2), "commits": len(stamps)}
            total_h += sum(days.values())
        if per_author:
            med = statistics.median(v["median_hours_per_active_day"] for v in per_author.values())
            values["author_hours_per_day"] = round(med, 1)
            conf["author_hours_per_day"] = "medium"
            evid["author_hours_per_day"] = ("median over the " + str(len(per_author)) + " most active committers of focused hours "
                                            "on days they committed (session = commits < 90 min apart, +0.5 h lead-in): "
                                            + "; ".join(f"{k} {v['median_hours_per_active_day']} h over {v['active_days']} days" for k, v in per_author.items()))
            hints["presence_on_working_days"] = {k: v["presence_on_working_days"] for k, v in per_author.items()}
            n_pr = len(humans)
            if n_pr >= 5:
                values["mean_author_hours_per_pr"] = round(total_h / n_pr, 2)
                conf["mean_author_hours_per_pr"] = "medium"
                evid["mean_author_hours_per_pr"] = (f"{total_h:.1f} focused hours of the top committers / {n_pr} merged PRs "
                                                    "(includes work on branches not yet merged)")
        wd_counts = [0] * 7
        for stamps in commits.values():
            for s in stamps:
                wd_counts[s.weekday()] += 1
        tot = sum(wd_counts) or 1
        hints["commit_share_by_weekday_mon_to_sun"] = [round(x / tot, 2) for x in wd_counts]
    except (RuntimeError, OSError) as e:
        notes.append(f"git log unavailable ({e}). Focused hours are not mined.")

    # ------------------------------------------------------------------ CI duration
    try:
        runs = json.loads(run(["gh", "run", "list", "--limit", "300", "--json",
                               "startedAt,updatedAt,status,conclusion,event,name"], args.repo))
        done = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") in ("success", "failure")
                and r.get("startedAt") and r.get("updatedAt")]
        pr_runs = [r for r in done if r.get("event") == "pull_request"] or done
        durs = [(parse_ts(r["updatedAt"]) - parse_ts(r["startedAt"])).total_seconds() / 3600 for r in pr_runs]
        if len(durs) >= 10:
            values["ci_hours"] = round(statistics.median(durs), 2)
            conf["ci_hours"] = "medium"
            evid["ci_hours"] = (f"median wall-clock of {len(durs)} completed CI runs"
                                f"{' (pull_request event)' if pr_runs is not done else ''}; p80 {pct(durs, 0.8):.2f} h")
    except (RuntimeError, OSError, json.JSONDecodeError) as e:
        notes.append(f"CI run history unavailable ({e}). ci_hours is not mined.")

    # ------------------------------------------------------------------ release interval from tags
    try:
        tags = run(["git", "for-each-ref", "refs/tags", "--sort=-creatordate", "--count=12",
                    "--format=%(creatordate:iso-strict)|%(refname:short)"], args.repo).splitlines()
        stamps = sorted(parse_ts(x.split("|")[0]) for x in tags if "|" in x)
        gaps_d = [(b - a).total_seconds() / 86400 for a, b in zip(stamps, stamps[1:])]
        if len(gaps_d) >= 3:
            med = statistics.median(gaps_d)
            values["release_interval_days"] = round(med * 5 / 7, 2)
            conf["release_interval_days"] = "low"
            evid["release_interval_days"] = (f"median {med:.1f} calendar days between the last {len(stamps)} tags "
                                             f"(newest {tags[0].split('|')[1]}); tags may be releases rather than deploys")
    except (RuntimeError, OSError) as e:
        notes.append(f"Tags unavailable ({e}).")

    required = ["authors", "author_hours_per_day", "author_availability", "reviewers", "reviewer_hours_per_day",
                "reviewer_availability", "days_per_week", "agent_attended_fraction", "release_interval_days",
                "decision_latency_days"]
    gaps = [k for k in required if k not in values]
    out = {"window": {"since_days": args.since_days, "from": start.date().isoformat(), "to": now.date().isoformat(),
                      "working_days": wdays, "merged_prs": len(humans)},
           "values": values, "confidence": conf, "evidence": evid, "hints": hints, "gaps": gaps, "notes": notes}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"mined {len(values)} parameters, {len(gaps)} required still need a person: {', '.join(gaps)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
