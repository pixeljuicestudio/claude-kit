#!/usr/bin/env python3
"""Groups — your own cross-inventory shortlists (Skills + Prompts + Tools).

Every other curated list in Kit — favourites, tags — is scoped to ONE
inventory on purpose: `/api/fav` validates a name against the skill scan,
`/api/ptag` against the prompt cache, and so on, so each file only ever
holds names its own endpoint can actually check. A Group breaks that
deliberately. Its entire point is "the avid-handoff skill AND the ytclip
tool, together" — a shortlist for one job (editing, writing, brainstorming)
that lives across tabs. So it is not "skills.tags but bigger" — it is the
one place in Kit that names its members with a (kind, name) pair instead of
a bare name, and the one file all three inventories write to.

Hooks and Progs are deliberately NOT groupable. They are plumbing you
reconcile, not "go-to tricks" you would curate a shortlist of.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

HERE = Path(__file__).resolve().parent
GROUPS = HERE / "groups.json"
KINDS = ("skill", "prompt", "tool")


def load_groups() -> dict:
    try:
        raw = json.loads(GROUPS.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_groups(groups: dict) -> dict:
    """Validated shape only — MEMBERSHIP against a live inventory is the
    caller's job (serve.py has the three scans already; this module would
    have to import all three just to duplicate a check it cannot do any
    better). Drops anything malformed rather than refusing the whole write,
    same as tags.json's save_tags."""
    clean: dict = {}
    for gid, g in groups.items():
        if not isinstance(gid, str) or not gid or not isinstance(g, dict):
            continue
        name = g.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        members = []
        seen = set()
        for m in g.get("members", []):
            if not isinstance(m, dict):
                continue
            kind, mname = m.get("kind"), m.get("name")
            if kind not in KINDS or not isinstance(mname, str) or not mname:
                continue
            key = (kind, mname)
            if key in seen:
                continue
            seen.add(key)
            members.append({"kind": kind, "name": mname})
        clean[gid] = {"name": name.strip(), "members": members}
    tmp = GROUPS.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    os.replace(tmp, GROUPS)
    return clean


def new_id(existing) -> str:
    """8 hex chars is plenty for a human curating a handful of shortlists by
    hand — collision odds do not matter at this scale, so this just re-rolls
    on the rare hit rather than reaching for anything heavier."""
    existing = set(existing)
    while True:
        gid = secrets.token_hex(4)
        if gid not in existing:
            return gid
