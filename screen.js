/* Slopsmith Arrangement Editor — DAW-style timeline note editor */

(function () {
'use strict';

// ════════════════════════════════════════════════════════════════════
// Constants
// ════════════════════════════════════════════════════════════════════

// Highway colours, keyed by pitch *label* (the same labels `laneLabels()`
// emits) so colours stay locked to a string's note regardless of the
// arrangement's string count. A 4-string bass G/D/A/E gets
// orange/blue/yellow/red just like the same pitches on a 6-string
// guitar. Extended-range strings (7/8-guitar's low B/F#, 6-bass's high C)
// reuse the dusty-pink/steel-blue accents.
const STRING_LABEL_COLORS = {
    'E':  '#FC3A51', // low E   — red
    'A':  '#FFC600', // A       — yellow
    'D':  '#3FAAFF', // D       — blue
    'G':  '#FF8A00', // G       — orange
    'B':  '#58D263', // B       — green (guitar string 4)
    'e':  '#C473FF', // high e  — purple
    'B↓': '#E07A8A', // 7-string low B          — dusty pink
    'C↑': '#E07A8A', // 6-string bass high C    — dusty pink
    'F#↓': '#8AA0B8',// 8-string low F#         — steel blue
};

// Cached per-frame alongside `lanes()` to avoid re-allocating
// `laneLabels()` per note inside drawNotes / drawLabels.
let _laneLabelsCacheValue = null;
function colorForLane(l) {
    // `laneLabels()` is low → high (string-index order); strToLane
    // converts string index → lane. During draw() the cache is hot
    // (set once per frame), so per-note colorForLane reads a single
    // index rather than re-running the label computation.
    const labels = _lanesCacheActive && _laneLabelsCacheValue
        ? _laneLabelsCacheValue
        : laneLabels();
    const lbl = labels[laneToStr(l)];
    return STRING_LABEL_COLORS[lbl] || '#888';
}

let WAVEFORM_H = 70;
let LANE_H = 44;
const MAX_LANES = 8;
let BEAT_H = 24;
const LABEL_W = 52;
const MIN_NOTE_W = 18;
const NOTE_PAD = 3;
const SNAP_VALUES = [1, 0.5, 0.25, 0.125, 0.0625, 0]; // 1/1 … 1/16, off
const DPR = window.devicePixelRatio || 1;

// ── Piano roll constants ────────────────────────────────────────────
const PIANO_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
const PIANO_OCTAVE_COLORS = [
    '#ff4466', '#ff8844', '#ffcc33', '#66dd55', '#44ccaa',
    '#44aaff', '#7766ff', '#cc55ff', '#ff55aa', '#aaaaaa',
];
let PIANO_LANE_H = 10;  // pixels per MIDI semitone
let pianoRange = { lo: 36, hi: 96 }; // MIDI range, updated per arrangement
// Names that should open in keys (piano-roll) editor mode. Arrangements
// named "Piano", "Keyboard", or "Synth" render as piano-roll charts rather
// than 6-string guitar charts.
const KEYS_PATTERN = /^(keys|piano|keyboard|synth)/i;

// ════════════════════════════════════════════════════════════════════
// State
// ════════════════════════════════════════════════════════════════════

const S = {
    // Song data
    title: '', artist: '', sessionId: null, filename: '',
    format: 'psarc',
    arrangements: [],
    currentArr: 0,
    beats: [], sections: [], duration: 0, offset: 0,

    // View
    scrollX: 0,   // seconds
    zoom: 120,     // px per second
    snapIdx: 2,    // default 1/4

    // Selection
    sel: new Set(),

    // Drag state
    drag: null, // { type, startX, startY, startTime, startString, noteIdx, origTimes, origStrings }

    // Playback
    playing: false,
    cursorTime: 0,
    audioCtx: null, audioBuffer: null, audioSource: null,
    playStartWall: 0, playStartTime: 0,

    // Waveform cache
    waveformPeaks: null,

    // History
    history: null,

    // Songs list cache
    songsList: null,

    // Clipboard
    clipboard: null, // { notes: [...], baseTime }
};

let canvas, ctx;
let rafId = null;

// ════════════════════════════════════════════════════════════════════
// Coordinate mapping
// ════════════════════════════════════════════════════════════════════

function isBassArr() {
    if (!S.arrangements.length) return false;
    const arr = S.arrangements[S.currentArr];
    return !!arr && /bass/i.test(arr.name || '');
}

// Active arrangement string count. Mirrors lib/song.py:arrangement_string_count
// so the editor agrees with the highway: combine name-based default (Bass→4,
// else→6), tuning length when ≠6 (length 6 is RS-schema padding), an
// explicit `_extendedStrings` counter that AddStringCmd / RemoveStringCmd
// bump (disambiguates the bass-with-tuning-length-6 case — could be
// either a 4-string padded or a genuine 6-string), chord-template width,
// and the max note-string index. Clamped to [4, MAX_LANES].
//
// `lanes()` is O(N) over notes+chords and is on the hot path (strToLane /
// laneToStr / yToStr are called per-note inside drawNotes and per-mousemove
// in hit-testing). To avoid the resulting O(N²) per frame on large
// arrangements, draw() seeds a per-frame cache that this function reads
// from when active. Mutations outside the draw frame still recompute.
let _lanesCacheActive = false;
let _lanesCacheValue = 6;
// Seed `_extendedStrings` from each arrangement's tuning length. Two
// modes:
//   * Always seed when `tuningLen > 6` — RS-XML never pads past 6, so
//     any length above that is an unambiguous extended-range signal
//     (string6+ attrs were emitted). Applies to all sources including
//     a previously-extended PSARC reloaded in this session.
//   * When `authoritativeLength` is true (sloppak / GP-imported create-
//     mode), also seed when `tuningLen > baseline` even if ≤ 6 —
//     those sources don't apply RS padding, so a 6-slot bass tuning
//     genuinely means 6-string bass. Skipping this path for PSARC
//     loads preserves the standard bass-padded-to-6 → 4 inference.
function _seedExtendedStringsFromTuning(arrangements, authoritativeLength) {
    for (const arr of arrangements || []) {
        if (typeof arr._extendedStrings === 'number') continue;  // already set
        const isBass = /bass/i.test(arr.name || '');
        const baseline = isBass ? 4 : 6;
        const tuningLen = Array.isArray(arr.tuning) ? arr.tuning.length : baseline;
        if (tuningLen > 6) {
            arr._extendedStrings = tuningLen - baseline;
        } else if (authoritativeLength && tuningLen > baseline) {
            arr._extendedStrings = tuningLen - baseline;
        }
    }
}

function _stringCountFor(arr) {
    if (!arr) return 6;
    const isBass = /bass/i.test(arr.name || '');
    const baseline = isBass ? 4 : 6;
    // User-added strings via the Strings modal — authoritative even
    // when tuning happens to be ambiguous length 6 (the standard RS-XML
    // bass padding length).
    let n = baseline + Math.max(0, arr._extendedStrings || 0);
    const tuningLen = Array.isArray(arr.tuning) ? arr.tuning.length : 6;
    if (tuningLen !== 6) n = Math.max(n, tuningLen);
    // Chord-template signal: count the highest *used* fret slot (not
    // the raw array length). RS XML pads chord_templates to width 6
    // unconditionally, so a 4-string bass arrangement also has
    // ct.frets.length === 6 with fret[4..5] === -1. Looking at the
    // last non(-1) index instead means a 4-string bass with no notes
    // on string 4/5 reads as 4 (correct), and a real 6/7-string
    // template that played notes on those high strings still bumps
    // `n` up.
    for (const ct of arr.chord_templates || []) {
        if (Array.isArray(ct.frets)) {
            for (let i = ct.frets.length - 1; i >= 0; i--) {
                if (ct.frets[i] !== -1) {
                    if (i + 1 > n) n = i + 1;
                    break;
                }
            }
        }
    }
    for (const note of arr.notes || []) {
        if (note.string + 1 > n) n = note.string + 1;
    }
    for (const ch of arr.chords || []) {
        for (const cn of ch.notes || []) {
            if (cn.string + 1 > n) n = cn.string + 1;
        }
    }
    return Math.max(4, Math.min(MAX_LANES, n));
}
function lanes() {
    if (_lanesCacheActive) return _lanesCacheValue;
    if (!S.arrangements.length) return 6;
    return _stringCountFor(S.arrangements[S.currentArr]);
}
// Build display labels in RS string-index order (low → high). Extended-range
// instruments add strings at the low end (7-string guitar adds low B below
// low E; 5-string bass adds low B below low E), and 6-string bass adds high
// C on top. The arrow notation marks those non-standard strings.
function laneLabels() {
    const L = lanes();
    if (isBassArr()) {
        // 4-string standard: E A D G
        // 5-string: B↓ E A D G  (low B added)
        // 6-string: B↓ E A D G C (low B + high C added)
        if (L <= 4) return ['E', 'A', 'D', 'G'].slice(0, L);
        if (L === 5) return ['B↓', 'E', 'A', 'D', 'G'];
        return ['B↓', 'E', 'A', 'D', 'G', 'C↑'].slice(0, L);
    }
    // Guitar standard: E A D G B e (low → high)
    // 7-string: B↓ E A D G B e  (low B added)
    // 8-string: F#↓ B↓ E A D G B e (low F# and low B added)
    if (L <= 6) return ['E', 'A', 'D', 'G', 'B', 'e'].slice(0, L);
    if (L === 7) return ['B↓', 'E', 'A', 'D', 'G', 'B', 'e'];
    return ['F#↓', 'B↓', 'E', 'A', 'D', 'G', 'B', 'e'].slice(0, L);
}

function timeToX(t)  { return LABEL_W + (t - S.scrollX) * S.zoom; }
function xToTime(x)  { return (x - LABEL_W) / S.zoom + S.scrollX; }
function laneToY(l)  { return WAVEFORM_H + l * LANE_H; }
function yToLane(y)  { return Math.floor((y - WAVEFORM_H) / LANE_H); }
function strToLane(s) { return (lanes() - 1) - s; }
function laneToStr(l) { return (lanes() - 1) - l; }
function strToY(s)   { return laneToY(strToLane(s)); }
function yToStr(y)   { const l = Math.max(0, Math.min(lanes() - 1, yToLane(y))); return laneToStr(l); }
function canvasH()   {
    if (isKeysMode()) return WAVEFORM_H + pianoLaneCount() * PIANO_LANE_H + BEAT_H;
    return WAVEFORM_H + lanes() * LANE_H + BEAT_H;
}

// ── Piano roll mode helpers ─────────────────────────────────────────

function isKeysMode() {
    if (!S.arrangements.length) return false;
    const arr = S.arrangements[S.currentArr];
    return arr && KEYS_PATTERN.test(arr.name || '');
}

function pianoLaneCount() { return pianoRange.hi - pianoRange.lo + 1; }

function midiToNote(midi) { return PIANO_NOTE_NAMES[midi % 12] + (Math.floor(midi / 12) - 1); }
function isBlackKey(midi) { const pc = midi % 12; return pc===1||pc===3||pc===6||pc===8||pc===10; }

function noteToMidi(string, fret) { return string * 24 + fret; }
function midiToString(midi) { return Math.floor(midi / 24); }
function midiToFret(midi) { return midi % 24; }

// Piano roll Y: higher MIDI = higher on screen (lower Y)
function midiToY(midi) { return WAVEFORM_H + (pianoRange.hi - midi) * PIANO_LANE_H; }
function yToMidi(y) {
    const m = pianoRange.hi - Math.floor((y - WAVEFORM_H) / PIANO_LANE_H);
    return Math.max(pianoRange.lo, Math.min(pianoRange.hi, m));
}

// expandOnly=true preserves any wider current range (used during in-place
// edits so adding a low note doesn't collapse the viewport and lose
// previously-clickable upper lanes). Load/import/arrangement-switch call
// without it so the viewport snaps cleanly to the new arrangement.
function updatePianoRange(expandOnly = false) {
    const nn = notes();
    // noteToMidi encodes up to string=5, fret=23 → max 143; match the drag-clamp ceiling.
    let lo = 143, hi = 0;
    for (const n of nn) {
        const m = noteToMidi(n.string, n.fret);
        if (m < lo) lo = m;
        if (m > hi) hi = m;
    }
    if (lo > hi) {
        // Empty arrangement: expose the full 88-key range so any starting
        // pitch is clickable. Lanes are deliberately thin (~4px) to keep the
        // viewport within ~352px — once a note is added the range snaps to
        // the actual note range and lanes return to normal height.
        pianoRange = { lo: 21, hi: 108, _fromEmpty: true };
        PIANO_LANE_H = 4;
        return;
    }
    // Expand to octave boundaries with padding; ceiling matches drag-clamp max of 143.
    let nlo = Math.max(0, Math.floor(lo / 12) * 12 - 6);
    let nhi = Math.min(143, Math.ceil((hi + 1) / 12) * 12 + 5);
    if (expandOnly && pianoRange && !pianoRange._fromEmpty) {
        nlo = Math.min(nlo, pianoRange.lo);
        nhi = Math.max(nhi, pianoRange.hi);
    }
    pianoRange = { lo: nlo, hi: nhi };
    // Adjust lane height to fill available space nicely. Allow down to 4px
    // so wide note ranges (many octaves) remain visible without overflowing
    // the canvas wrapper.
    PIANO_LANE_H = Math.max(4, Math.min(14, 350 / (nhi - nlo + 1)));
}

function snapTime(t) {
    const sv = SNAP_VALUES[S.snapIdx];
    if (sv === 0 || S.beats.length < 2) return t;
    // Find surrounding beat
    let bi = 0;
    for (let i = 0; i < S.beats.length - 1; i++) {
        if (S.beats[i].time <= t) bi = i; else break;
    }
    const bt = S.beats[bi].time;
    const nt = bi < S.beats.length - 1 ? S.beats[bi + 1].time : bt + 0.5;
    const bd = nt - bt;
    const subs = 1 / sv;
    const sd = bd / subs;
    const idx = Math.round((t - bt) / sd);
    return bt + idx * sd;
}

// ════════════════════════════════════════════════════════════════════
// Note accessors
// ════════════════════════════════════════════════════════════════════

function notes() { return S.arrangements.length ? S.arrangements[S.currentArr].notes : []; }
function chords() { return S.arrangements.length ? S.arrangements[S.currentArr].chords : []; }

// Flatten chord notes into the main notes array on load, tagging with _fromChord.
// On save, reconstruct chords from notes sharing the same time+_fromChord group.
function flattenChords() {
    if (!S.arrangements.length) return;
    const arr = S.arrangements[S.currentArr];
    for (const ch of arr.chords) {
        for (const cn of ch.notes) {
            arr.notes.push({
                time: cn.time || ch.time,
                string: cn.string,
                fret: cn.fret,
                sustain: cn.sustain || 0,
                techniques: cn.techniques || {},
                _fromChord: true,
                _chordId: ch.chord_id,
            });
        }
    }
    arr.chords = [];
    arr.notes.sort((a, b) => a.time - b.time);
}

// Reconstruct chords from notes at the same time before saving
function reconstructChords() {
    if (!S.arrangements.length) return;
    const arr = S.arrangements[S.currentArr];
    const byTime = {};
    const soloNotes = [];
    for (const n of arr.notes) {
        const key = n.time.toFixed(4);
        if (!byTime[key]) byTime[key] = [];
        byTime[key].push(n);
    }
    const newNotes = [];
    const newChords = [];
    // Always rebuild chord_templates from scratch so repeated saves don't
    // accumulate duplicate entries (flattenChords has already emptied
    // arr.chords, so the old templates are no longer referenced).
    const chordTemplates = [];
    const templateMap = {};

    for (const key of Object.keys(byTime).sort((a, b) => parseFloat(a) - parseFloat(b))) {
        const group = byTime[key];
        if (group.length === 1) {
            newNotes.push(group[0]);
        } else {
            // Multiple notes at same time = chord
            const L = lanes();
            const frets = new Array(L).fill(-1);
            for (const n of group) {
                if (n.string >= 0 && n.string < L) frets[n.string] = n.fret;
            }
            const fretKey = frets.join(',');
            let tmplIdx;
            if (fretKey in templateMap) {
                tmplIdx = templateMap[fretKey];
            } else {
                tmplIdx = chordTemplates.length;
                chordTemplates.push({
                    name: '',
                    frets: [...frets],
                    // Match `frets` width — on 7/8-string charts the
                    // template would otherwise have inconsistent
                    // frets.length=L but fingers.length=6, which
                    // serializes to misaligned `fingerN` slots.
                    fingers: new Array(L).fill(-1),
                });
                templateMap[fretKey] = tmplIdx;
            }
            newChords.push({
                time: group[0].time,
                chord_id: tmplIdx,
                high_density: false,
                notes: group.map(n => ({
                    time: n.time,
                    string: n.string,
                    fret: n.fret,
                    sustain: n.sustain || 0,
                    techniques: n.techniques || {},
                })),
            });
        }
    }
    arr.notes = newNotes;
    arr.chords = newChords;
    arr.chord_templates = chordTemplates;
}

// ════════════════════════════════════════════════════════════════════
// Drawing
// ════════════════════════════════════════════════════════════════════

function draw() {
    if (!canvas) return;
    const w = canvas.width / DPR;
    const h = canvas.height / DPR;
    ctx.save();
    ctx.scale(DPR, DPR);
    ctx.clearRect(0, 0, w, h);

    // Seed the per-frame `lanes()` cache. drawNotes calls strToLane on every
    // note (and per-note hit tests do the same), so without this every
    // frame is O(N²) over the arrangement. The labels array is cached
    // alongside since `colorForLane` reads it once per note. Enable
    // the cache BEFORE calling `laneLabels()` so that helper's internal
    // `lanes()` call hits the cache too (otherwise we'd do two full
    // O(N) scans per frame).
    _lanesCacheActive = false;  // force a real compute first
    _lanesCacheValue = lanes();
    _lanesCacheActive = true;
    _laneLabelsCacheValue = laneLabels();
    try {
        drawWaveform(w);
        drawLanes(w);
        drawGrid(w);
        drawSections(w);
        drawBeatBar(w);
        drawNotes(w);
        drawSelectionRect(w);
        drawGhostNotes();
        drawCursor(w, h);
        drawLabels(w);
    } finally {
        _lanesCacheActive = false;
        _laneLabelsCacheValue = null;
    }

    ctx.restore();
}

function drawWaveform(w) {
    ctx.fillStyle = '#08081a';
    ctx.fillRect(0, 0, w, WAVEFORM_H);
    if (!S.waveformPeaks) return;

    const peaks = S.waveformPeaks;
    const mid = WAVEFORM_H / 2;
    ctx.fillStyle = '#4080e060';
    for (let px = LABEL_W; px < w; px++) {
        const t = xToTime(px);
        if (t < 0 || t >= S.duration) continue;
        const i = Math.floor(t / S.duration * peaks.length);
        if (i < 0 || i >= peaks.length) continue;
        const bh = peaks[i] * (WAVEFORM_H / 2 - 4);
        ctx.fillRect(px, mid - bh, 1, bh * 2);
    }
}

function drawLanes(w) {
    if (isKeysMode()) return drawPianoLanes(w);
    const L = lanes();
    for (let l = 0; l < L; l++) {
        const y = laneToY(l);
        ctx.fillStyle = l % 2 === 0 ? '#0c0c1c' : '#0f0f24';
        ctx.fillRect(LABEL_W, y, w - LABEL_W, LANE_H);
        // Separator
        ctx.strokeStyle = '#1a1a35';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(LABEL_W, y + LANE_H);
        ctx.lineTo(w, y + LANE_H);
        ctx.stroke();
    }
}

function drawPianoLanes(w) {
    for (let midi = pianoRange.lo; midi <= pianoRange.hi; midi++) {
        const y = midiToY(midi);
        const black = isBlackKey(midi);
        ctx.fillStyle = black ? '#0a0a1a' : '#0e0e22';
        ctx.fillRect(LABEL_W, y, w - LABEL_W, PIANO_LANE_H);

        // Octave boundary (C notes)
        if (midi % 12 === 0) {
            ctx.strokeStyle = '#2a2a55';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(LABEL_W, y + PIANO_LANE_H);
            ctx.lineTo(w, y + PIANO_LANE_H);
            ctx.stroke();
        }
    }
}

function drawGrid(w) {
    const st = S.scrollX - 1;
    const et = S.scrollX + (w - LABEL_W) / S.zoom + 1;
    const laneBottom = isKeysMode()
        ? WAVEFORM_H + pianoLaneCount() * PIANO_LANE_H
        : WAVEFORM_H + lanes() * LANE_H;
    for (const b of S.beats) {
        if (b.time < st || b.time > et) continue;
        const x = timeToX(b.time);
        if (x < LABEL_W || x > w) continue;
        const meas = b.measure > 0;
        ctx.strokeStyle = meas ? '#2a2a50' : '#16162c';
        ctx.lineWidth = meas ? 1.5 : 0.5;
        ctx.beginPath();
        ctx.moveTo(x, WAVEFORM_H);
        ctx.lineTo(x, laneBottom);
        ctx.stroke();
    }
}

function drawSections(w) {
    const st = S.scrollX - 1;
    const et = S.scrollX + (w - LABEL_W) / S.zoom + 1;
    const laneBottom = isKeysMode()
        ? WAVEFORM_H + pianoLaneCount() * PIANO_LANE_H
        : WAVEFORM_H + lanes() * LANE_H;
    ctx.font = '9px monospace';
    ctx.textBaseline = 'top';
    for (const s of S.sections) {
        if (s.start_time < st || s.start_time > et) continue;
        const x = timeToX(s.start_time);
        if (x < LABEL_W || x > w) continue;
        // Dashed vertical line
        ctx.strokeStyle = '#e8c04060';
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(x, WAVEFORM_H);
        ctx.lineTo(x, laneBottom);
        ctx.stroke();
        ctx.setLineDash([]);
        // Label at top of lanes
        ctx.fillStyle = '#e8c040';
        ctx.textAlign = 'left';
        ctx.fillText(s.name, x + 3, WAVEFORM_H + 2);
    }
}

function drawBeatBar(w) {
    const y = WAVEFORM_H + lanes() * LANE_H;
    ctx.fillStyle = '#08081a';
    ctx.fillRect(0, y, w, BEAT_H);
    ctx.fillStyle = '#08081a';
    ctx.fillRect(0, y, LABEL_W, BEAT_H);

    ctx.fillStyle = '#555';
    ctx.font = '9px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const st = S.scrollX - 1;
    const et = S.scrollX + (w - LABEL_W) / S.zoom + 1;
    for (const b of S.beats) {
        if (b.measure <= 0 || b.time < st || b.time > et) continue;
        const x = timeToX(b.time);
        if (x < LABEL_W || x > w) continue;
        ctx.fillText(String(b.measure), x, y + BEAT_H / 2);
    }
}

function drawLabels(w) {
    // Waveform label
    ctx.fillStyle = '#0a0a1a';
    ctx.fillRect(0, 0, LABEL_W, WAVEFORM_H);
    ctx.fillStyle = '#555';
    ctx.font = '9px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('Audio', LABEL_W / 2, WAVEFORM_H / 2);

    if (isKeysMode()) return drawPianoLabels(w);

    // String labels. `labels` is in RS string-index order (low → high); lanes
    // are drawn high-to-low (lane 0 = top = highest string). Colours come
    // from `colorForLane()` which looks up the string's pitch label in
    // `STRING_LABEL_COLORS` — so a 4-string bass G/D/A/E reads orange/blue/
    // yellow/red just like the same pitches on a 6-string guitar.
    const L = lanes();
    const labels = laneLabels();
    for (let l = 0; l < L; l++) {
        const y = laneToY(l);
        ctx.fillStyle = '#0a0a1a';
        ctx.fillRect(0, y, LABEL_W, LANE_H);
        const s = laneToStr(l);
        ctx.fillStyle = colorForLane(l);
        ctx.font = 'bold 12px monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(labels[s] || String(s), LABEL_W / 2, y + LANE_H / 2);
    }
}

function drawPianoLabels() {
    // MIDI note labels on the left axis
    ctx.font = '8px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (let midi = pianoRange.lo; midi <= pianoRange.hi; midi++) {
        const y = midiToY(midi);
        ctx.fillStyle = '#0a0a1a';
        ctx.fillRect(0, y, LABEL_W, PIANO_LANE_H);

        // Only label C notes and F notes to avoid clutter
        if (midi % 12 === 0 || midi % 12 === 5) {
            const octave = Math.floor(midi / 12) - 1;
            const color = PIANO_OCTAVE_COLORS[Math.min(octave + 1, PIANO_OCTAVE_COLORS.length - 1)];
            ctx.fillStyle = color;
            ctx.fillText(midiToNote(midi), LABEL_W / 2, y + PIANO_LANE_H / 2);
        }
    }
}

function drawNotes(w) {
    const nn = notes();
    const st = S.scrollX - 2;
    const et = S.scrollX + (w - LABEL_W) / S.zoom + 2;
    const keysMode = isKeysMode();
    for (let i = 0; i < nn.length; i++) {
        const n = nn[i];
        if (n.time + (n.sustain || 0) < st || n.time > et) continue;
        if (keysMode) {
            _drawPianoNote(n, S.sel.has(i));
        } else {
            _drawNote(n, S.sel.has(i));
        }
    }
}

function _drawNote(n, selected) {
    const x = timeToX(n.time);
    const y = strToY(n.string) + NOTE_PAD;
    const sw = Math.max(MIN_NOTE_W, (n.sustain || 0) * S.zoom);
    const h = LANE_H - NOTE_PAD * 2;
    const color = colorForLane(strToLane(n.string));

    // Body
    ctx.fillStyle = color + 'cc';
    ctx.beginPath();
    ctx.roundRect(x, y, sw, h, 3);
    ctx.fill();

    // Border
    if (selected) {
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 2;
    } else {
        ctx.strokeStyle = color;
        ctx.lineWidth = 0.5;
    }
    ctx.beginPath();
    ctx.roundRect(x, y, sw, h, 3);
    ctx.stroke();

    // Fret number
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 13px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(String(n.fret), x + Math.min(sw, MIN_NOTE_W) / 2, y + h / 2);

    // Technique badges
    const techs = n.techniques || {};
    const badges = [];
    if (techs.hammer_on) badges.push('H');
    if (techs.pull_off) badges.push('P');
    if (techs.slide_to >= 0) badges.push('/' + techs.slide_to);
    if (techs.bend > 0) badges.push('b');
    if (techs.harmonic) badges.push('*');
    if (techs.palm_mute) badges.push('PM');
    if (techs.tap) badges.push('T');
    if (techs.tremolo) badges.push('~');
    if (techs.mute) badges.push('x');
    if (badges.length) {
        ctx.fillStyle = '#ffffffbb';
        ctx.font = '7px monospace';
        ctx.textAlign = 'left';
        ctx.fillText(badges.join(' '), x + 2, y + 9);
    }

    // Sustain tail
    if (sw > MIN_NOTE_W) {
        ctx.fillStyle = color + '40';
        ctx.fillRect(x + MIN_NOTE_W, y + h / 2 - 2, sw - MIN_NOTE_W, 4);
    }
}

function _drawPianoNote(n, selected) {
    const midi = noteToMidi(n.string, n.fret);
    if (midi < pianoRange.lo || midi > pianoRange.hi) return;

    const x = timeToX(n.time);
    const y = midiToY(midi) + 1;
    const sw = Math.max(MIN_NOTE_W, (n.sustain || 0) * S.zoom);
    const h = PIANO_LANE_H - 2;
    const octave = Math.floor(midi / 12);
    const color = PIANO_OCTAVE_COLORS[Math.min(octave, PIANO_OCTAVE_COLORS.length - 1)];

    // Body
    ctx.fillStyle = color + 'cc';
    ctx.beginPath();
    ctx.roundRect(x, y, sw, h, 2);
    ctx.fill();

    // Border
    if (selected) {
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 2;
    } else {
        ctx.strokeStyle = color;
        ctx.lineWidth = 0.5;
    }
    ctx.beginPath();
    ctx.roundRect(x, y, sw, h, 2);
    ctx.stroke();

    // Note name (only if enough space)
    if (sw >= 20 && h >= 8) {
        ctx.fillStyle = '#000';
        ctx.font = `bold ${Math.min(9, h - 1)}px monospace`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(midiToNote(midi), x + Math.min(sw, 24) / 2, y + h / 2);
    }
}

function drawCursor(w, h) {
    const x = timeToX(S.cursorTime);
    if (x < LABEL_W || x > w) return;
    ctx.strokeStyle = '#ff4444';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, canvasH());
    ctx.stroke();
}

