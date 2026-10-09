#!/usr/bin/env python3
"""Selftest for hooks.py - the reconcile that Progs cannot do.

This tab exists because a file listing cannot answer the only question worth
asking about a hook: will the harness ever run it? `review_gate.py` sat in the
Progs tab looking healthy - good docstring, has a test, 181 lines - while
failing to gate the commit it was written to gate. Nothing about a file says
that.

So the invariants here are about the RECONCILE, in both directions:

  a script registered three times is ONE hook, not three
  a file nobody registers is dead code and must say so
  a registration whose file is gone is a harness error waiting to happen

and about the two ways the reconcile could quietly stop working: test_ files
counted as orphans (which buries the real orphan in noise), and a settings
file that will not parse being swallowed (which would show every hook in that
scope as missing, with no hint that the file is the problem).

Run: python test_hooks.py      Exit 0 = every invariant holds.
"""
import inspect
import json
import sys
import tempfile
from pathlib import Path

import hooks

FAILS = []


def check(label, ok, detail=""):
    print("%-5s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILS.append("%s: %s" % (label, detail))


def settings(hooks_block):
    return {"hooks": hooks_block}


def reg(cmd, event, matcher=""):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": cmd}]}


# ---------------------------------------------------------------- a corpus
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    hookdir = root / "hooks"
    hookdir.mkdir()
    other = root / "narrator"
    other.mkdir()

    (hookdir / "alpha.py").write_text('"""Alpha guard - blocks a thing.\n\nmore\n"""\nx = 1\n',
                                      encoding="utf-8")
    (hookdir / "test_alpha.py").write_text('"""Tests for alpha."""\n', encoding="utf-8")
    (hookdir / "beta.py").write_text('"""Beta guard - fires on stop."""\n', encoding="utf-8")
    # An ORPHAN: on disk, registered nowhere.
    (hookdir / "ghost.py").write_text('"""Ghost - nothing calls me."""\n', encoding="utf-8")
    # No docstring at all, and a leading string that is NOT a docstring.
    (hookdir / "mute.py").write_text('import os\n"looks like a docstring"\n', encoding="utf-8")
    (other / "hook.py").write_text('"""Narrator stop hook."""\n', encoding="utf-8")

    user = root / "settings.json"
    user.write_text(json.dumps(settings({
        "PreToolUse": [reg(f'python "{hookdir / "alpha.py"}"', "PreToolUse", "Bash|PowerShell")],
        "Stop": [
            reg(f'python "{hookdir / "beta.py"}"', "Stop"),
            reg(f'python "{other / "hook.py"}"', "Stop"),
        ],
        "SessionStart": [
            # The same script on three matchers: ONE hook that fires three ways.
            reg(f'python "{hookdir / "mute.py"}"', "SessionStart", "startup"),
            reg(f'python "{hookdir / "mute.py"}"', "SessionStart", "clear"),
            reg(f'python "{hookdir / "mute.py"}"', "SessionStart", "compact"),
            # Not a script at all.
            reg("git -C /somewhere pull --ff-only --quiet", "SessionStart", "startup"),
            # Registered, but the file does not exist.
            reg(f'python "{hookdir / "vanished.py"}"', "SessionStart", "startup"),
        ],
    })), encoding="utf-8")

    proj = root / "proj.json"
    proj.write_text(json.dumps(settings({
        "SessionStart": [reg(f'powershell -File "{other / "shell.ps1"}"',
                             "SessionStart", "startup|resume")],
    })), encoding="utf-8")
    (other / "shell.ps1").write_text("# Shell hook that checks the repos.\nWrite-Host x\n",
                                     encoding="utf-8")

    keep_scopes, keep_dir, keep_favs = hooks.SCOPES, hooks.HOOKDIR, hooks.FAVS
    hooks.SCOPES = [("user", user), ("project", proj)]
    hooks.HOOKDIR = hookdir
    hooks.FAVS = root / "favs.json"
    try:
        rows, meta = hooks.scan()
        by = {r["name"]: r for r in rows}

        # ---- collapse ----
        check("three matchers on one script make one row",
              len([r for r in rows if r["name"] == "mute"]) == 1)
        check("that row keeps all three registrations",
              len(by["mute"]["regs"]) == 3, by["mute"]["regs"])
        check("its events collapse to one distinct event",
              by["mute"]["events"] == ["SessionStart"], by["mute"]["events"])

        # ---- orphan ----
        check("an unregistered file is present", "ghost" in by)
        check("the orphan is marked unregistered", not by["ghost"]["registered"])
        check("the orphan says it cannot fire",
              any("cannot fire" in f for f in by["ghost"]["flags"]), by["ghost"]["flags"])
        check("meta counts exactly one orphan", meta["orphans"] == 1, meta["orphans"])

        # ---- test files are NOT orphans ----
        check("a test_ file is not listed as a hook", "test_alpha" not in by)
        check("alpha is credited with its test", by["alpha"]["test"] == "test_alpha.py")
        check("beta has no test and says so", by["beta"]["test"] is None
              and any("no test" in f for f in by["beta"]["flags"]))

        # ---- missing file ----
        check("a registered-but-missing script still gets a row", "vanished" in by)
        check("the missing one is marked absent", not by["vanished"]["exists"])
        check("the missing one flags the harness problem",
              any("missing" in f for f in by["vanished"]["flags"]),
              by["vanished"]["flags"])
        check("meta counts exactly one missing", meta["missing"] == 1, meta["missing"])

        # ---- out-of-folder naming ----
        # `hook.py` alone renders as "hook", which identifies nothing.
        check("an outside hook is named by its parent", "narrator/hook" in by,
              sorted(by))
        check("the outside hook flags where it lives",
              any("outside the hooks folder" in f for f in by["narrator/hook"]["flags"]))
        check("meta counts both outsiders", meta["outside"] == 2, meta["outside"])

        # ---- scopes ----
        check("the project scope is read too", "narrator/shell" in by, sorted(by))
        check("a project hook is labelled project",
              by["narrator/shell"]["scopes"] == ["project"], by["narrator/shell"]["scopes"])
        check("a powershell hook is typed powershell",
              by["narrator/shell"]["kind"] == "powershell")
        check("a powershell hook takes its summary from the leading comment",
              by["narrator/shell"]["summary"] == "Shell hook that checks the repos.",
              by["narrator/shell"]["summary"])

        # ---- shell-only registration ----
        check("a bare shell command is not a hook row",
              not any("git" in r["name"] for r in rows), sorted(by))
        check("but it is reported in meta", len(meta["shell"]) == 1, meta["shell"])

        # ---- docstrings via ast, not regex ----
        check("summary is the docstring's first line",
              by["alpha"]["summary"] == "Alpha guard - blocks a thing.",
              by["alpha"]["summary"])
        check("a leading non-docstring string is not a summary",
              by["mute"]["summary"] is None, by["mute"]["summary"])

        # ---- registered rows sort first ----
        check("unregistered rows sort last",
              rows[-1]["registered"] is False, rows[-1]["name"])

        # ---- favourites validate ----
        hooks.save_favs(["narrator/hook", "not a hook at all"], set(by))
        got = hooks.load_favs()
        check("a slashed hook name can be starred", "narrator/hook" in got, got)
        check("an unknown name is dropped", "not a hook at all" not in got, got)

        # ---- a starred out-of-folder hook actually SHOWS as starred ----
        # save_favs/load_favs round-tripping the qualified name is not the
        # same claim as scan() crediting the right ROW with it. It used to
        # check `p.stem in favs` ("hook" in {"narrator/hook"}) while every
        # other consumer - the row's own "name", /api/hfav's validation,
        # the file just written above - all use the qualified label. That
        # mismatch meant a real star, correctly saved, silently never
        # rendered as on. Re-scan and check the ROW, not the file.
        rows3, _ = hooks.scan()
        by3 = {r["name"]: r for r in rows3}
        check("a starred out-of-folder hook's row says so",
              by3["narrator/hook"]["fav"] is True, by3["narrator/hook"]["fav"])

        # ---- a broken settings file is a finding, not a crash ----
        proj.write_text("{ this is not json", encoding="utf-8")
        rows2, meta2 = hooks.scan()
        check("a broken settings file does not raise", True)
        check("it is reported as a problem", len(meta2["problems"]) == 1, meta2["problems"])
        check("the problem says that scope is dead",
              "no hook" in meta2["problems"][0].lower(), meta2["problems"])
        check("the other scope still parses",
              any(r["name"] == "alpha" for r in rows2))
    finally:
        hooks.SCOPES, hooks.HOOKDIR, hooks.FAVS = keep_scopes, keep_dir, keep_favs

# ------------------------------------------------------------- the real thing
rows, meta = hooks.scan()
check("the live scan finds registered hooks", meta["registered"] > 0, meta)
check("no live row is both unregistered and unreadable",
      all(r["exists"] or r["registered"] for r in rows))
check("every live row carries a name that identifies it",
      all(r["name"] and r["name"] != "hook" for r in rows),
      [r["name"] for r in rows])
check("review_gate is registered", any(r["name"] == "review_gate" and r["registered"]
                                       for r in rows))

# ----------------------------------------------------------------- wiring
import build
import serve
bsrc = inspect.getsource(build)
check("build.py imports the hooks scanner", "import hooks as hooklib" in bsrc)
check("build.py puts hooks in the payload",
      '"hooks": hks' in bsrc and '"hmeta": hmeta' in bsrc)
check("build.py renders a hooks tab", "drawHooks()" in bsrc)
check("the page says registered is not firing",
      "Registered is not the same as firing" in bsrc)
ssrc = inspect.getsource(serve)
check("serve.py routes /api/hfav", '"/api/hfav"' in ssrc)
check("the hook star re-reads after writing",
      "hook_favourites.json still disagrees" in ssrc)
scan_src = inspect.getsource(hooks.scan)
check("scan() does not call the network",
      "urllib" not in scan_src and "requests" not in scan_src)

# ---- script extraction on every OS ----
# Matching only drive-letter paths made every hook on macOS/Linux a "shell
# command" with no script, so the Hooks tab there showed nothing at all.
def _script(cmd):
    m = hooks._SCRIPT.search(cmd)
    return hooks._resolve_script(m.group(1)) if m else None


check("a POSIX absolute path is a script",
      _script("python3 /home/user/.claude/hooks/guard.py") == "/home/user/.claude/hooks/guard.py",
      _script("python3 /home/user/.claude/hooks/guard.py"))
check("a quoted Windows path is a script",
      _script(r'python "D:\home\alex\.claude\hooks\guard.py"') == "D:/home/alex/.claude/hooks/guard.py")
check("~ expands to the home folder",
      (_script("python3 ~/.claude/hooks/guard.py") or "").endswith("/.claude/hooks/guard.py")
      and "~" not in (_script("python3 ~/.claude/hooks/guard.py") or "~"))
check("$HOME expands too",
      "$HOME" not in (_script("python3 $HOME/.claude/hooks/guard.py") or "$HOME"))
check("a shell-script hook is a script",
      _script("bash /opt/hooks/check.sh --x") == "/opt/hooks/check.sh")
check("a relative path inside a command is not mistaken for an absolute one",
      _script("cd hooks && python hooks/x.py") is None)
check("a bare shell command still has no script",
      _script("git -C ~/.claude pull --ff-only") is None)

print("\n=== SELFTEST COMPLETE - %d failure(s) ===" % len(FAILS))
for f in FAILS:
    print("  " + f)
sys.exit(1 if FAILS else 0)
