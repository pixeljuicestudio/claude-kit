#!/usr/bin/env python3
"""RUNNING — which tools have a live process right now.

⚠️ This is a DIFFERENT QUESTION from the live glow, and conflating them is
what produced "davinci bridge is running but not lit?".

The glow answers *did something happen recently* — a pulse for twelve seconds
then a fade over a minute. That is the right shape for a skill firing or a
script running, both of which are events. It is the wrong shape entirely for a
bridge, a server or a panel, which are STATES: they are started once and then
stay up for hours. By the time you look at Kit, the launch is long outside the
window and the tool is very much in use.

The DaVinci bridge is the clean example. It had six live processes — the MCP
bridge plus CursorBridge under fuscript — and Kit could not see any of them,
because nothing had *happened* recently.

How it works: enumerate every process with a command line, and attribute one
to a tool when its command line names that tool's folder as a path SEGMENT.
Segment matching, not substring — the same rule tools.py uses, and for the
same reason: `hyperframes` is a prefix of `hyperframes-tools`, so a substring
test would credit one tool's processes to another.

Cost is the reason this is not in tools.py. The CIM query takes roughly half a
second, which is fine on a ten-second timer in the server's watcher thread and
far too slow for the two-second live poll or for a page build.

Honest limits, stated because a status light that means less than it looks
like is worse than none:

  * A tool with no long-lived process can never show as running. That is
    correct — a script pack has no "up" state.
  * A GUI app started BY a tool is not the tool. A launcher that starts an
    editor whose command line says nothing about the folder shows running
    only while its own bridge or launcher is alive.
  * This says a process EXISTS, not that it is healthy or listening. A hung
    server still counts as running, because from here it is indistinguishable
    from a working one.

CONNECTED goes one question further, and only for the tools where a real
answer exists (see CONNECT_CHECKS below). An MCP bridge registered globally in
~/.claude.json is spawned by every open Claude Code session at startup whether
or not the app it bridges is even open — "running" is true almost all the
time, which is what made it read as a lie. "Connected" is reserved for a
signal that cannot be explained by Claude Code's own housekeeping.

Process listing: PowerShell CIM on Windows, `ps -eo pid=,ppid=,args=`
elsewhere. With no tools_root or an empty roster the probe does nothing.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import time

import tools as toollib

IS_WINDOWS = sys.platform == "win32"

# One line per process, tab-separated, so parsing needs no JSON and no
# PowerShell object formatting (which wraps long lines and would split a path).
_PS = (
    "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
    "Where-Object { $_.CommandLine } | "
    "ForEach-Object { $_.ProcessId.ToString() + \"`t\" + "
    "$_.ParentProcessId.ToString() + \"`t\" + "
    "($_.CommandLine -replace \"`t|`r|`n\", ' ') }"
)

# How far up the parent chain to look before giving up. Three is enough for
# `Launch.bat` -> `cmd /c "cd X && python Y"` -> `python Y` and short enough
# that a deep unrelated tree cannot drag a process into the wrong tool.
MAX_ANCESTRY = 3

TIMEOUT = 25.0


def _mentions(text: str, name: str) -> bool:
    """Same segment rule as tools.py — see that module for why substrings lie."""
    pat = r"[\\/]" + re.escape(name) + r"(?![A-Za-z0-9_.\-])"
    return re.search(pat, text, re.I) is not None


_SKIP = {"node_modules", ".venv", "venv", "__pycache__", ".git", "dist",
         "build", "out", ".next", "site-packages"}
_SCRIPTS: dict[str, str] | None = None


def script_owners(refresh: bool = False) -> dict[str, str]:
    """{script filename: tool} for every script under a rostered tool.

    ⚠️ The last resort, and the only thing that can attribute a process whose
    command line carries no path at all. A server started as
    `cd server && python app_server.py` has exactly that command line, its
    parent is `cmd /c` with the same text, and the grandparent has already
    exited because the .bat used `start`, which detaches. So there is no path
    anywhere in the process tree — but `app_server.py` is a file that exists
    in exactly one tool, and THAT is derivable from disk.

    A filename appearing under two tools is dropped rather than guessed at,
    the same rule the Tools glow applies to ambiguous program stems.
    """
    global _SCRIPTS
    if _SCRIPTS is not None and not refresh:
        return _SCRIPTS
    seen: dict[str, set[str]] = {}

    def walk(d, tool, depth):
        if depth > 3:
            return
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.is_dir():
                if e.name not in _SKIP and not e.name.startswith("."):
                    walk(e, tool, depth + 1)
            elif e.suffix.lower() in (".py", ".js", ".mjs"):
                seen.setdefault(e.name.lower(), set()).add(tool)

    if toollib.ROOT is None:
        _SCRIPTS = {}
        return _SCRIPTS
    for name in toollib.ROSTER:
        d = toollib.ROOT / name
        if d.is_dir():
            walk(d, name, 0)
    _SCRIPTS = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}
    return _SCRIPTS


_SCRIPT_RE = re.compile(r"[\w.\-]+\.(?:py|js|mjs)\b", re.I)


def _tcp_alive(host: str, port: int, timeout: float = 0.3) -> bool:
    """A connect attempt, nothing more — no data sent, no side effects."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# The stronger-than-"running" check. Each answers "is there a real signal past