function drawSelectionRect() {
    if (!S.drag || S.drag.type !== 'select') return;
    const x1 = Math.min(S.drag.startX, S.drag.curX);
    const y1 = Math.min(S.drag.startY, S.drag.curY);
    const x2 = Math.max(S.drag.startX, S.drag.curX);
    const y2 = Math.max(S.drag.startY, S.drag.curY);
    ctx.strokeStyle = '#4080e0';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
    ctx.setLineDash([]);
    ctx.fillStyle = '#4080e018';
    ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
}

// ════════════════════════════════════════════════════════════════════
// Hit testing
// ════════════════════════════════════════════════════════════════════

const EDGE_GRAB = 8; // pixels from right edge to trigger resize

function hitNote(mx, my) {
    const nn = notes();
    const keysMode = isKeysMode();
    for (let i = nn.length - 1; i >= 0; i--) {
        const n = nn[i];
        const x = timeToX(n.time);
        let y, w, h;
        if (keysMode) {
            const midi = noteToMidi(n.string, n.fret);
            y = midiToY(midi) + 1;
            w = Math.max(MIN_NOTE_W, (n.sustain || 0) * S.zoom);
            h = PIANO_LANE_H - 2;
        } else {
            y = strToY(n.string) + NOTE_PAD;
            w = Math.max(MIN_NOTE_W, (n.sustain || 0) * S.zoom);
            h = LANE_H - NOTE_PAD * 2;
        }
        if (mx >= x && mx <= x + w && my >= y && my <= y + h) return i;
    }
    return -1;
}

function hitNoteEdge(mx, my) {
    // Returns note index if mouse is near the right edge of a note (for sustain resize)
    const nn = notes();
    for (let i = nn.length - 1; i >= 0; i--) {
        const n = nn[i];
        const x = timeToX(n.time);
        const y = strToY(n.string) + NOTE_PAD;
        const w = Math.max(MIN_NOTE_W, (n.sustain || 0) * S.zoom);
        const h = LANE_H - NOTE_PAD * 2;
        const rightEdge = x + w;
        if (mx >= rightEdge - EDGE_GRAB && mx <= rightEdge + EDGE_GRAB && my >= y && my <= y + h) return i;
    }
    return -1;
}

// ════════════════════════════════════════════════════════════════════
// Undo / Redo
// ════════════════════════════════════════════════════════════════════

class EditHistory {
    constructor() { this.undo = []; this.redo = []; }
    exec(cmd) { cmd.exec(); this.undo.push(cmd); this.redo = []; this._afterEdit(); this._ui(); }
    doUndo() { if (!this.undo.length) return; const c = this.undo.pop(); c.rollback(); this.redo.push(c); this._afterEdit(); this._ui(); draw(); }
    doRedo() { if (!this.redo.length) return; const c = this.redo.pop(); c.exec(); this.undo.push(c); this._afterEdit(); this._ui(); draw(); }
    _afterEdit() {
        // Keep the keys viewport in sync with the current note range so
        // multi-octave authoring works without manual range control.
        // expandOnly=true so adding a note outside the current viewport
        // extends it instead of collapsing to the latest note's octave.
        if (typeof isKeysMode === 'function' && isKeysMode()) updatePianoRange(true);
    }
    _ui() {
        const u = document.getElementById('editor-undo');
        const r = document.getElementById('editor-redo');
        if (u) u.disabled = !this.undo.length;
        if (r) r.disabled = !this.redo.length;
    }
}

class MoveNoteCmd {
    constructor(indices, dtimes, dstrings, dfrets) {
        this.indices = indices;
        this.dtimes = dtimes;
        this.dstrings = dstrings;
        this.dfrets = dfrets; // null for guitar mode, array for piano mode
    }
    exec() {
        const nn = notes();
        for (let i = 0; i < this.indices.length; i++) {
            nn[this.indices[i]].time += this.dtimes[i];
            nn[this.indices[i]].string += this.dstrings[i];
            if (this.dfrets) nn[this.indices[i]].fret += this.dfrets[i];
        }
    }
    rollback() {
        const nn = notes();
        for (let i = 0; i < this.indices.length; i++) {
            nn[this.indices[i]].time -= this.dtimes[i];
            nn[this.indices[i]].string -= this.dstrings[i];
            if (this.dfrets) nn[this.indices[i]].fret -= this.dfrets[i];
        }
    }
}

class AddNoteCmd {
    constructor(note) { this.note = note; this.idx = -1; }
    exec() {
        const nn = notes();
        nn.push(this.note);
        this.idx = nn.length - 1;
        nn.sort((a, b) => a.time - b.time);
        // Find new index
        this.idx = nn.indexOf(this.note);
    }
    rollback() {
        const nn = notes();
        const i = nn.indexOf(this.note);
        if (i >= 0) nn.splice(i, 1);
    }
}

class DeleteNotesCmd {
    constructor(indices) {
        this.indices = [...indices].sort((a, b) => b - a);
        this.removed = [];
    }
    exec() {
        const nn = notes();
        this.removed = [];
        for (const i of this.indices) {
            this.removed.push({ idx: i, note: nn[i] });
            nn.splice(i, 1);
        }
        S.sel.clear();
    }
    rollback() {
        const nn = notes();
        for (const r of [...this.removed].reverse()) {
            nn.splice(r.idx, 0, r.note);
        }
    }
}

class ResizeSustainCmd {
    constructor(index, newSustain) {
        this.index = index;
        this.newSustain = newSustain;
        this.oldSustain = notes()[index].sustain || 0;
    }
    exec() { notes()[this.index].sustain = this.newSustain; }
    rollback() { notes()[this.index].sustain = this.oldSustain; }
}

class ChangeFretCmd {
    constructor(index, newFret) {
        this.index = index;
        this.newFret = newFret;
        this.oldFret = notes()[index].fret;
    }
    exec() { notes()[this.index].fret = this.newFret; }
    rollback() { notes()[this.index].fret = this.oldFret; }
}

// Extend an arrangement's string count by one. `position` is 'low' for
// adding at the lowest end (guitar low B/F#, 4→5-string bass low B) and
// 'high' for adding at the high end (5→6-string bass high C). Adding at
// the low end shifts every existing note's string index up by 1 so the
// chart visually stays put — only the new lowest lane is empty.
// Layout side-effect for any command that changes `lanes()`. Pulled
// out so AddStringCmd / RemoveStringCmd exec & rollback can drive a
// LANE_H recomputation on Ctrl-Z / Ctrl-Y too. Takes the target
// arrangement index because undo/redo may fire after the user has
// switched to a different arrangement — only resize when the
// mutation hits the visible chart, so we don't mis-size LANE_H on
// behalf of an off-screen arrangement.
//
// We defer to the next animation frame so the click-handler reflow
// completes before resizeCanvas reads `wrap.clientHeight`. Calling
// inline can hit a transient layout where the read returns 0; the
// early-return inside resizeCanvas then skips the LANE_H update,
// extra lanes overflow the canvas, and the new string isn't visible
// until the next legitimate resize event (e.g. screen-change observer).
function _resizeForLaneChange(arrIdx) {
    if (typeof resizeCanvas !== 'function') return;
    if (arrIdx !== undefined && arrIdx !== S.currentArr) return;
    requestAnimationFrame(() => resizeCanvas());
}

// Normalize `arr.tuning` so its length equals the arrangement's *real*
// string count instead of the RS-XML padded length (which is always 6
// for both 4-string bass and 6-string guitar). Without this, an
// add-string on a 4-string bass loaded from RS XML would treat the
// padded 6-slot tuning as 6 real strings and extend to 7. We slice
// excess zero-tail padding when tuning.length > realCount and pad
// when shorter. Idempotent — safe to call before every mutation.
function _normalizeTuningToLanes(arr, realCount) {
    let t = Array.isArray(arr.tuning) ? arr.tuning.slice() : [];
    if (t.length > realCount) {
        // Drop trailing zeros first (RS-XML padding). Callers compute
        // `realCount` via `_stringCountFor(arr)` which already factors
        // in any non-zero high-index offsets, so anything left after
        // that trim is stale and the explicit slice below honours the
        // length contract.
        while (t.length > realCount && t[t.length - 1] === 0) {
            t.pop();
        }
        if (t.length > realCount) {
            t = t.slice(0, realCount);
        }
    }
    while (t.length < realCount) t.push(0);
    arr.tuning = t;
}

