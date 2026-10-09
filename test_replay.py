#!/usr/bin/env python3
"""Selftest for the analysis chain: replay.py, guard.py, born.py.

One file for three modules because they are one thing: guard imports replay's
scorer and corpus reader, and born feeds replay's pre-birth suppression. A
defect in replay.score() is a defect in every guard verdict.

WHY THIS EXISTS. None of the three had a test suite. That is how a scorer
which INVERTED its own stated purpose - weighting rare, distinctive terms
LOWER, when the whole point of IDF is the opposite - ran for a full day and
produced findings that were acted on. Two adversarial reviews on 2026-08-23
listed exactly what a suite would have to assert; this is that list.

Every case is either a real defect those reviews found, or the behaviour that
defect broke. No network. Synthetic fixtures throughout, EXCEPT the two
end-to-end cases which build a whole fake home directory so the CLI can be
driven for real.

Run: python test_replay.py        Exit 0 = every case behaved.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import born
import guard
import replay
import scan

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{label}: wanted {want!r}, got {got!r}")
    print(f"{'PASS' if ok else 'FAIL':<6} {label}")


def near(label, got, want, tol=1e-9):
    ok = abs(got - want) < tol
    if not ok:
        FAILS.append(f"{label}: wanted ~{want}, got {got}")
    print(f"{'PASS' if ok else 'FAIL':<6} {label}")


def section(title):
    print(f"\n--- {title} ---")


# ============================================================ score()
section("score() — the headline defect")

T3 = {"alpha", "bravo", "charlie"}

# A cosine of a vector with ITSELF is 1.0 BY DEFINITION. It used to return
# 1/idf (0.333 on real descriptions) because the numerator was idf**1 against
# idf**2 norms. This single assertion would have caught the whole thing.
for u in (1.0, 2.0, 4.0, 8.0):
    near(f"self-score is 1.0 at idf={u}", replay.score(T3, T3, {w: u for w in T3})[0], 1.0)

# Scale invariance: multiplying every weight by a constant must not move the
# score. This is the assertion that FAILED before the fix.
_p, _s = {"a", "b", "c", "d"}, {"a", "b", "c", "z"}
_base = replay.score(_p, _s, {w: 2.0 for w in "abcdz"})[0]
for k in (0.5, 3.0, 10.0):
    near(f"scale-invariant under idf x{k}",
         replay.score(_p, _s, {w: 2.0 * k for w in "abcdz"})[0], _base, 1e-9)

# Rare vocabulary must not be PENALISED. Two identical-shape matches, one on
# rare terms and one on common: the rare match must score at least as high.
_common = replay.score(T3, T3, {w: 1.0 for w in T3})[0]
_rare = replay.score(T3, T3, {w: 9.0 for w in T3})[0]
# Tolerance, not `>=`: both are 1.0 to within float noise, and a bare
# comparison made this fail on a 2.2e-16 difference.
check("rare vocabulary is not ranked below common", _rare >= _common - 1e-9, True)

# MIN_SHARED_TERMS boundary, both sides.
_idf = {w: 3.0 for w in "abcdef"}
check("exactly 2 shared terms scores 0",
      replay.score({"a", "b", "x"}, {"a", "b", "y"}, _idf)[0], 0.0)
check("exactly 3 shared terms scores above 0",
      replay.score({"a", "b", "c"}, {"a", "b", "c"}, _idf)[0] > 0, True)

# Founding failure 2, in miniature: a long document padded with off-topic
# vocabulary must never outrank a short on-target prompt.
_skill = {"mix", "loudness", "dialogue", "bed"}
_short = {"mix", "loudness", "dialogue"}
_pad = [f"filler{i}" for i in range(1600)]
# Every padding term must carry a WEIGHT. A term missing from the idf map
# contributes 0 to the prompt norm, so padding with unknown vocabulary does
# not decay at all - which is exactly how the first version of this test
# fooled itself. In real use build_idf covers every prompt term.
_widf = {w: 4.0 for w in list(_skill) + _pad}
_hi = replay.score(_short, _skill, _widf)[0]
_lo = replay.score(_short | set(_pad[:400]), _skill, _widf)[0]
check("a padded document scores below a short on-target prompt", _lo < _hi, True)
check("padding decays the score monotonically",
      all(replay.score(_short | set(_pad[:n]), _skill, _widf)[0]
          > replay.score(_short | set(_pad[:n * 4]), _skill, _widf)[0]
          for n in (5, 25, 100)), True)

# Determinism of the evidence list. Most shared terms tie on IDF, and an
# unstable sort made the [:8] slice take an arbitrary 8 of many - two runs
# printed different evidence for the SAME candidate.
_tied = {f"w{i}" for i in range(30)}
_ev = replay.score(_tied, _tied, {w: 5.0 for w in _tied})[1]
check("tied evidence terms sort deterministically", _ev == sorted(_ev), True)


# ============================================================ build_idf()
section("build_idf() — weights can never go negative")

check("empty corpus", replay.build_idf([]), {})
# log(n/(1+c)) is negative whenever a term appears in every document, and on a
# tiny corpus EVERY term does. Reachable via --project on a new directory.
for label, docs in (("single document", [{"a", "b"}]),
                    ("term in every document", [{"a"}, {"a"}, {"a"}]),
                    ("two documents", [{"a", "b"}, {"a", "c"}])):
    idf = replay.build_idf(docs)
    check(f"no negative weight: {label}",
          all(v >= 0 for v in idf.values()), True)

# A KNOWN LIMIT, asserted so it is a decision rather than a surprise. On a
# corpus of one or two prompts every weight clamps to zero, so every score is
# zero and nothing is ever flagged. That is fail-SAFE - no false findings -
# but it also means --project on a nearly-empty directory reports nothing and
# cannot be read as "clean".
_tiny = replay.build_idf([{"a", "b"}, {"a", "c"}])
check("a two-prompt corpus yields all-zero weights",
      set(_tiny.values()), {0.0})
check("...and therefore a zero score, not a crash",
      replay.score({"a", "b", "c"}, {"a", "b", "c"}, _tiny)[0], 0.0)


# ============================================================ user_turns()
section("user_turns() — only what a human typed")


def transcript(tmp: Path, *records) -> Path:
    p = tmp / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def turn(text, **extra):
    r = {"type": "user", "message": {"content": text},
         "timestamp": "2026-07-22T10:00:00.000Z"}
    r.update(extra)
    return r


HUMAN = "please bring the music down about ten percent from sixteen seconds"

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    def only(*records):
        return replay.user_turns(transcript(tmp, *records))

    check("a plain human turn is kept", only(turn(HUMAN)), [HUMAN])
    check("an assistant record is dropped",
          only({"type": "assistant", "message": {"content": HUMAN},
                "timestamp": "2026-07-22T10:00:00.000Z"}), [])
    check("isMeta is dropped", only(turn(HUMAN, isMeta=True)), [])
    check("a tool_result block is dropped",
          only({"type": "user", "timestamp": "2026-07-22T10:00:00.000Z",
                "message": {"content": [{"type": "tool_result", "content": "x"}]}}), [])
    check("a block mixing tool_result and text is dropped whole",
          only({"type": "user", "timestamp": "2026-07-22T10:00:00.000Z",
                "message": {"content": [{"type": "tool_result", "content": "x"},
                                        {"type": "text", "text": HUMAN}]}}), [])
    check("a leading system-reminder is dropped",
          only(turn("<system-reminder>do a thing</system-reminder>")), [])
    check("local-command stdout is dropped",
          only(turn("<local-command-stdout>ok</local-command-stdout>")), [])
    check("the Caveat banner is dropped",
          only(turn("Caveat: The messages below were generated by the user "
                    "while running local commands.")), [])
    check("a bare slash command is dropped", only(turn("/wrap up now")), [])
    check("a long slash-prefixed sentence is kept",
          only(turn("/loop check the deploy every five minutes please")) != [], True)

    # The compaction banner - Claude's OWN writing, arriving as type:"user".
    # It ranked #1 as a "miss" on the first real run. Exact, mixed case and
    # leading whitespace must all be caught.
    for label, s in (("exact", "This session is being continued from a previous"),
                     ("lowercase", "this session is being continued from a previous"),
                     ("leading whitespace", "  This session is being continued from a previous")):
        check(f"compaction summary dropped: {label}",
              only(turn(s + " conversation. Summary: skill work.")), [])

    # Harness text glued onto the END of a real prompt. 52 of 2,550 real turns
    # carried one; on the top candidate it moved the score ~11%.
    check("a widget hint is stripped from the tail, prompt kept",
          only(turn(HUMAN + " <widget_context_hint>Interactive widgets in this "
                            "conversation: mcp__x__y</widget_context_hint>")), [HUMAN])
    check("a trailing system-reminder is stripped, prompt kept",
          only(turn(HUMAN + "\n<system-reminder>note</system-reminder>")), [HUMAN])
    check("an untouched prompt is not altered",
          only(turn(HUMAN)), [HUMAN])


# ============================================================ session_date()
section("session_date() — never mtime, and fails CLOSED")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    def dated(*records):
        return replay.session_date(transcript(tmp, *records))

    check("normal transcript", dated(turn(HUMAN)), "2026-07-22")
    # Format brittleness: the strict '"timestamp":"' find() returned "?" on a
    # spaced variant, and "?" then DISABLED the born filter for the session.
    p = tmp / "t.jsonl"
    p.write_text('{"type": "user", "timestamp": "2026-07-22T10:00:00Z"}', encoding="utf-8")
    check("spaced JSON form still parses", replay.session_date(p), "2026-07-22")
    # A leading non-conversation record must not date the file.
    check("a leading summary record does not win",
          dated({"type": "summary", "timestamp": "2020-01-01T00:00:00Z"},
                turn(HUMAN)), "2026-07-22")
    # FAIL CLOSED. "0000" makes every skill look older than the session, which
    # SUPPRESSES; the old "?"/"9999" pair admitted everything instead.
    p.write_text("", encoding="utf-8")
    check("empty file fails closed", replay.session_date(p), "0000")
    check("missing file fails closed",
          replay.session_date(tmp / "nope.jsonl"), "0000")
    p.write_text("{not json at all\n" + json.dumps(turn(HUMAN)), encoding="utf-8")
    check("recovers past a corrupt first line", replay.session_date(p), "2026-07-22")


# ============================================================ family_of()
section("family_of() — deterministic, longest ancestor")

check("a module credits its router parent",
      replay.family_of("hyperframes-core", {"hyperframes", "hyperframes-core"}),
      "hyperframes")
check("a standalone skill credits itself",
      replay.family_of("audio-balance", {"audio-balance", "hyperframes"}),
      "audio-balance")
# next() over a SET picked an arbitrary ancestor; with two present the credit
# flipped between runs on PYTHONHASHSEED. Longest wins, every time.
check("two ancestors: the longest wins, repeatably",
      {replay.family_of("x-y-z", {"x", "x-y", "x-y-z"}) for _ in range(200)},
      {"x-y"})
# A name that merely starts with another is NOT its module.
check("a shared prefix without a hyphen is not a family",
      replay.family_of("audiobook", {"audio", "audiobook"}), "audiobook")


# ============================================================ born.py
section("born.py — one pattern, both separators")

# The two modules must use the SAME pattern or they can silently disagree
# about what a skill path looks like.
check("born and replay share one path pattern",
      born.PATH_RE.pattern == replay.SKILL_PATH_RE.pattern, True)

# Golden case: an earlier version returned "esolve-confor", eating the first
# and last character, because the separator was matched with two dots.
for label, path in (
        ("windows escaped", r'skills\\resolve-conform\\SKILL.md'),
        ("posix", "skills/resolve-conform/SKILL.md"),
        ("windows single", r"skills\resolve-conform\SKILL.md")):
    check(f"path parses whole: {label}",
          born.PATH_RE.search(path).group(1) if born.PATH_RE.search(path) else None,
          "resolve-conform")

# Documented, deliberate drops. Asserted so a future change is a decision.
for label, name, want in (("two-char name", "ab", False),
                          ("uppercase", "Audio-Balance", False),
                          ("underscore", "audio_balance", False),
                          ("normal", "audio-balance", True)):
    got = bool(born.PATH_RE.search(f"skills/{name}/SKILL.md"))
    check(f"path name policy: {label}", got, want)


# ============================================================ guard.py
section("guard.py — identity, coverage, and a real regression")

check("the floor is imported, not a private literal",
      guard.FLOOR == replay.GUARD_FLOOR, True)

# Two DISTINCT prompts sharing their first 120 characters. The old identity
# was that prefix, so one of these vanished - 145 such collisions existed in
# the real corpus, three inside one skill's win list.
PRE = ("can you bring the music down a touch from about sixteen seconds in and "
       "also please have a look at the voice because it really sounds ")
A_TXT, B_TXT = PRE + "scratchy and thin", PRE + "boxy like a telephone"
check("the collision fixture really shares a 120-char prefix",
      A_TXT[:120] == B_TXT[:120], True)

SKILLS = [{"name": "audio-balance", "reach": "auto",
           "desc": "mix balance loudness dialogue bed music voice scratchy "
                   "telephone boxy thin levels"},
          {"name": "video-grab", "reach": "auto",
           "desc": "download grab a url reel post clip into the swipe file"}]


# Filler prompts so IDF has something to discriminate WITH. A two-prompt
# corpus clamps every weight to zero (see build_idf above), which made the
# first version of these cases score everything at 0.0 and silently assert
# nothing.
FILLER = [
    "render the timeline out to a mezzanine file and upload it to the review page",
    "what is the fastest way to get these stills onto the storyboard document",
    "the client wants the logo bigger on the end card and a slower fade there",
    "check whether the deploy finished and tell me if the smoke tests passed",
    "book a call with the producer about the schedule for next tuesday morning",
    "find the invoice from the studio and tell me what we paid for the licence",
    "summarise the meeting notes into three bullets for the weekly update email",
    "the export keeps failing at ninety percent with no error in the log file",
    "swap the background plate for the daylight version and rerender the shot",
    "write a caption for the reel that mentions the launch date and the price",
]


def fake_chain(tmp: Path, skills, prompts):
    """Point guard at synthetic skills and prompts, touching no real file."""
    p = tmp / "s.jsonl"
    p.write_text("\n".join(json.dumps(turn(t)) for t in list(prompts) + FILLER),
                 encoding="utf-8")
    scan.scan = lambda: ([dict(s) for s in skills], {})
    replay.transcripts = lambda project: [p]


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    real_scan, real_tx, real_snap = scan.scan, replay.transcripts, guard.SNAP
    real_floor = guard.FLOOR
    guard.SNAP = tmp / "baseline.json"
    # These cases test IDENTITY and RETENTION, not the floor. A two-skill
    # synthetic corpus produces different absolute scores from the real one,
    # so gating on the production floor would test the fixture, not the code.
    guard.FLOOR = 0.0
    try:
        fake_chain(tmp, SKILLS, [A_TXT, B_TXT])
        w = guard.winners()
        ab = w.get("audio-balance", [])
        check("two prefix-twin prompts are two distinct wins", len(ab), 2)
        check("win identity is a hash, not the visible text",
              len({x["id"] for x in ab}), 2)

        # A no-op must be silent.
        before = guard.winners()
        check("an unchanged description loses nothing",
              guard.winners() == before, True)

        # An edit that genuinely destroys the trigger must report LOST.
        gutted = [dict(SKILLS[0], desc="mix audio."), SKILLS[1]]
        fake_chain(tmp, gutted, [A_TXT, B_TXT])
        after = guard.winners()
        lost = {x["id"] for x in before.get("audio-balance", [])} - \
               {x["id"] for x in after.get("audio-balance", [])}
        check("gutting a description reports its wins as LOST", len(lost) > 0, True)

        # Regression-lock the phantom: guard keeps EVERY win, not a top-N
        # slice. v1 kept the top 6, so gaining one high scorer evicted a
        # prompt the skill still won and printed it as LOST.
        many = [f"{PRE}{i} scratchy telephone boxy mix levels bed" for i in range(12)]
        fake_chain(tmp, SKILLS, many)
        check("all wins are retained, not a top-N slice",
              len(guard.winners().get("audio-balance", [])) > 6, True)
    finally:
        scan.scan, replay.transcripts, guard.SNAP = real_scan, real_tx, real_snap
        guard.FLOOR = real_floor


# ======================================================= end-to-end
section("end-to-end — the CLI, in a fake home, byte-identical across seeds")


def fake_home(root: Path):
    """A whole synthetic ~/.claude so the CLI can be driven for real."""
    sk = root / ".claude" / "skills"
    for name, desc in (("audio-balance",
                        "mix balance loudness dialogue bed music voice levels"),
                       ("video-grab",
                        "download grab url reel post clip swipe file")):
        d = sk / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n",
            encoding="utf-8")
    proj = root / ".claude" / "projects" / "C--work-my-project"
    proj.mkdir(parents=True)
    lines = [json.dumps(turn(t)) for t in (
        "can you bring the music down and sort the voice levels out please",
        "download this reel and grab it into the swipe file for later",
        "what is the weather doing today outside right now",
    )]
    (proj / "a.jsonl").write_text("\n".join(lines), encoding="utf-8")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    fake_home(root)
    outs = []
    ok = True
    for seed in ("7", "21"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8",
                   USERPROFILE=str(root), HOME=str(root))
        out = root / f"r{seed}.json"
        r = subprocess.run([sys.executable, str(HERE / "replay.py"),
                            "--top", "0", "--json", str(out)],
                           capture_output=True, text=True, env=env, timeout=180)
        if r.returncode != 0:
            FAILS.append(f"CLI run seed={seed} exited {r.returncode}: {r.stderr[:200]}")
            ok = False
            break
        outs.append(out.read_text(encoding="utf-8"))
    if ok and len(outs) == 2:
        check("the CLI runs clean against a synthetic home", True, True)
        # Two runs, two hash seeds. The evidence list used to differ.
        check("--json is byte-identical across PYTHONHASHSEED", outs[0] == outs[1], True)
    elif ok:
        FAILS.append("end-to-end produced no output")

print()
if FAILS:
    print(f"=== {len(FAILS)} FAILURE(S) ===")
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("=== SELFTEST COMPLETE - 0 failure(s) ===")
