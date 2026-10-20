#!/usr/bin/env python3
"""
Turn a backlog markdown written in this skill's format (As a / I want / So that, Gherkin scenarios, tasks) back into a story
spec, so an existing backlog can be updated: import, edit the spec, rebuild with us_build.py.

Prefer the <name>.spec.json that us_build.py writes next to the markdown; it keeps what the markdown cannot (the paths a
story names, the exact evidence quotes, ticket overrides). Importing the markdown recovers the epic, features, stories, user
story lines, size, type, horizon, dependencies, context, open questions, facts (claim and source only), Gherkin scenarios,
tasks (and their technical background when the markdown has it) and the shared templates. Hours, lead times and every other
estimate are outputs and are recomputed, never imported.

Usage:
  python3 us_import.py --md backlog.md --out backlog.spec.json [--name NAME] [--tasks-policy all|large|none]
"""
import argparse
import json
import os
import re
import sys

sys.dont_write_bytecode = True  # keep the skill folder free of __pycache__
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import us_spec as U  # noqa: E402


def parse_gherkin(block):
    scenarios, cur, mode = [], None, None
    examples = False
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("Feature:"):
            continue
        m = re.match(r"^(Scenario Outline|Scenario):\s*(.*)$", line)
        if m:
            cur = {"title": m.group(2).strip(), "given": [], "when": [], "then": []}
            scenarios.append(cur)
            mode, examples = None, False
            continue
        if cur is None:
            continue
        if line.startswith("Examples:"):
            examples = True
            cur["examples"] = {"headers": [], "rows": []}
            continue
        if examples and line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not cur["examples"]["headers"]:
                cur["examples"]["headers"] = cells
            else:
                cur["examples"]["rows"].append(cells)
            continue
        m = re.match(r"^(Given|When|Then|And|But)\s+(.*)$", line)
        if m:
            kw = m.group(1).lower()
            if kw in ("and", "but"):
                kw = mode or "then"
            else:
                mode = kw
            cur[kw].append(m.group(2).strip())
    return scenarios


def split_blocks(md, pattern):
    """Split md into (heading_line, body) pairs at lines matching pattern (a regex with one group for the heading)."""
    pieces, cur = [], None
    for line in md.splitlines():
        if re.match(pattern, line):
            cur = [line, []]
            pieces.append(cur)
        elif re.match(r"^#{1,4}\s", line) and cur is not None and not re.match(r"^#{4}\s", line) and pattern.startswith(r"^#### "):
            cur = None
        elif cur is not None:
            cur[1].append(line)
    return [(h, "\n".join(b)) for h, b in pieces]


def field(body, label):
    m = re.search(rf"\*\*{re.escape(label)}\.?\*\*\s*(.+?)(?:\n\n|\Z)", body, flags=re.S)
    return m.group(1).strip() if m else ""


def bullets_after(body, label):
    m = re.search(rf"\*\*{re.escape(label)}\*\*\s*\n\n((?:- .*\n?)+)", body)
    return [re.sub(r"^- (\[ \] )?", "", x).strip() for x in m.group(1).strip().splitlines()] if m else []


def parse_task_list(body):
    m = re.search(r"\*\*Tasks\*\*\s*\n\n(.*?)(?:\n\n####|\n\n###|\n\n## |\Z)", body, flags=re.S)
    if not m:
        return []
    tasks, cur = [], None
    for line in m.group(1).splitlines():
        top = re.match(r"^\d+\.\s+(.*)$", line)
        if top:
            title = top.group(1).strip()
            title = re.sub(r"^\*\*(.+?)\*\*\s*(\(.*\))?\s*$", r"\1", title)
            cur = {"title": title, "weight": 1, "technical_background": {}}
            tasks.append(cur)
            continue
        sub = re.match(r"^\s+- \*([^*]+?)\.?\*\s*(.*)$", line)
        if sub and cur is not None:
            key = {"current state": "current_state", "approach": "approach", "test plan": "test_plan", "risks and rollback": "risks", "touches": "touches", "interfaces": "interfaces", "evidence": "evidence"}.get(sub.group(1).strip().lower())
            val = sub.group(2).strip()
            tb = cur["technical_background"]
            if key in ("touches", "interfaces"):
                tb[key] = [x.strip().strip("`") for x in val.split(",") if x.strip()]
            elif key == "evidence":
                m2 = re.match(r"^(.*?)\s*\(`?([^`)]*)`?\)\s*$", val)
                claim, src = (m2.group(1), m2.group(2)) if m2 else (val, "")
                ev = {"claim": claim, "source": src}
                p = re.match(r"^([^:\s]+?)(?::(\d+))?$", src)
                if p and ("/" in src or "." in src):
                    ev["path"] = p.group(1)
                    if p.group(2):
                        ev["line"] = int(p.group(2))
                tb.setdefault("evidence", []).append(ev)
            elif key:
                tb[key] = val
    return tasks


