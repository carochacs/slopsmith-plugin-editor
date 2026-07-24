"""Tests for the chroma/DTW sync-offset redesign specified in GitHub issue #16
(Maestro-Ltd/slopsmith-plugin-editor) — "Editor improvements inspired by
TabGrabber — replan after revert".

**Status: written ahead of implementation.** None of the functions below
exist in routes.py yet — this file is the executable specification the
future `/api/plugins/editor/detect-sync-offset` route (name TBD) should be
implemented against. Until then, every test in this file fails at
collection with a clear "helpers not found in routes.py: {...}" message
(see `_load_helpers()`), which is the expected state — not a bug in this
test file.

Same AST-extraction approach as test_analyze_chords.py / test_difficulty_keys.py
— the plugin has no importable pipeline module (it depends on `lib.*`
supplied by Slopsmith core at runtime, not present in this repo — see
CLAUDE.md), so nested helpers inside setup(app, context) are pulled from
the routes.py source via AST and exec'd standalone rather than standing up
the full FastAPI + slopsmith lib.* stack.

## Function contracts this file expects `routes.py:setup()` to define

- `_select_stem_for_arrangement(stems, arrangement_name)` — pure, no audio.
  `stems` is the manifest's `stems` list shape (`[{"id": ..., "file": ...}, ...]`,
  confirmed in routes.py's existing sloppak-load path). Returns the `file`
  string of the best-matching stem for `arrangement_name`, or None if
  `stems` is empty. Matching rule (a default per issue #16 — final
  semantics still open, adjust these tests alongside the real
  implementation if the maintainer picks a different rule):
    - arrangement name containing "bass" (case-insensitive) -> stem id "bass"
    - arrangement name matching the keys pattern (keys/piano/keyboard/synth,
      same regex as screen.js's KEYS_PATTERN) -> stem id "piano"
    - otherwise (guitar/lead/rhythm) -> stem id "guitar"
    - if no stem with the matched id exists: fall back to id "full", then
      the first stem in the list, in that order.
  Never triggers live stem separation (Demucs) — reads only stems the
  manifest already lists.

- `_note_onset_pitch_classes(notes, tuning, is_keys, chord_analysis)` — pure,
  no audio. Returns a list of `(time, pitch_class, weight)` tuples, reusing
  `chord_analysis.fret_to_midi` for guitar/bass (mirrors the analyze-chords
  route's existing pattern) or the `string*24+fret` keys encoding.
  `weight` defaults to the note's sustain (or 1.0 when absent/zero).

- `_chroma_frames_from_pitch_events(events, hop_seconds, n_frames)` — pure,
  no audio, no chord_analysis dependency. Bins `(time, pitch_class, weight)`
  events (as returned by `_note_onset_pitch_classes`) into a `(12, n_frames)`
  array — same orientation as `librosa.feature.chroma_cqt`'s output, for
  direct comparison via `librosa.sequence.dtw`.

- `_detect_bpm_and_offset(y, sr, arr_notes, tuning, is_keys, manual_bpm=None)`
  — the real librosa integration: BPM via `librosa.beat.beat_track` (shared
  spectral-feature compute with the alignment pass per issue #16's
  resolved "move BPM detection server-side" question), then chroma-based
  DTW alignment of the arrangement's opening notes (the same "first
  several notes within ~2s" pattern-window concept the prior client-side
  attempt used) against `librosa.feature.chroma_cqt` computed on `y`/`sr`.
  Returns `{"audio_bpm": float, "offset_seconds": float, "confidence": float}`
  where `confidence` is continuous in [0.0, 1.0] (not categorical — this
  was corrected from an earlier categorical proposal per review feedback
  on the issue), derived from the DTW alignment quality. `offset_seconds`
  must be measured against the TEMPO-CORRECTED note times (the bug in the
  reverted attempt was measuring against raw, unscaled note times) — see
  `test_detect_bpm_and_offset_accounts_for_tempo_factor` below, which
  regression-tests exactly that mistake.
"""

import ast
import json
import textwrap
from pathlib import Path

