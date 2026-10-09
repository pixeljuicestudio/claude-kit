#!/usr/bin/env python3
"""HOOKS — the fifth inventory: what is wired to fire, and on what.

⚠️ Why this is not a duplicate of the Progs tab. Progs already lists all 19
files in ~/.claude/hooks — it scans them deliberately, and they are 19 of its
96 rows. What Progs shows is "this Python file exists and here is its
docstring". What it CANNOT show is the only thing that matters about a hook:
whether the harness will ever run it, and on what.

That distinction is not academic. `review_gate.py` sat in Progs looking
perfectly healthy while failing to fire on the commit it exists to gate. A
list of files could never have surfaced that; a list of REGISTRATIONS can.

So settings.json is the source of truth here and the disk is the reconcile —
the exact inversion of every other tab, and the reason this one is separate.
Two kinds of scope are read, because hooks come from both:

  user     ~/.claude/settings.json             — fires in every session
  project  each path in project_settings       — fires only in that project
           (kit.config.json)

Reading both immediately turned up three things a files-only view misses:

  * `narrator/hook.py` was a registered Stop hook living in ~/.claude/narrator,
    NOT in the hooks folder. It is invisible to any scan of hooks/*.py.
  * `skills_shortlist.py` was a registered SessionStart hook that lives in a
    tool folder, so it is a tool AND a hook depending on which question you
    are asking.
  * one registration is not a script at all — `git -C ~/.claude pull
    --ff-only`, a bare shell command wired to session start.

Both directions of the reconcile are findings:

  registered, file missing   the harness will try to run it and fail
  on disk, never registered  dead code that cannot fire, however good it is

⚠️ What this tab still cannot tell you: whether a registered hook ACTUALLY
RAN. The harness fires hooks on an event and nothing in the transcript names
them, which is the same blind spot Kit's live glow declares on its lamp —
`heredoc_source_guard.py` blocked a write during the session that built this
and left no trace anywhere a scanner can read. "Registered" is a statement
about wiring, never about behaviour. Anything stronger would have to come
from the hooks writing their own receipts.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import config

HOME = Path.home()
HOOKDIR = HOME / ".claude" / "hooks"
HERE = Path(__file__).resolve().parent
FAVS = HERE / "hook_favourites.json"
TAGS = HERE / "hook_tags.json"

# (label, settings file). Order matters only for display.
SCOPES = [("user", HOME / ".claude" / "settings.json")] + [
    ("project", p) for p in config.PROJECT_SETTINGS
]

# A command string is shell, so the script inside it has to be dug out. This
# catches quoted and unquoted paths, forward and back slashes, and all three
# shapes an absolute path takes: a Windows drive (C:/...), a POSIX root
# (/home/...) and the home shorthand (~/... or $HOME/...). Drive-letter-only
# matching made every hook on mac/Linux a "shell command" with no script.
_SCRIPT = re.compile(
    # The lookbehind stops `/x.py` matching INSIDE a relative `hooks/x.py`.
    r'["\']?(?<![\w.\-])((?:[A-Za-z]:[\\/]|~[\\/]|\$HOME[\\/]|\$\{HOME\}[\\/]|/)'
    r'[^"\'\s]+\.(?:py|ps1|sh))["\']?', re.I)


def _resolve_script(s: str) -> str:
    """~ and $HOME expanded, so the disk check looks at the real file."""
    for pre in ("$HOME", "${HOME}"):
        if s.startswith(pre):
            s = str(HOME) + s[len(pre):]
    return os.path.expanduser(s).replace("\\", "/")


def _summary(f: Path) -> str | None:
    """First line of the module docstring, or of the leading comment block.

    ast for Python because a regex over source finds strings that are not
    docstrings. PowerShell has no docstring, so the leading `#` comments are
    the closest honest equivalent.
    """
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if f.suffix.lower() == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text))
        except (SyntaxError, ValueError):
            doc = None
        if not doc:
            return None
        first = doc.strip().splitlines()[0].strip()
        return first or None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            got = line.lstrip("# ").strip()
            if len(got) > 8:
                return got
            continue
        break
    return None


def _lines(f: Path) -> int:
    try:
        with f.open(encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def registrations() -> tuple[list[dict], list[str]]:
    """Every hook command declared in either settings file.

    Returns (records, problems). A settings file that will not parse is a
    problem worth reporting rather than swallowing: it means NO hook in that
    scope is firing, which is a much bigger fact than any single row.
    """
    out: list[dict] = []
    problems: list[str] = []
    for scope, path in SCOPES:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems.append(f"{scope} settings would not parse ({e}) - no hook "
                            f"in that scope is firing")
            continue
        for event, groups in (data.get("hooks") or {}).items():
            if not isinstance(groups, list):
                continue
            for grp in groups:
                matcher = grp.get("matcher", "")
                for hk in grp.get("hooks", []) or []:
                    cmd = (hk.get("command") or "").strip()
                    if not cmd:
                        continue
                    m = _SCRIPT.search(cmd)
                    out.append({
                        "cmd": cmd,
                        "script": _resolve_script(m.group(1)) if m else None,
                        "event": event,
                        "matcher": matcher,
                        "scope": scope,
                    })
    return out, problems


def load_favs() -> set[str]:
    try:
        raw = json.loads(FAVS.read_text(encoding="utf-8"))
        return set(raw) if isinstance(raw, list) else set()
    except (OSError, ValueError):
        return set()


def save_favs(names, known) -> set[str]:
    clean = sorted({n for n in names if n in set(known)})
    tmp = FAVS.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    os.replace(tmp, FAVS)
    return set(clean)


def load_tags() -> dict:
    try:
        raw = json.loads(TAGS.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_tags(tags: dict, known) -> dict:
    """Keyed by the same `name` a row shows - the qualified label
    (`narrator/hook`) for a hook outside hooks/, the bare stem otherwise -
    validated against `known` exactly like save_favs above."""
    known = set(known)
    clean = {}
    for n, v in tags.items():
        if n not in known or not isinstance(v, list):
            continue
        vv = sorted({t.strip() for t in v if isinstance(t, str) and t.strip()})
        if vv:
            clean[n] = vv
    tmp = TAGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    os.replace(tmp, TAGS)
    return clean


def scan() -> tuple[list[dict], dict]:
    """Registrations first, disk second. Never touches the network."""
    regs, problems = registrations()
    favs = load_favs()
    tags = load_tags()

    # Group registrations by the script they point at. A hook registered on
    # three SessionStart matchers is ONE hook that fires three ways, not
    # three hooks — collapsing them is what makes the count honest.
    by_script: dict[str, list[dict]] = {}
    shell_only: list[dict] = []
    for r in regs:
        if r["script"]:
            by_script.setdefault(r["script"].lower(), []).append(r)
        else:
            shell_only.append(r)

    rows: list[dict] = []
    seen: set[str] = set()

    for key, group in sorted(by_script.items()):
        p = Path(group[0]["script"])
        seen.add(key)
        exists = p.is_file()
        in_dir = exists and p.parent.resolve() == HOOKDIR.resolve()
        test = HOOKDIR / f"test_{p.stem}.py"
        flags: list[str] = []
        if not exists:
            flags.append("registered, but the file is missing")
        if exists and not in_dir:
            flags.append(f"lives outside the hooks folder ({p.parent.name}/)")
        if exists and p.suffix.lower() == ".py" and not test.is_file():
            flags.append("no test")
        # A hook outside the hooks folder is named by its parent, because its
        # filename alone may say nothing: the narrator's is literally
        # `hook.py`, which renders as "hook" and identifies nothing.
        label = p.stem if in_dir or not exists else f"{p.parent.name}/{p.stem}"
        rows.append({
            "name": label,
            "file": p.name,
            "path": str(p).replace("\\", "/"),
            "exists": exists,
            "registered": True,
            "regs": [{"event": r["event"], "matcher": r["matcher"],
                      "scope": r["scope"]} for r in group],
            "events": sorted({r["event"] for r in group}),
            "scopes": sorted({r["scope"] for r in group}),
            "kind": {".ps1": "powershell", ".sh": "shell"}.get(p.suffix.lower(), "python"),
            "summary": _summary(p) if exists else None,
            "lines": _lines(p) if exists else 0,
            "test": test.name if test.is_file() else None,
            "in_hookdir": in_dir,
            "flags": flags,
            "fav": label in favs,
            "tags": tags.get(label, []),
        })

    # The other direction: a file sitting in the hooks folder that nothing
    # ever calls. Test files are excluded — they are meant not to be
    # registered, so flagging them would be noise that buries the real case.
    orphans = 0
    if HOOKDIR.is_dir():
        for f in sorted(HOOKDIR.glob("*.py")):
            if f.name.startswith("test_"):
                continue
            if str(f).replace("\\", "/").lower() in seen:
                continue
            orphans += 1
            test = HOOKDIR / f"test_{f.stem}.py"
            rows.append({
                "name": f.stem,
                "file": f.name,
                "path": str(f).replace("\\", "/"),
                "exists": True,
                "registered": False,
                "regs": [],
                "events": [],
                "scopes": [],
                "kind": "python",
                "summary": _summary(f),
                "lines": _lines(f),
                "test": test.name if test.is_file() else None,
                "in_hookdir": True,
                "flags": ["on disk, but nothing registers it - it cannot fire"],
                "fav": f.stem in favs,
                "tags": tags.get(f.stem, []),
            })

    rows.sort(key=lambda r: (not r["registered"], r["name"].lower()))

    meta = {
        "count": len(rows),
        "registered": sum(1 for r in rows if r["registered"]),
        "orphans": orphans,
        "missing": sum(1 for r in rows if not r["exists"]),
        "outside": sum(1 for r in rows if r["exists"] and not r["in_hookdir"]),
        "untested": sum(1 for r in rows
                        if r["exists"] and r["kind"] == "python" and not r["test"]),
        "shell": [{"cmd": r["cmd"], "event": r["event"], "matcher": r["matcher"],
                   "scope": r["scope"]} for r in shell_only],
        "events": sorted({e for r in rows for e in r["events"]}),
        "problems": problems,
        "hookdir": str(HOOKDIR).replace("\\", "/"),
    }
    return rows, meta


def main() -> None:
    rows, meta = scan()
    print("=== HOOKS ===")
    print(f"  {meta['registered']} registered, {meta['orphans']} on disk but never "
          f"registered, {meta['missing']} registered but missing")
    print(f"  {meta['outside']} live outside the hooks folder, "
          f"{meta['untested']} have no test")
    for p in meta["problems"]:
        print(f"  !! {p}")
    print()
    for r in rows:
        where = ", ".join(f"{x['event']}:{x['matcher'] or '*'}" for x in r["regs"])
        print(f"  {r['name']:<28} {where or '(not registered)'}")
        if r["summary"]:
            print(f"      {r['summary'][:92]}")
        if r["flags"]:
            print(f"      ! {'; '.join(r['flags'])}")
    if meta["shell"]:
        print("\n  registrations that are not a script:")
        for s in meta["shell"]:
            print(f"    {s['event']}:{s['matcher'] or '*'}  {s['cmd'][:70]}")


if __name__ == "__main__":
    main()
