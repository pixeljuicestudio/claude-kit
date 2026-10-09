#!/usr/bin/env python3
"""
Categories — eight buckets for skills, inferred by keyword.

The eight categories came from a hand-labelled skills index. If you keep one
too, export it as notion_rows.json (see README, "Categories") with
a `category` per row, and those labels become authoritative: the launcher and
your index then sort into the same buckets, and a skill does not change
category by being looked at in a different place.

Keyword inference runs for every skill without a labelled row. Inference is a
guess and is labelled as such in the UI ("?"), because a wrong category that
looks authoritative is worse than a visible "unsorted".

    python categorise.py            accuracy against the labelled rows, if any
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Your eight, in the order the UI lists them.
CATEGORIES = [
    "AI image & video",
    "Video craft & editing",
    "Audio",
    "Design",
    "Content & writing",
    "Engine & build",
    "Business",
    "System & ops",
]

# Weighted keywords. Ordered most-specific first within each list: a term that
# only ever means one thing (seedance, lottie) outranks one that appears
# everywhere (design, video). Weights matter because most descriptions hit
# several categories and the winner should be the distinctive match.
SIGNALS: dict[str, list[tuple[str, int]]] = {
    "AI image & video": [
        ("seedance", 5), ("higgsfield", 5), ("midjourney", 5), ("nano banana", 5),
        ("diffusion", 4), ("img2vid", 4), ("image model", 3), ("ai-generated", 3),
        ("generate a video", 3), ("generate image", 3), ("upscal", 2), ("topaz", 3),
        ("prompt for", 2), ("text-to-image", 4), ("render looks", 2),
    ],
    "Video craft & editing": [
        ("davinci", 5), ("premiere", 5), ("timeline", 3), ("footage", 3),
        ("shot list", 3), ("shotlist", 3), ("cut", 2), ("clip", 2), ("subtitle", 3),
        ("caption", 2), ("reel", 2), ("frame rate", 3), ("edit", 1), ("montage", 3),
    ],
    "Audio": [
        ("audio", 3), ("voiceover", 4), ("transcribe", 4), ("transcription", 4), ("transcript of", 3), ("music", 3), ("sound", 2),
        ("loudness", 4), ("mix", 2), ("speech", 3), ("tts", 4), ("podcast", 3),
    ],
    "Design": [
        ("typograph", 5), ("wireframe", 5), ("mockup", 4), ("palette", 4),
        ("visual hierarchy", 5), ("spacing", 4), ("design system", 5), ("brand", 3),
        ("layout", 3), ("colour", 2), ("color", 2), ("ux", 3), ("ui ", 2),
        ("icon", 3), ("grid", 2), ("accessib", 3), ("critique", 3), ("aesthetic", 3),
        # UX research and product-design vocabulary. The designer-skills pack
        # is 96 of the 142 borrowed skills and is almost entirely this, so
        # without these the biggest single group lands in Unsorted.
        ("usability", 5), ("user research", 5), ("journey map", 5), ("empathy map", 5),
        ("affinity", 5), ("card sort", 5), ("service blueprint", 5), ("heuristic", 5),
        ("information architecture", 5), ("user flow", 5), ("prototyp", 4),
        ("interaction", 4), ("onboarding", 4), ("navigation", 3), ("form design", 5),
        ("design token", 5), ("dark mode", 4), ("motion system", 5), ("screen", 2),
        ("interface", 3), ("figma", 4), ("diary study", 5), ("usability test", 5),
        ("design critique", 5), ("visual", 2), ("legib", 4), ("readab", 3),
    ],
    "Content & writing": [
        ("copywriting", 5), ("caption", 2), ("script", 2), ("article", 3),
        ("prose", 3), ("headline", 3), ("write", 2), ("rewrit", 2), ("document", 2),
        ("narrative", 3), ("interview", 3), ("summar", 2),
    ],
    "Engine & build": [
        ("remotion", 5), ("react", 4), ("typescript", 4), ("mcp server", 5),
        ("plugin", 3), ("repository", 3), ("codebase", 4), ("api", 2), ("deploy", 3),
        ("component", 3), ("framework", 3), ("build", 2), ("css", 3), ("lint", 3),
        ("test suite", 4), ("refactor", 4), ("git", 2),
    ],
    "Business": [
        ("prospect", 5), ("lead gen", 5), ("apollo", 4), ("stakeholder", 4),
        ("revenue", 4), ("pricing", 3), ("roadmap", 3), ("client", 2),
        ("go-to-market", 5), ("persona", 3), ("competitive", 3), ("metric", 2),
    ],
    "System & ops": [
        ("settings.json", 5), ("hook", 3), ("permission", 4), ("session", 2),
        ("launcher", 4), ("install", 3), ("config", 3), ("cron", 4), ("schedule", 3),
        ("health", 3), ("workflow", 2), ("troubleshoot", 4), ("audit", 2),
    ],
}


# Keywords match on a word boundary, never as a bare substring. Without this,
# "tts" matched inside "Fitts's Law" and filed a Design skill under Audio, and
# "transcri" matched "session transcripts" - text about logs, not about sound.
# Stems still work: the boundary is only required at the START of the keyword.
_COMPILED: dict[str, list[tuple]] = {
    cat: [(re.compile(r"(?<![a-z0-9])" + re.escape(kw)), w) for kw, w in kws]
    for cat, kws in SIGNALS.items()
}


def infer(name: str, desc: str) -> tuple[str, int]:
    """Best-guess category and the score behind it. Score 0 means no signal."""
    hay = (name.replace("-", " ") + " " + desc).lower()
    best, best_score = "Unsorted", 0
    for cat in CATEGORIES:                       # stable order breaks ties
        score = sum(w for rx, w in _COMPILED[cat] if rx.search(hay))
        if score > best_score:
            best, best_score = cat, score
    return best, best_score


def notion_categories() -> dict[str, list[str]]:
    """{slug: [category, ...]} from the exported grid rows."""
    try:
        rows = json.loads((HERE / "notion_rows.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    from build import row_key
    out = {}
    for r in rows:
        cats = r.get("category")
        if isinstance(cats, str):
            try:
                cats = json.loads(cats)
            except ValueError:
                cats = [cats]
        if cats:
            out[row_key(r["name"])] = cats
    return out


def family_parent(name: str, names: set[str]) -> str | None:
    """'hyperframes-cli' -> 'hyperframes'. A module belongs where its parent does."""
    return next((p for p in names if p != name and name.startswith(p + "-")), None)


def assign(items: list[dict]) -> None:
    """Attach `cats` and `cat_guessed` to every item, in place."""
    known = notion_categories()
    from build import row_key
    names = {i["name"] for i in items}

    for i in items:
        cats = known.get(row_key(i["name"]))
        if cats:
            i["cats"], i["cat_guessed"] = cats, False
            continue
        # A family module inherits its parent's category before falling back to
        # keywords - "hyperframes-cli" reads as Audio to a keyword matcher
        # because its body is full of ffmpeg, which is true and irrelevant.
        parent = family_parent(i["name"], names)
        inherited = known.get(row_key(parent)) if parent else None
        if inherited:
            i["cats"], i["cat_guessed"] = inherited, True
            continue
        guess, score = infer(i["name"], i["desc"])
        i["cats"] = [guess] if score else ["Unsorted"]
        i["cat_guessed"] = True


if __name__ == "__main__":
    # Honest accuracy check: run inference on the skills Notion HAS labelled
    # and see how often it agrees. A classifier nobody scored is a classifier
    # nobody should trust.
    import scan
    items, _ = scan.scan()
    known = notion_categories()
    from build import row_key

    hits = misses = 0
    wrong = []
    for i in items:
        truth = known.get(row_key(i["name"]))
        if not truth:
            continue
        guess, score = infer(i["name"], i["desc"])
        if guess in truth:
            hits += 1
        else:
            misses += 1
            wrong.append((i["name"], guess, truth))

    total = hits + misses
    print(f"agreement with Notion: {hits}/{total} ({100*hits//max(total,1)}%)\n")
    for n, g, t in wrong:
        print(f"  {n:<28} guessed {g!r}, Notion says {t}")
