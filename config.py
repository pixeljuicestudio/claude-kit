#!/usr/bin/env python3
"""Configuration - one place for every path that differs between machines.

Kit works with no configuration at all: it reads ~/.claude and the prompts/
folder beside this file. Everything else is opt-in through kit.config.json
(gitignored - copy kit.config.example.json and edit it). Missing keys fall
back to the defaults below, and a config file that will not parse is reported
on stderr and ignored rather than taking the server down.

Secrets never live in the file. The optional Notion token is read ONLY from
the NOTION_TOKEN environment variable.

    python config.py        print the resolved configuration
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "kit.config.json"

DEFAULT_PORT = 8712


def _expand(p) -> Path | None:
    """A config path: ~ and env vars expanded, relative paths taken from the
    repo folder. Empty or non-string values mean "not configured"."""
    if not isinstance(p, str) or not p.strip():
        return None
    q = Path(os.path.expandvars(os.path.expanduser(p.strip())))
    return q if q.is_absolute() else (HERE / q)


def _load() -> dict:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        sys.stderr.write(f"kit: {CONFIG_FILE.name} would not parse ({e}) - using defaults\n")
        return {}
    return raw if isinstance(raw, dict) else {}


RAW = _load()

# Folder whose subfolders are your "tools". None = the Tools tab and the tool
# half of Progs show a "not configured" state.
TOOLS_ROOT: Path | None = _expand(RAW.get("tools_root"))

# Extra settings.json files whose hooks should be listed (project-scope hooks).
PROJECT_SETTINGS: list[Path] = [p for p in (_expand(x) for x in RAW.get("project_settings") or [])
                                if p is not None]

# Where launchers drop their activity markers (see mark_launchers.py).
RUNS_DIR: Path = _expand(RAW.get("runs_dir")) or (HERE / ".runs")

# Extra folders of <name>/SKILL.md to inventory beside ~/.claude/skills.
EXTRA_SKILL_DIRS: list[Path] = [p for p in (_expand(x) for x in RAW.get("extra_skill_dirs") or [])
                                if p is not None]

# Your prompt library: one .md file per prompt.
PROMPTS_DIR: Path = _expand(RAW.get("prompts_dir")) or (HERE / "prompts")

# Optional folder of downloaded reference videos, matched to a prompt's url by
# the post id in the filename ("... [1234567890].mp4").
REFERENCE_DIR: Path | None = _expand(RAW.get("reference_dir"))

# The ~/.claude/projects/<name> folder replay.py and guard.py read by default.
# None = derive it (the folder with the most transcripts), or pass --project.
TRANSCRIPTS_PROJECT: str | None = (RAW.get("transcripts_project") or None)

# Optional script run (with --fix-modes --quiet) after an Auto/Manual toggle,
# e.g. to sync an external skills index. None = skipped silently.
MODE_SYNC_SCRIPT: Path | None = _expand(RAW.get("mode_sync_script"))

_notion = RAW.get("notion") if isinstance(RAW.get("notion"), dict) else {}
NOTION_DB_ID: str | None = (_notion.get("db_id") or None)


def notion_token() -> str | None:
    """Read at call time, so a token exported after import still works."""
    return os.environ.get("NOTION_TOKEN") or None


def notion_configured() -> bool:
    return bool(NOTION_DB_ID and notion_token())


def port() -> int:
    """KIT_PORT env var, then "port" in the config, then 8712."""
    for v in (os.environ.get("KIT_PORT"), RAW.get("port")):
        try:
            if v is not None and int(v) > 0:
                return int(v)
        except (TypeError, ValueError):
            continue
    return DEFAULT_PORT


def tools_root_str() -> str | None:
    return str(TOOLS_ROOT).replace("\\", "/") if TOOLS_ROOT else None


if __name__ == "__main__":
    print(json.dumps({
        "config_file": str(CONFIG_FILE) if CONFIG_FILE.is_file() else None,
        "tools_root": tools_root_str(),
        "project_settings": [str(p) for p in PROJECT_SETTINGS],
        "runs_dir": str(RUNS_DIR),
        "extra_skill_dirs": [str(p) for p in EXTRA_SKILL_DIRS],
        "prompts_dir": str(PROMPTS_DIR),
        "reference_dir": str(REFERENCE_DIR) if REFERENCE_DIR else None,
        "transcripts_project": TRANSCRIPTS_PROJECT,
        "mode_sync_script": str(MODE_SYNC_SCRIPT) if MODE_SYNC_SCRIPT else None,
        "notion_db_id": NOTION_DB_ID,
        "notion_token_set": bool(notion_token()),
        "port": port(),
    }, indent=2))
