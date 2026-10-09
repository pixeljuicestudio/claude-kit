#!/usr/bin/env python3
"""
Skills scanner — builds the inventory the launcher renders.

Answers one question the other tools get wrong: not "what SKILL.md files
exist" but "which of them can a session actually reach". A plugin skill on
disk whose plugin is disabled in settings.json is inert — it cannot be named
and will not fire. That distinction is the whole point.

Emits a list of dicts; build.py turns it into HTML.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import config

HOME = Path.home()
CLAUDE = HOME / ".claude"

# Where skills live. Each entry: (location label, root, how to scan)
MACHINE_SKILLS = CLAUDE / "skills"
PLUGIN_CACHE = CLAUDE / "plugins" / "cache"          # installed plugins
PLUGIN_MARKET = CLAUDE / "plugins" / "marketplaces"  # catalogue of installable
# Extra folders of <name>/SKILL.md - a skills repo you upload to claude.ai, a
# project's .claude/skills, anything. Configured in kit.config.json.
EXTRA_SKILL_DIRS = list(config.EXTRA_SKILL_DIRS)

SETTINGS = CLAUDE / "settings.json"
INSTALLED = CLAUDE / "plugins" / "installed_plugins.json"
KNOWN_MARKETPLACES = CLAUDE / "plugins" / "known_marketplaces.json"


def marketplace_repos(path: Path | None = None) -> dict[str, str]:
    """{marketplace: "owner/repo"}, read from Claude Code's own
    known_marketplaces.json. A marketplace added from a local folder or a
    non-GitHub URL simply has no upstream. Missing or unreadable file -> {}."""
    try:
        raw = json.loads((path or KNOWN_MARKETPLACES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        src = entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(src, dict):
            continue
        repo = src.get("repo")
        if not repo and isinstance(src.get("url"), str):
            m = REPO_RE.search(src["url"])
            repo = m.group(1) if m else None
        if isinstance(repo, str) and repo:
            out[name] = repo
    return out

DESC_LIMIT = 1024  # claude.ai upload rejects longer; learned 2026-07-30


# ---------------------------------------------------------------- frontmatter

def parse_frontmatter(text: str) -> dict:
    """Parse the YAML-ish frontmatter block.

    Handles the folded block scalar (`description: >`) that the skills repo
    uses, plain scalars, and quoted values. Not a full YAML parser - it only
    needs name/description/allowed-tools, and a real parser is a dependency
    this does not want.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    fields: dict[str, str] = {}
    key: str | None = None
    folded = False

    for line in lines[1:]:
        if line.strip() == "---":
            break

        indented = line[:1] in (" ", "\t")

        if not indented and ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            folded = val in (">", ">-", "|", "|-")
            fields[key] = "" if folded else val.strip("'\"")
        elif key is not None and (indented or folded):
            fields[key] = (fields[key] + " " + line.strip()).strip()

    return fields


def read_skill(skill_md: Path) -> dict | None:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    fm = parse_frontmatter(text)
    folder = skill_md.parent.name
    name = fm.get("name") or folder
    desc = " ".join(fm.get("description", "").split())

    # Body = everything after the frontmatter. Its size is the on-demand
    # token cost; the description is the always-in-context cost.
    body = text.split("---", 2)[-1] if text.startswith("---") else text

    try:
        edited = skill_md.stat().st_mtime
    except OSError:
        edited = 0.0

    manual = str(fm.get("disable-model-invocation", "")).lower() == "true"

    return {
        "manual": manual,
        "license": (skill_md.parent / "LICENSE").exists(),
        "edited": edited,
        "name": name,
        "folder": folder,
        "desc": desc,
        "path": str(skill_md.parent).replace("\\", "/"),
        "file": str(skill_md).replace("\\", "/"),
        "desc_tokens": round(len(desc) / 4),
        "body_tokens": round(len(body) / 4),
        "flags": health_flags(name, folder, desc, fm),
    }


# github.com/owner/repo, however it is written in a README or a body.
REPO_RE = re.compile(r"github\.com[/:]([A-Za-z0-9_-]+/[A-Za-z0-9_.-]+?)(?:\.git)?(?![A-Za-z0-9_.-])")


