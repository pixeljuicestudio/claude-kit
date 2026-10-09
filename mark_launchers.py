#!/usr/bin/env python3
"""Add a Kit activity marker to the .bat launchers, so starting a tool the
NORMAL way lights its card.

The problem this closes. Kit's live glow reads Claude Code transcripts, which
means it can only ever see a Python file run through Claude Code. Many tools
are started by DESKTOP ICON, and a .bat, an icon, a terminal or Task Scheduler
writes nothing to a transcript at all - verified, RUN_RE matches no .bat
invocation in any form. So the way the user actually starts most tools was
invisible to the very feature built to show it.

Windows-only in practice: it patches .bat launchers. The marker directory is
runs_dir from kit.config.json (default: .runs/ beside this file).

The fix is three lines at the top of each launcher: make the tool announce
itself. It is deliberately the dumbest thing that works.

  mkdir the marker directory     - self-healing if it is ever deleted
  overwrite <Tool>.run           - one tiny file per tool, never grows
  the MTIME is the signal        - no timestamp is written, so there is no
                                   locale format and nothing to parse. A
                                   Windows .bat cannot print an ISO date
                                   without help, and %DATE% is whatever the
                                   machine's locale says it is.

Everything is guarded with 2>nul. A marker that cannot be written must never
be the reason a tool fails to start - the tool is the point, the light is not.

Idempotent: run it twice and the second run changes nothing. Every insert is
labelled and says it is safe to delete, because these are the user's working
launchers and a future reader deserves to know what put a Kit line in them.

  python mark_launchers.py            # dry run, prints what it would do
  python mark_launchers.py --apply    # write, after backing each file up
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import config

ROOT: Path | None = config.TOOLS_ROOT
RUNS = config.RUNS_DIR
MARK = "Kit activity marker"

# ⚠️ The first version of this list was the .bat files behind a DESKTOP
# SHORTCUT, and that was the wrong rule. A bridge launched from its own
# launch-bridge.bat had no desktop icon, so it was never patched and never
# lit. Scoping to "has a shortcut" quietly answered a different question from
# "how do you start this".
#
# The rule is now: EVERY .bat at the top level of a rostered tool folder, plus
# any nested launcher listed under "nested_launchers" in tools.json
# ({"tool/sub/launcher.bat": "tool"}). That catches maintenance scripts too
# (commit.bat, backup.bat), and that is accepted deliberately: "a batch file
# in this tool ran" is a true statement about the tool being used, and drawing
# a line between "launching" and "using" would mean guessing at filenames.
def _nested() -> dict[str, str]:
    import json
    import tools as toollib
    try:
        raw = json.loads(toollib.ROSTER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    got = raw.get("nested_launchers") if isinstance(raw, dict) else None
    return {k: v for k, v in (got or {}).items()
            if isinstance(k, str) and isinstance(v, str)} if isinstance(got, dict) else {}


def launchers() -> dict[str, str]:
    """{relative .bat path: tool name}, derived from the roster, not typed."""
    import tools as toollib
    if ROOT is None:
        return {}
    out: dict[str, str] = _nested()
    for name in toollib.ROSTER:
        d = ROOT / name
        if not d.is_dir():
            continue
        try:
            for b in sorted(d.glob("*.bat")):
                out[f"{name}/{b.name}"] = name
        except OSError:
            continue
    return out


def block(tool: str) -> list[str]:
    runs = str(RUNS).replace("/", "\\")
    return [
        f"REM --- {MARK}: lets Kit's Tools tab light up when you launch this.",
        "REM     Writes one empty file whose timestamp is the signal. Safe to delete.",
        f'mkdir "{runs}" 2>nul',
        f'echo.>"{runs}\\{tool}.run" 2>nul',
        "",
    ]


def read_lines(path: Path) -> tuple[list[str], str, bool]:
    """(lines, the file's own line ending, did it end with one).

    ⚠️ Preserving the ending is not cosmetic. The first version of this script
    wrote "\\r\\n".join(...) unconditionally, which silently converted every
    LF launcher to CRLF - 20 of the 25. It went unnoticed because the
    verification stripped \\r before comparing, so it checked CONTENT and
    called it "original preserved". The round-trip test caught it only
    because that one compares BYTES.

    Editing a file you were asked to add a line to must not also rewrite every
    other line in it.
    """
    blob = path.read_bytes()
    eol = "\r\n" if b"\r\n" in blob else "\n"
    text = blob.decode("utf-8", errors="replace")
    return text.splitlines(), eol, text.endswith(("\n", "\r"))


def write_lines(path: Path, lines: list[str], eol: str, trailing: bool) -> None:
    out = eol.join(lines) + (eol if trailing else "")
    path.write_bytes(out.encode("utf-8"))


def patch(path: Path, tool: str, apply: bool) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"UNREADABLE  {e}"
    if MARK in raw:
        return "already marked"

    lines, eol, trailing = read_lines(path)
    # Insert after @echo off, which every one of these opens with. If a file
    # ever does not, insert at the top rather than guessing at its structure.
    at = 0
    for i, ln in enumerate(lines[:3]):
        if ln.strip().lower().startswith("@echo off"):
            at = i + 1
            break
    new = lines[:at] + block(tool) + lines[at:]

    if not apply:
        return f"would insert 5 lines at line {at + 1}  (tool: {tool})"

    bak = path.with_suffix(path.suffix + ".bak-kit")
    if not bak.exists():
        shutil.copy2(path, bak)
    write_lines(path, new, eol, trailing)
    return f"patched at line {at + 1} ({'CRLF' if eol == chr(13)+chr(10) else 'LF'})"


def unpatch(path: Path, apply: bool) -> str:
    """Remove the marker block, exactly and only.

    Exists so the .bak-kit files can be deleted. A backup is an ARTEFACT that
    has to be kept, found and trusted; a reversible operation is a mechanism.
    Some launchers live in folders with no version control at all, so their
    backup was the only copy of the original and this is what replaces it.

    Verified by round-trip before the backups were removed: patch, unpatch,
    compare to the backup byte for byte.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"UNREADABLE  {e}"
    if MARK not in raw:
        return "not marked"

    lines, eol, trailing = read_lines(path)
    out, i, cut = [], 0, 0
    while i < len(lines):
        if MARK in lines[i]:
            # The block is exactly: two REM lines, mkdir, echo, blank.
            i += 5
            cut += 5
            continue
        out.append(lines[i])
        i += 1
    if not apply:
        return f"would remove {cut} lines"
    write_lines(path, out, eol, trailing)
    return f"removed {cut} lines"


def main() -> None:
    apply = "--apply" in sys.argv
    if ROOT is None:
        print("tools_root is not configured in kit.config.json - nothing to mark.")
        return
    if "--unmark" in sys.argv:
        print("=== %s ===" % ("UNMARKING" if apply else "DRY RUN - nothing written"))
        for rel in sorted(launchers()):
            p = ROOT / rel
            if p.is_file():
                print("  %-46s %s" % (rel, unpatch(p, apply)))
        return
    print("=== %s ===" % ("APPLYING" if apply else "DRY RUN - nothing written"))
    if apply:
        RUNS.mkdir(parents=True, exist_ok=True)
        print("marker dir: %s" % RUNS)
    print()
    missing = 0
    for rel, tool in sorted(launchers().items()):
        p = ROOT / rel
        if not p.is_file():
            print("  %-46s MISSING" % rel)
            missing += 1
            continue
        print("  %-46s %s" % (rel, patch(p, tool, apply)))
    print()
    if missing:
        print("!! %d launcher(s) not found - the roster and the disk disagree" % missing)
    if not apply:
        print("Re-run with --apply to write. Each file is backed up to .bak-kit first.")


if __name__ == "__main__":
    main()
