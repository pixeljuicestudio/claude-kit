#!/usr/bin/env python3
"""Selftest for scan.py - invariants that must hold against the live tree.

Written 2026-08-23 after a counting bug that had been dormant since the
launcher was built. The catalogue de-duplication keyed on `reach == "off"`
instead of on being an installed copy, so it only suppressed the twins of
DISABLED plugins. Enabling a plugin made its catalogue entry reappear next to
the installed one - meaning the inventory grew by 8 the moment two plugins were
turned on, and the tool that exists to say what you have reported 75 when the
answer was 67.

Nothing caught it because nothing had ever been enabled before.

Run: python test_scan.py      Exit 0 = every invariant holds.
"""
import collections
import json
import sys
import tempfile
from pathlib import Path

import scan

FAILS = []


def check(label, ok, detail=""):
    print("%-5s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILS.append("%s: %s" % (label, detail))


items, meta = scan.scan()

# ---------------------------------------------------------------- no doubles
# THE regression. A skill is one thing; it must appear once however many places
# on disk it happens to exist. Identity is (plugin, folder) for plugin skills
# and (path, folder) for everything else.
seen = collections.Counter(
    (i.get("plugin") or i["path"], i["folder"]) for i in items
)
dupes = [k for k, n in seen.items() if n > 1]
check("no duplicate (plugin, folder) pairs", not dupes, repr(dupes[:5]))

# The specific shape of the bug: an ENABLED installed plugin must not also be
# listed in the catalogue. Asserting the general rule above would pass if the
# catalogue pass were deleted entirely, so name the real case too.
inst = {(i["plugin"].split("@")[0], i["folder"])
        for i in items if i["location"] == "Plugins - installed"}
cat = {(i["plugin"].split("@")[0], i["folder"])
       for i in items if i["location"] == "Plugins - catalogue"}
overlap = inst & cat
check("installed copies are absent from the catalogue", not overlap,
      repr(sorted(overlap)[:5]))

# ...and the pass being tested is actually doing something. If there are no
# installed plugins at all, the two checks above are vacuous - they'd pass on a
# machine with an empty cache/ and prove nothing.
if not inst:
    # Reported, not failed: a fresh Claude Code install has no plugins, and
    # the fixture below covers the dedup logic either way.
    print("NOTE  no installed plugins on this machine - the live dedup checks "
          "above are vacuous; the fixture tests below still run")

# --------------------------------------------------------------- reachability
# reach is the whole point of this scanner: not "does a SKILL.md exist" but
# "can a session name it". Only these three values are meaningful.
bad_reach = {i["reach"] for i in items} - {"auto", "manual", "off"}
check("reach is only auto/manual/off", not bad_reach, repr(bad_reach))

# An enabled plugin's skills must be reachable, a disabled one's must not.
enabled = meta["enabled_plugins"]
wrong = [i["name"] for i in items
         if i["location"] == "Plugins - installed"
         and (i["reach"] != "off") != bool(enabled.get(i["plugin"], False))]
check("installed reach matches enabledPlugins", not wrong, repr(wrong[:5]))

# A catalogue entry is by definition not installed, so it can never be reached.
bad_cat = [i["name"] for i in items
           if i["location"] == "Plugins - catalogue" and i["reach"] != "off"]
check("catalogue entries are always off", not bad_cat, repr(bad_cat[:5]))

# ---------------------------------------------------------------- basic shape
required = ("name", "folder", "path", "file", "desc", "reach", "location",
            "origin", "flags")
missing = [i.get("name", "?") for i in items
           if any(k not in i for k in required)]
check("every record carries the required keys", not missing, repr(missing[:5]))

# Your skills may well have over-long descriptions - that is a finding about
# them, not a bug in Kit. What must hold is that every one is FLAGGED, so the
# page shows it.
check("every over-limit description carries a health flag",
      all(any("upload limit" in f for f in i["flags"])
          for i in items if len(i["desc"] or "") > scan.DESC_LIMIT),
      repr([i["name"] for i in items if len(i["desc"] or "") > scan.DESC_LIMIT
            and not any("upload limit" in f for f in i["flags"])][:5]))

# ------------------------------------------------------- stale plugin cache
# A plugin update does not delete its OLD cache-hash directory, only stops
# pointing installed_plugins.json at it. scan() used to walk the WHOLE cache
# tree with no regard for which hash is actually installed, so an update left
# one behind and every one of that plugin's skills doubled, each copy
# "competing with" itself under mark_collisions. Not hypothetical: this
# happened for real to plugin-dev on 2026-09-07. Fixture, not the live tree,
# so the regression stays testable after the real machine's stale directory
# eventually gets cleaned up and the live-tree check above goes quiet again.
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    good = root / "cache" / "mp" / "testplug" / "good_hash" / "skills" / "testskill"
    stale = root / "cache" / "mp" / "testplug" / "stale_hash" / "skills" / "testskill"
    good.mkdir(parents=True)
    stale.mkdir(parents=True)
    body = "---\nname: testskill\ndescription: a fixture skill\n---\nbody\n"
    (good / "SKILL.md").write_text(body, encoding="utf-8")
    (stale / "SKILL.md").write_text(body, encoding="utf-8")

    installed_json = root / "installed_plugins.json"
    installed_json.write_text(json.dumps({"plugins": {
        "testplug@mp": [{"scope": "user", "installPath": str(good.parent.parent),
                         "version": "good_hash"}]
    }}), encoding="utf-8")
    settings_json = root / "settings.json"
    settings_json.write_text(json.dumps({"enabledPlugins": {"testplug@mp": True}}),
                             encoding="utf-8")

    # Where each marketplace comes from is read from Claude Code's own
    # known_marketplaces.json - never a hardcoded table.
    known_json = root / "known_marketplaces.json"
    known_json.write_text(json.dumps({
        "mp": {"source": {"source": "github", "repo": "example-org/example-plugins"}},
        "by-url": {"source": {"source": "git",
                              "url": "https://github.com/someone/their-plugins.git"}},
        "local": {"source": {"source": "directory", "path": "/home/user/plugins"}},
    }), encoding="utf-8")

    # An extra skills folder (extra_skill_dirs).
    extra = root / "extra-skills" / "extraskill"
    extra.mkdir(parents=True)
    (extra / "SKILL.md").write_text(
        "---\nname: extraskill\ndescription: from an extra folder\n---\nbody\n",
        encoding="utf-8")

    keep = (scan.PLUGIN_CACHE, scan.INSTALLED, scan.SETTINGS, scan.MACHINE_SKILLS,
            scan.EXTRA_SKILL_DIRS, scan.PLUGIN_MARKET, scan.KNOWN_MARKETPLACES)
    scan.PLUGIN_CACHE = root / "cache"
    scan.INSTALLED = installed_json
    scan.SETTINGS = settings_json
    scan.MACHINE_SKILLS = root / "no-machine-skills"
    scan.EXTRA_SKILL_DIRS = [root / "extra-skills", root / "missing-extra"]
    scan.PLUGIN_MARKET = root / "no-marketplace"
    scan.KNOWN_MARKETPLACES = known_json
    try:
        fx_items, _ = scan.scan()
        matches = [i for i in fx_items if i["name"] == "testskill"]
        check("a stale plugin-cache hash is not scanned as a second copy",
              len(matches) == 1, [m["file"] for m in matches])
        if matches:
            check("the surviving copy is the one installed_plugins.json names",
                  "good_hash" in matches[0]["file"], matches[0]["file"])
            check("a plugin's upstream comes from known_marketplaces.json",
                  matches[0]["upstream"] == "example-org/example-plugins",
                  matches[0]["upstream"])
        repos = scan.marketplace_repos()
        check("a git-url marketplace resolves to owner/repo",
              repos.get("by-url") == "someone/their-plugins", repos)
        check("a local-folder marketplace has no upstream", "local" not in repos, repos)
        check("no known_marketplaces.json means {}",
              scan.marketplace_repos(root / "nope.json") == {})
        ex = [i for i in fx_items if i["name"] == "extraskill"]
        check("extra_skill_dirs are scanned (a missing one is skipped)", len(ex) == 1, ex)
        if ex:
            check("an extra-folder skill uses the stable 'cloud' home key",
                  ex[0]["home"] == "cloud" and ex[0]["location"] == "Extra folder", ex[0])
    finally:
        (scan.PLUGIN_CACHE, scan.INSTALLED, scan.SETTINGS, scan.MACHINE_SKILLS,
         scan.EXTRA_SKILL_DIRS, scan.PLUGIN_MARKET, scan.KNOWN_MARKETPLACES) = keep

print()
print("scanned %d skills: %s" % (
    len(items), dict(collections.Counter(i["reach"] for i in items))))
if FAILS:
    print("\n=== %d FAILURE(S) ===" % len(FAILS))
    for f in FAILS:
        print("  " + f)
    sys.exit(1)
print("=== SELFTEST COMPLETE - 0 failure(s) ===")