def origin_of(folder: Path, default: str) -> tuple[str, str | None]:
    """Is this skill yours, or someone else's copy?

    A LICENSE naming a third party is the reliable tell: skills you author do
    not ship one. power-design and prompt-master were both sitting in
    ~/.claude/skills looking native until this check was written - they are
    MIT-licensed copies that get no upstream updates, which matters because a
    borrowed skill's description was written for someone else's phrasing.
    """
    lic = folder / "LICENSE"
    if not lic.exists():
        return default, None

    upstream = None
    for f in ("README.md", "SKILL.md"):
        if (folder / f).exists():
            try:
                hits = REPO_RE.findall((folder / f).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                hits = []
            # A README often cites sister repos too, so prefer the one whose
            # repo name matches this folder - otherwise power-design reports
            # power-design-web as its upstream.
            upstream = next((h for h in hits if h.split("/")[-1] == folder.name),
                            hits[0] if hits else None)
            if upstream:
                break
    return "borrowed", upstream


def health_flags(name: str, folder: str, desc: str, fm: dict) -> list[str]:
    flags = []
    if not fm:
        flags.append("no frontmatter")
    if not desc:
        flags.append("no description - cannot fire")
    elif len(desc) > DESC_LIMIT:
        flags.append(f"description {len(desc)} chars - over the {DESC_LIMIT} upload limit")
    if name != folder:
        flags.append(f"name '{name}' != folder '{folder}'")
    return flags


# ------------------------------------------------------------------ enablement

def enabled_plugins() -> dict[str, bool]:
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("enabledPlugins", {}) or {}


def installed_plugins() -> dict:
    for candidate in (INSTALLED, CLAUDE / "installed_plugins.json"):
        try:
            return json.loads(candidate.read_text(encoding="utf-8")).get("plugins", {})
        except (OSError, ValueError):
            continue
    return {}


# ---------------------------------------------------------------------- scan

def scan() -> tuple[list[dict], dict]:
    items: list[dict] = []
    enabled = enabled_plugins()
    installed = installed_plugins()
    repos = marketplace_repos()

    # 1. Machine skills - always reachable, no gate.
    for md in sorted(MACHINE_SKILLS.glob("*/SKILL.md")):
        if (rec := read_skill(md)):
            rec |= {
                "location": "This PC",
                "home": "machine",
                **dict(zip(("origin", "upstream"), origin_of(md.parent, "yours"))),
                "reach": "manual" if rec["manual"] else "auto",
                "why": ("Manual - description not in context. Only /" + rec["name"] + " reaches it."
                        if rec["manual"] else
                        "In ~/.claude/skills - description loaded every session, fires on your phrasing."),
                "plugin": None,
            }
            items.append(rec)

    # 2. Extra folders (extra_skill_dirs) - SOURCE, not necessarily what runs.
    #    A skills repo you upload to claude.ai, or a project's own
    #    .claude/skills, fires from somewhere Kit cannot see, so presence here
    #    proves nothing about reachability. Kept under the stable home key
    #    "cloud" because the page branches on it to disable the toggle.
    for root in EXTRA_SKILL_DIRS:
        for md in sorted(root.glob("*/SKILL.md")):
            if (rec := read_skill(md)):
                rec |= {
                    "location": "Extra folder",
                    "home": "cloud",
                    **dict(zip(("origin", "upstream"), origin_of(md.parent, "yours"))),
                    "reach": "auto",
                    "why": ("Inventoried from an extra folder (extra_skill_dirs). It is not in "
                            "~/.claude/skills, so whether it fires depends on where it is "
                            "installed or uploaded - Kit cannot see that."),
                    "plugin": None,
                }
                items.append(rec)

    # 3. Plugin skills - gated on enabledPlugins. This is the tier every
    #    off-the-shelf manager gets wrong.
    for md in sorted(PLUGIN_CACHE.rglob("*/SKILL.md")):
        rec = read_skill(md)
        if not rec:
            continue
        parts = md.parts
        try:
            i = parts.index("cache")
            marketplace, plugin, cache_hash = parts[i + 1], parts[i + 2], parts[i + 3]
        except (ValueError, IndexError):
            marketplace, plugin, cache_hash = "?", "?", "?"

        key = f"{plugin}@{marketplace}"
        # A plugin update does not delete its old cache directory, only stops
        # pointing installed_plugins.json at it - so an updated plugin's cache
        # can hold TWO hash directories for the same skills. Walking the whole
        # cache tree picked up both, rendering as an exact duplicate that
        # "competed with" itself (found 2026-09-07: plugin-dev's whole skillset
        # doubled after an update left `50c7b0988de3` behind next to the real
        # `a39688dfd347`). Only the hash(es) installed_plugins.json still names
        # are reachable, so skip anything else - but only when there IS a
        # known-good set to check against, so a plugin absent from that file
        # for some other reason still scans as it always did.
        install_hashes = {Path(e["installPath"]).name for e in installed.get(key, [])
                          if isinstance(e, dict) and e.get("installPath")}
        if install_hashes and cache_hash not in install_hashes:
            continue

        on = enabled.get(key, False)
        rec |= {
            "location": "Plugins - installed",
            "home": "plugin",
            "reach": "auto" if on else "off",
            "why": (
                f"Plugin '{key}' is enabled."
                if on else
                f"Plugin '{key}' is disabled in settings.json. Cannot be named - "
                f"asking for it returns 'Unknown skill'. Usable only by reading the file."
            ),
            "plugin": key,
            "origin": "borrowed",
            "upstream": repos.get(marketplace),
            "marketplace": marketplace,
        }
        items.append(rec)

    # 4. Marketplace catalogue - plugins you could install but have not.
    #    Anything already present in cache/ is suppressed: the installed copy
    #    is the authoritative one and listing both would double-count.
    #    Keyed on BEING an installed copy, not on its reach. Filtering by
    #    reach == "off" only suppressed the twins of DISABLED plugins: enable a
    #    plugin and its catalogue entry reappeared alongside the installed one,
    #    so the total silently grew by the size of every plugin you turned on.
    #    Found 2026-08-23 when enabling plugin-dev (7) and skill-creator (1)
    #    made the count read 8 higher than the real inventory.
    installed_pairs = {
        (i.get("plugin", "").split("@")[0], i["folder"])
        for i in items if i["location"] == "Plugins - installed"
    }
    for md in sorted(PLUGIN_MARKET.rglob("*/SKILL.md")):
        rec = read_skill(md)
        if not rec:
            continue
        parts = md.parts
        try:
            i = parts.index("marketplaces")
            marketplace = parts[i + 1]
            # plugin name is the dir immediately above skills/
            j = parts.index("skills", i)
            plugin = parts[j - 1]
        except (ValueError, IndexError):
            marketplace, plugin = "?", "?"

        if (plugin, rec["folder"]) in installed_pairs:
            continue

        rec |= {
            "location": "Plugins - catalogue",
            "home": "plugin",
            "reach": "off",
            "why": (
                f"Offered by the '{marketplace}' marketplace via plugin '{plugin}', "
                f"not installed. Install with: /plugin install {plugin}@{marketplace}"
            ),
            "plugin": f"{plugin}@{marketplace}",
            "origin": "borrowed",
            "upstream": repos.get(marketplace),
            "marketplace": marketplace,
        }
        items.append(rec)

    meta = {
        "enabled_plugins": enabled,
        "installed_plugins": sorted(installed.keys()),
        "roots": {
            "machine": str(MACHINE_SKILLS).replace("\\", "/"),
            "extra": [str(d).replace("\\", "/") for d in EXTRA_SKILL_DIRS],
            "plugins": str(PLUGIN_CACHE).replace("\\", "/"),
        },
    }
    return items, meta


# ----------------------------------------------------------------- collisions

STOP = set("""a an the and or of to in for on with from by is are be as at it
this that use used using when whether what which how your you их use-this""".split())


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9-]{3,}", text.lower()) if w not in STOP}


def mark_collisions(items: list[dict], threshold: float = 0.5) -> None:
    """Flag skills whose descriptions compete for the same phrasing.

    Only compares skills that can actually both fire - two disabled plugin
    skills colliding is not a real problem.
    """
    live = [i for i in items if i["reach"] == "auto" and i["desc"]]
    sets = {id(i): words(i["desc"]) for i in live}

    for i, a in enumerate(live):
        for b in live[i + 1:]:
            wa, wb = sets[id(a)], sets[id(b)]
            if not wa or not wb:
                continue
            overlap = len(wa & wb) / len(wa | wb)
            if overlap >= threshold:
                a.setdefault("collides", []).append(b["name"])
                b.setdefault("collides", []).append(a["name"])


if __name__ == "__main__":
    found, meta = scan()
    mark_collisions(found)
    by_reach: dict[str, int] = {}
    for it in found:
        by_reach[it["reach"]] = by_reach.get(it["reach"], 0) + 1
    print(json.dumps({"counts": by_reach, "total": len(found)}, indent=2))