import numpy as np
import pytest

_ROUTES = Path(__file__).parent.parent / "routes.py"

_here = Path(__file__).parent
import sys
sys.path.insert(0, str(_here.parent))
import chord_analysis as ca

SR = 22050  # librosa's default; keeps synthetic-fixture generation fast

_WANTED = {
    "_select_stem_for_arrangement",
    "_select_bpm_stem",
    "_note_onset_pitch_classes",
    "_chroma_frames_from_pitch_events",
    "_detect_bpm_and_offset",
    "_load_disk_arrangement_notes",
}


def _load_helpers():
    """Exec the named nested functions from routes.py:setup into a namespace.

    Fails loudly (not skips) when a helper is missing — that's the correct
    signal right now, since none of these exist yet. Once the route is
    implemented, this same assertion becomes a real regression check that
    the expected function names weren't renamed/removed later.
    """
    src = _ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(src)
    setup_fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "setup"
    )
    ns = {"np": np, "chord_analysis": ca, "json": json, "Path": Path}
    found = set()
    for node in setup_fn.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED:
            code = textwrap.dedent(ast.get_source_segment(src, node))
            exec(compile(code, str(_ROUTES), "exec"), ns)
            found.add(node.name)
    missing = _WANTED - found
    assert not missing, (
        f"helpers not found in routes.py: {sorted(missing)} — this test file was "
        f"written against the design in GitHub issue #16 ahead of implementation; "
        f"implement these in routes.py:setup() (or update this file if the agreed "
        f"contract changed) before this suite can run for real."
    )
    return ns


@pytest.fixture(scope="module")
def H():
    return _load_helpers()


STD_GUITAR_TUNING = [0, 0, 0, 0, 0, 0]


def _note(string, fret, time=0.0, sustain=0.3):
    return {"string": string, "fret": fret, "time": time, "sustain": sustain}


# ── Synthetic audio fixtures ────────────────────────────────────────────────

def _silence(duration_sec, sr=SR):
    # A fixed local seed, not the global RNG — "silence" is a background
    # noise floor under a deterministic tone, and several tests assert tight
    # offset/confidence tolerances against it. Unseeded noise occasionally
    # correlates enough by chance to flake a real assertion (observed: both
    # this and a pass-the-threshold-by-luck failure in a verify_windows
    # test); seeding makes every synthetic fixture reproducible without
    # touching numpy's global random state (which other tests don't rely on
    # being unseeded either).
    rng = np.random.default_rng(20260724)
    return rng.uniform(-0.002, 0.002, int(duration_sec * sr)).astype(np.float32)


def midi_to_freq(midi):
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


def _tone_at(y, start_sec, freq_hz, dur_sec=0.3, sr=SR, amp=0.6):
    """Writes a sine tone into `y` in place, starting at start_sec."""
    start = int(start_sec * sr)
    n = int(dur_sec * sr)
    t = np.arange(n) / sr
    tone = (amp * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)
    end = min(len(y), start + n)
    y[start:end] += tone[: end - start]
    return y


def _click_track(bpm, duration_sec, sr=SR):
    """A metronome-like periodic click train at `bpm` — for BPM-detection
    fixtures. Not musically meaningful, just a clean, known-tempo pulse."""
    y = _silence(duration_sec, sr)
    interval = 60.0 / bpm
    t = 0.0
    click_len = int(0.01 * sr)
    while t < duration_sec:
        start = int(t * sr)
        end = min(len(y), start + click_len)
        y[start:end] += np.random.uniform(-1, 1, end - start).astype(np.float32)
        t += interval
    return y


# ── _select_stem_for_arrangement ────────────────────────────────────────────

def test_select_stem_matches_bass_arrangement(H):
    stems = [
        {"id": "vocals", "file": "stems/vocals.ogg"},
        {"id": "bass", "file": "stems/bass.ogg"},
        {"id": "guitar", "file": "stems/guitar.ogg"},
    ]
    assert H["_select_stem_for_arrangement"](stems, "Bass") == "stems/bass.ogg"


