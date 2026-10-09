#!/usr/bin/env python3
"""TOOLS — the fourth inventory: what is in your tools folder, and how you start it.

The other inventories have a rule. Skills are "a SKILL.md exists". Programs
are "a .py one level inside a tool". Prompts are "a file in prompts/". Tools
have no rule: in the folder this was built against, every automatic
definition disagreed with every other one (the folders with a README, the
git repos and the documented ones were three different sets).

So the roster is EXPLICIT — a hand-ticked list in tools.json (copy
tools.example.json). It is not a rule and does not pretend to be. The working
definition is "things I would want to find again", which no scan can derive.

⚠️ An explicit roster is hand-maintained data, and hand-maintained data
drifts. The escape valve is that scan() also reports every directory under
tools_root that is neither rostered nor listed in "not_tools", and the tab
shows that count. A new tool cannot hide; it shows up as "3 unlisted" and
someone decides. The check must be able to reach zero, or it becomes
wallpaper.

With no tools_root configured, scan() returns nothing and the tab says so.

Everything else on a card is measured, never typed:

  description   the first real line of README.md / CLAUDE.md / START_HERE.md.
                No file, no description — the card says so rather than
                inventing one.
  documented    optional: derived from a manifest JSON
                (<tools_root>/_engine/alignment_manifest.json, shaped
                {"pointers": [{"path", "source"}]}) naming the page that
                references the folder. Absent manifest = column n/a.
  shortcut      Windows only: Desktop .lnk files, matched by scanning the
                shortcut's bytes for the folder name. A .lnk stores its target
                as a plain string in both ASCII and UTF-16 and this needs no COM.
  scheduled     Windows only: C:/Windows/System32/Tasks — plain XML, readable
                without admin and far faster than Get-ScheduledTask.
  git           last commit date, whether a remote exists, whether anything
                is uncommitted. Read from .git/config plus two cheap calls.

The primary facet is HOW YOU START IT — desktop icon, scheduled, command
line, or nothing to run. That is a fact about the folder and it answers the
question actually being asked at the moment someone opens this tab. Grouping
by "what it does" would be inference, and the Prompts tab already established
that a category has to be a lookup, not a guess.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import config

ROOT: Path | None = config.TOOLS_ROOT
HERE = Path(__file__).resolve().parent
FAVS = HERE / "tool_favourites.json"
TAGS = HERE / "tool_tags.json"
ROSTER_FILE = HERE / "tools.json"
MANIFEST = (ROOT / "_engine" / "alignment_manifest.json") if ROOT else None
DESKTOP = Path.home() / "Desktop"
# Scheduled-task and .lnk facets are Windows facts; elsewhere they report n/a.
IS_WINDOWS = sys.platform == "win32"
TASKDIR = Path("C:/Windows/System32/Tasks") if IS_WINDOWS else None


def load_roster(path: Path | None = None) -> tuple[list[str], set[str]]:
    """(roster, not_tools) from tools.json.

    Shape: {"roster": ["folder", ...], "not_tools": ["folder", ...]}. A bare
    list is accepted as the roster. Missing or unreadable -> ([], set()).

    roster     folders under tools_root that ARE tools - ticked by hand.
    not_tools  folders deliberately rejected, so they do not count as
               "unlisted". Otherwise the drift counter never reaches zero and
               stops being read.
    """
    try:
        raw = json.loads((path or ROSTER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], set()
    if isinstance(raw, list):
        raw = {"roster": raw}
    if not isinstance(raw, dict):
        return [], set()
    roster = [x for x in raw.get("roster") or [] if isinstance(x, str) and x.strip()]
    rejected = {x for x in raw.get("not_tools") or [] if isinstance(x, str) and x.strip()}
    return roster, rejected


# See the module docstring for why this is a list and not a rule.
ROSTER, NOT_TOOLS = load_roster()

ENTRY_EXT = {".py", ".bat", ".ps1", ".cmd", ".exe", ".sh"}
DESC_FILES = ["README.md", "README.txt", "README", "CLAUDE.md", "START_HERE.md"]
DESC_LIMIT = 240
STALE_DAYS = 120

# A heading, a badge line, a bare title — none of these describe anything.
_NOISE = re.compile(r"^\s*(?:#{1,6}\s|>\s|!\[|\[!\[|---|===|\*\*?\s*$|```)")
# Nor does a language switcher, a table row, or a shields.io strip. premiere-mcp
# is a vendored fork whose README opens with "English | 中文 | Tiếng Việt | …",
# which passed every check above and rendered as the tool's description.
_BAR = re.compile(r"\|")


def _is_prose(line: str) -> bool:
    """Does this line read like a sentence about the tool, or like furniture?

    Two cheap tests, both learned from real output: at least four word-ish
    tokens (a parenthetical aside such as reel-forge's `(formerly "Explainer
    Pipeline")` is not a description), and mostly letters (a pipe-separated
    language switcher is not either).
    """
    if len(_BAR.findall(line)) >= 2:
        return False
    words = [w for w in re.split(r"\s+", line) if len(w) >= 2]
    if len(words) < 4:
        return False
    letters = sum(c.isalpha() or c.isspace() for c in line)
    return letters / max(1, len(line)) > 0.65


_LAUNCHY = ("launch", "start", "run", "open")


def _launcher(entries: list[str], name: str) -> str | None:
    """The one entry Kit may offer a button for, or None if it is ambiguous.

    Only .bat/.cmd/.exe: those exist to be double-clicked and carry their own
    environment, whereas a .py needs the right venv and a .ps1 needs an
    execution policy, so neither is safe to fire blind from a web page.

    AN UNCLEAR CHOICE MUST RETURN None, and that is the whole point of this
    function. Taking the first .bat alphabetically nominated a one-off
    "play this project" script for one tool and a client preview for another -
    both a project someone once ran, not the way you start the tool. A wrong button is
    worse than no button: no button leaves the command chip telling the truth,
    a wrong one runs something you did not ask for. So a tool with several
    candidates and nothing to distinguish them gets nothing.
    """
    cands = [e for e in entries if Path(e).suffix.lower() in {".bat", ".cmd", ".exe"}]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    flat = re.sub(r"[^a-z0-9]", "", name.lower())
    named = [e for e in cands
             if re.sub(r"[^a-z0-9]", "", Path(e).stem.lower()) in (flat, flat + "bat")]
    if len(named) == 1:
        return named[0]
    verbs = [e for e in cands
             if any(w in Path(e).stem.lower() for w in _LAUNCHY)]
    return verbs[0] if len(verbs) == 1 else None


def say(msg: str) -> None:
    """Console-safe: a tool name with an arrow in it killed a run once."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout.write(msg.encode(enc, "replace").decode(enc, "replace") + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------
