#!/usr/bin/env python3
"""
Usage mining — which skills have ACTUALLY fired, and when.

Reads ~/.claude/projects/*.jsonl for Skill invocations. That is ~2 GB here,
so results are cached; pass --refresh to re-mine.

Why this exists: the Skills Library page calls trigger testing "the part
nothing has ever done". A skill that has never fired is either one you have
never needed, or one whose description never matches how you speak. This
tells you which skills are in that set; the phrase test tells you which kind.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"
CACHE = Path(__file__).resolve().parent / "usage.json"

# "skill":"name" or "skill": "plugin:name" - the plugin prefix is stripped so
# a repo skill uploaded as anthropic-skills:card-factory matches card-factory.
PATTERN = re.compile(rb'"skill"\s*:\s*"([a-zA-Z0-9:_-]+)"')


def mine() -> dict:
    counts: dict[str, int] = {}
    last: dict[str, float] = {}

    if not PROJECTS.is_dir():
        return {"counts": {}, "last": {}, "scanned": 0, "at": time.time()}

    files = list(PROJECTS.rglob("*.jsonl"))
    for n, f in enumerate(files, 1):
        try:
            blob = f.read_bytes()
        except OSError:
            continue
        hits = PATTERN.findall(blob)
        if not hits:
            continue
        # File mtime is a good enough proxy for "when this was used" and far
        # cheaper than parsing every line's timestamp out of a 2 GB corpus.
        try:
            mtime = f.stat().st_mtime
        except OSError:
            mtime = 0.0
        for raw in hits:
            name = raw.decode("utf-8", "replace").split(":")[-1]
            counts[name] = counts.get(name, 0) + 1
            if mtime > last.get(name, 0):
                last[name] = mtime
        if n % 40 == 0:
            print(f"    …{n}/{len(files)} transcripts", flush=True)

    return {"counts": counts, "last": last, "scanned": len(files), "at": time.time()}


def load(refresh: bool = False) -> dict:
    if not refresh:
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    print("  mining transcripts (first run is slow)…", flush=True)
    data = mine()
    CACHE.write_text(json.dumps(data), encoding="utf-8")
    return data


def days_since(ts: float | None) -> int | None:
    if not ts:
        return None
    return int((datetime.now(timezone.utc) - datetime.fromtimestamp(ts, timezone.utc)).days)


if __name__ == "__main__":
    d = load("--refresh" in sys.argv)
    print(f"{d['scanned']} transcripts, {len(d['counts'])} skills ever invoked")
    for name, n in sorted(d["counts"].items(), key=lambda kv: -kv[1])[:20]:
        ago = days_since(d["last"].get(name))
        print(f"  {n:4}x  {name:<34} last used {ago}d ago")
