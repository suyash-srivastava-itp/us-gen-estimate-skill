#!/usr/bin/env python3
"""
Story specification: the one JSON file a backlog is written in, plus validation, fact checks, spec-clarity scoring and
Gherkin rendering. Shared by us_build.py (estimate and render), us_import.py (markdown back to a spec) and the selftest.

Spec shape (JSON, schema 1):

  {
    "schema": 1, "name": "payments-backlog",
    "epic": {"id": "E1", "title": "...", "goal": "...", "exit_criteria": ["..."]},
    "scope": {"features_expected": 4, "stories_expected": 24, "tasks_policy": "all | large | none", "source": "..."},
    "templates": {"MOVE": {"title": "...", "scenarios": [<scenario>, ...]}},          # optional shared scenarios
    "features": [{"id": "F1", "title": "...", "goal": "...", "why": "...", "exit_criteria": ["..."]}],
    "stories": [{
      "id": "S1.1", "feature": "F1", "title": "...", "type": "FEATURE", "horizon": "H1",
      "size": "M" | "focused_hours": 3,
      "role": "a release manager", "want": "...", "so": "...",
      "context": "...", "facts": [<fact>, ...],
      "scenarios": [<scenario>, ...],                    # 3 to 5, Gherkin
      "templates": ["MOVE"], "deps": ["S1.0"],
      "open_questions": [{"question": "...", "owner": "..."}],
      "paths": ["api/x"], "prs": 1, "diff_lines": null,
      "ticket_overrides": {"spec_clarity": 0.7, "reason": "..."},
      "design": "...",
      "tasks": [{"title": "...", "weight": 1, "technical_background": {
          "current_state": "...", "evidence": [<fact>, ...], "approach": "...",
          "touches": ["existing/path.py", "new:path/to/new_file.py"], "interfaces": ["..."],
          "test_plan": "...", "risks": "..."}}]
    }]
  }

  <scenario> = {"title": "...", "given": ["..."], "when": ["..."], "then": ["...", "..."],
                "examples": {"headers": ["a", "b"], "rows": [["1", "2"]]}}        # examples optional (Scenario Outline)
  <fact>     = {"claim": "...", "source": "path | command | document", "path": "repo/relative/path", "line": 12,
                "quote": "text that appears in that file", "command": "git grep ...", "checked_at": "2026-10-20"}

Facts with a "path" are verified against the repository (the file exists, the quote appears in it). Facts that rest on a
command or a document are listed under "Not verified" in the output, since this tool cannot re-run them.
"""
import json
import os
import re

SIZES = ("S", "M", "L", "XL")
TASK_BG_REQUIRED = ("current_state", "approach", "touches", "test_plan", "risks")
PLACEHOLDER_RE = re.compile(r"\b(TBD|TODO|FIXME|XXX|lorem ipsum|to be decided|to be confirmed)\b|\?\?\?|<insert|\[insert", re.I)
STEP_PREFIX_RE = re.compile(r"^(given|when|then|and|but)\s+", re.I)

VERIFY_RE = re.compile(r"`|\d|unchanged|identical|equal|passes|green|no longer|prints|fails|removed|empty|zero|exactly|returns|exits|refus|reject|contains|lists|is written|is deleted", re.I)
FAIL_RE = re.compile(r"fail|error|refus|reject|missing|unavailable|timeout|rollback|roll back|revert|fall ?back|invalid|denied|deny|conflict|without|stale|unsafe|corrupt|cannot|not (be )?(served|allowed|accepted)", re.I)
NFR_RE = re.compile(r"parity|unchanged|identical|rollback|backup|security|auth|performance|latency|throughput|compat|audit|privacy|retention|ratchet|idempoten|timeout|rate limit|accessib|encrypt|checksum", re.I)