# description
# --------------------------------------------------------------------------
def describe(d: Path) -> tuple[str | None, str | None]:
    """First real sentence of the first description file that has one.

    Returns (text, filename). No file or no usable line returns (None, None)
    — the card then says "nothing describes this", which is a true statement
    about the folder. Inventing a description from the folder name is the
    trap the Prompts parser fell into: a plausible wrong answer renders
    perfectly and nobody looks again.
    """
    for fname in DESC_FILES:
        f = d / fname
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or _NOISE.match(line):
                continue
            # Strip inline markdown emphasis and links, keep the words.
            line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
            line = re.sub(r"[*_`]+", "", line).strip()
            if len(line) < 12 or not _is_prose(line):
                continue
            if len(line) > DESC_LIMIT:
                cut = line[:DESC_LIMIT].rsplit(" ", 1)[0]
                line = cut + "\u2026"
            return line, fname
    return None, None


# --------------------------------------------------------------------------
# git — cheap, because this runs on every page build
# --------------------------------------------------------------------------
def _git(d: Path, *args) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(d), capture_output=True,
                           text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def git_state(d: Path) -> dict:
    if not (d / ".git").exists():
        return {"git": False, "remote": "", "commit": "", "dirty": False}
    # The remote comes out of .git/config directly — no subprocess needed for
    # something that is four lines of ini.
    remote = ""
    cfg = d / ".git" / "config"
    if cfg.is_file():
        try:
            m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)',
                          cfg.read_text(encoding="utf-8", errors="replace"), re.S)
            remote = m.group(1) if m else ""
        except OSError:
            pass
    commit = _git(d, "log", "-1", "--format=%cs")
    # --untracked-files=no: a build artefact nobody has ignored yet is not
    # "uncommitted work", and counting it as such makes the flag noise.
    dirty = bool(_git(d, "status", "--porcelain", "--untracked-files=no"))
    return {"git": True, "remote": remote, "commit": commit, "dirty": dirty}


