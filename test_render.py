#!/usr/bin/env python3
"""Selftest that the page's JavaScript actually RUNS - not just that it parses.

⚠️ This exists because of a specific gap, hit on 2026-08-24. `sortTools` called
`byName`, which is declared as a `const` INSIDE sortPrograms and sortPrompts
and does not exist at module scope. The result was a ReferenceError thrown
mid-render: the Tools sidebar populated, then the list and the status bar kept
the previous tab's contents. The tab looked half-broken in a way that reads
as a CSS problem, and:

  * `node --check` passed - it is valid syntax, not valid semantics.
  * test_tools.py passed all 46 - Python never executes the emitted JS.
  * the payload was correct, so every data assertion held.
  * nothing appeared in the browser console, because the throw happened
    inside a click handler.

Four green checks and a broken tab. The only thing that found it was opening
the page and clicking. That is vigilance, and the standing rule in this repo
is that the remedy has to be mechanical instead.

So: run the emitted script in node against a DOM stub, then call every draw
function. Any undefined identifier, any bad property access on the render
path, fails here in about a second with no browser involved.

What this does NOT cover, deliberately: layout, colour, and anything that
needs a real event loop. Those still need eyes. This covers "does the render
path throw", which is the part that was silently untested.

Run: python test_render.py      Exit 0 = every draw function completes.
"""
import json
import pathlib
import re
import subprocess
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent

FAILS = []


