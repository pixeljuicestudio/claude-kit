#!/usr/bin/env python3
"""Selftest for the on-demand re-pull of the optional Notion prompts.

Local prompt files are re-read on every page build, like skills, programs,
tools and hooks. The optional Notion rows are different: a cache, pulled once
when the server starts. A prompt added in Notion with Kit already open was
invisible - the cache was 131 minutes old and still said 17 while Notion said
18.

The Refresh button closes that. Without Notion configured the pull is a quick
no-op that must still SUCCEED (the local folder is the library; "nothing to
pull" is not an error). What matters about it:

  * It CANNOT be a blocking request. The export takes ~7s and HTTPServer
    handles one request at a time, so a synchronous pull would freeze every
    toggle, every star and the 2s live poll behind it.
  * Two pulls must not race. The button stays clickable, and overlapping
    exports on one cache file is how a half-written prompts.json happens.
  * A REFUSED pull must be reported. prompts.py deliberately exits non-zero
    rather than overwrite a good cache with a bad one; swallowing that would
    show a silent no-op, which reads as "the button does nothing".

Run: python test_ppull.py      Exit 0 = every invariant holds.
"""
import inspect
import sys
import time

import serve

FAILS = []


def check(label, ok, detail=""):
    print("%-5s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILS.append("%s: %s" % (label, detail))


# ------------------------------------------------------------------ the state
check("the pull state starts idle", serve.PULL["running"] is False, serve.PULL)
check("it is guarded by a lock", isinstance(serve.PULL_LOCK, type(serve.threading.Lock())))

# ------------------------------------------------------------- no double pulls
# Fake a pull in flight and confirm a second is refused rather than queued.
with serve.PULL_LOCK:
    serve.PULL.update(running=True)
try:
    check("a second pull is refused while one is running",
          serve._start_pull() is False)
finally:
    with serve.PULL_LOCK:
        serve.PULL.update(running=False)

# ------------------------------------------------------------------ a real one
before = len(serve.promptlib.scan()[0])
started = serve._start_pull()
check("a pull starts when idle", started is True)
check("it reports itself as running immediately", serve.PULL["running"] is True)

# It must NOT have blocked: _start_pull returns as soon as the thread is away.
t0 = time.time()
for _ in range(120):
    if not serve.PULL["running"]:
        break
    time.sleep(0.5)
took = time.time() - t0
check("the pull completed", not serve.PULL["running"], "still running after %.0fs" % took)
check("no error came back", not serve.PULL["error"], serve.PULL["error"])
check("the local prompts are counted even with no Notion configured",
      serve.PULL["count"] >= serve.promptlib.scan()[1]["local"] > 0, serve.PULL)
check("it reports a count", serve.PULL["count"] > 0, serve.PULL["count"])
check("the count matches what scan() now sees",
      serve.PULL["count"] == len(serve.promptlib.scan()[0]))
check("it did not lose prompts", serve.PULL["count"] >= before,
      "%d -> %d" % (before, serve.PULL["count"]))
check("it stamps when it finished", serve.PULL["at"] > 0)

# ----------------------------------------------------------------- the routes
src = inspect.getsource(serve.Handler)
check("POST /api/ppull starts a pull", '"/api/ppull"' in src and "_start_pull()" in src)
check("GET /api/ppull reports on one", "/api/ppull" in src and "**PULL" in src)
# The startup path and the button must be ONE implementation.
check("startup reuses the same pull function",
      "_start_pull()" in inspect.getsource(serve._refresh_prompts))
check("a non-zero export is surfaced, not swallowed",
      "returncode" in inspect.getsource(serve._start_pull))

# ------------------------------------------------------------------ the page
import build
bsrc = inspect.getsource(build)
check("the page has a refresh button", 'id="ppull"' in bsrc)
check("it polls rather than holding the connection", "for (let i = 0" in bsrc
      and "/api/ppull" in bsrc)
check("it reloads once the cache is fresh", "location.reload()" in bsrc)
check("it says so when the server is not running",
      "Refresh needs the server" in bsrc)
check("it surfaces a failed pull", "Notion pull failed" in bsrc)
check("the button only shows when Notion is configured",
      "PRMETA.notion_configured" in bsrc)
import prompts as _p
check("--export without Notion exits cleanly, not with an error",
      "notion_configured()" in inspect.getsource(_p.main))

print("\n=== SELFTEST COMPLETE - %d failure(s) ===" % len(FAILS))
for f in FAILS:
    print("  " + f)
sys.exit(1 if FAILS else 0)
