#!/usr/bin/env python3
"""
Kit — serves the page so the Auto/Manual toggles, stars and tags actually work.

    python serve.py                      http://127.0.0.1:8712/
    python serve.py --port 8800          another port (or KIT_PORT=8800)
    python serve.py --no-browser         do not open a browser tab

A page opened from disk cannot write files, so the toggle needs something
listening. This is that: a localhost-only server that rebuilds the inventory on
request and applies exactly one kind of change to your skills - flipping one
between Auto and Manual by calling mode.py.

WHAT IT WILL NOT DO, and why the toggle is disabled rather than hidden for these:

  * Skills from extra_skill_dirs. They run from wherever they are installed or
    uploaded, so editing this copy may change nothing that fires. A toggle
    that silently did nothing would be worse than no toggle.
  * Router-locked modules (hyperframes-*). disable-model-invocation blocks
    Claude from loading them at all, which breaks the parent that routes to them.
  * Anything off. It is not loaded; there is nothing to switch.

Bound to 127.0.0.1 so nothing outside this machine can reach it. Skill names are
matched against the scanned inventory rather than trusted, so a crafted request
cannot reach a path that is not already a skill on this machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import build
import config
import groups as grouplib
import hooks as hooklib
import live
import mode
import prompts as promptlib
import running as runlib
import scan
import tools as toollib

WATCHER = live.Watcher()
# "Is it up right now" is a different question from "did something just fire",
# and the Tools tab is full of bridges, servers and panels for which only the
# first one has an answer. See running.py.
PROBE = runlib.Probe()

HOST, PORT = "127.0.0.1", config.port()


def open_path(p: Path, cwd: Path | None = None) -> None:
    """Open a file with the OS's default handler. Raises OSError on failure.

    os.startfile is Windows-only; macOS has `open` and Linux `xdg-open`.
    """
    if sys.platform == "win32":
        try:
            os.startfile(str(p), cwd=str(cwd)) if cwd else os.startfile(str(p))  # noqa: S606
        except TypeError:
            # cwd= arrives in 3.10; older Pythons get the old behaviour rather
            # than no launch at all.
            os.startfile(str(p))                                             # noqa: S606
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(p)], cwd=str(cwd) if cwd else None,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def toggleable() -> dict[str, dict]:
    """{name: {path, locked}} for skills whose mode this server may change."""
    found = mode.skills()
    mods = mode.families(set(found))
    return {n: {"path": p, "locked": n in mods} for n, p in found.items()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):            # keep the console for our own output
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        blob = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _trusted(self) -> bool:
        """Only Kit's own page may talk to Kit.

        Binding to 127.0.0.1 keeps other machines out, but not other web
        pages: any site open in the same browser can POST to localhost, and
        a DNS-rebinding site can even read the replies. So the Host header
        must name this server (defeats rebinding), and a browser request
        that carries an Origin must come from this server's own page
        (defeats cross-site POSTs). Non-browser clients send no Origin and
        are already running on this machine.
        """
        ours = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.headers.get("Host", "") not in ours:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in ours}

    def do_GET(self):
        if not self._trusted():
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            html = build.render_html(live=True)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/ppull"):
            # How did the re-pull go? Polled by the Refresh button.
            with PULL_LOCK:
                self._send(200, {"ok": True, **PULL})

        elif self.path.startswith("/api/live"):
            # Reads what the background thread has already collected; it does
            # NOT tail the files itself. That split matters: the browser tab
            # stops polling when it is hidden, and if tailing happened here,
            # ten minutes in another window would come back as one burst of
            # events all stamped "now". The thread advances the marks on real
            # time, so every event carries the moment it actually happened.
            #
            # A poll and not an event stream: HTTPServer handles one request
            # at a time, so a long-lived SSE connection would hold the only
            # thread and every toggle on the page would hang behind it.
            try:
                fired = WATCHER.recent()
            except Exception:
                # A watcher fault must never take down the inventory. The page
                # treats a bad reply as "nothing fired".
                fired = []
            # Cached by the Probe on a ten-second clock, refreshed in the same
            # background thread. Reading it here never enumerates processes,
            # which would put 0.7s into every two-second poll.
            try:
                up = sorted(PROBE.map)
            except Exception:
                up = []
            try:
                conn = sorted(PROBE.connected)
            except Exception:
                conn = []
            self._send(200, {"ok": True, "events": fired, "at": time.time(),
                             "running": up, "connected": conn})
        else:
            self._send(404, {"error": "not found"})

    def _fav(self):
        """Star or unstar a skill. Writes favourites.json, then re-reads it.

        Unlike /api/mode this accepts ANY scanned skill, including off and
        plugin ones - a favourite is a bookmark, not a state change, and the
        140 you cannot toggle are exactly the ones worth bookmarking.
        """
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            name, on = req["name"], bool(req["on"])
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        # Never trust the name: it must be a skill this machine actually has.
        items, _ = scan.scan()
        if name not in {i["name"] for i in items}:
            return self._send(404, {"ok": False, "error": f"{name} is not a known skill"})

        favs = build.load_favs()
        favs.add(name) if on else favs.discard(name)
        try:
            build.save_favs(favs)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        # Re-read rather than trust the write - same rule as /api/mode.
        actual = name in build.load_favs()
        if actual != on:
            return self._send(500, {
                "ok": False, "error": "write did not take - favourites.json still disagrees",
            })
        return self._send(200, {"ok": True, "name": name, "fav": actual})

    def _tag(self):
        """Set (or clear) a skill's tags. Writes tags.json, then re-reads it -
        same rule as /api/fav: trust the file, not the write call.

        Skills only, like /api/fav: the +Tag button only exists on skill
        cards, so the inventory to validate against is the same one.
        """
        try:
            req = self._read()
            name, tags = req["name"], req["tags"]
            if not isinstance(name, str) or not isinstance(tags, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        items, _ = scan.scan()
        if name not in {i["name"] for i in items}:
            return self._send(404, {"ok": False, "error": f"{name} is not a known skill"})

        clean = sorted({t.strip() for t in tags if isinstance(t, str) and t.strip()})
        current = build.load_tags()
        if clean:
            current[name] = clean
        else:
            current.pop(name, None)
        try:
            build.save_tags(current)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        actual = build.load_tags().get(name, [])
        if actual != clean:
            return self._send(500, {
                "ok": False, "error": "write did not take - tags.json still disagrees",
            })
        return self._send(200, {"ok": True, "name": name, "tags": actual})

    def _read(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _pfav(self):
        """Star or unstar a PROMPT. Separate list from the skills' favourites.

        Sharing one file looked obvious and is wrong twice: favourites.json is
        keyed by name, and /api/fav validates every name against the scanned
        skill inventory - so a starred prompt would come back "not a known
        skill". Two inventories, two lists.
        """
        try:
            req = self._read()
            name, on = req["name"], bool(req["on"])
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        # Never trust the name: it must be a prompt in the exported cache.
        items, _ = promptlib.scan()
        if name not in {i["name"] for i in items}:
            return self._send(404, {"ok": False, "error": f"{name} is not a known prompt"})

        favs = promptlib.load_favs()
        favs.add(name) if on else favs.discard(name)
        try:
            promptlib.save_favs(favs)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        # Re-read rather than trust the write - same rule as /api/mode.
        actual = name in promptlib.load_favs()
        if actual != on:
            return self._send(500, {
                "ok": False,
                "error": "write did not take - prompt_favourites.json still disagrees",
            })
        return self._send(200, {"ok": True, "name": name, "fav": actual})

    def _pcopy(self):
        """Record that a prompt was copied out of Kit.

        This is the ONLY usage signal a prompt has. A skill's use is mined
        from transcripts because invoking one leaves a record; a prompt is
        pasted somewhere else, off this machine, and leaves none. So the copy
        is what gets counted, and the page labels it "copied", never "used".
        """
        try:
            name = self._read()["name"]
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        items, _ = promptlib.scan()
        if name not in {i["name"] for i in items}:
            return self._send(404, {"ok": False, "error": f"{name} is not a known prompt"})
        try:
            rec = promptlib.bump(name)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})
        return self._send(200, {"ok": True, "name": name, "n": rec["n"]})

    def _tfav(self):
        """Star or unstar a TOOL. A fourth list, for the third time the same
        reason: each /api/*fav validates names against the inventory it owns,
        so a tool posted to /api/fav would be refused as "not a known skill".
        """
        try:
            req = self._read()
            name, on = req["name"], bool(req["on"])
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        # Validated against the ROSTER, not against what happens to be on
        # disk: a rostered folder that has gone missing is still a tool you
        # are allowed to have starred, and un-starring it must keep working.
        if name not in set(toollib.ROSTER):
            return self._send(404, {"ok": False, "error": f"{name} is not a rostered tool"})

        favs = toollib.load_favs()
        favs.add(name) if on else favs.discard(name)
        try:
            toollib.save_favs(favs)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        actual = name in toollib.load_favs()
        if actual != on:
            return self._send(500, {
                "ok": False,
                "error": "write did not take - tool_favourites.json still disagrees",
            })
        return self._send(200, {"ok": True, "name": name, "fav": actual})

    def _hfav(self):
        """Star or unstar a HOOK.

        Names here can carry a slash (`narrator/hook`), because a hook outside
        the hooks folder is labelled by its parent - `hook` on its own
        identifies nothing. That is why the valid set is taken from scan()
        rather than from a directory listing.
        """
        try:
            req = self._read()
            name, on = req["name"], bool(req["on"])
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        rows, _ = hooklib.scan()
        known = {r["name"] for r in rows}
        if name not in known:
            return self._send(404, {"ok": False, "error": f"{name} is not a known hook"})

        favs = hooklib.load_favs()
        favs.add(name) if on else favs.discard(name)
        try:
            hooklib.save_favs(favs, known)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        actual = name in hooklib.load_favs()
        if actual != on:
            return self._send(500, {
                "ok": False,
                "error": "write did not take - hook_favourites.json still disagrees",
            })
        return self._send(200, {"ok": True, "name": name, "fav": actual})

    def _ptag(self):
        """Set (or clear) a PROMPT's tags. Same rule as /api/tag: validated
        against the scanned prompt cache, written to prompt_ktags.json, then
        re-read - never trust the write call over the file."""
        try:
            req = self._read()
            name, tags = req["name"], req["tags"]
            if not isinstance(name, str) or not isinstance(tags, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        items, _ = promptlib.scan()
        if name not in {i["name"] for i in items}:
            return self._send(404, {"ok": False, "error": f"{name} is not a known prompt"})

        clean = sorted({t.strip() for t in tags if isinstance(t, str) and t.strip()})
        current = promptlib.load_ktags()
        if clean:
            current[name] = clean
        else:
            current.pop(name, None)
        try:
            promptlib.save_ktags(current)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        actual = promptlib.load_ktags().get(name, [])
        if actual != clean:
            return self._send(500, {
                "ok": False,
                "error": "write did not take - prompt_ktags.json still disagrees",
            })
        return self._send(200, {"ok": True, "name": name, "tags": actual})

    def _ttag(self):
        """Set (or clear) a TOOL's tags. Validated against the ROSTER, like
        /api/tfav - a rostered tool missing from disk can still be tagged."""
        try:
            req = self._read()
            name, tags = req["name"], req["tags"]
            if not isinstance(name, str) or not isinstance(tags, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        if name not in set(toollib.ROSTER):
            return self._send(404, {"ok": False, "error": f"{name} is not a rostered tool"})

        clean = sorted({t.strip() for t in tags if isinstance(t, str) and t.strip()})
        current = toollib.load_tags()
        if clean:
            current[name] = clean
        else:
            current.pop(name, None)
        try:
            toollib.save_tags(current)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        actual = toollib.load_tags().get(name, [])
        if actual != clean:
            return self._send(500, {
                "ok": False, "error": "write did not take - tool_tags.json still disagrees",
            })
        return self._send(200, {"ok": True, "name": name, "tags": actual})

    def _htag(self):
        """Set (or clear) a HOOK's tags. Validated against scan()'s own
        names, same as /api/hfav - the qualified `parent/stem` label for a
        hook outside hooks/, the bare stem otherwise."""
        try:
            req = self._read()
            name, tags = req["name"], req["tags"]
            if not isinstance(name, str) or not isinstance(tags, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        rows, _ = hooklib.scan()
        known = {r["name"] for r in rows}
        if name not in known:
            return self._send(404, {"ok": False, "error": f"{name} is not a known hook"})

        clean = sorted({t.strip() for t in tags if isinstance(t, str) and t.strip()})
        current = hooklib.load_tags()
        if clean:
            current[name] = clean
        else:
            current.pop(name, None)
        try:
            hooklib.save_tags(current, known)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        actual = hooklib.load_tags().get(name, [])
        if actual != clean:
            return self._send(500, {
                "ok": False, "error": "write did not take - hook_tags.json still disagrees",
            })
        return self._send(200, {"ok": True, "name": name, "tags": actual})

    def _known_member(self, kind, name):
        """Is (kind, name) a real, currently-known item? Checked against
        three separate inventories because a group does not commit to one
        kind up front - the whole reason it exists is to mix them."""
        if kind == "skill":
            items, _ = scan.scan()
            return name in {i["name"] for i in items}
        if kind == "prompt":
            items, _ = promptlib.scan()
            return name in {i["name"] for i in items}
        if kind == "tool":
            return name in set(toollib.ROSTER)
        return False

    def _group_create(self):
        """New empty group. Members are added afterwards, one at a time,
        from wherever the item already lives - a group starts as just a
        name."""
        try:
            name = self._read()["name"]
            if not isinstance(name, str) or not name.strip():
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        groups = grouplib.load_groups()
        gid = grouplib.new_id(groups)
        groups[gid] = {"name": name.strip(), "members": []}
        try:
            grouplib.save_groups(groups)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        actual = grouplib.load_groups().get(gid)
        if not actual:
            return self._send(500, {
                "ok": False, "error": "write did not take - groups.json still disagrees",
            })
        return self._send(200, {"ok": True, "id": gid, "name": actual["name"],
                                "members": actual["members"]})

    def _group_rename(self):
        try:
            req = self._read()
            gid, name = req["id"], req["name"]
            if not isinstance(gid, str) or not isinstance(name, str) or not name.strip():
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        groups = grouplib.load_groups()
        if gid not in groups:
            return self._send(404, {"ok": False, "error": "no such group"})
        groups[gid]["name"] = name.strip()
        try:
            grouplib.save_groups(groups)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        actual = grouplib.load_groups().get(gid, {})
        if actual.get("name") != name.strip():
            return self._send(500, {
                "ok": False, "error": "write did not take - groups.json still disagrees",
            })
        return self._send(200, {"ok": True, "id": gid, "name": actual["name"]})

    def _group_delete(self):
        try:
            gid = self._read()["id"]
            if not isinstance(gid, str):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        groups = grouplib.load_groups()
        if gid not in groups:
            return self._send(404, {"ok": False, "error": "no such group"})
        del groups[gid]
        try:
            grouplib.save_groups(groups)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        if gid in grouplib.load_groups():
            return self._send(500, {
                "ok": False, "error": "write did not take - groups.json still disagrees",
            })
        return self._send(200, {"ok": True, "id": gid})

    def _group_member(self):
        """Add or remove ONE (kind, name) from ONE group. The member must be
        a real, currently-known item in its own inventory when ADDING -
        removing is always allowed, since a group must be able to drop a
        member that has since been deleted from its own inventory."""
        try:
            req = self._read()
            gid, kind, name = req["id"], req["kind"], req["name"]
            on = bool(req["on"])
            if (not isinstance(gid, str) or kind not in grouplib.KINDS
                    or not isinstance(name, str)):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        groups = grouplib.load_groups()
        if gid not in groups:
            return self._send(404, {"ok": False, "error": "no such group"})
        if on and not self._known_member(kind, name):
            return self._send(404, {"ok": False, "error": f"{name} is not a known {kind}"})

        members = groups[gid].get("members", [])
        key = {"kind": kind, "name": name}
        if on:
            if key not in members:
                members.append(key)
        else:
            members = [m for m in members
                      if not (m.get("kind") == kind and m.get("name") == name)]
        groups[gid]["members"] = members
        try:
            grouplib.save_groups(groups)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        actual = grouplib.load_groups().get(gid, {}).get("members", [])
        is_member = {"kind": kind, "name": name} in actual
        if is_member != on:
            return self._send(500, {
                "ok": False, "error": "write did not take - groups.json still disagrees",
            })
        return self._send(200, {"ok": True, "id": gid, "members": actual})

    def _openref(self):
        """Play a prompt's reference video in the default player.

        A browser cannot follow a file:// link from an http:// page - it is
        blocked, silently, which would look like a broken button. The server is
        already local, so it opens the file itself.

        ⚠️ The path is NEVER taken from the request. The client sends a prompt
        NAME; the server resolves the file through the same scan() the page was
        built from. A path posted by a page is a path an attacker can choose,
        and os.startfile on an arbitrary path is arbitrary execution.
        """
        try:
            name = self._read()["name"]
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        rows, _ = promptlib.scan()
        row = next((r for r in rows if r["name"] == name), None)
        if row is None:
            return self._send(404, {"ok": False, "error": f"{name} is not a known prompt"})
        path = row.get("reffile")
        if not path or promptlib.REFERENCE is None:
            return self._send(404, {"ok": False,
                                    "error": "no local copy of this reference"})
        p = Path(path)
        # Belt and braces: whatever scan() returned must still be a real file
        # inside the reference folder. A roster is data, and data can be wrong.
        try:
            inside = p.resolve().is_relative_to(promptlib.REFERENCE.resolve())
        except (OSError, ValueError):
            inside = False
        if not (p.is_file() and inside):
            return self._send(404, {"ok": False,
                                    "error": "the file is no longer there"})
        try:
            open_path(p)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})
        return self._send(200, {"ok": True, "opened": p.name})

    def _launch(self):
        """Start a rostered tool's launcher.

        Kit knew how every tool was started and could not start any of them -
        the `command` chip literally read "there is something runnable at the
        top level, but nothing launches it for you". This closes that, for the
        tools whose launcher is unambiguous.

        ⚠️ Same rule as _openref, and it is the important one: the path is
        NEVER taken from the request. The client sends a tool NAME; the server
        resolves it through the same scan() the page was built from, and only
        the .bat/.cmd/.exe that scan() already nominated can run. A path posted
        by a page is a path an attacker can choose, and os.startfile on an
        arbitrary path is arbitrary execution.
        """
        try:
            name = self._read()["name"]
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        rows, _ = toollib.scan()
        row = next((r for r in rows if r["name"] == name), None)
        if row is None:
            return self._send(404, {"ok": False, "error": f"{name} is not a rostered tool"})
        entry = row.get("launch")
        if not entry:
            return self._send(404, {"ok": False,
                                    "error": "nothing here is safe to launch blind - "
                                             "no .bat, .cmd or .exe at the top level"})

        if toollib.ROOT is None:
            return self._send(404, {"ok": False, "error": "tools_root is not configured"})
        p = Path(row["path"]) / entry
        # Belt and braces: whatever scan() named must still be a real file
        # inside the tools root. A roster is data, and data can be wrong.
        try:
            inside = p.resolve().is_relative_to(toollib.ROOT.resolve())
        except (OSError, ValueError):
            inside = False
        if not (p.is_file() and inside):
            return self._send(404, {"ok": False, "error": f"{entry} is no longer there"})
        try:
            # cwd matters: a launcher almost always assumes it runs from its
            # own folder, and would inherit the SERVER's cwd otherwise.
            open_path(p, cwd=p.parent)
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})
        return self._send(200, {"ok": True, "launched": entry})

    def do_POST(self):
        if not self._trusted():
            # Read the body anyway: closing on unread bytes resets the
            # connection on Windows and the caller sees an abort, not a 403.
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            return self._send(403, {"ok": False, "error": "forbidden"})
        if self.path == "/api/launch":
            return self._launch()
        if self.path == "/api/fav":
            return self._fav()
        if self.path == "/api/tag":
            return self._tag()
        if self.path == "/api/pfav":
            return self._pfav()
        if self.path == "/api/tfav":
            return self._tfav()
        if self.path == "/api/hfav":
            return self._hfav()
        if self.path == "/api/ptag":
            return self._ptag()
        if self.path == "/api/ttag":
            return self._ttag()
        if self.path == "/api/htag":
            return self._htag()
        if self.path == "/api/group-create":
            return self._group_create()
        if self.path == "/api/group-rename":
            return self._group_rename()
        if self.path == "/api/group-delete":
            return self._group_delete()
        if self.path == "/api/group-member":
            return self._group_member()
        if self.path == "/api/pcopy":
            return self._pcopy()
        if self.path == "/api/openref":
            return self._openref()
        if self.path == "/api/ppull":
            # Local prompt files are re-read on every page build; the optional
            # Notion rows are a cache, the one thing a running Kit can be wrong
            # about. This re-pulls it - the manual equivalent of restarting
            # the server, without restarting the server. With Notion not
            # configured it is a no-op that reports the local count.
            started = _start_pull()
            with PULL_LOCK:
                return self._send(200, {"ok": True, "started": started, **PULL})
        if self.path != "/api/mode":
            return self._send(404, {"error": "not found"})

        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            name, want = req["name"], req["mode"]
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})

        if want not in ("auto", "manual"):
            return self._send(400, {"ok": False, "error": "mode must be auto or manual"})

        # Never trust the name: it must be a skill this machine actually has.
        allowed = toggleable()
        info = allowed.get(name)
        if not info:
            return self._send(404, {
                "ok": False,
                "error": f"{name} is not a skill in ~/.claude/skills — "
                         f"repo and plugin skills cannot be switched from here",
            })
        if want == "manual" and info["locked"]:
            parent = name.split("-")[0]
            return self._send(409, {
                "ok": False,
                "error": f"'{parent}' routes to this module. Manual would stop Claude "
                         f"loading it and break the router.",
            })

        try:
            changed = mode.set_mode(info["path"], want == "manual")
        except OSError as exc:
            return self._send(500, {"ok": False, "error": str(exc)})

        # Re-read rather than trust the write: a write that took is not the
        # same as a write.
        actual = mode.mode_of(info["path"])
        if actual != want:
            return self._send(500, {
                "ok": False, "error": f"write did not take — file still reads {actual}",
            })

        if changed:
            _sync_mode_index()

        return self._send(200, {
            "ok": True, "name": name, "mode": actual, "changed": changed,
            "note": "Takes effect when you restart your Claude session.",
        })


CHECK_SKILLS = config.MODE_SYNC_SCRIPT


def _sync_mode_index():
    """Optional: tell an external skills index that a mode changed.

    If mode_sync_script is set in kit.config.json, it is run as
    `<python> <script> --fix-modes --quiet` after every successful toggle -
    for example to update a "fires how" column in a skills database, which
    otherwise drifts silently every time the toggle is flicked. Not
    configured, or the file is missing: skipped silently.

    Runs in a background thread and never reports failure to the caller. The
    toggle has already succeeded on disk by this point; making the switch look
    broken because some index was unreachable would be a lie about the thing
    the user actually asked for.
    """
    if CHECK_SKILLS is None or not CHECK_SKILLS.exists():
        return

    def run():
        try:
            subprocess.run(
                [sys.executable, str(CHECK_SKILLS), "--fix-modes", "--quiet"],
                capture_output=True, text=True, timeout=60,
                cwd=str(CHECK_SKILLS.parent))
        except (subprocess.TimeoutExpired, OSError):
            pass

    threading.Thread(target=run, daemon=True).start()


def _refresh_prompts() -> None:
    """Re-pull the optional Notion prompts in the background, once, at startup.

    Local prompt files are scanned on every build and cannot go stale, but
    prompts.json is a cache of Notion pages and silently ages. Adding a prompt
    in Notion and finding Kit still showing yesterday's list is exactly the
    drift this prevents. Without Notion configured this does nothing useful
    and costs one short subprocess.

    Fire and forget, on a daemon thread, for the same reason _sync_mode_index
    is: the page must open at once whether or not Notion answers. If the pull
    fails the cache is simply not replaced - and prompts.py refuses to
    overwrite a good cache with a bad pull, so a half-finished export cannot
    leave the tab emptier than it found it.

    The page is built per request, so a pull that lands after the first render
    shows up on the next one.
    """
    _start_pull()


# On-demand re-pull state, shared by the startup pull and the Refresh button so
# there is one implementation and one lock. The export takes ~7s, which is far
# too long to block a single-threaded HTTPServer: every other request, including
# the 2s live poll, would hang behind it. So it runs on a thread and the page
# asks how it went.
PULL = {"running": False, "at": 0.0, "count": 0, "error": "", "started": 0.0}
PULL_LOCK = threading.Lock()


def _start_pull() -> bool:
    """Kick off a re-pull. False if one is already in flight.

    Re-entrancy matters: the button is clickable while a pull is running, and
    two exports racing on the same cache file is exactly the kind of thing that
    leaves a half-written prompts.json. prompts.py writes atomically and
    refuses a bad pull, but not overlapping them is cheaper than relying on it.
    """
    with PULL_LOCK:
        if PULL["running"]:
            return False
        PULL.update(running=True, error="", started=time.time())

    def run():
        err, n = "", 0
        try:
            r = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("prompts.py")),
                 "--export", "--quiet"],
                capture_output=True, text=True, timeout=120,
                cwd=str(Path(__file__).parent))
            if r.returncode:
                # prompts.py exits non-zero when it REFUSES a bad pull - that is
                # the guard working, and the page must say so rather than
                # showing a silent no-op.
                err = (r.stderr or r.stdout or "export failed").strip()[-300:]
        except subprocess.TimeoutExpired:
            err = "Notion did not answer within 120s"
        except OSError as e:
            err = str(e)
        try:
            n = len(promptlib.scan()[0])
        except Exception:
            pass
        with PULL_LOCK:
            PULL.update(running=False, at=time.time(), count=n, error=err)

    threading.Thread(target=run, daemon=True).start()
    return True


def _start_watcher() -> None:
    """Tail the transcripts on our own clock, not the browser's.

    The page stops polling while its tab is hidden - correct, there is nothing
    to animate - but the FILES do not stop being written. If the tail only ran
    inside the request handler, coming back to the tab after ten minutes would
    read ten minutes of bytes at once and light every one of them as though it
    had just happened. Same failure as replaying history on the first poll,
    only later and harder to spot.

    So the marks advance here, every couple of seconds, whoever is watching.
    """
    def run():
        while True:
            try:
                WATCHER.poll()
            except Exception:
                pass          # a tail fault must never stop the server
            try:
                # Self-throttling: get() returns the cache untouched unless
                # ten seconds have passed, so this costs nothing most laps.
                PROBE.get()
            except Exception:
                pass          # so must a probe fault
            time.sleep(2.0)

    threading.Thread(target=run, daemon=True).start()


class Server(HTTPServer):
    # HTTPServer defaults allow_reuse_address on, which on Windows lets a SECOND
    # process bind the same port. Both then listen, requests land on whichever
    # answers first, and you get a stale page with no error anywhere - which is
    # exactly what happened while building this. Refuse instead.
    allow_reuse_address = False


def main(argv: list[str] | None = None) -> None:
    import argparse
    global PORT
    ap = argparse.ArgumentParser(description="Serve Kit on localhost.")
    ap.add_argument("--port", type=int, default=PORT,
                    help=f"port to listen on (default {PORT}; or set KIT_PORT)")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open a browser tab (or set KIT_NO_BROWSER=1)")
    args = ap.parse_args(argv)
    PORT = args.port
    no_browser = args.no_browser or bool(os.environ.get("KIT_NO_BROWSER"))
    try:
        srv = Server((HOST, PORT), Handler)
    except OSError:
        print("=== KIT ===")
        print(f"  port {PORT} is already in use - Kit is probably already")
        print(f"  running. Open http://{HOST}:{PORT}/ , or close the other")
        print("  window and try again (or use --port).")
        # Exit code 3, not 1 or a bare exception - this is a SUCCESSFUL
        # outcome (Kit is already up), and SKILLS.bat / kit.sh check for
        # exactly this code so they do not read "already running" as "Python
        # is broken". Keep them in sync if this ever changes.
        raise SystemExit(3)
    url = f"http://{HOST}:{PORT}/"
    _start_watcher()
    _refresh_prompts()
    print("=== KIT ===")
    print(f"  serving {url}")
    print("  toggles write to ~/.claude/skills; Ctrl-C to stop")
    print("  watching transcripts - skills and programs light up as they fire")
    if not no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
        srv.server_close()


if __name__ == "__main__":
    main()
