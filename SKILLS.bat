@echo off
REM Kit. Serves the page so the Auto/Manual toggles, stars and tags can write.
REM Leave this window open while you use it; Ctrl-C or close it when done.
REM Extra arguments are passed through, e.g.  SKILLS.bat --port 8800
cd /d "%~dp0"
py serve.py %*
REM serve.py exits 3, on purpose, when Kit is already running on this port -
REM that is SUCCESS, not a Python problem. Falling through to the "try
REM python, then blame the interpreter" cascade below is exactly how that
REM deliberate exit once became a false "Could not run Python" error on
REM screen while Kit was working fine the whole time.
REM (Opens KIT_PORT if set, else 8712. A --port argument is not parsed here.)
if %errorlevel%==3 (
  if defined KIT_PORT (start "" "http://127.0.0.1:%KIT_PORT%/") else (start "" "http://127.0.0.1:8712/")
  exit /b 0
)
if errorlevel 1 python serve.py %*
if errorlevel 1 (
  echo.
  echo Could not run Python. Check it is installed and on PATH.
  pause
)
