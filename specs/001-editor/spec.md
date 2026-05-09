# Feature Specification: Arrangement Editor

**Plugin id**: `editor`
**Status**: Shipped (v1.0.2)
**Type**: Full-screen plugin (`nav.screen = "editor"`) + 17 backend routes

## Summary

A DAW-style timeline editor for Rocksmith arrangements. Imports PSARC, sloppak, Guitar Pro (3-7), and MIDI; lets users edit notes/beats/sections on a 6-string guitar chart or piano-roll keys chart; saves to a session working dir; builds a finished CDLC PSARC into the user's DLC directory.

## User Stories

### US1 — Edit an existing CDLC (Priority: P1)

**Given** a PSARC or sloppak in my library,
**When** I click "Load" → pick a song → choose an arrangement,
**Then** the song appears as a scrollable timeline (waveform, lanes, beats, sections), I can select / drag / pitch-shift / delete notes, undo/redo, and save back into the session.

### US2 — Build a finished CDLC (Priority: P1)

**Given** I've edited and saved an arrangement,
**When** I click "Build CDLC",
**Then** the backend repacks via `lib.patcher.pack_psarc` and writes a `_p.psarc` into my DLC directory; success/failure is surfaced in the UI.

### US3 — Create from scratch (Priority: P2)

**Given** I have an audio file (or a YouTube URL) and (optionally) album art,
**When** I click "Create" → fill in title/artist/audio/art,
**Then** a new session is created with empty arrangements; I can add Lead/Rhythm/Bass/Drums/Keys arrangements and start authoring.

### US4 — Import from Guitar Pro / MIDI (Priority: P2)

**Given** a `.gp3/.gp4/.gp5/.gpx/.gp/.mid` file,
**When** I import it via the matching `/import-gp` or `/import-midi` route (or convert via `/convert-gp`),
**Then** notes / beats / sections / tempo populate the timeline and the chart re-renders.

### US5 — Author a Keys arrangement (Priority: P2)

**Given** I add a Keys arrangement (or open one whose name matches `/^(keys|piano|keyboard|synth)/i`),
**When** I edit notes,
**Then** the chart switches to piano-roll mode with `PIANO_LANE_H = 10 px` per semitone and a per-arrangement MIDI range tracker.

### US6 — Record a Keys arrangement live from MIDI (Priority: P3)

**Given** I have a MIDI keyboard connected (Chrome / Edge),
**When** I click "● Record" and play,
**Then** notes are captured live into a new Keys arrangement.

### US7 — Tempo / offset / snap (Priority: P2)

**Given** I want to adjust timing,
**When** I change BPM, offset, or snap,
**Then** notes/beats rescale (BPM), shift (offset), or align (snap) accordingly; all changes are undoable.

## Functional Requirements

- **FR1**: Plugin manifest mounts a full-screen at `editor` (`plugin-editor`).
- **FR2**: Backend session lifecycle: `/load` opens, `/save` persists into session dir, `/build` writes `_p.psarc` into DLC dir.
- **FR3**: Storage location MUST probe `slopsmith/static/` writability + `app.js` sentinel; fallback to `config_dir/editor_cache` served at `/api/plugins/editor/cache/...`.
- **FR4**: Both URL prefixes (`/static/...`, `/api/plugins/editor/cache/...`) MUST resolve on read-back so cross-upgrade sessions survive.
- **FR5**: All mutating edits MUST push through `S.history` (undo/redo).
- **FR6**: `KEYS_PATTERN` auto-switches the chart to piano-roll mode.
- **FR7**: BPM change MUST rescale notes/beats; offset change MUST shift them.
- **FR8**: `screen.js` MUST run as a single IIFE; no leaks to `window` beyond documented globals.
- **FR9**: Routes MUST be authenticated under the same plugin guard Slopsmith core applies (single-user model — no extra auth in plugin).

## Non-Functional Requirements

- Chrome/Edge required for MIDI recording; viewing/editing works in Firefox.
- DPR-aware canvas; 60 fps scroll target.
- Server-side imports MUST be tolerant of malformed GP/MIDI input — error JSON, not 500s.

## Out of Scope

- Multi-user / collaborative editing.
- Cloud sync.
- Cross-platform PSARC variants (`_m.psarc`).
- Generating arrangements from audio (no auto-transcribe).

## Open Items

- [NEEDS CLARIFICATION] Behaviour when session is abandoned mid-build — is the session dir cleaned up? When?
- [NEEDS CLARIFICATION] Concurrent edits on the same song from two tabs — last-writer-wins or session-conflict error?