# ------------------------------------------------------------------ io
def load_spec(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_spec(spec, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)
        f.write("\n")


def as_list(x):
    if x is None:
        return []
    return list(x) if isinstance(x, (list, tuple)) else [x]


def clean_step(text):
    return STEP_PREFIX_RE.sub("", str(text).strip())


def normalize_spec(spec):
    """Fix the small, common slips (leading 'As', 'I want', 'so that', 'Given ...' in a step) and fill defaults.
    Returns the list of changes made so the caller can tell the user."""
    notes = []
    spec.setdefault("schema", 1)
    spec.setdefault("scope", {})
    spec.setdefault("templates", {})
    spec.setdefault("epic", {})
    for f in spec.get("features", []):
        f["exit_criteria"] = as_list(f.get("exit_criteria"))
    for s in spec.get("stories", []):
        sid = s.get("id", "?")
        for key, pat in (("role", r"^as\s+"), ("want", r"^i\s+want\s+"), ("so", r"^so\s+that\s+")):
            v = str(s.get(key, "")).strip()
            n = re.sub(pat, "", v, flags=re.I)
            if n != v:
                notes.append(f"{sid}: removed the leading words from '{key}' (the format adds them)")
            s[key] = n
        s["deps"] = as_list(s.get("deps"))
        s["templates"] = as_list(s.get("templates"))
        s["facts"] = as_list(s.get("facts"))
        s["tasks"] = as_list(s.get("tasks"))
        s["paths"] = as_list(s.get("paths"))
        oq = []
        for q in as_list(s.get("open_questions")):
            oq.append(q if isinstance(q, dict) else {"question": str(q), "owner": ""})
        s["open_questions"] = oq
        scs = []
        for sc in as_list(s.get("scenarios")):
            if isinstance(sc, (list, tuple)):  # [title, given, when, then...]
                title, given, when, *then = sc
                sc = {"title": title, "given": given, "when": when, "then": [t for x in then for t in as_list(x)]}
            sc = dict(sc)
            for k in ("given", "when", "then"):
                sc[k] = [clean_step(x) for x in as_list(sc.get(k)) if str(x).strip()]
            scs.append(sc)
        s["scenarios"] = scs
        for t in s["tasks"]:
            t["technical_background"] = dict(t.get("technical_background") or {})
            tb = t["technical_background"]
            tb["touches"] = as_list(tb.get("touches"))
            tb["evidence"] = as_list(tb.get("evidence"))
            tb["interfaces"] = as_list(tb.get("interfaces"))
    return notes


# ------------------------------------------------------------------ facts
def _norm_ws(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_fact(fact, repo):
    """Return (status, message): 'verified' | 'unverified' | 'failed'."""
    if not isinstance(fact, dict) or not str(fact.get("claim", fact.get("finding", ""))).strip():
        return "failed", "a fact needs a claim"
    path = fact.get("path")
    if path:
        if not repo:
            return "unverified", f"{path}: no repository given to check against"
        full = os.path.join(repo, path.replace("/", os.sep))
        if not os.path.isfile(full):
            return "failed", f"evidence path not found in the repository: {path}"
        text = None
        if fact.get("quote") or fact.get("line"):
            try:
                with open(full, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError as e:
                return "failed", f"cannot read {path}: {e}"
        if fact.get("line") and text is not None and int(fact["line"]) > text.count("\n") + 1:
            return "failed", f"{path} has fewer than {fact['line']} lines"
        if fact.get("quote") and text is not None and _norm_ws(fact["quote"]) not in _norm_ws(text):
            return "failed", f"the quoted text does not appear in {path}: {str(fact['quote'])[:60]}"
        return "verified", f"{path} checked" + (" (quote found)" if fact.get("quote") else "")
    if fact.get("command") or fact.get("source"):
        return "unverified", "rests on a command or document; this tool cannot re-run it"
    return "failed", "a fact needs a path, a command or a source"


def check_touches(touches, repo):
    """Existing paths must exist; paths prefixed 'new:' are files the task will create."""
    bad = []
    if not repo:
        return bad
    for t in touches:
        if str(t).startswith("new:"):
            continue
        if not os.path.exists(os.path.join(repo, str(t).replace("/", os.sep))):
            bad.append(str(t))
    return bad


# ------------------------------------------------------------------ validation
def story_texts(s):
    out = [s.get("title", ""), s.get("role", ""), s.get("want", ""), s.get("so", ""), s.get("context", ""), s.get("design", "")]
    for sc in s.get("scenarios", []):
        out += [sc.get("title", "")] + sc.get("given", []) + sc.get("when", []) + sc.get("then", [])
    for t in s.get("tasks", []):
        out.append(t.get("title", ""))
        tb = t.get("technical_background", {})
        out += [str(tb.get(k, "")) for k in ("current_state", "approach", "test_plan", "risks")]
    return [str(x) for x in out if x]


def task_needed(story, policy, scale_hours=None):
    if policy == "all":
        return True
    if policy == "large":
        return story.get("size") in ("L", "XL") or float(story.get("focused_hours") or 0) >= (scale_hours or 8)
    return False


def validate_spec(spec, repo=None, require_facts=True, large_hours=8):
    """Return {'errors': [...], 'warnings': [...], 'not_verified': [...], 'facts': {'verified': n, ...}}."""
    errors, warnings, not_verified = [], [], []
    counts = {"verified": 0, "unverified": 0, "failed": 0}
    E = errors.append
    W = warnings.append

    if not spec.get("name"):
        E("the spec needs a name")
    if not (spec.get("epic") or {}).get("title"):
        E("the epic needs a title")
    features = spec.get("features") or []
    stories = spec.get("stories") or []
    if not features:
        E("the spec has no features")
    if not stories:
        E("the spec has no stories")
    policy = (spec.get("scope") or {}).get("tasks_policy", "none")
    if policy not in ("all", "large", "none"):
        E(f"scope.tasks_policy must be all, large or none, not {policy!r}")

    fids = [f.get("id") for f in features]
    if len(set(fids)) != len(fids) or any(not i for i in fids):
        E("feature ids must be present and unique")
    for f in features:
        if not f.get("title"):
            E(f"{f.get('id')}: feature needs a title")
        if not f.get("goal"):
            W(f"{f.get('id')}: feature has no goal")
        n = sum(1 for s in stories if s.get("feature") == f.get("id"))
        if n == 0:
            E(f"{f.get('id')}: feature has no stories")
        elif n > 15:
            W(f"{f.get('id')}: {n} stories in one feature; consider splitting the feature")

    ids = [s.get("id") for s in stories]
    if len(set(ids)) != len(ids) or any(not i for i in ids):
        E("story ids must be present and unique")
    idset = set(ids)
    templates = set((spec.get("templates") or {}).keys())

    for s in stories:
        sid = s.get("id", "?")
        for k in ("title", "type", "role", "want", "so"):
            if not str(s.get(k, "")).strip():
                E(f"{sid}: missing {k}")
        if s.get("feature") not in fids:
            E(f"{sid}: feature {s.get('feature')!r} does not exist")
        if s.get("size") not in SIZES and not s.get("focused_hours"):
            E(f"{sid}: give a size ({', '.join(SIZES)}) or focused_hours")
        if s.get("size") == "XL":
            W(f"{sid}: size XL; split it at planning")
        scs = s.get("scenarios", [])
        if not 3 <= len(scs) <= 5:
            E(f"{sid}: {len(scs)} acceptance scenarios; the standard is 3 to 5")
        titles = set()
        for i, sc in enumerate(scs, 1):
            tag = f"{sid} scenario {i}"
            if not str(sc.get("title", "")).strip():
                E(f"{tag}: needs a title")
            if sc.get("title") in titles:
                W(f"{tag}: duplicate scenario title")
            titles.add(sc.get("title"))
            for k in ("given", "when", "then"):
                if not sc.get(k):
                    E(f"{tag}: needs at least one '{k}' step")
            ex = sc.get("examples")
            if ex and (not ex.get("headers") or not ex.get("rows") or any(len(r) != len(ex["headers"]) for r in ex["rows"])):
                E(f"{tag}: examples need headers and rows of the same width")
        if scs and not any(FAIL_RE.search(" ".join([sc.get("title", "")] + sc.get("then", []))) for sc in scs):
            W(f"{sid}: no scenario describes a failure, refusal or rollback")
        for d in s.get("deps", []):
            if d not in idset:
                E(f"{sid}: unknown dependency {d}")
            elif d == sid:
                E(f"{sid}: depends on itself")
        for t in s.get("templates", []):
            if t not in templates:
                E(f"{sid}: template {t!r} is not defined in spec.templates")
        for txt in story_texts(s):
            m = PLACEHOLDER_RE.search(txt)
            if m:
                E(f"{sid}: placeholder text ({m.group(0)!r}) in: {txt[:70]}")
                break
        # facts
        facts = s.get("facts", [])
        for fact in facts:
            st, msg = verify_fact(fact, repo)
            counts[st] += 1
            if st == "failed":
                E(f"{sid}: fact failed: {msg}")
            elif st == "unverified":
                not_verified.append(f"{sid}: {str(fact.get('claim', ''))[:90]} ({msg})")
        if re.search(r"\d", s.get("context", "")) and not facts:
            W(f"{sid}: the context contains numbers but the story lists no facts with sources")
        if s.get("paths"):
            bad = check_touches(s["paths"], repo)
            if bad:
                W(f"{sid}: paths not found in the repository (use 'new:' for files to be created): {bad}")
        # tasks
        tasks = s.get("tasks", [])
        if task_needed(s, policy, large_hours) and not tasks:
            E(f"{sid}: tasks_policy is {policy!r} but the story has no tasks")
        for j, t in enumerate(tasks, 1):
            tag = f"{sid} task {j}"
            if not str(t.get("title", "")).strip():
                E(f"{tag}: needs a title")
            if float(t.get("weight", 1) or 0) <= 0:
                E(f"{tag}: weight must be positive")
            tb = t.get("technical_background", {})
            for k in TASK_BG_REQUIRED:
                if not tb.get(k):
                    E(f"{tag}: technical_background.{k} is required")
            bad = check_touches(tb.get("touches", []), repo)
            if bad:
                E(f"{tag}: touches paths that do not exist (prefix new: for files to be created): {bad}")
            ev = tb.get("evidence", [])
            if require_facts and not ev:
                E(f"{tag}: technical_background needs at least one evidence item (a file, line or quote that shows the current state)")
            for fact in ev:
                st, msg = verify_fact(fact, repo)
                counts[st] += 1
                if st == "failed":
                    E(f"{tag}: evidence failed: {msg}")
                elif st == "unverified":
                    not_verified.append(f"{tag}: {str(fact.get('claim', fact.get('finding', '')))[:90]} ({msg})")

    # dependency cycles
    state = {}
    by_id = {s.get("id"): s for s in stories}

    def visit(i, stack):
        if state.get(i) == 2 or i not in by_id:
            return
        if state.get(i) == 1:
            E("dependency cycle: " + " -> ".join(stack + [i]))
            return
        state[i] = 1
        for d in by_id[i].get("deps", []):
            visit(d, stack + [i])
        state[i] = 2

    for i in ids:
        visit(i, [])

    # counts against what the user asked for
    sc = spec.get("scope") or {}
    fe, se = sc.get("features_expected"), sc.get("stories_expected")
    if fe and len(features) != int(fe):
        W(f"you expected {fe} features and the spec has {len(features)}")
    if se and abs(len(stories) - int(se)) > max(1, round(0.2 * int(se))):
        W(f"you expected about {se} stories and the spec has {len(stories)}")
    return {"errors": errors, "warnings": warnings, "not_verified": not_verified, "facts": counts}


# ------------------------------------------------------------------ spec clarity (rubric, scored from the story text)
def score_clarity(s):
    """The six-criterion rubric (1 / 0.5 / 0), scored conservatively from the story. Returns (score, criteria, evidence)."""
    scs = s.get("scenarios", [])
    thens = [t for sc in scs for t in sc.get("then", [])]
    allsteps = [x for sc in scs for k in ("given", "when", "then") for x in sc.get(k, [])]
    q = lambda t, n=110: (re.sub(r"\s+", " ", str(t)).strip()[:n - 1] + "…") if len(str(t)) > n else re.sub(r"\s+", " ", str(t)).strip()
    ev = {}
    ver = [t for t in thens if VERIFY_RE.search(t)]
    c1 = 1.0 if len(scs) >= 3 and thens and len(ver) >= 0.6 * len(thens) else (0.5 if scs else 0.0)
    ev["acceptance criteria"] = f"{len(ver)} of {len(thens)} outcomes are checkable; e.g. {q(ver[0])}" if ver else "no checkable outcome"
    fm = [sc for sc in scs if FAIL_RE.search(" ".join([sc.get("title", "")] + sc.get("then", [])))]
    c2 = 1.0 if len(fm) >= 2 else (0.5 if len(fm) == 1 else 0.0)
    ev["edge cases and errors"] = q(fm[0].get("title", "")) if fm else "no failure case named"
    ex = [sc for sc in scs if sc.get("examples")]
    both = [sc for sc in scs if re.search(r"`", " ".join(sc.get("when", []))) and re.search(r"`", " ".join(sc.get("then", [])))]
    c3 = 1.0 if ex else (0.5 if both else 0.0)
    ev["concrete example"] = "an examples table" if ex else (q(both[0].get("title", "")) if both else "none")
    coded = re.findall(r"`[^`]*[/._][^`]*`", s.get("context", "") + " " + " ".join(allsteps))
    c4 = 1.0 if (s.get("paths") or coded) else 0.5
    ev["scope and dependencies"] = f"{len(s.get('paths', []))} paths, {len(coded)} named modules; deps {s.get('deps') or 'none'}"
    if s.get("open_questions"):
        c5, ev["design decided"] = 0.0, f"{len(s['open_questions'])} open question(s)"
    elif str(s.get("type", "")).upper() in ("SPIKE", "DOC", "DECISION"):
        c5, ev["design decided"] = 0.0, "the story's job is to decide"
    elif s.get("design") or any((t.get("technical_background") or {}).get("approach") for t in s.get("tasks", [])):
        c5, ev["design decided"] = 1.0, "an approach is written down"
    else:
        c5, ev["design decided"] = 0.5, "no design or approach recorded"
    nf = [x for x in [s.get("context", "")] + allsteps if NFR_RE.search(x)]
    c6 = 1.0 if nf else 0.5
    ev["non-functional constraints"] = q(nf[0]) if nf else "none stated in the story"
    crit = [c1, c2, c3, c4, c5, c6]
    return round(sum(crit) / 6, 3), crit, ev


CRITERIA_NAMES = ["acceptance criteria", "edge cases and errors", "concrete example", "scope and dependencies", "design decided", "non-functional constraints"]


# ------------------------------------------------------------------ gherkin
def gherkin(scenarios, feature=None):
    out = [f"Feature: {feature}", ""] if feature else []
    ind = "  " if feature else ""
    for i, sc in enumerate(scenarios):
        out.append(f"{ind}{'Scenario Outline' if sc.get('examples') else 'Scenario'}: {sc['title']}")
        for kw, key in (("Given", "given"), ("When", "when"), ("Then", "then")):
            for j, step in enumerate(sc.get(key, [])):
                out.append(f"{ind}  {kw if j == 0 else 'And'} {step}")
        ex = sc.get("examples")
        if ex:
            out.append("")
            out.append(f"{ind}  Examples:")
            widths = [max(len(str(x)) for x in [h] + [r[c] for r in ex["rows"]]) for c, h in enumerate(ex["headers"])]
            line = lambda cells: f"{ind}    | " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)) + " |"
            out.append(line(ex["headers"]))
            out.extend(line(r) for r in ex["rows"])
        if i < len(scenarios) - 1:
            out.append("")
    return "\n".join(out)


def user_story_line(s):
    return f"As {s['role']}, I want {s['want']}, so that {s['so']}."


def html_escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
