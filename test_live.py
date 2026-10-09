#!/usr/bin/env python3
"""Selftest for live.py - the transcript tailer behind Kit's "in use" glow.

THE INVARIANT THIS EXISTS FOR: history is not activity. The corpus holds 2.2 GB
of past work, and every skill in it has fired at some point. If the watcher
reports what it finds on its first pass, opening Kit lights up the entire
inventory at once and every light is a lie about the present. The first poll
must record where the files END and report nothing.

That failure would be invisible in normal use - a page that lights everything
on load looks enthusiastic, not broken - so it is asserted here rather than
left to be noticed.

Run: python test_live.py      Exit 0 = every invariant holds.
"""
import inspect
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import live

FAILS = []


def check(label, ok, detail=""):
    print("%-5s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILS.append("%s: %s" % (label, detail))


TMP = Path(tempfile.mkdtemp(prefix="kit-live-"))
live.PROJECTS = TMP
log = TMP / "session.jsonl"


def append(text):
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def names(events, kind=None):
    return sorted(e["name"] for e in events if kind is None or e["kind"] == kind)


try:
    # A transcript with history already in it - the state on every real machine.
    append('{"type":"tool_use","name":"Skill","input":{"skill":"audio-balance"}}')
    append('{"type":"tool_use","input":{"command":"python weekly_health.py"}}')

    w = live.Watcher()

    # ------------------------------------------------------- the invariant
    first = w.poll()
    check("first poll reports nothing (history is not activity)",
          first == [], repr(first[:4]))
    check("first poll still leaves the watcher primed", w.primed)

    # --------------------------------------------------------- new activity
    append('{"type":"tool_use","name":"Skill","input":{"skill":"gepeto"}}')
    got = w.poll()
    check("a new skill invocation is seen", names(got, "skill") == ["gepeto"], repr(got))
    check("history is not replayed alongside it",
          "audio-balance" not in names(got), repr(names(got)))

    # A plugin-prefixed name must land on the bare folder name, or it never
    # matches a card.
    append('{"input":{"skill":"anthropic-skills:card-factory"}}')
    check("plugin prefix is stripped",
          names(w.poll(), "skill") == ["card-factory"])

    # ------------------------------------------------------------- programs
    append('{"input":{"command":"python C:/work/tools/x/check_skills.py --fix"}}')
    got = w.poll()
    check("a program run is seen", names(got, "program") == ["check_skills"])

    # The PATH comes too, and the Tools tab depends on it. Four program stems
    # live in more than one tool folder (`serve` in three), so without a path
    # the tab cannot tell which card to light and deliberately lights none.
    # Drop this field and that disambiguation silently stops working while
    # every name assertion above still passes.
    check("the program event carries the path it was run from",
          got[0].get("path") == "C:/work/tools/x/check_skills.py",
          repr(got[0]))

    append('{"input":{"command":"python check_skills.py"}}')
    got = w.poll()
    check("a bare filename yields a path with no folder in it",
          got[0].get("path") == "check_skills.py", repr(got[0]))

    # A QUOTED path, exactly as a JSON transcript stores it: the quote arrives
    # backslash-escaped. This matched nothing at all until 2026-08-24, so every
    # `python "some/path.py"` run was invisible to the whole feature.
    append(r'{"input":{"command":"python \"/home/user/projects/tools/reel-forge/cut.py\" --go"}}')
    got = w.poll()
    check("a quoted path is still seen", names(got, "program") == ["cut"], repr(got))
    check("and its folder survives the quoting",
          "/reel-forge/" in got[0].get("path", ""), repr(got[0]))

    append(r"""{"input":{"command":"python 'C:/work/tools/resolve/build_fx.py'"}}""")
    got = w.poll()
    check("a single-quoted path is seen too",
          names(got, "program") == ["build_fx"], repr(got))

    # ------------------------------------------------------ launches (.bat)
    # Icon-started tools have no transcript presence at all, so the
    # launchers write a marker and the MTIME is the signal.
    runs = TMP / "runs"
    runs.mkdir(exist_ok=True)
    keep_runs = live.RUNS
    live.RUNS = runs
    try:
        # A marker that already exists when Kit starts is HISTORY. Same rule as
        # the transcripts: without it, opening Kit lights every tool you have
        # ever launched.
        (runs / "reel-forge.run").write_text("", encoding="utf-8")
        w2 = live.Watcher()
        w2.files = []
        check("first poll ignores markers that already existed",
              w2.poll() == [], "primed pass must be empty")
        check("a pre-existing marker is not replayed on the second poll",
              names(w2.poll(), "tool") == [], repr(w2.poll()))

        # Now launch something.
        (runs / "lead-gen.run").write_text("", encoding="utf-8")
        got = w2.poll()
        check("launching a tool is seen", names(got, "tool") == ["lead-gen"],
              repr(got))
        check("the launch event is a tool, not a program",
              got and got[0]["kind"] == "tool", repr(got))

        # Re-launching the SAME tool must fire again, not be swallowed as
        # "already seen" - the mtime moves forward.
        os.utime(runs / "lead-gen.run", (time.time() + 2, time.time() + 2))
        check("relaunching the same tool fires again",
              names(w2.poll(), "tool") == ["lead-gen"])

        # An old marker must not resurrect. A tool launched last week is not
        # activity now.
        old = time.time() - (live.KEEP + 600)
        (runs / "resolve.run").write_text("", encoding="utf-8")
        os.utime(runs / "resolve.run", (old, old))
        check("a marker older than the keep window is ignored",
              "resolve" not in names(w2.poll()), repr(w2.poll()))

        # The event carries the marker's OWN time, not now(). A tool started
        # before the page opened must read as old, not as "just now".
        (runs / "resolve.run").write_text("", encoding="utf-8")
        recent = time.time() - 30
        os.utime(runs / "resolve.run", (recent, recent))
        got = [e for e in w2.poll() if e["name"] == "resolve"]
        check("a launch is stamped when it happened, not when it was noticed",
              got and abs(got[0]["at"] - recent) < 2, repr(got))
    finally:
        live.RUNS = keep_runs

    # ------------------------------------------------------- hooks are not you
    # The harness writes its own hook runs at the root of a line as
    # hookInfos[{command,durationMs}], and CMD_RE cannot tell that key from a
    # Bash tool call. Four Stop hooks were arriving as PROGRAM events 194 times
    # in one session - once per turn. Left in, a registered hook that lives
    # inside a rostered tool would light that tool as though it had been used.
    keep = w._hooks, w._hooks_at
    w._hooks = ({"d:/home/alex/.claude/hooks/wrap_reminder.py",
                 "c:/work/tools/team-tools/skills_shortlist.py"},
                {"wrap_reminder", "skills_shortlist"})
    w._hooks_at = time.time()
    append(r'{"hookInfos":[{"command":"python \"D:\\home\\alex\\.claude\\hooks\\wrap_reminder.py\"","durationMs":126}]}')
    got = w.poll()
    check("a hook the harness ran is not a program event",
          names(got, "program") == [], repr(got))

    # The one that matters: a hook living INSIDE a rostered tool folder.
    append(r'{"hookInfos":[{"command":"python \"C:\\work\\tools\\team-tools\\skills_shortlist.py\""}]}')
    got = w.poll()
    check("a hook inside a tool folder cannot light that tool",
          names(got, "program") == [], repr(got))

    # A real program in the same folder must still come through, or the filter
    # has quietly blinded the whole tool.
    append('{"input":{"command":"python C:/work/tools/team-tools/check_skills.py"}}')
    got = w.poll()
    check("a real program in that folder is still seen",
          names(got, "program") == ["check_skills"], repr(got))
    w._hooks, w._hooks_at = keep

    # Backslashes are normalised, because the consumer matches "/name/".
    append(r'{"input":{"command":"python C:\\work\\tools\\reel-forge\\cut.py"}}')
    got = w.poll()
    check("windows separators are normalised to forward slashes",
          "/" in got[0].get("path", "") and "\\" not in got[0].get("path", ""),
          repr(got[0]))
    check("the normalised path still contains the folder segment",
          "/reel-forge/" in got[0].get("path", ""), repr(got[0]))

    # One command naming a program twice is one decision to run it.
    append('{"input":{"command":"python build.py && python build.py"}}')
    got = w.poll()
    check("one command counts a program once",
          names(got, "program") == ["build"], repr(got))

    # Two DIFFERENT things in one poll are two events.
    append('{"input":{"skill":"media-use"}}')
    append('{"input":{"command":"python serve.py"}}')
    got = w.poll()
    check("distinct events in one poll are kept",
          names(got) == ["media-use", "serve"], repr(names(got)))

    # A quiet poll is quiet.
    check("nothing new means no events", w.poll() == [])

    # ------------------------------------------------------ new session file
    # A session started after Kit did must be read whole, not primed past -
    # otherwise the transcript you are actively working in is the one thing
    # the watcher ignores.
    w.listed = 0.0                       # force the directory walk
    (TMP / "later.jsonl").write_text(
        '{"input":{"skill":"resolve-conform"}}\n', encoding="utf-8")
    check("a session that starts later is read from the top",
          "resolve-conform" in names(w.poll()), "new file was primed past")

    # ---------------------------------------------------------- resilience
    log.write_text("", encoding="utf-8")          # truncated / rotated
    w.listed = 0.0
    try:
        w.poll()
        ok = True
    except Exception as exc:                       # noqa: BLE001
        ok = False
        detail = repr(exc)
    check("truncation resyncs instead of crashing", ok,
          locals().get("detail", ""))

    # ------------------------------------------------------------- expiry
    w2 = live.Watcher()
    w2.events = [{"kind": "skill", "name": "old", "at": time.time() - live.KEEP - 5}]
    check("stale events expire out of recent()", w2.recent() == [])

    # ---------------------------------------------------------- the wiring
    # Deleting the prime step would leave the tests above green only until the
    # first run against a real corpus, so assert it is still called.
    src = inspect.getsource(live.Watcher.poll)
    check("poll() still primes before reading", "_prime()" in src)
    check("poll() still returns early on the priming pass",
          "return []" in src)
    prime_src = inspect.getsource(live.Watcher._prime)
    check("_prime records file sizes, not zero",
          "st_size" in prime_src)
    # Reading an unbounded delta would let one huge append stall the poll.
    check("poll() caps how much it reads", "MAX_DELTA" in src)

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n=== SELFTEST COMPLETE - %d failure(s) ===" % len(FAILS))
for f in FAILS:
    print("  " + f)
sys.exit(1 if FAILS else 0)
