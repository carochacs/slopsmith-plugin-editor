# Slopsmith — Arrangement Editor Plugin

DAW-style timeline editor for Rocksmith CDLC. Runs as a full-screen plugin inside [Slopsmith](https://github.com/carochacs/slopsmith).

## What it does

**Load & edit existing songs**
- Opens `.psarc` and `.sloppak` files from your DLC library
- Scrollable timeline with waveform, string lanes, beat grid, and section markers
- Select, drag, pitch-shift, and delete notes
- Full undo/redo history

**Guitar & bass charts**
- 6-string guitar and 4-string bass layouts (colour-coded per pitch label)
- Extended-range support: 7/8-string guitar and 5/6-string bass
- Per-note techniques: bend, slide, hammer-on/pull-off, harmonic, palm mute, mute, tremolo, accent, tap

**Keys / piano-roll mode**
- Any arrangement named Keys, Piano, Keyboard, or Synth renders as a piano-roll chart
- Live MIDI recording from a connected keyboard (Chrome/Edge)
- Import piano tracks from Guitar Pro files or MIDI files

**Drum editor**
- Visual drum-hit grid editor (`drum_tab.json` inside sloppak)
- Import drum tracks from Guitar Pro files or MIDI files

**Import sources**
- Guitar Pro files (`.gp3` / `.gp4` / `.gp5` / `.gpx` / `.gp`) — guitar, bass, keys, and drum tracks
- MIDI files (`.mid` / `.midi`) — any track as Keys arrangement
- YouTube URL — downloads and trims audio via yt-dlp + ffmpeg

**Arrangement management**
- Add and remove arrangements per song
- Auto-generate Easy/Medium/Hard difficulties from a Master arrangement
- Auto-name unnamed chord templates using key detection

**Save & build**
- Save edits back to `.psarc` or `.sloppak` in place (original is backed up as `.bak`)
- Convert a PSARC session to `.sloppak` to preserve extended-range data
- Replace audio track on a loaded sloppak session
- Build a finished `_p.psarc` into your DLC directory

**Create from scratch**
- Start with an audio file (uploaded or from YouTube) and optional album art
- Enter song metadata (title, artist, album, year)
- Add arrangements manually or import from Guitar Pro / MIDI

## Supported formats

| Format | Load | Save | Build PSARC |
|--------|------|------|-------------|
| `.psarc` | ✓ | ✓ | ✓ |
| `.sloppak` (zip) | ✓ | ✓ | ✓ |
| `.sloppak/` (dir) | ✓ | ✓ | ✓ |
| Guitar Pro (gp3–gp, gpx) | import only | — | — |
| MIDI (.mid/.midi) | import only | — | — |

## Requirements

- Python 3.10+
- `yt-dlp` and `ffmpeg` on PATH (for YouTube audio download)
- Chrome or Edge for live MIDI recording (Web MIDI API)
- Slopsmith core with the plugin system

Install Python dependencies:

```
pip install -r requirements.txt
```

## Plugin metadata

| Field | Value |
|-------|-------|
| id | `editor` |
| name | Arrangement Editor |
| version | 1.1.0 |
| nav label | Editor |
