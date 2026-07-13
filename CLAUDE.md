# Arrangement Editor — AI Agent Guide

Full design reference: [`specs/001-editor/plan.md`](specs/001-editor/plan.md). Read it first for architecture, route table, file map, and integration points with Slopsmith core.

## Quick orientation

- **Frontend** — single IIFE in `screen.js` (~3 300 lines). All state lives in module object `S`. Canvas-based timeline with two chart modes: guitar (6 lanes, `LANE_H = 44 px`) and piano-roll (semitone rows, `PIANO_LANE_H = 10 px`; triggered by `KEYS_PATTERN`).
- **Backend** — `routes.py` (~1 900 lines), 17 FastAPI routes. Module-scope `_sessions` dict stores live sessions; no persistent session store.
- **Sibling import** — `chord_analysis.py` is loaded via `context["load_sibling"]("chord_analysis")`, not a bare import.
- **Storage probe** — at startup, `routes.py:54–79` checks whether `slopsmith/static/app.js` is present and writable; if so files are served from `static/`, otherwise from `config_dir/editor_cache` at `/api/plugins/editor/cache/…`.

## Known open issues

See `specs/001-editor/tasks.md` (OPEN items):
- Build progress is a single blocking HTTP request — no streaming feedback.
- Sessions survive for process lifetime; no TTL or cleanup.
- Two-tab edit conflict: last-writer-wins.
- No test harness for import pipelines.
- Firefox lacks Web MIDI — the recording modal should say so.

## Conventions

- All backend output via `context["log"]`, never `print()`.
- New sibling modules must be loaded via `context["load_sibling"]`, not bare `import`.
- UI changes that add Tailwind classes must be reflected in Slopsmith core's `scripts/build-tailwind.sh` (or the plugin must ship its own `assets/plugin.css` via the `styles` manifest key).