# --------------------------------------------------------------------------
# reachability — shortcuts and scheduled tasks
# --------------------------------------------------------------------------
def _views(blob: bytes) -> list[str]:
    """The same bytes as text, both ways a Windows binary stores a path.

    latin-1 never fails and preserves byte-per-char, which is enough to find
    ASCII paths. UTF-16 wide strings are tried at both byte alignments because
    the string's offset inside the file is arbitrary.
    """
    return [
        blob.decode("latin-1", "replace"),
        blob.decode("utf-16-le", "replace"),
        blob[1:].decode("utf-16-le", "replace"),
    ]


def _mentions(text: str, name: str) -> bool:
    """Is `name` a whole PATH SEGMENT in text, not merely a substring?

    ⚠️ This is why the check is not `name in text`. `hyperframes` is a prefix
    of `hyperframes-tools`, so a plain substring test credited the folder
    `hyperframes` with the desktop icon that actually launches
    `hyperframes-tools` — a wrong fact, rendered confidently on a card, of
    exactly the kind the Prompts parser produced. The separator before and
    the negative lookahead after make the match a segment.
    """
    pat = r"[\\/]" + re.escape(name) + r"(?![A-Za-z0-9_.\-])"
    return re.search(pat, text, re.I) is not None


def desktop_shortcuts() -> dict[str, list[str]]:
    """folder name -> [shortcut names] by scanning .lnk bytes.

    A .lnk stores its target path as a literal string, so reading the bytes
    finds it without COM or PowerShell. Matching is segment-wise; see
    _mentions for the collision this avoids.
    """
    out: dict[str, list[str]] = {}
    if not IS_WINDOWS or not DESKTOP.is_dir():
        return out
    for lnk in DESKTOP.glob("*.lnk"):
        try:
            views = _views(lnk.read_bytes())
        except OSError:
            continue
        for name in ROSTER:
            if any(_mentions(v, name) for v in views):
                out.setdefault(name, []).append(lnk.stem)
    return out


def scheduled_tasks() -> dict[str, list[str]]:
    """folder name -> [task names], read from the Tasks XML tree.

    Get-ScheduledTask can take ~40 seconds, which is far too slow for
    something that runs on every page build. The Tasks directory is plain
    XML and readable without admin, so it is read directly. Tasks the user
    cannot read are skipped rather than raising — a permission error must not
    be the reason the page fails to build. Windows only; elsewhere, empty.
    """
    out: dict[str, list[str]] = {}
    if TASKDIR is None or ROOT is None or not TASKDIR.is_dir():
        return out
    # Only tasks that mention the tools root at all are worth a segment match.
    root_views = {str(ROOT).upper(), str(ROOT).replace("\\", "/").upper()}
    for f in TASKDIR.rglob("*"):
        if not f.is_file():
            continue
        try:
            blob = f.read_bytes()
        except OSError:
            continue
        views = _views(blob)
        if not any(r in v.upper() for v in views for r in root_views):
            continue
        for name in ROSTER:
            if any(_mentions(v, name) for v in views):
                out.setdefault(name, []).append(f.name)
    return out


