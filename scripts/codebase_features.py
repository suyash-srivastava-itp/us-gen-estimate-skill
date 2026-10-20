#!/usr/bin/env python3
"""
Measure the codebase inputs to estimate.py for the paths a ticket touches.

Outputs ticket_fields (test_coverage, complexity, tacit) with provenance:

  measured  test_coverage read from a coverage report (cobertura XML, lcov, or coverage.py JSON)
  proxy     inferred from repo structure. Directional only.

  test_coverage  without a report: share of in-scope source files that have a matching test file.
                 This is file-level presence, not line coverage.
  complexity     branch keywords per 100 lines of in-scope source, squashed to 0..1.
  tacit          how little the module is documented in markdown, lowered if CLAUDE.md or
                 AGENTS.md exists for it.

Usage:
  python3 codebase_features.py --repo . --paths src/billing src/invoices [--coverage coverage.xml]
"""
import argparse
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

SOURCE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".java", ".kt", ".rb", ".rs",
              ".cs", ".php", ".swift", ".scala", ".c", ".cc", ".cpp", ".h", ".hpp"}
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "dist", "build", "target", "vendor",
             "__pycache__", ".next", "coverage", ".tox", ".mypy_cache", ".pytest_cache"}
TEST_NAME_RE = re.compile(r"(^test_.*\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$"
                          r"|Test\.java$|Tests?\.kt$|_spec\.rb$)")
TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)")
BRANCH_RE = re.compile(r"\b(?:if|elif|else if|for|while|case|catch|except|match)\b|&&|\|\||\?\?")
INSTRUCTION_FILES = {"CLAUDE.md", "AGENTS.md"}
CI_MARKERS = [".github/workflows", ".gitlab-ci.yml", "Jenkinsfile", ".circleci",
              "azure-pipelines.yml", "bitbucket-pipelines.yml"]
DOC_EXT = (".md", ".rst", ".adoc", ".txt")


def norm(p):
    p = p.replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def is_test(rel):
    return bool(TEST_NAME_RE.search(os.path.basename(rel)) or TEST_DIR_RE.search(rel))


def in_scope(rel, paths):
    return any(rel == p or rel.startswith(p + "/") for p in paths)


def list_repo_files(repo):
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            out.append(norm(os.path.relpath(os.path.join(root, f), repo)))
    return out


def read_lines(repo, rel):
    try:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="ignore") as f:
            return f.read().splitlines()
    except OSError:
        return []