def test_select_stem_matches_guitar_for_lead_and_rhythm(H):
    stems = [{"id": "guitar", "file": "stems/guitar.ogg"}, {"id": "bass", "file": "stems/bass.ogg"}]
    assert H["_select_stem_for_arrangement"](stems, "Lead") == "stems/guitar.ogg"
    assert H["_select_stem_for_arrangement"](stems, "Rhythm") == "stems/guitar.ogg"


def test_select_stem_matches_keys_to_piano_stem(H):
    stems = [{"id": "piano", "file": "stems/piano.ogg"}, {"id": "guitar", "file": "stems/guitar.ogg"}]
    assert H["_select_stem_for_arrangement"](stems, "Keys") == "stems/piano.ogg"


def test_select_stem_falls_back_to_full_when_instrument_stem_missing(H):
    stems = [{"id": "full", "file": "stems/full.ogg"}, {"id": "vocals", "file": "stems/vocals.ogg"}]
    assert H["_select_stem_for_arrangement"](stems, "Bass") == "stems/full.ogg"


def test_select_stem_falls_back_to_first_stem_when_no_full_or_match(H):
    stems = [{"id": "vocals", "file": "stems/vocals.ogg"}, {"id": "drums", "file": "stems/drums.ogg"}]
    assert H["_select_stem_for_arrangement"](stems, "Bass") == "stems/vocals.ogg"


def test_select_stem_returns_none_for_empty_stem_list(H):
    assert H["_select_stem_for_arrangement"]([], "Bass") is None


# ── _note_onset_pitch_classes ───────────────────────────────────────────────

def test_note_onset_pitch_classes_guitar_matches_chord_analysis_fret_to_midi(H):
    notes = [_note(0, 0, time=1.0, sustain=0.5)]  # high e open = MIDI 64, pc 4
    events = H["_note_onset_pitch_classes"](notes, STD_GUITAR_TUNING, False, ca)
    assert len(events) == 1
    t, pc, weight = events[0]
    assert t == 1.0
    assert pc == ca.fret_to_midi(0, 0, STD_GUITAR_TUNING) % 12 == 4
    assert weight == pytest.approx(0.5)


def test_note_onset_pitch_classes_keys_uses_string24_plus_fret_encoding(H):
    notes = [_note(2, 12, time=0.5, sustain=0.2)]  # 2*24+12 = 60 (C4), pc 0
    events = H["_note_onset_pitch_classes"](notes, [], True, ca)
    assert len(events) == 1
    t, pc, weight = events[0]
    assert pc == 0


def test_note_onset_pitch_classes_defaults_weight_when_sustain_missing(H):
    notes = [{"string": 0, "fret": 0, "time": 0.0}]
    events = H["_note_onset_pitch_classes"](notes, STD_GUITAR_TUNING, False, ca)
    assert events[0][2] > 0  # some positive default weight, not 0


def test_note_onset_pitch_classes_empty_notes_returns_empty(H):
    assert H["_note_onset_pitch_classes"]([], STD_GUITAR_TUNING, False, ca) == []


# ── _chroma_frames_from_pitch_events ────────────────────────────────────────

def test_chroma_frames_shape_matches_hop_and_duration(H):
    events = [(0.0, 0, 1.0)]
    frames = H["_chroma_frames_from_pitch_events"](events, hop_seconds=0.1, n_frames=20)
    assert frames.shape == (12, 20)


def test_chroma_frames_places_energy_at_correct_pitch_class_and_time(H):
    # A pc=7 (G) event at t=0.5s, hop=0.1s -> frame index 5.
    events = [(0.5, 7, 1.0)]
    frames = H["_chroma_frames_from_pitch_events"](events, hop_seconds=0.1, n_frames=10)
    assert frames[7, 5] > 0
    # Every other pitch class at that frame should be ~0.
    assert np.sum(frames[:, 5]) == pytest.approx(frames[7, 5])