class AddStringCmd {
    constructor(arrIdx, position) {
        this.arrIdx = arrIdx;
        this.position = position;
    }
    _arr() { return S.arrangements[this.arrIdx]; }
    exec() {
        const arr = this._arr();
        // Normalize against `_stringCountFor(arr)` rather than the
        // global `lanes()` which reads from `S.currentArr`. Undo/redo
        // can fire after the user has switched arrangements, so we
        // must compute the count against the command's TARGET arr.
        _normalizeTuningToLanes(arr, _stringCountFor(arr));
        const tuning = arr.tuning.slice();
        if (this.position === 'low') {
            tuning.unshift(0);
            for (const n of arr.notes || []) n.string += 1;
            for (const ch of arr.chords || []) {
                for (const cn of ch.notes || []) cn.string += 1;
            }
            for (const ct of arr.chord_templates || []) {
                if (Array.isArray(ct.frets)) ct.frets.unshift(-1);
                if (Array.isArray(ct.fingers)) ct.fingers.unshift(-1);
            }
        } else {
            tuning.push(0);
            for (const ct of arr.chord_templates || []) {
                if (Array.isArray(ct.frets)) ct.frets.push(-1);
                if (Array.isArray(ct.fingers)) ct.fingers.push(-1);
            }
        }
        arr.tuning = tuning;
        // Bump the explicit extension counter so lanes() / the save
        // detection function don't have to guess when tuning.length
        // happens to be 6 (the ambiguous bass-padded-or-real-6 case).
        arr._extendedStrings = (arr._extendedStrings || 0) + 1;
        _resizeForLaneChange(this.arrIdx);
    }
    rollback() {
        const arr = this._arr();
        const tuning = Array.isArray(arr.tuning) ? arr.tuning.slice() : [0, 0, 0, 0, 0, 0];
        if (this.position === 'low') {
            tuning.shift();
            for (const n of arr.notes || []) n.string -= 1;
            for (const ch of arr.chords || []) {
                for (const cn of ch.notes || []) cn.string -= 1;
            }
            for (const ct of arr.chord_templates || []) {
                if (Array.isArray(ct.frets)) ct.frets.shift();
                if (Array.isArray(ct.fingers)) ct.fingers.shift();
            }
        } else {
            tuning.pop();
            for (const ct of arr.chord_templates || []) {
                if (Array.isArray(ct.frets)) ct.frets.pop();
                if (Array.isArray(ct.fingers)) ct.fingers.pop();
            }
        }
        arr.tuning = tuning;
        // AddStringCmd's rollback undoes a prior add, so decrement.
        arr._extendedStrings = Math.max(0, (arr._extendedStrings || 0) - 1);
        _resizeForLaneChange(this.arrIdx);
    }
}

// Remove a string from the active arrangement. `position === 'low'` peels
// off the low extension (guitar 7→6 / 8→7, bass 5→4); `position === 'high'`
// peels the high C off a 6-string bass — the editor exposes both via the
// Strings modal. Callers must first verify no notes live on the targeted
// string (validation lives in the UI handler so the user gets a clear
// error message in the modal rather than a silent data drop here).
class RemoveStringCmd {
    constructor(arrIdx, position) {
        this.arrIdx = arrIdx;
        this.position = position;
        // Snapshots filled in by exec() — keeping them off the
        // constructor means instantiation is a pure data move. If a
        // future code path ever builds a RemoveStringCmd without
        // running it (e.g. for previewing), the live arrangement
        // stays untouched.
        this.removedOffset = 0;
        this.removedTemplateCols = [];
    }
    _arr() { return S.arrangements[this.arrIdx]; }
    exec() {
        const arr = this._arr();
        // Normalize tuning to the real string count first so the
        // snapshot reflects the actual column we're dropping, not an
        // RS-XML padding zero. Snapshot happens immediately after so
        // rollback can restore the exact pre-remove state. Use
        // `_stringCountFor(arr)` (not `lanes()`) so undo/redo after
        // an arrangement switch still operates on this command's
        // TARGET arrangement.
        _normalizeTuningToLanes(arr, _stringCountFor(arr));
        const t = arr.tuning || [];
        this.removedOffset = this.position === 'low' ? t[0] : t[t.length - 1];
        this.removedTemplateCols = (arr.chord_templates || []).map(ct => {
            const fretLen = Array.isArray(ct.frets) ? ct.frets.length : 0;
            const fingerLen = Array.isArray(ct.fingers) ? ct.fingers.length : 0;
            // Empty arrays would otherwise yield colIdx == -1, store
            // `undefined`, and push that back as the rollback value —
            // corrupting the template on undo. Fall back to -1 when
            // the column doesn't exist.
            const fretCol = this.position === 'low' ? 0 : fretLen - 1;
            const fingerCol = this.position === 'low' ? 0 : fingerLen - 1;
            return {
                fret: fretLen > 0 && fretCol >= 0 ? ct.frets[fretCol] : -1,
                finger: fingerLen > 0 && fingerCol >= 0 ? ct.fingers[fingerCol] : -1,
            };
        });
        const tuning = arr.tuning.slice();
        if (this.position === 'low') {
            tuning.shift();
            for (const n of arr.notes || []) n.string -= 1;
            for (const ch of arr.chords || []) {
                for (const cn of ch.notes || []) cn.string -= 1;
            }
            for (const ct of arr.chord_templates || []) {
                if (Array.isArray(ct.frets)) ct.frets.shift();
                if (Array.isArray(ct.fingers)) ct.fingers.shift();
            }
        } else {
            tuning.pop();
            for (const ct of arr.chord_templates || []) {
                if (Array.isArray(ct.frets)) ct.frets.pop();
                if (Array.isArray(ct.fingers)) ct.fingers.pop();
            }
        }
        arr.tuning = tuning;
        arr._extendedStrings = Math.max(0, (arr._extendedStrings || 0) - 1);
        _resizeForLaneChange(this.arrIdx);
    }
    rollback() {
        const arr = this._arr();
        const tuning = arr.tuning.slice();
        const restore = (ct, i) => {
            const cols = this.removedTemplateCols[i] || { fret: -1, finger: -1 };
            return cols;
        };
        if (this.position === 'low') {
            tuning.unshift(this.removedOffset);
            for (const n of arr.notes || []) n.string += 1;
            for (const ch of arr.chords || []) {
                for (const cn of ch.notes || []) cn.string += 1;
            }
            (arr.chord_templates || []).forEach((ct, i) => {
                const cols = restore(ct, i);
                if (Array.isArray(ct.frets)) ct.frets.unshift(cols.fret);
                if (Array.isArray(ct.fingers)) ct.fingers.unshift(cols.finger);
            });
        } else {
            tuning.push(this.removedOffset);
            (arr.chord_templates || []).forEach((ct, i) => {
                const cols = restore(ct, i);
                if (Array.isArray(ct.frets)) ct.frets.push(cols.fret);
                if (Array.isArray(ct.fingers)) ct.fingers.push(cols.finger);
            });
        }
        arr.tuning = tuning;
        // RemoveStringCmd's rollback restores the removed string, so
        // re-increment the extension counter (mirrors AddStringCmd.exec).
        arr._extendedStrings = (arr._extendedStrings || 0) + 1;
        _resizeForLaneChange(this.arrIdx);
    }
}

// ════════════════════════════════════════════════════════════════════
// Mouse interactions
// ════════════════════════════════════════════════════════════════════

function getMousePos(e) {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
}

function onMouseDown(e) {
    const { x, y } = getMousePos(e);
    hideContextMenu();
    hideAddNote();

    // Middle button = pan
    if (e.button === 1) {
        e.preventDefault();
        S.drag = { type: 'pan', startX: x, origScroll: S.scrollX };
        return;
    }

    // Right button = context menu (handled in onContextMenu)
    if (e.button === 2) return;

    // Left button
    if (y < WAVEFORM_H) {
        // Block waveform seek while recording: restarting the AudioBufferSourceNode
        // would fire onended and prematurely finalize the take.
        if (_recState === 'recording') return;
        // Click on waveform = set cursor
        S.cursorTime = Math.max(0, xToTime(x));
        if (S.playing) { stopPlayback(); startPlayback(); }
        draw();
        return;
    }

    // Block note editing while recording: mid-take edits to arr.notes would be
    // silently overwritten by _recNotes when the take is finalized on Stop.
    if (_recState === 'recording') return;

    // Check for sustain edge grab first
    const edgeIdx = hitNoteEdge(x, y);
    if (edgeIdx >= 0) {
        if (!S.sel.has(edgeIdx)) { S.sel.clear(); S.sel.add(edgeIdx); }
        const n = notes()[edgeIdx];
        S.drag = {
            type: 'resize',
            noteIdx: edgeIdx,
            startX: x,
            origSustain: n.sustain || 0,
        };
        draw();
        return;
    }

    const idx = hitNote(x, y);

    if (idx >= 0) {
        // Click on note — also select all chord siblings (same time)
        const nn = notes();
        const clickedTime = nn[idx].time;
        const chordSiblings = [];
        for (let i = 0; i < nn.length; i++) {
            if (Math.abs(nn[i].time - clickedTime) < 0.001) chordSiblings.push(i);
        }
        const isChord = chordSiblings.length > 1;

        if (e.shiftKey) {
            // Multi-select toggle — toggle the whole chord group
            const allSelected = chordSiblings.every(i => S.sel.has(i));
            for (const i of chordSiblings) {
                if (allSelected) S.sel.delete(i); else S.sel.add(i);
            }
        } else if (!S.sel.has(idx)) {
            S.sel.clear();
            for (const i of chordSiblings) S.sel.add(i);
        }

        // Start drag
        const selArr = [...S.sel];
        S.drag = {
            type: 'move',
            startX: x, startY: y,
            origTimes: selArr.map(i => nn[i].time),
            origStrings: selArr.map(i => nn[i].string),
            origFrets: selArr.map(i => nn[i].fret),
            indices: selArr,
            moved: false,
        };
        draw();
    } else {
        // Click on empty space = start selection rect or deselect
        if (!e.shiftKey) S.sel.clear();
        S.drag = {
            type: 'select',
            startX: x, startY: y,
            curX: x, curY: y,
        };
        draw();
    }
}

function onMouseMove(e) {
    const { x, y } = getMousePos(e);
    // Activate the lane cache for the handler's lifetime so per-note
    // hit-test helpers (`hitNoteEdge` / `hitNote` → `strToY` →
    // `strToLane` → `lanes()`) stay O(1) per note instead of O(N).
    // A local `const L = lanes()` alone doesn't help those nested
    // calls; the global cache does. Cleared in `finally` so any
    // exception unwinding the handler doesn't leak the flag.
    const _prevActive = _lanesCacheActive;
    const _prevValue = _lanesCacheValue;
    _lanesCacheActive = false;
    _lanesCacheValue = lanes();
    const L = _lanesCacheValue;
    _lanesCacheActive = true;
    try {
        _onMouseMoveBody(e, x, y, L);
    } finally {
        _lanesCacheActive = _prevActive;
        _lanesCacheValue = _prevValue;
    }
}

function _onMouseMoveBody(e, x, y, L) {

    // Cursor hint when not dragging
    if (!S.drag) {
        if (canvas && y >= WAVEFORM_H && y < WAVEFORM_H + L * LANE_H) {
            canvas.style.cursor = hitNoteEdge(x, y) >= 0 ? 'ew-resize' : '';
        } else if (canvas) {
            canvas.style.cursor = '';
        }
        return;
    }

    if (S.drag.type === 'pan') {
        const dx = x - S.drag.startX;
        S.scrollX = Math.max(0, S.drag.origScroll - dx / S.zoom);
        draw();
        return;
    }

    if (S.drag.type === 'select') {
        S.drag.curX = x;
        S.drag.curY = y;
        draw();
        return;
    }

    if (S.drag.type === 'resize') {
        const dt = (x - S.drag.startX) / S.zoom;
        const nn = notes();
        nn[S.drag.noteIdx].sustain = Math.max(0, S.drag.origSustain + dt);
        draw();
        return;
    }

    if (S.drag.type === 'move') {
        S.drag.moved = true;
        const nn = notes();
        const dt = (x - S.drag.startX) / S.zoom;
        const dy = y - S.drag.startY;

        if (isKeysMode()) {
            const dMidi = -Math.round(dy / PIANO_LANE_H);
            for (let i = 0; i < S.drag.indices.length; i++) {
                const ni = S.drag.indices[i];
                let newTime = S.drag.origTimes[i] + dt;
                newTime = snapTime(Math.max(0, newTime));
                nn[ni].time = newTime;

                const origMidi = noteToMidi(S.drag.origStrings[i], S.drag.origFrets[i]);
                const newMidi = Math.max(0, Math.min(143, origMidi + dMidi));
                nn[ni].string = midiToString(newMidi);
                nn[ni].fret = midiToFret(newMidi);
            }
        } else {
            const dLanes = Math.round(dy / LANE_H);
            for (let i = 0; i < S.drag.indices.length; i++) {
                const ni = S.drag.indices[i];
                let newTime = S.drag.origTimes[i] + dt;
                newTime = snapTime(Math.max(0, newTime));
                nn[ni].time = newTime;

                const origLane = strToLane(S.drag.origStrings[i]);
                // Reuse the locally-cached `L` from onMouseMove instead of
                // calling lanes() per dragged note.
                const newLane = Math.max(0, Math.min(L - 1, origLane + dLanes));
                nn[ni].string = laneToStr(newLane);
            }
        }
        draw();
    }
}

function onMouseUp(e) {
    if (!S.drag) return;
    const { x, y } = getMousePos(e);

    if (S.drag.type === 'resize') {
        const nn = notes();
        const finalSustain = nn[S.drag.noteIdx].sustain;
        // Revert so the command can apply it
        nn[S.drag.noteIdx].sustain = S.drag.origSustain;
        if (finalSustain !== S.drag.origSustain) {
            S.history.exec(new ResizeSustainCmd(S.drag.noteIdx, finalSustain));
        }
    }

    if (S.drag.type === 'move' && S.drag.moved) {
        // Commit move as undo command
        const nn = notes();
        const dtimes = S.drag.indices.map((ni, i) => nn[ni].time - S.drag.origTimes[i]);
        const dstrings = S.drag.indices.map((ni, i) => nn[ni].string - S.drag.origStrings[i]);
        const dfrets = isKeysMode()
            ? S.drag.indices.map((ni, i) => nn[ni].fret - S.drag.origFrets[i])
            : null;

        // Revert to original first so exec() applies the delta
        for (let i = 0; i < S.drag.indices.length; i++) {
            nn[S.drag.indices[i]].time = S.drag.origTimes[i];
            nn[S.drag.indices[i]].string = S.drag.origStrings[i];
            if (dfrets) nn[S.drag.indices[i]].fret = S.drag.origFrets[i];
        }
        S.history.exec(new MoveNoteCmd(S.drag.indices, dtimes, dstrings, dfrets));
    }

    if (S.drag.type === 'select') {
        // Select notes inside rectangle
        const x1 = Math.min(S.drag.startX, S.drag.curX);
        const y1 = Math.min(S.drag.startY, S.drag.curY);
        const x2 = Math.max(S.drag.startX, S.drag.curX);
        const y2 = Math.max(S.drag.startY, S.drag.curY);

        const nn = notes();
        const keysMode = isKeysMode();
        for (let i = 0; i < nn.length; i++) {
            const nx = timeToX(nn[i].time);
            let ny;
            if (keysMode) {
                const midi = noteToMidi(nn[i].string, nn[i].fret);
                ny = midiToY(midi) + PIANO_LANE_H / 2;
            } else {
                ny = strToY(nn[i].string) + LANE_H / 2;
            }
            if (nx >= x1 && nx <= x2 && ny >= y1 && ny <= y2) {
                S.sel.add(i);
            }
        }
    }

    S.drag = null;
    draw();
    updateStatus();
}

function onDblClick(e) {
    if (_recState === 'recording') return;  // block note addition during active take
    const { x, y } = getMousePos(e);
    const keysMode = isKeysMode();
    const laneBottom = keysMode
        ? WAVEFORM_H + pianoLaneCount() * PIANO_LANE_H
        : WAVEFORM_H + lanes() * LANE_H;
    if (y < WAVEFORM_H || y > laneBottom) return;

    const idx = hitNote(x, y);
    if (idx >= 0) return; // double-click on existing note = no-op

    // Show add-note dialog
    const t = snapTime(Math.max(0, xToTime(x)));
    if (keysMode) {
        const midi = yToMidi(y);
        showAddNote(e.clientX, e.clientY, t, midiToString(midi), midiToFret(midi));
    } else {
        const s = yToStr(y);
        showAddNote(e.clientX, e.clientY, t, s);
    }
}

function onWheel(e) {
    e.preventDefault();
    if (e.ctrlKey) {
        // Ctrl+scroll = zoom
        const { x } = getMousePos(e);
        const timeBefore = xToTime(x);
        const factor = e.deltaY < 0 ? 1.15 : 0.87;
        S.zoom = Math.max(20, Math.min(2000, S.zoom * factor));
        // Keep the time under cursor stable
        S.scrollX = timeBefore - (x - LABEL_W) / S.zoom;
        S.scrollX = Math.max(0, S.scrollX);
    } else {
        // Scroll = pan
        S.scrollX = Math.max(0, S.scrollX + e.deltaY / S.zoom * 2);
    }
    updateZoomDisplay();
    draw();
}

function onContextMenu(e) {
    e.preventDefault();
    const { x, y } = getMousePos(e);

    // Right-click on beat bar or lanes with no note = section menu
    const beatBarY = isKeysMode()
        ? WAVEFORM_H + pianoLaneCount() * PIANO_LANE_H
        : WAVEFORM_H + lanes() * LANE_H;
    if (y >= beatBarY || (y >= WAVEFORM_H && hitNote(x, y) < 0)) {
        showSectionMenu(e.clientX, e.clientY, xToTime(x));
        return;
    }

    const idx = hitNote(x, y);
    if (idx < 0) return;

    if (!S.sel.has(idx)) {
        S.sel.clear();
        S.sel.add(idx);
    }
    draw();
    showContextMenu(e.clientX, e.clientY, idx);
}

function showSectionMenu(cx, cy, time) {
    const menu = document.getElementById('editor-context-menu');
    // Check if clicking near an existing section
    let nearSection = null;
    for (const s of S.sections) {
        if (Math.abs(s.start_time - time) < 1.0) { nearSection = s; break; }
    }

    let html = '';
    html += `<button class="w-full text-left px-3 py-1 text-xs hover:bg-dark-500" data-action="add">Add Section Here</button>`;
    if (nearSection) {
        html += `<button class="w-full text-left px-3 py-1 text-xs hover:bg-dark-500" data-action="rename">Rename "${nearSection.name}"</button>`;
        html += `<button class="w-full text-left px-3 py-1 text-xs hover:bg-dark-500 text-red-400" data-action="delete">Delete "${nearSection.name}"</button>`;
    }
    menu.innerHTML = html;
    menu.querySelectorAll('[data-action]').forEach(btn => {
        btn.onclick = () => {
            hideContextMenu();
            if (btn.dataset.action === 'add') {
                const name = prompt('Section name:', 'verse');
                if (!name) return;
                const num = S.sections.filter(s => s.name === name).length + 1;
                S.sections.push({ name, number: num, start_time: snapTime(time) });
                S.sections.sort((a, b) => a.start_time - b.start_time);
                draw();
            } else if (btn.dataset.action === 'rename' && nearSection) {
                const name = prompt('New name:', nearSection.name);
                if (name) { nearSection.name = name; draw(); }
            } else if (btn.dataset.action === 'delete' && nearSection) {
                const i = S.sections.indexOf(nearSection);
                if (i >= 0) { S.sections.splice(i, 1); draw(); }
            }
        };
    });
    menu.style.left = cx + 'px';
    menu.style.top = cy + 'px';
    menu.classList.remove('hidden');
}

