# prompts/

Every `.md` file in this folder (except this README) is one prompt in Kit's
**Prompts** tab. Add a file, reload the page, and it is there. No build step,
no database.

## Format

```markdown
---
name: Pre-mortem
tags: [thinking, planning]
platform: Claude
for: Before you commit to a plan, find out how it fails.
origin: Where it came from (a person, a post, "written in-house")
url: https://example.com/the-original-post
---
The prompt text itself goes here. Everything below the second `---`
is what the **Copy prompt** button copies, exactly as written.
```

| Field | Required | What it does |
|---|---|---|
| `name` | no | Card title. Defaults to the filename (`my-prompt.md` -> "My prompt"). Names must be unique: stars, tags and copy counts are keyed by name. |
| `tags` | no | `[a, b]`, `a, b`, or a YAML list. Drives the **category** (see below) and the tag filter. |
| `platform` (or `tool`) | no | Where you paste it: Claude, ChatGPT, Midjourney... Shown as its own facet. If absent, a platform name among the tags is used. |
| `for` | no | One line: when to reach for this prompt. Cards without one are marked. |
| `origin` | no | Provenance. Shown as a chip and as the Origin facet. |
| `url` | no | A link back to the source. Shown as a **Source** button. |

The parser is deliberately small: `key: value` lines, quoted values, inline
lists, and `- item` lists. It is not a full YAML parser.

## Setup and notes around the prompt

If the body has a `## The Prompt` (or `## The template`) heading, only that
section is the copyable prompt. Anything above it (how to use it, what to
attach) and anything from the next heading on (`## Notes`, `## Variations`)
shows on the card and is searchable, but stays out of the copy payload:

```markdown
---
name: Character turnaround
tags: Character Reference, Image Generation
tool: Midjourney
---
Attach one reference image first.

## The Prompt

Create a professional character reference sheet...

## Notes

- Why it works, variations, gotchas.
```

Without such a heading, the whole body is the prompt.

## Categories

Categories are a lookup from your tags, never a guess from the text. The first
rule that matches wins (case-insensitive):

| Category | Any of these tags |
|---|---|
| Thinking & critique | critique, thinking, review, pre-mortem, decision, planning, strategy |
| Video generation | video, image-to-video, text-to-video, animation |
| Reference sheets | character reference, vessel reference, product reference, reference sheet, turnaround |
| Image generation | image, image generation, text-to-image, photo |
| Coding | code, coding, debugging, refactor, testing, programming |
| Writing | writing, copywriting, editing, email, blog, social |
| Research | research, analysis, summary, summarise, learning |
| System & ops | ops, workflow, automation, setup, agents, claude code |

No matching tag files the prompt as **Unsorted**. The rules live in
`CATEGORY_RULES` in `prompts.py`.

## A different folder

Set `prompts_dir` in `kit.config.json` to keep your prompts somewhere else (a
private repo, a synced folder). This folder then stays as the example.