def test_chroma_frames_c_major_triad_has_energy_at_c_e_g(H):
    # C=0, E=4, G=7, all at the same instant.
    events = [(0.2, 0, 1.0), (0.2, 4, 1.0), (0.2, 7, 1.0)]
    frames = H["_chroma_frames_from_pitch_events"](events, hop_seconds=0.1, n_frames=10)
    col = frames[:, 2]
    for pc in (0, 4, 7):
        assert col[pc] > 0
    # Everything else in that column should be zero (no bleed to unrelated pcs).
    for pc in range(12):
        if pc not in (0, 4, 7):
            assert col[pc] == 0


def test_chroma_frames_no_events_is_all_zero(H):
    frames = H["_chroma_frames_from_pitch_events"]([], hop_seconds=0.1, n_frames=5)
    assert frames.shape == (12, 5)
    assert np.all(frames == 0)


# ── _detect_bpm_and_offset — behavioral tests against real librosa ─────────
#
# The exact chroma/DTW parameters (hop length, n_fft, chroma_cqt vs
# chroma_cens, DTW step pattern) are explicitly "TBD empirically" per issue
# #16, so these tests assert PROPERTIES the algorithm must satisfy rather
# than pinning specific internal numbers — they should keep passing across
# reasonable tuning changes to the real implementation.

def test_detect_bpm_and_offset_near_zero_when_already_aligned(H):
    duration = 6.0
    click_time = 2.0
    expected_midi = 64  # high e open
    # A short, onset-like burst (not a long sustain) — chroma matching
    # finds the right REGION but isn't onset-precise across a whole
    # sustain: two vetted reference implementations both showed the error
    # scale roughly with tone duration (a 0.4s tone produced ~0.2s of
    # error; 0.1-0.15s tones stayed under ~0.07s). If the real
    # implementation needs frame-tight precision on long sustained notes
    # too, it likely needs a secondary onset-refinement pass within the
    # winning chroma-matched region — flagged as a design note in issue
    # #16, not assumed away here.
    tone_dur = 0.15
    y = _silence(duration)
    _tone_at(y, click_time, midi_to_freq(expected_midi), dur_sec=tone_dur)
    notes = [_note(0, 0, time=click_time, sustain=tone_dur)]

    result = H["_detect_bpm_and_offset"](y, SR, notes, STD_GUITAR_TUNING, False)
    assert abs(result["offset_seconds"]) < 0.1, (
        f"expected ~0 offset when audio and tab note already coincide, got {result['offset_seconds']}"
    )


def test_detect_bpm_and_offset_accounts_for_tempo_factor(H):
    # Regression test for the exact bug found in the reverted client-side
    # attempt: the offset must be measured against the note time AFTER
    # tempo correction, not the raw tab time. Audio's real onset is at
    # 3.0s; if the detected/tab BPM ratio (factor) is 1.25, the tab's own
    # (unscaled) note time must be 3.75s for these to coincide once
    # factor is applied (n_new = n_old/factor + offset).
    #
    # This test drives the tempo factor via a manual_bpm override so the
    # ratio is deterministic regardless of the real beat-tracking result:
    # manual_bpm / tab_bpm = factor. We don't control tab_bpm directly
    # here (it's derived from the session's beat grid elsewhere, outside
    # this function's contract) — so this test instead pins the contract
    # via manual_bpm alongside a tab_bpm the function is told about
    # explicitly, whichever parameter name the implementation exposes for
    # it (see NOTE below).
    #
    # NOTE: _detect_bpm_and_offset's signature above only lists
    # `manual_bpm`, not an explicit `tab_bpm` parameter — this test
    # currently assumes the function also accepts `tab_bpm` as a keyword
    # so the factor is fully controllable in a unit test without a real
    # beat-tracked audio fixture. If the real implementation derives
    # tab_bpm differently, adjust this test's call signature to match
    # rather than the underlying assertion (offset must respect the
    # tempo-corrected timeline), which should not change.
    audio_click_time = 3.0
    factor = 1.25
    tab_note_time = audio_click_time * factor  # 3.75
    expected_midi = 64
    tone_dur = 0.15  # short/onset-like — see note in the test above

    y = _silence(6.0)
    _tone_at(y, audio_click_time, midi_to_freq(expected_midi), dur_sec=tone_dur)
    notes = [_note(0, 0, time=tab_note_time, sustain=tone_dur)]

    result = H["_detect_bpm_and_offset"](
        y, SR, notes, STD_GUITAR_TUNING, False,
        manual_bpm=100.0, tab_bpm=100.0 / factor,
    )
    # What matters here is that the error is nowhere near the ~0.75s a
    # factor-naive implementation would produce (audio_click_time -
    # tab_note_time = 3.0 - 3.75), not sub-frame precision.
    assert abs(result["offset_seconds"]) < 0.1, (
        "offset must be measured against the tempo-corrected note time "
        f"(note.time / factor), not the raw tab time — got {result['offset_seconds']}"
    )