# the idle MCP shim", not "does Kit think the tool exists". Declared per tool
# in tools.json under "connect":
#
#   "connect": {
#     "my-mcp-bridge": {"tcp": 9876},          a local port the real backend
#                                              opens only once it is usable
#     "my-panel":      {"process": "app.exe"}  a process that must be up for
#                                              the tool to be doing anything
#   }
#
# Why it exists: an MCP server registered globally is spawned by every Claude
# Code session at startup whether or not the app it bridges is even open, so
# "running" is true almost all the time and reads as a lie. A TCP connect is
# cheap, instant and has no side effects; a process check is not proof, but a
# real step past "some node.exe exists somewhere".
#
# A tool with no entry just never earns the stronger claim. That is the
# correct default, not a gap to fill in for its own sake.
def _connect_checks(path=None) -> dict:
    import json
    try:
        raw = json.loads((path or toollib.ROSTER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    spec = raw.get("connect") if isinstance(raw, dict) else None
    out = {}
    for name, rule in (spec or {}).items() if isinstance(spec, dict) else []:
        if not isinstance(rule, dict):
            continue
        if isinstance(rule.get("tcp"), int):
            port = rule["tcp"]
            out[name] = lambda cmds, port=port: _tcp_alive("127.0.0.1", port)
        elif isinstance(rule.get("process"), str) and rule["process"]:
            needle = rule["process"].lower()
            out[name] = lambda cmds, needle=needle: any(needle in c.lower() for c in cmds)
    return out


CONNECT_CHECKS = _connect_checks()


def _list_processes() -> tuple[str, str]:
    """(stdout as 'pid<TAB>ppid<TAB>cmdline' lines, error string)."""
    if IS_WINDOWS:
        cmd = ["powershell", "-NoProfile", "-Command", _PS]
    else:
        cmd = ["ps", "-eo", "pid=,ppid=,args="]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return "", "process probe failed: %s" % e
    if r.returncode != 0:
        return "", "process probe exited %d" % r.returncode
    if IS_WINDOWS:
        return r.stdout, ""
    lines = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            lines.append("\t".join(parts))
    return "\n".join(lines), ""


def probe() -> tuple[dict[str, list[int]], set[str], str]:
    """{tool: [pids]}, {tool} genuinely connected, and an error string.

    A failure returns ({}, set(), reason) rather than raising. The status
    column going blank is survivable; the watcher thread dying is not.
    """
    # Nothing to attribute to: skip the (slow) process enumeration entirely.
    if toollib.ROOT is None or not toollib.ROSTER:
        return {}, set(), ""
    stdout, err = _list_processes()
    if err:
        return {}, set(), err

    roster = list(toollib.ROSTER)
    # A command line must name the tools root before a segment match is
    # worth trying - otherwise an unrelated path that happens to contain a
    # folder of the same name would be credited to the tool.
    root = str(toollib.ROOT).replace("\\", "/").rstrip("/").upper()
    procs: dict[int, tuple[int, str]] = {}
    for line in stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        try:
            pid, ppid = int(parts[0].strip()), int(parts[1].strip())
        except ValueError:
            continue
        procs[pid] = (ppid, parts[2].replace("\\", "/"))

    def owner(cmd: str) -> str | None:
        for name in roster:
            if _mentions(cmd, name):
                return name
        return None

    out: dict[str, list[int]] = {}
    for pid, (ppid, cmd) in procs.items():
        name = owner(cmd) if root in cmd.upper() else None
        # ⚠️ A process started with a RELATIVE path names no folder at all.
        # One tool's server was launched as
        #   cmd /c "cd server && python app_server.py"
        # so its command line is literally `python app_server.py` - it was
        # listening on its port and showed as not running. The attribution lives
        # in its PARENT, whose command line is the .bat's full path. So walk
        # up when the process cannot speak for itself.
        hops, cur = 0, ppid
        while name is None and hops < MAX_ANCESTRY and cur in procs:
            pcmd = procs[cur][1]
            if root in pcmd.upper():
                name = owner(pcmd)
            cur = procs[cur][0]
            hops += 1
        if name is None:
            # Last resort: the script's own filename. See script_owners().
            owners = script_owners()
            for m in _SCRIPT_RE.findall(cmd):
                got = owners.get(m.lower())
                if got:
                    name = got
                    break
        if name:
            out.setdefault(name, []).append(pid)
    for v in out.values():
        v.sort()

    all_cmds = [cmd for _, cmd in procs.values()]
    connected = {name for name, check in CONNECT_CHECKS.items()
                 if name in out and check(all_cmds)}
    return out, connected, ""


class Probe:
    """Caches the result, because half a second is too slow to ask often."""

    EVERY = 10.0

    def __init__(self) -> None:
        self.at = 0.0
        self.map: dict[str, list[int]] = {}
        self.connected: set[str] = set()
        self.error = ""

    def get(self, force: bool = False) -> dict[str, list[int]]:
        now = time.time()
        if not force and now - self.at < self.EVERY and self.at:
            return self.map
        self.at = now
        self.map, self.connected, self.error = probe()
        return self.map


def main() -> None:
    got, connected, err = probe()
    if err:
        print("!! " + err)
        raise SystemExit(2)
    print("=== RUNNING NOW ===")
    if not got:
        print("  nothing on the roster has a live process")
        return
    for name in sorted(got):
        pids = got[name]
        tag = "  [CONNECTED]" if name in connected else ""
        print("  %-22s %d process%s  %s%s" % (
            name, len(pids), "" if len(pids) == 1 else "es",
            ", ".join(str(p) for p in pids[:6]), tag))
    quiet = [n for n in toollib.ROSTER if n not in got]
    print("\n  not running (%d): %s" % (len(quiet), ", ".join(sorted(quiet))))


if __name__ == "__main__":
    main()
