#!/usr/bin/env python3
"""
Replay — score real prompts against skill descriptions to find misses.

STAGE 1 ONLY. This is a word-overlap scorer. It cannot tell whether a skill
SHOULD have fired; the real matcher is semantic and this is not. Its entire job
is to narrow ~48,800 user turns to a few hundred a session can actually read.

    python replay.py                      default project, top candidates
    python replay.py --project ALL        every project
    python replay.py --skill audio-balance
    python replay.py --json out.json      full candidate list for stage 2

Read the output. Do not summarise it into a verdict - "no invocation" has three
causes and only one is a defect:

  * a real miss - the words used are not in the description;
  * Claude did the work WITHOUT loading the skill, so the skill's method never
    got applied. This is the worst kind: it looks like it worked;
  * correctly not firing - the prompt only resembles the skill superficially.

Only stage 2 (a session reading the candidates) can tell those apart.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path

import config
import scan

PROJECTS = Path.home() / ".claude" / "projects"


def default_project(root: Path | None = None) -> str:
    """The ~/.claude/projects/<name> folder to read when --project is not given.

    transcripts_project from kit.config.json if set; otherwise the folder
    holding the most transcripts (where you actually work); otherwise "ALL".
    """
    if config.TRANSCRIPTS_PROJECT:
        return config.TRANSCRIPTS_PROJECT
    root = root or PROJECTS
    best, most = "ALL", 0
    try:
        for d in root.iterdir():
            if d.is_dir():
                n = sum(1 for _ in d.glob("*.jsonl"))
                if n > most:
                    best, most = d.name, n
    except OSError:
        pass
    return best


DEFAULT_PROJECT = default_project()

# Which skill was actually invoked, per session. Same pattern usage.py uses.
INVOKE_RE = re.compile(r'"skill"\s*:\s*"([a-zA-Z0-9:_-]+)"')

WORD_RE = re.compile(r"[a-z][a-z0-9-]{2,}")

# Words that carry no signal about WHICH skill a prompt wants. Kept short on
# purpose: an aggressive stoplist hides real vocabulary mismatches, which are
# the exact thing being hunted.
STOP = set("""
a an the and or but of to in for on with from by as at it its this that these
those is are was were be been being do does did done doing have has had having
can could will would should may might must shall
i me my we us our you your he she they them their
what which who whom when where why how not no yes if then than so such
use used using make made get got go going want need like just now here there
one two some any all both each few more most other own same
please thanks thanku ok okay right good great fine sure yeah yep nope
add put set new old also too very really quite bit lot
file files folder path run ran running open close save
claude session chat message thing stuff way things
rather whenever someone without through works work covers carries fires
describe describes including instead across already change even still
""".split())

# A prompt shorter than this is not evidence of anything.
MIN_PROMPT_WORDS = 6

# A candidate must share at least this many DISTINCT distinctive terms with the
# description. One shared word is coincidence, not a signal.
MIN_SHARED_TERMS = 3

# Above this many matching skills, the prompt is a document, not a request.
#
# NOTE, measured 2026-08-23: this has NEVER fired. Maximum observed breadth
# across 2,550 prompts is 3, against a threshold of >4, so it is unreachable
# at the default floor. The cosine's double normalisation already does the
# whole job - it was added at the same time and is what actually killed the
# nine-skills-one-document failure. Kept as a cheap backstop for a future
# corpus or a lower --min, NOT relied on. It also measures breadth AFTER the
# unborn and quoted filters, so a document matching nine skills of which six
# are unborn keeps three rows and escapes.
MAX_SKILLS_PER_PROMPT = 4

# Two floors, because the two tools want different things - but both defined
# HERE so neither can drift into a private literal.
#
# Recalibrated 2026-08-23 after the cosine fix. The old 0.025 was tuned to a
# scorer whose maximum was 0.056; a real cosine spreads the same corpus over
# 0-0.30, so the number had to move or it would have kept everything.
#
#   REPLAY wants a READABLE candidate list. p90 = 0.121, and 0.10 keeps 151
#   prompts - a set a session can actually read, which is the whole design
#   constraint of stage 1.
DEFAULT_FLOOR = 0.10
#
#   GUARD wants COVERAGE. Its job is noticing that an edit changed who wins a
#   prompt, and a win at 0.06 is still a real ranking fact - excluding it
#   means an edit that destroys it reports nothing. Adversarial review found
#   the old shared floor made 85% of the corpus invisible to guard. Lower, at
#   the cost of a longer diff.
GUARD_FLOOR = 0.05


# ---------------------------------------------------------------- corpus

def user_turns(path: Path) -> list[str]:
    """Real things the human typed in one transcript.

    Excludes tool results, hook output, slash-command stdout and the injected
    system reminders. Those are not prompts and would swamp the corpus - a
    scorer fed its own tooling's text finds matches in it and reports them
    with a straight face.
    """
    out = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    for line in raw.splitlines():
        if '"type":"user"' not in line and '"type": "user"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "user" or rec.get("isMeta"):
            continue

        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            # A list containing tool_result blocks is the harness replying to
            # itself, not the user speaking.
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue

        text = content.strip()
        # An injection that LEADS means the whole turn is harness output -
        # drop it. An injection that TRAILS is a real prompt with harness text
        # glued on, so keep the prompt and cut the tail. The old code dropped
        # both, which silently discarded genuine prompts whose only sin was a
        # reminder appended after them.
        if text.startswith("<"):
            continue
        if text.startswith("/") and len(text.split()) < 4:      # bare slash command
            continue
        if "Caveat: The messages below were generated" in text:
            continue
        # Compaction summaries arrive as type:"user" but the user did not type
        # them - Claude wrote them, about skill work, using skill vocabulary.
        # Scoring them finds Claude's own summaries quoting descriptions back
        # and ranks them top. Three of the first five hits were this.
        if text.lstrip().lower().startswith(
                "this session is being continued from a previous"):
            continue
        cleaned = strip_injections(text)
        if cleaned:
            out.append(cleaned)
    return out


# Harness text glued onto the END of a real prompt. The leading-`<` and
# `in text` guards above only catch an injection that leads or that we named;
# 52 of 2,550 scored turns carried a <widget_context_hint> tail nobody saw.
# It is not free: on the highest-scoring candidate in the whole run it
# depressed the score ~11% and CHANGED which skill ranked third.
INJECTED = re.compile(
    r"<(widget_context_hint|system-reminder|local-command[a-z-]*|"
    r"function_results|thinking)\b.*", re.S | re.I)


def strip_injections(text: str) -> str:
    """Cut a harness injection off the tail of an otherwise human prompt."""
    return INJECTED.sub("", text).strip()


SKILL_PATH_RE = re.compile(r"skills[\\/]+([a-z0-9][a-z0-9-]{2,})[\\/]+SKILL\.md")


def created_in(path: Path) -> set[str]:
    """Skills whose SKILL.md was WRITTEN during this session.

    Date alone is not enough. baked-text-patch was born the same day as the
    prompt that scored against it - born.json let it through, and the prompt
    turned out to be the one that caused the skill to be written. A skill
    cannot miss the conversation that created it.
    """
    out = set()
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"type":"create"' in line:
                out.update(SKILL_PATH_RE.findall(line))
    except OSError:
        pass
    return out


def session_date(path: Path) -> str:
    """First real timestamp IN the file.

    NOT the file mtime. mtime is when the transcript was last touched, which
    for a resumed or compacted session is weeks after the conversation. Using
    it dated a 22 July session to 15 August and produced a false finding.
    """
    fallback = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # Tolerate `"timestamp": "` with a space - the strict form was
                # format-brittle and returned "?" on a spaced variant.
                m = TS_RE.search(line)
                if not m:
                    continue
                # Prefer a CONVERSATION record. A transcript can open with a
                # summary or metadata record carrying its own timestamp, and
                # taking the first one blindly dated such a file wrongly.
                if CONV_RE.search(line):
                    return m.group(1)
                if fallback is None:
                    fallback = m.group(1)
        if fallback:
            return fallback
    except OSError:
        pass
    # FAIL CLOSED. "?" used to flow into `born > dates.get(sess, "9999")`,
    # where the 9999 default disabled the born filter for that whole session -
    # an unreadable date silently switched OFF a suppression. "9999" here
    # means "treat every skill as older than this session", which suppresses
    # rather than admits.
    return "0000"


TS_RE = re.compile(r'"timestamp"\s*:\s*"(\d{4}-\d\d-\d\d)')
CONV_RE = re.compile(r'"type"\s*:\s*"(?:user|assistant)"')


def family_of(name: str, names: set[str]) -> str:
    """'hyperframes-core' -> 'hyperframes' when the parent exists.

    A router parent loads the module it needs. Crediting only the exact skill
    reported hyperframes-core as a miss in a session where its sibling
    hyperframes-animation had fired - the family did engage.

    LONGEST ancestor wins, and the max() makes it deterministic. `next()` over
    a set picked an arbitrary ancestor when more than one matched, so with
    both 'x' and 'x-y' present the credit for 'x-y-z' flipped between runs
    depending on PYTHONHASHSEED. Latent today - no live name has two
    ancestors - and silent when it lands.

    This is a name-prefix test, not routing: it assumes a hyphenated child of
    an existing skill IS a module of it. True for every current name.
    """
    parents = [p for p in names if p != name and name.startswith(p + "-")]
    return max(parents, key=len) if parents else name


def invoked_in(path: Path) -> set[str]:
    try:
        return set(INVOKE_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return set()


def transcripts(project: str) -> list[Path]:
    if project == "ALL":
        return sorted(PROJECTS.rglob("*.jsonl"))
    return sorted((PROJECTS / project).glob("*.jsonl"))


# ---------------------------------------------------------------- scoring

def terms(text: str) -> set[str]:
    return {w for w in WORD_RE.findall(text.lower()) if w not in STOP}


def shingles(text: str, n: int = 6) -> set[str]:
    w = text.lower().split()
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def quotes_description(prompt: str, desc_shingles: set[str]) -> bool:
    """Did the user paste the description INTO the prompt?

    People do this when a skill fails to fire: they paste its trigger list to
    force it. The overlap is then total and meaningless - the description is
    matching itself. Top hit on the first run was exactly this.
    """
    return bool(desc_shingles & shingles(prompt))


def build_idf(docs: list[set[str]]) -> dict[str, float]:
    """Rare words discriminate; common ones do not.

    Without this, every description containing "video" matches every prompt
    containing "video" and the top of the list is noise.
    """
    n = len(docs) or 1
    df = Counter()
    for d in docs:
        df.update(d)
    # Clamped at zero. log(n/(1+c)) goes NEGATIVE for a term appearing in
    # every document, and on a one- or two-prompt corpus EVERY term does -
    # so every score came out negative and meaninglessly ordered. Unreachable
    # on the 2,550-prompt corpus, entirely reachable via --project on a new
    # or small project directory.
    return {w: max(0.0, math.log(n / (1 + c))) for w, c in df.items()}


def score(prompt_terms: set[str], skill_terms: set[str], idf: dict[str, float]) -> tuple[float, list[str]]:
    shared = prompt_terms & skill_terms
    if len(shared) < MIN_SHARED_TERMS:
        return 0.0, []
    # Cosine, NOT hit/skill-weight. Dividing by the skill alone rewards long
    # prompts: a pasted 2,000-word spec owns so many terms it intersects every
    # description, and the first run duly ranked one document against nine
    # different skills at 0.49-0.58. Normalising by BOTH sides kills that.
    #
    # The numerator squares the IDF, and that is not cosmetic. It used to be
    # idf**1 against idf**2 norms, which left a residual 1/idf in the ratio:
    # a term set scored against ITSELF came out at 1/idf instead of 1.0, and
    # two structurally identical matches ranked up to 8x apart purely on how
    # rare their shared words were - with the RARER, more distinctive match
    # ranking LOWER. That inverted build_idf's entire purpose. Found by
    # adversarial review 2026-08-23; the double normalisation that killed the
    # pasted-document failure is untouched by the fix.
    hit = sum(idf.get(w, 0.0) ** 2 for w in shared)
    ns = math.sqrt(sum(idf.get(w, 0.0) ** 2 for w in skill_terms)) or 1.0
    np_ = math.sqrt(sum(idf.get(w, 0.0) ** 2 for w in prompt_terms)) or 1.0
    # Tie-break by word so the evidence list is deterministic. Most shared
    # terms occur once and share an identical IDF, so an unstable sort made
    # the [:8] slice pick an arbitrary 8 of many ties - two runs printed
    # completely different evidence for the same candidate.
    return hit / (ns * np_), sorted(shared, key=lambda w: (-idf.get(w, 0.0), w))[:8]


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=DEFAULT_PROJECT)
    ap.add_argument("--skill", help="only this skill")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min", type=float, default=DEFAULT_FLOOR, dest="floor")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    # Windows consoles default to cp1252 and die on an arrow in a real prompt.
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    items, _ = scan.scan()
    # Only skills that could plausibly be expected to fire. An off plugin skill
    # not firing is not a defect, it is the setting.
    skills = [i for i in items if i["reach"] == "auto" and i["desc"]]
    if args.skill:
        skills = [i for i in skills if i["name"] == args.skill]
        if not skills:
            raise SystemExit(f"no auto skill named {args.skill!r}")

    files = transcripts(args.project)
    if not files:
        raise SystemExit(f"no transcripts under {PROJECTS / args.project}")

    prompts: list[tuple[str, str]] = []      # (session stem, text)
    names = {s["name"] for s in skills}
    # A skill cannot fail to fire before it was written, and the prompts that
    # CAUSED it to be written score highest against it - its description was
    # built from those words. Unknown birth means "predates the corpus".
    try:
        born = json.loads((Path(__file__).resolve().parent / "born.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        born = {}
    fired: dict[str, set[str]] = {}
    dates: dict[str, str] = {}
    newborn: dict[str, set[str]] = {}
    for f in files:
        raw = invoked_in(f)
        # Credit the whole family, and strip any plugin namespace prefix.
        fired[f.stem] = {family_of(x.split(":")[-1], names) for x in raw} | raw
        dates[f.stem] = session_date(f)
        newborn[f.stem] = created_in(f)
        for t in user_turns(f):
            if len(t.split()) >= MIN_PROMPT_WORDS:
                prompts.append((f.stem, t))

    prompt_terms = [terms(t) for _, t in prompts]
    skill_terms = {s["name"]: terms(s["name"].replace("-", " ") + " " + s["desc"])
                   for s in skills}
    skill_shingles = {s["name"]: shingles(s["desc"]) for s in skills}

    # IDF over the PROMPTS, not the descriptions: the question is which words
    # are rare in how the user actually writes.
    idf = build_idf(prompt_terms)

    cands = []
    quoted = 0
    unborn = 0
    promiscuous = 0
    for idx, ((sess, text), pt) in enumerate(zip(prompts, prompt_terms)):
        row = []
        for s in skills:
            sc, shared = score(pt, skill_terms[s["name"]], idf)
            if sc < args.floor:
                continue
            if s["name"] in newborn.get(sess, set()):
                unborn += 1
                continue
            # Unknown session date defaults to "0000" (fail closed - suppress),
            # matching session_date()'s own fallback. It used to be "9999",
            # which admitted everything from a session whose date failed to
            # parse.
            if born.get(s["name"], "0000") > dates.get(sess, "0000"):
                unborn += 1
                continue
            if quotes_description(text, skill_shingles[s["name"]]):
                quoted += 1
                continue
            row.append({
                "score": round(sc, 3),
                "skill": s["name"],
                "fired_in_session": (s["name"] in fired.get(sess, set())
                                     or family_of(s["name"], names) in fired.get(sess, set())),
                "date": dates.get(sess, "?"),
                "session": sess,
                "shared": shared,
                "prompt": text[:400],
            })

        # A prompt that "wants" five different skills wants none of them - it is
        # a pasted document, and every hit on it is an artefact of its length.
        if len(row) > MAX_SKILLS_PER_PROMPT:
            promiscuous += 1
            continue
        cands.extend(row)

    cands.sort(key=lambda c: -c["score"])
    misses = [c for c in cands if not c["fired_in_session"]]

    print(f"project     {args.project}  ({len(files)} transcripts)")
    print(f"user turns  {len(prompts):,} scored (>= {MIN_PROMPT_WORDS} words)")
    print(f"skills      {len(skills)} auto-reachable with a description")
    print(f"candidates  {len(cands):,} above {args.floor}, "
          f"{len(misses):,} where that skill did not fire in that session")
    print(f"suppressed  {quoted:,} quoting the description back, "
          f"{promiscuous:,} pasted documents matching "
          f">{MAX_SKILLS_PER_PROMPT} skills, "
          f"{unborn:,} predating the skill's creation")
    print()
    print("!! Word overlap only. A high score is a REASON TO READ, not a verdict.")
    print()

    for c in misses[:args.top]:
        print(f"[{c['score']:.3f}] {c['skill']}   ({c['session'][:8]} {c.get('date','?')})")
        print(f"   shared: {', '.join(c['shared'])}")
        print(f"   > {' '.join(c['prompt'].split())[:300]}")
        print()

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(misses, indent=1), encoding="utf-8")
        print(f"wrote {len(misses):,} candidates -> {args.json_out}")


if __name__ == "__main__":
    main()