def test_detect_bpm_and_offset_confidence_higher_for_matching_pitch(H):
    # Relative comparison, not a hard threshold — the exact confidence
    # formula is TBD, but a genuine pitch+rhythm match must score higher
    # than an audio pitch that clearly doesn't match the expected note.
    click_time = 2.0
    expected_midi = 64  # E4

    y_match = _silence(6.0)
    _tone_at(y_match, click_time, midi_to_freq(expected_midi), dur_sec=0.4)
    notes = [_note(0, 0, time=click_time, sustain=0.4)]
    result_match = H["_detect_bpm_and_offset"](y_match, SR, notes, STD_GUITAR_TUNING, False)

    y_mismatch = _silence(6.0)
    _tone_at(y_mismatch, click_time, midi_to_freq(57), dur_sec=0.4)  # A3, unrelated pitch class
    result_mismatch = H["_detect_bpm_and_offset"](y_mismatch, SR, notes, STD_GUITAR_TUNING, False)

    assert 0.0 <= result_match["confidence"] <= 1.0
    assert 0.0 <= result_mismatch["confidence"] <= 1.0
    assert result_match["confidence"] > result_mismatch["confidence"], (
        f"matching-pitch confidence ({result_match['confidence']}) should exceed "
        f"mismatched-pitch confidence ({result_mismatch['confidence']})"
    )


def test_detect_bpm_and_offset_confidence_is_low_for_silence(H):
    y = _silence(6.0)  # no tone anywhere — nothing to align to
    notes = [_note(0, 0, time=2.0, sustain=0.4)]
    result = H["_detect_bpm_and_offset"](y, SR, notes, STD_GUITAR_TUNING, False)
    assert result["confidence"] < 0.5


def test_detect_bpm_and_offset_returns_bpm_close_to_click_track_tempo(H):
    true_bpm = 120.0
    y = _click_track(true_bpm, duration_sec=8.0)
    notes = [_note(0, 0, time=0.5, sustain=0.2)]
    result = H["_detect_bpm_and_offset"](y, SR, notes, STD_GUITAR_TUNING, False)
    # Beat trackers commonly report a tempo octave off (half/double);
    # accept either the true tempo or a clean harmonic of it.
    ratio = result["audio_bpm"] / true_bpm
    nearest_harmonic = min((0.5, 1.0, 2.0), key=lambda h: abs(ratio - h))
    assert abs(ratio - nearest_harmonic) < 0.05, (
        f"expected audio_bpm near a harmonic of {true_bpm}, got {result['audio_bpm']}"
    )


def test_detect_bpm_and_offset_no_notes_returns_zero_offset_low_confidence(H):
    y = _silence(3.0)
    result = H["_detect_bpm_and_offset"](y, SR, [], STD_GUITAR_TUNING, False)
    assert result["offset_seconds"] == 0
    assert result["confidence"] < 0.5


def test_detect_bpm_and_offset_response_shape(H):
    y = _silence(3.0)
    notes = [_note(0, 0, time=1.0, sustain=0.3)]
    result = H["_detect_bpm_and_offset"](y, SR, notes, STD_GUITAR_TUNING, False)
    assert set(result.keys()) >= {"audio_bpm", "offset_seconds", "confidence"}
    assert isinstance(result["confidence"], float)
    assert isinstance(result["offset_seconds"], float)


