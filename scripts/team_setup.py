#!/usr/bin/env python3
"""
Build team.json with a person in the loop. The agent asks the questions; this script holds the rules.

Every parameter ends with a provenance:
  user      a person typed or chose it
  measured  mined from history (mine_history.py) and confirmed by a person, or accepted as an optional measurement
  prior     the estimator's default; nobody supplied a value

Required parameters (estimate.REQUIRED_TEAM) decide whether a forecast is trustworthy. Mined values are suggestions:
a required one counts as measured only when the person answers "mined" for it.

Subcommands
  questions  --mined mined.json [--existing team.json]
             Print the questions still to ask (required, not yet answered by a person), each with the mined suggestion,
             its confidence and evidence, and the prior. The agent asks these (AskUserQuestion, up to 4 per round).
  build      --answers answers.json [--mined mined.json] [--existing team.json] [--accept-mined-optional] --out team.json
             answers.json maps key -> number | "mined" | "prior". Writes team.json with provenance and prints the table.
  show       --team team.json
             Print every parameter with its value, source and evidence.

Usage:
  python3 team_setup.py questions --mined mined.json
  python3 team_setup.py build --answers answers.json --mined mined.json --accept-mined-optional --out .ticket-estimator/team.json
"""
import argparse
import copy
import datetime as dt
import json
import os
import sys

sys.dont_write_bytecode = True  # keep the skill folder free of __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import estimate as E  # noqa: E402

QUESTIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "team_questions.json")
SCALES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "size_scales.json")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def questions_def():
    return {q["key"]: q for q in load(QUESTIONS_PATH)["questions"]}


def size_presets():
    return load(SCALES_PATH)["presets"]


def resolve_size_scale(val):
    """A preset name, or {"default": {S, M, L, XL}, "by_type": {TYPE: {S, M, L, XL}}}. Returns the stored form."""
    if isinstance(val, str):
        presets = size_presets()
        if val not in presets:
            raise ValueError(f"unknown size scale preset {val!r}; choose from {sorted(presets)} or give a table")
        val = {"default": presets[val]["default"], "by_type": presets[val]["by_type"], "preset": val}
    if not isinstance(val, dict) or "default" not in val:
        raise ValueError("size_scale needs a \"default\" table with S, M, L and XL")
    for name, table in [("default", val["default"])] + list((val.get("by_type") or {}).items()):
        if not all(k in table and float(table[k]) > 0 for k in ("S", "M", "L", "XL")):
            raise ValueError(f"size_scale {name} needs positive S, M, L and XL hours")
        if not float(table["S"]) <= float(table["M"]) <= float(table["L"]) <= float(table["XL"]):
            raise ValueError(f"size_scale {name} must not shrink from S to XL")
    return {"default": {k: float(v) for k, v in val["default"].items()},
            "by_type": {t: {k: float(v) for k, v in tb.items()} for t, tb in (val.get("by_type") or {}).items()},
            **({"preset": val["preset"]} if "preset" in val else {})}


def coerce(key, val, qdef):
    kind = qdef[key]["type"] if key in qdef else "float"
    v = int(round(float(val))) if kind == "int" else float(val)
    rng = E.TEAM_RANGES.get(key)
    if rng and not rng[0] <= v <= rng[1]:
        raise ValueError(f"{key}={v} is outside the allowed range {rng}")
    return v


def cmd_questions(args):
    qdef = questions_def()
    mined = load(args.mined) if args.mined else {"values": {}, "confidence": {}, "evidence": {}}
    existing = load(args.existing) if args.existing and os.path.exists(args.existing) else {}
    prov = existing.get("provenance", {})
    pending = []
    for key, q in qdef.items():
        if not (q["required"] or (args.stories and q.get("stories"))):
            continue
        if prov.get(key, {}).get("source") == "user":
            continue
        item = {k: q[k] for k in ("key", "group", "ask", "why", "unit", "type", "suggest")}
        item["prior"] = E.DEFAULT_TEAM[key]
        if q["type"] == "preset":
            item["presets"] = {name: p["label"] for name, p in size_presets().items()}
        if key in mined["values"]:
            item.update(mined_value=mined["values"][key], confidence=mined["confidence"].get(key),
                        evidence=mined["evidence"].get(key))
        pending.append(item)
    optional = []
    for key, q in qdef.items():
        if q["required"] or q.get("stories") and args.stories or key not in mined["values"]:
            continue
        optional.append({"key": key, "unit": q["unit"], "mined_value": mined["values"][key],
                         "confidence": mined["confidence"].get(key), "evidence": mined["evidence"].get(key),
                         "prior": E.DEFAULT_TEAM[key]})
    json.dump({"pending_required": pending, "optional_mined": optional, "hints": mined.get("hints", {}),
               "notes": mined.get("notes", []),
               "how_to_answer": "answers.json: {key: number | \"mined\" | \"prior\"}. \"prior\" keeps the estimator's default and the forecast will say so."},
              sys.stdout, indent=2)
    print()
    return 0


