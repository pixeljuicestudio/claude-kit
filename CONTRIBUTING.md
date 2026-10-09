# Contributing

Thanks for helping. A few ground rules keep Kit easy to run anywhere:

- **Standard library only.** No new dependencies for the server or scanners.
  (`icon.py` and Pillow are the one optional exception.)
- **No personal paths.** Anything machine-specific goes through `config.py`
  and `kit.config.json`, with a sensible default or a "not configured" state.
- **Never crash the page on missing data.** A missing folder, file or setting
  should render as empty or "not configured", never as a traceback.
- **Measured, not guessed.** If Kit cannot know something, the card should say
  so rather than show a plausible guess.
- **Run the tests** before opening a pull request:

  ```sh
  for t in test_*.py; do python "$t" || echo "FAILED: $t"; done
  ```

  `test_render.py` needs Node. Add or extend a `test_*.py` for any behaviour
  you change; tests that need tools or prompts should build a temporary
  fixture rather than read your own `~/.claude`.

## Adding prompts to the shipped set

Example prompts live in `prompts/` (format in `prompts/README.md`). Keep them
original or clearly licensed for redistribution, and credit the source in
`origin:`.

## Reporting a bug

Include your OS, Python version, `python config.py` output (it contains no
secrets), and any traceback from the terminal running `serve.py`.
