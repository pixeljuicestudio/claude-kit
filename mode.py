#!/usr/bin/env python3
"""
Auto / Manual — control which of your own skills fire on their own.

    python mode.py list                 every machine skill and its mode
    python mode.py manual <name>...     stop it auto-firing (still /name-able)
    python mode.py auto <name>...       let it fire on your phrasing
    python mode.py manual --all         everything except the forced ones

  Auto    description in context, fires on what you say        ~187 tok each
  Manual  description NOT in context, only `/name` reaches it  ~0 tok
  Off     not in ~/.claude/skills at all — paste its path      0 tok

Manual is `disable-model-invocation: true`. Two things to know before using it:

  * It blocks CLAUDE from invoking the skill, not just auto-firing. A skill
    another skill routes to (the hyperframes-* modules) breaks if set Manual,
    so those are refused unless you pass --force.
  * It only works on skills in ~/.claude/skills. Skills from extra_skill_dirs
    run from wherever they are installed or uploaded, so editing that copy
    may change nothing that fires.

Nothing is deleted and every change is one command to undo.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from scan import MACHINE_SKILLS, parse_frontmatter

FLAG = "disable-model-invocation"


def families(names: set[str]) -> set[str]:
    """Skills routed to by a parent skill — 'x-y' where 'x' is also installed."""
    return {n for n in names if any(n.startswith(p + "-") for p in names if p != n)}


def skills() -> dict[str, Path]:
    return {p.parent.name: p for p in sorted(MACHINE_SKILLS.glob("*/SKILL.md"))}


def mode_of(md: Path) -> str:
    fm = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
    return "manual" if str(fm.get(FLAG, "")).lower() == "true" else "auto"


def set_mode(md: Path, manual: bool) -> bool:
    """Add or remove the flag. Returns True if the file changed."""
    text = md.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        print(f"  ! {md.parent.name}: no frontmatter — skipped")
        return False

    end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if end is None:
        print(f"  ! {md.parent.name}: unterminated frontmatter — skipped")
        return False

    body = lines[1:end]
    has = [i for i, l in enumerate(body) if re.match(rf"^{FLAG}\s*:", l)]

    if manual and not has:
        body.append(f"{FLAG}: true\n")
    elif not manual and has:
        for i in reversed(has):
            body.pop(i)
    else:
        return False

    md.write_text("".join([lines[0], *body, *lines[end:]]), encoding="utf-8")
    return True


def apply(names: list[str], manual: bool, force: bool) -> None:
    found = skills()
    mods = families(set(found))
    verb = "manual" if manual else "auto"

    for n in names:
        if n not in found:
            print(f"  ! {n}: not in {MACHINE_SKILLS}")
            continue
        if manual and n in mods and not force:
            print(f"  ! {n}: routed to by '{n.split('-')[0]}' — Manual would break that "
                  f"router. Use --force if you are sure.")
            continue
        print(f"  {verb:>6}  {n}" if set_mode(found[n], manual) else f"  {'—':>6}  {n} (already {verb})")

    print("\nRestart your session for the change to take effect.")


def main() -> None:
    argv = [a for a in sys.argv[1:] if a not in ("--force", "--all")]
    force = "--force" in sys.argv
    every = "--all" in sys.argv
    cmd = argv[0] if argv else "list"
    names = argv[1:]

    found = skills()
    mods = families(set(found))

    if cmd == "list":
        auto = [n for n, p in found.items() if mode_of(p) == "auto"]
        man = [n for n, p in found.items() if mode_of(p) == "manual"]
        print(f"{len(found)} machine skills — {len(auto)} auto, {len(man)} manual\n")
        print("AUTO — fires on what you say")
        for n in auto:
            print(f"  {n}" + ("   [routed to by its parent — must stay auto]" if n in mods else ""))
        if man:
            print("\nMANUAL — /name only, costs nothing")
            for n in man:
                print(f"  /{n}")

    elif cmd in ("auto", "manual"):
        target = names or ([n for n in found if n not in mods] if every else [])
        if not target:
            sys.exit("name(s) required, or --all")
        apply(target, cmd == "manual", force)

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