def cmd_build(args):
    qdef = questions_def()
    answers = load(args.answers)
    mined = load(args.mined) if args.mined else {"values": {}, "confidence": {}, "evidence": {}}
    existing = load(args.existing) if args.existing and os.path.exists(args.existing) else {}
    today = dt.date.today().isoformat()
    team = E.merge_team({k: v for k, v in existing.items() if k in E.DEFAULT_TEAM and k != "provenance"})
    prov = dict(existing.get("provenance", {}))

    for key, val in answers.items():
        if key not in E.DEFAULT_TEAM or key in ("calibrated", "schema_version"):
            raise ValueError(f"unknown team parameter: {key}")
        if key == "size_scale":
            if val == "prior":
                team[key] = copy.deepcopy(E.DEFAULT_TEAM[key])
                prov[key] = {"source": "prior", "note": "a person looked at it and kept the default scale", "at": today}
            else:
                team[key] = resolve_size_scale(val)
                prov[key] = {"source": "user", "at": today}
            continue
        if val == "prior":
            team[key] = E.DEFAULT_TEAM[key]
            prov[key] = {"source": "prior", "note": "a person looked at it and kept the estimator's default", "at": today}
        elif val == "mined":
            if key not in mined["values"]:
                raise ValueError(f"{key}: no mined value to accept")
            team[key] = coerce(key, mined["values"][key], qdef)
            prov[key] = {"source": "measured", "confidence": mined["confidence"].get(key),
                         "evidence": mined["evidence"].get(key), "confirmed_by_person": True, "at": today}
        else:
            team[key] = coerce(key, val, qdef)
            prov[key] = {"source": "user", "at": today}
            if key in mined["values"] and abs(float(mined["values"][key]) - float(team[key])) > 1e-9:
                prov[key]["mined_value_overridden"] = mined["values"][key]

    if args.accept_mined_optional:
        for key, val in mined["values"].items():
            q = qdef.get(key)
            if q is None or q["required"] or key in answers or prov.get(key, {}).get("source") == "user":
                continue
            team[key] = coerce(key, val, qdef)
            prov[key] = {"source": "measured", "confidence": mined["confidence"].get(key),
                         "evidence": mined["evidence"].get(key), "accepted_as_optional": True, "at": today}

    team["provenance"] = prov
    team["user_set_params"] = sorted(k for k, v in prov.items() if v.get("source") == "user")
    team["required_at_prior"] = E.required_at_prior(team)
    team["complete"] = not team["required_at_prior"]
    team["calibrated"] = bool(existing.get("calibrated", False))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(team, f, indent=2)
    print_table(team, sys.stderr)
    print(f"wrote {args.out} (complete={team['complete']}; required still at a prior: "
          f"{', '.join(team['required_at_prior']) or 'none'})", file=sys.stderr)
    return 0


def print_table(team, out):
    team = E.merge_team(team) if "provenance" not in team else team
    prov = team.get("provenance", {})
    rows = []
    for key in list(E.REQUIRED_TEAM) + [k for k in E.DEFAULT_TEAM if k not in E.REQUIRED_TEAM
                                         and k not in ("schema_version", "calibrated") and not k.startswith(("b_", "r_"))
                                         and k not in ("b0", "r0", "prob_concentration", "max_agent_attempts")]:
        src = E.source_of(team, key)
        rows.append((key, team.get(key), src, "required" if key in E.REQUIRED_TEAM else "", (prov.get(key) or {}).get("evidence", "")))
    print("| parameter | value | source | | evidence |\n| --- | --- | --- | --- | --- |", file=out)
    for k, v, s, r, ev in rows:
        print(f"| {k} | {v} | {s} | {r} | {str(ev)[:110]} |", file=out)


def cmd_show(args):
    print_table(load(args.team), sys.stdout)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build team.json with a person in the loop.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("questions"); q.add_argument("--mined"); q.add_argument("--existing")
    q.add_argument("--stories", action="store_true", help="also ask the story-sizing questions (size scale, retries, PR size)")
    b = sub.add_parser("build")
    b.add_argument("--answers", required=True); b.add_argument("--mined"); b.add_argument("--existing")
    b.add_argument("--accept-mined-optional", action="store_true"); b.add_argument("--out", required=True)
    s = sub.add_parser("show"); s.add_argument("--team", required=True)
    args = ap.parse_args(argv)
    try:
        return {"questions": cmd_questions, "build": cmd_build, "show": cmd_show}[args.cmd](args)
    except (ValueError, OSError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
