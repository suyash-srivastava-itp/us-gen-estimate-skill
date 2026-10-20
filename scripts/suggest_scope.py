#!/usr/bin/env python3
"""
Suggest how many features and stories to write, as a starting point for the question put to the person.

The person always decides. This script only counts what is in the material and applies a stated rule, so the
suggestion can be defended and the evidence shown:

  source text (--source, --text)   candidate features = the section headings that are not boilerplate (level 2 when two
                                   or more exist, else level 1 below the title); candidate stories = requirement-like
                                   lines (bullets and sentences with must, shall, should, need, support, allow, add,
                                   migrate and similar verbs), de-duplicated.
  existing backlog (--spec, --md)  the current numbers of features and stories (update mode).
  nothing                          a generic starting point: 4 features, 24 stories (one epic, 4 to 6 features,
                                   5 to 8 stories each).

Rule: features = the candidate headings clamped to 2..8, or items / 8 clamped to 2..6 when there are no headings;
stories = the candidate items clamped to 3..12 per feature. The lean option is about 60% of the suggested story count
(fewer, larger stories), the granular one about 150% (more, smaller stories), each capped at 15 per feature.

Usage:
  python3 suggest_scope.py [--source FILE ...] [--text "free text brief"] [--spec backlog.spec.json] [--md backlog.md]
"""
import argparse
import json
import os
import re
import sys

sys.dont_write_bytecode = True  # keep the skill folder free of __pycache__
BOILERPLATE = re.compile(r"^(summary|overview|background|introduction|goals?|non-goals?|risks?|assumptions?|open questions?|appendix.*|references?|glossary|timeline|"
                         r"table of contents|status|scope|out of scope|context|why|purpose|notes?|changelog|history|contents|decisions?|dependencies|rollout|references and links)\b", re.I)
REQ_VERBS = re.compile(r"\b(must|shall|should|can|could|may|need(s)? to|want(s)? to|will|support(s)?|allow(s)?|enable(s)?|provide(s)?|let(s)?|ability to|able to|add|create|build|migrate|move|replace|"
                       r"remove|retire|define|expose|validate|generate|import|export|show|display|notify|send|store|track|log|audit|secure|encrypt|schedule|search|filter|sort|"
                       r"download|upload|view|see|edit|manage|configure|receive|reset|sign in|log in|pay|book|approve|review|publish|delete|update|assign)\b", re.I)
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")


def candidates(text):
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    heads = {1: [], 2: [], 3: []}
    items, seen = [], set()
    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*\S)\s*$", line)
        if m:
            heads[len(m.group(1))].append(m.group(2).strip())
            continue
        body = None
        b = BULLET.match(line)
        if b:
            body = b.group(1)
        elif len(line.split()) >= 6 and REQ_VERBS.search(line) and not line.lstrip().startswith(("|", ">", "<")):
            body = line.strip()
        if not body:
            continue
        words = re.sub(r"[`*_\[\]()]", "", body).split()
        if len(words) < 4 or not REQ_VERBS.search(body):
            continue
        key = re.sub(r"\W+", " ", body.lower()).strip()
        if key and key not in seen:
            seen.add(key)
            items.append(body)
    return heads, items


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def options(lean, suggested, granular):
    vals = []
    for v in (lean, suggested, granular):
        if v not in vals:
            vals.append(v)
    return {"lean": lean, "suggested": suggested, "granular": granular, "choices": vals}


def suggest_from_counts(n_heads, n_items):
    if n_heads >= 2:
        f = clamp(n_heads, 2, 8)
        rule_f = f"{n_heads} candidate section headings, clamped to 2..8"
    elif n_items:
        f = clamp(round(n_items / 8), 2, 6)
        rule_f = f"no usable headings, so {n_items} items / 8, clamped to 2..6"
    else:
        f, rule_f = 4, "no material: the generic starting point of 4 features"
    s = clamp(n_items, 3 * f, 12 * f) if n_items else 6 * f
    lean_s = max(2 * f, round(0.6 * s))
    gran_s = min(round(1.5 * s), 15 * f)
    return f, s, lean_s, gran_s, rule_f


def main(argv=None):
    ap = argparse.ArgumentParser(description="Suggest feature and story counts.")
    ap.add_argument("--source", nargs="*", default=[], help="requirements, brief or design documents (markdown or text)")
    ap.add_argument("--text", help="a free-text brief")
    ap.add_argument("--spec", help="an existing story spec (update mode)")
    ap.add_argument("--md", help="an existing backlog markdown in this skill's format (update mode)")
    args = ap.parse_args(argv)

    out = {"basis": "none", "counts": {}, "rationale": []}
    if args.spec or args.md:
        if args.spec:
            spec = json.load(open(args.spec, encoding="utf-8"))
            nf, ns = len(spec.get("features", [])), len(spec.get("stories", []))
        else:
            md = open(args.md, encoding="utf-8").read()
            nf = len(re.findall(r"^### F\d+\.", md, flags=re.M))
            ns = len(re.findall(r"^#### S\d+\.\d+\.", md, flags=re.M))
        out["basis"] = "existing backlog"
        out["counts"] = {"features": nf, "stories": ns, "stories_per_feature": round(ns / nf, 1) if nf else None}
        f = max(nf, 1)
        out["suggestion"] = {"features": options(max(1, f - 1), f, f + 1), "stories": options(max(f * 2, round(0.8 * ns)), ns, round(1.25 * ns))}
        out["rationale"].append(f"Update mode: the backlog has {nf} features and {ns} stories ({out['counts']['stories_per_feature']} per feature). Suggested keeps those counts; the other options allow for a leaner or a more granular backlog.")
    else:
        text = args.text or ""
        for p in args.source:
            with open(p, encoding="utf-8", errors="ignore") as f_:
                text += "\n" + f_.read()
        if text.strip():
            heads, items = candidates(text)
            level = 2 if len([h for h in heads[2] if not BOILERPLATE.match(h)]) >= 2 else 1
            feats = [h for h in heads[level] if not BOILERPLATE.match(h)]
            if level == 1 and len(feats) >= 1:
                feats = feats[1:] if len(feats) > 1 else []  # the first level-1 heading is the document title
            f, s, lean_s, gran_s, rule_f = suggest_from_counts(len(feats), len(items))
            out["basis"] = "source text"
            out["counts"] = {"candidate_feature_headings": feats[:20], "requirement_like_items": len(items), "heading_levels": {k: len(v) for k, v in heads.items()}, "sources": args.source}
            out["suggestion"] = {"features": options(max(1, f - 1), f, min(10, f + 1)), "stories": options(lean_s, s, gran_s)}
            out["rationale"] += [f"Features: {rule_f}.",
                                 f"Stories: {len(items)} requirement-like lines found; clamped to 3..12 per feature gives {s}. Lean {lean_s} (fewer, larger stories), granular {gran_s} (more, smaller stories).",
                                 "These come from counting lines, not from understanding them. Treat them as a starting point and ask the person."]
        else:
            f, s, lean_s, gran_s, rule_f = suggest_from_counts(0, 0)
            out["suggestion"] = {"features": options(3, f, 6), "stories": options(12, s, 36)}
            out["rationale"].append("No material was given, so this is the generic starting point: one epic, 4 features, about 6 stories each. Ask the person what they expect.")
    out["next"] = "Ask the person how many features and stories they expect, offering the suggested values first, then record the answers in spec.scope.features_expected and spec.scope.stories_expected."
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