function onKeyDown(e) {
    // Only handle when editor screen is visible
    const screen = document.getElementById('plugin-editor');
    if (!screen || !screen.classList.contains('active')) return;

    if (e.key === ' ' && !e.target.matches('input, select, textarea')) {
        e.preventDefault();
        editorTogglePlay();
        return;
    }

    // Block all note-mutating shortcuts while a take is active so mid-take
    // edits can't be silently overwritten when arr.notes = _recNotes on Stop.
    // Spacebar (above) is still allowed because it routes to editorTogglePlay
    // → editorStopRecordMidi, which cleanly finalizes the take.
    if (_recState === 'recording') return;

    if (e.key === 'Delete' || e.key === 'Backspace') {
        if (S.sel.size && !e.target.matches('input, select, textarea')) {
            e.preventDefault();
            S.history.exec(new DeleteNotesCmd([...S.sel]));
            draw();
            updateStatus();
            return;
        }
    }

    if ((e.ctrlKey || e.metaKey) && e.key === 'z') {
        e.preventDefault();
        editorUndo();
        return;
    }
    if ((e.ctrlKey || e.metaKey) && (e.key === 'y' || (e.shiftKey && e.key === 'Z'))) {
        e.preventDefault();
        editorRedo();
        return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
        if (!e.target.matches('input, select, textarea')) {
            e.preventDefault();
            const nn = notes();
            for (let i = 0; i < nn.length; i++) S.sel.add(i);
            draw();
            return;
        }
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'c') {
        if (S.sel.size && !e.target.matches('input, select, textarea')) {
            e.preventDefault();
            const nn = notes();
            const selNotes = [...S.sel].map(i => nn[i]);
            const baseTime = Math.min(...selNotes.map(n => n.time));
            S.clipboard = {
                notes: selNotes.map(n => ({
                    time: n.time - baseTime,
                    string: n.string,
                    fret: n.fret,
                    sustain: n.sustain || 0,
                    techniques: { ...(n.techniques || {}) },
                })),
                baseTime,
            };
            setStatus(`Copied ${selNotes.length} notes`);
            return;
        }
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'v') {
        if (S.clipboard && S.clipboard.notes.length && !e.target.matches('input, select, textarea')) {
            e.preventDefault();
            const pasteTime = S.cursorTime;
            const newNotes = S.clipboard.notes.map(n => ({
                time: n.time + pasteTime,
                string: n.string,
                fret: n.fret,
                sustain: n.sustain,
                techniques: { ...(n.techniques || {}) },
            }));
            // Batch add via a compound command
            const nn = notes();
            const addCmd = {
                _notes: newNotes,
                exec() { for (const n of this._notes) nn.push(n); nn.sort((a, b) => a.time - b.time); },
                rollback() { for (const n of this._notes) { const i = nn.indexOf(n); if (i >= 0) nn.splice(i, 1); } },
            };
            S.history.exec(addCmd);
            // Select pasted notes
            S.sel.clear();
            for (const n of newNotes) { const i = nn.indexOf(n); if (i >= 0) S.sel.add(i); }
            draw();
            updateStatus();
            setStatus(`Pasted ${newNotes.length} notes at cursor`);
            return;
        }
    }
}

// ════════════════════════════════════════════════════════════════════
// Context menu
// ════════════════════════════════════════════════════════════════════

function showContextMenu(cx, cy, idx) {
    const menu = document.getElementById('editor-context-menu');
    const items = [
        { label: 'Change Fret...', action: () => promptFret(idx) },
        { label: 'Bend...', action: () => promptBend(idx) },
        { label: 'Slide To...', action: () => promptSlide(idx) },
        { label: 'Delete', action: () => { S.history.exec(new DeleteNotesCmd([...S.sel])); draw(); updateStatus(); } },
        { type: 'sep' },
        { label: 'Hammer-On', toggle: 'hammer_on', idx },
        { label: 'Pull-Off', toggle: 'pull_off', idx },
        { label: 'Palm Mute', toggle: 'palm_mute', idx },
        { label: 'Harmonic', toggle: 'harmonic', idx },
        { label: 'Accent', toggle: 'accent', idx },
        { label: 'Tap', toggle: 'tap', idx },
        { label: 'Tremolo', toggle: 'tremolo', idx },
        { label: 'Mute', toggle: 'mute', idx },
    ];

    const n = notes()[idx];
    let html = '';
    for (const it of items) {
        if (it.type === 'sep') {
            html += '<div class="border-t border-gray-700 my-1"></div>';
            continue;
        }
        if (it.toggle) {
            const techs = n.techniques || {};
            const on = techs[it.toggle];
            html += `<button class="w-full text-left px-3 py-1 text-xs hover:bg-dark-500 flex items-center gap-2" onclick="editorToggleTech(${idx},'${it.toggle}')">
                <span class="w-3">${on ? '✓' : ''}</span>${it.label}</button>`;
        } else {
            html += `<button class="w-full text-left px-3 py-1 text-xs hover:bg-dark-500" data-action="${items.indexOf(it)}">${it.label}</button>`;
        }
    }
    menu.innerHTML = html;
    // Wire up non-toggle actions
    menu.querySelectorAll('[data-action]').forEach(btn => {
        const actionItem = items[parseInt(btn.dataset.action)];
        btn.onclick = () => { hideContextMenu(); actionItem.action(); };
    });

    menu.style.left = cx + 'px';
    menu.style.top = cy + 'px';
    menu.classList.remove('hidden');
}

function hideContextMenu() {
    document.getElementById('editor-context-menu').classList.add('hidden');
}

function promptFret(idx) {
    hideContextMenu();
    const current = notes()[idx].fret;
    const val = prompt('Fret number (0-24):', current);
    if (val === null) return;
    const fret = Math.max(0, Math.min(24, parseInt(val) || 0));
    S.history.exec(new ChangeFretCmd(idx, fret));
    draw();
}

function promptBend(idx) {
    hideContextMenu();
    const n = notes()[idx];
    const techs = n.techniques || {};
    const current = techs.bend || 0;
    const val = prompt('Bend amount in semitones (0 = none, 1 = full, 0.5 = half):', current);
    if (val === null) return;
    const bend = Math.max(0, Math.min(3, parseFloat(val) || 0));
    if (!n.techniques) n.techniques = {};
    n.techniques.bend = bend;
    draw();
}

function promptSlide(idx) {
    hideContextMenu();
    const n = notes()[idx];
    const techs = n.techniques || {};
    const current = techs.slide_to >= 0 ? techs.slide_to : '';
    const val = prompt('Slide to fret (-1 or empty = no slide):', current);
    if (val === null) return;
    if (!n.techniques) n.techniques = {};
    const fret = parseInt(val);
    n.techniques.slide_to = isNaN(fret) || fret < 0 ? -1 : Math.min(24, fret);
    draw();
}

// ════════════════════════════════════════════════════════════════════
// Add note dialog
// ════════════════════════════════════════════════════════════════════

let addNoteData = null;

function showAddNote(cx, cy, time, string, fret) {
    const isKeys = isKeysMode();
    addNoteData = { time, string, fret, isKeys };
    const dlg = document.getElementById('editor-add-note-dialog');
    dlg.style.left = cx + 'px';
    dlg.style.top = cy + 'px';
    dlg.classList.remove('hidden');

    document.getElementById('editor-add-fret-col').classList.toggle('hidden', isKeys);
    document.getElementById('editor-add-pitch-col').classList.toggle('hidden', !isKeys);

    if (isKeys) {
        const midi = noteToMidi(string, fret);
        document.getElementById('editor-add-pitch-label').textContent = midiToNote(midi);
        const sus = document.getElementById('editor-add-sustain');
        sus.focus();
        sus.select();
    } else {
        const inp = document.getElementById('editor-add-fret');
        inp.value = fret != null ? String(fret) : '0';
        inp.focus();
        inp.select();
    }
}

function hideAddNote() {
    document.getElementById('editor-add-note-dialog').classList.add('hidden');
    addNoteData = null;
}

window.editorConfirmAddNote = function() {
    if (!addNoteData) return;
    const fret = addNoteData.isKeys
        ? addNoteData.fret
        : Math.max(0, Math.min(24, parseInt(document.getElementById('editor-add-fret').value) || 0));
    const sustain = Math.max(0, parseFloat(document.getElementById('editor-add-sustain').value) || 0);
    const note = {
        time: addNoteData.time,
        string: addNoteData.string,
        fret,
        sustain,
        techniques: {},
    };
    S.history.exec(new AddNoteCmd(note));
    hideAddNote();
    draw();
    updateStatus();
};

window.editorHideAddNote = hideAddNote;

// Handle Enter key in add-note dialog
document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && addNoteData) {
        e.preventDefault();
        editorConfirmAddNote();
    }
    if (e.key === 'Escape') {
        hideAddNote();
        hideContextMenu();
        editorHideLoadModal();
    }
});

// ════════════════════════════════════════════════════════════════════
// Audio / Playback
// ════════════════════════════════════════════════════════════════════

async function loadAudio(url) {
    if (!url) return;
    try {
        if (!S.audioCtx) S.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const resp = await fetch(url);
        const buf = await resp.arrayBuffer();
        S.audioBuffer = await S.audioCtx.decodeAudioData(buf);
        S.duration = S.audioBuffer.duration;
        computeWaveform();
    } catch (e) {
        console.error('Audio load error:', e);
    }
}

function computeWaveform() {
    if (!S.audioBuffer) return;
    const data = S.audioBuffer.getChannelData(0);
    const buckets = 4000;
    const peaks = new Float32Array(buckets);
    const samplesPerBucket = Math.floor(data.length / buckets);
    for (let b = 0; b < buckets; b++) {
        let max = 0;
        const start = b * samplesPerBucket;
        for (let s = 0; s < samplesPerBucket; s++) {
            const v = Math.abs(data[start + s]);
            if (v > max) max = v;
        }
        peaks[b] = max;
    }
    S.waveformPeaks = peaks;
}

function startPlayback() {
    if (!S.audioBuffer || !S.audioCtx) return;
    if (S.audioCtx.state === 'suspended') S.audioCtx.resume();
    S.audioSource = S.audioCtx.createBufferSource();
    S.audioSource.buffer = S.audioBuffer;
    S.audioSource.connect(S.audioCtx.destination);
    S.audioSource.start(0, S.cursorTime);
    S.playStartWall = S.audioCtx.currentTime;
    S.playStartTime = S.cursorTime;
    S.playing = true;
    updatePlayIcon();
    playbackTick();
}

function stopPlayback() {
    if (S.audioSource) {
        try { S.audioSource.stop(); } catch (_) {}
        S.audioSource = null;
    }
    S.playing = false;
    updatePlayIcon();
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
}

function playbackTick() {
    if (!S.playing) return;
    S.cursorTime = S.playStartTime + (S.audioCtx.currentTime - S.playStartWall);
    if (S.cursorTime >= S.duration) {
        // If a live MIDI recording is active, finalize it at the song end
        // before resetting the cursor — otherwise chartTimeNow() keeps
        // advancing past S.duration and emits notes beyond the chart.
        if (_recState === 'recording') {
            editorStopRecordMidi();
        } else {
            stopPlayback();
        }
        S.cursorTime = 0;
        updateTimeDisplay(); // reflect the reset immediately before returning
        draw();
        return; // stopPlayback() already cancelled rafId; don't re-schedule.
    }

    // Auto-scroll to follow cursor
    const cx = timeToX(S.cursorTime);
    const w = canvas ? canvas.width / DPR : 800;
    if (cx > w * 0.8) {
        S.scrollX = S.cursorTime - (w * 0.3) / S.zoom;
    }

    updateTimeDisplay();
    draw();
    rafId = requestAnimationFrame(playbackTick);
}

function updatePlayIcon() {
    const icon = document.getElementById('editor-play-icon');
    if (!icon) return;
    if (S.playing) {
        icon.innerHTML = '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>';
    } else {
        icon.innerHTML = '<path d="M8 5v14l11-7z"/>';
    }
}

function updateTimeDisplay() {
    const el = document.getElementById('editor-time-display');
    if (!el) return;
    const fmt = (t) => {
        const m = Math.floor(t / 60);
        const s = Math.floor(t % 60);
        return m + ':' + String(s).padStart(2, '0');
    };
    el.textContent = fmt(S.cursorTime) + ' / ' + fmt(S.duration);
}

// ════════════════════════════════════════════════════════════════════
// File operations
// ════════════════════════════════════════════════════════════════════

async function loadCDLC(filename) {
    setStatus('Loading ' + filename + '...');
    try {
        const resp = await fetch('/api/plugins/editor/load', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename }),
        });
        const data = await resp.json();
        if (data.error) { setStatus('Error: ' + data.error); return; }

        S.title = data.title || '';
        S.artist = data.artist || '';
        S.filename = filename;
        S.sessionId = data.session_id;
        S.format = data.format || 'psarc';
        S.arrangements = data.arrangements || [];
        // Sloppak sources don't pad tuning to 6 slots like RS XML does,
        // so a bass arrangement arriving with tuning.length === 6 from
        // a sloppak is a genuine 6-string bass (not padded 4-string).
        // Seed `_extendedStrings` so `_stringCountFor` doesn't fall
        // back to the baseline-and-ignore-length-6 heuristic for these.
        // Sloppak sources have authoritative tuning lengths (no RS
        // padding). PSARC sources still get the `tuningLen > 6` path so
        // a previously-extended-saved PSARC is detected on reload.
        _seedExtendedStringsFromTuning(S.arrangements, S.format !== 'psarc');
        S.beats = data.beats || [];
        S.sections = data.sections || [];
        S.duration = data.duration || 0;
        S.offset = data.offset || 0;
        S.currentArr = 0;
        S.sel.clear();
        S.scrollX = 0;
        S.cursorTime = 0;
        S.history = new EditHistory();

        // Reset offset UI so _effectiveAudioOffset() doesn't carry over a
        // delta from a previous session's sync nudge into this one.
        _resetOffsetUI();

        // Flatten chord notes into main notes array for unified editing
        flattenChords();
        if (isKeysMode()) updatePianoRange();

        // Update UI
        document.getElementById('editor-song-title').textContent =
            `${S.artist} — ${S.title}`;
        S.createMode = false;
        document.getElementById('editor-save-btn').disabled = false;
        document.getElementById('editor-save-btn').classList.remove('hidden');
        document.getElementById('editor-build-btn').classList.add('hidden');
        document.getElementById('editor-play-btn').disabled = !data.audio_url;
        document.getElementById('editor-sync-btn').classList.toggle('hidden', !data.audio_url);
        document.getElementById('editor-replace-audio-btn').classList.remove('hidden');
        updateArrangementSelector();
        updateStatus();
        updateTimeDisplay();
        updateBPMDisplay();

        // Load audio
        if (data.audio_url) {
            await loadAudio(data.audio_url);
        }

        draw();
        setStatus('Loaded: ' + S.artist + ' — ' + S.title);
    } catch (e) {
        setStatus('Load failed: ' + e.message);
    }
}

function updateArrangementSelector() {
    const sel = document.getElementById('editor-arrangement');
    sel.innerHTML = '';
    S.arrangements.forEach((arr, i) => {
        const opt = document.createElement('option');
        opt.value = i;
        opt.textContent = arr.name;
        sel.appendChild(opt);
    });
    sel.style.display = S.arrangements.length > 1 ? '' : 'none';
    // Re-apply the active arrangement after the rebuild so callers that
    // changed S.currentArr (e.g. + Keys / + Drums append, remove-arr)
    // don't end up with a `<select>` snapped back to option 0 while the
    // canvas edits the appended arrangement. Clamp to the valid range
    // so an out-of-bounds S.currentArr doesn't render as a blank value.
    if (S.arrangements.length > 0) {
        const idx = Math.max(0, Math.min(S.currentArr || 0, S.arrangements.length - 1));
        S.currentArr = idx;
        sel.value = String(idx);
    }

    // Show "+ Drums" button when a session is active and no drums arrangement exists
    const hasDrums = S.arrangements.some(a => /^drums/i.test(a.name || ''));
    const drumsBtn = document.getElementById('editor-add-drums-btn');
    if (drumsBtn) {
        drumsBtn.classList.toggle('hidden', !S.sessionId || hasDrums);
    }

    // Show "+ Keys" button on sloppak sessions; multiple Keys arrangements are allowed.
    const keysBtn = document.getElementById('editor-add-keys-btn');
    if (keysBtn) {
        keysBtn.classList.toggle('hidden', !S.sessionId || S.format !== 'sloppak');
    }

    // Show "⋮ Strings" tuning editor whenever a guitar/bass arrangement is
    // active (not Keys-mode — piano-roll arrangements have no string concept).
    // Available on both PSARC and sloppak; the save-time prompt handles the
    // format constraint if PSARC can't carry the result.
    const stringsBtn = document.getElementById('editor-strings-btn');
    if (stringsBtn) {
        const active = S.arrangements[S.currentArr];
        const stringsMode = !!active
            && !KEYS_PATTERN.test(active.name || '')
            && !/^drums/i.test(active.name || '');
        stringsBtn.classList.toggle('hidden', !S.sessionId || !stringsMode);
    }

    // Show "● Record" (live MIDI) button on sloppak sessions only — PSARC's
    // add-arrangement path requires an xml_path we can't synthesize, and
    // PSARC build silently drops extra arrangements anyway. Mirror the
    // "+ Keys" gate exactly so users only see Record where it persists.
    const recBtn = document.getElementById('editor-record-midi-btn');
    if (recBtn) {
        recBtn.classList.toggle('hidden', !S.sessionId || S.format !== 'sloppak');
        if (!navigator.requestMIDIAccess) {
            recBtn.disabled = true;
            recBtn.title = 'Web MIDI not available — use Chrome or Edge.';
        } else {
            recBtn.disabled = false;
            recBtn.title = 'Record a Keys arrangement live from a MIDI keyboard (Chrome/Edge)';
        }
    }

    // Show remove button when there are multiple arrangements
    const removeBtn = document.getElementById('editor-remove-arr-btn');
    if (removeBtn) {
        removeBtn.classList.toggle('hidden', S.arrangements.length <= 1);
    }
}

// ════════════════════════════════════════════════════════════════════
// Load modal
// ════════════════════════════════════════════════════════════════════

async function showLoadModal() {
    const modal = document.getElementById('editor-load-modal');
    modal.classList.remove('hidden');
    document.getElementById('editor-load-search').value = '';

    if (!S.songsList) {
        try {
            S.songsList = await fetch('/api/plugins/editor/songs').then(r => r.json());
        } catch {
            S.songsList = [];
        }
    }
    renderSongList(S.songsList);
    document.getElementById('editor-load-search').focus();
}

// Escape a string for safe interpolation into innerHTML. Covers the five
// chars that matter for HTML context (& must be first to avoid double-escape).
function _editorEscHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Reset the offset input and its applied-delta dataset, called when loading
// any session so _effectiveAudioOffset() doesn't carry over a previous nudge.
function _resetOffsetUI() {
    const el = document.getElementById('editor-offset');
    if (el) { el.value = '0'; el.dataset.applied = '0'; }
}

function _normalizeSongList(raw) {
    // Backend now returns [{filename, format}] objects. Older deployments
    // may still return plain string filenames — normalize either shape and
    // default missing fields so callers can rely on a consistent shape.
    return (raw || []).map(item => {
        if (typeof item === 'string') {
            return {
                filename: item,
                format: item.toLowerCase().endsWith('.sloppak') ? 'sloppak' : 'psarc',
            };
        }
        const filename = String(item?.filename ?? '');
        const format = String(item?.format
            ?? (filename.toLowerCase().endsWith('.sloppak') ? 'sloppak' : 'psarc'));
        return { filename, format };
    });
}

