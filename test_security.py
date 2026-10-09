#!/usr/bin/env python3
"""Selftest for the two ways a web page could reach past Kit's own page.

Binding to 127.0.0.1 keeps other machines out, but not other web pages:

  * Any site open in the same browser can POST to localhost. Without an
    Origin check, a page you merely visit could launch a rostered tool or
    flip a skill to Manual.
  * A DNS-rebinding site can point its own name at 127.0.0.1 and read the
    replies. The Host header is what gives that away.
  * A prompt is someone else's text. If it contains </script>, embedding the
    inventory in the page must not let it close the tag and run as script.

Run: python test_security.py      Exit 0 = every invariant holds.
"""
import http.client
import json
import sys
import threading

import build
import serve

FAILS = []


def check(label, ok, detail=""):
    print("%-5s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILS.append("%s: %s" % (label, detail))


srv = serve.Server(("127.0.0.1", 0), serve.Handler)
serve.PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
HOST = f"127.0.0.1:{serve.PORT}"


def call(method, path, headers, body=None):
    c = http.client.HTTPConnection("127.0.0.1", serve.PORT, timeout=10)
    c.putrequest(method, path, skip_host=True)
    for k, v in headers.items():
        c.putheader(k, v)
    blob = json.dumps(body).encode() if body is not None else b""
    c.putheader("Content-Length", str(len(blob)))
    c.endheaders(blob)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


# ------------------------------------------------------------ cross-site POST
evil = {"Host": HOST, "Origin": "https://example.com",
        "Content-Type": "text/plain"}
for path in ("/api/launch", "/api/mode", "/api/fav", "/api/ppull"):
    st = call("POST", path, evil, {"name": "x", "mode": "manual"})
    check(f"cross-site POST {path} is refused", st == 403, st)

# ------------------------------------------------------------ DNS rebinding
rebound = {"Host": f"attacker.example:{serve.PORT}"}
check("GET / under a foreign Host is refused",
      call("GET", "/", rebound) == 403)
check("GET /api/live under a foreign Host is refused",
      call("GET", "/api/live", rebound) == 403)

# ------------------------------------------------------------ Kit's own page
own = {"Host": HOST, "Origin": f"http://{HOST}",
       "Content-Type": "application/json"}
st = call("POST", "/api/fav", own, {"name": "no-such-skill", "on": True})
check("Kit's own page gets past the guard (404 = reached the handler)",
      st == 404, st)
check("localhost:<port> is accepted as well",
      call("GET", "/api/live", {"Host": f"localhost:{serve.PORT}"}) == 200)
check("a non-browser client (no Origin) is accepted",
      call("GET", "/api/live", {"Host": HOST}) == 200)

srv.shutdown()

# ------------------------------------------------------------ </script> in data
real = build.TEMPLATE
try:
    build.TEMPLATE = "<script>const P = __PAYLOAD__;</script>"
    orig = build.json.dumps
    html = build.TEMPLATE.replace(
        "__PAYLOAD__",
        orig({"x": "</script><script>alert(1)</script>"}).replace("</", "<\\/"))
finally:
    build.TEMPLATE = real
check("an embedded </script> cannot close the page's script tag",
      html.count("</script>") == 1, html)
src = open(build.__file__, encoding="utf-8").read()
check("build.py escapes </ when it embeds the payload",
      '.replace("</", "<\\\\/")' in src)
check("the page's esc() escapes single quotes",
      "\"'\":'&#39;'" in src)

if FAILS:
    print("\n%d FAILED" % len(FAILS))
    sys.exit(1)
print("\nall passed")
