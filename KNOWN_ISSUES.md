# Known UI Issues — Arrangement Editor

Findings from a frontend UI-bug audit of `screen.js` / `screen.html` (2026-07-22). Ranked by severity/confidence within each area. No code changes have been made — this is a catalog for follow-up work.

## 1. No screen-leave cleanup for playback/MIDI recording (High)

There is no hook for losing the `#plugin-editor` `.active` class (only a `MutationObserver` for *gaining* it, `screen.js:3517-3524`, used to call `resizeCanvas()`). `startPlayback`/`stopPlayback` (`screen.js:1901-1923`) are only ever triggered by explicit UI actions.

**Failure scenario:** User hits Play (or starts a MIDI take), then clicks "Back" (`screen.html:5`, a bare `onclick="showScreen('home')"`) to leave the editor. The `AudioContext` is never suspended, the buffer source never stopped, and the `playbackTick` rAF loop (`screen.js:1925-1953`) keeps running against a hidden canvas indefinitely. A MIDI take in progress keeps recording notes into a session the user can no longer see.

## 2. Ctrl+Z / Ctrl+Y fire while a text input has focus (High)

`onKeyDown` (`screen.js:1623-1632`). Most other shortcuts in the same function guard with `!e.target.matches('input, select, textarea')` (e.g. lines 1583-1605, 1633-1650, 1656, 1676) — the undo/redo branches don't.

**Failure scenario:** User is editing BPM, Title/Artist, or a YouTube-URL field and presses Ctrl+Z expecting native text-field undo. Instead `editorUndo()` fires and `e.preventDefault()` blocks native undo — an unrelated note/chart edit is silently reverted from history while the text field is untouched, with no visible indication anything happened.

## 3. `drumEditMode` not reset on arrangement switch (High)

`editorSelectArrangement` (`screen.js:2596-2603`) clears `S.sel`, re-flattens chords, redraws — but never touches `S.drumEditMode`/`S.drumSel`. `draw()` (`screen.js:430-446`) branches purely on `S.drumEditMode && S.drumTab`, ignoring `S.currentArr`.

**Failure scenario:** User enters Drum Edit mode, then switches the arrangement dropdown to e.g. "Lead" guitar. Toolbar buttons update to reflect the guitar arrangement, but the canvas keeps rendering the drum-hit grid — controls and chart visibly disagree about what's being edited.

## 4. Context menu / Add-Note popover capture a stale note index (Medium-High)

Same root cause as #3 — `editorSelectArrangement` never calls `hideContextMenu()` / `hideAddNote()`. `showContextMenu` (`screen.js:1709-1753`) and `promptFret`/`promptBend`/`promptSlide` (`screen.js:1759-1793`) close over a captured note `idx` and act on `notes()[idx]` at click time.

**Failure scenario:** User right-clicks a note (menu captures `idx`), switches arrangements via the dropdown, then chooses a menu action ("Change Fret…", "Delete"). It indexes into the *new* arrangement's `notes()` array with the *old* `idx` — silently mutating an unrelated note, or throwing if `idx` is now out of range.

## 5. "Build CDLC" has no double-submit guard (Medium)

`window.editorBuild` (`screen.js:3239-3377`) polls `/api/plugins/editor/build-progress/:id` in a loop (lines 3327-3349). The button is only ever shown/hidden via `classList`, never `.disabled`.

**Failure scenario:** Double-click (or a second click mid-poll) starts two concurrent build+poll loops racing to overwrite `#editor-status`. The backend's duplicate-output-dir guard then surfaces a confusing "output directory already exists" error instead of the double-click being prevented client-side.

## 6. Context menu / Add-Note dialog not clamped to viewport (Medium)

`showContextMenu` (`screen.js:1750-1751`) and `showAddNote` (`screen.js:1805-1806`) position directly from `e.clientX`/`e.clientY` with no clamping.

**Failure scenario:** Right-click or double-click near the right/bottom edge of a maximized/wide window renders the popup partially or fully off-screen — its Add/Cancel/technique buttons become unreachable.

## 7. `DPR` captured once at script load (Low-Medium)

`const DPR = window.devicePixelRatio || 1;` (`screen.js:51`) is used in `resizeCanvas` (`screen.js:2511-2512`) and `draw()` (`screen.js:432-435`) but never refreshed.

**Failure scenario:** Dragging the browser window to a different-DPI monitor, or changing OS/browser zoom, leaves the canvas rendering at the stale pixel ratio — visibly blurry until a full reload.

## 8. Escape only closes 3 of ~10 modals (Low)

The global Escape handler (`screen.js:1853-1863`) only calls `hideAddNote()`, `hideContextMenu()`, `editorHideLoadModal()`. Create, Replace-Audio, Add-Drums, Add-Keys, Add-Guitar, Strings, Save-Format, Sync, and Record-MIDI modals aren't wired to Escape.

**Failure scenario:** User opens any modal besides Load and presses Escape expecting it to close (as Load does) — nothing happens.

---

*Verified clean:* the Firefox Web MIDI gap is handled correctly — both the toolbar Record button (`screen.js:2155-2165`) and the recording modal (`screen.js:4718-4720`, `screen.html:517`) branch on `!navigator.requestMIDIAccess` and show a clear "use Chrome or Edge" message.
