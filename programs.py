#!/usr/bin/env python3
"""
Program scanner - the inventory of Python you can actually RUN.

Answers a question nothing else does: what programs exist on this drive, what
does each one do, and can I run it or is it a module something else imports.

DELIBERATELY SHALLOW. In the tools folder this was built against there were
370 .py files once you recurse; 299 of them were package internals - src/
trees, vendored modules. Listing those answers "what files exist", which nobody
asks. Listing the top level answers "what can I run", which is the question.

So: one level inside each tool (<tools_root>/<tool>/<name>.py), plus the hooks
in ~/.claude/hooks, which are programs too and are the ones most likely to be
running without anyone remembering they exist. With no tools_root configured,
only the hooks are listed.

WHY THE DOCSTRING IS THE DESCRIPTION. Nearly every tool script already opens
with one, and they are good - "Did the SessionStart compact hooks actually
fire?", "docs <-> disk drift check". That is a description written by the
person who wrote the code, next to the code, updated when the code is. Any
separate index would be a second copy to keep in sync, and this project has
already learned what happens to those.

RUNNABLE IS THE FIELD THAT EARNS ITS PLACE. A file with `if __name__ ==
"__main__"` is a program; one without is a library. Same folder, same
extension, completely different thing to a person deciding what to type.

Emits a list of dicts; build.py renders them.
"""

from __future__ import annotations

import ast
import collections
import json
import re
import sys
from pathlib import Path

import config

TOOLS: Path | None = config.TOOLS_ROOT
HOOKS = Path.home() / ".claude" / "hooks"

# Directories that hold no program worth listing. `_archive` and `_QUARANTINE`
# are deliberate graveyards; listing their contents would put dead code beside
# live code with nothing but a folder name to tell them apart.
SKIP_DIRS = {"venv", "__pycache__", "node_modules", ".git", "site-packages",
             "_archive", "_QUARANTINE", "_scratch", ".venv"}

ENTRY_RE = re.compile(r"^if\s+__name__\s*==\s*['\"]__main__['\"]", re.M)


def _skip(p: Path) -> bool:
    return any(part in SKIP_DIRS for part in p.parts)


def _flags(tree: ast.AST) -> list[str]:
    """Command-line flags, read from add_argument calls.

    Read from the AST rather than by regex over the source: a regex finds
    '--force' inside a docstring, an error message or a comment just as
    happily as in a real add_argument, and then the card promises a flag the
    program does not have.
    """
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith("-") and arg.value not in out:
                        out.append(arg.value)
    return out


def read_program(path: Path, home: str, group: str) -> dict | None:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    doc, flags, broken = None, [], False
    try:
        tree = ast.parse(src)
        doc = ast.get_docstring(tree)
        flags = _flags(tree)
    except SyntaxError:
        # A file that will not parse is a finding, not something to skip. A
        # scanner that quietly drops what it cannot read reports a clean
        # inventory of the files that happened to be fine.
        broken = True

    doc = (doc or "").strip()
    lines = src.count("\n") + 1
    name = path.stem

    return {
        "name": name,
        "file": str(path).replace("\\", "/"),
        "folder": path.parent.name,
        "home": home,                       # "tool" | "hook"
        "group": group,                     # tool folder, or "hooks"
        "summary": doc.split("\n")[0].strip() if doc else "",
        "doc": doc,
        "runnable": bool(ENTRY_RE.search(src)),
        "flags": flags,
        "is_test": name.startswith("test_"),
        "lines": lines,
        "broken": broken,
        "edited": path.stat().st_mtime,
    }


PROJECTS = Path.home() / ".claude" / "projects"
USAGE_CACHE = Path(__file__).resolve().parent / "program_usage.json"

# An INVOCATION, not a mention. `python .../check_skills.py` is a run;
# "check_skills.py came back clean" is prose about a run, and counting both
# inflates the busiest scripts most - they are the ones most written about.
#
# TWO THINGS MAKE THIS WORK, both learned by checking the matches rather than
# trusting the count:
#
# (?<![.\w]) - without it, the `py` alternative matches the EXTENSION of any
# other filename. "check_alignment.py already finds..." contains `py ` and so
# read as an interpreter; the window then ran on to the next .py in the
# sentence and credited a run to a program nobody invoked. weekly_health.py
# scored 441 runs that way, for a job that runs weekly.
#
# {0,120} and no quotes/semicolons - a real invocation puts the script right
# after the interpreter and its flags. 240 characters of anything was enough
# to jump a sentence boundary and land on an unrelated filename.
# THIRD FIX, and the one that mattered most: scan only what was EXECUTED.
# A Bash tool call records its command here. Everything else in a transcript -
# file contents returned by Read, quoted output, prose - is text about
# programs, not programs running. weekly_health.py's own docstring carries a
# "Usage:" block listing four `python weekly_health.py ...` lines, so every
# time the file was READ it scored four runs. It read 237; the real figure is
# an order of magnitude lower.
#
# This also made the mining ~15x faster, because it scans command payloads
# instead of 2 GB of prose.
CMD_RE = re.compile(rb'"command"\s*:\s*"((?:[^"\\]|\\.){0,4000})"')