# ── Regression: real-file bugs found on a TabGrabber sloppak (Black — Pearl
# Jam). Beat tracking octave-errored on the isolated guitar stem (read 152
# for a 77 BPM song), and the offset search over the whole decoded region
# locked onto a far-away pitch-class recurrence instead of the real onset.

def test_detect_bpm_and_offset_folds_octave_error_toward_tab_bpm(H):
    # A 152 BPM click is a clean double of a 76 BPM tab. With the tab tempo
    # supplied, the reported BPM must land near the tab's octave (≈76), not
    # the doubled 152 — otherwise the client derives a ~2x factor and halves
    # the whole chart.
    y = _click_track(152.0, duration_sec=8.0)
    notes = [_note(0, 0, time=0.5, sustain=0.2)]
    result = H["_detect_bpm_and_offset"](
        y, SR, notes, STD_GUITAR_TUNING, False, tab_bpm=76.0)
    ratio = result["audio_bpm"] / 76.0
    assert abs(ratio - 1.0) < 0.1, (
        f"detected BPM should fold to the tab's octave (~76), got {result['audio_bpm']}"
    )


def test_detect_bpm_and_offset_near_unity_factor_snaps_to_one(H):
    # Audio and tab at essentially the same tempo: the reported BPM must come
    # back consistent with a factor of exactly 1 (audio_bpm ≈ tab_bpm), so a
    # small beat-tracking wobble can't rescale an already-aligned tab.
    y = _click_track(77.0, duration_sec=8.0)
    notes = [_note(0, 0, time=0.5, sustain=0.2)]
    result = H["_detect_bpm_and_offset"](
        y, SR, notes, STD_GUITAR_TUNING, False, tab_bpm=77.0)
    assert abs(result["audio_bpm"] / 77.0 - 1.0) < 0.02


def test_detect_bpm_and_offset_load_offset_keeps_absolute_timeline(H):
    # When the route decodes only a local window that begins at `load_offset`
    # seconds, the returned offset must still be in the tab's absolute
    # timeline. Here the note sits at absolute 12.0s and the audio window
    # begins at 10.0s with the matching tone 2.0s into it (= absolute 12.0s),
    # so the two already coincide and the offset must be ~0 (not the ~-10s a
    # load-offset-naive implementation would report).
    load_offset = 10.0
    tone_in_window = 2.0          # 2s into the decoded window
    note_abs = load_offset + tone_in_window  # 12.0s absolute
    y = _silence(6.0)             # represents audio[10.0 .. 16.0]
    _tone_at(y, tone_in_window, midi_to_freq(64), dur_sec=0.15)
    notes = [_note(0, 0, time=note_abs, sustain=0.15)]
    result = H["_detect_bpm_and_offset"](
        y, SR, notes, STD_GUITAR_TUNING, False, load_offset=load_offset)
    assert abs(result["offset_seconds"]) < 0.15, (
        f"offset must be in the absolute timeline via load_offset, got {result['offset_seconds']}"
    )


# ── verify_windows — independent-evidence multi-window verification ────────
#
# Regression tests for a real bug found validating against a synced/desynced
# pair of real files: the opening-window match alone can be confidently
# WRONG (a correctly-synced guitar stem's opening notes coincidentally
# matched a spot elsewhere, reporting a spurious offset at confidence 0.89).
# verify_windows lets the caller supply independent evidence (a later
# section of the same arrangement, or another arrangement's own notes) that
# must corroborate the primary window before it's trusted at high
# confidence. A second real-file regression (a verify window with only
# confidence 0.21 in ITS OWN alignment was still allowed to veto a correct,
# confidence-1.00 primary match) is why an unreliable verify window must be
# excluded from voting entirely, not just weighted down.