function renderSongList(files) {
    const list = document.getElementById('editor-load-list');
    files = _normalizeSongList(files);
    list.innerHTML = '';
    if (!files.length) {
        list.innerHTML = '<div class="text-xs text-gray-500 p-2">No CDLC files found</div>';
        return;
    }
    // Build the DOM imperatively so filenames never reach innerHTML.
    for (const f of files) {
        const btn = document.createElement('button');
        btn.className = 'w-full text-left px-3 py-1.5 text-xs text-gray-300 hover:bg-dark-500 rounded flex items-center gap-2';
        btn.addEventListener('click', () => editorLoadFile(f.filename));

        const name = document.createElement('span');
        name.className = 'flex-1 truncate';
        name.textContent = f.filename;
        btn.appendChild(name);

        const badge = document.createElement('span');
        const badgeColor = f.format === 'sloppak'
            ? 'bg-green-900/40 text-green-300'
            : 'bg-blue-900/40 text-blue-300';
        badge.className = `px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide ${badgeColor}`;
        badge.textContent = f.format;
        btn.appendChild(badge);

        list.appendChild(btn);
    }
}

function filterSongs(q) {
    if (!S.songsList) return;
    const list = _normalizeSongList(S.songsList);
    const low = q.toLowerCase();
    const filtered = list.filter(f => f.filename.toLowerCase().includes(low));
    renderSongList(filtered);
}

// ════════════════════════════════════════════════════════════════════
// Save
// ════════════════════════════════════════════════════════════════════

// True if the *active* arrangement has more strings than stock-RS
// PSARC can carry (>6 guitar, >4 bass). PSARC saves are
// per-arrangement (the /save endpoint only writes `arrangement_index`),
// so checking other arrangements would surface the format prompt
// even when the save would only touch a standard one — annoying for
// users who, say, edited bass while leaving an extended lead alone.
// Uses `_stringCountFor` which composes the explicit
// `_extendedStrings` counter with chord-template width and max-note-
// index signals (so a 5-string bass with no notes on the new lane
// still trips the prompt, and a 6-string bass after a high-C add
// does too because `_extendedStrings` is set).
function _activeArrangementExceedsPsarcLimit() {
    const a = S.arrangements[S.currentArr];
    if (!a) return false;
    const isBass = /bass/i.test(a.name || '');
    const roleLimit = isBass ? 4 : 6;
    return _stringCountFor(a) > roleLimit;
}

// Prep work common to all save paths: normalise chord state across
// arrangements, then return the request body for the chosen endpoint.
// `forceFullSnapshot` is true for save_as_sloppak so the new sloppak
// gets every arrangement (not just S.currentArr).
function _buildSaveBody(forceFullSnapshot) {
    if (_recState === 'recording') editorStopRecordMidi();

    const savedArr = S.currentArr;
    if (S.format === 'sloppak' || forceFullSnapshot) {
        for (let i = 0; i < S.arrangements.length; i++) {
            S.currentArr = i;
            flattenChords();
            reconstructChords();
        }
        S.currentArr = savedArr;
    } else {
        reconstructChords();
    }

    const arr = S.arrangements[S.currentArr];
    const body = {
        session_id: S.sessionId,
        arrangement_index: S.currentArr,
        notes: arr.notes,
        chords: arr.chords,
        chord_templates: arr.chord_templates,
        beats: S.beats,
        sections: S.sections,
        // Always ship title/artist so PSARC saves persist in-session
        // metadata edits too. Backend merges with session metadata
        // (album/year captured at load time) so all four fields
        // round-trip regardless of save path.
        metadata: {
            title: S.title,
            artist: S.artist,
        },
    };
    if (S.format === 'sloppak' || forceFullSnapshot) {
        body.arrangements = S.arrangements;
    }
    return body;
}

async function saveCDLC() {
    if (!S.sessionId) return;
    // PSARC can't carry >6-string guitar / >4-string bass. If the user
    // pushed past those limits while editing, ask them whether to spill
    // into a new .sloppak or accept the truncation before we touch disk.
    if (S.format === 'psarc' && _activeArrangementExceedsPsarcLimit()) {
        document.getElementById('editor-save-format-modal').classList.remove('hidden');
        return;
    }
    setStatus('Saving...');
    const body = _buildSaveBody(false);
    try {
        const resp = await fetch('/api/plugins/editor/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.error) { setStatus('Save error: ' + data.error); return; }
        setStatus('Saved successfully');
    } catch (e) {
        setStatus('Save failed: ' + e.message);
    } finally {
        flattenChords();
        draw();
    }
}

window.editorHideSaveFormatModal = () => {
    document.getElementById('editor-save-format-modal').classList.add('hidden');
};

// "Save as Sloppak" — POST the full arrangement snapshot to the new
// /save_as_sloppak route. The backend writes a .sloppak next to the
// source .psarc, then flips the session into sloppak mode so the next
// regular Save uses the native sloppak path.
window.editorSaveAsSloppakConfirm = async () => {
    document.getElementById('editor-save-format-modal').classList.add('hidden');
    if (!S.sessionId) return;
    setStatus('Saving as Sloppak...');
    const body = _buildSaveBody(true);
    try {
        const resp = await fetch('/api/plugins/editor/save_as_sloppak', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.error) { setStatus('Save error: ' + data.error); return; }
        // Flip session into sloppak mode so subsequent edits route to
        // _save_sloppak. The original PSARC stays on disk untouched.
        if (data.filename) S.filename = data.filename;
        S.format = 'sloppak';
        // Normalize in-memory tuning to the real string count so a
        // subsequent /save (which now goes through the native sloppak
        // path) doesn't serialize the RS-XML length-6 padding back into
        // the sloppak manifest — a later reload would otherwise seed
        // `_extendedStrings` from the padded length and mis-detect a
        // 4-string bass as 6-string.
        for (const arr of S.arrangements) {
            _normalizeTuningToLanes(arr, _stringCountFor(arr));
        }
        // `updateArrangementSelector` is what owns the + Keys / Strings /
        // Record toolbar gates and the remove-arrangement button. Refresh
        // it immediately so the user sees sloppak-only controls light up
        // the moment the conversion lands.
        updateArrangementSelector();
        // Prefer the relative filename over `data.path` so we don't
        // leak absolute server filesystem paths into the status UI.
        const displayName = data.filename || (data.path ? data.path.split('/').pop() : '');
        setStatus('Saved as Sloppak: ' + displayName);
    } catch (e) {
        setStatus('Save failed: ' + e.message);
    } finally {
        flattenChords();
        draw();
    }
};

// "Save as PSARC (lose extra strings)" — fall back to the regular
// /save route with `force_psarc_truncate: true`. The backend drops
// notes on string ≥ 6 (or ≥ 4 for bass) and trims chord templates
// before XML rebuild, so the resulting PSARC is internally consistent
// and works in stock Rocksmith.
window.editorSavePsarcTruncateConfirm = async () => {
    document.getElementById('editor-save-format-modal').classList.add('hidden');
    if (!S.sessionId) return;
    setStatus('Saving (extra strings will be dropped)...');
    const body = _buildSaveBody(false);
    body.force_psarc_truncate = true;
    // Ship `_extendedStrings` so the backend knows exactly how many
    // extension columns to peel — independent of RS-XML padding
    // ambiguity. A standard 4-string bass arrives with
    // tuning.length==6 (padding) but _extendedStrings==0, so the
    // backend correctly skips the peel even when another arrangement
    // triggered the modal. The backend rebuilds <tuning> from the
    // source XML's attrs (string0..string5), so we don't ship tuning
    // separately.
    const activeArr = S.arrangements[S.currentArr];
    body._extendedStrings = activeArr ? (activeArr._extendedStrings || 0) : 0;
    try {
        const resp = await fetch('/api/plugins/editor/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.error) { setStatus('Save error: ' + data.error); return; }
        setStatus('Saved as PSARC (extra strings dropped)');
    } catch (e) {
        setStatus('Save failed: ' + e.message);
    } finally {
        flattenChords();
        draw();
    }
};

// ════════════════════════════════════════════════════════════════════
// UI Helpers
// ════════════════════════════════════════════════════════════════════

function setStatus(msg) {
    const el = document.getElementById('editor-status');
    if (el) el.textContent = msg;
}

function updateStatus() {
    const nn = notes();
    const cc = chords();
    document.getElementById('editor-note-count').textContent =
        `${nn.length} notes, ${cc.length} chords` + (S.sel.size ? ` | ${S.sel.size} selected` : '');
    setStatus('Ready');
}

function updateZoomDisplay() {
    const el = document.getElementById('editor-zoom-display');
    if (el) el.textContent = Math.round(S.zoom);
}

function updateBPMDisplay() {
    const el = document.getElementById('editor-bpm');
    if (el && S.beats.length >= 2) el.value = getTabBPM().toFixed(1);
}

function resizeCanvas() {
    if (!canvas) return;
    const wrap = document.getElementById('editor-canvas-wrap');
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (w <= 0 || h <= 0) return;

    // Dynamically size lanes to fill available height
    const minBeat = 20, minWave = 50;
    BEAT_H = Math.max(minBeat, Math.floor(h * 0.05));
    WAVEFORM_H = Math.max(minWave, Math.floor(h * 0.12));
    LANE_H = Math.max(30, Math.floor((h - WAVEFORM_H - BEAT_H) / lanes()));

    canvas.width = w * DPR;
    canvas.height = h * DPR;
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    draw();
}

// ════════════════════════════════════════════════════════════════════
// Global API (called from HTML)
// ════════════════════════════════════════════════════════════════════

window.editorShowLoadModal = showLoadModal;
window.editorHideLoadModal = () => document.getElementById('editor-load-modal').classList.add('hidden');
window.editorFilterSongs = filterSongs;
window.editorLoadFile = (f) => { editorHideLoadModal(); loadCDLC(f); };
window.editorSave = saveCDLC;
window.editorUndo = () => S.history && S.history.doUndo();
window.editorRedo = () => S.history && S.history.doRedo();
window.editorTogglePlay = () => {
    // Route stops through the recorder while a take is active so the
    // spacebar (or any other transport caller) finalizes the recording
    // cleanly instead of leaving _recState stuck in 'recording'.
    if (_recState === 'recording') {
        editorStopRecordMidi();
        return;
    }
    if (S.playing) stopPlayback(); else startPlayback();
};
window.editorZoom = (dir) => {
    const factor = dir > 0 ? 1.3 : 0.77;
    S.zoom = Math.max(20, Math.min(2000, S.zoom * factor));
    updateZoomDisplay();
    draw();
};
window.editorSetSnap = (idx) => { S.snapIdx = idx; };
window.editorSetBPM = (val) => {
    const newBPM = parseFloat(val);
    if (!newBPM || newBPM <= 0 || S.beats.length < 2) return;
    const oldBPM = getTabBPM();
    const factor = oldBPM / newBPM;
    if (Math.abs(factor - 1) < 0.001) return;

    // Scale all times
    const nn = notes();
    for (const n of nn) {
        n.time *= factor;
        if (n.sustain) n.sustain *= factor;
    }
    for (const b of S.beats) b.time *= factor;
    for (const s of S.sections) s.start_time *= factor;

    draw();
    setStatus(`Tempo changed: ${oldBPM.toFixed(1)} → ${newBPM.toFixed(1)} BPM`);
};
window.editorApplyOffset = (val) => {
    const offset = parseFloat(val) || 0;
    const currentOffset = parseFloat(document.getElementById('editor-offset').dataset.applied || '0');
    const delta = offset - currentOffset;
    if (Math.abs(delta) < 0.0001) return;
    const nn = notes();
    for (const n of nn) n.time += delta;
    for (const b of S.beats) b.time += delta;
    for (const s of S.sections) s.start_time += delta;
    document.getElementById('editor-offset').dataset.applied = String(offset);
    draw();
    setStatus(`Offset: ${offset >= 0 ? '+' : ''}${(offset * 1000).toFixed(0)}ms`);
};

// Effective audio offset to send when importing a new arrangement: the
// song's loaded offset plus any UI-applied shift the user already made
// via editorApplyOffset (which moves notes/beats but never updates
// S.offset). Without this, a +Keys/+Drums import after a sync nudge
// lands out of phase with the chart the user just realigned.
function _effectiveAudioOffset() {
    const base = Number(S.offset) || 0;
    const el = document.getElementById('editor-offset');
    const applied = el ? parseFloat(el.dataset.applied || '0') || 0 : 0;
    return base + applied;
}
window.editorNudgeOffset = (delta) => {
    const el = document.getElementById('editor-offset');
    const current = parseFloat(el.value) || 0;
    el.value = (current + delta).toFixed(3);
    editorApplyOffset(el.value);
};
window.editorSelectArrangement = (val) => {
    S.currentArr = parseInt(val) || 0;
    S.sel.clear();
    flattenChords();
    if (isKeysMode()) updatePianoRange();
    draw();
    updateStatus();
};
window.editorToggleTech = (idx, tech) => {
    const n = notes()[idx];
    if (!n.techniques) n.techniques = {};
    n.techniques[tech] = !n.techniques[tech];
    hideContextMenu();
    draw();
};

// Allow loading from other plugins/screens
window.editSong = (filename) => {
    showScreen('plugin-editor');
    loadCDLC(filename);
};

// ════════════════════════════════════════════════════════════════════
// Sync Tempo — detect audio BPM and scale notes to match
// ════════════════════════════════════════════════════════════════════

let syncState = { tabBPM: 0, audioBPM: 0 };

function detectAudioBPM() {
    if (!S.audioBuffer) return 0;
    const data = S.audioBuffer.getChannelData(0);
    const sr = S.audioBuffer.sampleRate;

    // Bandpass-approximate: use short + long energy windows for spectral flux
    const winSize = 1024;
    const hopSize = 512;
    const numFrames = Math.floor((data.length - winSize) / hopSize);
    const energy = new Float32Array(numFrames);
    for (let i = 0; i < numFrames; i++) {
        let sum = 0;
        const off = i * hopSize;
        for (let j = 0; j < winSize; j++) {
            sum += data[off + j] * data[off + j];
        }
        energy[i] = Math.sqrt(sum / winSize);
    }

    // Onset: spectral flux with adaptive threshold
    const onset = new Float32Array(numFrames);
    const avgWin = 16;
    for (let i = avgWin; i < numFrames; i++) {
        const diff = Math.max(0, energy[i] - energy[i - 1]);
        // Subtract local average to suppress sustained notes
        let localAvg = 0;
        for (let j = i - avgWin; j < i; j++) localAvg += Math.max(0, energy[j] - energy[j - 1]);
        localAvg /= avgWin;
        onset[i] = Math.max(0, diff - localAvg * 1.2);
    }

    // Autocorrelation for BPM range 60-220
    const frameDur = hopSize / sr;
    const minLag = Math.floor(60 / (220 * frameDur));
    const maxLag = Math.floor(60 / (60 * frameDur));
    const useLen = Math.min(onset.length, Math.floor(30 / frameDur));

    // Collect all peaks, not just the best
    const corrs = new Float32Array(maxLag + 1);
    for (let lag = minLag; lag <= Math.min(maxLag, useLen / 2); lag++) {
        let corr = 0;
        const n = useLen - lag;
        for (let i = 0; i < n; i++) corr += onset[i] * onset[i + lag];
        corrs[lag] = corr;
    }

    // Find top peaks in autocorrelation
    const peaks = [];
    for (let lag = minLag + 1; lag < maxLag; lag++) {
        if (corrs[lag] > corrs[lag - 1] && corrs[lag] > corrs[lag + 1] && corrs[lag] > 0) {
            peaks.push({ lag, corr: corrs[lag], bpm: 60 / (lag * frameDur) });
        }
    }
    peaks.sort((a, b) => b.corr - a.corr);

    if (!peaks.length) return 120;

    // Score each candidate: prefer strong correlation + BPM in 80-180 sweet spot
    // Also check if 2x or 0.5x of a candidate has strong correlation (harmonic check)
    let bestScore = -Infinity;
    let bestBPM = peaks[0].bpm;

    for (const p of peaks.slice(0, 10)) {
        let score = p.corr;

        // Boost BPMs in the 90-180 range (most common for music)
        if (p.bpm >= 90 && p.bpm <= 180) score *= 1.5;
        else if (p.bpm >= 70 && p.bpm <= 200) score *= 1.1;

        // Check if half-tempo has strong support (penalize sub-harmonics)
        const halfLag = Math.round(p.lag / 2);
        if (halfLag >= minLag && halfLag <= maxLag && corrs[halfLag] > p.corr * 0.6) {
            // Half-lag is also strong — this candidate might be a sub-harmonic
            score *= 0.7;
        }

        // Check if double-tempo also has support (confirms this is the real beat)
        const dblLag = p.lag * 2;
        if (dblLag <= maxLag && corrs[dblLag] > p.corr * 0.3) {
            score *= 1.3;
        }

        if (score > bestScore) {
            bestScore = score;
            bestBPM = p.bpm;
        }
    }

    return bestBPM;
}

function getTabBPM() {
    if (S.beats.length < 2) return 120;
    // Find average BPM from downbeats (measure > 0)
    const downbeats = S.beats.filter(b => b.measure > 0);
    if (downbeats.length < 2) {
        // Fallback: use all consecutive beats
        let total = 0;
        for (let i = 1; i < Math.min(S.beats.length, 50); i++) {
            total += S.beats[i].time - S.beats[i - 1].time;
        }
        const avgInterval = total / (Math.min(S.beats.length, 50) - 1);
        return 60 / avgInterval;
    }
    // Measure intervals between consecutive downbeats, divide by beats per measure
    let intervals = [];
    for (let i = 1; i < downbeats.length; i++) {
        const dt = downbeats[i].time - downbeats[i - 1].time;
        // Count beats between these downbeats
        const beatsInMeasure = S.beats.filter(
            b => b.time >= downbeats[i - 1].time && b.time < downbeats[i].time
        ).length;
        if (beatsInMeasure > 0) intervals.push(dt / beatsInMeasure);
    }
    if (!intervals.length) return 120;
    const avg = intervals.reduce((a, b) => a + b, 0) / intervals.length;
    return 60 / avg;
}

window.editorSyncTempo = () => {
    if (!S.audioBuffer || S.beats.length < 2) {
        setStatus('Need audio and beats loaded for sync');
        return;
    }

    setStatus('Detecting audio BPM...');
    syncState.tabBPM = getTabBPM();
    syncState.audioBPM = detectAudioBPM();

    document.getElementById('sync-tab-bpm').textContent = syncState.tabBPM.toFixed(1);
    document.getElementById('sync-audio-bpm').textContent = syncState.audioBPM.toFixed(1);
    document.getElementById('sync-manual-bpm').value = '';
    document.getElementById('sync-offset').value = '0';
    editorSyncUpdateFactor();

    const dlg = document.getElementById('editor-sync-dialog');
    const btn = document.getElementById('editor-sync-btn');
    const rect = btn.getBoundingClientRect();
    dlg.style.left = rect.left + 'px';
    dlg.style.top = (rect.bottom + 4) + 'px';
    dlg.classList.remove('hidden');
    setStatus('Ready');
};

window.editorSyncUpdateFactor = () => {
    const manual = parseFloat(document.getElementById('sync-manual-bpm').value);
    const audioBPM = manual > 0 ? manual : syncState.audioBPM;
    const factor = audioBPM / syncState.tabBPM;
    document.getElementById('sync-factor').textContent = factor.toFixed(4);
    if (manual > 0) {
        document.getElementById('sync-audio-bpm').textContent = manual.toFixed(1) + ' (manual)';
    } else {
        document.getElementById('sync-audio-bpm').textContent = syncState.audioBPM.toFixed(1);
    }
};

window.editorHideSyncDialog = () => {
    document.getElementById('editor-sync-dialog').classList.add('hidden');
};

window.editorApplySync = () => {
    const manual = parseFloat(document.getElementById('sync-manual-bpm').value);
    const audioBPM = manual > 0 ? manual : syncState.audioBPM;
    const factor = audioBPM / syncState.tabBPM;
    const offset = parseFloat(document.getElementById('sync-offset').value) || 0;

    if (factor <= 0 || !isFinite(factor)) return;

    // Scale all note times and sustains
    const nn = notes();
    for (const n of nn) {
        n.time = n.time / factor + offset;
        if (n.sustain) n.sustain = n.sustain / factor;
    }

    // Scale beat times
    for (const b of S.beats) {
        b.time = b.time / factor + offset;
    }

    // Scale section times
    for (const s of S.sections) {
        s.start_time = s.start_time / factor + offset;
    }

    editorHideSyncDialog();
    draw();
    setStatus(`Tempo synced: scaled ${factor.toFixed(4)}x` + (offset ? `, offset ${offset}s` : ''));
};

// ════════════════════════════════════════════════════════════════════
// Create mode
// ════════════════════════════════════════════════════════════════════

let createState = {
    gpPath: null,
    tracks: null,
    audioUrl: null,
    audioMode: 'file', // 'file' or 'youtube'
    artPath: null,
};

window.editorShowCreateModal = () => {
    createState = { gpPath: null, tracks: null, audioUrl: null, audioMode: 'file', artPath: null };
    document.getElementById('editor-create-modal').classList.remove('hidden');
    document.getElementById('editor-create-tracks').classList.add('hidden');
    document.getElementById('editor-create-go').disabled = true;
    document.getElementById('editor-create-status').textContent = '';
    document.getElementById('editor-audio-status').textContent = '';
    document.getElementById('editor-create-gp').value = '';
    document.getElementById('editor-create-audio').value = '';
    document.getElementById('editor-create-yt-url').value = '';
    document.getElementById('editor-create-title').value = '';
    document.getElementById('editor-create-artist').value = '';
    document.getElementById('editor-create-album').value = '';
    document.getElementById('editor-create-year').value = '';
    editorSetAudioMode('file');
};

window.editorHideCreateModal = () => {
    document.getElementById('editor-create-modal').classList.add('hidden');
};

window.editorSetAudioMode = (mode) => {
    createState.audioMode = mode;
    document.getElementById('editor-audio-file-input').classList.toggle('hidden', mode !== 'file');
    document.getElementById('editor-audio-yt-input').classList.toggle('hidden', mode !== 'youtube');
    document.getElementById('editor-audio-mode-file').className =
        'px-3 py-1 rounded text-xs ' + (mode === 'file' ? 'bg-accent' : 'bg-dark-600 hover:bg-dark-500');
    document.getElementById('editor-audio-mode-yt').className =
        'px-3 py-1 rounded text-xs ' + (mode === 'youtube' ? 'bg-accent' : 'bg-dark-600 hover:bg-dark-500');
};

window.editorGPFileSelected = async (input) => {
    if (!input.files.length) return;
    const file = input.files[0];
    const status = document.getElementById('editor-create-status');
    status.textContent = 'Uploading Guitar Pro file...';

    const form = new FormData();
    form.append('file', file);

    try {
        const resp = await fetch('/api/plugins/editor/import-gp', { method: 'POST', body: form });
        const data = await resp.json();
        if (data.error) { status.textContent = 'Error: ' + data.error; return; }

        createState.gpPath = data.gp_path;
        createState.tracks = data.tracks;

        // Show track list
        const listEl = document.getElementById('editor-create-track-list');
        listEl.innerHTML = data.tracks.map(t => {
            const isDrums = !!(t.is_drums || t.is_percussion);
            const badge = isDrums ? ' (drums)'
                : t.is_piano ? ' (keys)'
                : '';
            const disabled = t.notes === 0;
            const safeName = _editorEscHtml(t.name);
            return `<label class="flex items-center gap-2 text-xs text-gray-300 py-0.5">
                <input type="checkbox" value="${t.index}" checked
                    class="accent-accent" ${disabled ? 'disabled' : ''}>
                <span class="${isDrums ? 'text-red-300' : t.is_piano ? 'text-indigo-300' : ''}">${safeName}</span>
                <span class="text-gray-600">${Number(t.strings) || 0}str, ${Number(t.notes) || 0} notes${badge}</span>
            </label>`;
        }).join('');
        document.getElementById('editor-create-tracks').classList.remove('hidden');

        // Auto-fill title from filename
        const stem = file.name.replace(/\.(gp[345x]?|gpx)$/i, '');
        if (!document.getElementById('editor-create-title').value) {
            document.getElementById('editor-create-title').value = stem;
        }

        status.textContent = `Parsed: ${data.tracks.length} tracks found`;
        updateCreateButton();
    } catch (e) {
        status.textContent = 'Upload failed: ' + e.message;
    }
};

// Shared upload helper for the Create modal and the Replace Audio modal.
// Returns the new audio URL on success or null on missing input / failure.
// The caller is responsible for any "missing input" UX (the helper returns
// null silently in that case so its callers can decide whether to show a
// message — `uploadCreateAudio`'s caller prechecks; the replace flow shows
// a "Choose a file" hint).
async function _uploadAudioForMode({ mode, ytInputId, fileInputId, statusEl }) {
    if (mode === 'youtube') {
        const url = document.getElementById(ytInputId).value.trim();
        if (!url) return null;
        statusEl.textContent = 'Downloading from YouTube...';
        try {
            const resp = await fetch('/api/plugins/editor/youtube-audio', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url }),
            });
            const data = await resp.json();
            if (data.error) { statusEl.textContent = 'Error: ' + data.error; return null; }
            statusEl.textContent = 'Audio ready: ' + (data.title || 'downloaded');
            return data.audio_url;
        } catch (e) {
            statusEl.textContent = 'Download failed: ' + e.message;
            return null;
        }
    }
    const input = document.getElementById(fileInputId);
    if (!input.files.length) return null;
    statusEl.textContent = 'Uploading audio...';
    const form = new FormData();
    form.append('file', input.files[0]);
    try {
        const resp = await fetch('/api/plugins/editor/upload-audio', { method: 'POST', body: form });
        const data = await resp.json();
        if (data.error) { statusEl.textContent = 'Error: ' + data.error; return null; }
        statusEl.textContent = 'Audio uploaded';
        return data.audio_url;
    } catch (e) {
        statusEl.textContent = 'Upload failed: ' + e.message;
        return null;
    }
}