# Within a command: an interpreter, its flags, then the script.
# (?<![.\w]) stops the `py` alternative matching the EXTENSION of another
# filename - "check_alignment.py already finds..." contains `py ` and read as
# an interpreter, then ran on to credit the next .py in the sentence.
RUN_RE = re.compile(
    rb"(?<![.\w])(?:python3?|py)\s+(?:-[\w]+\s+)*[\"']?"
    rb"[^\"'\n;|&]{0,120}?([A-Za-z0-9_]+)\.py\b")


def mine_usage(refresh: bool = False) -> dict:
    """{stem: {"runs": n, "last": mtime}} from the transcript corpus.

    WHAT THIS CANNOT SEE, and the card has to say so: only runs that went
    through Claude Code. A program launched from a terminal, a .bat, or the
    Windows scheduler leaves no trace here. A job scheduled every Monday may
    still read as never-run - so this is "runs I can see", never "runs".
    A count presented without that caveat would be read as the second.
    """
    if not refresh:
        try:
            return json.loads(USAGE_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    counts: dict[str, int] = {}
    last: dict[str, float] = {}
    files = list(PROJECTS.rglob("*.jsonl")) if PROJECTS.is_dir() else []
    for f in files:
        try:
            blob = f.read_bytes()
            mtime = f.stat().st_mtime
        except OSError:
            continue
        for cmd in CMD_RE.findall(blob):
            # One command counts once per distinct program, however many times
            # it names it. `python x.py && python x.py` is one decision to run
            # x, and a loop written into a single command is not 40 uses.
            for stem in {m.decode("utf-8", "replace") for m in RUN_RE.findall(cmd)}:
                counts[stem] = counts.get(stem, 0) + 1
                if mtime > last.get(stem, 0):
                    last[stem] = mtime

    data = {"counts": counts, "last": last, "scanned": len(files)}
    try:
        USAGE_CACHE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


def attach_usage(items: list[dict], data: dict) -> None:
    """Set runs / last_run / ago. Keyed on the STEM, so two programs sharing a
    filename in different tools share a count - a `feed.py` can exist in two
    tools at once. The command line rarely carries enough
    path to tell them apart, so the count is attributed to both rather than
    guessed at, and the card says the number is a floor."""
    import time as _t
    counts, last = data.get("counts", {}), data.get("last", {})
    stems = collections.Counter(i["name"] for i in items)
    now = _t.time()
    for i in items:
        i["runs"] = counts.get(i["name"], 0)
        ts = last.get(i["name"])
        i["last_run"] = ts
        i["ago"] = int((now - ts) / 86400) if ts else None
        # True when another program shares this filename, so the count cannot
        # be attributed cleanly. Shown on the card rather than silently ignored.
        i["name_shared"] = stems[i["name"]] > 1


def link_skills(items: list[dict], skills: list[dict]) -> None:
    """Set item["skills"] - the skills whose SKILL.md NAMES this program.

    "Named in", not "used by", and the wording matters. `health-check` names
    `notion_move_blocks.py` only to record that it was SUPERSEDED, so calling
    that a usage would be false. Naming is what the evidence supports; whether
    the mention is an endorsement is a judgement no matcher can make.

    THE DIRECTORY GUARD IS THE WHOLE TRICK. A bare filename match alone was
    wrong 3 times in 8 on the first run:
      - resolve-conform names `.../otio-conform/tools/preflight.py`, a
        different file that happens to share a name with another tool's
      - another skill names `clients/trim.py`, a hypothetical example, not
        the launcher's own trim.py
    So a match is rejected when the mention carries a directory prefix that is
    not this program's own folder. Anchoring on the FULL path instead would be
    safer still, but it also drops real links: adversarial-review names
    `review_gate.py` with no path at all, and that link is true.

    Also matched: a skill whose name IS the program's, with underscores as
    hyphens - `spend_gate.py` is the mechanical half of the `spend-gate` skill.
    """
    texts: dict[str, str] = {}
    for s in skills:
        f = s.get("file")
        if not f:
            continue
        try:
            texts[s["name"]] = Path(f).read_text(
                encoding="utf-8", errors="replace").replace("\\", "/")
        except OSError:
            continue

    for p in items:
        found: set[str] = set()
        rx = re.compile(r"(?:([A-Za-z0-9_.\-]+)/)?" + re.escape(p["name"] + ".py"),
                        re.I)
        own = p["folder"].lower()
        for sname, text in texts.items():
            for m in rx.finditer(text):
                d = (m.group(1) or "").lower()
                if d and d != own:
                    continue                    # a path to a different file
                found.add(sname)
                break
        alt = p["name"].replace("_", "-")
        if alt in texts:
            found.add(alt)
        p["skills"] = sorted(found)


def _git_repos(root: Path) -> set[str]:
    return {p.parent.name for p in root.glob("*/.git")}


def scan() -> tuple[list[dict], dict]:
    items: list[dict] = []

    if TOOLS is not None and TOOLS.is_dir():
        repos = _git_repos(TOOLS)
        for py in sorted(TOOLS.glob("*/*.py")):
            if _skip(py.relative_to(TOOLS)):
                continue
            rec = read_program(py, "tool", py.parent.name)
            if rec:
                rec["repo"] = rec["group"] in repos
                items.append(rec)

    if HOOKS.is_dir():
        for py in sorted(HOOKS.glob("*.py")):
            if _skip(py.relative_to(HOOKS)):
                continue
            rec = read_program(py, "hook", "hooks")
            if rec:
                rec["repo"] = True          # ~/.claude is a repo
                items.append(rec)

    # Pair each program with its test. "Has a test" is the single most useful
    # thing to know about a script in a system whose recurring lesson is that a
    # passing test is not evidence until you have watched it fail - and the
    # absence of one is worth seeing at a glance.
    by_group: dict[tuple, set] = {}
    for i in items:
        by_group.setdefault(i["group"], set()).add(i["name"])
    for i in items:
        i["tested"] = ("test_" + i["name"]) in by_group.get(i["group"], set())
        i["tests"] = i["name"][5:] if i["is_test"] else None
        i.setdefault("skills", [])      # filled by link_skills when skills are known

    meta = {
        "roots": {"tools": str(TOOLS).replace("\\", "/") if TOOLS else None,
                  "hooks": str(HOOKS).replace("\\", "/")},
        "tools_configured": TOOLS is not None,
        "groups": sorted({i["group"] for i in items}),
    }
    return items, meta


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Inventory of runnable Python.")
    ap.add_argument("--json", metavar="PATH", help="write the inventory as JSON")
    ap.add_argument("--quiet", action="store_true", help="counts only")
    args = ap.parse_args()

    items, meta = scan()
    runnable = [i for i in items if i["runnable"] and not i["is_test"]]
    tests = [i for i in items if i["is_test"]]
    libs = [i for i in items if not i["runnable"] and not i["is_test"]]
    nodoc = [i for i in items if not i["summary"] and not i["is_test"]]
    broken = [i for i in items if i["broken"]]

    print("PROGRAMS - %d across %d groups" % (len(items), len(meta["groups"])))
    print("  runnable %d | library %d | tests %d" % (
        len(runnable), len(libs), len(tests)))
    print("  with a summary: %d of %d non-test files" % (
        len(items) - len(tests) - len(nodoc), len(items) - len(tests)))
    covered = [i for i in items if i["tested"]]
    print("  have a test_ alongside: %d" % len(covered))

    if broken:
        print("\nWILL NOT PARSE (%d) - reported, never skipped" % len(broken))
        for i in broken:
            print("   %s" % i["file"])
    if nodoc and not args.quiet:
        print("\nNO DOCSTRING (%d) - these are the only ones needing a human"
              % len(nodoc))
        for i in nodoc:
            print("   %-28s %s" % (i["name"], i["file"]))

    if not args.quiet:
        print()
        for g in meta["groups"]:
            rows = [i for i in items if i["group"] == g]
            print("%s (%d)" % (g, len(rows)))
            for i in sorted(rows, key=lambda x: x["name"]):
                mark = "*" if i["runnable"] else " "
                print("  %s %-30s %s" % (mark, i["name"], i["summary"][:72]))

    if args.json:
        Path(args.json).write_text(
            json.dumps({"items": items, "meta": meta}, indent=1),
            encoding="utf-8")
        print("\nwrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