def _clean_window(note_time, tone_time, dur=6.0, midi=64, tone_dur=0.15):
    """A (notes, y) pair where a note at `note_time` has a clean matching
    tone at `tone_time` in the synthetic audio — a reliable window."""
    y = _silence(dur)
    _tone_at(y, tone_time, midi_to_freq(midi), dur_sec=tone_dur)
    notes = [_note(0, 0, time=note_time, sustain=tone_dur)]
    return notes, y


def test_verify_window_agreement_boosts_confidence(H):
    # Primary and verify windows both cleanly match at essentially the same
    # offset (~0) — independent agreement should not lower confidence below
    # what the primary alone already achieved, and may raise it.
    p_notes, p_y = _clean_window(note_time=2.0, tone_time=2.0)
    v_notes, v_y = _clean_window(note_time=3.0, tone_time=3.0)
    primary_alone = H["_detect_bpm_and_offset"](p_y, SR, p_notes, STD_GUITAR_TUNING, False)
    result = H["_detect_bpm_and_offset"](
        p_y, SR, p_notes, STD_GUITAR_TUNING, False,
        verify_windows=[{"notes": v_notes, "y": v_y, "sr": SR, "chroma": None, "load_offset": 0.0}])
    assert result["confidence"] >= primary_alone["confidence"] - 1e-6
    assert abs(result["offset_seconds"]) < 0.15


def test_verify_window_own_low_confidence_does_not_veto_strong_primary(H):
    # The primary window is a clean, confident match. The verify window is
    # pure silence — it can't confidently determine its own offset, so it
    # must NOT be allowed to drag the primary's confidence down: a verify
    # window that doesn't trust its own alignment gets no vote, for or
    # against. (Regression: this exact case previously dragged a real
    # confidence-1.00 primary match down to 0.30 on a correctly-detected
    # desync, because the old logic counted ANY successfully-aligned verify
    # window toward the "nothing agreed" cap, regardless of its own
    # confidence.)
    p_notes, p_y = _clean_window(note_time=2.0, tone_time=2.0)
    v_notes = [_note(0, 0, time=3.0, sustain=0.2)]
    # Exact digital silence (not _silence()'s tiny random noise floor) — a
    # deterministic "no signal at all" fixture, so this test can't flake on
    # noise that occasionally correlates enough by chance to clear the
    # reliability threshold.
    v_y = np.zeros(int(6.0 * SR), dtype=np.float32)
    primary_alone = H["_detect_bpm_and_offset"](p_y, SR, p_notes, STD_GUITAR_TUNING, False)
    result = H["_detect_bpm_and_offset"](
        p_y, SR, p_notes, STD_GUITAR_TUNING, False,
        verify_windows=[{"notes": v_notes, "y": v_y, "sr": SR, "chroma": None, "load_offset": 0.0}])
    assert result["confidence"] >= primary_alone["confidence"] - 1e-6, (
        "an unreliable verify window must not lower a strong primary's confidence"
    )


def test_verify_window_reliable_disagreement_caps_confidence(H):
    # Both windows cleanly match (each is independently confident), but at
    # offsets far apart from each other. A real song-wide desync holds
    # everywhere in the song; two confident, independent windows finding
    # different offsets means the primary's match was likely coincidental,
    # not a real alignment — confidence must be capped low regardless of how
    # clean the primary window's own match looked in isolation.
    p_notes, p_y = _clean_window(note_time=2.0, tone_time=2.0)          # offset ~0
    v_notes, v_y = _clean_window(note_time=3.0, tone_time=3.0 + 2.5)    # offset ~+2.5, far from 0
    result = H["_detect_bpm_and_offset"](
        p_y, SR, p_notes, STD_GUITAR_TUNING, False,
        verify_windows=[{"notes": v_notes, "y": v_y, "sr": SR, "chroma": None, "load_offset": 0.0}])
    assert result["confidence"] <= 0.3 + 1e-6


