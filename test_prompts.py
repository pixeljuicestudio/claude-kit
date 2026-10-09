#!/usr/bin/env python3
"""Selftest for prompts.py - the file format, the Notion parser and the
category lookup.

The local .md format is the primary source: frontmatter + body, where the body
is exactly what Copy copies. The Notion parser is the risky optional half: it
pulls the copyable prompt out of a Notion page, and a real library used FOUR
different page shapes, none of them declared anywhere. Getting it wrong is not
cosmetic: the first version shipped the trailing "Why it worked: describes
only the delta..." commentary inside the copy payload, so pasting that prompt
would have handed the model an explanation of itself.

The category rules are ORDER-DEPENDENT - a prompt can carry both "character
reference" and "video", and only the order of the rules decides whether it
files as an image or as the video it actually produces. An order change would
be silent without this.

Run: python test_prompts.py      Exit 0 = every invariant holds.
"""
import collections
import inspect
import os
import sys
import tempfile
from pathlib import Path

import prompts

FAILS = []


def check(label, ok, detail=""):
    print("%-5s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILS.append("%s: %s" % (label, detail))


# ------------------------------------------------------------------ shapes
# Shape 1: an explicit '## The Prompt' heading, commentary after it.
body, how = prompts.extract_prompt([
    "A post-answer reality check.",
    "## The Prompt",
    "List every assumption you made.",
    "## Why it works",
    "Most plans die on hidden assumptions.",
])
check("section shape found", how == "section", how)
check("section takes only the prompt", body == "List every assumption you made.", body)
check("section drops the intro line", "reality check" not in body, body)
check("section drops the commentary", "hidden assumptions" not in body, body)

# Shape 2: a plain 'Prompt:' paragraph, with a 'Why it worked:' tail. THE
# regression - this used to fall through to the whole-page reading and carry
# the tail into the clipboard.
body, how = prompts.extract_prompt([
    "Prompt:",
    "Animate @image_1 into a live-action video.",
    "Why it worked: describes only the delta so the model doesn't reinterpret.",
])
check("labelled shape found", how == "labelled", how)
check("labelled drops the 'Why it worked' tail",
      "Why it worked" not in body and "describes only" not in body, body)
check("labelled keeps the prompt", body.startswith("Animate @image_1"), body)

# Same label with the text on the same line.
body, how = prompts.extract_prompt(["Prompt: Do the thing.", "Notes: not this."])
check("labelled reads same-line text", body == "Do the thing.", body)

# The prompt does not always live under a heading called "Prompt". A real page
# filed its under '## The template', and matching only "prompt" made that page
# fall through to the fallback below.
body, how = prompts.extract_prompt([
    "Purpose. A reusable image-prompt template.",
    "## The template (fill [BRACKETS], keep the rest fixed)",
    "You are a professional storyboard artist.",
    "## Notes",
    "- Swap STYLE per beat.",
])
check("'The template' counts as a prompt heading", how == "section", how)
check("template section takes the template, not the preamble",
      body == "You are a professional storyboard artist.", body)

# THE REGRESSION THIS FILE EXISTS FOR. Headings present, none of them names
# the prompt. Cutting at the first heading returns the preamble - a silent
# wrong answer that renders perfectly and copies an explanation instead of a
# prompt. It must take everything instead.
body, how = prompts.extract_prompt([
    "Purpose. Some preamble.",
    "## Shot list",
    "0.0s-1.5s: static low angle.",
    "## Locks",
    "Outfit stays consistent.",
])
check("unmarked shape is reported, not guessed at", how == "unmarked", how)
check("unmarked never returns just the preamble",
      "0.0s-1.5s" in body and "Outfit stays consistent" in body, body)
check("unmarked keeps the preamble too (lossless)", "Some preamble" in body, body)

# Shape 3: no marker at all - the whole page IS the prompt, after a credit.
body, how = prompts.extract_prompt([
    "Source: https://example.com/reel",
    "SCENE CONTEXT",
    "In broad daylight, he kicks the doors open.",
])
check("whole-page shape found", how == "whole page", how)
check("whole page drops the credit line", "example.com" not in body, body)
check("whole page keeps everything else",
      "SCENE CONTEXT" in body and "broad daylight" in body, body)

check("empty page reports empty", prompts.extract_prompt([])[1] == "empty")

# ------------------------------------------------------------- category rules
# Order is the rule, so assert the case that DEPENDS on it. This prompt makes a
# video sequence and uses a character reference as the technique; if the
# image/reference rule ever moves above the video rule it silently misfiles.
check("video outranks character-reference",
      prompts.category_for(["Seedance", "Image-to-Video",
                            "Character Reference", "Video"]) == "Video generation",
      prompts.category_for(["Character Reference", "Video"]))
check("a real reference sheet still files as one",
      prompts.category_for(["Character Reference", "Image Generation"]) == "Reference sheets")
check("a plain image prompt files as an image",
      prompts.category_for(["Image Generation", "Higgsfield"]) == "Image generation")
check("critique outranks everything",
      prompts.category_for(["video", "critique"]) == "Thinking & critique")
check("tags match case-insensitively",
      prompts.category_for(["CODING"]) == "Coding")
check("no tags means Unsorted, not a guess",
      prompts.category_for([]) == "Unsorted")
check("a platform alone decides no category",
      prompts.category_for(["Midjourney"]) == "Unsorted",
      "platform is not an output type")
check("tool is read separately from category",
      prompts.tool_for(["Midjourney", "Image"]) == "Midjourney")
check("an explicit platform field wins over tags",
      prompts.tool_for(["Midjourney"], "ChatGPT") == "ChatGPT")
check("no platform tag means no tool", prompts.tool_for(["critique"]) is None)

# ------------------------------------------------------------- the file format
fm, body = prompts.parse_prompt_file(
    "---\nname: 'Quoted name'\ntags: [a, b , c]\nplatform: Claude\n"
    "for: When stuck.\n---\nLine one.\n\nLine two.\n")
check("frontmatter name unquoted", fm.get("name") == "Quoted name", fm)
check("inline tag list parsed", prompts._list(fm.get("tags")) == ["a", "b", "c"], fm)
check("body is everything after the frontmatter",
      body == "Line one.\n\nLine two.", repr(body))
fm, _ = prompts.parse_prompt_file("---\ntags:\n  - x\n  - y\n---\nz")
check("YAML list tags parsed", prompts._list(fm.get("tags")) == ["x", "y"], fm)
check("comma-string tags parsed", prompts._list("one, two") == ["one", "two"])
fm, body = prompts.parse_prompt_file("No frontmatter at all.\n")
check("a file with no frontmatter is all body",
      fm == {} and body == "No frontmatter at all.", (fm, body))
fm, body = prompts.parse_prompt_file("---\nname: x\nnever closed\n")
check("an unterminated block is treated as body, not swallowed",
      fm == {} and "never closed" in body, (fm, body))

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    (d / "README.md").write_text("# not a prompt\n", encoding="utf-8")
    (d / "my-first-prompt.md").write_text(
        "---\ntags: writing\nurl: https://example.com/p\n---\nWrite it.\n", encoding="utf-8")
    (d / "notes.txt").write_text("ignored", encoding="utf-8")
    got = prompts.local_prompts(d)
    check("only .md files, README excluded", [g["filename"] for g in got] == ["my-first-prompt.md"],
          [g["filename"] for g in got])
    g = got[0] if got else {}
    check("name defaults from the filename", g.get("name") == "My first prompt", g.get("name"))
    check("local prompt categorised from its tags", g.get("cat") == "Writing", g.get("cat"))
    check("local prompt body", g.get("body") == "Write it.", g.get("body"))
    check("url doubles as the source link", g.get("ref") == "https://example.com/p", g.get("ref"))
    check("created/edited are ISO strings",
          str(g.get("created", "")).endswith("Z") and str(g.get("edited", "")).endswith("Z"))
    check("the full card shape is present",
          all(k in g for k in ("name", "for", "origin", "ref", "tags", "cat", "tool", "body",
                               "body_from", "words", "tokens", "url", "created",
                               "edited", "flags")), sorted(g))
# A local file with a '## The Prompt' section: the setup above and the
# '## Notes' below must stay OUT of the copy payload, and the blank lines
# inside the prompt must survive (it pastes exactly as written).
body, how = prompts.local_body(
    "Setup: attach the image first.\n\n## The Prompt\n\nPara one.\n\nPara two.\n\n"
    "## Notes\n\n- why it works\n")
check("local section shape found", how == "section", how)
check("local section keeps only the prompt, blank lines intact",
      body == "Para one.\n\nPara two.", repr(body))
body, how = prompts.local_body("## The template\n\nFill [X].\n")
check("'## The template' counts locally too", (body, how) == ("Fill [X].", "section"))
body, how = prompts.local_body("Just a prompt.\n\nWith two paragraphs.")
check("no section heading means the whole body",
      (body, how) == ("Just a prompt.\n\nWith two paragraphs.", "file"), (body, how))

check("a missing prompts folder is an empty list, not a crash",
      prompts.local_prompts(Path(tempfile.gettempdir()) / "kit-no-such-dir-xyz") == [])

# Every category a rule can produce must be in CATEGORIES, or the sidebar
# builds a facet the dropdown filter list has never heard of.
produced = {name for name, _ in prompts.CATEGORY_RULES}
check("every rule's category is declared",
      produced <= set(prompts.CATEGORIES), repr(produced - set(prompts.CATEGORIES)))

# -------------------------------------------------------------- the live cache
items, meta = prompts.scan()
if not items:
    check("the prompts folder has prompts", False,
          "prompts/example-pre-mortem.md should always be there")
else:
    check("cache has prompts", len(items) > 0)
    check("every prompt has a name", all(i.get("name") for i in items))
    check("every prompt has a category",
          all(i.get("cat") for i in items),
          repr([i["name"] for i in items if not i.get("cat")][:3]))
    check("every category is a declared one",
          all(i["cat"] in prompts.CATEGORIES + ["Unsorted"] for i in items),
          repr({i["cat"] for i in items} - set(prompts.CATEGORIES + ["Unsorted"])))

    # The copy payload must never carry the commentary. This is the shipped
    # form of the labelled-shape regression above.
    leaks = [i["name"] for i in items if "why it work" in i["body"].lower()
             or "## notes" in i["body"].lower()]
    check("no commentary leaked into any body", not leaks, repr(leaks[:3]))

    bodyless = [i["name"] for i in items if not i["body"]]
    check("every prompt has a body", not bodyless, repr(bodyless[:3]))

    # A body_from value the card has no label for renders as a raw token.
    known = {"section", "labelled", "whole page", "unmarked", "empty", "file"}
    odd = {i["body_from"] for i in items} - known
    check("every body_from has a card label", not odd, repr(odd))

    # An unmarked page must warn on the card, or its Copy button quietly
    # hands over several sections as if they were one prompt.
    unflagged = [i["name"] for i in items
                 if i["body_from"] == "unmarked" and not i["flags"]]
    check("every unmarked page carries a flag", not unflagged, repr(unflagged))

    # Names are the key for favourites AND for the copy counter. A duplicate
    # means starring one stars both and the count is shared - the same trap
    # load_favs() documents for skills.
    dupes = [n for n, c in collections.Counter(i["name"] for i in items).items() if c > 1]
    check("prompt names are unique", not dupes, repr(dupes))

    check("meta count matches the list", meta["count"] == len(items))
    check("the shipped example prompt is there",
          any(i.get("filename") == "example-pre-mortem.md" for i in items))

# ------------------------------------------------------------- references
# A prompt's reference video. ONE field (the source URL) serves both halves,
# because a downloader that puts the post id in every filename makes the id
# the join - so the local copy is found by id, never by matching titles,
# which are social-post text full of emoji. Exercised against a temp folder.
_saved_ref = prompts.REFERENCE
with tempfile.TemporaryDirectory() as td:
    prompts.REFERENCE = Path(td)
    (Path(td) / "Some reel title [2145907529671691].mp4").write_bytes(b"")
    (Path(td) / "A short [dcvC9wBdHO4].mp4").write_bytes(b"")
    check("a facebook reel id resolves to the local file",
          (prompts.local_reference("https://www.facebook.com/reel/2145907529671691") or "")
          .endswith("[2145907529671691].mp4"))
    check("a youtube shorts id resolves too",
          (prompts.local_reference("https://www.youtube.com/shorts/dcvC9wBdHO4") or "")
          .endswith("[dcvC9wBdHO4].mp4"))
    check("an id with no file on disk returns None",
          prompts.local_reference("https://www.facebook.com/reel/9999999999999") is None)
    check("a url with no id at all returns None",
          prompts.local_reference("https://example.com/nothing") is None)
    check("an empty reference returns None", prompts.local_reference("") is None)
prompts.REFERENCE = None
check("no reference_dir configured means no local copy, not a crash",
      prompts.local_reference("https://www.facebook.com/reel/2145907529671691") is None)
prompts.REFERENCE = _saved_ref

# ⚠️ flatten() had NO branch for a url property, so Reference came back None and
# read as "not set" - indistinguishable from genuinely empty, so the feature
# would have looked built and done nothing. Caught only because the export
# reported ZERO prompts with a Reference straight after one had been set.
check("flatten reads a url property",
      prompts.flatten({"type": "url", "url": "https://x/y"}) == "https://x/y")
check("flatten reads an empty url as None",
      prompts.flatten({"type": "url", "url": None}) is None)
check("flatten still reads rich_text",
      prompts.flatten({"type": "rich_text",
                       "rich_text": [{"plain_text": "hi"}]}) == "hi")

# ---------------------------------------------------------------- wiring
# The export refuses to overwrite a good cache with a broken pull. Deleting
# that guard would leave every test above passing, because they read the cache
# that a previous good run wrote.
src = inspect.getsource(prompts.export)
check("export still guards against a bodyless pull",
      "refusing to write" in src and "0.6" in src)
check("export still writes atomically",
      "os.replace" in src and ".tmp" in src)
# scan() must never reach the network - the page has to build with Notion down.
scan_src = inspect.getsource(prompts.scan)
check("scan() does not call the API", "api(" not in scan_src and "urllib" not in scan_src)
check("scan() reads the local folder itself", "local_prompts(" in scan_src)
# The token comes from the environment and nowhere else.
tsrc = inspect.getsource(prompts.token)
check("the Notion token is read from the environment only",
      "notion_token()" in tsrc and "read_text" not in tsrc)
# The reference must be resolved at SCAN time. A cache can be days old while
# the file was downloaded five minutes ago, and a card claiming a local copy
# that is not there is worse than one offering only the link.
check("scan() resolves the local reference itself", "local_reference(" in scan_src)

import build
import serve as _serve
ssrc = inspect.getsource(_serve.Handler)
check("serve.py routes /api/openref", '"/api/openref"' in ssrc)
# ⚠️ The client posts a NAME; the server resolves the path. A path sent by a
# page is a path anything could have chosen, and os.startfile on an arbitrary
# path is arbitrary execution.
osrc = inspect.getsource(_serve.Handler._openref)
check("openref takes a name, never a path from the request",
      '["name"]' in osrc and 'req["path"]' not in osrc)
check("openref re-checks the file is inside the reference folder",
      "is_relative_to" in osrc)
bsrc2 = inspect.getsource(build)
check("the card offers the local copy through the server",
      "data-rref" in bsrc2 and "/api/openref" in bsrc2)
check("the card also links the original post", "data-rn" in bsrc2 and "p.ref" in bsrc2)

print("\n=== SELFTEST COMPLETE - %d failure(s) ===" % len(FAILS))
for f in FAILS:
    print("  " + f)
sys.exit(1 if FAILS else 0)
