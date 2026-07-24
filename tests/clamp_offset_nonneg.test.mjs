// _clampOffsetNonNegative guards the Sync/Offset apply paths from a negative
// over-shift that would push note/beat/section times before 0:00. screen.js
// is one big IIFE (see CLAUDE.md), so — same trick as clamp_popover_pos.test.mjs
// / the routes.py AST tests — the pure function is extracted by brace-matching
// and eval'd in isolation rather than imported.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const _here = path.dirname(fileURLToPath(import.meta.url));
const _screenSrc = readFileSync(path.join(_here, '..', 'screen.js'), 'utf8');

function extractFunction(source, name) {
    const start = source.indexOf(`function ${name}(`);
    if (start < 0) throw new Error(`function ${name} not found in screen.js`);
    const bodyStart = source.indexOf('{', start);
    let depth = 0;
    for (let i = bodyStart; i < source.length; i++) {
        if (source[i] === '{') depth++;
        else if (source[i] === '}') {
            depth--;
            if (depth === 0) return source.slice(start, i + 1);
        }
    }
    throw new Error(`unbalanced braces extracting ${name}`);
}

const _clampOffsetNonNegative = new Function(
    `${extractFunction(_screenSrc, '_clampOffsetNonNegative')}; return _clampOffsetNonNegative;`
)();

const arr = (times) => ({ notes: times.map(t => ({ time: t, sustain: 0.3 })), chords: [] });

test('leaves a positive shift untouched', () => {
    const r = _clampOffsetNonNegative(1, +0.5, [arr([2.0, 4.0])], []);
    assert.equal(r.clamped, false);
    assert.equal(r.offset, 0.5);
});

test('leaves a safe negative shift untouched (earliest note stays >= 0)', () => {
    // earliest note at 2.0, offset -1.5 -> lands at 0.5, still >= 0.
    const r = _clampOffsetNonNegative(1, -1.5, [arr([2.0, 4.0])], []);
    assert.equal(r.clamped, false);
    assert.equal(r.offset, -1.5);
});

test('clamps a negative over-shift so the earliest note lands at exactly 0', () => {
    // earliest note at 2.0, requested offset -3 -> would be -1.0; clamp to -2.0.
    const r = _clampOffsetNonNegative(1, -3.0, [arr([2.0, 5.0])], []);
    assert.equal(r.clamped, true);
    assert.ok(Math.abs(r.offset - (-2.0)) < 1e-9, `expected -2.0, got ${r.offset}`);
    // Sanity: applying the clamped offset puts the earliest note at 0.
    assert.ok(Math.abs((2.0 / 1 + r.offset) - 0) < 1e-9);
});

test('accounts for the tempo factor (t/factor + offset)', () => {
    // note at 5.0, factor 1.25 -> corrected 4.0; offset -6 would be -2.0.
    // clamp so 4.0 + offset = 0 -> offset -4.0.
    const r = _clampOffsetNonNegative(1.25, -6.0, [arr([5.0])], []);
    assert.equal(r.clamped, true);
    assert.ok(Math.abs(r.offset - (-4.0)) < 1e-9, `expected -4.0, got ${r.offset}`);
    assert.ok(Math.abs((5.0 / 1.25 + r.offset) - 0) < 1e-9);
});

test('uses the earliest of notes and the shared grid times', () => {
    // notes start at 3.0 but a beat sits at 1.0 — the beat is the binding
    // constraint, so an offset of -2 (safe for the notes) must still clamp.
    const grid = [1.0, 2.5, 4.0];
    const r = _clampOffsetNonNegative(1, -2.0, [arr([3.0, 6.0])], grid);
    assert.equal(r.clamped, true);
    assert.ok(Math.abs(r.offset - (-1.0)) < 1e-9, `expected -1.0, got ${r.offset}`);
});

test('considers chord and chord-note times', () => {
    const arrangements = [{
        notes: [{ time: 5.0 }],
        chords: [{ time: 1.0, notes: [{ time: 0.5 }] }],
    }];
    // earliest is the chord note at 0.5; offset -1 would push it to -0.5.
    const r = _clampOffsetNonNegative(1, -1.0, arrangements, []);
    assert.equal(r.clamped, true);
    assert.ok(Math.abs(r.offset - (-0.5)) < 1e-9, `expected -0.5, got ${r.offset}`);
});

test('empty arrangements and no grid: nothing to clamp against', () => {
    const r = _clampOffsetNonNegative(1, -100, [{ notes: [], chords: [] }], []);
    assert.equal(r.clamped, false);
    assert.equal(r.offset, -100);
});

test('a shift landing exactly on 0 is not flagged as clamped', () => {
    const r = _clampOffsetNonNegative(1, -2.0, [arr([2.0, 5.0])], []);
    assert.equal(r.clamped, false);
    assert.equal(r.offset, -2.0);
});
