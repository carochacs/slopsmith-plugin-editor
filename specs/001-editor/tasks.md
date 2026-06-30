# Tasks — Arrangement Editor

Status legend: **DONE** = shipped in v1.0.2; **OPEN** = candidate work. `[P]` = parallel-safe.

## US1 — Edit existing CDLC

- **DONE** Load modal + song picker — `screen.js:editorShowLoadModal`, `editorSelectArrangement`
- **DONE** Backend `/load` route + session creation — `routes.py:222`
- **DONE** Timeline canvas render (waveform + lanes + beats + sections) — `screen.js` draw loop
- **DONE** Note selection + drag (snap-aware) — `S.drag`, `S.snapIdx` paths
- **DONE** Undo / Redo (`Ctrl+Z`/`Ctrl+Y`) — `S.history`
- **DONE** Save — `screen.js:editorSave`, `routes.py:448`

## US2 — Build CDLC

- **DONE** Build button + handler — `screen.html:25`, `screen.js:editorBuild`
- **DONE** Backend `/build` route → `lib.patcher.pack_psarc` — `routes.py:1439`
- **DONE** Output path = user's DLC dir
- **DONE** [P] Build progress / streaming feedback — `/build` now returns `build_id`; poll `/build-progress/{build_id}`
- **DONE** [P] Build retry on transient errors — frontend retries the start request up to 3× with exponential backoff

## US3 — Create from scratch

- **DONE** Create modal — `screen.js:editorShowCreateModal`
- **DONE** Audio upload — `routes.py:733`
- **DONE** Art upload — `routes.py:722`
- **DONE** YouTube audio import (yt-dlp) — `routes.py:744`
- **DONE** Add arrangement / remove arrangement — `routes.py:1342`, `routes.py:1404`

## US4 — Import GP / MIDI

- **DONE** GP import — `routes.py:799`
- **DONE** GP convert (full conversion) — `routes.py:961`
- **DONE** MIDI import (guitar) — `routes.py:824`
- **DONE** MIDI import (keys) — `routes.py:869`
- **DONE** Drums import — `routes.py:1217`
- **DONE** [P] Better error reporting from malformed GP files — `struct.error`/`UnicodeDecodeError` surface specific messages; all exceptions include the exception type name

## US5 — Keys / piano-roll authoring

- **DONE** `KEYS_PATTERN` triggers piano-roll mode — `screen.js:42`
- **DONE** `PIANO_LANE_H` semitone-per-row rendering
- **DONE** Per-arrangement MIDI range tracker (`pianoRange`)
- **DONE** `+ Keys` button — `screen.html:27`
- **DONE** Keys import (saved live recording) — `routes.py:1087`

## US6 — Live MIDI keys recording

- **DONE** Record button + modal — `screen.html:28`
- **DONE** Web MIDI capture (Chrome/Edge) — `screen.js` MIDI block
- **DONE** Save recording into session as new keys arrangement
- **DONE** Document Firefox-not-supported state in the modal UI — `#editor-record-midi-no-webmidi` shown when `!navigator.requestMIDIAccess` (screen.html:517, screen.js:4651)

## US7 — Tempo / offset / snap

- **DONE** BPM input rescales notes/beats — `screen.js:editorSetBPM`
- **DONE** Offset nudges (±10 ms) — `screen.js:editorNudgeOffset`, `editorApplyOffset`
- **DONE** Snap selector (1/1 → 1/16, off) — `screen.js:editorSetSnap`
- **DONE** Tempo sync to audio — `screen.js:editorSyncTempo`

## Cross-cutting

- **DONE** Storage probe (legacy static vs cache fallback) — `routes.py:54-79`
- **DONE** Both URL prefixes resolve on read-back — `routes.py:48-52`
- **DONE** IIFE scoping; minimal window leakage
- **DONE** [P] Session TTL / cleanup — startup task evicts sessions untouched > 1 h; deletes temp dirs for psarc/create sessions
- **DONE** [P] Two-tab edit conflict detection — `_version` counter on each session; save returns 409 on mismatch, frontend shows conflict message
- **DONE** [P] Test harness for `chord_analysis.py` — `tests/test_chord_analysis.py`, 31 cases covering `fret_to_midi` (7), Pearson correlation (3), note naming (3), key name (4), chord naming (10), and `detect_key` (4). Run with `python -m pytest tests/test_chord_analysis.py`. Import pipeline tests omitted — all routes require binary GP/MIDI fixtures.
- **DONE** README — install steps, format table, feature overview

## Documentation

- **DONE** README — install + workflow walkthrough, format support table
- **DONE** [P] CHANGELOG / version history — `[Unreleased]` section populated
- **DONE** [P] CLAUDE.md — points at `specs/001-editor/plan.md`