async function uploadCreateAudio() {
    const url = await _uploadAudioForMode({
        mode: createState.audioMode,
        ytInputId: 'editor-create-yt-url',
        fileInputId: 'editor-create-audio',
        statusEl: document.getElementById('editor-audio-status'),
    });
    if (!url) return false;
    createState.audioUrl = url;
    return true;
}

function updateCreateButton() {
    const hasGP = !!createState.gpPath;
    const hasAudio = createState.audioMode === 'youtube'
        ? !!document.getElementById('editor-create-yt-url').value.trim()
        : !!(document.getElementById('editor-create-audio').files || []).length;
    document.getElementById('editor-create-go').disabled = !hasGP;
}

// Wire up input change events for enabling the create button
document.addEventListener('change', (e) => {
    if (e.target.id === 'editor-create-audio') updateCreateButton();
});
document.addEventListener('input', (e) => {
    if (e.target.id === 'editor-create-yt-url') updateCreateButton();
});

window.editorDoCreate = async () => {
    if (!createState.gpPath) return;
    const status = document.getElementById('editor-create-status');
    const btn = document.getElementById('editor-create-go');
    btn.disabled = true;

    // Upload/download audio first
    const hasAudioInput = createState.audioMode === 'youtube'
        ? !!document.getElementById('editor-create-yt-url').value.trim()
        : !!(document.getElementById('editor-create-audio').files || []).length;

    if (hasAudioInput && !createState.audioUrl) {
        const ok = await uploadCreateAudio();
        if (!ok) { btn.disabled = false; return; }
    }

    // Get selected track indices
    const checkboxes = document.querySelectorAll('#editor-create-track-list input[type=checkbox]:checked:not(:disabled)');
    const trackIndices = [...checkboxes].map(cb => parseInt(cb.value));

    status.textContent = 'Converting Guitar Pro to Rocksmith...';

    try {
        const resp = await fetch('/api/plugins/editor/convert-gp', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                gp_path: createState.gpPath,
                audio_url: createState.audioUrl || '',
                track_indices: trackIndices.length ? trackIndices : null,
                title: document.getElementById('editor-create-title').value || 'Untitled',
                artist: document.getElementById('editor-create-artist').value || 'Unknown',
                album: document.getElementById('editor-create-album').value || '',
                year: document.getElementById('editor-create-year').value || '',
            }),
        });
        const data = await resp.json();
        if (data.error) { status.textContent = 'Error: ' + data.error; btn.disabled = false; return; }

        // Load into editor
        editorHideCreateModal();
        S.title = data.title || '';
        S.artist = data.artist || '';
        S.filename = '';
        S.sessionId = data.session_id;
        S.format = 'psarc';
        S.arrangements = data.arrangements || [];
        // Create-mode (fresh GP import) — gp2rs builds tuning to the
        // actual string count, so length 6 means a genuine 6-string
        // bass / standard guitar (not RS-XML padding). Seed
        // `_extendedStrings` to keep `_stringCountFor` honest.
        _seedExtendedStringsFromTuning(S.arrangements, /* authoritative */ true);
        S.beats = data.beats || [];
        S.sections = data.sections || [];
        S.duration = data.duration || 0;
        S.offset = data.offset || 0;
        S.currentArr = 0;
        S.sel.clear();
        S.scrollX = 0;
        S.cursorTime = 0;
        S.history = new EditHistory();
        S.createMode = true;

        // Reset offset UI so _effectiveAudioOffset() doesn't carry over a
        // delta from a previous session's sync nudge.
        _resetOffsetUI();

        flattenChords();
        if (isKeysMode()) updatePianoRange();

        document.getElementById('editor-song-title').textContent =
            `${S.artist} — ${S.title} (new)`;
        document.getElementById('editor-save-btn').classList.add('hidden');
        document.getElementById('editor-build-btn').classList.remove('hidden');
        document.getElementById('editor-play-btn').disabled = !data.audio_url;
        document.getElementById('editor-sync-btn').classList.toggle('hidden', !data.audio_url);
        document.getElementById('editor-replace-audio-btn').classList.remove('hidden');
        updateArrangementSelector();
        updateStatus();
        updateTimeDisplay();
        updateBPMDisplay();

        if (data.audio_url) await loadAudio(data.audio_url);
        draw();
        setStatus('Imported — edit notes then click Build CDLC');
    } catch (e) {
        status.textContent = 'Import failed: ' + e.message;
        btn.disabled = false;
    }
};

window.editorBuild = async () => {
    if (!S.sessionId || !S.createMode) return;
    setStatus('Building CDLC...');

    // Reconstruct chords for ALL arrangements before sending
    const savedArr = S.currentArr;
    const allArrangements = [];
    for (let i = 0; i < S.arrangements.length; i++) {
        S.currentArr = i;
        reconstructChords();
        const arr = S.arrangements[i];
        allArrangements.push({
            name: arr.name,
            // Ship tuning + capo so the backend's `_is_extended_range`
            // tuning-length check fires for arrangements where the
            // user extended via the Strings modal but hasn't placed
            // notes on the new lanes yet. Without these the build
            // would route to PSARC and then crash inside RsCli's SNG
            // compiler when it sees the >6 tuning slots.
            tuning: Array.isArray(arr.tuning) ? arr.tuning.slice() : [0, 0, 0, 0, 0, 0],
            capo: arr.capo || 0,
            // Explicit extension counter — required for the 6-string
            // bass case where tuning.length==6 is ambiguous between
            // RS-padded 4-string and genuine 6-string. Backend's
            // `_is_extended_range` consumes this signal too.
            _extendedStrings: arr._extendedStrings || 0,
            notes: arr.notes,
            chords: arr.chords,
            chord_templates: arr.chord_templates,
        });
    }
    S.currentArr = savedArr;

    // Upload album art if selected
    const artInput = document.getElementById('editor-create-art');
    if (artInput && artInput.files && artInput.files.length && !createState.artPath) {
        const form = new FormData();
        form.append('file', artInput.files[0]);
        try {
            const r = await fetch('/api/plugins/editor/upload-art', { method: 'POST', body: form });
            const d = await r.json();
            if (d.art_path) createState.artPath = d.art_path;
        } catch (_) {}
    }

    try {
        const resp = await fetch('/api/plugins/editor/build', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: S.sessionId,
                arrangements: allArrangements,
                beats: S.beats,
                sections: S.sections,
                audio_url: createState.audioUrl || '',
                art_path: createState.artPath || '',
                metadata: {
                    title: S.title,
                    artist: S.artist,
                    artistName: S.artist,
                },
            }),
        });
        const data = await resp.json();
        if (data.error) { setStatus('Build error: ' + data.error); return; }
        setStatus('CDLC built: ' + data.path);
    } catch (e) {
        setStatus('Build failed: ' + e.message);
    } finally {
        // Re-flatten current arrangement for continued editing
        flattenChords();
        draw();
    }
};

// ════════════════════════════════════════════════════════════════════
// Replace audio
// ════════════════════════════════════════════════════════════════════

let replaceAudioState = { audioMode: 'file' };

window.editorShowReplaceAudioModal = () => {
    if (!S.sessionId) return;
    replaceAudioState = { audioMode: 'file' };
    document.getElementById('editor-replace-audio').value = '';
    document.getElementById('editor-replace-yt-url').value = '';
    document.getElementById('editor-replace-audio-status').textContent = '';
    document.getElementById('editor-replace-audio-apply').disabled = false;
    document.getElementById('editor-replace-audio-modal').classList.remove('hidden');
    editorSetReplaceAudioMode('file');
};

window.editorHideReplaceAudioModal = () => {
    document.getElementById('editor-replace-audio-modal').classList.add('hidden');
};

window.editorSetReplaceAudioMode = (mode) => {
    replaceAudioState.audioMode = mode;
    document.getElementById('editor-replace-audio-file-input').classList.toggle('hidden', mode !== 'file');
    document.getElementById('editor-replace-audio-yt-input').classList.toggle('hidden', mode !== 'youtube');
    document.getElementById('editor-replace-mode-file').className =
        'px-3 py-1 rounded text-xs ' + (mode === 'file' ? 'bg-accent' : 'bg-dark-600 hover:bg-dark-500');
    document.getElementById('editor-replace-mode-yt').className =
        'px-3 py-1 rounded text-xs ' + (mode === 'youtube' ? 'bg-accent' : 'bg-dark-600 hover:bg-dark-500');
};

async function _uploadReplaceAudio() {
    const statusEl = document.getElementById('editor-replace-audio-status');
    // Pre-check missing input so we surface a hint here (the shared helper
    // returns null silently on missing input so the create-modal flow's
    // optional-audio path keeps its existing no-status behavior).
    if (replaceAudioState.audioMode === 'youtube') {
        if (!document.getElementById('editor-replace-yt-url').value.trim()) {
            statusEl.textContent = 'Enter a YouTube URL';
            return null;
        }
    } else if (!document.getElementById('editor-replace-audio').files.length) {
        statusEl.textContent = 'Choose a file';
        return null;
    }
    return _uploadAudioForMode({
        mode: replaceAudioState.audioMode,
        ytInputId: 'editor-replace-yt-url',
        fileInputId: 'editor-replace-audio',
        statusEl,
    });
}

window.editorApplyReplaceAudio = async () => {
    if (!S.sessionId) return;
    const status = document.getElementById('editor-replace-audio-status');
    const apply = document.getElementById('editor-replace-audio-apply');
    apply.disabled = true;
    try {
        const audioUrl = await _uploadReplaceAudio();
        if (!audioUrl) { apply.disabled = false; return; }

        const resp = await fetch('/api/plugins/editor/replace-audio', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: S.sessionId, audio_url: audioUrl }),
        });
        const data = await resp.json();
        if (data.error) {
            status.textContent = 'Error: ' + data.error;
            apply.disabled = false;
            return;
        }

        // Keep create-mode build in sync — Build CDLC reads createState.audioUrl.
        if (S.createMode) createState.audioUrl = audioUrl;

        // Stop active playback before swapping the buffer; otherwise the old
        // BufferSource keeps playing under the new S.audioBuffer/duration and
        // playbackTick desyncs against the new track length.
        if (S.playing) stopPlayback();
        // loadAudio() swallows fetch/decode errors and only logs to console,
        // so detect failure by checking that the buffer reference actually
        // changed. Without this we would close the modal and announce
        // "Audio replaced" even on an unsupported / corrupt upload.
        const prevBuffer = S.audioBuffer;
        await loadAudio(audioUrl);
        if (!S.audioBuffer || S.audioBuffer === prevBuffer) {
            status.textContent = 'Failed to decode audio (unsupported format?)';
            apply.disabled = false;
            return;
        }
        if (S.cursorTime > S.duration) S.cursorTime = 0;
        document.getElementById('editor-play-btn').disabled = false;
        document.getElementById('editor-sync-btn').classList.remove('hidden');
        updateTimeDisplay();
        draw();

        const HINTS = {
            none:    'Audio replaced',
            save:    'Audio replaced (Save to persist to .sloppak)',
            build:   'Audio replaced (will persist on next Build CDLC)',
            rebuild: "Audio replaced (playback only — PSARC won't be repacked)",
        };
        editorHideReplaceAudioModal();
        setStatus(HINTS[data.next_step] || (data.persisted ? HINTS.none : HINTS.rebuild));
    } catch (e) {
        status.textContent = 'Failed: ' + e.message;
        apply.disabled = false;
    }
};

// ════════════════════════════════════════════════════════════════════
// Init
// ════════════════════════════════════════════════════════════════════

function init() {
    canvas = document.getElementById('editor-canvas');
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    S.history = new EditHistory();

    canvas.addEventListener('mousedown', onMouseDown);
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    canvas.addEventListener('dblclick', onDblClick);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('contextmenu', onContextMenu);
    document.addEventListener('keydown', onKeyDown);

    // Prevent middle-click paste
    canvas.addEventListener('auxclick', (e) => { if (e.button === 1) e.preventDefault(); });

    resizeCanvas();
    window.addEventListener('resize', resizeCanvas);

    // Observe screen visibility for resize
    const obs = new MutationObserver(() => {
        const screen = document.getElementById('plugin-editor');
        if (screen && screen.classList.contains('active')) {
            setTimeout(resizeCanvas, 50);
        }
    });
    const screen = document.getElementById('plugin-editor');
    if (screen) obs.observe(screen, { attributes: true, attributeFilter: ['class'] });

    draw();
}

