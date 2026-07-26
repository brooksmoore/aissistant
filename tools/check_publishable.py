#!/usr/bin/env python3
"""Publish gate — refuse to let this repo go public while it still carries user data.

RULE (Brooks, 2026-07-25): user transcripts with aissistant instances must NEVER be
public. Not the databases, and not content *derived* from them — the verbatim messages,
real first names, and personal errands that ended up in code comments and test fixtures
because every guard here was written from a live incident.

Run before making the repo public, before any push to a public remote, and in CI:

    python3 tools/check_publishable.py        # exit 0 = safe to publish

Exit 1 lists every hit. This gate is expected to FAIL today — that is the point; it
records the debt rather than hiding it. Scrub, then let it pass. Do not weaken the
denylist to make it green.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Real people (first names used in live incidents) and real personal errands quoted
# verbatim from instance conversations. Add to this list; never remove to pass.
PERSONAL_TOKENS = [
    r"\bBrian\b", r"\bClint\b", r"\bKyra\b", r"\bNikki\b",
    r"\bNespresso\b", r"\bREVOLVE\b", r"\bRevolve\b",
    r"\bmovie night\b", r"\bcar clubs?\b", r"\bWorkday\b",
]

# Files that must never be tracked at all.
FORBIDDEN_PATHS = [
    re.compile(r"^instances/"),
    re.compile(r"\.db$"),
    re.compile(r"\.log$"),
    re.compile(r"google_(credentials|token)\.json$"),
    re.compile(r"^\.env$"),
]

# Health / mental-state framing about a real owner. The default OWNER_FRAME shipped in
# .env.example described a real person's anxiety — third-party health information that
# person never consented to publish.
SENSITIVE_FRAMING = [
    re.compile(r"anxiety", re.I),
    re.compile(r"\bdepress", re.I),
    re.compile(r"\btherap", re.I),
    # NB: deliberately no /diagnos/ — it fires on "self-diagnosis" and "diagnosable",
    # which are engineering words here. Keep this list about the owner, not the code.
]


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f.strip()]


def offending_commits(token_re: re.Pattern) -> list[str]:
    """Commit MESSAGES carrying personal content — the exposure a file scan
    cannot see and a file edit cannot fix.

    Scrubbing the working tree makes this gate green while the same names sit in
    every commit message in the history, and publishing a repo publishes its
    history. Nothing about `git rm` or an edit touches that. The only real fixes
    are to publish as a fresh repo with one squashed initial commit (free while
    the repo is still private) or to rewrite history (breaks every clone). So
    this check reports rather than blocks: it prints the count and the guidance,
    and leaves the publish decision informed instead of falsely reassured."""
    out = subprocess.run(
        ["git", "log", "--all", "--format=%H%x1f%s%x1f%b%x1e"],
        cwd=ROOT, capture_output=True, text=True,
    )
    hits = []
    for record in out.stdout.split("\x1e"):
        if not record.strip():
            continue
        parts = record.strip().split("\x1f")
        sha, subject = parts[0], (parts[1] if len(parts) > 1 else "")
        body = parts[2] if len(parts) > 2 else ""
        if token_re.search(subject) or token_re.search(body):
            hits.append(f"{sha[:8]} {subject[:72]}")
    return hits


def main() -> int:
    files = tracked_files()
    problems: list[str] = []

    for f in files:
        for pat in FORBIDDEN_PATHS:
            if pat.search(f):
                problems.append(f"TRACKED USER DATA: {f} (must never be in git)")

    token_re = re.compile("|".join(PERSONAL_TOKENS))
    # This file necessarily CONTAINS every pattern it searches for — the denylist
    # above and the comments explaining why each entry exists. Scanning itself
    # produces guaranteed false positives that can only be silenced by weakening
    # the denylist, which is the one thing this gate must never do. Exempt this
    # single path by exact name (not a glob, not a directory) so nothing else can
    # hide behind it.
    SELF = "tools/check_publishable.py"
    for f in files:
        if f == SELF:
            continue
        p = ROOT / f
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if token_re.search(line):
                problems.append(f"PERSONAL CONTENT: {f}:{i}: {line.strip()[:100]}")
            for pat in SENSITIVE_FRAMING:
                if pat.search(line):
                    problems.append(f"SENSITIVE FRAMING: {f}:{i}: {line.strip()[:100]}")

    history = offending_commits(token_re)

    if problems:
        print(f"NOT PUBLISHABLE — {len(problems)} finding(s):\n")
        for pr in problems[:60]:
            print("  " + pr)
        if len(problems) > 60:
            print(f"  ... and {len(problems) - 60} more")
        print(
            "\nUser transcripts and anything derived from them stay private. "
            "Scrub to fixtures that are not real people, then re-run."
        )
        _report_history(history)
        return 1

    print("working tree: no tracked user data, no personal content, no sensitive framing")
    _report_history(history)
    if history:
        print("\n=> A CLEAN WORKING TREE IS NOT A CLEAN REPO. Publishing publishes the history.")
        return 1
    print("publishable.")
    return 0


def _report_history(history: list[str]) -> None:
    if not history:
        print("history: no personal content in any commit message")
        return
    print(f"\nHISTORY: {len(history)} commit message(s) carry personal content — "
          "a file scan cannot see this and an edit cannot fix it:")
    for line in history[:10]:
        print("  " + line)
    if len(history) > 10:
        print(f"  ... and {len(history) - 10} more")
    print("  FIX: publish as a fresh repo with ONE squashed initial commit (free while this\n"
          "       repo is private), or rewrite history (breaks every existing clone).")


if __name__ == "__main__":
    sys.exit(main())
