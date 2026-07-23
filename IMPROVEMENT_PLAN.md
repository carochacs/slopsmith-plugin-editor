# Improvement Plan — Arrangement Editor (slopsmith)

Derived from an in-depth comparison against `feedBack-plugin-editor`, a much
more mature fork of this same plugin (renamed for the "feedBack" host; frontend
split into 79 modules under `src/`, 275-file test suite, CI). Items below are
things the fork already solved, or gaps visible only by contrast with it.
Ordered by leverage vs. cost, not by chronology.

## P0 — Bug fixes (small, isolated, high value)

These are all cataloged in `KNOWN_ISSUES.md`. Cross-checking against the fork
showed only 3 of 8 got fixed there — the rest are unfixed in *both* repos,
which makes them good candidates to fix once here and port back upstream.

1. **Ctrl+Z/Ctrl+Y fire while a text input has focus** (`KNOWN_ISSUES.md` #2,
   `screen.js:1623-1632`). Add the same `!e.target.matches('input, select,
   textarea')` guard used by sibling shortcuts (e.g. `screen.js:1583-1605`).
2. **Stale note index after an arrangement switch** (`KNOWN_ISSUES.md` #4).
   `editorSelectArrangement` needs to call `hideContextMenu()`/`hideAddNote()`
   before switching, closing any popover that captured an index into the old
   arrangement's `notes()`.
3. **No double-submit guard on "Build CDLC"** (`KNOWN_ISSUES.md` #5,
   `window.editorBuild`, `screen.js:3239-3377`). Set the button `.disabled`
   (not just `classList`) for the duration of the poll loop.
4. **Popover/context-menu not clamped to viewport** (`KNOWN_ISSUES.md` #6,
   `screen.js:1750-1751`, `1805-1806`). Clamp `left`/`top` against
   `window.innerWidth/Height` minus the element's rendered size.
5. **`DPR` captured once at load** (`KNOWN_ISSUES.md` #7, `screen.js:51`).
   Re-read `window.devicePixelRatio` in `resizeCanvas()`, or add a
   `matchMedia('(resolution: ...)')` listener that calls it.

## P1 — Fixed in the fork; port the pattern

6. **No screen-leave cleanup for playback/MIDI recording**
   (`KNOWN_ISSUES.md` #1). The fork's fix: a single `MutationObserver` on
   `#plugin-editor`'s `class` attribute that reacts to *both* directions —
   gaining `.active` (existing `resizeCanvas()` call) and losing it (new: an
   idempotent teardown that stops playback and any in-progress MIDI take).
   Don't add a second observer; extend the existing one to branch on the
   transition direction.
7. **`drumEditMode` not reset on arrangement switch** (`KNOWN_ISSUES.md` #3).
   The fork's fix: move mode-flag resets (`drumEditMode`, and analogues like
   `tabViewMode` if/when added) into the *outer* UI-facing switch handler
   (the `<select onchange>` handler), not into the lower-level
   `editorSelectArrangement` — keep that function mode-agnostic so future
   mode flags don't need to be threaded through it individually.
8. **Escape only closes 3 of ~10 modals** (`KNOWN_ISSUES.md` #8). Introduce
   one shared `installModalKeyboard(modal, onClose)` helper (Escape + focus
   trap) and route every modal open/close through it, rather than wiring
   Escape ad hoc per modal. Note: the fork only rolled this out to
   dynamically-built modals and never finished migrating its static/legacy
   ones — don't stop halfway; apply it to all modals in `screen.html` in one
   pass since there's no legacy-markup excuse here yet.

## P2 — Undo/redo hardening

Both repos use the same command pattern (`exec()`/`rollback()` pairs pushed
onto `EditHistory`), so this is a maturity upgrade, not a rewrite:

9. Cap the undo stack (e.g. 500 entries, evicting oldest) — currently
   unbounded in `EditHistory` (`screen.js:865-883`).
10. Tag each command with the arrangement index it was executed against
    (`cmd._arrIdx`) and guard `doUndo`/`doRedo` against acting on a command
    from an arrangement that's no longer active. This is the structural fix
    underlying item #2 above, not just a point patch.
11. Wrap currently-direct `S` mutations in real command classes so they
    become undoable: section add/edit (`screen.js:1574-1583`), drum-tab hits
    (`~5219`), and BPM/tempo-offset rescale (`~2583-2596`). Right now undo
    silently doesn't cover these.
12. Add a cheap `editGen` counter (bumped on every mutation) and a
    `sessionDirty`/`markSessionDirty()` pair decoupled from the undo stack —
    useful for detecting "needs save" from non-undoable changes (MIDI
    takes, imports) that bypass `EditHistory` entirely.

## P3 — Session lifecycle (frontend)

13. Add a client-side "unsaved changes" guard before navigating away from
    the editor screen or closing the tab (`beforeunload` + an explicit
    `POST` to close the backend session), mirroring the fork's
    `guardSessionTransition()`/`disposeBackendSession()` pattern. Slopsmith
    currently has no equivalent — it relies solely on the backend's 5-minute
    TTL sweep (`routes.py:140-155`), which is good but not a substitute for
    an explicit close signal.

    **Do not remove or weaken the existing TTL eviction loop while doing
    this** — the fork actually dropped its own equivalent sweep, which is a
    regression on that side, not something to imitate.

## P4 — Chord / music-theory logic (`chord_analysis.py`)

14. Replace the hardcoded 15-entry `SHARP_KEYS` set (`chord_analysis.py:12-17`)
    with a formula derived from each key's relative-major position on the
    circle of fifths. This is what lets the fork's `theory.js` extend
    cleanly to modes (Dorian, Mixolydian, etc.) without enumerating every
    case by hand — worth doing here even without adding mode support yet,
    since it removes a maintenance trap.
15. Add the missing chord qualities the fork's naming table has and this
    one doesn't: `6`, `m6`, `mMaj7` (alongside the existing
    `CHORD_QUALITIES`, `chord_analysis.py:23-37`).
16. Add a bass-note tie-break for ambiguous voicings (e.g. m7 vs. 6,
    symmetric aug/dim7): prefer the interpretation whose root matches the
    sounding bass pitch class before falling back to `lowest_pc`.

    Two chord-model items are explicitly **not** recommended here: the
    fork's `chords.js` flatten/reconstruct/harmony-function round-trip is a
    bigger data-model change (chord templates re-linked by fret pattern,
    handshapes remapped through an index map) that only pays off if/when
    this repo's own chord-template + handshape editing grows in scope —
    don't import it speculatively.

## P5 — Testing & CI

17. Add a CI workflow (there is currently none) that just runs the existing
    `pytest tests/` — near-zero cost, catches regressions in
    `chord_analysis.py` and the difficulty-scoring helpers immediately.
18. Continue extracting pure, state-free logic out of `routes.py`'s
    `setup()`-nested closures into real top-level functions in
    `chord_analysis.py` or a new sibling module (loaded via
    `context["load_sibling"]`, per this repo's convention). Each extraction
    turns one `ast.parse`-and-`exec()` test (like the workaround in
    `tests/test_difficulty_keys.py`) into a normal import-based test.
19. On the frontend, adopt the fork's transitional testing trick for
    `screen.js` before attempting any large modularization: pick one pure,
    self-contained function (geometry/hit-test/chord math), regex- or
    brace-extract it into a `node --test` file, and only promote it to a
    real module export later. This gets test coverage started without
    requiring the IIFE to be broken up first.

## Explicitly out of scope for now

- Full modularization of `screen.js` into `src/*.js` files — a large,
  multi-week effort the fork undertook; nothing above requires it as a
  prerequisite.
- Persistent (disk-backed) session storage — neither repo has this; not
  worth adding unilaterally without a concrete crash-recovery requirement.
