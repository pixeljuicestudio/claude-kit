# Security

Kit is a local tool. This page says exactly what it reads, what it writes,
what it can start, and who can talk to it, so you can decide whether to run it
without auditing the code yourself.

## Who can reach it

- The server binds to **127.0.0.1** only. Nothing on your network can connect.
- Every request must carry a `Host` of `127.0.0.1:<port>` or
  `localhost:<port>`. This blocks DNS-rebinding, where a website points its own
  name at your machine to read the replies.
- A browser request with an `Origin` must come from Kit's own page. Any other
  website open in your browser gets **403**, so a page you visit cannot star,
  toggle or launch anything.
- `test_security.py` checks all three on every run.

## What it reads

- `~/.claude/skills`, `~/.claude/plugins`, `~/.claude/settings.json`: the
  skill and hook inventory.
- `~/.claude/projects/*.jsonl`: Claude Code transcripts, for usage counts and
  the live view. Read only; nothing is sent anywhere.
- Your `prompts/` folder, and the folders you name in `kit.config.json`
  (`tools_root`, `extra_skill_dirs`, `reference_dir`).

## What it writes

- **One field in your skills:** the `disable-model-invocation` flag in a
  `SKILL.md` under `~/.claude/skills`, when you click Auto/Manual. Nothing
  else in a skill is touched; Kit never moves, deletes or creates skill
  folders and never writes `settings.json`.
- Its own small JSON files beside the code (`favourites.json`, `tags.json`,
  `groups.json`, ...), all gitignored.
- `mark_launchers.py`, only if you run it yourself, adds a one-line marker to
  `.bat` launchers under `tools_root`.

## What it can start

- **Launch** on a Tools card runs the one `.bat`/`.cmd`/`.exe` that the scan
  found at the top of that tool's folder. The page sends a tool *name*; the
  server looks up the path itself and re-checks it is inside `tools_root`. A
  path is never accepted from a request.
- **Open reference** plays a prompt's reference video under the same rule,
  inside `reference_dir`.
- `mode_sync_script`, if you configure one, runs after a toggle.
- All subprocesses are started with argument lists, never through a shell.

## What touches the network

Nothing, unless you configure the optional Notion prompt database. Then the
server calls the Notion API with the `NOTION_TOKEN` from your environment. The
token is never read from or written to a file.

## Prompts and skills from other people

A prompt or skill is someone else's text. Kit escapes it before it reaches the
page, including text that tries to close the page's `<script>` tag, so a
shared prompt cannot run code in Kit. Still read a skill before you install
it: a skill instructs Claude, and Kit cannot judge what it tells Claude to do.

## Reporting a problem

Open an issue at
[github.com/pixeljuicestudio/claude-kit/issues](https://github.com/pixeljuicestudio/claude-kit/issues).
For anything you would rather not post publicly, use GitHub's
**Report a vulnerability** button on the repo's Security tab.
