# Arrangement Editor — AI Agent Guide

Full design reference: [`specs/001-editor/plan.md`](specs/001-editor/plan.md). Read it first for architecture, route table, file map, and integration points with Slopsmith core.

## Quick orientation

- **Frontend** — single IIFE in `screen.js` (~5 550 lines). All state lives in module object `S`. Canvas-based timeline with two chart modes: guitar (6 lanes, `LANE_H = 44 px`) and piano-roll (semitone rows, `PIANO_LANE_H = 10 px`; triggered by `KEYS_PATTERN`).
- **Backend** — `routes.py` (~4 530 lines), 27 FastAPI routes. Module-scope `_sessions` dict stores live sessions; no persistent session store.
- **Sibling import** — `chord_analysis.py` is loaded via `context["load_sibling"]("chord_analysis")`, not a bare import.
- **Storage probe** — at startup, `routes.py:54–79` checks whether `slopsmith/static/app.js` is present and writable; if so files are served from `static/`, otherwise from `config_dir/editor_cache` at `/api/plugins/editor/cache/…`.

See `specs/001-editor/tasks.md` for shipped/candidate work — all items tracked there as of this writing are marked DONE.

## Conventions

- All backend output via `context["log"]`, never `print()`.
- New sibling modules must be loaded via `context["load_sibling"]`, not bare `import`.
- UI changes that add Tailwind classes must be reflected in Slopsmith core's `scripts/build-tailwind.sh` (or the plugin must ship its own `assets/plugin.css` via the `styles` manifest key).

## Working with CodeRabbit

CodeRabbit reviews PRs on this repo automatically. `.coderabbit.yaml` tells it about the conventions above (`context["log"]`, `load_sibling`, minimal docstrings, the dual-path storage probe) so it doesn't re-flag deliberate decisions as findings — read that file before assuming a review comment is wrong. When working a PR here:
- After pushing a fix, give CodeRabbit a pass (`@coderabbitai review` if it doesn't auto-trigger) and treat its comments like any other reviewer's — fix or reply with a reason, don't silently ignore.
- Resolve review threads as fixes land so the PR reflects live state.
- Don't let CodeRabbit approval substitute for a human merge decision.
