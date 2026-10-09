#!/usr/bin/env python3
"""
Prompt Library — your prompts, one Markdown file each, served to Kit.

    python prompts.py              show what Kit sees
    python prompts.py --export     also pull the optional Notion database

THE LOCAL FOLDER IS THE SOURCE. Every prompts_dir/*.md (default: prompts/
beside this file) is one prompt: a small frontmatter block, then the prompt
text itself. Drop a file in, reload the page, it is there. See
prompts/README.md for the format.

    ---
    name: Pre-mortem
    tags: [thinking, planning]
    platform: Claude
    for: Before committing to a plan, find how it fails.
    origin: Written in-house
    url: https://example.com/where-it-came-from
    ---
    Imagine it is six months from now and this plan has failed...

WHY THE BODY IS THE PROMPT, not a description of it. A tab that showed only
"what this is for" would be a list of things you then have to go somewhere
else to actually get, which is the trip Kit exists to save. The body is what
the Copy button copies.

NOTION IS AN OPTIONAL EXTRA. If NOTION_TOKEN is set in the environment and
notion.db_id is set in kit.config.json, --export (and the server's Refresh
button) also pulls that database into prompts.json, a gitignored cache, and
those rows appear beside the local ones. A local file wins over a Notion row
with the same name. Without Notion configured nothing here touches the
network, and the server starts exactly the same.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import config

HERE = Path(__file__).resolve().parent
CACHE = HERE / "prompts.json"          # Notion cache only; gitignored
FAVS = HERE / "prompt_favourites.json"
USAGE = HERE / "prompt_usage.json"
KTAGS = HERE / "prompt_ktags.json"
PROMPTS_DIR = config.PROMPTS_DIR

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


# --------------------------------------------------------------------------
# categories
# --------------------------------------------------------------------------
# Prompts get their OWN category vocabulary rather than borrowing the skills'
# one. Run the skills categories over a prompt library and most of it lands in
# one bucket - a facet that sorts nothing is not a facet.
#
# Every category is DERIVED FROM THE TAGS YOU SET, never guessed from the
# prose. categorise.py has to infer a skill's category and is right only part
# of the time, so those cards carry a "?". Here the tags are yours, so the
# category is a lookup and needs no hedge. A prompt with no matching tag files
# as Unsorted, which is a true statement about the file rather than a failed
# guess. Matching is case-insensitive.
#
# ORDER IS THE RULE. First match wins, and the list is ordered by what the
# prompt PRODUCES. A storyboard prompt tagged both "character reference" and
# "video" makes a video; the reference is the technique, not the output. So
# the output categories outrank the technique ones.
CATEGORY_RULES = [
    ("Thinking & critique", {"critique", "thinking", "review", "pre-mortem",
                             "premortem", "decision", "planning", "strategy"}),
    ("Video generation",    {"video", "image-to-video", "text-to-video", "animation"}),
    ("Reference sheets",    {"character reference", "vessel reference", "product reference",
                             "reference sheet", "turnaround"}),
    ("Image generation",    {"image", "image generation", "text-to-image", "photo"}),
    ("Coding",              {"code", "coding", "debugging", "refactor", "testing",
                             "programming", "review code"}),
    ("Writing",             {"writing", "copywriting", "editing", "email", "blog",
                             "social"}),
    ("Research",            {"research", "analysis", "summary", "summarise",
                             "summarize", "learning"}),
    # Prompts that drive the workshop rather than make something to look at -
    # scaffolding a project, wiring a folder tree, setting up an agent. Last
    # deliberately: it can only catch a row no other rule wanted.
    ("System & ops",        {"system & ops", "ops", "workflow", "automation",
                             "setup", "agents", "claude code"}),
]
CATEGORIES = [name for name, _ in CATEGORY_RULES]

# The platform a prompt was written against, when only the tags say so. Kept
# apart from the category because it is a different question - "what does this
# make" versus "where do I paste it" - and a prompt written for one platform
# rarely transfers verbatim. A file's own `platform:` field always wins.
TOOL_TAGS = ["Claude", "Claude Code", "ChatGPT", "Gemini", "Midjourney",
             "Higgsfield", "Seedance", "Runway", "Kling", "Suno"]


def say(msg: str) -> None:
    """print() that cannot take the run down.

    A Windows console is often cp1252, and prompt titles are full of
    em-dashes and arrows. A bare print() of one raises UnicodeEncodeError and
    kills the run mid-walk, so the progress line degrades instead.
    """
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout.write(msg.encode(enc, "replace").decode(enc, "replace") + "\n")
    sys.stdout.flush()


def _low(tags) -> set[str]:
    return {str(t).strip().lower() for t in (tags or []) if str(t).strip()}


def category_for(tags) -> str:
    have = _low(tags)
    for name, trigger in CATEGORY_RULES:
        if have & trigger:
            return name
    return "Unsorted"


def tool_for(tags, explicit: str | None = None) -> str | None:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    have = _low(tags)
    hit = [t for t in TOOL_TAGS if t.lower() in have]
    return " + ".join(hit) if hit else None


# --------------------------------------------------------------------------
# the local folder - the primary source
# --------------------------------------------------------------------------

def _scalar(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v


def _list(v) -> list[str]:
    """tags: [a, b] / tags: a, b / a YAML list - all the same thing."""
    if isinstance(v, list):
        items = v
    else:
        s = str(v or "").strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        items = s.split(",")
    out = []
    for x in items:
        x = _scalar(str(x))
        if x and x not in out:
            out.append(x)
    return out


def parse_prompt_file(text: str) -> tuple[dict, str]:
    """(frontmatter fields, body). Deliberately tiny - not a YAML parser.

    Understands `key: value`, quoted values, inline lists and `- item` list
    lines under a key. A file with no frontmatter is all body.
    """
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    fields: dict = {}
    key = None
    end = None
    for n, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = n
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and key is not None:
            cur = fields.get(key)
            if not isinstance(cur, list):
                cur = [] if not cur else [cur]
            cur.append(_scalar(stripped[2:]))
            fields[key] = cur
            continue
        if ":" in line and line[:1] not in (" ", "\t"):
            k, _, v = line.partition(":")
            key = k.strip().lower()
            fields[key] = _scalar(v)
    if end is None:                       # unterminated block: treat as body
        return {}, text.strip()
    return fields, "\n".join(lines[end + 1:]).strip()


MD_HEAD = re.compile(r"^#{1,6}\s")


def local_body(text: str) -> tuple[str, str]:
    """(copyable prompt, how it was found) for a local file's body.

    If the file has a `## The Prompt` or `## The template` heading, that
    section - up to the next heading, such as `## Notes` - is the prompt, and
    the setup text above it and the commentary below it stay out of the copy
    payload. Same rule the Notion parser applies, so a prompt moved between
    the two copies identically. Otherwise the whole body is the prompt.

    Unlike the Notion path, blank lines INSIDE the prompt are kept: a local
    file's paragraphs are the author's formatting, and the copy should paste
    exactly as written.
    """
    lines = text.splitlines()
    for n, line in enumerate(lines):
        if PROMPT_HEAD.match(line.strip().replace("### ", "## ", 1)):
            out = []
            for l in lines[n + 1:]:
                if MD_HEAD.match(l.strip()):
                    break
                out.append(l)
            section = "\n".join(out).strip()
            if section:
                return section, "section"
    return text.strip(), "file"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def read_prompt_file(f: Path) -> dict | None:
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
        st = f.stat()
    except OSError:
        return None
    fm, full = parse_prompt_file(text)
    body, how = local_body(full)
    name = fm.get("name") or fm.get("title") or ""
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        name = f.stem.replace("-", " ").replace("_", " ").strip().capitalize() or f.stem
    tags = _list(fm.get("tags"))
    platform = fm.get("platform") or fm.get("tool")
    platform = platform if isinstance(platform, str) else None
    # created: the file's birth time where the OS records one, else its mtime.
    born = getattr(st, "st_birthtime", None)
    if born is None and sys.platform == "win32":
        born = st.st_ctime                 # creation time on Windows
    created = min(born, st.st_mtime) if born else st.st_mtime
    url = fm.get("url") if isinstance(fm.get("url"), str) else ""
    item = {
        "name": name,
        "for": str(fm.get("for") or fm.get("description") or ""),
        "origin": str(fm.get("origin") or ""),
        "ref": str(fm.get("ref") or fm.get("reference") or url or ""),
        "tags": tags,
        "cat": category_for(tags),
        "tool": tool_for(tags, platform),
        "platform": platform,
        "body": body,
        "body_from": how,
        # The whole file body, setup and notes included. Search covers it, so
        # a word that only appears in the notes still finds the prompt.
        "notes": full if how == "section" else "",
        "words": len(body.split()),
        "tokens": round(len(body) / 4),
        "url": url,
        "created": _iso(created),
        "edited": _iso(st.st_mtime),
        "source": "local",
        "file": str(f).replace("\\", "/"),
        "filename": f.name,
    }
    item["flags"] = flags_for(item)
    return item


def local_prompts(folder: Path | None = None) -> list[dict]:
    """Every *.md in the prompts folder except its README. Never raises."""
    d = folder or PROMPTS_DIR
    out: list[dict] = []
    if not d.is_dir():
        return out
    try:
        files = sorted(d.glob("*.md"))
    except OSError:
        return out
    for f in files:
        if f.name.lower() in ("readme.md", "_template.md") or f.name.startswith("."):
            continue
        rec = read_prompt_file(f)
        if rec:
            out.append(rec)
    return out


# --------------------------------------------------------------------------
# Notion - optional
# --------------------------------------------------------------------------

def token() -> str:
    """NOTION_TOKEN from the environment, and nowhere else. Never stored."""
    tok = config.notion_token()
    if not tok:
        raise SystemExit("NOTION_TOKEN is not set in the environment.")
    return tok


def api(tok, path, method="GET", body=None, retries=3):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("%s/%s" % (API, path), data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % tok)
    req.add_header("Notion-Version", NOTION_VERSION)
    req.add_header("Content-Type", "application/json")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            # TimeoutError is not a URLError subclass - catch both. A long
            # walk was once lost to exactly that distinction.
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
    return None


def flatten(prop):
    if not prop:
        return None
    t = prop.get("type")
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in prop.get(t) or []) or None
    if t == "select":
        sel = prop.get("select")
        return sel.get("name") if sel else None
    if t == "multi_select":
        return [x.get("name") for x in prop.get("multi_select") or []] or None
    # ⚠️ Without a branch for a url-typed property, flatten() returns None and
    # the field reads as "not set" - indistinguishable from the property
    # genuinely being empty, so the feature looks built and does nothing.
    # Caught because an export reported ZERO prompts with a Reference
    # immediately after one had been set.
    if t in ("url", "email", "phone_number"):
        return prop.get(t) or None
    if t == "number":
        return prop.get("number")
    if t == "checkbox":
        return bool(prop.get("checkbox"))
    return None


RICH = ("paragraph", "heading_1", "heading_2", "heading_3", "quote", "callout",
        "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "code")


def block_text(tok, block_id, depth=0) -> list[str]:
    """Flatten a page's blocks to lines of plain text.

    Recurses, because a prompt tucked inside a toggle is still the prompt -
    and toggles are exactly where long ones get put. Depth-capped so a
    pathological page cannot spin.
    """
    if depth > 4:
        return []
    out, cursor = [], None
    while True:
        q = "blocks/%s/children?page_size=100" % block_id
        if cursor:
            q += "&start_cursor=%s" % cursor
        res = api(tok, q)
        if res is None:
            return out
        for b in res.get("results", []):
            t = b.get("type")
            if t in RICH:
                spans = (b.get(t) or {}).get("rich_text") or []
                line = "".join(s.get("plain_text", "") for s in spans)
                # Headings are kept marked so extract_prompt can find the
                # "The Prompt" section. Everything else goes in flat.
                if t.startswith("heading"):
                    out.append("## " + line)
                elif t in ("bulleted_list_item", "numbered_list_item", "to_do"):
                    out.append(("  " * depth) + "- " + line)
                else:
                    out.append(line)
            elif t == "divider":
                out.append("---")
            if b.get("has_children") and t != "child_page":
                out.extend(block_text(tok, b["id"], depth + 1))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return out


# --------------------------------------------------------------------------
# pulling the prompt out of a Notion page
# --------------------------------------------------------------------------

# The heading a prompt hides under. "The template" is NOT a synonym invented
# for tidiness - a real page filed its prompt under
# '## The template (fill [BRACKETS], keep the rest fixed)', and matching only
# "prompt" made that page fall through to the whole-page reading, which then
# cut at the first heading and returned the three intro paragraphs. Copying it
# would have handed over the explanation of the prompt instead of the prompt.
PROMPT_HEAD = re.compile(
    r"^##\s*(?:the\s+)?(?:prompt|template|prompt\s+template)\b", re.I)
ANY_HEAD = re.compile(r"^##\s")
# The same marker written as a plain paragraph instead of a heading. Content
# may follow on the same line.
PROMPT_LABEL = re.compile(r"^\s*(?:the\s+)?prompt\s*:\s*(.*)$", re.I)
# Commentary that follows the prompt. It explains why the prompt works, which
# is worth READING and worth never pasting into the model.
TAIL = re.compile(r"^\s*(why\s+it\s+work|why\s+this\s+work|notes?\s*[:\-]|"
                  r"swap\s*[:\-]|variations?\s*[:\-])", re.I)
# A leading attribution line - provenance, not something to paste.
LEAD_NOISE = re.compile(r"^\s*(source|credit|via|from)\s*[:\-]", re.I)


def _until_tail(lines: list[str]) -> list[str]:
    out = []
    for l in lines:
        if ANY_HEAD.match(l.strip()) or TAIL.match(l):
            break
        out.append(l)
    return out


def extract_prompt(lines: list[str]) -> tuple[str, str]:
    """Return (prompt_text, how_it_was_found), for a Notion page body.

    FOUR SHAPES turn up in a real library and all four are correct, so the
    parser handles all four rather than making three of them wrong:

      * '## The Prompt' - or '## The template' - as a heading, with more
        sections after it. Take the section. -> "section"
      * 'Prompt:' as a plain paragraph, with a 'Why it worked:' tail. Take
        from the label to the tail. -> "labelled"
      * No headings at all: the whole page IS the prompt, perhaps opening
        with a 'Source:' credit. Take the page, drop the credit.
        -> "whole page"
      * Headings, but none of them names the prompt. Nothing here can
        identify which section to take, so take ALL of it. -> "unmarked"

    THE LAST CASE IS WHY THIS IS CAREFUL. An earlier version had no such case:
    it cut the fallback at the first heading, which on a structured page
    returns the preamble sitting above it. One page came out as its three
    intro paragraphs - Purpose, Workflow, Model - with the actual template
    silently dropped. Nothing looked wrong: the card rendered, the word count
    was plausible, the Copy button worked, and what it copied was a
    description of the prompt instead of the prompt. Handing over too much is
    a trim the reader can do; handing over the wrong section is a trap.

    Trailing commentary is cut in the shapes that can identify it. Getting
    that wrong is not cosmetic either - the first version shipped 'Why it
    worked: describes only the delta...' inside the copy payload, so pasting
    that prompt would have sent the model an explanation of itself.
    """
    body = [l for l in lines if l.strip()]
    if not body:
        return "", "empty"

    for n, line in enumerate(body):
        if PROMPT_HEAD.match(line.strip()):
            text = "\n".join(_until_tail(body[n + 1:])).strip()
            if text:
                return text, "section"

    for n, line in enumerate(body):
        m = PROMPT_LABEL.match(line)
        if m:
            rest = [m.group(1)] if m.group(1).strip() else []
            text = "\n".join(rest + _until_tail(body[n + 1:])).strip()
            if text:
                return text, "labelled"

    kept = list(body)
    while kept and LEAD_NOISE.match(kept[0]):
        kept.pop(0)

    # THE DANGEROUS CASE. If the page has headings but none of them named the
    # prompt, cutting at the first heading returns whatever sat above it -
    # which on a structured page is the preamble, never the prompt. So when
    # headings exist, keep the WHOLE page, headings and all.
    if any(ANY_HEAD.match(l.strip()) for l in kept):
        return "\n".join(kept).strip(), "unmarked"

    return "\n".join(_until_tail(kept)).strip(), "whole page"


DELETE_MARK = re.compile(r"^\s*(🗑️?|\[?to.?delete\]?)", re.I)
DUMP_WORDS = re.compile(r"raw dump|parking lot|unfiled|not yet curated", re.I)


REFERENCE = config.REFERENCE_DIR
# Optional. A video downloader that names files `... [<post id>].<ext>` puts
# the same id that is in the source URL into the filename. So ONE field - the
# URL - gives both a link and a deterministic way to find the local copy. No
# second field to drift, and no parsing of prose.
REF_ID = re.compile(r"/(?:reel|videos?|watch|shorts|posts)/(\d{6,})|[?&]v=(\d{6,})"
                    r"|/([A-Za-z0-9_-]{8,})/?$")
REF_EXT = (".mp4", ".mov", ".webm", ".mkv")


def local_reference(url: str) -> str | None:
    """The downloaded copy of `url` in reference_dir, if there is one.

    Matched on the post id carried in the filename, never on the title:
    titles are social-post text full of emoji. The id is the only stable join.
    """
    if not url or REFERENCE is None or not REFERENCE.is_dir():
        return None
    m = REF_ID.search(url.rstrip("/"))
    if not m:
        return None
    ident = next((g for g in m.groups() if g), None)
    if not ident:
        return None
    try:
        for f in REFERENCE.iterdir():
            if f.suffix.lower() in REF_EXT and ("[%s]" % ident) in f.name:
                return str(f).replace("\\", "/")
    except OSError:
        pass
    return None


def flags_for(item: dict) -> list[str]:
    """Health flags, same idea as a skill's. Only things worth acting on."""
    local = item.get("source") == "local"
    f = []
    if DELETE_MARK.match(item["name"]):
        f.append("Marked TO DELETE — superseded, but still in the library.")
    if not item["tags"]:
        f.append("No tags, so no category. Add a tags: line to its frontmatter "
                 "and it files itself." if local else
                 "No tags, so no category. Tag it in Notion and it files itself.")
    if item.get("body_from") == "unmarked":
        f.append("The page has sections but none is named as the prompt, so Copy "
                 "gives you all of them. Retitle the right one “The Prompt” and "
                 "this picks it up.")
    if not item["body"]:
        f.append("The file has no prompt text below its frontmatter." if local else
                 "The Notion page is empty — there is no prompt here to copy.")
    elif DUMP_WORDS.search(item["for"] or "") or DUMP_WORDS.search(item["name"]):
        f.append("A parking lot, not a single prompt — Copy gives you the whole "
                 "dump. Split it into separate prompts to make it usable.")
    return f


