#!/usr/bin/env python3
"""
Guard — catch a description edit that breaks a trigger.

Trimming a description to fit the 1024-char upload limit is a real risk: the
words removed may be the ones that made the skill win. This records, for every
auto-reachable skill, which REAL prompts from the transcripts it currently
ranks first on - then tells you what changed after you edit.

    python guard.py --save          snapshot before editing
    python guard.py --check         what moved since the snapshot

A LOST line is a prompt the skill used to win and now does not. That is the
failure this exists to catch. A GAINED line is usually fine but worth reading -
it can mean a description has broadened into another skill's territory.

Same caveat as replay.py: this scores word overlap, not meaning. It cannot
prove a trigger still works; it can only prove the words that used to
distinguish it are still there. That is a floor, not a guarantee.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import replay
import scan

HERE = Path(__file__).resolve().parent
SNAP = HERE / "guard_baseline.json"
# Same default as replay.py: transcripts_project from kit.config.json, else
# the project folder with the most transcripts. --project overrides.
PROJECT = replay.DEFAULT_PROJECT

# ONE definition, imported - not a literal repeated here. replay.py exposes the
# same number as --min, so the two could silently diverge and the pair would
# then disagree about which prompts even count.
FLOOR = replay.GUARD_FLOOR

# Remember EVERY prompt a skill ranks first on, not a top-N slice.
#
# The first version kept the top 6 by score. That made the diff lie: when a
# skill gained one high-scoring win, a prompt it STILL won got pushed out of
# the list and was reported as LOST. Ten such phantom losses appeared on the
# first real run. A regression test that invents regressions is worse than no
# regression test, because it trains you to skim the output.


def winners(project: str | None = None) -> dict[str, list[str]]:
    """{skill: [prompts it currently ranks FIRST on]}"""
    items, _ = scan.scan()
    skills = [i for i in items if i["reach"] == "auto" and i["desc"]]

    prompts: list[str] = []
    for f in replay.transcripts(project or PROJECT):
        prompts += [t for t in replay.user_turns(f)
                    if len(t.split()) >= replay.MIN_PROMPT_WORDS]

    pterms = [replay.terms(t) for t in prompts]
    idf = replay.build_idf(pterms)
    sterms = {s["name"]: replay.terms(s["name"].replace("-", " ") + " " + s["desc"])
              for s in skills}

    best: dict[str, list[tuple[float, str]]] = {s["name"]: [] for s in skills}
    for text, pt in zip(prompts, pterms):
        ranked = []
        for s in skills:
            sc, _ = replay.score(pt, sterms[s["name"]], idf)
            if sc > 0:
                ranked.append((sc, s["name"]))
        if not ranked:
            continue
        ranked.sort(reverse=True)
        top_score, top_name = ranked[0]
        # Below this, no skill really matches and the "winner" is a coin flip
        # among near-zero scores. Tracking those produced 97 phantom losses
        # that buried the two findings worth reading.
        if top_score < FLOOR:
            continue
        # Identity is a hash of the WHOLE prompt, not its first 120 chars.
        # Distinct prompts sharing a prefix collapsed into one set member -
        # 145 collisions across 2,550 prompts, three of them inside
        # resolve-conform's win list alone. An edit that killed one win while
        # its prefix-twin survived printed NOTHING, which is the exact false
        # negative this file exists to prevent.
        flat = " ".join(text.split())
        key = hashlib.sha1(flat.encode("utf-8")).hexdigest()[:12]
        best[top_name].append((top_score, key, flat[:120]))

    return {k: [{"id": i, "text": t} for _, i, t in sorted(v, reverse=True)]
            for k, v in best.items() if v}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--project", default=PROJECT,
                    help="~/.claude/projects/<name> to read, or ALL")
    args = ap.parse_args()

    now = winners(args.project)

    if args.save or not SNAP.exists():
        SNAP.write_text(json.dumps(now, indent=1), encoding="utf-8")
        print(f"baseline saved: {sum(len(v) for v in now.values())} winning prompts "
              f"across {len(now)} skills -> {SNAP.name}")
        return

    was = json.loads(SNAP.read_text(encoding="utf-8"))
    lost = gained = 0
    for name in sorted(set(was) | set(now)):
        a = {w["id"]: w["text"] for w in was.get(name, [])}
        b = {w["id"]: w["text"] for w in now.get(name, [])}
        for k in sorted(a.keys() - b.keys(), key=lambda k: a[k]):
            print(f"LOST   {name}\n         > {a[k]}")
            lost += 1
        for k in sorted(b.keys() - a.keys(), key=lambda k: b[k]):
            print(f"GAINED {name}\n         > {b[k]}")
            gained += 1

    print(f"\n{lost} lost, {gained} gained")
    if lost:
        print("A LOST prompt is a trigger that used to win and no longer does. Read it.")


if __name__ == "__main__":
    main()
