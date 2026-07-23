// screen.js is one big IIFE with no module boundary (see CLAUDE.md), so a
// pure, self-contained function can't be `import`ed directly yet. As a
// stepping stone (same trick used elsewhere for routes.py — see
// test_difficulty_keys.py), extract the function's source by brace-matching
// and eval it in isolation. When screen.js is ever split into real modules,
// this file's extraction step goes away and the test keeps working unchanged.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const _here = path.dirname(fileURLToPath(import.meta.url));
const _screenSrc = readFileSync(path.join(_here, '..', 'screen.js'), 'utf8');

// Pull `function <name>(...) { ... }` out of screen.js by counting braces
// from the first `{` after the signature to its match — regex alone can't
// find the end of a multi-line function body reliably.
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

// _clampPopoverPos reads window.innerWidth/innerHeight — stub just that.
globalThis.window = { innerWidth: 800, innerHeight: 600 };
const _clampPopoverPos = new Function(`${extractFunction(_screenSrc, '_clampPopoverPos')}; return _clampPopoverPos;`)();

test('leaves an in-bounds position untouched', () => {
    const el = { offsetWidth: 100, offsetHeight: 50 };
    assert.deepEqual(_clampPopoverPos(el, 200, 200), { x: 200, y: 200 });
});

test('clamps a position that would overflow the right/bottom edge', () => {
    const el = { offsetWidth: 100, offsetHeight: 50 };
    // window is 800x600 (stubbed above); requesting (750, 580) would hang
    // 50px off the right and 30px off the bottom.
    assert.deepEqual(_clampPopoverPos(el, 750, 580), { x: 700, y: 550 });
});

test('never returns a negative coordinate even for a huge popover', () => {
    const el = { offsetWidth: 2000, offsetHeight: 2000 };
    assert.deepEqual(_clampPopoverPos(el, 10, 10), { x: 0, y: 0 });
});
