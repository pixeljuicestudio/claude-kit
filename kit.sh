#!/usr/bin/env sh
# Kit on macOS / Linux. Serves the page so toggles, stars and tags can write.
# Extra arguments are passed through, e.g.  ./kit.sh --port 8800
cd "$(dirname "$0")" || exit 1
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
"$PY" serve.py "$@"
code=$?
# serve.py exits 3 when Kit is already running on the port - that is success.
if [ "$code" -eq 3 ]; then
  exit 0
fi
exit "$code"
