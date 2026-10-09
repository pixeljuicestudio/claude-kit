#!/usr/bin/env python3
"""
Kit — scans, then writes and opens skills.html (a static, read-only copy).

    python build.py          static page; `python serve.py` is the live one

Read-only against the skills themselves. It never edits a SKILL.md, never
moves a folder, and never writes settings.json — a plugin regression caused by
a stray settings write is not something a browser page should be able to
recreate. Stars, tags and groups are the only things it stores, in sidecars.
"""

from __future__ import annotations

import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import usage
from mode import families, skills as machine_skills
import categorise
import config
import groups as grouplib
import programs
import prompts as promptlib
import tools as toollib
import hooks as hooklib
from scan import scan, mark_collisions

HERE = Path(__file__).resolve().parent
OUT = HERE / "skills.html"
TAGS = HERE / "tags.json"
NOTION = HERE / "notion_rows.json"   # optional; see README


FAVS = HERE / "favourites.json"


def load_favs() -> set[str]:
    """Starred skills. A FILE, not localStorage.

    Tags live in localStorage and that was already a compromise: clear the
    browser and they are gone, and no other tool can see them. Favourites are
    a curated list worth more than that - it is the answer to "which of my 173
    do I actually reach for" - so it lives on disk next to the code, survives
    a browser wipe, and any other script could read it.

    One source of truth, deliberately. A localStorage mirror would drift, and
    this project has been bitten by two-sources-of-truth more than once.

    KEYED BY NAME, and two skills can share one. Six cards do: `access` and
    `configure` each appear three times, all in the never-installed
    marketplace catalogue. Starring one stars all three of that name. Keying
    by path would fix it and would also make this file unreadable to a human
    and useless to any other tool, which is most of why it is a file at all -
    so the trade is deliberate. Revisit if a name collision ever appears among
    skills you actually use.
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


def load_tags() -> dict:
    try:
        return json.loads(TAGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_tags(tags: dict) -> dict:
    """Written by the server on every tag edit - see save_favs for why this
    is a file rather than localStorage alone. An empty tag list is dropped,
    not stored, so tags.json never accumulates dead keys for a skill whose
    last tag was just removed."""
    clean = {}
    for n, v in tags.items():
        if not isinstance(n, str) or not isinstance(v, list):
            continue
        vv = sorted({t.strip() for t in v if isinstance(t, str) and t.strip()})
        if vv:
            clean[n] = vv
    TAGS.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    return clean


def load_notion() -> list:
    try:
        return json.loads(NOTION.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def row_key(name: str) -> str:
    """Normalise a grid row title down to the identifier.

    Rows are titled inconsistently - some are bare identifiers, some carry a
    prose tail ('Power-design (pitch decks...)'), some a '(built-in)' suffix.
    The naming rule on the Skills Library page says the title should BE the
    identifier; until every row follows it, normalise rather than mis-report.
    """
    n = name.strip()
    for sep in (" — ", " – ", " - ", ": "):
        if sep in n:
            n = n.split(sep)[0]
    if " (" in n:
        n = n.split(" (")[0]
    # Slugify: a row titled 'Factual Defensibility' and a folder named
    # 'factual-defensibility' are the same skill, not two misses.
    return "-".join(n.strip().lower().split())


def reconcile(items: list[dict], rows: list) -> dict:
    """Diff the on-disk inventory against the Notion grid.

    Exact match on the normalised identifier - never substring. Substring
    matching is what let init / morning / setup-cowork / az-jobs-voice pass
    an audit while having no row of their own.

    Only compares rows that SHOULD have a file. A 'Notion playbook' row is a
    page, not a skill, so its absence from disk is correct, not drift.
    """
    if not rows:
        return {"available": False}

    FILED = {"claude code", "skills repo", "mixed"}
    filed_rows = {
        row_key(r["name"]): r for r in rows
        if r.get("name") and (r.get("home", "").lower() in FILED)
        and r.get("status", "Active") != "Retired"
    }
    # Skills that live on disk and are ours to track. Catalogue and disabled
    # plugin skills are third-party - they were never meant to have rows.
    disk = {row_key(i["name"]): i for i in items if i["reach"] != "off"}

    return {
        "available": True,
        "missing_row": sorted(set(disk) - set(filed_rows)),
        "missing_file": sorted(set(filed_rows) - set(disk)),
        "rows": len(filed_rows),
        "files": len(disk),
        "playbooks": sum(1 for r in rows if r.get("home") == "Notion playbook"),
    }


def render_html(live: bool = False) -> str:
    """Build the page. `live` tells the page a server is behind it, so the
    toggles write instead of falling back to copying a command."""
    return _build(live)[0]


def _server_up(host: str = "127.0.0.1", port: int | None = None) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.25)
        return s.connect_ex((host, port or config.port())) == 0


def main() -> None:
    html, out_path = _build(False)
    out_path.write_text(html, encoding="utf-8")
    # Never open a dead file:// tab while a live server is running. Doing that
    # is how "the toggle is broken" happened: a rebuild popped open a
    # read-only copy that looks identical to the served page, and every switch
    # in it is inert. The served page is the one that works, so open that.
    if _server_up():
        print("  server is up - opening the live page, not the static file")
        webbrowser.open(f"http://127.0.0.1:{config.port()}/")
    else:
        webbrowser.open(out_path.as_uri())


def _current_port() -> int:
    """The port the page should tell you to open: the running server's own
    (it may have been started with --port), else the configured one."""
    srv = sys.modules.get("serve") or sys.modules.get("__main__")
    p = getattr(srv, "PORT", None)
    return p if isinstance(p, int) else config.port()


def _build(live: bool):
    items, meta = scan()
    mark_collisions(items)

    tags = load_tags()
    favs = load_favs()
    u = usage.load()
    _mods = families(set(machine_skills()))
    categorise.assign(items)
    for it in items:
        it["tags"] = tags.get(it["name"], [])
        it["fav"] = it["name"] in favs
        it["used"] = u["counts"].get(it["name"], 0)
        it["ago"] = usage.days_since(u["last"].get(it["name"]))
        # A skill routed to by a parent cannot go Manual without breaking it.
        it["locked"] = it["name"] in _mods and it["reach"] != "off"

    order = {"auto": 0, "manual": 1, "off": 2}
    items.sort(key=lambda i: (order.get(i["reach"], 9), i["name"].lower()))

    # Programs are a SECOND inventory, not more skills. They keep their own
    # list and their own facets because nothing about them maps onto reach,
    # origin or location - a scanner that forced them into the skill shape
    # would have to invent values for fields that do not apply, and invented
    # values are indistinguishable from measured ones once rendered.
    progs, pmeta = programs.scan()
    # Link AFTER both inventories exist. Only our own skills are searched -
    # a plugin skill's body is someone else's text and naming a program there
    # would mean nothing about this drive.
    programs.link_skills(progs, [i for i in items if i["home"] in ("machine", "cloud")])
    programs.attach_usage(progs, programs.mine_usage())
    progs.sort(key=lambda p: (p["group"].lower(), p["name"].lower()))

    # Prompts are a THIRD inventory: one .md file each in prompts_dir, plus an
    # optional Notion cache. scan() never touches the network, so a dead API
    # makes the Notion rows stale, never slow. See prompts.py.
    prom, prmeta = promptlib.scan()
    prom.sort(key=lambda p: (p["cat"], p["name"].lower()))

    # Tools are a FOURTH inventory and the only one whose membership is a
    # decision rather than a rule - see tools.py. Hooks are a FIFTH, and the
    # only one where settings.json is the truth and the disk is the reconcile.
    tls, tmeta = toollib.scan()
    tls.sort(key=lambda t: t["name"].lower())
    hks, hmeta = hooklib.scan()

    payload = {
        "items": items,
        "meta": meta,
        "notion": reconcile(items, load_notion()),
        "usage": {"scanned": u["scanned"]},
        "categories": categorise.CATEGORIES + ["Unsorted"],
        "programs": progs,
        "pmeta": pmeta,
        "prompts": prom,
        "prmeta": prmeta,
        "pcats": promptlib.CATEGORIES + ["Unsorted"],
        "tools": tls,
        "tmeta": tmeta,
        "hooks": hks,
        "hmeta": hmeta,
        "groups": grouplib.load_groups(),
        "stamp": datetime.now().strftime("%a %d %b %Y, %H:%M"),
        "port": _current_port(),
    }

    counts: dict[str, int] = {}
    for it in items:
        counts[it["reach"]] = counts.get(it["reach"], 0) + 1

    print("=== KIT ===")
    for k in ("auto", "manual", "off"):
        print(f"  {counts.get(k, 0):4}  {k}")
    flagged = sum(1 for i in items if i["flags"])
    print(f"  {flagged:4}  with health flags")
    print(f"  {tmeta['count']:4}  tools ({len(tmeta['unlisted'])} unlisted on disk)")
    print(f"  {hmeta['registered']:4}  hooks registered "
          f"({hmeta['orphans']} never fire, {hmeta['missing']} missing)")
    if tmeta["missing"]:
        print(f"  ! rostered tools missing from disk: {', '.join(tmeta['missing'])}")
    if tmeta["unlisted"]:
        print(f"  ! unlisted under tools_root: {', '.join(tmeta['unlisted'])}")
    if not tmeta.get("configured", True):
        print("  (tools_root not configured - Tools tab shows setup instructions)")
    for p in hmeta["problems"]:
        print(f"  ! {p}")
    if not payload["notion"]["available"]:
        print("  (no notion_rows.json - reconcile tab disabled)")

    payload["live"] = live
    return TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False)), OUT


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kit</title>
<style>
:root{
  --bg:#14171C; --panel:#1C2027; --panel2:#22272F; --edge:#2C333D; --edge2:#3A424E;
  --ink:#E9E7E2; --mute:#8B93A0; --dim:#808993;
  --live:#5FBF87; --amber:#E0A33E; --off:#B4614E; --blue:#6E9FD4;
  --t-on:#4DA3FF; --t-off:#E8C14A; --t-none:#E0574A; --t-src:#4FB3A5;
  /* Gold is its OWN colour, not --t-off. The tier amber already means
     "installed, off"; reusing it would make a starred skill read as a state. */
  --gold:#F0B429; --gold-dim:#8A6D1F;
  /* CONNECTED is a step past --t-on ("a process exists"), reserved for a
     verified live signal (running.CONNECT_CHECKS). Not --live: that colour
     already means "an event just fired", and this is a steady state. */
  --t-conn:#22D3EE; --t-conn-ink:#04141A;
  /* Groups get their OWN colour for the same reason gold split from --t-off:
     --blue already means "tag" on every card, and a grouped item would read
     as tagged rather than grouped if it borrowed that colour. */
  --purple:#A78BFA;
  --mono:"Cascadia Mono",Consolas,ui-monospace,monospace;
  --ui:"Segoe UI Variable Display","Segoe UI",Inter,system-ui,sans-serif;
}
:root[data-theme="light"]{
  --bg:#F7F6F3; --panel:#FFFFFF; --panel2:#F0EEEA; --edge:#DEDAD2; --edge2:#C6C1B6;
  --ink:#1E2227; --mute:#5C636D; --dim:#6B717A;
  --live:#2F855A; --amber:#A9691A; --off:#B0432C; --blue:#2B6CB0;
  --t-on:#0B67C7; --t-off:#9A7400; --t-none:#C0392B; --t-src:#1F7A6E;
  --gold:#C98A04; --gold-dim:#E0C078;
  --t-conn:#004C99; --t-conn-ink:#FFFFFF;
  --purple:#6B46C1;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--ui);font-size:15px;
     line-height:1.5;-webkit-font-smoothing:antialiased;display:flex;height:100vh;overflow:hidden}

/* ---- sidebar ---- */
aside{width:236px;flex:none;background:var(--panel);border-right:1px solid var(--edge);
      overflow-y:auto;padding:20px 0 40px}
aside h1{font-size:13px;font-weight:650;letter-spacing:.18em;text-transform:uppercase;
         margin:0 20px 18px;color:var(--ink)}
.sec{font-family:var(--mono);font-size:10px;letter-spacing:.16em;text-transform:uppercase;
     color:var(--dim);margin:22px 20px 8px}
.nav{display:flex;align-items:center;gap:9px;padding:7px 20px;cursor:pointer;
     border-left:2px solid transparent;font-size:13.5px;color:var(--mute)}
.nav:hover{background:var(--panel2);color:var(--ink)}
.nav.on{background:var(--panel2);color:var(--ink);border-left-color:var(--amber)}
.nav .c{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--dim)}
.dot{width:7px;height:7px;border-radius:50%;flex:none}
.d-auto{background:var(--t-on)} .d-manual{background:var(--t-off)}
.d-off{background:var(--t-none)}

/* ---- main ---- */
main{flex:1;display:flex;flex-direction:column;overflow:hidden}
header{padding:18px 26px 0;border-bottom:1px solid var(--edge);background:var(--bg)}
.tools{display:flex;gap:10px;align-items:center}
#q{flex:1;background:var(--panel);border:1px solid var(--edge);border-radius:4px;
   color:var(--ink);font-family:var(--mono);font-size:14px;padding:10px 13px}
#q:focus{outline:none;border-color:var(--amber)}
#q::placeholder{color:var(--dim)}
.btn{background:var(--panel);border:1px solid var(--edge);color:var(--mute);font-family:var(--mono);
     font-size:11px;letter-spacing:.08em;text-transform:uppercase;padding:9px 13px;border-radius:4px;
     cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--amber);color:var(--amber)}
.btn.on{border-color:var(--amber);color:var(--amber);background:#2A2318}
.tagbar{display:flex;gap:6px;flex-wrap:wrap;padding:12px 0 14px}
.tag{font-family:var(--mono);font-size:11px;padding:4px 9px;border-radius:11px;cursor:pointer;
     background:var(--panel2);border:1px solid var(--edge);color:var(--mute)}
.tag:hover{border-color:var(--edge2);color:var(--ink)}
.tag.on{background:#2A2318;border-color:var(--amber);color:var(--amber)}

/* ---- phrase test ---- */
#phrase{display:none;padding:14px 26px;background:var(--panel);border-bottom:1px solid var(--edge)}
#phrase.show{display:block}
#phrase p{margin:0 0 9px;font-size:12.5px;color:var(--mute)}
#pin{width:100%;background:var(--bg);border:1px solid var(--edge2);border-radius:4px;
     color:var(--ink);font-size:14px;padding:10px 13px;font-family:var(--ui)}
#pin:focus{outline:none;border-color:var(--amber)}
#pout{margin-top:12px;display:grid;gap:6px}
.hit{display:grid;grid-template-columns:44px 1fr;gap:12px;align-items:center;
     background:var(--bg);border:1px solid var(--edge);border-radius:4px;padding:9px 12px}
.score{font-family:var(--mono);font-size:12px;color:var(--amber);text-align:right}
.hit .n{font-family:var(--mono);font-size:13px}
.hit .w{font-size:12px;color:var(--dim);margin-top:2px}

/* ---- cards ---- */
.scroll{flex:1;overflow-y:auto;padding:16px 26px 60px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--edge);border-radius:5px;padding:13px 15px;
      display:flex;flex-direction:column;gap:8px}
.card:hover{border-color:var(--edge2)}
.top{display:flex;align-items:center;gap:8px}
.nm{font-family:var(--mono);font-size:13.5px;font-weight:600;flex:1;word-break:break-word}
/* ---- tool brand icon ----
   Only the handful of tools that ARE an integration with a real outside
   app (Resolve, Premiere, After Effects, Notion, YouTube) get that app's
   mark - everything else gets a plain lettered placeholder, on purpose:
   inventing a logo for a homegrown tool would be the same mistake describe()
   already refuses to make for a missing README. Muted vs coloured is the
   scan signal - brand icons are meant to jump out, the fallback is meant not to. */
.ticon{width:16px;height:16px;border-radius:4px;flex:none;display:flex;
  align-items:center;justify-content:center;overflow:hidden}
.ticon svg{width:100%;height:100%;display:block}
.tico-generic{background:var(--panel2);border:1px solid var(--edge2);
  color:var(--mute);font-family:var(--mono);font-size:9px;font-weight:700}
.list .ticon{width:14px;height:14px}
.ds{font-size:12.5px;color:var(--mute);display:-webkit-box;-webkit-line-clamp:3;
    -webkit-box-orient:vertical;overflow:hidden}
.card.open .ds{-webkit-line-clamp:unset}
.meta{display:flex;gap:5px;flex-wrap:wrap;align-items:center;margin-top:auto;padding-top:4px}
.chip{font-family:var(--mono);font-size:10px;padding:3px 7px;border-radius:3px;
      background:var(--panel2);color:var(--dim);border:1px solid var(--edge)}
.chip.tk{color:var(--mute)}
.chip.tg{color:var(--blue);border-color:#2E4358}
.chip.gp{color:var(--purple);border-color:#4C3D7A;cursor:pointer}
.chip.gp:hover{border-color:var(--purple)}
.flag{font-family:var(--mono);font-size:10.5px;color:var(--amber);padding:2px 0}
.coll{font-family:var(--mono);font-size:10.5px;color:var(--dim)}
.acts{display:flex;gap:6px}
.acts .btn{flex:1;text-align:center;padding:7px 8px;font-size:10px}
.btn.done{border-color:var(--live);color:var(--live)}
/* An <a> styled as a .btn needs the button's own box back - links are inline
   and ignore flex:1, so the row silently stops being equal-width. */
a.btn{display:inline-flex;align-items:center;justify-content:center;
  text-decoration:none;line-height:1.2}
.acts .btn.ref{color:var(--t-src);border-color:#2A5E7A}
.acts .btn.ref:hover{color:var(--ink);border-color:var(--t-src)}

/* ---- modal: the group picker (and anything else that needs one) ----
   A centered overlay, not a dropdown anchored to the clicked button - the
   grid scrolls under a fixed-position anchor and a card near the viewport
   edge would push a dropdown off-screen. One modal, reused: openModal()
   just swaps #modal-body's innerHTML. */
#modal-backdrop{position:fixed;inset:0;background:#000A;z-index:20;
  display:flex;align-items:center;justify-content:center}
#modal-backdrop[hidden]{display:none}
#modal-panel{background:var(--panel);border:1px solid var(--edge2);border-radius:7px;
  width:360px;max-width:90vw;max-height:80vh;overflow-y:auto;padding:18px 20px;
  box-shadow:0 20px 60px #000A}
#modal-panel h3{margin:0 0 4px;font-size:14px}
#modal-panel .sub{font-size:12px;color:var(--dim);margin:0 0 14px}
.mrow{display:flex;align-items:center;gap:9px;padding:7px 0;font-size:13.5px}
.mrow input[type=checkbox]{width:15px;height:15px;accent-color:var(--purple);flex:none}
.mrow .gn{flex:1;word-break:break-word}
.mrow .gc{font-family:var(--mono);font-size:11px;color:var(--dim)}
.mnew{display:flex;gap:6px;margin-top:10px;padding-top:12px;border-top:1px solid var(--edge)}
.mnew input{flex:1;background:var(--bg);border:1px solid var(--edge2);border-radius:4px;
  color:var(--ink);font-size:13px;padding:8px 10px}
.mempty{font-size:12.5px;color:var(--dim);padding:4px 0 2px}
.mclose{display:block;width:100%;margin-top:14px;text-align:center}

/* ---- groups: a member is someone else's card with one thing added ----
   The card itself is whatever card()/promptCard()/toolCard() already
   render for that item's own tab, unmodified - the ✕ sits ON TOP of it in
   its own absolutely-positioned corner rather than inside .acts, so this
   never has to know that card's internal layout. */
.group-member{position:relative}
.group-remove{position:absolute;top:10px;right:10px;width:20px;height:20px;border-radius:50%;
  background:var(--panel2);border:1px solid var(--edge2);color:var(--dim);
  font-size:12px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center}
.group-remove:hover{color:var(--off);border-color:var(--off)}
.ghost .ds{color:var(--amber)}
#bar{padding:9px 26px;border-bottom:1px solid var(--edge);font-size:12.5px;color:var(--mute);display:flex;align-items:center;gap:8px;flex-wrap:wrap}
#bar b{color:var(--amber)}
.fchip{font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:10px;background:var(--panel2);border:1px solid var(--edge2);color:var(--ink)}
#clr{padding:4px 9px;font-size:10px;margin-left:4px}
select.btn{appearance:none;padding-right:22px;
  background-image:linear-gradient(45deg,transparent 50%,currentColor 50%),
                   linear-gradient(135deg,currentColor 50%,transparent 50%);
  background-position:calc(100% - 12px) 13px,calc(100% - 8px) 13px;
  background-size:4px 4px;background-repeat:no-repeat}
select.btn option{background:var(--panel);color:var(--ink)}
.cathead{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--amber);margin:20px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--edge);
  display:flex;align-items:baseline;gap:8px}
.cathead span{color:var(--dim);letter-spacing:0;text-transform:none;font-size:11px}
.grp1:first-child .cathead{margin-top:0}
.sw{position:relative;width:34px;height:18px;border-radius:9px;flex:none;cursor:pointer;
    background:var(--edge2);border:1px solid var(--edge2);transition:background .15s,transform .12s}
.sw::after{content:"";position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;
    background:var(--bg);transition:transform .15s}
.sw.on{background:var(--t-on);border-color:var(--t-on)}
.sw.on::after{transform:translateX(16px)}
.sw.busy{opacity:.5;cursor:wait}
/* A locked switch must still show its STATE. `background:transparent` used to
   be here and it wiped the blue off hyperframes-animation - a skill that is
   Auto and ON rendered as an empty switch beside a label reading AUTO, which
   flatly contradicts itself. Dead now means dimmed and not-clickable, never
   "shows the wrong state". */
.sw.dead{cursor:not-allowed;opacity:.4}
/* Every other clickable thing here (.btn, .tag, .card, .star) reacts on
   hover; the switch didn't, despite being the single most-clicked control
   in the app. Scale rather than a colour/border change, because border
   is already load-bearing for state (.sw.on's border IS the "on" signal)
   and a hover treatment must not read as a state change. */
.sw:hover:not(.busy):not(.dead){transform:scale(1.08)}
/* The read-only page looked IDENTICAL to the working one, and that cost real
   confusion: `python build.py` writes skills.html and opens it as a file://
   tab, where nothing can write, so every switch ghosts out - including the 32
   Auto ones that work perfectly when served. "The toggle is broken" was the
   entirely reasonable conclusion. A hover tooltip is not enough when nine in
   ten switches are dead for unrelated reasons. */
/* Emptying innerHTML does NOT remove the box: border, padding and margin all
   still render, so the live page grew a hollow amber bar across the top. */
#offline:empty{display:none}
#offline{margin:0 0 12px;padding:11px 14px;border-radius:6px;font-size:13px;
  background:color-mix(in srgb,var(--t-off) 12%,var(--panel));
  border:1px solid var(--t-off);color:var(--ink)}
#offline b{color:var(--t-off)}
#offline code{font-family:var(--mono);font-size:12px;background:var(--panel2);
  padding:2px 6px;border-radius:4px}
/* Favourite star. Outline on every card; gold when starred. The hit area is
   padded well beyond the glyph - a 14px star is a hard target with a mouse. */
.star{flex:none;cursor:pointer;padding:0 4px;margin:0 -3px 0 0;font-size:15px;
  /* Fixed box, centred glyph. Without the height the 15px glyph drove the
     whole row: list cards went 121px -> 141px, on every one of 173. A
     decoration must never set the height of the thing it decorates. */
  height:18px;display:inline-flex;align-items:center;justify-content:center;
  color:var(--edge2);background:none;border:0;border-radius:4px;
  transition:color .12s,transform .12s}
.star:hover{color:var(--gold-dim);transform:scale(1.18)}
.star.on{color:var(--gold)}
.star.on:hover{color:var(--gold)}
.star.busy{opacity:.4;pointer-events:none}
.list .star{font-size:13px}
.swrap{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:9.5px;
    letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.list .swrap{grid-column:3}
.dens{display:flex;align-items:center;gap:7px;font-family:var(--mono);font-size:10px;
      letter-spacing:.08em;text-transform:uppercase;color:var(--dim);white-space:nowrap}
.dens input{width:78px;accent-color:var(--amber);cursor:pointer}

/* Density is CSS-only so dragging the slider never re-renders 173 cards. */
[data-den="1"] .ds,[data-den="1"] .why,[data-den="1"] .meta,
[data-den="1"] .flag,[data-den="1"] .coll,[data-den="1"] .pa{display:none}
[data-den="2"] .ds{-webkit-line-clamp:1}
[data-den="2"] .why,[data-den="2"] .pa,[data-den="2"] .coll{display:none}
[data-den="3"] .pa{display:none}
[data-den="4"] .ds{-webkit-line-clamp:unset}
[data-den="1"] .card{padding:9px 13px;gap:6px}
[data-den="1"] .grid{grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}

/* List view: one line per skill. Name, description, chips and actions are
   four columns, so nothing wraps onto a second grid row - the detail lines
   (why / flags / path) only appear at max density, where height is expected. */
.list .grid{display:block}
.list .card{display:grid;
            grid-template-columns:minmax(140px,230px) minmax(0,1fr) auto auto;
            align-items:center;gap:0 16px;padding:6px 14px;margin-bottom:3px;border-radius:3px}
.list .top{grid-column:1;min-width:0}
/* Kit's own names are kebab-case, and most run past the 140-230px name
   column at two or three words - "seedance-shotlist-director" wrapped to
   3 lines while "gepeto" sat at 1, so row height in list view followed
   word length instead of anything meaningful. Truncate instead of wrapping;
   the full name is one hover away via `title`, same trade every OS file
   list makes. Cards view is untouched - there, wrapping onto a second line
   costs nothing. */
.list .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.list .ds{grid-column:2;-webkit-line-clamp:1;margin:0;min-width:0}
.list .meta{grid-column:3;margin:0;padding:0;flex-wrap:nowrap}
.list .acts{grid-column:4;gap:5px}
.list .acts .btn{flex:none;padding:5px 8px}
.list .why,.list .flag,.list .coll,.list .pa{display:none}
.list[data-den="4"] .why,.list[data-den="4"] .flag,
.list[data-den="4"] .coll,.list[data-den="4"] .pa{display:block;grid-column:1/-1;font-size:11px}
.list[data-den="4"] .card{align-items:start;padding:10px 14px}
.list[data-den="1"] .card{padding:4px 14px}
/* Without this, list density 2 and 3 render identically. */
.list[data-den="2"] .meta{display:none}

/* Too narrow for four columns - stack, or the description collapses to nothing. */
@media (max-width:820px){
  .list .card{grid-template-columns:1fr auto}
  .list .top{grid-column:1} .list .acts{grid-column:2;grid-row:1/3}
  .list .ds,.list .meta{grid-column:1}
}
.empty{color:var(--dim);font-family:var(--mono);font-size:13px;padding:40px 0}
.why{font-size:11.5px;color:var(--dim);font-style:italic}
.pa{font-family:var(--mono);font-size:10.5px;color:var(--dim);word-break:break-all}
.across{margin:18px 2px 4px;font-size:12.5px;color:var(--dim);display:flex;
  align-items:center;gap:10px}
.across .btn{padding:3px 10px;font-size:11px}

/* ---- section tabs ----
   These sit above the mode switch and mean something different from it, so
   they cannot look the same. Rendered as vertical nav rows they were
   indistinguishable from Mine/Catalogue - two levels of navigation, one amber
   bar, and no way to tell which level you were changing. */
.tabs{display:flex;border-bottom:1px solid var(--edge);margin:2px 0 10px}
.tab{flex:1;text-align:center;padding:8px 2px 7px;cursor:pointer;
  font-family:var(--mono);font-size:9px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--dim);border-bottom:2px solid transparent;
  transition:color .12s,border-color .12s}
.tab:hover{color:var(--mute)}
.tab.on{color:var(--ink);border-bottom-color:var(--amber)}
.tab .c{display:block;font-family:var(--ui);font-size:11px;letter-spacing:0;
  margin-top:3px;color:var(--dim)}
.tab.on .c{color:var(--amber)}
/* An unbuilt tab is shown, not hidden: it says what is coming and what is not
   there yet. A tab that looks live and does nothing is worse than an obvious
   placeholder. */
.tab.soon{opacity:.34;cursor:not-allowed}
.tab.soon:hover{color:var(--dim)}

/* The mode switch is now subordinate to the tabs, and reads that way. */
#modenav .nav{font-size:12.5px;padding-top:5px;padding-bottom:5px}

/* ---- programs ---- */
.pb{margin-left:auto;flex:none;font-family:var(--mono);font-size:9.5px;
  letter-spacing:.1em;padding:2px 6px;border-radius:3px;border:1px solid}
.pb.run {color:var(--t-on);border-color:var(--t-on)}
.pb.lib {color:var(--dim);border-color:var(--edge2)}
.pb.test{color:var(--amber);border-color:var(--amber)}
.pmeta{font-family:var(--mono);font-size:10.5px;color:var(--dim);margin-left:7px}
.pflags{margin:8px 0 0;display:flex;flex-wrap:wrap;gap:5px}
.pflags code{background:var(--panel2);border:1px solid var(--edge);
  border-radius:3px;padding:1px 5px;font-size:10.5px}
.chip.ok  {color:var(--t-on);border-color:var(--t-on)}
.chip.warn{color:var(--amber);border-color:var(--amber)}
.chip.sk  {color:var(--blue);border-color:#2E4358;cursor:pointer}
.chip.sk:hover{border-color:var(--blue)}
/* The rest of the docstring. Clamped to two lines so a 40-line "why this
   exists" header cannot make one card taller than the screen; the card's
   existing click-to-open handler unclamps it. */
.pdoc{white-space:pre-wrap;font-size:11px;line-height:1.5;color:var(--dim);
  margin:6px 0 2px;cursor:pointer;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden}
.card.open .pdoc{-webkit-line-clamp:unset}
/* The prompt text itself. Clamped hard on purpose: the Saloon Brawl prompt is
   1,094 words, and unclamped it makes one card several screens tall and the
   grid unreadable. Click opens it; the scroll cap keeps an open card from
   doing the same thing. Monospace because these are full of @image_1 tokens
   and 0.0s-1.5s timings, which proportional type mangles. */
.pbody{white-space:pre-wrap;font-family:var(--mono);font-size:11px;line-height:1.55;
  color:var(--ink);background:var(--panel2);border:1px solid var(--edge);
  border-radius:4px;padding:8px 9px;margin:8px 0 2px;cursor:pointer;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.card.open .pbody{-webkit-line-clamp:unset;max-height:440px;overflow:auto}
.pfrom{font-family:var(--mono);font-size:10px;color:var(--dim);margin:5px 0 0}
.list .pbody,.list .pfrom,.list .pnotes{display:none}
.pnotes{margin:4px 0 0;font-size:11px;color:var(--dim)}
.pnotes summary{cursor:pointer;font-family:var(--mono);font-size:10px}
.pnotes pre{white-space:pre-wrap;font-family:var(--mono);font-size:11px;line-height:1.5;
  color:var(--mute);background:var(--panel2);border:1px solid var(--edge);border-radius:4px;
  padding:8px 9px;margin:6px 0 0;max-height:320px;overflow:auto}

/* ---- running: a STATE, so it must not pulse ----
   Made deliberately loud after the user asked for it: the first version was
   an outline chip sitting sixth in a row of six, which reads as one more
   fact rather than as the answer to "is it up". A filled badge next to the
   NAME and a solid left edge are both catchable while scrolling; a chip in
   the meta row is not. */
.card.up{border-color:#2F5F86;box-shadow:inset 3px 0 0 var(--t-on)}
.upbadge{font-family:var(--mono);font-size:9.5px;font-weight:700;
  letter-spacing:.11em;text-transform:uppercase;color:#0D1117;
  background:var(--t-on);border-radius:3px;padding:2px 7px;flex:none;
  line-height:1.55;white-space:nowrap}
.list .card.up{box-shadow:inset 3px 0 0 var(--t-on)}
/* ---- connected: a step past "up" ----
   "up" only means a process exists, and davinci-bridge/premiere-mcp are
   spawned by every open Claude Code session regardless of whether Resolve
   or Premiere is open — so it reads as true almost always for those two.
   "connected" is reserved for the tools running.CONNECT_CHECKS can verify a
   real signal from (CursorBridge's own port; Premiere's own process) and
   gets a visibly stronger colour so the two states are never confused at a
   glance. Same "steady, no pulse" rule as .up: this is a state, not an
   event. */
.card.connected{border-color:var(--t-conn);box-shadow:inset 3px 0 0 var(--t-conn)}
.connbadge{font-family:var(--mono);font-size:9.5px;font-weight:700;
  letter-spacing:.11em;text-transform:uppercase;color:var(--t-conn-ink);
  background:var(--t-conn);border-radius:3px;padding:2px 7px;flex:none;
  line-height:1.55;white-space:nowrap}
.list .card.connected{box-shadow:inset 3px 0 0 var(--t-conn)}

/* ---- hooks: the registration block ----
   The matcher is shown as code because it IS a regex the harness evaluates.
   Paraphrasing it into friendly English would be a second source of truth
   that can disagree with the first. */
.hregs{display:flex;flex-direction:column;gap:4px;margin:8px 0 0}
.hreg{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap;
  background:var(--bg);border:1px solid var(--edge);border-radius:4px;
  padding:4px 8px}
.hreg code{font-family:var(--mono);font-size:10.5px;color:var(--mute);
  word-break:break-all}
.hev{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--t-on);white-space:nowrap}
.hsc{font-family:var(--mono);font-size:9px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--gold);border:1px solid var(--edge2);
  border-radius:3px;padding:1px 4px;margin-left:auto}
.list .hregs{display:none}
[data-den="1"] .pbody,[data-den="2"] .pbody,[data-den="1"] .pnotes,[data-den="2"] .pnotes{display:none}

/* ---------- live: something just fired ----------
   Two states, because "it happened" and "it is happening" are different
   facts. .hot is the first two seconds - a bright pulse you cannot miss.
   .warm is the fade that follows, so a card that fired forty seconds ago
   still reads as recently active without competing with one firing now. */
@keyframes hotpulse{
  0%  {box-shadow:0 0 0 0 rgba(95,191,135,.55); border-color:var(--live)}
  70% {box-shadow:0 0 0 9px rgba(95,191,135,0); border-color:var(--live)}
  100%{box-shadow:0 0 0 0 rgba(95,191,135,0);   border-color:var(--live)}
}
.card.hot{border-color:var(--live);animation:hotpulse 1.1s ease-out 2}
.card.warm{border-color:#2F6B4C}
.card.hot .nm,.card.warm .nm{color:var(--live)}
.livedot{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--live);margin-left:7px;vertical-align:middle;flex:none}
.card.hot .livedot{animation:hotpulse 1.1s ease-out infinite}
/* "Running now" is a STATE, not an event, so it must not pulse - a steady
   ring reads as "still up", a pulse reads as "just happened". Conflating the
   two is what made a bridge with ten live processes look idle. */
@media (prefers-reduced-motion:reduce){
  .card.hot,.card.hot .livedot{animation:none}
}
/* The header lamp. Dim until the watcher confirms it is running - an
   indicator that looks the same whether or not it works is not an indicator. */
#lamp{font-family:var(--mono);font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim);display:flex;align-items:center;
  gap:6px;cursor:help;white-space:nowrap}
#lamp .b{width:6px;height:6px;border-radius:50%;background:var(--edge2)}
#lamp.on .b{background:var(--live)}
#lamp.on{color:var(--mute)}
#lamp.fire .b{animation:hotpulse 1.1s ease-out infinite}
table{border-collapse:collapse;width:100%;font-size:13px}
td,th{text-align:left;padding:6px 10px;border-bottom:1px solid var(--edge)}
th{font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
code{font-family:var(--mono);font-size:11.5px;color:var(--mute)}
</style></head><body>

<aside>
  <!-- KIT, not Skills: the wordmark used to name one of the tabs below it,
       so the page read "SKILLS > SKILLS 67". -->
  <h1>Kit</h1>
  <div id="sectionnav"></div>
  <div id="modenav"></div>
  <div id="favnav"></div>
  <!-- The three group headings are RELABELLED per section. They were static
       and said State / Origin / Location on every tab, which is honest only on
       Skills: the Tools tab filed "Running now" under "Location" and left an
       empty "Checks" hanging below it. A heading that describes another
       inventory is worse than no heading. -->
  <div class="sec" id="sec-a">State</div><div id="tiers"></div>
  <div class="sec" id="sec-b">Origin</div><div id="origins"></div>
  <div class="sec" id="sec-c">Location</div><div id="locs"></div>
  <div class="sec" id="sec-d">Checks</div>
  <div class="nav" id="nav-never">Never fired<span class="c" id="c-never"></span></div>
  <div class="nav" id="nav-sleep">Auto but unused<span class="c" id="c-sleep"></span></div>
  <div class="nav" id="nav-flags">Health flags<span class="c" id="c-flags"></span></div>
  <div class="nav" id="nav-rec">Notion reconcile<span class="c" id="c-rec"></span></div>
</aside>

<main>
  <header>
    <div class="tools">
      <input id="q" placeholder="Search name, description, tag…" autocomplete="off" autofocus>
      <button class="btn" id="pt">Phrase test</button>
      <button class="btn" id="ex">Export tags</button>
      <button class="btn" id="th" title="Light / dark">Light</button>
      <button class="btn" id="vw" title="Cards / list">List</button>
      <select id="srt" class="btn" title="Sort">
        <option value="name">A–Z</option>
        <option value="used">Most used</option>
        <option value="unused">Least used</option>
        <option value="recent">Recently used</option>
        <option value="cost">Biggest context cost</option>
      </select>
      <select id="cat" class="btn" title="Show one category"></select>
      <label class="dens" title="How much detail to show">
        <span>less</span><input type="range" id="den" min="1" max="4" step="1" value="3"><span>more</span>
      </label>
      <span id="lamp" title="Checking…"><span class="b"></span><span class="t">live</span></span>
    </div>
    <div class="tagbar" id="tagbar"></div>
  </header>

  <div id="phrase">
    <p>Type a sentence the way you'd actually say it. This scores it against every
       description that can currently fire — the same text Claude matches on.
       Approximate word overlap, not the real matcher, but it finds the blind spots.</p>
    <input id="pin" placeholder="e.g. the music is drowning out the voiceover">
    <div id="pout"></div>
  </div>

  <div id="offline"></div>
  <div id="bar"></div>
  <div class="scroll"><div id="list"></div></div>
</main>

<div id="modal-backdrop" hidden>
  <div id="modal-panel" role="dialog" aria-modal="true">
    <div id="modal-body"></div>
  </div>
</div>

<script>
const P = __PAYLOAD__;
const ITEMS = P.items;
const S = { tier:null, loc:null, tag:null, q:'', view:null, origin:null, cat:null,
            fav:false, sort:'name', mode:'mine',
            section:'skills', pgroup:null, pkind:null, psort:'name',
            /* prompts keep their own filter set, prefixed r-, for the same
               reason programs do: a "Location: This PC" filter carried into
               another inventory silently matches nothing and reads as a
               broken switch. */
            rcat:null, rtool:null, rtag:null, rorigin:null, rsort:'name', rfav:false,
            /* tools keep t-, hooks keep h-. Same reason again: five
               inventories sharing one filter set means every switch between
               them silently matches nothing. */
            tstart:null, tflag:null, tsort:'name', tfav:false, tup:false,
            hevent:null, hview:null, hsort:'name', hfav:false,
            /* groups: which one (if any) is open. No hfav-style favourite of
               its own - a group IS a curated shortlist, so favouriting one
               would be a shortlist of shortlists nobody asked for. */
            groupOpen:null, gsort:'name' };

/* The sort options belong to the section, not to the page. "Most used" over a
   list of programs is a control that cannot do anything: usage comes from
   transcript invocations, which only skills have. Leaving it there was the
   same fault as the category dropdown reading "ALL CATEGORIES (44)" above 90
   programs - a control describing the other inventory. */
const SORTS = {
  skills: [['name','A–Z'], ['used','Most used'], ['unused','Least used'],
           ['recent','Recently used'], ['cost','Biggest context cost']],
  programs: [['name','A–Z'], ['used','Most used'], ['unused','Never run here'],
             ['recent','Recently run'], ['group','By tool'],
             ['lines','Largest first'], ['edited','Recently changed'],
             ['untested','Untested first']],
  // "Most copied", never "Most used". A prompt's use happens in another app,
  // off this machine and invisible to Kit; the copy is the only part Kit
  // actually witnesses, so the label names that and not a bigger claim.
  prompts: [['name','A–Z'], ['used','Most copied'], ['recent','Recently copied'],
            ['new','Recently added'], ['cat','By category'],
            ['long','Longest first'], ['short','Shortest first']],
  // No "Most used" for tools. Kit sees a tool run only when Claude Code runs
  // it; a double-clicked desktop icon leaves no trace here at all, and many
  // tools are launched exactly that way. A usage column would
  // rank the ones you use most as the ones you never touch.
  tools: [['name','A–Z'], ['start','How you start it'], ['recent','Recently changed'],
          ['stale','Oldest first'], ['flags','Most problems first'],
          ['undoc','Undescribed first']],
  hooks: [['name','A–Z'], ['event','By event'], ['lines','Largest first'],
          ['untested','Untested first'], ['problem','Problems first']],
  // One option, not a dropdown pretending to offer more: groupList() always
  // sorts by name, so anything else here would be a control that changes
  // nothing when picked — the same fault programs' sort menu was written up
  // for avoiding elsewhere on this page.
  groups: [['name','A–Z']]
};
let _sortFilled = null;   // rebuilding the <select> every draw would steal focus
const sortKey = () => S.section==='programs' ? 'psort'
                    : S.section==='prompts'  ? 'rsort'
                    : S.section==='tools'    ? 'tsort'
                    : S.section==='hooks'    ? 'hsort'
                    : S.section==='groups'   ? 'gsort' : 'sort';
function fillSort(){
  if (_sortFilled === S.section) return;
  _sortFilled = S.section;
  const sel = document.getElementById('srt');
  const k = sortKey(), cur = S[k];
  sel.innerHTML = SORTS[S.section]
    .map(([v,l])=>'<option value="'+v+'">'+l+'</option>').join('');
  sel.value = SORTS[S.section].some(([v])=>v===cur) ? cur : 'name';
  S[k] = sel.value;
}

function sortProgs(a){
  const byName = (x,y) => x.name.localeCompare(y.name);
  const s = S.psort;
  if (s==='used')    a.sort((x,y)=> (y.runs-x.runs) || byName(x,y));
  // Runnable first. A file with no entry point CANNOT have been run, so
  // ranking it alongside a program that could have been and wasn't answers a
  // question nobody asked - and buries the ones that do mean something.
  else if (s==='unused') a.sort((x,y)=>{
    // Hooks rank last for the same reason they show no count: their zero is
    // structural, not evidence. Ranking them as "never run" would put the
    // busiest code on the machine at the top of a list of dead scripts.
    const rank = p => (p.home==='hook' && !p.is_test) ? 2 : (p.runnable ? 0 : 1);
    return (rank(x)-rank(y)) || (x.runs-y.runs) || (y.lines-x.lines) || byName(x,y);
  });
  else if (s==='recent') a.sort((x,y)=>{
    // Never-run sinks to the bottom rather than floating up as if it were fresh.
    const ax = x.ago==null ? Infinity : x.ago, ay = y.ago==null ? Infinity : y.ago;
    return (ax-ay) || byName(x,y);
  });
  else if (s==='group')   a.sort((x,y)=> x.group.localeCompare(y.group) || byName(x,y));
  else if (s==='lines')  a.sort((x,y)=> (y.lines-x.lines) || byName(x,y));
  else if (s==='edited') a.sort((x,y)=> (y.edited-x.edited) || byName(x,y));
  // Untested-first ranks the ones that matter: runnable, no test beside it.
  // Tests themselves sort last - a test file having no test of its own is not
  // a gap, and listing them among the findings would be noise.
  else if (s==='untested') a.sort((x,y)=>{
    const rank = p => p.is_test ? 2 : (p.tested ? 1 : (p.runnable ? 0 : 1.5));
    return (rank(x)-rank(y)) || (y.lines-x.lines) || byName(x,y);
  });
  else a.sort(byName);
}

/* ---------- programs: a second inventory, not more skills ----------
   Everything below the section switch is separate on purpose. A program has
   no reach, no origin and no location; a skill has no entry point, no flags
   and no test file. Forcing one shape over both would mean inventing values
   for fields that do not apply, and an invented value renders identically to
   a measured one. */
const PROGS = P.programs || [];
const PKINDS = {
  runnable: ['Runnable',  'Has an `if __name__ == "__main__"` block — you can run it.'],
  library : ['Imported',  'No entry point. Something else imports it; running it does nothing.'],
  test    : ['Tests',     'A test_ file. Its subject is named on the card.'],
  nodoc   : ['No summary','No module docstring — the only ones needing a human to describe them.']
};
const pkindOf = p => p.is_test ? 'test' : (p.runnable ? 'runnable' : 'library');

/* ---------- prompts: one .md file each in prompts/ ----------
   Read fresh from prompts_dir on every page build, like skills and
   programs. Optional Notion rows come from a cache (prompts.json) written by
   `python prompts.py --export`; only those can be STALE, so the header says
   when they were pulled instead of pretending they are live.

   Category here is a LOOKUP, not a guess. A skill's category is inferred from
   its description and is right about 69% of the time, which is why those
   cards carry a "?". These come from the tags you set on each prompt, so
   there is nothing to hedge - a prompt that lands in Unsorted has no matching
   tag, which is a true statement about the file rather than a failed guess. */
const PROMPTS = P.prompts || [];
const PRMETA  = P.prmeta || {};

/* ---------- tools & hooks: the fourth and fifth inventories ----------
   Tools are the only inventory whose membership is a DECISION. The roster in
   tools.json is ticked by hand because no rule reproduces it — see the
   tools.py docstring. Everything ON a tool card is measured;
   only which tools appear is chosen.

   Hooks invert every other tab: settings.json is the truth and the disk is
   the reconcile. That is the whole reason they are not just a filter over
   Progs, which already lists all 19 hook FILES and cannot say which of them
   the harness will ever run. */
const TOOLS  = P.tools  || [];
const TMETA  = P.tmeta  || {};
const HOOKS  = P.hooks  || [];
const HMETA  = P.hmeta  || {};

const STARTS = {
  icon     : ['Desktop icon', 'You have a shortcut for it. Double-click and go.'],
  scheduled: ['Scheduled',    'Windows runs it on a timer, whether or not you are here.'],
  command  : ['Command line', 'There is something runnable at the top level, but nothing launches it for you.'],
  none     : ['Nothing to run','No script, no icon, no task. It is a folder you open, not a thing you start.']
};

/* ---------- mode: what you HAVE vs what you could install ----------
   A catalogue entry is a marketplace listing for a plugin that is not
   installed. It can never fire, and there is no per-skill way to remove one -
   they arrive and leave as a whole marketplace. So they are hidden by default
   rather than deleted, and the default view is the inventory you can act on.

   POOL() is the ONLY place the mode is applied. Every count, filter, check and
   search reads from it. Counting over ITEMS while rendering a subset is the
   exact bug that made this page claim 174 skills while showing 33 - and the
   same shape as the catalogue-dedup bug in scan.py. One scoping point, or it
   comes back.

   Favourites deliberately ignore the mode: a star is a bookmark, not a state,
   and a shortlist that silently drops half its entries is worse than useless. */
const isCat = i => i.location === 'Plugins - catalogue';
const POOL  = () => S.fav ? ITEMS : ITEMS.filter(i => S.mode === 'cat' ? isCat(i) : !isCat(i));

const TIERS = {
  auto  : ['Auto',   'Description in context — fires on what you say. ~187 tok each.'],
  manual: ['Manual', 'Description NOT in context. Only /name reaches it. Costs nothing.'],
  off   : ['Off',    'Not loaded at all. Paste its path to use it. Costs nothing.']
};

const esc = s => String(s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* ---------- tags: server-persisted via /api/tag, localStorage is a mirror ----------
   tags.json (seeded into ITEMS by build.py) is the source of truth once the
   server is up - see setTags(). localStorage here is only a same-browser
   cache for the file:// case, where nothing can write to disk at all; it
   must never take the page down if it throws (opaque origin, some browsers
   under file://), so every call is wrapped. */
const THK='skills-launcher-theme';
function thGet(){ try { return localStorage.getItem(THK); } catch(e) { return null; } }
function thSet(v){ try { localStorage.setItem(THK, v); } catch(e) {} }
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  const b = document.getElementById('th');
  if (b) b.textContent = (t === 'light' ? 'Dark' : 'Light');
}
applyTheme(thGet() || 'dark');   // dark stays the default

const TK='skills-launcher-tags';
function lsGet(){ try { return localStorage.getItem(TK); } catch(e) { return null; } }
function lsSet(v){ try { localStorage.setItem(TK, v); return true; } catch(e) { return false; } }

let TAGS;
try { TAGS = JSON.parse(lsGet() || 'null'); } catch(e) { TAGS = null; }
if (!TAGS) TAGS = Object.fromEntries(ITEMS.filter(i=>(i.tags||[]).length).map(i=>[i.name,i.tags]));

const saveTags = () => { lsSet(JSON.stringify(TAGS)); };
const tagsFor = n => TAGS[n] || [];
const allTags = () => [...new Set(Object.values(TAGS).flat())].sort();

/* Same idea, three more times - one per inventory, same reason the
   favourites are four separate files: each /api/*tag validates names
   against the inventory it owns, so one shared map would mean accepting
   names none of the others could check. Prompts use `ktags` rather than
   `tags` because every prompt already has a `tags` field from its file (the
   category chips) - reusing the name here would collide with it. */
const PTK='skills-launcher-ptags', TTK='skills-launcher-ttags', HTK='skills-launcher-htags';
function lsGetK(k){ try { return localStorage.getItem(k); } catch(e) { return null; } }
function lsSetK(k,v){ try { localStorage.setItem(k, v); return true; } catch(e) { return false; } }

let PTAGS; try { PTAGS = JSON.parse(lsGetK(PTK) || 'null'); } catch(e) { PTAGS = null; }
if (!PTAGS) PTAGS = Object.fromEntries(PROMPTS.filter(p=>(p.ktags||[]).length).map(p=>[p.name,p.ktags]));
let TTAGS; try { TTAGS = JSON.parse(lsGetK(TTK) || 'null'); } catch(e) { TTAGS = null; }
if (!TTAGS) TTAGS = Object.fromEntries(TOOLS.filter(t=>(t.tags||[]).length).map(t=>[t.name,t.tags]));
let HTAGS; try { HTAGS = JSON.parse(lsGetK(HTK) || 'null'); } catch(e) { HTAGS = null; }
if (!HTAGS) HTAGS = Object.fromEntries(HOOKS.filter(h=>(h.tags||[]).length).map(h=>[h.name,h.tags]));

const savePTags = () => { lsSetK(PTK, JSON.stringify(PTAGS)); };
const saveTTags = () => { lsSetK(TTK, JSON.stringify(TTAGS)); };
const saveHTags = () => { lsSetK(HTK, JSON.stringify(HTAGS)); };
const ptagsFor = n => PTAGS[n] || [];
const ttagsFor = n => TTAGS[n] || [];
const htagsFor = n => HTAGS[n] || [];

/* ---------- groups: cross-inventory shortlists, server-only ----------
   No localStorage mirror, unlike tags/favourites above: a group's whole
   point is a NAME that outlives one browser tab, and two tabs each keeping
   their own idea of what "Trial by Listener" contains is worse than the
   feature not working offline. If the server is down you can still SEE
   groups (they came baked into the page like everything else); creating,
   renaming, deleting or adding to one all refuse with the same message
   every other write on this page already gives when P.live is false. */
let GROUPS = P.groups || {};
const groupList = () => Object.entries(GROUPS)
  .map(([id,g])=>({id, name:g.name, members:g.members||[]}))
  .sort((a,b)=>a.name.localeCompare(b.name));
const groupsFor = (kind, name) => groupList().filter(g =>
  g.members.some(m=>m.kind===kind && m.name===name));

async function createGroup(name){
  if (!P.live){ note('Groups need the server — start it with python serve.py, not by '+
                     'opening the .html file.', true); return null; }
  try{
    const r = await fetch('/api/group-create', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    GROUPS[j.id] = {name: j.name, members: j.members};
    return j.id;
  }catch(e){ note('Could not create the group: '+e.message, true); return null; }
}
async function renameGroup(id, name){
  if (!P.live){ note('Groups need the server — start it with python serve.py.', true); return; }
  try{
    const r = await fetch('/api/group-rename', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({id, name})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    if (GROUPS[id]) GROUPS[id].name = j.name;
    draw();
  }catch(e){ note('Could not rename the group: '+e.message, true); }
}
async function deleteGroup(id){
  if (!P.live){ note('Groups need the server — start it with python serve.py.', true); return; }
  try{
    const r = await fetch('/api/group-delete', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    delete GROUPS[id];
    if (S.groupOpen === id) S.groupOpen = null;
    closeModal();
    draw();
  }catch(e){ note('Could not delete the group: '+e.message, true); }
}
async function toggleGroupMember(id, kind, name, on, after){
  if (!P.live){ note('Groups need the server — start it with python serve.py.', true); return; }
  try{
    const r = await fetch('/api/group-member', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id, kind, name, on})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    if (GROUPS[id]) GROUPS[id].members = j.members;
    if (after) after();
    draw();
  }catch(e){ note('Could not update the group: '+e.message, true); }
}

/* ---------- the group picker modal ----------
   One popup, opened from the "+ Group" button on any Skill/Prompt/Tool
   card. Ticking a checkbox toggles membership immediately (same pattern as
   the ☆ star, just inside a modal instead of on the card) - there is no
   separate Save, so closing the modal can never discard anything. */
let MODAL_ITEM = null;   // {kind, name} while the picker is open, else null

function openModal(html){
  document.getElementById('modal-body').innerHTML = html;
  document.getElementById('modal-backdrop').hidden = false;
}
function closeModal(){
  document.getElementById('modal-backdrop').hidden = true;
  document.getElementById('modal-body').innerHTML = '';
  MODAL_ITEM = null;
}

function groupPickerHTML(kind, name){
  const mine = new Set(groupsFor(kind, name).map(g=>g.id));
  const rows = groupList().map(g =>
    '<label class="mrow"><input type="checkbox" data-gm="'+g.id+'"'+
    (mine.has(g.id)?' checked':'')+'>'+
    '<span class="gn">'+esc(g.name)+'</span>'+
    '<span class="gc">'+g.members.length+'</span></label>').join('');
  return '<h3>Add to a group</h3>'+
    '<p class="sub">'+esc(name)+'</p>'+
    (rows || '<p class="mempty">No groups yet — create the first one below.</p>')+
    '<div class="mnew">'+
      '<input id="mnew-name" placeholder="New group name…" autocomplete="off">'+
      '<button class="btn" id="mnew-add">Create &amp; add</button>'+
    '</div>'+
    '<button class="btn mclose" id="mclose">Done</button>';
}
function wireGroupPicker(){
  const {kind, name} = MODAL_ITEM;
  document.querySelectorAll('[data-gm]').forEach(cb=>cb.onchange=()=>{
    toggleGroupMember(cb.dataset.gm, kind, name, cb.checked, () => {
      if (MODAL_ITEM) { openModal(groupPickerHTML(kind, name)); wireGroupPicker(); }
    });
  });
  const add = document.getElementById('mnew-add');
  const input = document.getElementById('mnew-name');
  add.onclick = async () => {
    const v = input.value.trim();
    if (!v) { input.focus(); return; }
    add.disabled = true;
    const id = await createGroup(v);
    add.disabled = false;
    if (!id) return;
    toggleGroupMember(id, kind, name, true, () => {
      if (MODAL_ITEM) { openModal(groupPickerHTML(kind, name)); wireGroupPicker(); }
    });
  };
  input.onkeydown = e => { if (e.key==='Enter'){ e.preventDefault(); add.onclick(); } };
  document.getElementById('mclose').onclick = closeModal;
}
function openGroupPicker(kind, name){
  MODAL_ITEM = {kind, name};
  openModal(groupPickerHTML(kind, name));
  wireGroupPicker();
}

/* A group chip on any card jumps straight to that group's own view - the
   same "show me everything, not just this tab's slice of it" move the
   tag search's "N more match in Mine/Catalogue" link already makes. */
function goToGroup(id){
  S.section = 'groups'; S.groupOpen = id;
  S.tier=S.loc=S.tag=S.origin=S.cat=S.pgroup=S.pkind=null;
  S.rcat=S.rtool=S.rtag=S.rorigin=null; S.rfav=false;
  S.tstart=S.tflag=null; S.tfav=false; S.tup=false;
  S.hevent=S.hview=null; S.hfav=false;
  S.fav=false; S.view=null; S.q='';
  const q = document.getElementById('q'); if (q) q.value='';
  _sortFilled = null;
  draw();
}
function wireGroupChips(){
  document.querySelectorAll('[data-groupchip]').forEach(e=>e.onclick=ev=>{
    ev.stopPropagation(); goToGroup(e.dataset.groupchip);
  });
}

/* ---------- what to copy, per tier ---------- */
function invocation(i){
  if (i.reach === 'off')     return 'Activate this skill: ' + i.file;
  if (i.reach === 'manual')  return '/' + i.name + ' ';
  return 'Use the ' + i.name + ' skill for this: ';
}
function copy(text, btn){
  const done = () => { const t=btn.textContent; btn.textContent='Copied';
    btn.classList.add('done'); setTimeout(()=>{btn.textContent=t;btn.classList.remove('done')},1100); };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, ()=>fb(text,done));
  else fb(text, done);
}
function fb(text, done){
  const ta=document.createElement('textarea'); ta.value=text; ta.style.position='fixed';
  ta.style.opacity=0; document.body.appendChild(ta); ta.select();
  try{document.execCommand('copy'); done();}catch(e){} ta.remove();
}

/* ---------- sidebar ---------- */
/* The section switch sits above everything, including the mode. It decides
   which inventory you are looking at, so it also decides what every count
   below it is counting. Rendered once here, wired once. */
/* Relabel the three sidebar group headings for the section being shown, and
   hide any that section leaves empty. A heading over nothing ("Checks" with
   no checks under it) reads as something failing to load. */
function secHeads(a, b, c, d){
  [['sec-a', a, 'tiers'], ['sec-b', b, 'origins'], ['sec-c', c, 'locs']]
    .forEach(([id, label, listId]) => {
      const h = document.getElementById(id), l = document.getElementById(listId);
      if (!h) return;
      const empty = !label || !l || !l.innerHTML.trim();
      h.textContent = label || '';
      h.style.display = empty ? 'none' : '';
    });
  const hd = document.getElementById('sec-d');
  if (hd){ hd.textContent = d || ''; hd.style.display = d ? '' : 'none'; }
}

function sectionbar(){
  // Five tabs, all built as of 2026-08-24. Tools sat greyed with a dash for
  // two days rather than being hidden, so its arrival was a tab filling in
  // rather than the navigation rearranging itself.
  const secs = [
    ['skills',  'Skills',  ITEMS.length, null],
    ['prompts', 'Prompts', PROMPTS.length, null],
    ['tools',   'Tools',   TOOLS.length, null],
    ['hooks',   'Hooks',   HOOKS.length, null],
    ['programs','Progs',   PROGS.length, null],
    ['groups',  'Groups',  groupList().length, null]
  ];
  document.getElementById('sectionnav').innerHTML =
    '<div class="tabs">'+ secs.map(([k,lbl,n,soon]) =>
      '<div class="tab'+(S.section===k?' on':'')+(soon?' soon':'')+'"'+
      (soon ? ' title="'+esc(soon)+'"' : ' data-sec="'+k+'"')+'>'+
      lbl+'<span class="c">'+(n===null?'–':n)+'</span></div>').join('') +'</div>';
  document.querySelectorAll('[data-sec]').forEach(e=>e.onclick=()=>{
    if (S.section === e.dataset.sec) return;
    // Filters belong to a section's facets. Carrying a "Location: This PC"
    // filter into Programs would silently show nothing and read as a broken
    // switch - the same reason the mode switch clears them.
    S.section = e.dataset.sec;
    S.tier=S.loc=S.tag=S.origin=S.cat=S.pgroup=S.pkind=null;
    S.rcat=S.rtool=S.rtag=S.rorigin=null; S.rfav=false;
    S.tstart=S.tflag=null; S.tfav=false; S.tup=false;
    S.hevent=S.hview=null; S.hfav=false;
    S.fav=false; S.view=null; S.groupOpen=null;
    _sortFilled = null;          // the dropdown's options belong to the section
    draw();
  });
}

function programSidebar(){
  const kc={}, gc={};
  PROGS.forEach(p=>{
    kc[pkindOf(p)] = (kc[pkindOf(p)]||0)+1;
    if (!p.summary && !p.is_test) kc.nodoc = (kc.nodoc||0)+1;
    gc[p.group] = (gc[p.group]||0)+1;
  });

  document.getElementById('modenav').innerHTML = '';
  document.getElementById('favnav').innerHTML  = '';
  document.getElementById('tiers').innerHTML =
    '<div class="nav'+(!S.pkind&&!S.pgroup&&!S.q?' on':'')+'" data-pk=""><span class="dot" style="background:var(--edge2)"></span>All<span class="c">'+PROGS.length+'</span></div>'+
    Object.keys(PKINDS).filter(k=>kc[k]).map(k =>
      '<div class="nav'+(S.pkind===k?' on':'')+'" data-pk="'+k+'" title="'+esc(PKINDS[k][1])+'">'+
      PKINDS[k][0]+'<span class="c">'+kc[k]+'</span></div>').join('');

  // "Has a test" is the most useful single fact about a script here, and its
  // absence is the more interesting half.
  const tested = PROGS.filter(p=>p.tested).length;
  const untested = PROGS.filter(p=>!p.tested && !p.is_test && p.runnable).length;
  document.getElementById('origins').innerHTML =
    '<div class="nav'+(S.pkind==='tested'?' on':'')+'" data-pk="tested" title="A test_ file sits beside it.">Has a test<span class="c">'+tested+'</span></div>'+
    '<div class="nav'+(S.pkind==='untested'?' on':'')+'" data-pk="untested" title="Runnable, no test_ file beside it.">Runnable, untested<span class="c">'+untested+'</span></div>';

  document.getElementById('locs').innerHTML =
    Object.keys(gc).sort((a,b)=>gc[b]-gc[a]).map(g =>
      '<div class="nav'+(S.pgroup===g?' on':'')+'" data-pg="'+esc(g)+'">'+esc(g)+
      '<span class="c">'+gc[g]+'</span></div>').join('');

  document.querySelectorAll('[data-pk]').forEach(e=>e.onclick=()=>{
    if(!e.dataset.pk){ S.pkind=S.pgroup=null; S.q=''; document.getElementById('q').value=''; }
    else S.pkind = S.pkind===e.dataset.pk ? null : e.dataset.pk;
    draw();
  });
  document.querySelectorAll('[data-pg]').forEach(e=>e.onclick=()=>{
    S.pgroup = S.pgroup===e.dataset.pg ? null : e.dataset.pg; draw();
  });
}

function programCard(p){
  const kind = pkindOf(p);
  const badge = p.runnable
    ? '<span class="pb run">RUN</span>'
    : (p.is_test ? '<span class="pb test">TEST</span>' : '<span class="pb lib">IMPORT</span>');
  const flags = (p.flags||[]).length
    ? '<div class="pflags">'+p.flags.map(f=>'<code>'+esc(f)+'</code>').join(' ')+'</div>' : '';
  const sub = p.is_test && p.tests ? ' <span class="pmeta">tests '+esc(p.tests)+'</span>' : '';
  const warn = p.broken ? '<div class="flag">⚠ will not parse</div>' : '';
  // The rest of the docstring, when there is one. Most of these explain WHY
  // the program exists, which is the half a one-line summary cannot carry and
  // the half worth reading before deciding whether to run it.
  const rest = (p.doc || '').split('\n').slice(1).join('\n').trim();
  const more = rest ? '<div class="pdoc">'+esc(rest)+'</div>' : '';

  // Same classes as the skill card on purpose: it inherits the grid, the list
  // view and the density slider for free. A second card shape would need all
  // three re-solved, and would drift from the first one within a week.
  return '<div class="card p-card" data-n="'+esc(p.name)+'">'+
    '<div class="top"><span class="dot d-'+(kind==='runnable'?'auto':kind==='test'?'manual':'off')+'"></span>'+
      '<span class="nm" title="'+esc(p.name)+'">'+esc(p.name)+'</span>'+ sub + badge +'</div>'+
    (p.summary
      ? '<div class="ds">'+esc(p.summary)+'</div>'
      : '<div class="ds" style="color:var(--amber)">No module docstring — nothing in the file says what it is for.</div>')+
    more +
    '<div class="pa">'+esc(p.file)+'</div>'+
    warn + flags +
    '<div class="meta">'+
      '<span class="chip">'+esc(p.group)+'</span>'+
      '<span class="chip tk">'+p.lines+' lines</span>'+
      (p.is_test
        ? ''
        : (p.tested ? '<span class="chip ok">has a test</span>'
                    : (p.runnable ? '<span class="chip warn">no test</span>' : '')))+
      // "here" is doing real work in this label. This counts Bash commands in
      // the transcripts — a program run from a terminal, a .bat or the Windows
      // scheduler leaves no trace, so 0 means "never run THROUGH CLAUDE", not
      // "never run". Dropping that word would turn a scope limit into a claim.
      // A hook is invoked by the harness on an event, never by a Bash command,
      // so its run count is structurally zero however hard it works.
      // heredoc_source_guard blocked two writes during the session that built
      // this and still read "never run here". Showing 0 there is not a missing
      // measurement, it is the wrong measurement — so it is not shown.
      // ...but a test_ file that happens to LIVE in hooks/ is not a hook. It
      // is run by a command like any other test, and its count is real.
      (p.home === 'hook' && !p.is_test
        ? '<span class="chip" style="color:var(--t-src)" title="Hooks are fired by '+
          'Claude Code on an event, not by a command, so a run count cannot see '+
          'them. Registration lives in settings.json.">runs on events</span>'
        : (p.runnable
          ? '<span class="chip" style="color:'+(p.runs?'var(--live)':'var(--off)')+'" '+
            'title="Runs found in Bash commands across your transcripts. Programs '+
            'launched from a terminal, a .bat or Task Scheduler are invisible here'+
            (p.name_shared ? ' — and another program shares this filename, so this count covers both.' : '.')+'">'+
            (p.runs ? p.runs+'x · '+p.ago+'d ago' : 'never run here')+
            (p.name_shared ? ' ?' : '')+'</span>'
          : ''))+
      // "named in", never "used by": a skill may name a program only to
      // record that it was superseded. Naming is what the evidence
      // shows; whether the mention endorses the program is not.
      (p.skills||[]).map(s=>'<span class="chip sk" data-goskill="'+esc(s)+'" '+
        'title="This skill’s SKILL.md names this file. Click to open it in Skills.">'+
        'named in '+esc(s)+'</span>').join('')+
    '</div>'+
    '<div class="acts">'+
      (p.runnable ? '<button class="btn" data-prun="'+esc(p.name)+'">Copy run</button>' : '')+
      '<button class="btn" data-ppath="'+esc(p.name)+'">Copy path</button>'+
    '</div></div>';
}

function drawPrograms(){
  sectionbar(); fillSort(); programSidebar();
  secHeads('Kind', 'By tool', null, null);
  document.getElementById('tagbar').innerHTML = '';
  // The category dropdown is a skills facet. Left visible it reads "ALL
  // CATEGORIES (44)" over a list of 90 programs - a control that describes
  // the other section.
  document.getElementById('cat').style.display = 'none';
  ['nav-never','nav-sleep','nav-flags','nav-rec'].forEach(id=>{
    const e=document.getElementById(id); if(e) e.style.display='none';
  });

  const f = S.q.trim().toLowerCase();
  const hits = PROGS.filter(p =>
    (!S.pgroup || p.group===S.pgroup) &&
    (!S.pkind ||
       (S.pkind==='tested'   ? p.tested :
        S.pkind==='untested' ? (!p.tested && !p.is_test && p.runnable) :
        S.pkind==='nodoc'    ? (!p.summary && !p.is_test) :
        pkindOf(p)===S.pkind)) &&
    (!f || (p.name+' '+p.summary+' '+p.group+' '+(p.doc||'')).toLowerCase().includes(f))
  );

  const on = [S.pkind && (PKINDS[S.pkind] ? PKINDS[S.pkind][0] : S.pkind),
              S.pgroup, S.q && ('"'+S.q+'"')].filter(Boolean);
  document.getElementById('bar').innerHTML = on.length
    ? 'Showing <b>'+hits.length+'</b> of '+PROGS.length+' programs — filtered by '+
      on.map(x=>'<span class="fchip">'+esc(x)+'</span>').join('')+
      '<button class="btn" id="clr">Clear</button>'
    : '<span style="color:var(--dim)">All '+PROGS.length+
      ' runnable-or-importable Python files, one level inside each tool plus the hooks. '+
      'Package internals are deliberately not listed.'+
      ((P.pmeta||{}).tools_configured === false
        ? ' <span style="color:var(--amber)">Tools are not configured — set '+
          '<code>tools_root</code> in <code>kit.config.json</code> to list the '+
          'programs inside them. Showing hooks only.</span>' : '')+
      '</span>';
  const c = document.getElementById('clr');
  if (c) c.onclick = () => { S.pkind=S.pgroup=null; S.q='';
    document.getElementById('q').value=''; draw(); };

  sortProgs(hits);
  document.getElementById('list').innerHTML = hits.length
    ? '<div class="grid">'+hits.map(programCard).join('')+'</div>'
    : '<p class="empty">Nothing matches.</p>';

  const find = n => PROGS.find(x=>x.name===n);
  document.querySelectorAll('[data-prun]').forEach(b=>b.onclick=()=>
    copy('python "'+find(b.dataset.prun).file+'"', b));
  document.querySelectorAll('[data-ppath]').forEach(b=>b.onclick=()=>
    copy(find(b.dataset.ppath).file, b));
  document.querySelectorAll('.p-card .ds, .p-card .pdoc').forEach(d=>d.onclick=()=>
    d.parentElement.classList.toggle('open'));

  // A link you cannot follow is a label. Clicking jumps to that skill.
  document.querySelectorAll('[data-goskill]').forEach(e=>e.onclick=(ev)=>{
    ev.stopPropagation();
    S.section='skills'; S.mode='mine';
    S.tier=S.loc=S.tag=S.origin=S.cat=S.pgroup=S.pkind=null; S.fav=false; S.view=null;
    S.q = e.dataset.goskill;
    document.getElementById('q').value = S.q;
    draw();
  });
}

/* ---------- prompts ---------- */

function sortPrompts(a){
  const byName = (x,y) => x.name.localeCompare(y.name);
  const s = S.rsort;
  if (s==='used')       a.sort((x,y)=> (y.used-x.used) || byName(x,y));
  else if (s==='recent') a.sort((x,y)=>{
    // Never-copied sinks rather than floating up as if it were fresh - the
    // same trap the programs "Recently run" sort had to dodge.
    const ax = x.ago==null ? Infinity : x.ago, ay = y.ago==null ? Infinity : y.ago;
    return (ax-ay) || byName(x,y);
  });
  else if (s==='new')   a.sort((x,y)=> String(y.created).localeCompare(String(x.created)) || byName(x,y));
  else if (s==='cat')   a.sort((x,y)=> x.cat.localeCompare(y.cat) || byName(x,y));
  else if (s==='long')  a.sort((x,y)=> (y.words-x.words) || byName(x,y));
  else if (s==='short') a.sort((x,y)=> (x.words-y.words) || byName(x,y));
  else a.sort(byName);
}

function promptSidebar(){
  const cc={}, tc={}, oc={};
  PROMPTS.forEach(p=>{
    cc[p.cat] = (cc[p.cat]||0)+1;
    if (p.tool) tc[p.tool] = (tc[p.tool]||0)+1;
    if (p.origin) { const o = shortOrigin(p.origin); oc[o] = (oc[o]||0)+1; }
  });

  document.getElementById('modenav').innerHTML = '';

  const nf = PROMPTS.filter(p=>p.fav).length;
  document.getElementById('favnav').innerHTML =
    '<div class="nav'+(S.rfav?' on':'')+'" id="nav-rfav" title="Prompts you starred">'+
    '<span style="color:var(--gold);font-size:13px;line-height:1">★</span>Favourites'+
    '<span class="c">'+nf+'</span></div>';
  document.getElementById('nav-rfav').onclick = () => { S.rfav = !S.rfav; draw(); };

  const clean = !S.rcat&&!S.rtool&&!S.rtag&&!S.rorigin&&!S.rfav&&!S.q;
  document.getElementById('tiers').innerHTML =
    '<div class="nav'+(clean?' on':'')+'" data-rc=""><span class="dot" style="background:var(--edge2)"></span>All<span class="c">'+PROMPTS.length+'</span></div>'+
    (P.pcats||[]).filter(c=>cc[c]).map(c =>
      '<div class="nav'+(S.rcat===c?' on':'')+'" data-rc="'+esc(c)+'">'+esc(c)+
      '<span class="c">'+cc[c]+'</span></div>').join('');

  // The platform, kept apart from the category because it answers a different
  // question: what a prompt MAKES versus where you paste it. A prompt
  // written for one platform rarely transfers verbatim to another.
  document.getElementById('origins').innerHTML =
    Object.keys(tc).sort((a,b)=>tc[b]-tc[a]).map(t =>
      '<div class="nav'+(S.rtool===t?' on':'')+'" data-rt="'+esc(t)+'" '+
      'title="Written against '+esc(t)+' — the @ reference syntax and shot '+
      'grammar are that platform\'s.">'+esc(t)+'<span class="c">'+tc[t]+'</span></div>').join('');

  document.getElementById('locs').innerHTML =
    Object.keys(oc).sort((a,b)=>oc[b]-oc[a]).map(o =>
      '<div class="nav'+(S.rorigin===o?' on':'')+'" data-ro="'+esc(o)+'" '+
      'title="Where this prompt came from.">'+esc(o)+'<span class="c">'+oc[o]+'</span></div>').join('');

  document.querySelectorAll('[data-rc]').forEach(e=>e.onclick=()=>{
    if(!e.dataset.rc){ S.rcat=S.rtool=S.rtag=S.rorigin=null; S.rfav=false; S.q='';
                       document.getElementById('q').value=''; }
    else S.rcat = S.rcat===e.dataset.rc ? null : e.dataset.rc;
    draw();
  });
  document.querySelectorAll('[data-rt]').forEach(e=>e.onclick=()=>{
    S.rtool = S.rtool===e.dataset.rt ? null : e.dataset.rt; draw(); });
  document.querySelectorAll('[data-ro]').forEach(e=>e.onclick=()=>{
    S.rorigin = S.rorigin===e.dataset.ro ? null : e.dataset.ro; draw(); });
}

/* Origin strings are often long provenance notes ("Jane Doe (Studio Name,
   IG: handle)"). The sidebar needs a label, so cut at the first bracket or
   dash - the full string still shows on the card. */
function shortOrigin(o){
  let s = String(o).split(/[(—–]|\s-\s/)[0].trim().replace(/[,;]\s*$/,'');
  // Some origins are a whole sentence ("Built during the launch video
  // project"), with no bracket or dash to cut at. A sidebar row that
  // wraps to three lines is not a facet, so cap it - the card still carries
  // the full string, and the tooltip shows it.
  if (s.length > 26) s = s.slice(0, 25).replace(/[\s,]+\S*$/,'') + '…';
  return s || String(o);
}

function promptTags(){
  const t = [...new Set(PROMPTS.flatMap(p=>p.tags||[]))].sort();
  document.getElementById('tagbar').innerHTML = t.length
    ? t.map(x=>'<span class="tag'+(S.rtag===x?' on':'')+'" data-rtag="'+esc(x)+'">'+esc(x)+'</span>').join('')
    : '';
  document.querySelectorAll('[data-rtag]').forEach(e=>e.onclick=()=>{
    S.rtag = S.rtag===e.dataset.rtag ? null : e.dataset.rtag; draw(); });
}

function promptCard(p){
  const copies = p.used
    ? '<span class="chip" style="color:var(--live)" title="Times you copied this '+
      'out of Kit. A prompt is used in another app, off this machine, so the '+
      'copy is the only part Kit can honestly see.">'+p.used+'× · '+p.ago+'d ago</span>'
    : '<span class="chip" style="color:var(--dim)" title="Counting copies made '+
      'from Kit. Nothing is mined from your transcripts — a prompt leaves no '+
      'trace there, so this starts at zero rather than guessing.">not copied yet</span>';

  // Say where the text came from. The "whole page" reading is the loose one,
  // and a prompt that is really a page of many prompts is worth knowing about
  // BEFORE you paste it, not after.
  const from = {
    'section'   : 'from the “The Prompt” section'+(p.filename ? ' of prompts/'+p.filename : '')+
                  ' — setup and notes are left out of the copy',
    'labelled'  : 'from the page’s “Prompt:” block',
    'whole page': 'the whole page — no headings on it, so this is all of it',
    'unmarked'  : 'the whole page including its headings — no section is named '+
                  'as the prompt, so nothing here can tell which part to take',
    'file'      : 'from '+(p.filename ? 'prompts/'+p.filename : 'a local file'),
    'empty'     : 'nothing found'
  }[p.body_from] || p.body_from;
  const catTip = p.source === 'notion'
    ? 'From the Notion row’s Tags — a lookup, not a guess.'
    : 'From the tags in this prompt’s frontmatter — a lookup, not a guess.';

  return '<div class="card" data-rn="'+esc(p.name)+'">'+
    '<div class="top"><span class="dot d-'+(p.flags.length?'manual':'auto')+'"></span>'+
      '<span class="nm" title="'+esc(p.name)+'">'+esc(p.name)+'</span>'+ pstar(p) +'</div>'+
    (p.for ? '<div class="ds">'+esc(p.for)+'</div>'
           : '<div class="ds" style="color:var(--amber)">No “For” note — nothing says when to reach for this.</div>')+
    (p.body ? '<div class="pbody">'+esc(p.body)+'</div>'+
              '<div class="pfrom">'+from+'</div>' : '')+
    // The whole file - setup above the prompt and notes below it - for
    // reading, never for copying. Collapsed so the card stays a card.
    (p.notes ? '<details class="pnotes"><summary>Whole file: setup &amp; notes</summary>'+
               '<pre>'+esc(p.notes)+'</pre></details>' : '')+
    (p.flags||[]).map(f=>'<div class="flag">⚠ '+esc(f)+'</div>').join('')+
    '<div class="meta">'+
      '<span class="chip" title="'+catTip+'">'+esc(p.cat)+'</span>'+
      (p.tool ? '<span class="chip" style="color:var(--t-src)">'+esc(p.tool)+'</span>' : '')+
      '<span class="chip tk" title="'+p.tokens+' tokens if you paste it">'+p.words+' words</span>'+
      copies +
      (p.origin ? '<span class="chip tk" title="'+esc(p.origin)+'">'+esc(shortOrigin(p.origin))+'</span>' : '')+
      (p.tags||[]).map(x=>'<span class="chip tg">'+esc(x)+'</span>').join('')+
      (p.ktags||[]).map(x=>'<span class="chip tg" title="Your own Kit tag, not from the prompt file">'+esc(x)+'</span>').join('')+
      groupsFor('prompt', p.name).map(g=>'<span class="chip gp" data-groupchip="'+g.id+'" '+
        'title="In group \''+esc(g.name)+'\' — click to open">'+esc(g.name)+'</span>').join('')+
    '</div>'+
    '<div class="acts">'+
      (p.body ? '<button class="btn" data-rcopy="'+esc(p.name)+'">Copy prompt</button>' : '')+
      (p.url && p.source === 'notion'
        ? '<button class="btn" data-ropen="'+esc(p.name)+'">Open in Notion</button>' : '')+
      '<button class="btn" data-ptagbtn="'+esc(p.name)+'">+ Tag</button>'+
      '<button class="btn" data-groupbtn-prompt="'+esc(p.name)+'">+ Group</button>'+
      // The reference video this prompt produced. Two routes, because they
      // fail in different places: the local copy plays instantly and works
      // offline, the source link survives the file being deleted. A browser
      // cannot follow file:// from an http:// page, so the local one goes
      // through the server.
      (p.reffile
        ? '<button class="btn ref" data-rref="'+esc(p.name)+'" title="'+
          esc(p.reffile.split("/").pop())+'">▶ Reference</button>' : '')+
      (p.ref
        ? '<a class="btn" href="'+esc(p.ref)+'" target="_blank" rel="noopener" '+
          'title="The original post">Source'+(p.reffile ? '' : ' ↗')+'</a>' : '')+
    '</div></div>';
}

function pstar(p){
  return '<button class="star'+(p.fav?' on':'')+'" data-rfav="'+esc(p.name)+'"'+
    ' aria-pressed="'+(p.fav?'true':'false')+'"'+
    ' title="'+(p.fav?'In Favourites — click to remove':'Add to Favourites')+'">'+
    (p.fav?'★':'☆')+'</button>';
}

/* Re-pull the Prompt Library on demand.
   The export takes ~7s and the server is single-threaded, so this cannot be
   one blocking request - it would freeze every toggle and the live poll behind
   it. POST starts it, GET reports on it, and the page reloads when it lands
   because the HTML is built per request: a fresh cache is a fresh page with no
   restart. That is the whole reason this button can exist at all. */
async function pullPrompts(btn){
  if (!P.live){
    note('Refresh needs the server — start it with python serve.py, not by opening '+
         'the .html file.', true);
    return;
  }
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Pulling…';
  const fail = m => { btn.disabled = false; btn.textContent = label; note(m, true); };
  try {
    const r = await (await fetch('/api/ppull', {method:'POST'})).json();
    if (!r.ok) return fail('Could not start the pull.');
    // Poll rather than hold the connection open, for the same reason the live
    // feed polls: one long request owns the only thread.
    for (let i = 0; i < 60; i++){
      await new Promise(res => setTimeout(res, 1200));
      const s = await (await fetch('/api/ppull')).json();
      if (s.running) continue;
      if (s.error) return fail('Notion pull failed: ' + s.error);
      btn.textContent = 'Got ' + s.count + ' — reloading';
      location.reload();
      return;
    }
    fail('The pull is taking longer than 72s. It may still finish — reload to check.');
  } catch (e){
    fail('Could not reach the server: ' + e.message);
  }
}

function drawPrompts(){
  sectionbar(); fillSort(); promptSidebar(); promptTags();
  secHeads('Category', 'Platform', 'Origin', null);
  document.getElementById('cat').style.display = 'none';
  ['nav-never','nav-sleep','nav-flags','nav-rec'].forEach(id=>{
    const e=document.getElementById(id); if(e) e.style.display='none';
  });

  document.getElementById('offline').innerHTML = P.live ? '' :
    '<b>Read-only.</b> This page was opened as a file, so nothing can write: '+
    'every ★ is inert and copies are not counted. Close this tab and run '+
    '<code>python serve.py</code>, then use <code>http://127.0.0.1:'+(P.port||8712)+'/</code>.';

  if (!PROMPTS.length){
    document.getElementById('bar').innerHTML = '';
    document.getElementById('list').innerHTML =
      '<p class="empty">No prompts yet.<br><br>Drop a <code>.md</code> file into '+
      '<code>'+esc(PRMETA.dir || 'prompts/')+'</code> and reload — see '+
      '<code>prompts/README.md</code> for the format.</p>';
    return;
  }

  const f = S.q.trim().toLowerCase();
  const hits = PROMPTS.filter(p =>
    (!S.rfav || p.fav) &&
    (!S.rcat || p.cat===S.rcat) &&
    (!S.rtool || p.tool===S.rtool) &&
    (!S.rtag || (p.tags||[]).includes(S.rtag)) &&
    (!S.rorigin || shortOrigin(p.origin||'')===S.rorigin) &&
    // The BODY is searched, not just the title and the For note. Half of why
    // this tab exists is finding the prompt that mentioned "bar brawl" or
    // "@image_1" without opening every prompt to look.
    (!f || (p.name+' '+p.for+' '+p.origin+' '+(p.tags||[]).join(' ')+' '+
            (p.ktags||[]).join(' ')+' '+p.body+' '+(p.notes||''))
             .toLowerCase().includes(f))
  );

  const on = [S.rfav && '★ Favourites', S.rcat, S.rtool, S.rorigin,
              S.rtag && ('#'+S.rtag), S.q && ('"'+S.q+'"')].filter(Boolean);
  // Local files are read fresh on every build. Only the optional Notion rows
  // are a cache, so only they get an age - and it is said LOUDLY once it is
  // old enough to be wrong, because a quiet timestamp is silent drift.
  const nat = PRMETA.notion_at || 0;
  const ageD = nat ? (Date.now()/1000 - nat)/86400 : null;
  const when = nat
    ? new Date(nat*1000).toLocaleDateString(undefined,{day:'numeric',month:'short'})
    : null;
  const stale = ageD !== null && ageD > 7
    ? ' <span style="color:var(--amber)">Notion rows pulled '+Math.round(ageD)+' days ago — '+
      'anything added there since is not here.</span>' : '';
  const nNotion = PRMETA.notion || 0;
  const src = '<span style="color:var(--dim)">All '+PROMPTS.length+' prompts — '+
      (PRMETA.local||0)+' from <code>'+esc(PRMETA.dir||'prompts/')+'</code>'+
      (nNotion ? ', '+nNotion+' from Notion'+(when?' (pulled '+when+')':'') : '')+
      '. Add a .md file there and reload to add one.'+stale+'</span>';
  document.getElementById('bar').innerHTML = on.length
    ? 'Showing <b>'+hits.length+'</b> of '+PROMPTS.length+' prompts — filtered by '+
      on.map(x=>'<span class="fchip">'+esc(x)+'</span>').join('')+
      '<button class="btn" id="clr">Clear</button>'
    : src +
      // The button lives next to the AGE, because that is the sentence that
      // makes someone want it. Only shown when Notion is configured: the
      // local folder needs no refresh, a reload re-reads it.
      (PRMETA.notion_configured
        ? '<button class="btn" id="ppull" title="Re-pull the optional Notion '+
          'prompts now. Takes a few seconds.">Refresh from Notion</button>' : '');
  const c = document.getElementById('clr');
  if (c) c.onclick = () => { S.rcat=S.rtool=S.rtag=S.rorigin=null; S.rfav=false; S.q='';
    document.getElementById('q').value=''; draw(); };
  const pb = document.getElementById('ppull');
  if (pb) pb.onclick = () => pullPrompts(pb);

  sortPrompts(hits);
  document.getElementById('list').innerHTML = hits.length
    ? '<div class="grid">'+hits.map(promptCard).join('')+'</div>'
    : '<p class="empty">Nothing matches.</p>';

  wirePromptCards();
}

/* Pulled out of drawPrompts() so the Groups tab can wire the SAME prompt
   cards when they show up as a group's members - one wiring path, so a
   button added here can never work on its home tab and silently not on a
   group's. Queries the whole document, same as skills' wire(); harmless
   when the other tabs' own data-attributes simply are not present. */
function wirePromptCards(){
  const find = n => PROMPTS.find(x=>x.name===n);
  document.querySelectorAll('[data-rcopy]').forEach(b=>b.onclick=()=>{
    const p = find(b.dataset.rcopy);
    copy(p.body, b);
    countCopy(p);
  });
  document.querySelectorAll('[data-ropen]').forEach(b=>b.onclick=()=>{
    const p = find(b.dataset.ropen);
    if (p.url) window.open(p.url, '_blank', 'noopener');
  });
  document.querySelectorAll('[data-rref]').forEach(b=>b.onclick=async ()=>{
    if (!P.live){ note('Playing the local copy needs the server.', true); return; }
    const was = b.textContent; b.disabled = true; b.textContent = 'Opening…';
    try{
      // The NAME is posted, never the path - the server resolves it. A path
      // sent by the page is a path anything could have chosen.
      const r = await (await fetch('/api/openref', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({name: b.dataset.rref})})).json();
      if (!r.ok) throw new Error(r.error || 'failed');
      b.textContent = 'Playing';
    }catch(e){ note('Could not open the reference: '+e.message, true); }
    setTimeout(()=>{ b.disabled = false; b.textContent = was; }, 1500);
  });
  document.querySelectorAll('[data-rfav]').forEach(e=>e.onclick=()=>pfav(e, e.dataset.rfav));
  document.querySelectorAll('[data-ptagbtn]').forEach(b=>b.onclick=()=>{
    const n=b.dataset.ptagbtn, v=prompt('Tag for '+n+' (comma-separated):', ptagsFor(n).join(', '));
    if(v===null) return;
    setPTags(b, n, v.split(',').map(s=>s.trim()).filter(Boolean));
  });
  document.querySelectorAll('[data-groupbtn-prompt]').forEach(b=>b.onclick=()=>
    openGroupPicker('prompt', b.dataset.groupbtnPrompt));
  wireGroupChips();
  document.querySelectorAll('.card .ds, .card .pbody').forEach(d=>d.onclick=()=>
    d.parentElement.classList.toggle('open'));
}

/* Record the copy, then redraw so the count moves under your hand. Fire and
   forget: the text is already on the clipboard by the time this runs, and a
   failed counter must never make a successful copy look broken. */
function countCopy(p){
  if (!P.live) return;
  p.used = (p.used||0) + 1; p.ago = 0;
  fetch('/api/pcopy', {method:'POST', headers:{'Content-Type':'application/json'},
                       body: JSON.stringify({name:p.name})}).catch(()=>{});
  markLive('prompt', p.name);
  setTimeout(draw, 900);
}

async function pfav(el, name){
  const p = PROMPTS.find(x=>x.name===name);
  const want = !p.fav;
  if (!P.live){
    note('Favourites need the server — start it with python serve.py, not by opening '+
         'the .html file. Nothing was saved.', true);
    return;
  }
  el.classList.add('busy');
  try{
    const r = await fetch('/api/pfav', {method:'POST', headers:{'Content-Type':'application/json'},
                                       body: JSON.stringify({name, on: want})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    p.fav = j.fav;
    draw();
  }catch(e){
    el.classList.remove('busy');
    note('Could not star '+name+': '+e.message, true);
  }
}

/* Tools and hooks get their own endpoints for the same reason prompts did:
   /api/fav validates a name against the scanned SKILL inventory, so a starred
   tool would come back "not a known skill". Three lists, three files, three
   validators — each one refusing names it does not own. */
async function starPost(el, name, url, row){
  const want = !row.fav;
  if (!P.live){
    note('Favourites need the server — start it with python serve.py, not by opening '+
         'the .html file. Nothing was saved.', true);
    return;
  }
  el.classList.add('busy');
  try{
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                                body: JSON.stringify({name, on: want})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    row.fav = j.fav;
    draw();
  }catch(e){
    el.classList.remove('busy');
    note('Could not star '+name+': '+e.message, true);
  }
}
const tfav = (el, name) => starPost(el, name, '/api/tfav', TOOLS.find(x=>x.name===name));
const hfav = (el, name) => starPost(el, name, '/api/hfav', HOOKS.find(x=>x.name===name));

function sidebar(){
  sectionbar(); fillSort();
  ['nav-never','nav-sleep','nav-flags','nav-rec'].forEach(id=>{
    const e=document.getElementById(id); if(e) e.style.display='';
  });
  document.getElementById('cat').style.display = '';
  const pool = POOL();
  const tc={}, lc={};
  pool.forEach(i=>{ tc[i.reach]=(tc[i.reach]||0)+1; lc[i.location]=(lc[i.location]||0)+1; });

  // Mode sits above everything: it decides what every count below it means.
  const nMine = ITEMS.filter(i=>!isCat(i)).length;
  const nCat  = ITEMS.filter(isCat).length;
  document.getElementById('modenav').innerHTML =
    '<div class="nav'+(S.mode==='mine'?' on':'')+'" data-mode="mine" '+
      'title="Installed or written by you — everything that can actually be reached.">'+
      'Mine<span class="c">'+nMine+'</span></div>'+
    (nCat ? '<div class="nav'+(S.mode==='cat'?' on':'')+'" data-mode="cat" '+
      'title="Marketplace listings for plugins you have NOT installed. None of these can fire.">'+
      'Catalogue<span class="c">'+nCat+'</span></div>' : '');
  document.querySelectorAll('[data-mode]').forEach(e=>e.onclick=()=>{
    if (S.mode === e.dataset.mode) return;
    // Filters are scoped to a mode's contents, so carrying them across shows
    // an empty page and looks like a broken switch. Only the search survives.
    S.mode = e.dataset.mode;
    S.tier=S.loc=S.tag=S.origin=S.cat=null; S.fav=false; S.view=null;
    draw();
  });

  document.getElementById('tiers').innerHTML =
    '<div class="nav'+(!S.tier&&!S.loc&&!S.tag&&!S.q&&!S.origin&&!S.cat&&!S.fav?' on':'')+'" data-t=""><span class="dot" style="background:var(--edge2)"></span>All<span class="c">'+pool.length+'</span></div>'+
    Object.keys(TIERS).filter(k=>tc[k]).map(k =>
      '<div class="nav'+(S.tier===k?' on':'')+'" data-t="'+k+'" title="'+esc(TIERS[k][1])+'">'+
      '<span class="dot d-'+k+'"></span>'+TIERS[k][0]+'<span class="c">'+tc[k]+'</span></div>').join('');

  // Favourites sits above the tiers: it is the shortlist, and the whole point
  // is reaching it without reading past 173 skills first.
  const nf = ITEMS.filter(i=>i.fav).length;
  document.getElementById('favnav').innerHTML =
    '<div class="nav'+(S.fav?' on':'')+'" id="nav-fav" title="Skills you starred">'+
    '<span style="color:var(--gold);font-size:13px;line-height:1">★</span>Favourites'+
    '<span class="c">'+nf+'</span></div>';
  document.getElementById('nav-fav').onclick = () => {
    S.fav = !S.fav; S.view = null; draw();
  };

  const cc={}; pool.forEach(i=>i.cats.forEach(c=>{cc[c]=(cc[c]||0)+1}));
  const sel = document.getElementById('cat');
  const want = S.cat || '';
  sel.innerHTML = '<option value="">All categories ('+pool.length+')</option>'+
    P.categories.filter(c=>cc[c]).map(c =>
      '<option value="'+esc(c)+'">'+esc(c)+' ('+cc[c]+')</option>').join('');
  sel.value = want;

  const oc={}; pool.forEach(i=>{oc[i.origin]=(oc[i.origin]||0)+1});
  document.getElementById('origins').innerHTML =
    [['yours','Yours'],['borrowed','Borrowed']].filter(([k])=>oc[k]).map(([k,lbl]) =>
      '<div class="nav'+(S.origin===k?' on':'')+'" data-o="'+k+'" title="'+
      (k==='yours'?'You wrote it.':'Someone else wrote it — a LICENSE names them. Copies get no upstream updates.')+
      '">'+lbl+'<span class="c">'+oc[k]+'</span></div>').join('');
  document.querySelectorAll('[data-o]').forEach(e=>e.onclick=()=>{
    S.origin = S.origin===e.dataset.o ? null : e.dataset.o; S.view=null; draw();
  });

  document.getElementById('locs').innerHTML =
    Object.keys(lc).sort((a,b)=>lc[b]-lc[a]).map(l =>
      '<div class="nav'+(S.loc===l?' on':'')+'" data-l="'+esc(l)+'">'+esc(l)+
      '<span class="c">'+lc[l]+'</span></div>').join('');

  document.querySelectorAll('[data-t]').forEach(e=>e.onclick=()=>{
    if(!e.dataset.t){ S.tier=S.loc=S.tag=S.origin=S.cat=null; S.fav=false; S.q='';
                      document.getElementById('q').value=''; }
    else S.tier=e.dataset.t;
    S.view=null; draw();
  });
  document.querySelectorAll('[data-l]').forEach(e=>e.onclick=()=>{S.loc=S.loc===e.dataset.l?null:e.dataset.l;S.view=null;draw()});
}

function tagbar(){
  const t = allTags();
  document.getElementById('tagbar').innerHTML = t.length
    ? t.map(x=>'<span class="tag'+(S.tag===x?' on':'')+'" data-tag="'+esc(x)+'">'+esc(x)+'</span>').join('')
    : '<span class="tag" style="cursor:default;opacity:.5">no tags yet — use + Tag on any card</span>';
  document.querySelectorAll('[data-tag]').forEach(e=>e.onclick=()=>{
    S.tag = S.tag===e.dataset.tag ? null : e.dataset.tag; draw();
  });
}

/* ---------- phrase test ---------- */
const STOP = new Set('a an the and or of to in for on with from by is are be as at it this that i my me can you your do does how what when if want need make get some'.split(' '));
const bag = s => new Set((s||'').toLowerCase().match(/[a-z0-9-]{3,}/g)?.filter(w=>!STOP.has(w)) || []);

function phraseTest(text){
  const q = bag(text);
  const out = document.getElementById('pout');
  if (!q.size){ out.innerHTML=''; return; }

  const scored = ITEMS.filter(i => i.reach==='auto' || i.reach==='source')
    .map(i => {
      const d = bag(i.desc + ' ' + i.name.replace(/-/g,' '));
      let hit = 0; q.forEach(w => { if (d.has(w)) hit++; });
      return { i, score: hit / q.size };
    })
    .filter(r => r.score > 0)
    .sort((a,b) => b.score - a.score)
    .slice(0, 6);

  out.innerHTML = scored.length
    ? scored.map(r =>
        '<div class="hit"><div class="score">'+Math.round(r.score*100)+'%</div>'+
        '<div><div class="n">'+esc(r.i.name)+'</div>'+
        '<div class="w">'+esc(r.i.desc.slice(0,120))+'…</div></div></div>').join('')
    : '<div class="empty">Nothing matches — that phrasing would fire no skill at all.</div>';
}

/* ---------- cards ---------- */
function card(i){
  const t = tagsFor(i.name);
  return '<div class="card" data-n="'+esc(i.name)+'">'+
    '<div class="top"><span class="dot d-'+i.reach+'" title="'+esc(TIERS[i.reach][0])+'"></span>'+
      '<span class="nm" title="'+esc(i.name)+'">'+esc(i.name)+'</span>'+ toggle(i) + star(i) +'</div>'+
    (i.desc ? '<div class="ds">'+esc(i.desc)+'</div>'
            : '<div class="ds" style="color:var(--amber)">No description — this can never fire.</div>')+
    '<div class="why">'+esc(i.why)+'</div>'+
    '<div class="pa">'+esc(i.file)+'</div>'+
    (i.flags||[]).map(f=>'<div class="flag">⚠ '+esc(f)+'</div>').join('')+
    (i.collides ? '<div class="coll">competes with: '+esc([...new Set(i.collides)].join(', '))+'</div>' : '')+
    '<div class="meta">'+
      '<span class="chip tk" title="always in context / loaded on demand">'+i.desc_tokens+' + '+i.body_tokens+' tok</span>'+
      '<span class="chip" title="'+(i.cat_guessed?'inferred from the description — a guess':'from your labelled skills index (notion_rows.json)')+'">'+
        esc(i.cats[0])+(i.cat_guessed?' ?':'')+'</span>'+
      (i.origin==='borrowed' ? '<span class="chip" style="color:var(--t-src)" title="not yours — no upstream updates">'+
        (i.upstream ? 'borrowed · '+esc(i.upstream) : 'borrowed')+'</span>' : '')+
      (i.locked ? '<span class="chip" style="color:var(--t-src)" title="a parent skill routes to this — Manual would break it">locked auto</span>' : '')+
      (i.reach!=='off'
        ? '<span class="chip" style="color:'+(i.used?'var(--live)':'var(--off)')+'" title="invocations found in transcripts">'+
          (i.used? i.used+'x · '+i.ago+'d ago' : 'never fired')+'</span>' : '')+
      (i.verdict==='watch' ? '<span class="chip" style="color:var(--t-src)">just rewritten</span>' : '')+
      t.map(x=>'<span class="chip tg">'+esc(x)+'</span>').join('')+
      groupsFor('skill', i.name).map(g=>'<span class="chip gp" data-groupchip="'+g.id+'" '+
        'title="In group \''+esc(g.name)+'\' — click to open">'+esc(g.name)+'</span>').join('')+
    '</div>'+
    '<div class="acts">'+
      '<button class="btn" data-copy="'+esc(i.name)+'">'+
        (i.reach==='off'?'Copy path':i.reach==='manual'?'Copy /name':'Copy trigger')+'</button>'+
      '<button class="btn" data-path="'+esc(i.name)+'">Path</button>'+
      '<button class="btn" data-tagbtn="'+esc(i.name)+'">+ Tag</button>'+
      '<button class="btn" data-groupbtn-skill="'+esc(i.name)+'">+ Group</button>'+
    '</div></div>';
}

function reconcileView(){
  const n = P.notion;
  if (!n.available) return '<p class="empty">No notion_rows.json — see the README.</p>';
  const rowList = a => a.length
    ? '<ul style="margin:6px 0 0;padding-left:18px">'+a.map(x=>'<li><code>'+esc(x)+'</code></li>').join('')+'</ul>'
    : '<p style="color:var(--live);font-size:13px;margin:6px 0 0">None.</p>';
  return '<div style="max-width:760px">'+
    '<table><tr><th>On disk (yours)</th><th>Grid rows (should have a file)</th><th>Playbooks</th></tr>'+
    '<tr><td>'+n.files+'</td><td>'+n.rows+'</td><td>'+n.playbooks+' (pages, no file expected)</td></tr></table>'+
    '<h3 style="font-size:13px;margin:22px 0 0">Skills with no grid row</h3>'+
    '<p style="font-size:12.5px;color:var(--dim);margin:4px 0 0">Exact match on the normalised identifier, never substring.</p>'+
    rowList(n.missing_row)+
    '<h3 style="font-size:13px;margin:22px 0 0">Rows whose file is missing</h3>'+
    '<p style="font-size:12.5px;color:var(--dim);margin:4px 0 0">Row claims Home = Claude Code / Skills repo / Mixed, but nothing is on disk under that name. Either the file moved, or the Home field is wrong.</p>'+
    rowList(n.missing_file)+'</div>';
}

function flagsView(){
  // POOL(), not ITEMS: the sidebar's flag COUNT is scoped to the mode, so a
  // view that ignored it would show more cards than the number that opened it.
  // A count and its own list disagreeing is how a tool stops being believed.
  const bad = POOL().filter(i=>i.flags.length);
  const hidden = ITEMS.filter(i=>i.flags.length).length - bad.length;
  return (bad.length
    ? '<div class="grid">'+bad.map(card).join('')+'</div>'
    : '<p class="empty">No health flags in '+(S.mode==='cat'?'Catalogue':'Mine')+'.</p>')
    + (hidden ? '<p class="across">'+hidden+' more flagged in '+
       (S.mode==='cat'?'Mine':'Catalogue')+'</p>' : '');
}

/* Sorting. `ago` is null for anything never used, so those sink to the bottom
   of "recently used" rather than floating to the top as if they were fresh. */
function sortHits(a){
  const byName = (x,y) => x.name.localeCompare(y.name);
  const S_ = S.sort;
  if (S_==='used')   a.sort((x,y)=> (y.used-x.used) || byName(x,y));
  else if (S_==='unused') a.sort((x,y)=> (x.used-y.used) || byName(x,y));
  else if (S_==='cost')   a.sort((x,y)=> (y.desc_tokens-x.desc_tokens) || byName(x,y));
  else if (S_==='recent') a.sort((x,y)=>{
    const ax = x.ago==null ? Infinity : x.ago, ay = y.ago==null ? Infinity : y.ago;
    return (ax-ay) || byName(x,y);
  });
  else a.sort(byName);
}

/* Only your own machine skills can be switched. Everything else gets a dead
   switch WITH the reason - a hidden control makes people wonder, a disabled one
   with a tooltip answers the question. */
function why_dead(i){
  if (i.reach === 'off')   return 'Not loaded — nothing to switch. Paste its path to use it.';
  if (i.home === 'cloud')  return 'From an extra folder (extra_skill_dirs). It fires from wherever it is installed or uploaded, so Kit does not switch it.';
  if (i.locked)            return "'"+i.name.split('-')[0]+"' routes to this module — Manual would break the router.";
  if (!P.live)             return 'Opened as a file, so nothing can write. Run python serve.py to enable toggles.';
  return null;
}
/* ☆ outline on every card, ★ gold when starred. A <button> rather than a span
   so it is keyboard-reachable and announces itself; the outline is the same
   glyph, not a different icon, so the only thing that changes is the fill. */
function star(i){
  return '<button class="star'+(i.fav?' on':'')+'" data-fav="'+esc(i.name)+'"'+
    ' aria-pressed="'+(i.fav?'true':'false')+'"'+
    ' title="'+(i.fav?'In Favourites — click to remove':'Add to Favourites')+'">'+
    (i.fav?'★':'☆')+'</button>';
}

function toggle(i){
  const dead = why_dead(i);
  return '<span class="swrap">'+(i.reach==='auto'?'auto':i.reach==='manual'?'manual':'off')+
    '<span class="sw'+(i.reach==='auto'?' on':'')+(dead?' dead':'')+'"'+
    (dead ? ' title="'+esc(dead)+'"' : ' data-sw="'+esc(i.name)+'" title="Switch between Auto and Manual"')+
    '></span></span>';
}

async function flip(el, name){
  const i = ITEMS.find(x=>x.name===name);
  const want = i.reach === 'auto' ? 'manual' : 'auto';
  el.classList.add('busy');
  try{
    const r = await fetch('/api/mode', {method:'POST', headers:{'Content-Type':'application/json'},
                                        body: JSON.stringify({name, mode: want})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    i.reach = j.mode;
    i.why = j.mode === 'manual'
      ? 'Manual - description not in context. Only /'+name+' reaches it.'
      : 'In ~/.claude/skills - description loaded every session, fires on your phrasing.';
    draw();
    note(name+' → '+j.mode+'. '+j.note);
  }catch(e){
    el.classList.remove('busy');
    note('Could not switch '+name+': '+e.message, true);
  }
}

async function fav(el, name){
  const i = ITEMS.find(x=>x.name===name);
  const want = !i.fav;
  if (!P.live){
    note('Favourites need the server — start it with python serve.py, not by opening '+
         'the .html file. Nothing was saved.', true);
    return;
  }
  el.classList.add('busy');
  try{
    const r = await fetch('/api/fav', {method:'POST', headers:{'Content-Type':'application/json'},
                                       body: JSON.stringify({name, on: want})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    // Trust the SERVER's answer, not the click: it re-reads the file after
    // writing, so j.fav is what is actually on disk.
    i.fav = j.fav;
    draw();
  }catch(e){
    el.classList.remove('busy');
    note('Could not star '+name+': '+e.message, true);
  }
}

async function setTags(el, name, list){
  if (!P.live){
    note('Tags need the server — start it with python serve.py, not by opening '+
         'the .html file. Nothing was saved.', true);
    return;
  }
  el.classList.add('busy');
  try{
    const r = await fetch('/api/tag', {method:'POST', headers:{'Content-Type':'application/json'},
                                       body: JSON.stringify({name, tags: list})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    // Trust the SERVER's answer, not the click: it re-reads tags.json after
    // writing, so j.tags is what is actually on disk.
    if (j.tags.length) TAGS[name] = j.tags; else delete TAGS[name];
    saveTags();   // best-effort localStorage mirror, for the file:// fallback
    draw();
  }catch(e){
    el.classList.remove('busy');
    note('Could not save tags for '+name+': '+e.message, true);
  }
}

/* One tiny helper, called three times below, rather than three near-copies
   of setTags() diverging quietly over time the way the four favourites
   implementations already have (each carries its own comment explaining
   why it is separate - the endpoint and file per inventory, not the logic). */
async function setKindTags(el, endpoint, name, list, map, persist){
  if (!P.live){
    note('Tags need the server — start it with python serve.py, not by opening '+
         'the .html file. Nothing was saved.', true);
    return;
  }
  el.classList.add('busy');
  try{
    const r = await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'},
                                     body: JSON.stringify({name, tags: list})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    if (j.tags.length) map[name] = j.tags; else delete map[name];
    persist();
    draw();
  }catch(e){
    el.classList.remove('busy');
    note('Could not save tags for '+name+': '+e.message, true);
  }
}
const setPTags = (el, name, list) => setKindTags(el, '/api/ptag', name, list, PTAGS, savePTags);
const setTTags = (el, name, list) => setKindTags(el, '/api/ttag', name, list, TTAGS, saveTTags);
const setHTags = (el, name, list) => setKindTags(el, '/api/htag', name, list, HTAGS, saveHTags);

function note(msg, bad){
  let n = document.getElementById('note');
  if (!n){ n = document.createElement('div'); n.id='note'; document.body.appendChild(n); }
  n.textContent = msg;
  n.style.cssText = 'position:fixed;bottom:18px;left:50%;transform:translateX(-50%);z-index:9;'+
    'background:var(--panel);border:1px solid '+(bad?'var(--t-none)':'var(--t-on)')+';color:var(--ink);'+
    'padding:10px 16px;border-radius:5px;font-size:13px;box-shadow:0 6px 24px #0007';
  clearTimeout(n._t); n._t = setTimeout(()=>n.remove(), 4200);
}

/* ================= TOOLS ================= */

function tstar(t){
  return '<button class="star'+(t.fav?' on':'')+'" data-tfav="'+esc(t.name)+'"'+
    ' aria-pressed="'+(t.fav?'true':'false')+'"'+
    ' title="'+(t.fav?'In Favourites — click to remove':'Add to Favourites')+'">'+
    (t.fav?'★':'☆')+'</button>';
}

const START_RANK = {icon:0, scheduled:1, command:2, none:3};

function sortTools(a){
  // Declared here, not shared: sortPrograms and sortPrompts each keep their
  // own. Reaching for one of theirs is a ReferenceError at run time that
  // node --check cannot see and the Python suites never execute - it cost a
  // silently blank tab once already.
  const byName = (x,y) => x.name.localeCompare(y.name);
  const k = S.tsort;
  if (k==='start')      a.sort((x,y)=>(START_RANK[x.start]-START_RANK[y.start])||byName(x,y));
  else if (k==='recent')a.sort((x,y)=>(y.asof||'').localeCompare(x.asof||'')||byName(x,y));
  else if (k==='stale') a.sort((x,y)=>(x.asof||'9999').localeCompare(y.asof||'9999')||byName(x,y));
  else if (k==='flags') a.sort((x,y)=>(y.flags.length-x.flags.length)||byName(x,y));
  else if (k==='undoc') a.sort((x,y)=>((x.desc?1:0)-(y.desc?1:0))||byName(x,y));
  else a.sort(byName);
}

function toolSidebar(){
  const sc={}, fc={};
  TOOLS.forEach(t=>{
    sc[t.start] = (sc[t.start]||0)+1;
    t.flags.forEach(f=>{ fc[f] = (fc[f]||0)+1; });
  });

  document.getElementById('modenav').innerHTML = '';

  const nf = TOOLS.filter(t=>t.fav).length;
  document.getElementById('favnav').innerHTML =
    '<div class="nav'+(S.tfav?' on':'')+'" id="nav-tfav" title="Tools you starred">'+
    '<span style="color:var(--gold);font-size:13px;line-height:1">★</span>Favourites'+
    '<span class="c">'+nf+'</span></div>';
  document.getElementById('nav-tfav').onclick = () => { S.tfav = !S.tfav; draw(); };

  // The primary facet is HOW YOU START IT, which is a fact about the folder.
  // Grouping by "what it does" would be a guess, and the Prompts tab already
  // settled that a category has to be a lookup rather than an inference.
  const clean = !S.tstart&&!S.tflag&&!S.tfav&&!S.q;
  document.getElementById('tiers').innerHTML =
    '<div class="nav'+(clean?' on':'')+'" data-ts=""><span class="dot" style="background:var(--edge2)"></span>All<span class="c">'+TOOLS.length+'</span></div>'+
    Object.keys(STARTS).filter(k=>sc[k]).map(k =>
      '<div class="nav'+(S.tstart===k?' on':'')+'" data-ts="'+k+'" title="'+
      esc(STARTS[k][1])+'">'+esc(STARTS[k][0])+'<span class="c">'+sc[k]+'</span></div>').join('');

  // Health, straight from the measured flags. Every one of these is a thing
  // somebody could fix this afternoon, which is the only reason to list it.
  //
  // "nothing to run" is EXCLUDED here: it is exactly the start=none facet
  // above, so listing it in both put the same word and the same count twice
  // in one sidebar, reading as two different filters that happen to agree.
  // The card still carries it as a warning, where it is not a duplicate.
  delete fc['nothing to run'];
  document.getElementById('origins').innerHTML =
    Object.keys(fc).sort((a,b)=>fc[b]-fc[a]).map(f =>
      '<div class="nav'+(S.tflag===f?' on':'')+'" data-tf="'+esc(f)+'">'+
      esc(f[0].toUpperCase()+f.slice(1))+'<span class="c">'+fc[f]+'</span></div>').join('');

  // "What is up right now" is the question a tab full of bridges, servers and
  // panels is actually asked, and it is not the same as "what fired recently".
  document.getElementById('locs').innerHTML = RUNNING.size
    ? '<div class="nav'+(S.tup?' on':'')+'" id="nav-tup" title="Tools with a '+
      'live process this second. Refreshed every ten seconds.">'+
      '<span class="dot" style="background:var(--t-src)"></span>Running now'+
      '<span class="c">'+RUNNING.size+'</span></div>'
    : '';
  const upNav = document.getElementById('nav-tup');
  if (upNav) upNav.onclick = () => { S.tup = !S.tup; draw(); };

  document.querySelectorAll('[data-ts]').forEach(e=>e.onclick=()=>{
    if(!e.dataset.ts){ S.tstart=S.tflag=null; S.tfav=false; S.tup=false; S.q='';
                       document.getElementById('q').value=''; }
    else S.tstart = S.tstart===e.dataset.ts ? null : e.dataset.ts;
    draw();
  });
  document.querySelectorAll('[data-tf]').forEach(e=>e.onclick=()=>{
    S.tflag = S.tflag===e.dataset.tf ? null : e.dataset.tf; draw();
  });
}

/* Single source of truth for the running/connected badge, shared by
   toolCard() (first paint) and paintLive() (every poll after that) - the
   two must never drift, or a badge added by one and expected by the other
   silently stops updating, which is exactly the bug this function fixes. */
function toolBadge(name){
  if (CONNECTED.has(name))
    return '<span class="connbadge" title="A verified live signal, not just a '+
      'process: CursorBridge answering on its own port, or the app '+
      'itself running. Stronger than ‘running’, which some MCP '+
      'tools show constantly because Claude Code spawns their bridge in '+
      'every open session whether or not the app is open.">connected</span>';
  if (RUNNING.has(name))
    return '<span class="upbadge" title="A live process belongs to this tool '+
      'right now — for some MCP-backed tools that process is spawned by '+
      'every open Claude Code session regardless of whether the app '+
      'itself is open, so this does not by itself mean connected. '+
      'Different from the glow: that says something happened recently, '+
      'this says a process is still up.">running</span>';
  return '';
}

/* Only tools that ARE an integration with a real outside app/service get
   its mark - a fork or MCP bridge counts, a folder that merely mentions the
   name in passing does not. Keyed by the tool's roster name in tools.py. */
const TOOL_BRAND = {
  'davinci-bridge': 'resolve', 'editors-panel': 'resolve', 'resolve': 'resolve',
  'premiere-mcp': 'premiere',
  'ae-toolkit': 'ae',
  'notion-backup': 'notion',
  'ytclip': 'youtube',
};
const BRAND_TITLE = {
  resolve: 'DaVinci Resolve', premiere: 'Adobe Premiere Pro',
  ae: 'Adobe After Effects', notion: 'Notion', youtube: 'YouTube',
};
const BRAND_SVG = {
  resolve: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="5" fill="#0B4F4F"/>'+
    '<polygon points="24,0 24,24 0,24" fill="#E8792B"/>'+
    '<text x="12" y="16.3" font-family="Arial,Helvetica,sans-serif" font-size="7.5" font-weight="700" '+
    'fill="#fff" text-anchor="middle">DR</text></svg>',
  premiere: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="5" fill="#00005B"/>'+
    '<text x="12" y="16.3" font-family="Arial,Helvetica,sans-serif" font-size="9.5" font-weight="700" '+
    'fill="#fff" text-anchor="middle">Pr</text></svg>',
  ae: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="5" fill="#2B0B3F"/>'+
    '<text x="12" y="16.3" font-family="Arial,Helvetica,sans-serif" font-size="9.5" font-weight="700" '+
    'fill="#fff" text-anchor="middle">Ae</text></svg>',
  notion: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="1" y="1" width="22" height="22" rx="5" '+
    'fill="#fff" stroke="#000" stroke-width="1.4"/>'+
    '<text x="12" y="16.8" font-family="Georgia,\'Times New Roman\',serif" font-size="13" font-weight="700" '+
    'fill="#000" text-anchor="middle">N</text></svg>',
  youtube: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="5" fill="#FF0000"/>'+
    '<path d="M10 8.3l6.2 3.7-6.2 3.7z" fill="#fff"/></svg>',
};

function toolIcon(name){
  const brand = TOOL_BRAND[name];
  if (brand)
    return '<span class="ticon" title="'+esc(BRAND_TITLE[brand])+'">'+BRAND_SVG[brand]+'</span>';
  const ch = ((name.replace(/^[^A-Za-z0-9]+/, '')[0]) || '?').toUpperCase();
  return '<span class="ticon tico-generic" title="One of your own tools - no external app or '+
    'service to show a mark for">'+esc(ch)+'</span>';
}

function toolCard(t){
  const s = STARTS[t.start] || ['?',''];
  const age = t.age === null || t.age === undefined ? ''
    : t.age < 1 ? 'today' : t.age === 1 ? 'yesterday'
    : t.age < 400 ? t.age+'d ago' : Math.round(t.age/365)+'y ago';

  return '<div class="card" data-tn="'+esc(t.name)+'">'+
    '<div class="top"><span class="dot d-'+(t.flags.length?'manual':'auto')+'"></span>'+
      toolIcon(t.name)+
      '<span class="nm" title="'+esc(t.name)+'">'+esc(t.name)+'</span>'+
      toolBadge(t.name)+
      tstar(t) +'</div>'+
    (t.desc
      ? '<div class="ds">'+esc(t.desc)+'</div>'+
        '<div class="pfrom">from '+esc(t.desc_from)+'</div>'
      : '<div class="ds" style="color:var(--amber)">Nothing describes this. '+
        'No README, no CLAUDE.md, no START_HERE — so Kit will not invent one.</div>')+
    (t.flags||[]).map(f=>'<div class="flag">⚠ '+esc(f)+'</div>').join('')+
    '<div class="meta">'+
      '<span class="chip" title="'+esc(s[1])+'">'+esc(s[0])+'</span>'+
      (t.shortcuts||[]).map(x=>'<span class="chip" style="color:var(--gold)" '+
        'title="A desktop shortcut points here">'+esc(x)+'</span>').join('')+
      (t.tasks||[]).map(x=>'<span class="chip" style="color:var(--gold)" '+
        'title="A Windows scheduled task runs this">'+esc(x)+'</span>').join('')+
      (t.git
        ? '<span class="chip gt" title="'+(t.remote?esc(t.remote):'no remote — this exists only on this machine')+
          '">git · '+esc(t.commit||'?')+'</span>'
        : '<span class="chip" style="color:var(--amber)" title="No .git — there is no history and no commit to cite">no git</span>')+
      (age ? '<span class="chip tk" title="Last commit for a repo, newest file otherwise">'+age+'</span>' : '')+
      (t.n_entries ? '<span class="chip tk" title="'+esc((t.entries||[]).join(', '))+
        '">'+t.n_entries+' entry point'+(t.n_entries===1?'':'s')+'</span>' : '')+
      (t.notion.length
        ? '<span class="chip" style="color:var(--live)" title="'+esc(t.notion.join(' · '))+
          '">Docs ×'+t.notion.length+'</span>'
        : '')+
      (t.tags||[]).map(x=>'<span class="chip tg">'+esc(x)+'</span>').join('')+
      groupsFor('tool', t.name).map(g=>'<span class="chip gp" data-groupchip="'+g.id+'" '+
        'title="In group \''+esc(g.name)+'\' — click to open">'+esc(g.name)+'</span>').join('')+
    '</div>'+
    '<div class="acts">'+
      (t.launch
        ? '<button class="btn" data-tlaunch="'+esc(t.name)+'" title="Run '+
          esc(t.launch)+'">▶ Launch</button>'
        : '')+
      '<button class="btn" data-tpath="'+esc(t.name)+'">Copy path</button>'+
      '<button class="btn" data-ttagbtn="'+esc(t.name)+'">+ Tag</button>'+
      '<button class="btn" data-groupbtn-tool="'+esc(t.name)+'">+ Group</button>'+
    '</div></div>';
}

function drawTools(){
  sectionbar(); fillSort(); toolSidebar();
  secHeads('How you start it', 'Health', 'Right now', null);
  document.getElementById('cat').style.display = 'none';
  document.getElementById('tagbar').innerHTML = '';
  ['nav-never','nav-sleep','nav-flags','nav-rec'].forEach(id=>{
    const e=document.getElementById(id); if(e) e.style.display='none';
  });
  document.getElementById('offline').innerHTML = P.live ? '' :
    '<b>Read-only.</b> This page was opened as a file, so every ★ is inert. '+
    'Close this tab and run <code>python serve.py</code>, then use '+
    '<code>http://127.0.0.1:'+(P.port||8712)+'/</code>.';

  if (!TOOLS.length){
    document.getElementById('bar').innerHTML = '';
    document.getElementById('list').innerHTML =
      (TMETA.configured === false
        ? '<p class="empty">Tools are not configured.<br><br>Set <code>tools_root</code> '+
          'in <code>kit.config.json</code> to the folder whose subfolders are your '+
          'tools, then list the ones worth finding again in <code>tools.json</code> '+
          '(copy <code>tools.example.json</code>). The roster is a decision, not a rule.</p>'
        : '<p class="empty">No tools rostered.<br><br>The roster lives in '+
          '<code>tools.json</code> (see <code>tools.example.json</code>) — it is a '+
          'decision, not a rule.'+
          ((TMETA.unlisted||[]).length ? '<br><br>'+TMETA.unlisted.length+' folder(s) under '+
            '<code>'+esc(TMETA.root||'')+'</code> are waiting to be rostered or rejected: '+
            esc(TMETA.unlisted.join(', '))+'.' : '')+'</p>');
    return;
  }

  const f = S.q.trim().toLowerCase();
  const hits = TOOLS.filter(t =>
    (!S.tfav || t.fav) &&
    (!S.tup || RUNNING.has(t.name)) &&
    (!S.tstart || t.start===S.tstart) &&
    (!S.tflag || t.flags.includes(S.tflag)) &&
    (!f || (t.name+' '+(t.desc||'')+' '+(t.entries||[]).join(' ')+' '+
            (t.notion||[]).join(' ')+' '+(t.shortcuts||[]).join(' ')+' '+
            (t.tags||[]).join(' '))
             .toLowerCase().includes(f))
  );
  sortTools(hits);

  const on = [S.tfav && '★ Favourites', S.tup && 'Running now',
              S.tstart && STARTS[S.tstart][0],
              S.tflag, S.q && ('"'+S.q+'"')].filter(Boolean);

  // The drift valve, stated where it will actually be read. An explicit
  // roster silently going out of date is the failure this whole tab was
  // designed around; a count nobody sees would not prevent it.
  const un = (TMETA.unlisted||[]).length;
  const miss = (TMETA.missing||[]).length;
  const notes =
    (miss ? '<span style="color:var(--amber)"> · '+miss+' rostered '+
            (miss===1?'folder is':'folders are')+' missing from disk: '+
            esc((TMETA.missing||[]).join(', '))+'</span>' : '')+
    (un ? '<span style="color:var(--amber)" title="'+esc((TMETA.unlisted||[]).join(', '))+
          '"> · '+un+' folder'+(un===1?'':'s')+' under tools_root '+(un===1?'is':'are')+
          ' neither rostered nor rejected — decide, or the roster quietly rots</span>' : '');

  document.getElementById('bar').innerHTML = on.length
    ? 'Showing <b>'+hits.length+'</b> of '+TOOLS.length+' tools — filtered by '+
      on.map(x=>'<b>'+esc(x)+'</b>').join(' + ')+
      ' <span class="clear" id="clr">clear</span>'
    : '<span style="color:var(--dim)">All '+TOOLS.length+' tools — ticked by '+
      'hand in tools.json, because no rule reproduces the answer.'+
      (TMETA.notion_ok ? ' Docs column derived from the alignment manifest.' : '')+
      (TMETA.windows === false ? ' Desktop-icon and scheduled-task facets are '+
                                 'Windows-only and read n/a here.' : '')+
      '</span>'+notes;
  const clr = document.getElementById('clr');
  if (clr) clr.onclick = () => { S.tstart=S.tflag=null; S.tfav=false; S.tup=false; S.q='';
                                 document.getElementById('q').value=''; draw(); };

  document.getElementById('list').innerHTML = hits.length
    ? '<div class="grid">'+hits.map(toolCard).join('')+'</div>'
    : '<p class="empty">Nothing matches.</p>';

  wireToolCards();
}

/* Pulled out of drawTools() for the same reason wirePromptCards() was -
   the Groups tab renders these same cards as members and needs the same
   buttons to work there, not a second copy that can drift from this one. */
function wireToolCards(){
  const find = n => TOOLS.find(x=>x.name===n);
  document.querySelectorAll('[data-tpath]').forEach(b=>b.onclick=()=>
    copy(find(b.dataset.tpath).path, b));
  /* Launch needs the server: a browser cannot start a .bat from an http:// page,
     so the server does it, resolving the NAME through its own scan(). Opened as
     a file there is no server, hence the same guard the group buttons use. */
  document.querySelectorAll('[data-tlaunch]').forEach(b=>b.onclick=async ()=>{
    const n = b.dataset.tlaunch;
    if (!P.live){ note('Launch needs the server — start it with python serve.py.', true); return; }
    const was = b.textContent; b.textContent = 'starting…'; b.disabled = true;
    try{
      const r = await (await fetch('/api/launch', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({name:n})})).json();
      if (r.ok){ b.textContent = '▶ '+r.launched; note(n+' started.'); }
      else { b.textContent = was; note(r.error || 'could not launch '+n, true); }
    } catch(e){ b.textContent = was; note('could not reach the server', true); }
    setTimeout(()=>{ b.textContent = was; b.disabled = false; }, 2500);
  });
  document.querySelectorAll('[data-tfav]').forEach(e=>e.onclick=()=>tfav(e, e.dataset.tfav));
  document.querySelectorAll('[data-ttagbtn]').forEach(b=>b.onclick=()=>{
    const n=b.dataset.ttagbtn, v=prompt('Tag for '+n+' (comma-separated):', ttagsFor(n).join(', '));
    if(v===null) return;
    setTTags(b, n, v.split(',').map(s=>s.trim()).filter(Boolean));
  });
  document.querySelectorAll('[data-groupbtn-tool]').forEach(b=>b.onclick=()=>
    openGroupPicker('tool', b.dataset.groupbtnTool));
  wireGroupChips();
  document.querySelectorAll('.card .ds').forEach(d=>d.onclick=()=>
    d.parentElement.classList.toggle('open'));
}

/* ================= HOOKS ================= */

function hstar(h){
  return '<button class="star'+(h.fav?' on':'')+'" data-hfav="'+esc(h.name)+'"'+
    ' aria-pressed="'+(h.fav?'true':'false')+'"'+
    ' title="'+(h.fav?'In Favourites — click to remove':'Add to Favourites')+'">'+
    (h.fav?'★':'☆')+'</button>';
}

function sortHooks(a){
  const byName = (x,y) => x.name.localeCompare(y.name);
  const k = S.hsort;
  if (k==='event')        a.sort((x,y)=>(x.events[0]||'~').localeCompare(y.events[0]||'~')||byName(x,y));
  else if (k==='lines')   a.sort((x,y)=>(y.lines-x.lines)||byName(x,y));
  else if (k==='untested')a.sort((x,y)=>((x.test?1:0)-(y.test?1:0))||byName(x,y));
  else if (k==='problem') a.sort((x,y)=>(y.flags.length-x.flags.length)||byName(x,y));
  else a.sort(byName);
}

function hookSidebar(){
  const ec={};
  HOOKS.forEach(h=>h.events.forEach(e=>{ ec[e]=(ec[e]||0)+1; }));

  document.getElementById('modenav').innerHTML = '';

  const nf = HOOKS.filter(h=>h.fav).length;
  document.getElementById('favnav').innerHTML =
    '<div class="nav'+(S.hfav?' on':'')+'" id="nav-hfav" title="Hooks you starred">'+
    '<span style="color:var(--gold);font-size:13px;line-height:1">★</span>Favourites'+
    '<span class="c">'+nf+'</span></div>';
  document.getElementById('nav-hfav').onclick = () => { S.hfav = !S.hfav; draw(); };

  const clean = !S.hevent&&!S.hview&&!S.hfav&&!S.q;
  document.getElementById('tiers').innerHTML =
    '<div class="nav'+(clean?' on':'')+'" data-he=""><span class="dot" style="background:var(--edge2)"></span>All<span class="c">'+HOOKS.length+'</span></div>'+
    Object.keys(ec).sort().map(e =>
      '<div class="nav'+(S.hevent===e?' on':'')+'" data-he="'+esc(e)+'" '+
      'title="Fires on the '+esc(e)+' event">'+esc(e)+
      '<span class="c">'+ec[e]+'</span></div>').join('');

  // The two findings a file listing can never produce, and the reason this
  // tab is not a filter over Progs.
  const orph = HOOKS.filter(h=>!h.registered).length;
  const miss = HOOKS.filter(h=>!h.exists).length;
  const out  = HOOKS.filter(h=>h.exists&&!h.in_hookdir).length;
  const unt  = HOOKS.filter(h=>h.exists&&h.kind==='python'&&!h.test).length;
  const views = [
    ['orphan', 'Cannot fire', orph, 'On disk, but nothing in settings.json registers it.'],
    ['missing','Missing file', miss, 'Registered, but the file is not there — the harness will fail on it.'],
    ['outside','Outside hooks/', out, 'Registered from somewhere other than ~/.claude/hooks.'],
    ['untested','No test', unt, 'A guard with no test is a guard nobody has watched fail.']
  ];
  document.getElementById('origins').innerHTML = views.map(([k,l,n,t]) =>
    '<div class="nav'+(S.hview===k?' on':'')+'" data-hv="'+k+'" title="'+esc(t)+'">'+
    esc(l)+'<span class="c">'+n+'</span></div>').join('');

  document.getElementById('locs').innerHTML = '';

  document.querySelectorAll('[data-he]').forEach(e=>e.onclick=()=>{
    if(!e.dataset.he){ S.hevent=S.hview=null; S.hfav=false; S.q='';
                       document.getElementById('q').value=''; }
    else S.hevent = S.hevent===e.dataset.he ? null : e.dataset.he;
    draw();
  });
  document.querySelectorAll('[data-hv]').forEach(e=>e.onclick=()=>{
    S.hview = S.hview===e.dataset.hv ? null : e.dataset.hv; draw();
  });
}

function hookCard(h){
  // The matcher IS the answer to "when does this run", so it is shown raw
  // rather than prettified. A regex the harness actually matches on is worth
  // more than a friendly paraphrase of it that could be wrong.
  const regs = (h.regs||[]).map(r =>
    '<div class="hreg"><span class="hev">'+esc(r.event)+'</span>'+
    '<code>'+esc(r.matcher || '(everything)')+'</code>'+
    (r.scope==='project' ? '<span class="hsc" title="From a project settings.json '+
      '(project_settings) — fires only in that project">project</span>' : '')+
    '</div>').join('');

  return '<div class="card" data-hn="'+esc(h.name)+'">'+
    '<div class="top"><span class="dot d-'+
      (!h.exists ? 'off' : h.registered ? (h.flags.length?'manual':'auto') : 'off')+
      '"></span><span class="nm" title="'+esc(h.name)+'">'+esc(h.name)+'</span>'+ hstar(h) +'</div>'+
    (h.summary
      ? '<div class="ds">'+esc(h.summary)+'</div>'
      : '<div class="ds" style="color:var(--amber)">No docstring — nothing says what this does.</div>')+
    (regs ? '<div class="hregs">'+regs+'</div>'
          : '<div class="flag">⚠ Not registered anywhere. This file cannot fire.</div>')+
    (h.flags||[]).map(f=>'<div class="flag">⚠ '+esc(f)+'</div>').join('')+
    '<div class="meta">'+
      '<span class="chip" title="'+esc(h.path)+'">'+esc(h.kind)+'</span>'+
      (h.lines ? '<span class="chip tk">'+h.lines+' lines</span>' : '')+
      (h.test ? '<span class="chip" style="color:var(--live)" title="'+esc(h.test)+
                '">tested</span>'
              : '<span class="chip" style="color:var(--amber)">no test</span>')+
      (h.scopes||[]).map(s=>'<span class="chip tg">'+esc(s)+'</span>').join('')+
      (h.tags||[]).map(x=>'<span class="chip tg">'+esc(x)+'</span>').join('')+
    '</div>'+
    '<div class="acts">'+
      '<button class="btn" data-hpath="'+esc(h.name)+'">Copy path</button>'+
      '<button class="btn" data-htagbtn="'+esc(h.name)+'">+ Tag</button>'+
    '</div></div>';
}

function drawHooks(){
  sectionbar(); fillSort(); hookSidebar();
  secHeads('Fires on', 'Problems', null, null);
  document.getElementById('cat').style.display = 'none';
  document.getElementById('tagbar').innerHTML = '';
  ['nav-never','nav-sleep','nav-flags','nav-rec'].forEach(id=>{
    const e=document.getElementById(id); if(e) e.style.display='none';
  });
  document.getElementById('offline').innerHTML = P.live ? '' :
    '<b>Read-only.</b> This page was opened as a file, so every ★ is inert. '+
    'Close this tab and run <code>python serve.py</code>, then use '+
    '<code>http://127.0.0.1:'+(P.port||8712)+'/</code>.';

  if (!HOOKS.length){
    document.getElementById('bar').innerHTML = '';
    document.getElementById('list').innerHTML =
      '<p class="empty">No hooks found.<br><br>Nothing is registered in '+
      '<code>~/.claude/settings.json</code> or any <code>project_settings</code> '+
      'file in kit.config.json.</p>';
    return;
  }

  const f = S.q.trim().toLowerCase();
  const VIEW = {
    orphan  : h => !h.registered,
    missing : h => !h.exists,
    outside : h => h.exists && !h.in_hookdir,
    untested: h => h.exists && h.kind==='python' && !h.test
  };
  const hits = HOOKS.filter(h =>
    (!S.hfav || h.fav) &&
    (!S.hevent || h.events.includes(S.hevent)) &&
    (!S.hview || VIEW[S.hview](h)) &&
    // The MATCHER is searched too. "which hook fires on Bash" is the actual
    // question people arrive with, and the answer lives in the regex.
    (!f || (h.name+' '+(h.summary||'')+' '+h.events.join(' ')+' '+
            (h.regs||[]).map(r=>r.matcher).join(' ')+' '+(h.tags||[]).join(' '))
             .toLowerCase().includes(f))
  );
  sortHooks(hits);

  const on = [S.hfav && '★ Favourites', S.hevent,
              S.hview && {orphan:'Cannot fire', missing:'Missing file',
                          outside:'Outside hooks/', untested:'No test'}[S.hview],
              S.q && ('"'+S.q+'"')].filter(Boolean);

  document.getElementById('bar').innerHTML = on.length
    ? 'Showing <b>'+hits.length+'</b> of '+HOOKS.length+' hooks — filtered by '+
      on.map(x=>'<b>'+esc(x)+'</b>').join(' + ')+
      ' <span class="clear" id="clr">clear</span>'
    : '<span style="color:var(--dim)">'+HMETA.registered+' registered from '+
      'settings.json'+(HMETA.orphans ? ', '+HMETA.orphans+' on disk that cannot fire' : '')+
      '. <b>Registered is not the same as firing</b> — the harness names no '+
      'hook in the transcript, so Kit can show you the wiring and never the '+
      'behaviour.</span>'+
      ((HMETA.shell||[]).length ? '<span style="color:var(--dim)"> · '+
        HMETA.shell.length+' registration'+(HMETA.shell.length===1?' is':'s are')+
        ' not a script at all (<code>'+esc(HMETA.shell[0].cmd.slice(0,42))+
        '…</code>)</span>' : '')+
      (HMETA.problems||[]).map(p=>'<div class="flag">⚠ '+esc(p)+'</div>').join('');
  const clr = document.getElementById('clr');
  if (clr) clr.onclick = () => { S.hevent=S.hview=null; S.hfav=false; S.q='';
                                 document.getElementById('q').value=''; draw(); };

  document.getElementById('list').innerHTML = hits.length
    ? '<div class="grid">'+hits.map(hookCard).join('')+'</div>'
    : '<p class="empty">Nothing matches.</p>';

  const find = n => HOOKS.find(x=>x.name===n);
  document.querySelectorAll('[data-hpath]').forEach(b=>b.onclick=()=>
    copy(find(b.dataset.hpath).path, b));
  document.querySelectorAll('[data-hfav]').forEach(e=>e.onclick=()=>hfav(e, e.dataset.hfav));
  document.querySelectorAll('[data-htagbtn]').forEach(b=>b.onclick=()=>{
    const n=b.dataset.htagbtn, v=prompt('Tag for '+n+' (comma-separated):', htagsFor(n).join(', '));
    if(v===null) return;
    setHTags(b, n, v.split(',').map(s=>s.trim()).filter(Boolean));
  });
}

/* ================= GROUPS ================= */
/* Cross-inventory shortlists. Skills/Prompts/Tools only, deliberately - see
   groups.py: Hooks and Progs are plumbing you reconcile, not "go-to tricks"
   you would curate a list of for a job like editing or brainstorming. */

function groupSidebar(){
  document.getElementById('modenav').innerHTML = '';
  document.getElementById('favnav').innerHTML = '';
  const list = groupList();
  const clean = !S.groupOpen;
  document.getElementById('tiers').innerHTML =
    '<div class="nav'+(clean?' on':'')+'" data-gnav="">'+
    '<span class="dot" style="background:var(--edge2)"></span>All groups'+
    '<span class="c">'+list.length+'</span></div>'+
    list.map(g=>'<div class="nav'+(S.groupOpen===g.id?' on':'')+'" data-gnav="'+g.id+'">'+
      '<span class="dot" style="background:var(--purple)"></span>'+esc(g.name)+
      '<span class="c">'+g.members.length+'</span></div>').join('');
  document.getElementById('origins').innerHTML = '';
  document.getElementById('locs').innerHTML = '';
  document.querySelectorAll('[data-gnav]').forEach(e=>e.onclick=()=>{
    S.groupOpen = e.dataset.gnav || null; draw();
  });
}

function groupCard(g){
  const counts = {skill:0, prompt:0, tool:0};
  g.members.forEach(m=>{ if (m.kind in counts) counts[m.kind]++; });
  const bits = [['skill','skill'],['prompt','prompt'],['tool','tool']]
    .filter(([k])=>counts[k]).map(([k])=>counts[k]+' '+k+(counts[k]===1?'':'s'));
  return '<div class="card" data-gopen="'+g.id+'" style="cursor:pointer">'+
    '<div class="top"><span class="dot" style="background:var(--purple)"></span>'+
      '<span class="nm">'+esc(g.name)+'</span></div>'+
    '<div class="ds">'+(bits.length ? bits.join(' · ')
      : 'Empty — open any Skill, Prompt or Tool card and click <b>+ Group</b>.')+'</div>'+
    '<div class="acts">'+
      '<button class="btn" data-grename="'+g.id+'">Rename</button>'+
      '<button class="btn" data-gdelete="'+g.id+'">Delete</button>'+
    '</div></div>';
}

/* A member renders as EXACTLY the card its home tab would show - same
   star, same tags, same +Group button - with one thing added: a ✕ that
   removes it from just THIS group. A member whose own inventory no longer
   has it (a tool renamed, a prompt deleted in Notion) renders as a ghost
   naming what is gone, because silently dropping it would look like the
   group forgot rather than the source did. */
function wrapMember(kind, name, html){
  return '<div class="group-member">'+html+
    '<button class="group-remove" data-gremove-kind="'+kind+'" '+
    'data-gremove-name="'+esc(name)+'" title="Remove from this group">✕</button></div>';
}
function ghostMember(kind, name){
  return wrapMember(kind, name,
    '<div class="card ghost"><div class="top"><span class="nm">'+esc(name)+'</span></div>'+
    '<div class="ds">No longer a known '+kind+' — removed from its own inventory '+
    'since it was added here.</div></div>');
}
function memberCard(m){
  if (m.kind === 'skill'){
    const it = ITEMS.find(x=>x.name===m.name);
    return it ? wrapMember('skill', m.name, card(it)) : ghostMember('skill', m.name);
  }
  if (m.kind === 'prompt'){
    const p = PROMPTS.find(x=>x.name===m.name);
    return p ? wrapMember('prompt', m.name, promptCard(p)) : ghostMember('prompt', m.name);
  }
  if (m.kind === 'tool'){
    const t = TOOLS.find(x=>x.name===m.name);
    return t ? wrapMember('tool', m.name, toolCard(t)) : ghostMember('tool', m.name);
  }
  return '';
}

function drawGroups(){
  sectionbar(); fillSort();
  document.getElementById('cat').style.display = 'none';
  document.getElementById('tagbar').innerHTML = '';
  ['nav-never','nav-sleep','nav-flags','nav-rec'].forEach(id=>{
    const e=document.getElementById(id); if(e) e.style.display='none';
  });
  groupSidebar();
  secHeads('Your groups', null, null, null);

  document.getElementById('offline').innerHTML = P.live ? '' :
    '<b>Read-only.</b> This page was opened as a file, so groups cannot be created, '+
    'renamed, deleted, or added to. Close this tab and run <code>python serve.py</code>, '+
    'then use <code>http://127.0.0.1:'+(P.port||8712)+'/</code>.';

  const open = S.groupOpen ? GROUPS[S.groupOpen] : null;

  if (!open){
    const list = groupList();
    document.getElementById('bar').innerHTML = list.length
      ? 'Showing <b>'+list.length+'</b> group'+(list.length===1?'':'s')
      : '<span style="color:var(--dim)">No groups yet. A group is a shortlist that '+
        'mixes Skills, Prompts and Tools — a set of go-to tricks for one job, like '+
        'editing or brainstorming.</span>';

    document.getElementById('list').innerHTML =
      '<div class="grid">'+
        '<div class="card" id="gnew" style="cursor:pointer;border-style:dashed;'+
          'align-items:center;justify-content:center;text-align:center">'+
          '<div class="nm">+ New Group</div></div>'+
        list.map(groupCard).join('')+
      '</div>';

    document.getElementById('gnew').onclick = async () => {
      const v = prompt('Name this group (e.g. "Trial by Listener", "Editing"):');
      if (!v || !v.trim()) return;
      const id = await createGroup(v.trim());
      if (id){ S.groupOpen = id; draw(); }
    };
    document.querySelectorAll('[data-gopen]').forEach(e=>e.onclick=()=>{
      S.groupOpen = e.dataset.gopen; draw();
    });
    document.querySelectorAll('[data-grename]').forEach(b=>b.onclick=ev=>{
      ev.stopPropagation();
      const g = GROUPS[b.dataset.grename];
      const v = prompt('Rename group:', g.name);
      if (v && v.trim()) renameGroup(b.dataset.grename, v.trim());
    });
    document.querySelectorAll('[data-gdelete]').forEach(b=>b.onclick=ev=>{
      ev.stopPropagation();
      const g = GROUPS[b.dataset.gdelete];
      if (confirm('Delete "'+g.name+'"? The '+g.members.length+' item'+
                  (g.members.length===1?'':'s')+' inside are untouched — only the '+
                  'group itself goes.'))
        deleteGroup(b.dataset.gdelete);
    });
    return;
  }

  document.getElementById('bar').innerHTML =
    '<span class="clear" id="gback">← All groups</span>'+
    '<span class="fchip">'+esc(open.name)+'</span>'+
    '<span style="color:var(--dim)">'+open.members.length+' member'+
    (open.members.length===1?'':'s')+'</span>'+
    '<button class="btn" id="grename2">Rename</button>'+
    '<button class="btn" id="gdelete2">Delete</button>';
  document.getElementById('gback').onclick = () => { S.groupOpen=null; draw(); };
  document.getElementById('grename2').onclick = () => {
    const v = prompt('Rename group:', open.name);
    if (v && v.trim()) renameGroup(S.groupOpen, v.trim());
  };
  document.getElementById('gdelete2').onclick = () => {
    if (confirm('Delete "'+open.name+'"? The '+open.members.length+' item'+
                (open.members.length===1?'':'s')+' inside are untouched — only the '+
                'group itself goes.'))
      deleteGroup(S.groupOpen);
  };

  if (!open.members.length){
    document.getElementById('list').innerHTML =
      '<p class="empty">Nothing in this group yet. Find a Skill, Prompt or Tool '+
      'card and click <b>+ Group</b> to add it here.</p>';
    return;
  }

  const bykind = k => open.members.filter(m=>m.kind===k);
  const section = (label, k) => {
    const ms = bykind(k);
    if (!ms.length) return '';
    return '<div class="grp1"><h3 class="cathead">'+label+
      '<span> · '+ms.length+'</span></h3>'+
      '<div class="grid">'+ms.map(memberCard).join('')+'</div></div>';
  };
  document.getElementById('list').innerHTML =
    section('Skills','skill') + section('Prompts','prompt') + section('Tools','tool');

  // Re-wire each rendered member exactly as its home tab would - same
  // star/tag/copy/group buttons, none of it duplicated here.
  wire();
  wirePromptCards();
  wireToolCards();
  document.querySelectorAll('[data-gremove-kind]').forEach(b=>b.onclick=()=>
    toggleGroupMember(S.groupOpen, b.dataset.gremoveKind, b.dataset.gremoveName, false));
}

function draw(){
  if (S.section === 'programs'){ drawPrograms(); return; }
  if (S.section === 'prompts'){ drawPrompts(); return; }
  if (S.section === 'tools'){ drawTools(); return; }
  if (S.section === 'hooks'){ drawHooks(); return; }
  if (S.section === 'groups'){ drawGroups(); return; }
  sidebar(); tagbar();
  secHeads('State', 'Origin', 'Location', 'Checks');

  // Say it once, loudly, at the top - not 173 times on hover.
  document.getElementById('offline').innerHTML = P.live ? '' :
    '<b>Read-only.</b> This page was opened as a file, so nothing can write: '+
    'every Auto/Manual switch and every ★ is inert, including the ones that '+
    'work normally. Nothing is broken. Close this tab and run '+
    '<code>python serve.py</code>, then use <code>http://127.0.0.1:'+(P.port||8712)+'/</code>.';

  // Every check below reads the POOL, not ITEMS. A "Never fired" count taken
  // over the whole inventory while the page renders one mode's worth is the
  // sidebar-says-174-list-shows-33 bug, and it is invisible until someone adds
  // the numbers up.
  const pool = POOL();
  const reachable = pool.filter(i=>i.reach!=='off');
  document.getElementById('c-never').textContent = reachable.filter(i=>!i.used).length;
  document.getElementById('c-sleep').textContent =
    pool.filter(i=>i.reach==='auto'&&!i.used&&!i.locked).length;
  document.getElementById('nav-never').classList.toggle('on', S.view==='never');
  document.getElementById('nav-sleep').classList.toggle('on', S.view==='sleep');
  document.getElementById('c-flags').textContent = pool.filter(i=>i.flags.length).length;
  document.getElementById('c-rec').textContent = P.notion.available
    ? (P.notion.missing_row.length + P.notion.missing_file.length) : '–';
  document.getElementById('nav-flags').classList.toggle('on', S.view==='flags');
  document.getElementById('nav-rec').classList.toggle('on', S.view==='rec');

  if (S.view){ document.getElementById('bar').innerHTML = '<span style="color:var(--dim)">'+(S.view==='rec'?'Notion reconcile':'Health flags')+'</span>'; }
  if (S.view === 'rec'){ document.getElementById('list').innerHTML = reconcileView(); return; }
  if (S.view === 'never' || S.view === 'sleep'){
    const set = S.view==='never'
      ? reachable.filter(i=>!i.used)
      : pool.filter(i=>i.reach==='auto'&&!i.used&&!i.locked);
    // mode.py can only edit ~/.claude/skills — a repo skill runs from
    // whatever was uploaded to claude.ai, so the command must exclude them.
    const switchable = set.filter(i=>i.home==='machine');
    const repoOnly   = set.filter(i=>i.home!=='machine');
    document.getElementById('list').innerHTML =
      (S.view==='sleep'
        ? '<p style="color:var(--mute);font-size:13px;margin:0 0 14px;max-width:720px">'+
          'Auto, but never fired — so it is costing context every session for nothing. '+
          'Manual keeps it available as /name at zero cost, and is one command to undo.<br>'+
          '<code>python mode.py manual '+switchable.map(i=>i.name).join(' ')+'</code>'+
          (repoOnly.length ? '<br><br>'+repoOnly.length+' more are repo skills ('+
            repoOnly.map(i=>i.name).join(', ')+') — they run from what was uploaded to '+
            'claude.ai, so this command cannot reach them. Change them in the repo and re-upload.' : '')+
          '</p>'
        : '<p style="color:var(--mute);font-size:13px;margin:0 0 14px;max-width:720px">'+
          'Never invoked in '+P.usage.scanned+' transcripts. That is either a skill you have never needed, '+
          'or one whose description cannot match how you speak — use the phrase test to tell which.</p>')+
      '<div class="grid">'+set.map(card).join('')+'</div>';
    wire(); return;
  }
  if (S.view === 'flags'){
    document.getElementById('list').innerHTML = flagsView();
    wire(); return;
  }

  const f = S.q.trim().toLowerCase();
  const hits = pool.filter(i =>
    (!S.fav  || i.fav) &&
    (!S.tier || i.reach===S.tier) &&
    (!S.loc  || i.location===S.loc) &&
    (!S.origin || i.origin===S.origin) &&
    (!S.cat || i.cats.includes(S.cat)) &&
    (!S.tag  || tagsFor(i.name).includes(S.tag)) &&
    (!f || (i.name+' '+i.desc+' '+tagsFor(i.name).join(' ')).toLowerCase().includes(f))
  );

  const on = [S.fav && '★ Favourites', S.tier && TIERS[S.tier][0], S.loc,
             S.origin && ('origin: '+S.origin), S.cat, S.tag && ('#'+S.tag),
             S.q && ('"'+S.q+'"')].filter(Boolean);
  const scope = S.fav ? 'starred' : (S.mode==='cat' ? 'in Catalogue' : 'in Mine');
  document.getElementById('bar').innerHTML = on.length
    ? 'Showing <b>'+hits.length+'</b> of '+pool.length+' '+scope+' — filtered by '+
      on.map(x=>'<span class="fchip">'+esc(x)+'</span>').join('')+
      '<button class="btn" id="clr">Clear</button>'
    : '<span style="color:var(--dim)">'+
      (S.mode==='cat'
        ? 'Catalogue — '+pool.length+' skills in marketplaces you have not installed. '+
          'None of these can fire.'
        : 'Mine — all '+pool.length+' skills you have')+'</span>';
  const c = document.getElementById('clr');
  if (c) c.onclick = () => { S.tier=S.loc=S.tag=S.origin=S.cat=null; S.fav=false; S.q='';
    document.getElementById('q').value=''; draw(); };

  // A search that silently omits the other mode makes the hidden half a place
  // where things go to be forgotten - the exact problem hiding them was meant
  // to solve. Say what is over there and offer one click to it.
  let across = '';
  if (f && !S.fav){
    const other = ITEMS.filter(i => (S.mode==='cat' ? !isCat(i) : isCat(i)))
      .filter(i => (i.name+' '+i.desc+' '+tagsFor(i.name).join(' ')).toLowerCase().includes(f));
    if (other.length) across =
      '<p class="across">'+other.length+' more match "'+esc(S.q)+'" in '+
      (S.mode==='cat' ? 'Mine' : 'Catalogue')+
      ' <button class="btn" id="goother">Show</button></p>';
  }

  sortHits(hits);
  document.getElementById('list').innerHTML = (hits.length
    ? '<div class="grid">'+hits.map(card).join('')+'</div>'
    : (S.fav && !ITEMS.some(i=>i.fav)
        ? '<p class="empty">No favourites yet — click the ☆ on any card and it turns gold. '
          + 'They are saved to favourites.json, not to this browser.</p>'
        : '<p class="empty">Nothing matches'+(across?' here':'')+'.</p>')) + across;
  const go = document.getElementById('goother');
  if (go) go.onclick = () => {
    S.mode = S.mode==='cat' ? 'mine' : 'cat';
    S.tier=S.loc=S.tag=S.origin=S.cat=null; S.fav=false; S.view=null;
    draw();   // the query deliberately survives - it is what you came for
  };

  wire();
}

function wire(){
  const find = n => ITEMS.find(x=>x.name===n);
  document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>copy(invocation(find(b.dataset.copy)),b));
  document.querySelectorAll('[data-path]').forEach(b=>b.onclick=()=>copy(find(b.dataset.path).file,b));
  document.querySelectorAll('[data-sw]').forEach(e=>e.onclick=()=>flip(e, e.dataset.sw));
  document.querySelectorAll('[data-fav]').forEach(e=>e.onclick=()=>fav(e, e.dataset.fav));
  document.querySelectorAll('[data-tagbtn]').forEach(b=>b.onclick=()=>{
    const n=b.dataset.tagbtn, v=prompt('Tag for '+n+' (comma-separated):', tagsFor(n).join(', '));
    if(v===null) return;
    const list=v.split(',').map(s=>s.trim()).filter(Boolean);
    setTags(b, n, list);
  });
  document.querySelectorAll('[data-groupbtn-skill]').forEach(b=>b.onclick=()=>
    openGroupPicker('skill', b.dataset.groupbtnSkill));
  wireGroupChips();
  document.querySelectorAll('.card .ds').forEach(d=>d.onclick=()=>d.parentElement.classList.toggle('open'));
}

/* ---------- live: light up what is firing right now ----------
   serve.py tails the Claude Code transcripts. A skill invocation and a
   program run are both written there within a second of happening, so this
   polls for the new ones and glows the matching card.

   HONEST LIMITS, stated on the lamp's tooltip because a light that means less
   than it looks like is worse than no light:
     * programs are seen only when run THROUGH Claude Code - a .bat, a
       terminal or Task Scheduler writes nothing, so a weekly scheduled job
       will never light up;
     * hooks never light, because nothing names them when the harness fires
       them on an event;
     * prompts light on COPY, from the click itself, not from this feed - a
       prompt is used in another app where Kit cannot see it.

   Polling, not an event stream: HTTPServer serves one request at a time, so
   a long-lived connection would hold the only thread and hang every toggle. */
const LIT = new Map();          // "kind:name" -> when it last fired
const LIT_PATH = new Map();     // "kind:name" -> the path the command used
/* Tools with a live process RIGHT NOW. Refreshed from the same poll, but on
   the server's own slower timer - enumerating every process costs ~0.7s,
   which is fine every ten seconds and far too slow every two. */
let RUNNING = new Set();
/* Subset of RUNNING that running.CONNECT_CHECKS could actually verify - see
   the .connected CSS block for why "up" alone is not enough for these two. */
let CONNECTED = new Set();
const HOT_MS = 12000;           // bright pulse
const WARM_MS = 75000;          // fading "recently active"
let LIVE_OK = false, LIVE_FAILS = 0;

function markLive(kind, name){
  LIT.set(kind+':'+name, Date.now());
  paintLive();
}

const litState = (kind, name) => {
  const t = LIT.get(kind+':'+name);
  if (!t) return '';
  const d = Date.now() - t;
  return d < HOT_MS ? 'hot' : (d < WARM_MS ? 'warm' : '');
};

/* Repaint by TOUCHING CLASSES, never by redrawing. A full draw() every two
   seconds would fight the scroll position, collapse any card you had opened
   and steal focus from the search box - all to animate a border. */
/* A tool has no event of its own - nothing in a transcript ever says
   "my-tool ran". What CAN be seen is a program inside it running, and
   PROGS already carries the folder as `group`. So a tool lights when
   something in it fires, and the chip says which one.

   ⚠️ Hooks deliberately never light, and that is not the same as being
   forgotten. The harness fires a hook on an event and names it nowhere a
   scanner can read - heredoc_source_guard blocked a write during the session
   that built this tab and left no trace at all. A dark card here is the
   honest answer; the lamp says so too. */
function litToolFolders(){
  // Which folders could a given program name belong to? Four stems live in
  // more than one: crawl, feed and scan in two, `serve` in THREE.
  const homes = {};
  PROGS.forEach(p => { if (p.group) (homes[p.name] = homes[p.name] || []).push(p.group); });

  const out = new Map();
  Object.keys(homes).forEach(name => {
    const st = litState('program', name);
    if (!st) return;
    const groups = [...new Set(homes[name])];
    let group = groups[0];
    if (groups.length > 1){
      // ⚠️ Ambiguous. Resolve it from the path the command actually used, and
      // if that cannot, light NOTHING. Lighting all three would put a
      // confident wrong glow on two cards - the same failure as the
      // `hyperframes` icon, and precisely what the lamp promises not to do.
      const path = (LIT_PATH.get('program:' + name) || '').toLowerCase();
      const hit = groups.filter(g => path.includes('/' + g.toLowerCase() + '/'));
      if (hit.length !== 1) return;
      group = hit[0];
    }
    const prev = out.get(group);
    if (!prev || (prev.st === 'warm' && st === 'hot')) out.set(group, {st, via: name});
  });

  // A DIRECT launch beats anything inferred from a program. The .bat markers
  // are the only evidence that survives a desktop icon, and they say the tool
  // itself was started rather than that something inside it ran - so they
  // overwrite, and the dot says "you launched this".
  TOOLS.forEach(t => {
    const st = litState('tool', t.name);
    if (st) out.set(t.name, {st, via: null});
  });
  return out;
}

function paintLive(){
  if (S.section === 'hooks'){                     // see the note above
    const lampH = document.getElementById('lamp');
    if (lampH) lampH.classList.remove('fire');
    return;
  }
  if (S.section === 'tools'){
    const lit = litToolFolders();
    let hot = false;
    document.querySelectorAll('.card[data-tn]').forEach(el=>{
      const name = el.getAttribute('data-tn');
      const rec = lit.get(name);
      const st = rec ? rec.st : '';
      // Running is independent of the glow: a bridge up for six hours has no
      // recent event at all, and an event does not mean anything is still up.
      el.classList.toggle('up', RUNNING.has(name));
      el.classList.toggle('connected', CONNECTED.has(name));
      el.classList.toggle('hot',  st==='hot');
      el.classList.toggle('warm', st==='warm');
      if (st==='hot') hot = true;
      // Sync the badge itself, not just the border - a poll that only
      // toggled classes left the text reading "running" for as long as ten
      // seconds after a real connection, which looked like nothing changed.
      const wantConn = CONNECTED.has(name), wantUp = RUNNING.has(name);
      const oldBadge = el.querySelector('.upbadge, .connbadge');
      const curConn = !!oldBadge && oldBadge.classList.contains('connbadge');
      const curUp = !!oldBadge && oldBadge.classList.contains('upbadge');
      if (wantConn !== curConn || wantUp !== curUp){
        if (oldBadge) oldBadge.remove();
        const html = toolBadge(name);
        if (html) el.querySelector('.nm').insertAdjacentHTML('afterend', html);
      }
      const has = el.querySelector('.livedot');
      if (st && !has) el.querySelector('.nm').insertAdjacentHTML(
        'afterend', '<span class="livedot" title="'+
        (rec.via ? esc(rec.via)+'.py just ran in this folder'
                 : 'You launched this — the .bat left a marker')+'"></span>');
      if (!st && has) has.remove();
    });
    const lampT = document.getElementById('lamp');
    if (lampT) lampT.classList.toggle('fire', hot);
    return;
  }
  const key = S.section==='programs' ? 'data-n'
            : S.section==='prompts'  ? 'data-rn' : 'data-n';
  const kind = S.section==='programs' ? 'program'
             : S.section==='prompts'  ? 'prompt' : 'skill';
  let anyHot = false;
  document.querySelectorAll('.card['+key+']').forEach(el=>{
    const st = litState(kind, el.getAttribute(key));
    el.classList.toggle('hot',  st==='hot');
    el.classList.toggle('warm', st==='warm');
    if (st==='hot') anyHot = true;
    const has = el.querySelector('.livedot');
    if (st && !has) el.querySelector('.nm')
      .insertAdjacentHTML('afterend','<span class="livedot"></span>');
    if (!st && has) has.remove();
  });
  const lamp = document.getElementById('lamp');
  if (lamp) lamp.classList.toggle('fire', anyHot);
}

function lampState(){
  const lamp = document.getElementById('lamp');
  if (!lamp) return;
  lamp.classList.toggle('on', LIVE_OK);
  lamp.querySelector('.t').textContent = LIVE_OK ? 'live' : 'live off';
  lamp.title = LIVE_OK
    ? 'Watching your Claude Code transcripts. A skill firing or a program run '+
      'lights its card within a couple of seconds.\n\n'+
      'Tools light when you START them — the .bat launchers leave a marker, '+
      'so a desktop icon works. They also light when a Python program inside '+
      'them runs.\n\n'+
      'What it CANNOT see:\n'+
      '• Programs, unless run through Claude Code. A terminal or Task '+
      'Scheduler leaves nothing to read — a weekly scheduled job will never '+
      'light.\n'+
      '• Hooks. The harness records only its Stop hooks, and those fire on '+
      'every single turn, so a light would mean "this is a Stop hook" rather '+
      'than "this just did something". Hooks that gate tool calls are never '+
      'recorded at all. Both are filtered out deliberately.\n'+
      '• Prompts. They are used off this machine, so the copy is counted '+
      'instead.\n\n'+
      'A tool lights when a Python program INSIDE it runs, because nothing '+
      'ever names the folder itself.'
    : 'Not watching. Run python serve.py and open http://127.0.0.1:'+(P.port||8712)+'/ '+
      'rather than as a file, and this lights up.';
}

async function pollLive(){
  if (!P.live) return;
  if (document.hidden) return;              // no point animating a hidden tab
  try{
    const r = await fetch('/api/live', {cache:'no-store'});
    const j = await r.json();
    LIVE_FAILS = 0;
    if (!LIVE_OK){ LIVE_OK = true; lampState(); }
    // Translate the server's clock to this one rather than stamping Date.now().
    // The server tails on its own timer, so an event can legitimately be 40
    // seconds old by the time a hidden tab comes back and asks - and calling
    // that "now" is exactly the lie this whole feature has to avoid.
    const skew = Date.now() - (j.at||0)*1000;
    (j.events||[]).forEach(e => {
      const when = e.at*1000 + skew;
      const k = e.kind+':'+e.name;
      if (when > (LIT.get(k)||0)){
        LIT.set(k, when);
        // Only the Tools tab needs this, and only to disambiguate a stem that
        // lives in more than one folder. Stored beside the timestamp so it
        // cannot go stale relative to it.
        if (e.path) LIT_PATH.set(k, e.path);
      }
    });
    if (Array.isArray(j.running)) RUNNING = new Set(j.running);
    if (Array.isArray(j.connected)) CONNECTED = new Set(j.connected);
    paintLive();
  }catch(e){
    // Never nag. The server going away mid-session is a dead lamp, not a
    // dialog box on top of an inventory that still works perfectly.
    if (++LIVE_FAILS >= 3 && LIVE_OK){ LIVE_OK = false; lampState(); }
  }
}

if (P.live){
  lampState();
  pollLive();
  setInterval(pollLive, 2000);
  // Expire the glow even when nothing new arrives, or a card that fired once
  // stays lit forever and the light stops meaning "recently".
  setInterval(paintLive, 3000);
  document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) pollLive(); });
} else {
  lampState();
}

/* A blank page tells you nothing. If rendering ever throws, say so. */
const _draw = draw;
draw = function(){
  try { _draw(); paintLive(); }
  catch(e){
    document.getElementById('list').innerHTML =
      '<p class="empty">Render failed: '+esc(e.message)+
      '<br><br>The scan itself is fine — '+ITEMS.length+' skills are loaded. '+
      'This is a display bug.</p>';
  }
};

document.getElementById('nav-flags').onclick=()=>{S.view=S.view==='flags'?null:'flags'; draw()};
document.getElementById('nav-never').onclick=()=>{S.view=S.view==='never'?null:'never'; draw()};
document.getElementById('nav-sleep').onclick=()=>{S.view=S.view==='sleep'?null:'sleep'; draw()};
document.getElementById('nav-rec').onclick=()=>{S.view=S.view==='rec'?null:'rec'; draw()};
document.getElementById('q').addEventListener('input', e=>{S.q=e.target.value; S.view=null; draw()});
document.getElementById('pt').onclick = e => {
  const p=document.getElementById('phrase'); p.classList.toggle('show');
  e.target.classList.toggle('on'); if(p.classList.contains('show')) document.getElementById('pin').focus();
};
document.getElementById('pin').addEventListener('input', e=>phraseTest(e.target.value));
function pref(k, d){ try { return localStorage.getItem(k) ?? d; } catch(e) { return d; } }
function setPref(k, v){ try { localStorage.setItem(k, v); } catch(e) {} }

const listEl = document.querySelector('.scroll');
function applyView(v){
  listEl.classList.toggle('list', v === 'list');
  document.getElementById('vw').textContent = v === 'list' ? 'Cards' : 'List';
}
function applyDen(n){ listEl.setAttribute('data-den', n); document.getElementById('den').value = n; }

applyView(pref('skills-launcher-view', 'cards'));
applyDen(pref('skills-launcher-den', '3'));

S.sort  = pref('skills-launcher-sort', 'name');
S.psort = pref('skills-launcher-psort', 'name');
S.rsort = pref('skills-launcher-rsort', 'name');
fillSort();

document.getElementById('srt').onchange = e => {
  // Each section remembers its own sort. Sharing one would mean switching to
  // Programs silently reset the skills sort, and switching back showed a list
  // ordered by something the dropdown no longer offers.
  const k = sortKey();
  S[k] = e.target.value;
  setPref('skills-launcher-' + k, S[k]);
  draw();
};
document.getElementById('cat').onchange = e => {
  S.cat = e.target.value || null; S.view = null; draw();
};

document.getElementById('vw').onclick = () => {
  const v = listEl.classList.contains('list') ? 'cards' : 'list';
  applyView(v); setPref('skills-launcher-view', v);
};
document.getElementById('den').oninput = e => {
  applyDen(e.target.value); setPref('skills-launcher-den', e.target.value);
};

document.getElementById('th').onclick = () => {
  const t = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  applyTheme(t); thSet(t);
};
document.getElementById('ex').onclick = e => {
  copy(JSON.stringify(TAGS,null,2), e.target);
  alert('Tags copied to clipboard. Note: tags now save to tags.json automatically '+
        'as you add them (while the server is running) - this is just a backup copy.');
};
document.getElementById('modal-backdrop').onclick = e => {
  if (e.target.id === 'modal-backdrop') closeModal();
};
document.addEventListener('keydown', e=>{
  if (e.key==='Escape' && !document.getElementById('modal-backdrop').hidden){
    closeModal(); return;
  }
  const q=document.getElementById('q');
  if(e.key==='/' && document.activeElement!==q){e.preventDefault(); q.focus();}
  if(e.key==='Escape'){q.value=''; S.q=''; S.tier=S.loc=S.tag=S.origin=S.cat=S.view=null; draw(); q.focus();}
});
draw();
</script></body></html>
"""


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        input("Press Enter to close…")
        sys.exit(1)
