#!/usr/bin/env python3
"""Selftest for tools.py - the matcher, the roster, and the drift valve.

The risky part of this module is _mentions, and it has already been wrong
once. The first version asked `name in blob`, which credited the folder
`hyperframes` with the desktop icon that actually launches `hyperframes-tools`
- because one name is a prefix of the other. It rendered as a confident
"Desktop icon" chip on the wrong card, and nothing about the page looked
broken. Same failure shape as the Prompts parser: a plausible wrong answer
that survives every check a human makes by eye.

The second risk is the roster. It is hand-maintained by design, which means
it drifts by design, which means the ONLY thing standing between it and
quiet rot is the unlisted count. If that valve stops working, the tab keeps
rendering the same tools for ever while the folder grows.

Roster and drift checks run against a temporary tools folder and tools.json,
so the suite does not depend on what this machine has configured.

Run: python test_tools.py      Exit 0 = every invariant holds.
"""
import inspect
import json
import sys
import tempfile
from pathlib import Path

import tools

FAILS = []


def check(label, ok, detail=""):
    print("%-5s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILS.append("%s: %s" % (label, detail))


# ------------------------------------------------------- the prefix collision
# THE test. Everything else in this file is hygiene; this is the bug.
ICON = r"C:\x00D:\work\tools\hyperframes-tools\hf-project-picker.bat"
check("prefix does NOT match a longer sibling",
      not tools._mentions(ICON, "hyperframes"), ICON)
check("the longer sibling matches itself",
      tools._mentions(ICON, "hyperframes-tools"))
check("a real segment matches",
      tools._mentions(r"D:\work\tools\hyperframes\index.html", "hyperframes"))
check("forward slashes match too",
      tools._mentions("/home/user/tools/resolve/launch.sh", "resolve"))
check("matching is case-insensitive",
      tools._mentions(r"d:\work\tools\PinVault\x", "pinvault"))
check("a trailing segment with no separator after still matches",
      tools._mentions(r"D:\work\tools\reel-forge", "reel-forge"))
check("an underscore suffix does not match",
      not tools._mentions(r"D:\work\tools\resolve_old\x", "resolve"))
check("a dot suffix does not match",
      not tools._mentions(r"D:\work\tools\resolve.bak\x", "resolve"))
check("a bare mention with no separator does not match",
      not tools._mentions("see the resolve folder for details", "resolve"))

# Both byte views have to be searched, or wide-string targets go unseen.
blob = ("x" + r"D:\work\tools\PinVault\index.html").encode("utf-16-le")
check("utf-16 targets are found in one of the views",
      any(tools._mentions(v, "PinVault") for v in tools._views(blob)))
check("_views never raises on arbitrary bytes",
      len(tools._views(bytes(range(256)))) == 3)

# --------------------------------------------------------------- description
check("a language switcher is not prose",
      not tools._is_prose("English | 中文 | Tiếng Việt | Deutsch | Español"))
check("a short parenthetical is not prose",
      not tools._is_prose('(formerly "Explainer Pipeline")'))
check("a real sentence is prose",
      tools._is_prose("An MCP server that lets any assistant drive DaVinci Resolve."))
check("a path-heavy line is not prose",
      not tools._is_prose("D:/A/B/C/D/E/F/G.py D:/A/B/C/H.py D:/A/B/I.py"))

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    (d / "README.md").write_text(
        "# Title\n\n![badge](x)\n\n> a quote\n\n"
        "This tool renders explainer videos from a data file.\n",
        encoding="utf-8")
    (d / "CLAUDE.md").write_text("Read the other file first please now.\n",
                                 encoding="utf-8")
    text, src = tools.describe(d)
    check("description skips heading, badge and blockquote",
          text == "This tool renders explainer videos from a data file.", text)
    check("README wins over CLAUDE.md", src == "README.md", src)

    (d / "README.md").unlink()
    text, src = tools.describe(d)
    check("falls back to CLAUDE.md", src == "CLAUDE.md", src)

    (d / "CLAUDE.md").unlink()
    text, src = tools.describe(d)
    check("nothing usable returns (None, None)", (text, src) == (None, None),
          repr((text, src)))

    # A folder whose only README is furniture must NOT get a description -
    # inventing one is the exact trap this module refuses to fall into.
    (d / "README.md").write_text("# reel-forge\n\n| a | b |\n", encoding="utf-8")
    text, _ = tools.describe(d)
    check("furniture-only README yields no description", text is None, text)

    # _touched must ignore .git, or reading a repo's log makes it look edited.
    (d / "plain.txt").write_text("x", encoding="utf-8")
    git = d / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    import os
    import time
    future = time.time() + 86400 * 30
    os.utime(git, (future, future))
    check("_touched ignores .git", tools._touched(d) == tools._touched(d))
    stamp = tools._touched(d)
    import datetime
    later = datetime.datetime.fromtimestamp(future).strftime("%Y-%m-%d")
    check("_touched does not report the future .git mtime", stamp != later,
          "%s vs %s" % (stamp, later))

# --------------------------------------------------------------- tools.json
with tempfile.TemporaryDirectory() as td:
    f = Path(td) / "tools.json"
    f.write_text(json.dumps({"roster": ["a", "b", ""], "not_tools": ["_x"]}), encoding="utf-8")
    check("tools.json is read", tools.load_roster(f) == (["a", "b"], {"_x"}),
          tools.load_roster(f))
    f.write_text(json.dumps(["a", "c"]), encoding="utf-8")
    check("a bare list is accepted as the roster", tools.load_roster(f) == (["a", "c"], set()))
    f.write_text("{not json", encoding="utf-8")
    check("a broken tools.json is empty, not a crash", tools.load_roster(f) == ([], set()))
    check("a missing tools.json is empty", tools.load_roster(Path(td) / "no.json") == ([], set()))

# ---------------------------------------------------------- not configured
_saved = (tools.ROOT, tools.ROSTER, tools.NOT_TOOLS, tools.MANIFEST, tools.FAVS)
tools.ROOT, tools.MANIFEST = None, None
rows, meta = tools.scan()
check("no tools_root: no rows, configured=False",
      rows == [] and meta["configured"] is False, meta)

# --------------------------------------------------------------- the fixture
FIX = Path(tempfile.mkdtemp(prefix="kit-tools-"))
for name in ("hyperframes", "hyperframes-tools", "reel-forge", "_archive", "new-thing"):
    (FIX / name).mkdir()
(FIX / "reel-forge" / "README.md").write_text(
    "# reel-forge\n\nRenders explainer videos from a script and a data file.\n",
    encoding="utf-8")
(FIX / "reel-forge" / "launch.bat").write_text("@echo off\n", encoding="utf-8")
(FIX / "hyperframes-tools" / "build.py").write_text("", encoding="utf-8")
tools.ROOT, tools.MANIFEST = FIX, None
tools.ROSTER = ["hyperframes", "hyperframes-tools", "reel-forge", "gone-missing"]
tools.NOT_TOOLS = {"_archive"}
tools.FAVS = FIX / "tool_favourites.json"

# --------------------------------------------------------------- the roster
roster, rejected = set(tools.ROSTER), set(tools.NOT_TOOLS)
check("roster has no duplicates", len(roster) == len(tools.ROSTER))
# A name in both lists is a contradiction: rostered AND explicitly not a tool.
check("roster and rejected do not overlap", not (roster & rejected),
      ", ".join(sorted(roster & rejected)))
# This asserted `len(roster) == 21` until 2026-08-31 and had been failing for
# over a week - through 23, 24 and 25 - because the roster is SUPPOSED to grow
# when a tool is built. The test owned a copy of a number the roster owns, so
# every legitimate addition broke it, and a suite with one permanent red is a
# suite nobody reads. That is the "two copies of one fact drifted apart" class,
# aimed at itself.
#
# Assert the INVARIANTS instead - things that stay true at any size. (On YOUR
# roster, a rostered folder missing from disk is a finding the page reports;
# the fixture deliberately includes one, below.)
check("every rejected folder actually exists on disk",
      not [f for f in rejected if not (tools.ROOT / f).is_dir()],
      ", ".join(sorted(f for f in rejected if not (tools.ROOT / f).is_dir())))
check("the roster is not empty", len(roster) > 0, len(roster))

# ------------------------------------------------------------ the drift valve
rows, meta = tools.scan()
on_disk = {p.name for p in tools.ROOT.iterdir() if p.is_dir()}
expected_unlisted = on_disk - roster - rejected
check("unlisted is exactly what is neither rostered nor rejected",
      set(meta["unlisted"]) == expected_unlisted,
      "%s vs %s" % (sorted(meta["unlisted"]), sorted(expected_unlisted)))
check("every directory on disk is accounted for",
      not (on_disk - roster - rejected - set(meta["unlisted"])))
check("meta count matches the rows", meta["count"] == len(rows))
check("a rostered folder that vanished is reported, not skipped",
      len(rows) + len(meta["missing"]) == len(tools.ROSTER)
      and meta["missing"] == ["gone-missing"], meta["missing"])
check("an unrostered, unrejected folder shows as unlisted",
      meta["unlisted"] == ["new-thing"], meta["unlisted"])
rf = next((r for r in rows if r["name"] == "reel-forge"), {})
check("description read from the README",
      rf.get("desc") == "Renders explainer videos from a script and a data file.", rf.get("desc"))
check("a single .bat is offered as the launcher", rf.get("launch") == "launch.bat", rf.get("launch"))
check("a folder with nothing to run says so",
      next(r for r in rows if r["name"] == "hyperframes")["start"] == "none")

# --------------------------------------------------------------- row shape
for r in rows:
    check("%s has a start value we render" % r["name"],
          r["start"] in ("icon", "scheduled", "command", "none"), r["start"])
check("no row claims a description it did not find",
      all(bool(r["desc"]) == bool(r["desc_from"]) for r in rows))
check("a tool with a shortcut is never start=none",
      all(r["start"] != "none" for r in rows if r["shortcuts"]))
check("start=none means nothing runnable and no launcher",
      all(not r["entries"] and not r["shortcuts"] and not r["tasks"]
          for r in rows if r["start"] == "none"))
check("notion refs name a page, not a bool",
      all(isinstance(x, str) and x for r in rows for x in r["notion"]))

# hyperframes is the regression case, checked against the LIVE scan and not
# only against the unit above - the bug was in how the scan wired it together.
hf = next((r for r in rows if r["name"] == "hyperframes"), None)
if hf:
    check("hyperframes does not borrow hyperframes-tools' icon",
          not hf["shortcuts"], hf["shortcuts"])

# --------------------------------------------------------------- favourites
# Written to the fixture folder, never to your real tool_favourites.json.
tools.save_favs(["reel-forge", "definitely not a tool"])
got = tools.load_favs()
check("save_favs keeps a rostered name", "reel-forge" in got)
check("save_favs drops a name it does not own",
      "definitely not a tool" not in got, got)

(tools.ROOT, tools.ROSTER, tools.NOT_TOOLS, tools.MANIFEST, tools.FAVS) = _saved
import shutil
shutil.rmtree(FIX, ignore_errors=True)

# ----------------------------------------------------------------- wiring
# Without these, every assertion above can pass while the tab shows nothing.
import build
import serve
bsrc = inspect.getsource(build)
check("build.py imports the tools scanner", "import tools as toollib" in bsrc)
check("build.py puts tools in the payload",
      '"tools": tls' in bsrc and '"tmeta": tmeta' in bsrc)
check("build.py renders a tools tab", "drawTools()" in bsrc)
check("the Tools tab is no longer a placeholder", "'Not built yet" not in bsrc)
ssrc = inspect.getsource(serve)
check("serve.py routes /api/tfav", '"/api/tfav"' in ssrc)
check("the tool star validates against the roster", "toollib.ROSTER" in ssrc)
check("the tool star re-reads after writing",
      "tool_favourites.json still disagrees" in ssrc)
# scan() must never reach the network - the page has to build with Notion down.
scan_src = inspect.getsource(tools.scan)
check("scan() does not call the network",
      "urllib" not in scan_src and "requests" not in scan_src)

print("\n=== SELFTEST COMPLETE - %d failure(s) ===" % len(FAILS))
for f in FAILS:
    print("  " + f)
sys.exit(1 if FAILS else 0)
