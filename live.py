#!/usr/bin/env python3
"""
Live activity — what fired in the last few seconds, so Kit can light it up.

    python live.py          watch the console instead of the page

Claude Code appends to a transcript as it works: a skill firing writes
"skill":"<name>", a program run writes a Bash "command" naming the file. Both
land on disk within a second of happening. This tails those files and reports
what is new, and serve.py hands the result to the page, which glows the
matching card.

WHAT THIS CAN AND CANNOT SEE - the page says the same, because a green light
that means less than it looks like is worse than no light at all:

  * SKILLS: seen. A skill invocation is written to the transcript by name.
  * PROGRAMS: seen ONLY when run through Claude Code. A program started from
    a terminal, a .bat or a scheduler writes nothing here. A weekly scheduled
    job will never once light up.
  * HOOKS: never. They are fired by the harness on an event, not by a command,
    so nothing names them. A guard hook can block a write and stay dark
    throughout.
  * PROMPTS: not from here. A prompt is pasted somewhere else - another app,
    a web tool - off this machine's transcripts. The page lights those on
    copy instead, which is the one moment it can honestly observe.

HISTORY IS NOT ACTIVITY. The first poll records where every file currently
ends and reports NOTHING. Without that, opening the page would light up every
skill you have ever used, all at once, and each light would be a lie about the
present. Only bytes written after Kit started count.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import config

PROJECTS = Path.home() / ".claude" / "projects"
# Where the .bat launchers announce themselves (runs_dir in kit.config.json).
# A desktop icon reaches no transcript, so without this an icon-started tool
# could never light - see _launches() and mark_launchers.py.
RUNS = config.RUNS_DIR

# Same patterns the two miners already use. Imported rather than re-typed
# would be better, but usage.py and programs.py each compile their own for
# their own corpus walk; these are deliberately identical and must stay so.
SKILL_RE = re.compile(rb'"skill"\s*:\s*"([a-zA-Z0-9:_-]+)"')
CMD_RE = re.compile(rb'"command"\s*:\s*"((?:[^"\\]|\\.){0,4000})"')
# Group 1 is the whole path as typed, group 2 the bare stem. The path used to
# be matched and thrown away, which was fine while only the Progs tab consumed
# this - a program is unique there. It is NOT fine for the Tools tab: a stem
# like `serve` can live in several tool folders, so a stem alone would light
# three cards and be wrong about two of them.
# ⚠️ The opening quote may arrive BACKSLASH-ESCAPED. These bytes come out of a
# JSON transcript, so `python "C:/x/scan.py"` is stored as
# `python \"C:/x/scan.py\"` - and a bare ["']? could not step over the
# backslash, so the path stopped dead at the quote and NOTHING matched. Every
# quoted invocation was invisible to the whole feature, in every tab, since it
# was written. Found 2026-08-24 by running one and watching nothing light up.
RUN_RE = re.compile(
    rb"(?<![.\w])(?:python3?|py)\s+(?:-[\w]+\s+)*(?:\\?[\"'])?"
    rb"([^\"'\n;|&]{0,120}?([A-Za-z0-9_]+)\.py)\b")

RELIST_EVERY = 20.0      # seconds between directory walks
KEEP = 90.0              # how long an event stays "recent"
MAX_DELTA = 4_000_000    # bytes read from one file in one poll


class Watcher:
    """Tails the transcript corpus. One instance, polled by serve.py."""

    def __init__(self) -> None:
        self.marks: dict[str, int] = {}
        self.files: list[Path] = []
        self.listed = 0.0
        self.events: list[dict] = []
        self.primed = False
        self._hooks: tuple[set[str], set[str]] = (set(), set())
        self._hooks_at = 0.0
        self.run_marks: dict[str, float] = {}

    def _registered_hooks(self) -> tuple[set[str], set[str]]:
        """(normalised paths, bare stems) of every registered hook.

        Re-read on the same clock as the directory walk, so adding a hook takes
        effect without restarting the server. A failure here must never stop
        the watcher: an unreadable settings.json means "filter nothing", which
        shows a few extra events, rather than "crash", which shows none.
        """
        now = time.time()
        if now - self._hooks_at < RELIST_EVERY and self._hooks[0]:
            return self._hooks
        self._hooks_at = now
        paths: set[str] = set()
        stems: set[str] = set()
        try:
            import hooks as hooklib
            regs, _ = hooklib.registrations()
            for r in regs:
                s = r.get("script")
                if not s:
                    continue
                s = s.replace("\\", "/").lower()
                paths.add(s)
                stems.add(s.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        except Exception:
            return self._hooks
        self._hooks = (paths, stems)
        return self._hooks

    def _launches(self) -> list[dict]:
        """Tools started the NORMAL way - by desktop icon or by the .bat.

        Nothing about a double-clicked icon reaches a transcript, so the
        launchers announce themselves instead: each one overwrites
        _engine/runs/<Tool>.run, and the file's MTIME is the whole signal.
        No timestamp is written, so there is no locale format to parse - a
        .bat cannot print an ISO date without help, and %DATE% is whatever
        the machine says it is.

        The event carries the marker's own mtime, NOT now(). A tool started
        four minutes before the page was opened must read as four minutes
        old; stamping it "now" is the same lie the server-clock design exists
        to prevent.
        """
        out: list[dict] = []
        try:
            entries = list(RUNS.iterdir()) if RUNS.is_dir() else []
        except OSError:
            return out
        for f in entries:
            if f.suffix.lower() != ".run":
                continue
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            was = self.run_marks.get(f.name)
            self.run_marks[f.name] = mt
            # Priming is handled by _prime(); reaching here with no mark means
            # a launcher that has never been seen, which IS new activity.
            if was is not None and mt <= was:
                continue
            if time.time() - mt > KEEP:
                continue      # a marker left over from before Kit started
            out.append({"kind": "tool", "name": f.stem, "at": mt})
        return out

    def _is_hook(self, stem: str, path: str) -> bool:
        """Is this command the harness running a hook, rather than you?"""
        paths, stems = self._registered_hooks()
        p = path.replace("\\", "/").lower()
        if any(p.endswith(h) or h.endswith(p) for h in paths):
            return True
        # A bare `python wrap_reminder.py` carries no folder to match on. A
        # registered stem with no path is the harness far more often than it is
        # you, and Progs lists every hook anyway - so nothing is lost.
        return "/" not in p and stem.lower() in stems

    def _relist(self) -> None:
        now = time.time()
        if now - self.listed < RELIST_EVERY and self.files:
            return
        self.listed = now
        # The walk costs ~350ms over 203 files; the stat-only pass between
        # walks costs ~15ms. Doing the walk every poll would make the page's
        # 2-second heartbeat the most expensive thing on the machine.
        try:
            self.files = list(PROJECTS.rglob("*.jsonl")) if PROJECTS.is_dir() else []
        except OSError:
            self.files = []

    def _prime(self) -> None:
        """Record where every file ENDS. See the module docstring: without
        this, the first poll replays the entire corpus as if it just happened."""
        for f in self.files:
            try:
                self.marks[str(f)] = f.stat().st_size
            except OSError:
                continue
        # The launch markers prime too, and for the same reason. Every one of
        # them exists on disk from the last time that tool was started, so
        # without this, opening Kit lights every tool you have ever launched.
        try:
            for f in (RUNS.iterdir() if RUNS.is_dir() else []):
                if f.suffix.lower() == ".run":
                    try:
                        self.run_marks[f.name] = f.stat().st_mtime
                    except OSError:
                        continue
        except OSError:
            pass
        self.primed = True

    def poll(self) -> list[dict]:
        self._relist()
        if not self.primed:
            self._prime()
            return []

        now = time.time()
        fresh: list[dict] = self._launches()

        for f in self.files:
            key = str(f)
            try:
                size = f.stat().st_size
            except OSError:
                continue
            was = self.marks.get(key)
            if was is None:
                # A session that started after Kit did. Its whole file is new
                # activity, so read it rather than priming past it.
                was = 0
            if size <= was:
                # Truncated or rewritten - resync rather than read backwards.
                self.marks[key] = size
                continue

            start = max(was, size - MAX_DELTA)
            try:
                with open(f, "rb") as fh:
                    fh.seek(start)
                    blob = fh.read(size - start)
            except OSError:
                continue
            self.marks[key] = size

            for m in SKILL_RE.findall(blob):
                fresh.append({"kind": "skill",
                              "name": m.decode("utf-8", "replace").split(":")[-1],
                              "at": now})
            # ⚠️ Not every "command" in a transcript is something you ran. The
            # harness records its own hook executions at the ROOT of a line as
            # hookInfos[{command, durationMs}], and CMD_RE - a regex over raw
            # bytes, which cannot see JSON structure - matches those exactly as
            # readily as a Bash tool call.
            #
            # Discovered 2026-08-24: four hooks were showing up as PROGRAM
            # events (precisely the Stop hooks) 194 times each, i.e. once per
            # turn. Harmless in the Progs tab, actively wrong in the Tools tab:
            # a registered hook that LIVES IN a rostered tool would light that
            # tool as though it had been used.
            #
            # Filtered by REGISTRATION rather than by folder, so adding a hook
            # anywhere keeps this correct with no second list to maintain.
            for cmd in CMD_RE.findall(blob):
                # Keep the longest path seen for a stem in this batch. A bare
                # `python serve.py` carries no folder at all, so path is often
                # just the filename - the consumer has to cope with that
                # rather than assume it can always resolve one.
                paths: dict[str, str] = {}
                for full, stem in RUN_RE.findall(cmd):
                    s = stem.decode("utf-8", "replace")
                    p = full.decode("utf-8", "replace").replace("\\\\", "/") \
                            .replace("\\", "/").strip()
                    if len(p) > len(paths.get(s, "")):
                        paths[s] = p
                for stem, p in paths.items():
                    if self._is_hook(stem, p):
                        continue
                    fresh.append({"kind": "program", "name": stem,
                                  "path": p, "at": now})

        if fresh:
            # Collapse duplicates inside one poll: a skill named three times in
            # the same batch of bytes is one thing happening, not three.
            seen = set()
            keep = []
            for e in fresh:
                k = (e["kind"], e["name"])
                if k not in seen:
                    seen.add(k)
                    keep.append(e)
            self.events.extend(keep)
            fresh = keep

        self.events = [e for e in self.events if now - e["at"] < KEEP]
        return fresh

    def recent(self) -> list[dict]:
        now = time.time()
        self.events = [e for e in self.events if now - e["at"] < KEEP]
        return self.events


def main() -> None:
    w = Watcher()
    print("watching %s — Ctrl-C to stop" % PROJECTS)
    print("(priming: existing history is skipped, only new activity shows)")
    w.poll()
    try:
        while True:
            for e in w.poll():
                print("  %s  %-8s %s" % (time.strftime("%H:%M:%S"), e["kind"], e["name"]))
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