def import_md(md, name, tasks_policy):
    spec = {"schema": 1, "name": name, "epic": {}, "scope": {"tasks_policy": tasks_policy}, "templates": {}, "features": [], "stories": []}
    m = re.search(r"^###\s+E\d+\.\s+(.*)$", md, flags=re.M) or re.search(r"^#\s+(.*)$", md, flags=re.M)
    spec["epic"] = {"id": "E1", "title": m.group(1).strip() if m else name}
    head = md.split("\n## ")[1] if "\n## " in md else ""
    g = re.search(r"\*\*Goal\.\*\*\s*(.+?)(?:\n\n|\Z)", md, flags=re.S)
    if g:
        spec["epic"]["goal"] = g.group(1).strip()
    ex = re.search(r"\*\*Epic exit criteria\*\*[^\n]*\n\n((?:\d+\. .*\n?)+)", md)
    if ex:
        spec["epic"]["exit_criteria"] = [re.sub(r"^\d+\.\s+", "", x).strip() for x in ex.group(1).strip().splitlines()]

    for h, body in split_blocks(md, r"^### F\d+\."):
        mm = re.match(r"^### (F\d+)\.\s+(.*)$", h)
        spec["features"].append({"id": mm.group(1), "title": mm.group(2).strip(), "goal": field(body, "Goal"), "why": field(body, "Why"), "exit_criteria": bullets_after(body, "Feature exit criteria")})

    for h, body in split_blocks(md, r"^#### S\d+\.\d+\."):
        mm = re.match(r"^#### (S\d+\.\d+)\.\s+(.*)$", h)
        sid, title = mm.group(1), mm.group(2).strip()
        fid = "F" + sid[1:].split(".")[0]
        us = re.search(r">\s*\*\*As\*\*\s*(.+?),\s*\*\*I want\*\*\s*(.+?),\s*\*\*so that\*\*\s*(.+?)\.\s*$", body, flags=re.M)
        story = {"id": sid, "feature": fid, "title": title, "type": "", "horizon": "", "role": us.group(1).strip() if us else "", "want": us.group(2).strip() if us else "", "so": us.group(3).strip() if us else ""}
        t = re.search(r"\*\*Type\*\*\s*([A-Za-z_]+)", body)
        story["type"] = t.group(1) if t else "FEATURE"
        hz = re.search(r"\*\*Horizon\*\*\s*(H\d|-)", body)
        story["horizon"] = hz.group(1) if hz and hz.group(1) != "-" else ""
        dp = re.search(r"\*\*Depends on:\*\*\s*([^·\n]+)", body)
        story["deps"] = [] if not dp or dp.group(1).strip() in ("none", "") else [x.strip() for x in dp.group(1).split(",") if x.strip()]
        ap = re.search(r"\*\*Also applies:\*\*\s*([^\n(]+)", body)
        story["templates"] = [] if not ap else [x.strip() for x in re.sub(r"\btemplates?\b", "", ap.group(1)).replace(" and ", ",").split(",") if x.strip()]
        sz = re.search(r"\bSize\s+(S|M|L|XL)\b", body) or re.search(r"\|\s*Size / focused hours\s*\|\s*(S|M|L|XL)\b", body)
        if sz:
            story["size"] = sz.group(1)
        else:
            fh = re.search(r"Size / focused hours\s*\|\s*-\s*·\s*([\d.]+)\s*h", body)
            if fh:
                story["focused_hours"] = float(fh.group(1))
        ctx = field(body, "Context")
        if ctx:
            story["context"] = ctx
        oq = bullets_after(body, "Open questions")
        story["open_questions"] = [{"question": re.sub(r"\s*\(owner: .*\)$", "", q), "owner": (re.search(r"\(owner: (.*)\)$", q) or [None, ""])[1]} for q in oq]
        facts = []
        for f in bullets_after(body, "Facts and sources"):
            f2 = re.match(r"^(.*?)\s*\(([^;)]*?)(?:;[^)]*)?\)\s*$", f)
            claim, src = (f2.group(1), f2.group(2)) if f2 else (f, "")
            fact = {"claim": claim, "source": src}
            p = re.match(r"^([^:\s]+?)(?::(\d+))?$", src)
            if p and re.search(r"[/.]", src) and " " not in src:
                fact["path"] = p.group(1)
                if p.group(2):
                    fact["line"] = int(p.group(2))
            facts.append(fact)
        story["facts"] = facts
        gb = re.search(r"```gherkin\n(.*?)```", body, flags=re.S)
        story["scenarios"] = parse_gherkin(gb.group(1)) if gb else []
        story["tasks"] = parse_task_list(body)
        spec["stories"].append(story)

    for blk in re.finditer(r"(?:\*\*([^*\n]+)\*\*\s*\n\n)?```gherkin\n(Feature: (\w+) template.*?)```", md, flags=re.S):
        key = blk.group(3)
        spec["templates"][key] = {"title": (blk.group(1) or key).strip(), "scenarios": parse_gherkin(blk.group(2))}
    U.normalize_spec(spec)
    return spec


def main(argv=None):
    ap = argparse.ArgumentParser(description="Import a backlog markdown into a story spec.")
    ap.add_argument("--md", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--name")
    ap.add_argument("--tasks-policy", choices=["all", "large", "none"], default="none")
    args = ap.parse_args(argv)
    md = open(args.md, encoding="utf-8").read()
    name = args.name or os.path.splitext(os.path.basename(args.md))[0].lower().replace("_", "-")
    spec = import_md(md, name, args.tasks_policy)
    U.save_spec(spec, args.out)
    n_sc = sum(len(s["scenarios"]) for s in spec["stories"])
    n_t = sum(len(s["tasks"]) for s in spec["stories"])
    no_bg = sum(1 for s in spec["stories"] for t in s["tasks"] if not t["technical_background"].get("approach"))
    print(json.dumps({"features": len(spec["features"]), "stories": len(spec["stories"]), "scenarios": n_sc, "tasks": n_t, "tasks_without_technical_background": no_bg,
                      "templates": list(spec["templates"]), "note": "paths, evidence quotes and ticket overrides are not in the markdown; add them to the spec before rebuilding"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
