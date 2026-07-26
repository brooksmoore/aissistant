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


def main() -> int:
    files = tracked_files()
    problems: list[str] = []

    for f in files:
        for pat in FORBIDDEN_PATHS:
            if pat.search(f):
                problems.append(f"TRACKED USER DATA: {f} (must never be in git)")

    token_re = re.compile("|".join(PERSONAL_TOKENS))
    for f in files:
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
        return 1

    print("publishable: no tracked user data, no personal content, no sensitive framing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