// ════════════════════════════════════════════════════════════════════
// Remove arrangement
// ════════════════════════════════════════════════════════════════════

window.editorRemoveArrangement = async () => {
    if (_recState !== 'idle') {
        setStatus('Cannot remove an arrangement while recording. Stop the take first.');
        return;
    }
    if (S.arrangements.length <= 1) return;
    const removeIdx = S.currentArr;
    const arr = S.arrangements[removeIdx];
    if (!confirm(`Remove "${arr.name}" arrangement?`)) return;

    // Remove from backend first
    if (S.sessionId) {
        try {
            const resp = await fetch('/api/plugins/editor/remove-arrangement', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_id: S.sessionId,
                    arrangement_index: removeIdx,
                }),
            });
            const result = await resp.json();
            if (result.error) {
                setStatus('Remove failed: ' + result.error);
                return;
            }
        } catch (e) {
            setStatus('Remove failed: ' + e.message);
            return;
        }
    }

    // Then update frontend state
    S.arrangements.splice(removeIdx, 1);
    S.currentArr = Math.min(removeIdx, S.arrangements.length - 1);
    S.sel.clear();
    flattenChords();
    updateArrangementSelector();
    document.getElementById('editor-arrangement').value = S.currentArr;
    updateStatus();
    draw();
    setStatus(`Removed "${arr.name}" arrangement`);
};

// ════════════════════════════════════════════════════════════════════
// Add Drums arrangement from GP file
// ════════════════════════════════════════════════════════════════════

let _addDrumsGpPath = null;

window.editorShowAddDrumsModal = () => {
    _addDrumsGpPath = null;
    document.getElementById('editor-add-drums-modal').classList.remove('hidden');
    document.getElementById('editor-add-drums-tracks').classList.add('hidden');
    document.getElementById('editor-add-drums-go').disabled = true;
    document.getElementById('editor-add-drums-status').textContent = '';
    const fileInput = document.getElementById('editor-add-drums-gp');
    if (fileInput) fileInput.value = '';
};

window.editorHideAddDrumsModal = () => {
    document.getElementById('editor-add-drums-modal').classList.add('hidden');
};

window.editorDrumsGPSelected = async (input) => {
    const file = input.files[0];
    if (!file) return;

    const statusEl = document.getElementById('editor-add-drums-status');
    statusEl.textContent = 'Parsing GP file...';

    // Drop any state from a previous successful parse so a later parse
    // failure (or empty-tracks result) can't be silently committed via
    // editorDoAddDrums using the older file's path.
    _addDrumsGpPath = null;
    document.getElementById('editor-add-drums-go').disabled = true;
    document.getElementById('editor-add-drums-tracks').classList.add('hidden');

    const formData = new FormData();
    formData.append('file', file);

    try {
        const resp = await fetch('/api/plugins/editor/import-gp', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();
        if (data.error) {
            statusEl.textContent = 'Error: ' + data.error;
            return;
        }

        // Show only drum/percussion tracks (accept legacy is_percussion alias
        // from older slopsmith server.py revisions).
        const tracks = data.tracks || [];
        const drumTracks = tracks.filter(t => (t.is_drums || t.is_percussion) && t.notes > 0);
        if (drumTracks.length === 0) {
            statusEl.textContent = 'No drum/percussion tracks found in this file.';
            // Leave the cleared state from above in place — no usable
            // tracks means editorDoAddDrums must remain disabled.
            return;
        }

        // Only commit the new state once we know there's a usable track set.
        _addDrumsGpPath = data.gp_path;

        const listEl = document.getElementById('editor-add-drums-track-list');
        listEl.innerHTML = drumTracks.map(t => {
            const safeName = _editorEscHtml(t.name);
            return `<label class="flex items-center gap-2 text-xs text-gray-300 py-0.5">
                <input type="radio" name="drums-track" value="${t.index}" checked class="accent-red-500">
                <span class="text-red-300">${safeName}</span>
                <span class="text-gray-600">${Number(t.notes) || 0} notes</span>
            </label>`;
        }).join('');
        document.getElementById('editor-add-drums-tracks').classList.remove('hidden');
        document.getElementById('editor-add-drums-go').disabled = false;
        statusEl.textContent = `Found ${drumTracks.length} drum track(s).`;
    } catch (e) {
        statusEl.textContent = 'Failed: ' + e.message;
    }
};

window.editorDoAddDrums = async () => {
    if (!_addDrumsGpPath || !S.sessionId) return;

    const statusEl = document.getElementById('editor-add-drums-status');
    const goBtn = document.getElementById('editor-add-drums-go');
    goBtn.disabled = true;
    statusEl.textContent = 'Importing drum track...';

    // Get selected track index
    const radio = document.querySelector('input[name="drums-track"]:checked');
    const trackIndex = radio ? parseInt(radio.value) : 0;

    try {
        // Import the drum track
        const resp = await fetch('/api/plugins/editor/import-drums', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                gp_path: _addDrumsGpPath,
                track_index: trackIndex,
                audio_offset: _effectiveAudioOffset(),
            }),
        });
        const data = await resp.json();
        if (data.error) {
            statusEl.textContent = 'Error: ' + data.error;
            goBtn.disabled = false;
            return;
        }

        // Add to current session
        const addResp = await fetch('/api/plugins/editor/add-arrangement', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: S.sessionId,
                arrangement: data.arrangement,
                xml_path: data.xml_path,
            }),
        });
        const addResult = await addResp.json().catch(() => ({}));
        if (!addResp.ok || addResult.error) {
            statusEl.textContent = 'Error adding: ' + (addResult.error || addResp.status);
            goBtn.disabled = false;
            return;
        }

        // Add the arrangement to local state
        S.arrangements.push(data.arrangement);
        S.currentArr = S.arrangements.length - 1;
        const sel = document.getElementById('editor-arrangement');
        sel.value = S.currentArr;

        flattenChords();
        updateArrangementSelector();
        updateStatus();
        draw();

        editorHideAddDrumsModal();
        setStatus('Added Drums arrangement (' + data.arrangement.notes.length + ' notes)');
    } catch (e) {
        statusEl.textContent = 'Failed: ' + e.message;
        goBtn.disabled = false;
    }
};

// ════════════════════════════════════════════════════════════════════
// Strings (tuning) editor — add/remove strings on the active arrangement
// ════════════════════════════════════════════════════════════════════

// Range per role. Bass extends low-then-high (4 → 5 add low B → 6 add high
// C); guitar extends low-only (6 → 7 low B → 8 low F#).
function _stringsRangeForActive() {
    const arr = S.arrangements[S.currentArr];
    const isBass = arr && /bass/i.test(arr.name || '');
    return isBass
        ? { min: 4, max: 6, defaultPos: 'low' }
        : { min: 6, max: 8, defaultPos: 'low' };
}

function _nextAddPosition(arr, isBass) {
    // Use `_stringCountFor(arr)` so the result is anchored to the
    // passed arrangement (not whichever one is currently visible).
    // It already disambiguates RS-XML padding from a genuine
    // extended count — without that, a 4-string bass with padded
    // length-6 tuning would be treated as "5→6 high-C add" instead
    // of the expected "4→5 low-B".
    const cur = _stringCountFor(arr);
    if (isBass && cur === 5) return 'high';  // 5→6 bass adds high C
    return 'low';
}

function _notesOnString(arr, idx) {
    let count = 0;
    for (const n of arr.notes || []) if (n.string === idx) count += 1;
    for (const ch of arr.chords || []) {
        for (const cn of ch.notes || []) if (cn.string === idx) count += 1;
    }
    return count;
}

function _renderStringsModal() {
    const arr = S.arrangements[S.currentArr];
    if (!arr) return;
    const labels = laneLabels();           // low → high, length === lanes()
    // Normalize the display tuning to the real string count so we don't
    // surface RS-XML padding zeros as if they were real strings.
    const tuning = (arr.tuning || []).slice(0, labels.length);
    while (tuning.length < labels.length) tuning.push(0);
    const { min, max } = _stringsRangeForActive();
    const isBass = /bass/i.test(arr.name || '');

    const summary = document.getElementById('editor-strings-summary');
    if (summary) {
        summary.textContent = `${arr.name || 'Arrangement'} — ${labels.length} string${labels.length === 1 ? '' : 's'} (${isBass ? 'bass' : 'guitar'}; range ${min}–${max})`;
    }

    const list = document.getElementById('editor-strings-list');
    if (list) {
        // Build rows with createElement / textContent rather than
        // innerHTML — `tuning[i]` arrives from imported/edited JSON
        // and could be non-numeric, so interpolating it raw would
        // open a DOM-injection vector. Coercing to Number defends
        // both against bad input AND against future code that may
        // surface `lbl` values that aren't already HTML-safe.
        // Display low → high so it reads naturally; `tuning` is also
        // low → high in RS XML order, so iterating tuning matches.
        list.textContent = '';
        for (let i = 0; i < labels.length; i++) {
            const lbl = labels[i];
            const rawOff = tuning[i];
            const off = Number.isFinite(Number(rawOff)) ? Number(rawOff) : 0;
            const offTxt = off === 0 ? '0' : (off > 0 ? `+${off}` : `${off}`);
            const row = document.createElement('div');
            row.className = 'flex justify-between bg-dark-800 rounded px-2 py-1';
            const left = document.createElement('span');
            left.textContent = `String ${i} (${lbl})`;
            const right = document.createElement('span');
            right.className = 'text-gray-500';
            right.textContent = `${offTxt} st`;
            row.appendChild(left);
            row.appendChild(right);
            list.appendChild(row);
        }
    }

    const addBtn = document.getElementById('editor-strings-add');
    const removeBtn = document.getElementById('editor-strings-remove');
    const warn = document.getElementById('editor-strings-warning');
    const curCount = labels.length;  // === lanes()
    if (addBtn) addBtn.disabled = curCount >= max;
    if (removeBtn) {
        // Only the most-recently-added low/high string is removable, and
        // only if no notes live on it. For 6-bass, that's the high C
        // (last index). For everything else it's the low extension
        // (index 0). We mirror the add-position logic.
        const pos = curCount === 6 && isBass ? 'high' : 'low';
        const targetIdx = pos === 'low' ? 0 : curCount - 1;
        const blockers = _notesOnString(arr, targetIdx);
        const atFloor = curCount <= min;
        removeBtn.disabled = atFloor || blockers > 0;
        if (warn) {
            if (atFloor) {
                warn.textContent = `Already at the minimum ${min} strings.`;
            } else if (blockers > 0) {
                warn.textContent = `${blockers} note${blockers === 1 ? '' : 's'} on string ${targetIdx} — delete or move them before removing.`;
            } else {
                warn.textContent = '';
            }
        }
    }
}

window.editorShowStringsModal = () => {
    const arr = S.arrangements[S.currentArr];
    if (!arr) return;
    if (KEYS_PATTERN.test(arr.name || '') || /^drums/i.test(arr.name || '')) return;
    document.getElementById('editor-strings-modal').classList.remove('hidden');
    _renderStringsModal();
};

window.editorHideStringsModal = () => {
    document.getElementById('editor-strings-modal').classList.add('hidden');
};

window.editorAddString = () => {
    const arr = S.arrangements[S.currentArr];
    if (!arr) return;
    const isBass = /bass/i.test(arr.name || '');
    const { max } = _stringsRangeForActive();
    // Compute the count directly from the active arrangement rather
    // than going through `lanes()` — the latter consults a per-draw
    // cache and our intent here is explicitly "what is this
    // arrangement's current string count?", independent of draw state.
    if (_stringCountFor(arr) >= max) return;
    const pos = _nextAddPosition(arr, isBass);
    // The command's exec() calls _resizeForLaneChange() itself, which
    // covers undo/redo too — no need to duplicate the resize here.
    S.history.exec(new AddStringCmd(S.currentArr, pos));
    _renderStringsModal();
    draw();
    updateStatus();
};

window.editorRemoveString = () => {
    const arr = S.arrangements[S.currentArr];
    if (!arr) return;
    const isBass = /bass/i.test(arr.name || '');
    const { min } = _stringsRangeForActive();
    // Same reasoning as editorAddString — anchor on `arr` directly
    // rather than the cached `lanes()`.
    const cur = _stringCountFor(arr);
    if (cur <= min) return;
    // Mirror the position logic from add: 6-bass removes high (last),
    // everything else removes the low extension (index 0).
    const pos = cur === 6 && isBass ? 'high' : 'low';
    const targetIdx = pos === 'low' ? 0 : cur - 1;
    if (_notesOnString(arr, targetIdx) > 0) return;  // UI button is disabled too
    // The command's exec() handles the resize internally (covers
    // undo/redo too); see editorAddString.
    S.history.exec(new RemoveStringCmd(S.currentArr, pos));
    _renderStringsModal();
    draw();
    updateStatus();
};

// ════════════════════════════════════════════════════════════════════
// Add Keys arrangement (sloppak — GP or MIDI source)
// ════════════════════════════════════════════════════════════════════

let _addKeysSourcePath = null;       // server-side path to the uploaded file
let _addKeysSourceFormat = null;     // 'gp' or 'midi'
// Cached after a successful list-tracks call; the keys-track radio value
// is an index into this array, not the track's MIDI/GP index, because
// format-0 channel splits can yield multiple picker entries sharing the
// same MIDI `index`.
let _addKeysSortedTracks = [];

window.editorShowAddKeysModal = () => {
    if (S.format !== 'sloppak') return;
    document.getElementById('editor-add-keys-modal').classList.remove('hidden');
    document.getElementById('editor-add-keys-tracks').classList.add('hidden');
    document.getElementById('editor-add-keys-go').disabled = true;
    document.getElementById('editor-add-keys-status').textContent = '';
    const fi = document.getElementById('editor-add-keys-file');
    if (fi) fi.value = '';
    _addKeysSourcePath = null;
    _addKeysSourceFormat = null;
};

window.editorHideAddKeysModal = () => {
    document.getElementById('editor-add-keys-modal').classList.add('hidden');
};

window.editorKeysFileSelected = async (input) => {
    const file = input.files && input.files[0];
    if (!file) return;
    const statusEl = document.getElementById('editor-add-keys-status');
    statusEl.textContent = 'Parsing ' + file.name + '...';

    // Drop any state from a previous successful parse so a later parse
    // failure (or empty-tracks result) can't be silently committed via
    // editorDoAddKeys using the older file's path.
    _addKeysSourcePath = null;
    _addKeysSortedTracks = [];
    document.getElementById('editor-add-keys-go').disabled = true;
    document.getElementById('editor-add-keys-tracks').classList.add('hidden');

    const lower = file.name.toLowerCase();
    const isMidi = lower.endsWith('.mid') || lower.endsWith('.midi');
    _addKeysSourceFormat = isMidi ? 'midi' : 'gp';

    const fd = new FormData();
    fd.append('file', file);

    try {
        const url = isMidi
            ? '/api/plugins/editor/import-midi'
            : '/api/plugins/editor/import-gp';
        const resp = await fetch(url, { method: 'POST', body: fd });
        const data = await resp.json();
        if (data.error) {
            statusEl.textContent = 'Error: ' + data.error;
            return;
        }
        const tracks = data.tracks || [];
        // Surface piano-flagged tracks first; include all so the user can override.
        const sorted = tracks.slice().sort((a, b) => {
            const ap = (a.is_piano ? 0 : 1);
            const bp = (b.is_piano ? 0 : 1);
            if (ap !== bp) return ap - bp;
            return (b.notes || 0) - (a.notes || 0);
        });

        if (sorted.length === 0) {
            statusEl.textContent = 'No tracks found in this file.';
            // Leave the cleared state from above in place — no usable
            // tracks means editorDoAddKeys must remain disabled.
            return;
        }

        // Only commit the new state once we know there's a usable track set.
        _addKeysSourcePath = isMidi ? data.midi_path : data.gp_path;
        // Stash so editorDoAddKeys can resolve the radio value back to the
        // full track entry (it carries both `index` and `channel_filter`,
        // which can collide if a format-0 file produced multiple entries
        // sharing the same `index`).
        _addKeysSortedTracks = sorted;

        const listEl = document.getElementById('editor-add-keys-track-list');
        const firstPianoPos = sorted.findIndex(t => t.is_piano);
        const defaultPos = firstPianoPos >= 0 ? firstPianoPos : 0;
        // Radio value is the position in `sorted` (not t.index) because
        // format-0 channel splits produce multiple entries that share the
        // same MIDI track_index — we need a unique key.
        listEl.innerHTML = sorted.map((t, pos) => {
            const checked = pos === defaultPos ? 'checked' : '';
            const isDrums = !!(t.is_drums || t.is_percussion);
            const flag = t.is_piano ? '<span class="text-indigo-300">[keys]</span>' : '';
            const drumsTag = isDrums ? '<span class="text-red-400">[drums]</span>' : '';
            const safeName = _editorEscHtml(t.name || '') || _editorEscHtml('Track ' + t.index);
            return `<label class="flex items-center gap-2 text-xs text-gray-300 py-0.5">
                <input type="radio" name="keys-track" value="${pos}" ${checked} class="accent-indigo-500">
                <span class="text-gray-200">${safeName}</span>
                ${flag} ${drumsTag}
                <span class="text-gray-600 ml-auto">${Number(t.notes) || 0} notes</span>
            </label>`;
        }).join('');
        document.getElementById('editor-add-keys-tracks').classList.remove('hidden');
        document.getElementById('editor-add-keys-go').disabled = false;
        const found = sorted.filter(t => t.is_piano).length;
        statusEl.textContent = found > 0
            ? `Found ${found} keyboard track(s). Pick one.`
            : `No tracks auto-flagged as keyboard — pick one manually.`;
    } catch (e) {
        statusEl.textContent = 'Failed: ' + e.message;
    }
};