# --------------------------------------------------------------------------
# export (Notion only)
# --------------------------------------------------------------------------

def export(quiet: bool = False) -> list[dict]:
    tok = token()
    db = config.NOTION_DB_ID
    if not db:
        raise SystemExit("notion.db_id is not set in kit.config.json.")
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = api(tok, "databases/%s/query" % db, "POST", body)
        if res is None:
            raise SystemExit("Notion unreachable — prompts.json left untouched.")
        rows.extend(res.get("results", []))
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")

    out = []
    for n, page in enumerate(rows, 1):
        props = page.get("properties", {})
        name = flatten(props.get("Name")) or "(untitled)"
        if not quiet:
            say("    %d/%d  %s" % (n, len(rows), name[:58]))
        tags = flatten(props.get("Tags")) or []
        text, how = extract_prompt(block_text(tok, page["id"]))
        item = {
            "name": name,
            "for": flatten(props.get("For")) or "",
            "origin": flatten(props.get("Origin")) or "",
            "ref": flatten(props.get("Reference")) or "",
            "tags": tags,
            "cat": category_for(tags),
            "tool": tool_for(tags),
            "body": text,
            "body_from": how,
            "words": len(text.split()),
            "tokens": round(len(text) / 4),
            "url": page.get("url", ""),
            "created": page.get("created_time", ""),
            "edited": page.get("last_edited_time", ""),
            "source": "notion",
        }
        item["flags"] = flags_for(item)
        out.append(item)

    # Refuse to replace a good cache with a bad one. A partial export that
    # overwrites seventeen good rows with three is worse than no export - it
    # looks like the library shrank. The floor is about the CONTENT, not the
    # count: rows can legitimately be added or deleted, but a run where most
    # pages came back bodyless means the blocks API failed.
    with_body = sum(1 for i in out if i["body"])
    if out and with_body < len(out) * 0.6:
        raise SystemExit(
            "refusing to write: only %d of %d rows came back with a body. "
            "That is an API failure, not an empty library. Cache untouched."
            % (with_body, len(out)))

    tmp = CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CACHE)
    return out


