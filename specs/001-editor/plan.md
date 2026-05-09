# Implementation Plan — Arrangement Editor

## Architecture

**Frontend** (`screen.html` 363 lines, `screen.js` 3277 lines):
- Single IIFE in `screen.js` with module state object `S` (see `screen.js:48`).
- HTML = top bar (back / song info / arrangement select), toolbar (Load, Create, Save, Build, +Drums, +Keys, ●Record, Undo/Redo, Play, Zoom, BPM, Offset, Snap), main canvas, modals.
- Canvas-based timeline rendering: waveform + lanes + beats + sections + cursor. DPR-aware (`DPR = window.devicePixelRatio || 1`).
- Two chart modes:
  - **Guitar** (default): 6 lanes ordered low-E bottom → high-e top, `STRING_COLORS` palette, `LANE_H = 44 px`.
  - **Keys / Piano-roll**: triggered by `KEYS_PATTERN = /^(keys|piano|keyboard|synth)/i`, `PIANO_LANE_H = 10 px` per semitone, MIDI range tracker.
- Snap values: `[1, 0.5, 0.25, 0.125, 0.0625, 0]` = 1/1 to 1/16, off (`screen.js:28`).
- Undo/Redo via `S.history` (every mutating edit pushes pre-state).
- MIDI live recording in Chrome/Edge for keys arrangements.

**Backend** (`routes.py` 1924 lines, 17 routes):

| Route | Purpose |
|---|---|
| `GET /api/plugins/editor/cache/{name:path}` | serve files from desktop fallback storage |
| `GET /api/plugins/editor/songs` | list importable songs |
| `POST /api/plugins/editor/load` | open a song into a new session |
| `POST /api/plugins/editor/save` | persist edits into session dir |
| `POST /api/plugins/editor/upload-art` | attach album art to session |
| `POST /api/plugins/editor/upload-audio` | attach audio file to session |
| `POST /api/plugins/editor/youtube-audio` | yt-dlp → cache → session audio |
| `POST /api/plugins/editor/import-gp` | import Guitar Pro into current session |
| `POST /api/plugins/editor/import-midi` | import MIDI into current session |
| `POST /api/plugins/editor/import-keys-midi` | import keys MIDI |
| `POST /api/plugins/editor/convert-gp` | full GP→arrangement conversion (new session seed) |
| `POST /api/plugins/editor/import-keys` | finalise live-recorded keys arrangement |
| `POST /api/plugins/editor/import-drums` | import drum chart |
| `POST /api/plugins/editor/remove-arrangement` | drop an arrangement from session |
| `POST /api/plugins/editor/add-arrangement` | add empty arrangement to session |
| `POST /api/plugins/editor/build` | repack session → `_p.psarc` in DLC dir |

Storage path probe at startup (`routes.py:54-79`):
1. If `slopsmith/static/app.js` sentinel exists AND dir is writable → use `static/`.
2. Else fall back to `config_dir/editor_cache`, served at `/api/plugins/editor/cache/...`.

Session state: module-scope `_sessions` dict in `routes.py`.

## Integration Points (Slopsmith core)

| Surface | How used |
|---|---|
| `context['config_dir']` | desktop fallback storage location |
| `context['get_dlc_dir']` | build output destination |
| `lib.song.load_song`, `phrase_to_wire` | song model / wire format |
| `lib.psarc.unpack_psarc` | open existing CDLC |
| `lib.patcher.pack_psarc` | build new CDLC |
| `lib.audio.find_wem_files`, `convert_wem` | extract / convert audio |
| `lib.sloppak` | sloppak format support |
| `yt-dlp` (subprocess) | YouTube audio import |
| `window.showScreen('editor')` | navigation entry |
| Tailwind classes from core stylesheet | UI styling |

## File Map

| Path | Purpose | Lines |
|---|---|---|
| `plugin.json` | manifest | ~10 |
| `screen.html` | toolbar + canvas + modals scaffold | 363 |
| `screen.js` | timeline editor, undo, MIDI record, modals | 3277 |
| `routes.py` | 17 routes, storage probe, session lifecycle | 1924 |

## Tech Stack

- Python: FastAPI, asyncio, subprocess (yt-dlp), zipfile, ElementTree.
- JS: vanilla, Web Audio API, Web MIDI API, canvas 2D.
- Audio: WEM extraction via `lib.audio.find_wem_files` + `convert_wem`.

## Out-of-Plan / Won't Build

- Multi-user / collaborative editing.
- Cloud sync.
- Auto-transcription from audio.
- Cross-platform PSARC (`_m.psarc`).