def test_verify_windows_empty_list_behaves_like_no_verification(H):
    # An empty verify_windows list (e.g. the route couldn't build any) must
    # be a no-op — same result as not passing verify_windows at all.
    notes, y = _clean_window(note_time=2.0, tone_time=2.0)
    r_none = H["_detect_bpm_and_offset"](y, SR, notes, STD_GUITAR_TUNING, False)
    r_empty = H["_detect_bpm_and_offset"](y, SR, notes, STD_GUITAR_TUNING, False, verify_windows=[])
    assert r_none["offset_seconds"] == r_empty["offset_seconds"]
    assert r_none["confidence"] == r_empty["confidence"]


# ── _select_bpm_stem ────────────────────────────────────────────────────────
# Pure, no audio — the stem chosen for BEAT TRACKING (as opposed to
# _select_stem_for_arrangement, which picks the chroma/offset source).

def test_select_bpm_stem_prefers_drums(H):
    stems = [
        {"id": "guitar", "file": "stems/guitar.ogg"},
        {"id": "drums", "file": "stems/drums.ogg"},
        {"id": "full", "file": "stems/full.ogg"},
    ]
    assert H["_select_bpm_stem"](stems) == "stems/drums.ogg"


def test_select_bpm_stem_falls_back_to_full_without_drums(H):
    stems = [{"id": "guitar", "file": "stems/guitar.ogg"}, {"id": "full", "file": "stems/full.ogg"}]
    assert H["_select_bpm_stem"](stems) == "stems/full.ogg"


def test_select_bpm_stem_falls_back_to_first_stem_when_no_drums_or_full(H):
    stems = [{"id": "vocals", "file": "stems/vocals.ogg"}, {"id": "guitar", "file": "stems/guitar.ogg"}]
    assert H["_select_bpm_stem"](stems) == "stems/vocals.ogg"


def test_select_bpm_stem_returns_none_for_empty_stem_list(H):
    assert H["_select_bpm_stem"]([]) is None


def test_select_bpm_stem_id_matching_is_case_insensitive(H):
    stems = [{"id": "Drums", "file": "stems/drums.ogg"}]
    assert H["_select_bpm_stem"](stems) == "stems/drums.ogg"


# ── _load_disk_arrangement_notes ────────────────────────────────────────────
# Reads an on-disk arrangement JSON (compact t/s/f/sus keys) and normalizes
# to the wire format. Real file I/O, so these use tmp_path.

def test_load_disk_arrangement_notes_normalizes_compact_keys(H, tmp_path):
    p = tmp_path / "bass.json"
    p.write_text(json.dumps({"notes": [{"t": 1.5, "s": 2, "f": 3, "sus": 0.4}]}))
    notes = H["_load_disk_arrangement_notes"](str(p))
    assert notes == [{"string": 2, "fret": 3, "time": 1.5, "sustain": 0.4}]


def test_load_disk_arrangement_notes_chord_members_inherit_chord_time(H, tmp_path):
    p = tmp_path / "arr.json"
    p.write_text(json.dumps({
        "notes": [],
        "chords": [{
            "t": 2.0,
            "notes": [
                {"s": 0, "f": 1, "sus": 0.3},           # no own time -> inherits chord's 2.0
                {"s": 1, "f": 2, "t": 2.5, "sus": 0.2},  # own time -> 2.5
            ],
        }],
    }))
    times = sorted(n["time"] for n in H["_load_disk_arrangement_notes"](str(p)))
    assert times == [2.0, 2.5]


def test_load_disk_arrangement_notes_output_is_time_sorted(H, tmp_path):
    p = tmp_path / "arr.json"
    p.write_text(json.dumps({"notes": [{"t": 5.0, "s": 0, "f": 0}, {"t": 1.0, "s": 0, "f": 0}]}))
    notes = H["_load_disk_arrangement_notes"](str(p))
    assert [n["time"] for n in notes] == [1.0, 5.0]


def test_load_disk_arrangement_notes_missing_file_returns_empty(H, tmp_path):
    assert H["_load_disk_arrangement_notes"](str(tmp_path / "nope.json")) == []


def test_load_disk_arrangement_notes_malformed_json_returns_empty(H, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    assert H["_load_disk_arrangement_notes"](str(p)) == []