window.editorDoAddKeys = async () => {
    if (!_addKeysSourcePath || !S.sessionId) return;
    const statusEl = document.getElementById('editor-add-keys-status');
    const goBtn = document.getElementById('editor-add-keys-go');
    goBtn.disabled = true;
    statusEl.textContent = 'Importing keys track...';

    const radio = document.querySelector('input[name="keys-track"]:checked');
    // Radio value is a position in _addKeysSortedTracks; resolve it back to
    // the full entry so we can pull both `index` and `channel_filter`.
    const pos = radio ? parseInt(radio.value) : 0;
    const picked = _addKeysSortedTracks[pos] || _addKeysSortedTracks[0];
    if (!picked) { statusEl.textContent = 'No track selected.'; goBtn.disabled = false; return; }
    const trackIndex = Number(picked.index) || 0;
    const channelFilter = (picked.channel_filter == null) ? null : Number(picked.channel_filter);

    try {
        const url = _addKeysSourceFormat === 'midi'
            ? '/api/plugins/editor/import-keys-midi'
            : '/api/plugins/editor/import-keys';
        const audioOffset = _effectiveAudioOffset();
        const body = _addKeysSourceFormat === 'midi'
            ? { midi_path: _addKeysSourcePath, track_index: trackIndex, audio_offset: audioOffset,
                channel_filter: channelFilter }
            : { gp_path: _addKeysSourcePath, track_index: trackIndex, audio_offset: audioOffset };
        const resp = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.error) {
            statusEl.textContent = 'Error: ' + data.error;
            goBtn.disabled = false;
            return;
        }

        // Register the new arrangement with the server-side session (no-op for sloppak).
        const addResp = await fetch('/api/plugins/editor/add-arrangement', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: S.sessionId,
                arrangement: data.arrangement,
                xml_path: data.xml_path || '',
            }),
        });
        const addData = await addResp.json().catch(() => ({}));
        if (!addResp.ok || addData.error) {
            statusEl.textContent = 'Error registering arrangement: ' + (addData.error || addResp.status);
            goBtn.disabled = false;
            return;
        }

        // Append in-memory and switch to it
        S.arrangements.push(data.arrangement);
        S.currentArr = S.arrangements.length - 1;
        const sel = document.getElementById('editor-arrangement');
        sel.value = S.currentArr;

        flattenChords();
        if (typeof updatePianoRange === 'function') updatePianoRange();
        updateArrangementSelector();
        updateStatus();
        draw();

        editorHideAddKeysModal();
        setStatus('Added Keys arrangement (' + data.arrangement.notes.length + ' notes). Save to commit.');
    } catch (e) {
        statusEl.textContent = 'Failed: ' + e.message;
        goBtn.disabled = false;
    }
};

function _uniqueKeysName() {
    const taken = new Set(S.arrangements.map(a => (a.name || '').trim().toLowerCase()));
    if (!taken.has('keys')) return 'Keys';
    // The taken set has a finite number of entries, so a free slot is guaranteed
    // within taken.size + 1 iterations; the +2 ceiling is a safety margin.
    const limit = taken.size + 2;
    for (let i = 2; i <= limit; i++) if (!taken.has(`keys ${i}`)) return `Keys ${i}`;
    return `Keys ${Date.now()}`;
}

let _addingEmptyKeys = false;

window.editorAddEmptyKeys = async () => {
    if (S.format !== 'sloppak' || !S.sessionId) return;
    if (_addingEmptyKeys) return;
    _addingEmptyKeys = true;
    const statusEl = document.getElementById('editor-add-keys-status');
    const arrangement = {
        name: _uniqueKeysName(),
        tuning: [0, 0, 0, 0, 0, 0],
        capo: 0,
        notes: [],
        chords: [],
        chord_templates: [],
    };
    try {
        const resp = await fetch('/api/plugins/editor/add-arrangement', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: S.sessionId, arrangement }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.error) {
            statusEl.textContent = 'Error registering arrangement: ' + (data.error || resp.status);
            return;
        }

        S.arrangements.push(arrangement);
        S.currentArr = S.arrangements.length - 1;
        const sel = document.getElementById('editor-arrangement');
        if (sel) sel.value = S.currentArr;

        flattenChords();
        if (typeof updatePianoRange === 'function') updatePianoRange();
        updateArrangementSelector();
        updateStatus();
        draw();

        editorHideAddKeysModal();
        setStatus('Added empty Keys arrangement. Double-click the chart to add notes; save to commit.');
    } catch (e) {
        statusEl.textContent = 'Failed: ' + e.message;
    } finally {
        _addingEmptyKeys = false;
    }
};

// ════════════════════════════════════════════════════════════════════
// Record Keys arrangement live from a MIDI keyboard (Web MIDI API)
// ════════════════════════════════════════════════════════════════════

let _recMidiAccess = null;
let _recMidiInput = null;
let _recState = 'idle';                    // idle | recording | finalizing
let _recChannel = -1;                      // -1 = all, else 0..15
const _recHeld = new Map();                // pitch -> [{onTime, channel}, ...] FIFO
const _recPending = new Map();             // pitch -> [{onTime, channel}, ...] FIFO (pedal-deferred)
const _recSustainOn = new Set();           // channels with CC64 pedal currently held
let _recNotes = [];                        // finalized {time,string,fret,sustain,techniques}
let _recArrIdx = -1;                       // index of the in-progress Keys arrangement
let ghostNotes = null;                     // alias of _recNotes while recording (for drawGhostNotes)
let _recCountEl = null;                    // cached count DOM element (set at record-start)
let _recCountLastMs = 0;                   // last timestamp _recCount updated the DOM
const REC_COUNT_THROTTLE_MS = 80;          // max DOM update rate for the note counter

function chartTimeNow() {
    // editorStartRecordMidi guards against !S.audioCtx, so this only runs
    // during an active recording with a loaded audio context.
    return S.playStartTime + (S.audioCtx.currentTime - S.playStartWall);
}

async function _recMidiInit() {
    if (_recMidiAccess) return true;
    if (!navigator.requestMIDIAccess) return false;
    try {
        _recMidiAccess = await navigator.requestMIDIAccess({ sysex: false });
        _recMidiAccess.onstatechange = () => _recMidiUpdateDeviceList();
        return true;
    } catch (e) {
        console.warn('[Editor] MIDI access denied:', e);
        return false;
    }
}

function _recMidiUpdateDeviceList() {
    const sel = document.getElementById('editor-record-midi-device');
    const noDevice = document.getElementById('editor-record-midi-no-device');
    const startBtn = document.getElementById('editor-record-midi-start');
    if (!sel) return;
    const inputs = [];
    if (_recMidiAccess) _recMidiAccess.inputs.forEach(inp => inputs.push(inp));

    const saved = localStorage.getItem('editor.recordMidiDeviceId') || '';
    // Build options with createElement so device-supplied id/name strings
    // can't break out into HTML — Web MIDI metadata comes from the OS/USB
    // descriptor and isn't safe to interpolate via innerHTML.
    sel.replaceChildren();
    for (const inp of inputs) {
        const opt = document.createElement('option');
        opt.value = inp.id;
        const label = inp.name || inp.manufacturer || `MIDI Device (${inp.id})`;
        opt.textContent = label;
        if (inp.id === saved) opt.selected = true;
        sel.appendChild(opt);
    }

    const empty = !inputs.length;
    if (noDevice) noDevice.classList.toggle('hidden', !empty);
    if (startBtn) startBtn.disabled = empty;
}

function _recMidiConnect(id) {
    if (_recMidiInput) _recMidiInput.onmidimessage = null;
    _recMidiInput = null;
    if (!_recMidiAccess) return;
    _recMidiAccess.inputs.forEach(inp => {
        if (inp.id === id) {
            _recMidiInput = inp;
            _recMidiInput.onmidimessage = _recMidiOnMessage;
            localStorage.setItem('editor.recordMidiDeviceId', id);
        }
    });
}

function _recMidiOnMessage(e) {
    if (_recState !== 'recording') return;
    const [status, data1, velocity] = e.data;
    const ch = status & 0x0F;
    if (_recChannel >= 0 && ch !== _recChannel) return;
    const cmd = status & 0xF0;
    const note = data1;  // semantic alias: note number for on/off, cc number for B0 messages

    if (cmd === 0x90 && velocity > 0) {
        // Note on — push held entry (FIFO supports rapid retriggers).
        // Tag with `ch` so multi-channel layered/split keyboards in
        // "All channels" mode can pair note-offs with the correct take.
        let q = _recHeld.get(note);
        if (!q) { q = []; _recHeld.set(note, q); }
        q.push({ onTime: chartTimeNow(), channel: ch });
    } else if (cmd === 0x80 || (cmd === 0x90 && velocity === 0)) {
        // Note off — match the oldest held entry from the same channel.
        // Without the channel match, two layered channels playing the same
        // pitch would close each other's notes in arbitrary order.
        const q = _recHeld.get(note);
        if (!q || !q.length) return;
        const idx = q.findIndex(e => e.channel === ch);
        if (idx < 0) return;
        const [entry] = q.splice(idx, 1);
        if (!q.length) _recHeld.delete(note);
        if (_recSustainOn.has(ch)) {
            let p = _recPending.get(note);
            if (!p) { p = []; _recPending.set(note, p); }
            p.push(entry);
        } else {
            _recFinalizeNote(note, entry.onTime, chartTimeNow());
        }
    } else if (cmd === 0xB0 && data1 === 64) {
        // CC64 sustain pedal — per-channel state so layered/split keyboards
        // that emit CC64 on multiple channels don't cross-flush takes.
        if (velocity >= 64) {
            _recSustainOn.add(ch);
        } else {
            _recSustainOn.delete(ch);
            const off = chartTimeNow();
            for (const [pitch, queue] of _recPending) {
                const remaining = [];
                for (const entry of queue) {
                    if (entry.channel === ch) {
                        _recFinalizeNote(pitch, entry.onTime, off);
                    } else {
                        remaining.push(entry);
                    }
                }
                if (remaining.length) _recPending.set(pitch, remaining);
                else _recPending.delete(pitch);
            }
        }
    }
}

function _recFinalizeNote(pitch, onTime, offTime) {
    const sustain = Math.max(0, offTime - onTime);
    _recNotes.push({
        time: onTime,
        string: Math.floor(pitch / 24),
        fret: pitch % 24,
        sustain: sustain < 0.05 ? 0 : sustain,
        techniques: {},
    });
    _recCount();
}

function _recCount() {
    const now = performance.now();
    if (now - _recCountLastMs < REC_COUNT_THROTTLE_MS) return;   // throttle DOM writes
    _recCountLastMs = now;
    if (_recCountEl) _recCountEl.textContent = _recNotes.length + ' notes';
}

window.editorShowRecordMidiModal = async () => {
    if (!S.sessionId) return;
    const modal = document.getElementById('editor-record-midi-modal');
    const setup = document.getElementById('editor-record-midi-setup');
    const active = document.getElementById('editor-record-midi-active');
    const status = document.getElementById('editor-record-midi-status');
    const noWebMidi = document.getElementById('editor-record-midi-no-webmidi');
    const startBtn = document.getElementById('editor-record-midi-start');
    const chanSel = document.getElementById('editor-record-midi-channel');

    setup.classList.remove('hidden');
    active.classList.add('hidden');
    status.textContent = '';

    // Populate channel dropdown 1..16 once.
    if (chanSel.options.length === 1) {
        for (let i = 1; i <= 16; i++) {
            const opt = document.createElement('option');
            opt.value = String(i - 1);
            opt.textContent = String(i);
            chanSel.appendChild(opt);
        }
    }

    if (!navigator.requestMIDIAccess) {
        if (noWebMidi) noWebMidi.classList.remove('hidden');
        if (startBtn) startBtn.disabled = true;
    } else {
        if (noWebMidi) noWebMidi.classList.add('hidden');
        const granted = await _recMidiInit();
        if (!granted) {
            status.textContent = 'MIDI access denied — grant permission in browser settings and reload this page.';
            if (startBtn) startBtn.disabled = true;
        } else {
            status.textContent = '';
            _recMidiUpdateDeviceList();
        }
    }

    modal.classList.remove('hidden');
};

window.editorHideRecordMidiModal = () => {
    // Refuse to close while a take is active — explicit Stop is required.
    if (_recState !== 'idle') return;
    document.getElementById('editor-record-midi-modal').classList.add('hidden');
};

window.editorStartRecordMidi = () => {
    if (_recState !== 'idle') return;
    const sel = document.getElementById('editor-record-midi-device');
    const chanSel = document.getElementById('editor-record-midi-channel');
    const status = document.getElementById('editor-record-midi-status');
    const setup = document.getElementById('editor-record-midi-setup');
    const active = document.getElementById('editor-record-midi-active');
    if (S.format !== 'sloppak' || !S.sessionId) {
        status.textContent = 'Recording requires a sloppak editing session.';
        return;
    }
    if (!S.audioBuffer || !S.audioCtx) {
        status.textContent = 'Audio not loaded — cannot derive note timing.';
        return;
    }
    if (!sel || !sel.value) {
        status.textContent = 'Select a MIDI device first.';
        return;
    }
    _recMidiConnect(sel.value);
    if (!_recMidiInput) {
        status.textContent = 'Failed to connect to MIDI device.';
        return;
    }

    // Splice + start playback synchronously inside the click handler:
    //   (a) Chrome/Edge autoplay policy requires the AudioContext.resume()
    //       inside startPlayback() to fire during the user-gesture grace
    //       period — an awaited fetch would expire it and the transport
    //       would never advance, putting every captured note at t=0.
    //   (b) Punch-in (Record while already playing) must arm at the exact
    //       playhead the user clicked from, not wherever audio drifted to
    //       during a network round-trip.
    // The /add-arrangement POST is fired-and-forgotten — for sloppak it's
    // a no-op acknowledgement, and saving the session commits whatever
    // is in S.arrangements regardless.
    const arrangement = {
        name: _uniqueKeysName(),
        tuning: [0, 0, 0, 0, 0, 0],
        capo: 0,
        notes: [],
        chords: [],
        chord_templates: [],
    };

    S.arrangements.push(arrangement);
    S.currentArr = S.arrangements.length - 1;
    _recArrIdx = S.currentArr;
    const arrSel = document.getElementById('editor-arrangement');
    if (arrSel) arrSel.value = S.currentArr;
    flattenChords();
    if (typeof updatePianoRange === 'function') updatePianoRange();
    updateArrangementSelector();
    // Lock the selector for the duration of the take so a mid-recording
    // switch can't make Stop finalize into a stale arrangement index.
    if (arrSel) arrSel.disabled = true;

    _recHeld.clear();
    _recPending.clear();
    _recSustainOn.clear();
    _recNotes = [];
    _recCountEl = document.getElementById('editor-record-midi-count');
    _recCountLastMs = 0;  // reset throttle so the initial "0 notes" shows immediately
    _recCount();
    _recChannel = parseInt(chanSel.value);
    if (Number.isNaN(_recChannel)) _recChannel = -1;

    setup.classList.add('hidden');
    active.classList.remove('hidden');
    status.textContent = '';

    ghostNotes = _recNotes;
    _recState = 'recording';
    // Restart cleanly if a playback is already running — startPlayback()
    // allocates a fresh AudioBufferSourceNode and overwrites S.audioSource,
    // which would otherwise orphan the existing source and desync stop.
    // Refresh S.cursorTime from chartTimeNow() before the restart so
    // punch-in resumes from the actual audio position, not the last
    // playbackTick() snapshot (which can lag on throttled/slow frames).
    if (S.playing) {
        S.cursorTime = chartTimeNow();
        stopPlayback();
    }
    startPlayback();

    // Reliable end-of-song finalize: rAF (playbackTick) can be throttled
    // or paused in backgrounded tabs and miss the EOF clamp, leaving
    // _recState='recording' after audio actually ends. AudioBufferSourceNode's
    // onended fires regardless of tab visibility. The state guard inside
    // also makes this a no-op when stopPlayback() triggers onended via
    // explicit Stop / spacebar — those paths set _recState='finalizing'
    // before audioSource.stop() runs.
    if (S.audioSource) {
        S.audioSource.onended = () => {
            if (_recState === 'recording') editorStopRecordMidi();
        };
    }

    fetch('/api/plugins/editor/add-arrangement', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: S.sessionId, arrangement }),
    }).catch(e => console.warn('[Editor] add-arrangement registration failed:', e));
};

window.editorStopRecordMidi = () => {
    if (_recState !== 'recording') return;
    _recState = 'finalizing';

    // Capture stop-time before stopping audio so the chart-time formula
    // still reads the in-flight playhead. Clamp to S.duration: when this
    // path is reached via the EOF branch in playbackTick, chartTimeNow()
    // has already crossed the song boundary, and any held/pedal-deferred
    // notes would otherwise be finalized past the chart length.
    const stopTime = Math.min(chartTimeNow(), S.duration || Infinity);
    stopPlayback();

    // When the take finalized at EOF (e.g. via audioSource.onended in a
    // backgrounded tab where playbackTick was throttled), playbackTick's
    // cursor-reset branch never ran. Reset here so the next playback
    // starts from 0, not from a stale end-of-song position.
    if (S.duration && stopTime >= S.duration) {
        S.cursorTime = 0;
        updateTimeDisplay();
    }

    // Cap any still-held notes (key never released).
    for (const [pitch, queue] of _recHeld) {
        for (const { onTime } of queue) _recFinalizeNote(pitch, onTime, stopTime);
    }
    _recHeld.clear();
    // Cap any pedal-deferred notes (sustain still down at stop).
    for (const [pitch, queue] of _recPending) {
        for (const { onTime } of queue) _recFinalizeNote(pitch, onTime, stopTime);
    }
    _recPending.clear();
    _recSustainOn.clear();

    if (_recMidiInput) _recMidiInput.onmidimessage = null;

    // Populate the target arrangement registered at Start time. No second
    // POST: the arrangement was already registered with the backend, so
    // the splice is purely an in-memory note swap.
    _recNotes.sort((a, b) => a.time - b.time);
    const arr = S.arrangements[_recArrIdx];
    if (arr) arr.notes = _recNotes;

    // Flush the final note count to the modal before hiding it.
    _recCountLastMs = 0;
    _recCount();

    // Restore focus to the recorded arrangement (user may have switched the
    // selector via keyboard / OS events that bypass the disabled flag) and
    // unlock the selector now that the take is final.
    S.currentArr = _recArrIdx;
    const arrSel = document.getElementById('editor-arrangement');
    if (arrSel) {
        arrSel.disabled = false;
        arrSel.value = String(_recArrIdx);
    }

    // Clear the ghost overlay BEFORE the redraw so the new notes don't
    // render twice (once as real notes, once as translucent ghosts).
    ghostNotes = null;
    _recState = 'idle';

    flattenChords();
    if (typeof updatePianoRange === 'function') updatePianoRange();
    updateArrangementSelector();
    updateStatus();
    draw();

    document.getElementById('editor-record-midi-modal').classList.add('hidden');
    const n = arr ? arr.notes.length : 0;
    setStatus(n
        ? `Recorded Keys arrangement (${n} notes). Save to commit.`
        : 'Stopped — no notes captured. The empty Keys arrangement is in the switcher.');
};

function drawGhostNotes() {
    if (!ghostNotes || !ghostNotes.length || !isKeysMode()) return;
    const w = canvas.width / DPR;
    const st = S.scrollX - 2;
    const et = S.scrollX + (w - LABEL_W) / S.zoom + 2;
    ctx.save();
    ctx.globalAlpha = 0.45;
    ctx.fillStyle = '#f43f5e';   // rose-500 — echoes the Record button
    for (const n of ghostNotes) {
        if (n.time + (n.sustain || 0) < st || n.time > et) continue;
        const midi = noteToMidi(n.string, n.fret);
        const x = timeToX(n.time);
        const y = midiToY(midi);
        const nw = Math.max(2, (n.sustain || 0) * S.zoom);
        ctx.fillRect(x, y, nw + 2, Math.max(2, PIANO_LANE_H - 1));
    }
    ctx.restore();
}

// Run init after DOM is ready
if (document.getElementById('editor-canvas')) {
    init();
} else {
    // Wait for plugin screen to be injected
    const check = setInterval(() => {
        if (document.getElementById('editor-canvas')) {
            clearInterval(check);
            init();
        }
    }, 100);
}

})();