# --------------------------------------------------------------------------
# notion — derived, never typed
# --------------------------------------------------------------------------
def notion_refs() -> tuple[dict[str, list[str]], bool, str]:
    """folder name -> [documentation pages that reference it].

    Optional. Source is a manifest that some external checker builds by
    harvesting every path-valued reference in your docs. Entries marked
    "narrative" (logs, postmortems) are excluded because those cite paths as
    HISTORY — being mentioned in a postmortem is not being documented.
    No manifest -> ({}, False, "") and the column reads n/a.
    """
    if MANIFEST is None or ROOT is None:
        return {}, False, ""
    try:
        raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, False, ""
    out: dict[str, list[str]] = {}
    root = str(ROOT).replace("\\", "/").rstrip("/").lower()
    for p in raw.get("pointers", []):
        if p.get("narrative"):
            continue
        path = str(p.get("path", "")).replace("\\", "/").lower()
        if not path.startswith(root + "/"):
            continue
        seg = path[len(root) + 1:].split("/")[0]
        src = str(p.get("source", "")).strip()
        for name in ROSTER:
            if name.lower() == seg and src and src not in out.setdefault(name, []):
                out[name].append(src)
    return out, True, str(raw.get("generated", ""))


# --------------------------------------------------------------------------
# favourites
# --------------------------------------------------------------------------
def load_favs() -> set[str]:
    try:
        raw = json.loads(FAVS.read_text(encoding="utf-8"))
        return set(raw) if isinstance(raw, list) else set()
    except (OSError, ValueError):
        return set()


def save_favs(names) -> set[str]:
    """Validated against the ROSTER, then written atomically."""
    clean = sorted({n for n in names if n in set(ROSTER)})
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


