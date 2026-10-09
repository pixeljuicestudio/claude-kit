#!/usr/bin/env python3
"""
When did each skill first exist?

A skill cannot fail to fire before it was written. Without this, replay.py
reports every prompt that PREDATES a skill as a miss - and worse, the prompts
that caused a skill to be created score highest against it, because its
description was written from those very words. audio-balance was created
mid-session on 22 July out of the exact complaints that then ranked #1 as
"misses".

Reads the earliest SKILL.md creation for each skill out of the transcripts and
writes born.json.

    python born.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
OUT = Path(__file__).resolve().parent / "born.json"

# The separator is one "/" or an escaped "\\", so it must be a quantified class,
# not two dots. Two dots silently ate the first and last letter of every name
# that used a single slash: "resolve-conform" came back as "esolve-confor".
# Written to a file rather than piped through a heredoc, which mangles
# backslashes in this environment.
PATH_RE = re.compile(r"skills[\\/]+([a-z0-9][a-z0-9-]{2,})[\\/]+SKILL\.md")
TS_RE = re.compile(r'"timestamp":"(\d{4}-\d\d-\d\d)')


def main() -> None:
    born: dict[str, str] = {}
    for f in PROJECTS.rglob("*.jsonl"):
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "SKILL.md" not in raw:
            continue
        for line in raw.splitlines():
            if '"type":"create"' not in line:
                continue
            m, t = PATH_RE.search(line), TS_RE.search(line)
            if not (m and t):
                continue
            name, day = m.group(1), t.group(1)
            if name not in born or day < born[name]:
                born[name] = day

    OUT.write_text(json.dumps(born, indent=1, sort_keys=True), encoding="utf-8")
    print(f"{len(born)} skills dated -> {OUT.name}")
    for k in sorted(born):
        print(f"  {k:<32} {born[k]}")


if __name__ == "__main__":
    main()