# --------------------------------------------------------------------------
# your own state: stars, tags, copy counts
# --------------------------------------------------------------------------

def load_favs() -> set[str]:
    """Starred prompts. A SEPARATE file from the skills' favourites.json.

    Sharing one file was the obvious move and is wrong twice over: that file
    is keyed by name, and /api/fav validates every name against the scanned
    SKILL inventory, so a starred prompt would be rejected as "not a known
    skill". Two inventories, two lists - and each stays readable on its own.
    """
    try:
        v = json.loads(FAVS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {x for x in v if isinstance(x, str)} if isinstance(v, list) else set()


def save_favs(names) -> set[str]:
    clean = sorted({n for n in names if isinstance(n, str)})
    FAVS.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    return set(clean)


def load_ktags() -> dict:
    """Your own findability tags on a prompt - NOT the prompt's `tags` field.

    That collision is exactly why this is called ktags everywhere rather than
    tags: every prompt already carries a `tags` field from its file (or its
    Notion row), which drives the category. A tag you type into "+ Tag" in the
    page is a separate, Kit-only label, kept in prompt_ktags.json.
    """
    try:
        v = json.loads(KTAGS.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ktags(tags: dict) -> dict:
    clean = {}
    for n, v in tags.items():
        if not isinstance(n, str) or not isinstance(v, list):
            continue
        vv = sorted({t.strip() for t in v if isinstance(t, str) and t.strip()})
        if vv:
            clean[n] = vv
    KTAGS.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    return clean


def load_usage() -> dict:
    """{name: {"n": copies, "last": epoch}} — written by serve.py on copy.

    A SKILL's use is mined from transcripts, because invoking one leaves a
    record. A PROMPT leaves none: you copy it out of here and paste it
    somewhere else, and nothing on this machine ever sees that. So the only
    moment Kit can honestly observe is the copy, and this counts exactly that.

    It was tempting to mine the transcripts anyway - prompt names DO appear
    there. Checked before shipping: every hit was the library talking about
    itself (notes about curating it, the export being read), and not one was
    a prompt being used. "Pre-Mortem: 7 uses" would have been a fabricated
    ranking presented as a measurement. The counter starts at zero instead,
    and the page says so.
    """
    try:
        v = json.loads(USAGE.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def bump(name: str) -> dict:
    u = load_usage()
    rec = u.get(name) or {"n": 0, "last": 0}
    rec["n"] = int(rec.get("n", 0)) + 1
    rec["last"] = time.time()
    u[name] = rec
    tmp = USAGE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(u, indent=1), encoding="utf-8")
    os.replace(tmp, USAGE)
    return rec


def days_since(ts) -> int | None:
    if not ts:
        return None
    return max(0, int((time.time() - float(ts)) / 86400))


def notion_cache() -> list[dict]:
    try:
        raw = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = [r for r in raw if isinstance(r, dict) and r.get("name")] if isinstance(raw, list) else []
    for r in rows:
        r.setdefault("source", "notion")
        r.setdefault("tags", [])
        r.setdefault("body", "")
        r.setdefault("for", "")
        r.setdefault("flags", [])
    return rows


def scan() -> tuple[list[dict], dict]:
    """Local files, plus the Notion cache if there is one. NEVER touches the
    network - see the module docstring."""
    local = local_prompts()
    names = {i["name"] for i in local}
    # A local file wins over a Notion row of the same name: the file is the
    # one you can see and edit right here.
    remote = [r for r in notion_cache() if r["name"] not in names]
    raw = local + remote

    favs, use, ktags = load_favs(), load_usage(), load_ktags()
    for i in raw:
        i["fav"] = i["name"] in favs
        i["ktags"] = ktags.get(i["name"], [])
        rec = use.get(i["name"]) or {}
        i["used"] = int(rec.get("n", 0))
        i["ago"] = days_since(rec.get("last"))
        # Recompute rather than trust a cached value: the rules live here, so
        # a cache written before a rule changed would otherwise keep reporting
        # the old category with no sign it was stale.
        i["cat"] = category_for(i.get("tags"))
        i["tool"] = tool_for(i.get("tags"), i.get("platform"))
        # Resolved at SCAN time: the file may have been downloaded five
        # minutes ago, and a card claiming a local copy that is not there is
        # worse than one that offers only the link.
        i["reffile"] = local_reference(i.get("ref") or "")

    newest = max((Path(i["file"]).stat().st_mtime for i in local
                  if i.get("file") and Path(i["file"]).exists()), default=0)
    meta = {
        "count": len(raw),
        "local": len(local),
        "notion": len(remote),
        "dir": str(PROMPTS_DIR).replace("\\", "/"),
        "notion_configured": config.notion_configured(),
        "notion_at": CACHE.stat().st_mtime if CACHE.exists() else 0,
        "at": newest,
        "stale": False,
        "copies": sum(int((use.get(i["name"]) or {}).get("n", 0)) for i in raw),
    }
    return raw, meta


def main() -> None:
    if "--export" in sys.argv:
        quiet = "--quiet" in sys.argv
        if not config.notion_configured():
            # Not an error: the local folder is the library. The server's
            # Refresh button calls this, and "nothing to pull" must read as
            # success, not as a failed pull.
            if not quiet:
                say("Notion not configured (NOTION_TOKEN + notion.db_id) - "
                    "local prompts only, nothing to pull.")
        else:
            items = export(quiet=quiet)
            say("wrote %s - %d prompts, %d with a body"
                % (CACHE.name, len(items), sum(1 for i in items if i["body"])))
        if quiet:
            return
    items, meta = scan()
    if not items:
        say("no prompts - add a .md file to %s (see prompts/README.md)" % meta["dir"])
        return
    say("%d prompts (%d local, %d from Notion), %d copies recorded"
        % (meta["count"], meta["local"], meta["notion"], meta["copies"]))
    for i in sorted(items, key=lambda x: (x["cat"], x["name"])):
        say("  %-20s %-5s %s" % (i["cat"], "%dw" % i["words"], i["name"][:52]))
    bad = [i for i in items if i["flags"]]
    if bad:
        say("\n%d with flags:" % len(bad))
        for i in bad:
            say("  %s\n    %s" % (i["name"][:60], i["flags"][0]))


if __name__ == "__main__":
    main()