def check(label, ok, detail=""):
    print("%-5s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILS.append("%s: %s" % (label, detail))


if not shutil.which("node"):
    print("SKIP  node is not on PATH - cannot run the render check")
    print("\n=== SELFTEST COMPLETE - 0 failure(s) ===")
    sys.exit(0)

# The page is rendered HERE, in-process, against a temporary tools folder, so
# the test does not depend on skills.html having been built or on what this
# machine happens to have configured. The fixture is shaped to exercise every
# path: two tools that both contain serve.py (the ambiguous-stem rule), one
# unique program, a launcher, a README.
import build      # noqa: E402
import programs   # noqa: E402
import tools      # noqa: E402

FIX = pathlib.Path(tempfile.mkdtemp(prefix="kit-render-"))
for tool, extra in (("alpha-tool", "alpha_only"), ("beta-tool", "beta_only")):
    d = FIX / tool
    d.mkdir()
    (d / "README.md").write_text(
        "# %s\n\nA small example tool that does one useful thing well.\n" % tool,
        encoding="utf-8")
    (d / "serve.py").write_text('"""Serve it."""\nif __name__ == "__main__":\n    pass\n',
                                encoding="utf-8")
    (d / ("%s.py" % extra)).write_text('"""Only here."""\n', encoding="utf-8")
    (d / "launch.bat").write_text("@echo off\n", encoding="utf-8")
(FIX / "unrostered").mkdir()

_saved = (tools.ROOT, tools.ROSTER, tools.NOT_TOOLS, tools.MANIFEST, programs.TOOLS)
tools.ROOT, tools.ROSTER, tools.NOT_TOOLS, tools.MANIFEST = FIX, ["alpha-tool", "beta-tool"], set(), None
programs.TOOLS = FIX
try:
    html = build.render_html(live=False)
finally:
    # And once with NOTHING configured - the stranger's first run.
    tools.ROOT, tools.ROSTER, tools.NOT_TOOLS, tools.MANIFEST = None, [], set(), None
    programs.TOOLS = None
    html_bare = build.render_html(live=False)
    (tools.ROOT, tools.ROSTER, tools.NOT_TOOLS, tools.MANIFEST, programs.TOOLS) = _saved
    shutil.rmtree(FIX, ignore_errors=True)

blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
check("the page carries exactly one script block", len(blocks) == 1, len(blocks))
js = "\n".join(blocks)
js_bare = "\n".join(re.findall(r"<script>(.*?)</script>", html_bare, re.S))

# A DOM stub, not a DOM. Every element is the same shape and getElementById
# memoises, so the script can write innerHTML and read it back. querySelectorAll
# returns [] on purpose: the render path is what is under test, and pretending
# to return live nodes would mean writing a selector engine to test a sort
# function.
STUB = r"""
globalThis.window = globalThis;
globalThis.isSecureContext = false;
const _els = new Map();
function mkEl(id){
  const el = {
    id, innerHTML:'', textContent:'', value:'', checked:false,
    style:new Proxy({}, {get:()=> '', set:()=> true}),
    dataset:{},
    classList:{ _s:new Set(),
      add(...c){c.forEach(x=>this._s.add(x)); return this;},
      remove(...c){c.forEach(x=>this._s.delete(x)); return this;},
      toggle(c,f){ const on = f===undefined ? !this._s.has(c) : !!f;
                   on ? this._s.add(c) : this._s.delete(c); return on; },
      contains(c){return this._s.has(c);} },
    appendChild(){return null;}, focus(){}, blur(){}, click(){},
    setAttribute(){}, getAttribute(){return null;}, removeAttribute(){},
    addEventListener(){}, closest(){return null;},
    // querySelector returns a stub rather than null. The page does
    // `lamp.querySelector('.t').textContent = ...` with no guard, which is
    // correct against a real DOM and explodes against a null. Returning a
    // stub keeps the harness honest about the render path instead of failing
    // on the stub's own poverty.
    querySelectorAll(){return [];}, querySelector(){return mkEl(id+' >');},
    get parentElement(){return mkEl(id+'-parent');},
    scrollIntoView(){}
  };
  return el;
}
globalThis.document = {
  documentElement: mkEl('html'),
  body: mkEl('body'),
  getElementById(id){ if(!_els.has(id)) _els.set(id, mkEl(id)); return _els.get(id); },
  // `#list` must resolve to the SAME object getElementById('list') hands out,
  // or the page holds two different stubs for one element and assertions read
  // the wrong one.
  querySelector(sel){
    if (typeof sel === 'string' && sel.startsWith('#') && !/[ .>\[]/.test(sel))
      return this.getElementById(sel.slice(1));
    return mkEl(String(sel));
  },
  querySelectorAll(){ return []; },
  createElement(){ return mkEl('created'); },
  addEventListener(){},
  get activeElement(){ return null; }
};
globalThis.localStorage = { _m:new Map(),
  getItem(k){return this._m.has(k)?this._m.get(k):null;},
  setItem(k,v){this._m.set(k,String(v));}, removeItem(k){this._m.delete(k);} };
globalThis.navigator = { clipboard:{ writeText(){ return Promise.resolve(); } } };
globalThis.fetch = () => Promise.resolve({ json: () => Promise.resolve({ok:true}) });
globalThis.alert = () => {};
globalThis.matchMedia = () => ({matches:false, addEventListener(){}, addListener(){}});
// No timers. pollLive would otherwise keep the process alive for ever, and a
// render test has no business making network calls on a loop.
globalThis.setTimeout = () => 0;
globalThis.setInterval = () => 0;
globalThis.clearTimeout = () => {};
globalThis.clearInterval = () => {};
globalThis.requestAnimationFrame = () => 0;
globalThis.__els = _els;
"""

TAIL = r"""
// ---- the actual assertions ----
const results = [];
function run(section, fn){
  const list = document.getElementById('list');
  list.innerHTML = '';
  try {
    S.section = section;
    fn();
    results.push({section, ok:true, chars:list.innerHTML.length});
  } catch (e) {
    results.push({section, ok:false, error:e.message,
                  where:(e.stack||'').split('\n')[1] || ''});
  }
}
run('skills',   draw);
run('prompts',  drawPrompts);
run('tools',    drawTools);
run('hooks',    drawHooks);
run('programs', drawPrograms);
run('groups',   drawGroups);   // the empty/top-level view - no group open

// The composite view is the one nothing else exercises: an OPEN group
// renders a skill card, a prompt card and a tool card side by side via
// wire()/wirePromptCards()/wireToolCards() all firing in the same draw,
// plus a ghost member for a name that no longer exists anywhere. This is
// new composition, not a rerun of an existing path - exactly the shape of
// thing sortTools' ReferenceError hid inside.
(function(){
  const skillName = ITEMS[0] && ITEMS[0].name;
  const promptName = PROMPTS[0] && PROMPTS[0].name;
  const toolName = TOOLS[0] && TOOLS[0].name;
  const members = [];
  if (skillName)  members.push({kind:'skill',  name:skillName});
  if (promptName) members.push({kind:'prompt', name:promptName});
  if (toolName)   members.push({kind:'tool',   name:toolName});
  members.push({kind:'skill', name:'__nonexistent-for-ghost-test__'});
  GROUPS['__test__'] = {name:'Render test', members};
  S.section = 'groups'; S.groupOpen = '__test__';
  const list = document.getElementById('list');
  list.innerHTML = '';
  try {
    drawGroups();
    results.push({section:'groups (open, mixed members)', ok:true, chars:list.innerHTML.length});
  } catch (e) {
    results.push({section:'groups (open, mixed members)', ok:false, error:e.message,
                  where:(e.stack||'').split('\n')[1] || ''});
  }
  delete GROUPS['__test__'];
  S.groupOpen = null;
})();

// Sorting is where the ReferenceError actually lived, and the default sort
// would not have reached it. Exercise EVERY option on every section.
for (const [sec, list] of Object.entries(SORTS)) {
  for (const [value] of list) {
    const key = sec==='programs' ? 'psort' : sec==='prompts' ? 'rsort'
              : sec==='tools' ? 'tsort' : sec==='hooks' ? 'hsort'
              : sec==='groups' ? 'gsort' : 'sort';
    S.section = sec; S[key] = value;
    const fn = {skills:draw, prompts:drawPrompts, tools:drawTools,
                hooks:drawHooks, programs:drawPrograms, groups:drawGroups}[sec];
    try { fn(); }
    catch (e) { results.push({section:sec+' sort='+value, ok:false,
                              error:e.message,
                              where:(e.stack||'').split('\n')[1] || ''}); }
  }
}
// ---- the live painter ----
// paintLive is called on a timer, not by draw(), so nothing above touches it.
// It is also the easiest thing in the page to silently break: it takes an
// attribute name per section, and a section whose cards use a different
// attribute simply matches nothing and lights nothing. That is exactly what
// happened to Tools on the day it shipped - no error, no glow, no clue.
function paintOn(section){
  S.section = section;
  try { paintLive(); return {section, ok:true}; }
  catch (e) { return {section, ok:false, error:e.message,
                      where:(e.stack||'').split('\n')[1] || ''}; }
}
['skills','prompts','tools','hooks','programs','groups'].forEach(s =>
  results.push(Object.assign(paintOn(s), {paint:true})));

// A section can only light cards whose attribute it actually queries. Assert
// the pairing rather than trusting that paintLive was updated alongside the
// card renderer.
const SEL = {skills:'data-n', prompts:'data-rn', programs:'data-n',
             tools:'data-tn', hooks:'data-hn'};
const src = paintLive.toString();
for (const [sec, attr] of Object.entries(SEL)) {
  if (sec === 'hooks') continue;          // dark on purpose
  results.push({section:'paintLive knows ' + attr, paint:true,
                ok: src.includes(attr)});
}
results.push({section:'hooks is dark deliberately', paint:true,
              ok: src.includes("S.section === 'hooks'")});

// ---- the ambiguous-stem rule ----
// `serve` lives in three tool folders. Lighting all three would be a
// confident wrong glow on two cards. These assertions are the reason the
// path is carried through live.py at all.
(function(){
  const homes = {};
  PROGS.forEach(p => { if (p.group) (homes[p.name] = homes[p.name] || new Set()).add(p.group); });
  const amb = Object.keys(homes).filter(n => homes[n].size > 1);
  results.push({section:'the corpus still contains an ambiguous stem', paint:true,
                ok: amb.length > 0, error:'nothing to test against'});
  if (!amb.length) return;
  const name = amb[0], groups = [...homes[name]];

  LIT.clear(); LIT_PATH.clear();
  // 1. ambiguous with no path -> light NOTHING.
  LIT.set('program:'+name, Date.now());
  results.push({section:'an ambiguous stem with no path lights nothing', paint:true,
                ok: litToolFolders().size === 0,
                error:[...litToolFolders().keys()].join(',')});

  // 2. ambiguous WITH a resolving path -> light exactly the one.
  LIT_PATH.set('program:'+name, 'c:/work/tools/'+groups[0]+'/'+name+'.py');
  const one = litToolFolders();
  results.push({section:'a path resolves an ambiguous stem to one folder', paint:true,
                ok: one.size === 1 && one.has(groups[0]),
                error:[...one.keys()].join(',')});

  // 3. a path that matches none of them -> still nothing.
  LIT_PATH.set('program:'+name, 'c:/somewhere/else/'+name+'.py');
  results.push({section:'an unresolving path still lights nothing', paint:true,
                ok: litToolFolders().size === 0,
                error:[...litToolFolders().keys()].join(',')});

  // 4. an UNambiguous stem needs no path at all.
  const solo = Object.keys(homes).find(n => homes[n].size === 1);
  LIT.clear(); LIT_PATH.clear();
  LIT.set('program:'+solo, Date.now());
  const s = litToolFolders();
  results.push({section:'an unambiguous stem lights without a path', paint:true,
                ok: s.size === 1, error:solo+' -> '+[...s.keys()].join(',')});
  LIT.clear(); LIT_PATH.clear();
})();

console.log('@@RESULT@@' + JSON.stringify(results));
"""

td = tempfile.mkdtemp()
harness = pathlib.Path(td) / "render.cjs"
harness.write_text(STUB + "\n" + js + "\n" + TAIL, encoding="utf-8")
proc = subprocess.run(["node", str(harness)], capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=120)

out = proc.stdout
m = re.search(r"@@RESULT@@(.*)", out)
if not m:
    # Keep the harness and print node's own words. A test that says only
    # "it did not run" and then deletes the evidence is a test you have to
    # debug twice - the second time by rebuilding what it already had.
    check("the harness ran to completion", False, "see below")
    print("\n--- node said ---")
    print((proc.stderr or out).strip()[:2500] or "(nothing)")
    print("\n--- harness kept for debugging ---")
    print(harness)
    print("\n=== SELFTEST COMPLETE - %d failure(s) ===" % len(FAILS))
    sys.exit(1)
shutil.rmtree(td, ignore_errors=True)

check("the harness ran to completion", True)
results = json.loads(m.group(1))

for r in results:
    if r.get("paint"):
        # Only the five bare section names get the suffix; everything else is
        # already a sentence and reads as gibberish with one appended.
        bare = r["section"] in ("skills", "prompts", "tools", "hooks", "programs", "groups")
        check("%s paints live state without throwing" % r["section"] if bare
              else r["section"],
              r["ok"], "%s %s" % (r.get("error", ""), r.get("where", "")))
    elif r.get("chars") is not None:
        check("%s renders without throwing" % r["section"], r["ok"],
              "%s %s" % (r.get("error", ""), r.get("where", "")))
        # An empty #list after a successful render means the function returned
        # early. That is the OTHER half of the same bug: no exception, no
        # content, and a tab that looks broken for a different reason. 100,
        # not 200: a from-scratch Groups tab legitimately renders just one
        # small "+ New Group" tile (194 chars) with zero groups yet, and every
        # other section's real corpus clears either floor by a wide margin.
        check("%s actually wrote cards" % r["section"], r["chars"] > 100,
              "%d chars" % r["chars"])
    else:
        check("%s renders without throwing" % r["section"], r["ok"],
              "%s %s" % (r.get("error", ""), r.get("where", "")))

check("no sort option throws",
      all(r["ok"] for r in results if "sort=" in r["section"]),
      "; ".join("%s: %s" % (r["section"], r.get("error"))
                for r in results if not r["ok"] and "sort=" in r["section"]))

# ---- the unconfigured page: no tools_root, no tools.json ----
# Every tab must still render, and Tools must say how to set it up rather
# than show a blank list.
BARE_TAIL = r"""
const out = [];
for (const [sec, fn] of [['skills',draw],['prompts',drawPrompts],['tools',drawTools],
                         ['hooks',drawHooks],['programs',drawPrograms],['groups',drawGroups]]) {
  const list = document.getElementById('list'); list.innerHTML = '';
  try { S.section = sec; fn(); out.push({sec, ok:true, html:list.innerHTML}); }
  catch (e) { out.push({sec, ok:false, error:e.message}); }
}
console.log('@@BARE@@' + JSON.stringify(out));
"""
td2 = tempfile.mkdtemp()
h2 = pathlib.Path(td2) / "bare.cjs"
h2.write_text(STUB + "\n" + js_bare + "\n" + BARE_TAIL, encoding="utf-8")
p2 = subprocess.run(["node", str(h2)], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=120)
m2 = re.search(r"@@BARE@@(.*)", p2.stdout)
check("the unconfigured page ran to completion", bool(m2), (p2.stderr or "")[:500])
if m2:
    for r in json.loads(m2.group(1)):
        check("unconfigured: %s renders without throwing" % r["sec"], r["ok"], r.get("error"))
        if r["sec"] == "tools" and r["ok"]:
            check("unconfigured: Tools explains how to set it up",
                  "tools_root" in r["html"] and "kit.config.json" in r["html"], r["html"][:200])
shutil.rmtree(td2, ignore_errors=True)

print("\n=== SELFTEST COMPLETE - %d failure(s) ===" % len(FAILS))
for f in FAILS:
    print("  " + f)
sys.exit(1 if FAILS else 0)
