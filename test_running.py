#!/usr/bin/env python3
"""Selftest for running.py — attribution, and the two fallbacks it needed.

The user: "the bridge is running but not lit?". It was: ten live processes.
Kit could not see any of them, because the glow answers "did something happen
recently" and a bridge started six hours ago is a STATE, not an event. This
module answers the other question.

Getting a process to the right tool took three passes, and each one exists
because the previous one was not enough:

  1. the command line names the folder    - an MCP bridge's own path
  2. a PARENT names it                    - `cmd /c "cd X && python Y"`
  3. the SCRIPT FILENAME is unique to it  - a server whose whole command
                                            line is `python app_server.py`
                                            and whose grandparent has exited,
                                            because .bat used `start`

The interesting assertions are the ones that must NOT match: substring
collisions (hyperframes vs hyperframes-tools, the bug this repo has already
shipped once), and a filename that exists in two tools, which is dropped
rather than guessed at.

Runs against a temporary tools folder, so it does not depend on what this
machine has configured.

Run: python test_running.py      Exit 0 = every invariant holds.
"""
import inspect
import json
import pathlib
import shutil
import sys
import tempfile

import running
import tools as toollib

FAILS = []


def check(label, ok, detail=""):
    print("%-5s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILS.append("%s: %s" % (label, detail))


# ------------------------------------------------------------- segment match
check("a folder named as a path segment matches",
      running._mentions("c:/work/tools/davinci-bridge/src/x.py", "davinci-bridge"))
check("a PREFIX of a longer sibling does not match",
      not running._mentions("c:/work/tools/hyperframes-tools/x.py", "hyperframes"))
check("the longer sibling matches itself",
      running._mentions("/home/user/tools/hyperframes-tools/x.py", "hyperframes-tools"))
check("a bare mention with no separator does not match",
      not running._mentions("restarting the resolve service", "resolve"))
# running.py and tools.py must not drift apart on this - they answer the same
# question about the same names, and a difference would be invisible.
check("running and tools use the same matcher",
      inspect.getsource(running._mentions).split("pat =")[1].strip()
      == inspect.getsource(toollib._mentions).split("pat =")[1].strip())

# ----------------------------------------------------------- not configured
_saved = (toollib.ROOT, list(toollib.ROSTER))
toollib.ROOT, toollib.ROSTER = None, []
check("no tools_root: the script index is empty, not a crash",
      running.script_owners(refresh=True) == {})
check("no tools_root: the probe returns at once with no error",
      running.probe() == ({}, set(), ""))

# ----------------------------------------------------------- the fixture
FIX = pathlib.Path(tempfile.mkdtemp(prefix="kit-running-"))
for tool in ("lead-gen", "content-feed", "signal-radar"):
    (FIX / tool).mkdir()
    (FIX / tool / "serve.py").write_text("", encoding="utf-8")   # in all three
(FIX / "lead-gen" / "server").mkdir()
(FIX / "lead-gen" / "server" / "app_server.py").write_text("", encoding="utf-8")
(FIX / "lead-gen" / "node_modules").mkdir()
(FIX / "lead-gen" / "node_modules" / "vendored.js").write_text("", encoding="utf-8")
toollib.ROOT, toollib.ROSTER = FIX, ["lead-gen", "content-feed", "signal-radar"]

# ----------------------------------------------------------- script ownership
owners = running.script_owners(refresh=True)
check("the script index is not empty", len(owners) >= 1, len(owners))
check("a script unique to one tool is attributed",
      owners.get("app_server.py") == "lead-gen",
      owners.get("app_server.py"))
# The two-owner rule. `serve.py` exists in three rostered tools, so it must be
# ABSENT from the index rather than pointing at one of them.
check("a filename in more than one tool is dropped, not guessed",
      "serve.py" not in owners, owners.get("serve.py"))
check("the index only ever holds one owner per name",
      all(isinstance(v, str) for v in owners.values()))
check("heavy vendor trees are skipped",
      "vendored.js" not in owners, owners.get("vendored.js"))

# ------------------------------------------------------------------ the probe
got, connected, err = running.probe()
check("the probe runs without error", not err, err)
check("it returns pid lists", all(isinstance(v, list) and v for v in got.values()),
      repr(got)[:200])
check("every attributed tool is on the roster",
      set(got) <= set(toollib.ROSTER), sorted(set(got) - set(toollib.ROSTER)))
check("no pid is attributed to two tools",
      len([p for v in got.values() for p in v]) ==
      len({p for v in got.values() for p in v}))
# Kit is serving this very check in most runs, but not when the suite is run
# standalone - so this is reported, never asserted.
print("      (running now: %s)" % (", ".join(sorted(got)) or "nothing"))

# --------------------------------------------------------------- connected
# The stronger claim. It must never outrun the weaker one, and it must never
# claim a tool CONNECT_CHECKS has no opinion about.
check("connected is always a subset of running",
      set(connected) <= set(got), sorted(set(connected) - set(got)))
check("connected only names tools with a real check",
      set(connected) <= set(running.CONNECT_CHECKS),
      sorted(set(connected) - set(running.CONNECT_CHECKS)))
_cfg = FIX / "tools.json"
_cfg.write_text(json.dumps({"roster": [], "connect": {
    "a-bridge": {"tcp": 1}, "a-panel": {"process": "Editor.exe"},
    "junk": "not a rule", "empty": {}}}), encoding="utf-8")
_checks = running._connect_checks(_cfg)
check("connect checks are read from tools.json, junk ignored",
      set(_checks) == {"a-bridge", "a-panel"}, sorted(_checks))
check("a process check matches case-insensitively",
      _checks["a-panel"](["c:/apps/EDITOR.EXE --x"]) and not _checks["a-panel"](["other.exe"]))
check("no tools.json means no connect checks",
      running._connect_checks(FIX / "missing.json") == {})
check("a closed port is not mistaken for a connection",
      not running._tcp_alive("127.0.0.1", 1))  # port 1 is never a real MCP bridge
print("      (connected now: %s)" % (", ".join(sorted(connected)) or "nothing"))

# ------------------------------------------------------------------- the cache
p = running.Probe()
first = p.get()
stamp = p.at
second = p.get()
check("a second call inside the window does not re-probe", p.at == stamp)
check("and returns the same answer", second is first)
check("the cache interval is slower than the live poll", running.Probe.EVERY >= 5,
      running.Probe.EVERY)

toollib.ROOT, toollib.ROSTER = _saved
running.script_owners(refresh=True)
shutil.rmtree(FIX, ignore_errors=True)

# ----------------------------------------------------------------- wiring
import build
import serve
bsrc = inspect.getsource(build)
check("the page reads a running set", "RUNNING = new Set(j.running)" in bsrc)
check("running is painted as its own state, not as the glow",
      "classList.toggle('up'" in bsrc)
check("the page says running and recent are different questions",
      "still up" in bsrc)
check("the page reads a connected set",
      "CONNECTED = new Set(j.connected)" in bsrc)
check("connected is painted as its own state",
      "classList.toggle('connected'" in bsrc)
# The badge text must live-update on the poll, not just the border - a
# badge stuck on "running" for up to ten seconds after a real connection
# read as "nothing changed" (the user hit this within a minute of shipping
# the feature). Both call sites sharing one function is what prevents it
# recurring: two copies of the badge HTML can only drift, never merge back.
check("toolCard and paintLive share one badge function, not two copies",
      bsrc.count("function toolBadge(name)") == 1 and
      "toolBadge(t.name)" in bsrc and "toolBadge(name)" in bsrc.split("function toolBadge")[1])
ssrc = inspect.getsource(serve)
check("serve.py imports the probe", "import running as runlib" in ssrc)
check("/api/live carries the running set", '"running": up' in ssrc)
check("/api/live carries the connected set", '"connected": conn' in ssrc)
check("the request handler never enumerates processes itself",
      "PROBE.map" in ssrc and "PROBE.get()" not in ssrc.split("def do_GET")[-1]
      .split("def ")[0])
check("the background thread refreshes it", "PROBE.get()" in ssrc)
check("a probe fault cannot stop the watcher thread",
      ssrc.count("pass          # so must a probe fault") == 1)

print("\n=== SELFTEST COMPLETE - %d failure(s) ===" % len(FAILS))
for f in FAILS:
    print("  " + f)
sys.exit(1 if FAILS else 0)