def save_tags(tags: dict) -> dict:
    """Same shape as the skills tab's tags.json - a separate file, because
    /api/ttag validates every name against the ROSTER, not the skill scan."""
    clean = {}
    for n, v in tags.items():
        if n not in set(ROSTER) or not isinstance(v, list):
            continue
        vv = sorted({t.strip() for t in v if isinstance(t, str) and t.strip()})
        if vv:
            clean[n] = vv
    tmp = TAGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    os.replace(tmp, TAGS)
    return clean


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------
def _touched(d: Path) -> str | None:
    """Newest top-level entry, ignoring .git.

    .git is excluded deliberately: reading a repo's log touches it, so a
    scanner that counted .git would report that every repo changed the moment
    it looked. That is a measuring tool manufacturing its own finding, which
    this repo has already been burned by once (guard.py v1, 97 phantom
    regressions).
    """
    best = 0.0
    try:
        for k in d.iterdir():
            if k.name == ".git":
                continue
            try:
                best = max(best, k.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return None
    if not best:
        return None
    return datetime.fromtimestamp(best, timezone.utc).strftime("%Y-%m-%d")


def _age_days(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        d = datetime.strptime(stamp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - d).days)


def scan() -> tuple[list[dict], dict]:
    """Read the roster off disk. Never touches the network."""
    if ROOT is None:
        return [], {
            "configured": False, "count": 0, "roster": len(ROSTER), "missing": [],
            "unlisted": [], "rejected": len(NOT_TOOLS), "notion_ok": False,
            "notion_at": "", "root": None, "windows": IS_WINDOWS,
        }
    shortcuts = desktop_shortcuts()
    tasks = scheduled_tasks()
    refs, refs_ok, refs_at = notion_refs()
    favs = load_favs()
    tags = load_tags()

    rows: list[dict] = []
    missing: list[str] = []

    for name in ROSTER:
        d = ROOT / name
        if not d.is_dir():
            # A rostered folder that is gone is a finding, not a silent skip.
            missing.append(name)
            continue

        try:
            kids = list(d.iterdir())
        except OSError:
            kids = []
        files = [k for k in kids if k.is_file()]
        subs = [k for k in kids if k.is_dir() and k.name != ".git"]
        entries = sorted(k.name for k in files if k.suffix.lower() in ENTRY_EXT)

        desc, desc_from = describe(d)
        g = git_state(d)
        lnks = sorted(shortcuts.get(name, []))
        tks = sorted(tasks.get(name, []))
        pages = sorted(refs.get(name, []))
        touched = _touched(d)

        # How you start it. Ordered by immediacy: an icon beats a schedule
        # beats a command line, because that is the order you would reach
        # for them.
        if lnks:
            start = "icon"
        elif tks:
            start = "scheduled"
        elif entries:
            start = "command"
        else:
            start = "none"

        launch = _launcher(entries, name)

        # Freshness: a repo's real age is its last COMMIT; a plain folder's
        # is its newest file. Mixing the two would rank repos as stale purely
        # for being tidy.
        asof = g["commit"] if g["git"] and g["commit"] else touched
        age = _age_days(asof)

        flags: list[str] = []
        if not desc:
            flags.append("nothing describes this")
        if not g["git"]:
            flags.append("not version-controlled")
        elif not g["remote"]:
            flags.append("no remote — only on this machine")
        if g["git"] and g["dirty"]:
            flags.append("uncommitted work")
        if start == "none":
            flags.append("nothing to run")
        if not pages and refs_ok:
            flags.append("not documented")
        if age is not None and age > STALE_DAYS:
            flags.append(f"untouched for {age} days")

        rows.append({
            "name": name,
            "path": str(d).replace("\\", "/"),
            "desc": desc,
            "desc_from": desc_from,
            "start": start,
            "launch": launch,
            "shortcuts": lnks,
            "tasks": tks,
            "entries": entries[:8],
            "n_entries": len(entries),
            "n_files": len(files),
            "n_subdirs": len(subs),
            "git": g["git"],
            "remote": g["remote"],
            "commit": g["commit"],
            "dirty": g["dirty"],
            "readme": desc_from if desc_from and desc_from.lower().startswith("readme") else None,
            "notion": pages,
            "touched": touched,
            "asof": asof,
            "age": age,
            "flags": flags,
            "fav": name in favs,
            "tags": tags.get(name, []),
        })

    # The drift valve. Anything on disk that is neither rostered nor
    # explicitly rejected is surfaced by count, so a new tool announces
    # itself instead of quietly never appearing.
    unlisted: list[str] = []
    try:
        for k in sorted(ROOT.iterdir()):
            if k.is_dir() and k.name not in set(ROSTER) and k.name not in NOT_TOOLS:
                unlisted.append(k.name)
    except OSError:
        pass

    meta = {
        "configured": True,
        "windows": IS_WINDOWS,
        "count": len(rows),
        "roster": len(ROSTER),
        "missing": missing,
        "unlisted": unlisted,
        "rejected": len(NOT_TOOLS),
        "notion_ok": refs_ok,
        "notion_at": refs_at,
        "root": str(ROOT).replace("\\", "/"),
    }
    return rows, meta


def main() -> None:
    rows, meta = scan()
    say("=== TOOLS ===")
    if not meta["configured"]:
        say("  not configured - set tools_root in kit.config.json and list your")
        say("  tools in tools.json (see tools.example.json)")
        return
    say(f"  {meta['count']} of {meta['roster']} rostered folders found")
    if meta["missing"]:
        say(f"  MISSING from disk: {', '.join(meta['missing'])}")
    if meta["unlisted"]:
        say(f"  unlisted on disk : {', '.join(meta['unlisted'])}")
    if not meta["notion_ok"]:
        say("  (no alignment manifest - documented column unavailable)")
    say("")
    for r in rows:
        bits = [r["start"]]
        if r["git"]:
            bits.append("git " + (r["commit"] or "?"))
        if r["notion"]:
            bits.append(f"notion x{len(r['notion'])}")
        say(f"  {r['name']:<22} {'  '.join(bits)}")
        if r["desc"]:
            say(f"      {r['desc'][:96]}")
        else:
            say("      (nothing describes this)")
        if r["flags"]:
            say(f"      ! {'; '.join(r['flags'])}")


if __name__ == "__main__":
    main()
