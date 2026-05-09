# Analysis — Arrangement Editor

## Coverage

| Area | Spec'd | Implemented | Notes |
|---|---|---|---|
| Load / Save / Build pipeline | yes | yes | `routes.py:222 / 448 / 1439` |
| GP / MIDI / Drums / Keys imports | yes | yes | 6 import routes |
| Storage probe (Docker vs desktop) | yes | yes | `routes.py:54-79` |
| Undo / Redo | yes | yes | `S.history` |
| Snap-aware editing | yes | yes | `SNAP_VALUES` |
| Keys / piano-roll mode | yes | yes | `KEYS_PATTERN` |
| Live MIDI keys record | yes | yes | screen.html ●Record |
| YouTube audio import | yes | yes | yt-dlp subprocess |
| Tests | yes (open) | no | no fixtures, no harness |
| README | yes (open) | no | empty file |

## Drift

- **README is empty.** This is a multi-thousand-line plugin with rich features and zero user-facing docs in the repo. The Slopsmith MEMORY indexes it via `project_slopsmith_editor.md`, but the repo itself doesn't document install or workflow.
- **CLAUDE.md is the speckit stub.** Standard for spec-kit-onboarded repos; no drift, but it points at "the current plan" — these spec files satisfy that pointer.
- **`screen.html` declares many buttons that are conditionally `hidden`** (Build, +Drums, +Keys, ●Record, Sync). Visibility logic lives in `screen.js`; behaviour is correct but the toolbar can be visually crowded once a session is loaded. Not a bug, just dense UX.
- **`_sessions` has no documented eviction.** Sessions accumulate for the process lifetime. Long-running deployments may leak working dirs.

## Gaps

1. **No README.** Highest-priority documentation gap in the entire plugin set.
2. **No automated tests.** Import paths (GP/MIDI/Drums/Keys) and the build pipeline are entirely untested in-repo. A small synthetic GP fixture and a minimal PSARC fixture would catch regressions on `pack_psarc` shape changes.
3. **No build progress streaming.** Build is a single POST that may take seconds; the UI shows a spinner with no granularity. WebSocket progress (like discextract has) would help.
4. **No session conflict detection.** Two tabs editing the same song silently last-writer-wins on Build.
5. **No session TTL.** Sessions live until process restart.
6. **Firefox MIDI fallback messaging.** The `●Record` button is visible regardless of MIDI support; a disabled state with a tooltip explaining the Chrome/Edge requirement would prevent confusion.

## Recommendations

- **Highest value**: write a README. Even a 50-line "what it does + load/edit/build flow + supported imports" doc.
- **High value**: synthetic test fixtures for the 6 import routes + a known-good build round-trip. The import routes are pure server logic and easy to regression-test once fixtures exist.
- **Medium**: WebSocket-based build progress (already a pattern in the discextract plugin). Reuse the prior art.
- **Medium**: a session TTL (e.g. evict after 2h idle) + a `/api/plugins/editor/sessions/cleanup` admin route.
- **Low cost**: disable the `●Record` button on non-MIDI browsers with a tooltip.
- **Cosmetic**: split `screen.js` into logical modules (timeline draw, modals, MIDI, history) — 3277 lines in one IIFE is hard to navigate. Could be done without breaking the "single IIFE, no bundler" principle by concatenating in `plugin.json` (or accepting the in-file region markers).
