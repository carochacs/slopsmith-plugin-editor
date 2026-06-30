# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Build progress / streaming feedback (currently a single blocking request)
- Build retry on transient errors
- Firefox-not-supported notice in the MIDI recording modal
- Session TTL / cleanup (sessions currently survive for process lifetime)
- Two-tab edit conflict detection (currently last-writer-wins)
- Test harness for import pipelines

## [1.1.0] - 2026-06-30

### Added
- **Dynamic difficulty generation** — auto-generates Easy, Medium, and Hard phrase-difficulty ladders from a Master arrangement using chord density heuristics. Accessible via the `+Difficulties` toolbar button and the "Add Difficulties" library card button on GP-imported sloppaks.
- **Chord and key analysis** — bundled `chord_analysis.py` (loaded via `load_sibling` to avoid `sys.path` collisions) identifies chord shapes and detects the arrangement key for use during difficulty generation and auto-naming.
- **Extended-range support** — 7/8-string guitar and 5/6-string bass lane layouts.
- **Drum tab editor** — visual drum-hit grid editor for `drum_tab.json` inside sloppak packages; import drum tracks from Guitar Pro or MIDI files.
- **Keys / piano-roll mode** — arrangements matching `/^(keys|piano|keyboard|synth)/i` render as a semitone-per-row piano roll with MIDI range auto-tracking. Includes `+ Keys` toolbar button and live MIDI recording (Chrome/Edge).
- **Extended GP/MIDI import** — separate import routes for guitar, bass, keys, and drums tracks from Guitar Pro files; MIDI import for both guitar and keys arrangements.
- **YouTube audio import** — download and trim audio from a URL via `yt-dlp` + `ffmpeg` directly into a session.
- **Album art upload** — attach custom artwork to a session before building.

### Fixed
- `generate-difficulties` erased notes on subsequent playback (phrase-note wire format not applied before save).
- Undefined `markDirty` call in `editorGenerateDifficulties` caused a silent crash.
- `notes()`/`chords()` accessor methods not used in `generate-difficulties` and group selection, causing stale data.
- `chord_analysis` bundled as a plugin sibling to avoid a hard dependency on `lib/`.

## [1.0.2] - 2025-11-01

### Added
- Initial release.
- Load and edit existing `.psarc` and `.sloppak` songs from the DLC library.
- Scrollable timeline with waveform, string lanes, beat grid, and section markers.
- Note selection, drag (snap-aware), pitch-shift, and delete.
- Full undo/redo history (`Ctrl+Z` / `Ctrl+Y`).
- Save edits back to `.psarc` or `.sloppak` in place (`.bak` backup created).
- Build a finished `_p.psarc` into the user's DLC directory.
- Create new songs from scratch: audio upload, art upload, metadata entry.
- Guitar Pro import (`.gp3` / `.gp4` / `.gp5` / `.gpx` / `.gp`) for guitar and bass tracks.
- MIDI import for guitar arrangements.
- Add and remove arrangements per song.
- BPM rescale, offset nudge (±10 ms), and snap selector (1/1 → 1/16, off).
- Storage probe: uses `static/` when running inside a live Slopsmith tree, falls back to `config_dir/editor_cache` otherwise.