def test_stem(name):
    stem = os.path.splitext(name)[0]
    for suffix in ("_test", ".test", ".spec", "_spec", "Test"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if stem.startswith("test_"):
        stem = stem[5:]
    return stem.lower()


def load_coverage(path):
    """Return {repo-relative file: (covered_lines, total_lines)}."""
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    cov = {}
    if path.endswith(".json"):
        for fn, info in json.loads(text).get("files", {}).items():
            s = info.get("summary", {})
            cov[norm(fn)] = (s.get("covered_lines", 0), s.get("num_statements", 0))
    elif path.endswith(".xml"):
        for cls in ET.fromstring(text).iter("class"):
            fn = cls.get("filename")
            lines = list(cls.iter("line"))
            if fn and lines:
                cov[norm(fn)] = (sum(int(x.get("hits", "0")) > 0 for x in lines), len(lines))
    else:  # lcov
        cur, lf, lh = None, 0, 0
        for line in text.splitlines():
            if line.startswith("SF:"):
                cur, lf, lh = norm(line[3:].strip()), 0, 0
            elif line.startswith("LF:"):
                lf = int(line[3:])
            elif line.startswith("LH:"):
                lh = int(line[3:])
            elif line.strip() == "end_of_record" and cur:
                cov[cur] = (lh, lf)
    return cov


def match_coverage(cov, files):
    covered = total = matched = 0
    for f in files:
        key = next((k for k in cov if k == f or f.endswith("/" + k) or k.endswith("/" + f)), None)
        if key is not None:
            cv, tot = cov[key]
            covered += cv
            total += tot
            matched += 1
    rate = covered / total if total else None
    return rate, matched


def proxy_test_coverage(all_files, src):
    if not src:
        return 0.0
    test_stems = {test_stem(os.path.basename(f)) for f in all_files if is_test(f)}
    hit = sum(os.path.splitext(os.path.basename(f))[0].lower() in test_stems for f in src)
    return hit / len(src)


def measure(repo, paths, coverage=None, all_files=None, doc_cache=None):
    """Measure test coverage, complexity and tacit-knowledge proxies for `paths`.

    all_files and doc_cache may be passed in and reused across calls, so measuring many stories walks the repository
    and reads the documents once. doc_cache maps a document path to its lower-cased text and is filled on demand.
    """
    repo = os.path.abspath(repo)
    paths = [norm(os.path.normpath(p)).rstrip("/") for p in paths]
    if all_files is None:
        all_files = list_repo_files(repo)
    if doc_cache is None:
        doc_cache = {}
    scope = [f for f in all_files if in_scope(f, paths)]
    src = [f for f in scope if os.path.splitext(f)[1] in SOURCE_EXT and not is_test(f)]
    tests_in_scope = [f for f in scope if is_test(f)]
    missing = [p for p in paths if not any(f == p or f.startswith(p + "/") for f in all_files)]
    notes = []
    if missing:
        notes.append(f"Paths not found in repo: {missing}")
    if not src:
        notes.append("No source files found in the given paths. Check --paths.")

    # Test coverage: measured if a report is given and matches, otherwise a proxy.
    tc, tc_source = None, "proxy"
    if coverage:
        cov = load_coverage(coverage)
        rate, matched = match_coverage(cov, src)
        if rate is not None:
            tc, tc_source = rate, "measured"
            notes.append(f"Line coverage measured over {matched} in-scope source files.")
        else:
            notes.append("Coverage report has no in-scope files. Check that its paths match the repo layout.")
    if tc is None:
        tc = proxy_test_coverage(all_files, src)
        notes.append("test_coverage is a proxy (share of source files with a matching test file), "
                     "not line coverage. Generate a coverage report for a real value.")

    # Complexity: branch keywords per 100 lines of in-scope source.
    loc = branches = 0
    for f in src:
        for line in read_lines(repo, f):
            if line.strip():
                loc += 1
            branches += len(BRANCH_RE.findall(line))
    density = 100.0 * branches / max(loc, 1)
    complexity = 1.0 - math.exp(-density / 20.0)

    # Tacit knowledge: how often markdown in the repo mentions the module names.
    names = {os.path.splitext(os.path.basename(p))[0].lower() for p in paths}
    names = {n for n in names if len(n) >= 4}
    doc_files = [f for f in all_files if f.lower().endswith(DOC_EXT)]
    mentions = 0
    for f in doc_files:
        if f not in doc_cache:
            doc_cache[f] = "\n".join(read_lines(repo, f)).lower()
        if any(n in doc_cache[f] for n in names):
            mentions += 1
    tacit = 0.7 if mentions == 0 else (0.5 if mentions <= 2 else 0.3)

    # Instruction files at the root or on the path to the scope.
    instr = []
    for f in all_files:
        if os.path.basename(f) not in INSTRUCTION_FILES:
            continue
        d = os.path.dirname(f)
        if d == "" or any(p == d or p.startswith(d + "/") or d.startswith(p + "/") for p in paths):
            instr.append(f)
    if instr:
        tacit = max(0.1, tacit - 0.15)

    ci = [m for m in CI_MARKERS if os.path.exists(os.path.join(repo, m))]

    return {
        "ticket_fields": {"test_coverage": round(tc, 3),
                          "complexity": round(complexity, 3),
                          "tacit": round(tacit, 3)},
        "provenance": {"test_coverage": tc_source, "complexity": "proxy", "tacit": "proxy"},
        "scope": {"paths": paths, "source_files": len(src), "test_files": len(tests_in_scope),
                  "loc": loc, "branch_keywords_per_100_loc": round(density, 2)},
        "mentioning_docs": mentions,
        "instruction_files": instr,
        "ci_configs": ci,
        "notes": notes,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Measure codebase features for a ticket's paths.")
    ap.add_argument("--repo", default=".", help="repository root")
    ap.add_argument("--paths", nargs="+", required=True,
                    help="files or directories the ticket touches, relative to the repo root")
    ap.add_argument("--coverage", help="coverage report: cobertura XML, lcov .info, or coverage.py JSON")
    args = ap.parse_args(argv)
    json.dump(measure(args.repo, args.paths, args.coverage), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
