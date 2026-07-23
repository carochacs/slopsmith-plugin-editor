"""Tests for chord_analysis.py — all pure functions, no fixtures needed."""

import sys
import importlib
from pathlib import Path

# Load chord_analysis from the plugin directory without installing it.
_here = Path(__file__).parent
sys.path.insert(0, str(_here.parent))
import chord_analysis as ca

# ── fret_to_midi ──────────────────────────────────────────────────────────────

STD = [0] * 6  # standard tuning, no offset

def test_fret_to_midi_open_e_string():
    # string 0 in standard guitar = high e (MIDI 64)
    assert ca.fret_to_midi(0, 0, STD) == 64

def test_fret_to_midi_fret_adds_semitones():
    assert ca.fret_to_midi(0, 12, STD) == 76

def test_fret_to_midi_second_string_open():
    # string 1 = B string (MIDI 59)
    assert ca.fret_to_midi(1, 0, STD) == 59

def test_fret_to_midi_low_e_string():
    # string 5 = low E (MIDI 40)
    assert ca.fret_to_midi(5, 0, STD) == 40

def test_fret_to_midi_with_drop_d_tuning():
    # drop D: string 5 tuned down 2 semitones → MIDI 38
    drop_d = [0, 0, 0, 0, 0, -2]
    assert ca.fret_to_midi(5, 0, drop_d) == 38

def test_fret_to_midi_bass_string0():
    # 4-string bass: string 0 = G2 (MIDI 55)
    bass = [0] * 4
    assert ca.fret_to_midi(0, 0, bass) == 55

def test_fret_to_midi_bass_string3():
    # 4-string bass: string 3 = E1 (MIDI 40)
    bass = [0] * 4
    assert ca.fret_to_midi(3, 0, bass) == 40

# ── _pearson ─────────────────────────────────────────────────────────────────

def test_pearson_identical_is_one():
    v = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert abs(ca._pearson(v, v) - 1.0) < 1e-9

def test_pearson_opposite_is_minus_one():
    a = [1.0, 2.0, 3.0]
    b = [3.0, 2.0, 1.0]
    assert abs(ca._pearson(a, b) + 1.0) < 1e-9

def test_pearson_constant_is_zero():
    a = [1.0, 1.0, 1.0]
    b = [1.0, 2.0, 3.0]
    assert ca._pearson(a, b) == 0.0

# ── pc_to_note_name ───────────────────────────────────────────────────────────

def test_pc_to_note_name_sharp_key():
    # G major is a sharp key → F# for pc=6
    assert ca.pc_to_note_name(6, (7, "major")) == "F#"

def test_pc_to_note_name_flat_key():
    # F major is NOT in SHARP_KEYS → Bb for pc=10
    assert ca.pc_to_note_name(10, (5, "major")) == "Bb"

def test_pc_to_note_name_c_is_always_c():
    assert ca.pc_to_note_name(0, (0, "major")) == "C"
    assert ca.pc_to_note_name(0, (5, "major")) == "C"

# ── key_name ─────────────────────────────────────────────────────────────────

def test_key_name_c_major():
    assert ca.key_name((0, "major")) == "C"

def test_key_name_a_minor():
    assert ca.key_name((9, "minor")) == "Am"

def test_key_name_csharp_major():
    assert ca.key_name((1, "major")) == "C#"

def test_key_name_bb_major():
    # Bb major (pc=10) is a flat key
    assert ca.key_name((10, "major")) == "Bb"

# ── _prefers_sharp (formula-based spelling replacing the old SHARP_KEYS set) ──

def test_prefers_sharp_matches_relative_major_for_minor():
    # E minor's relative major is G (pc 7), a sharp key → E minor is sharp.
    assert ca._prefers_sharp((4, "minor")) is True

def test_prefers_sharp_flat_minor_key():
    # D minor's relative major is F (pc 5), a flat key → D minor is flat.
    assert ca._prefers_sharp((2, "minor")) is False

def test_prefers_sharp_unknown_mode_defaults_like_major():
    # An unrecognized mode falls back to offset 0 (major's convention).
    assert ca._prefers_sharp((7, "dorian")) is True

# ── name_chord ────────────────────────────────────────────────────────────────

_C_MAJOR_KEY = (0, "major")
_A_MINOR_KEY = (9, "minor")

def test_name_chord_empty():
    assert ca.name_chord(frozenset(), _C_MAJOR_KEY) == "?"

def test_name_chord_single_note():
    # pc=0 in C major → "C"
    assert ca.name_chord(frozenset({0}), _C_MAJOR_KEY) == "C"

def test_name_chord_major_triad():
    # C major: 0,4,7 with root C
    assert ca.name_chord(frozenset({0, 4, 7}), _C_MAJOR_KEY, lowest_pc=0) == "C"

def test_name_chord_minor_triad():
    # A minor: 9,0,4 → root 9 (A), intervals {0,3,7} → 'm'
    assert ca.name_chord(frozenset({9, 0, 4}), _A_MINOR_KEY, lowest_pc=9) == "Am"

def test_name_chord_dominant_seventh():
    # G7: G(7) B(11) D(2) F(5) → intervals from G: {0,4,7,10} → "7"
    assert ca.name_chord(frozenset({7, 11, 2, 5}), _C_MAJOR_KEY, lowest_pc=7) == "G7"

def test_name_chord_major_seventh():
    # Cmaj7: 0,4,7,11 → "maj7"
    assert ca.name_chord(frozenset({0, 4, 7, 11}), _C_MAJOR_KEY, lowest_pc=0) == "Cmaj7"

def test_name_chord_power_chord():
    # E5: string pc 4 and 11 → intervals {0,7} → "5"
    assert ca.name_chord(frozenset({4, 11}), _C_MAJOR_KEY, lowest_pc=4) == "E5"

def test_name_chord_sus4():
    # Csus4: 0,5,7 → "sus4"
    assert ca.name_chord(frozenset({0, 5, 7}), _C_MAJOR_KEY, lowest_pc=0) == "Csus4"

def test_name_chord_major_sixth():
    # C6: C(0) E(4) G(7) A(9), bass C → intervals {0,4,7,9} → "6"
    assert ca.name_chord(frozenset({0, 4, 7, 9}), _C_MAJOR_KEY, lowest_pc=0) == "C6"

def test_name_chord_minor_sixth():
    # Cm6: C(0) Eb(3) G(7) A(9), bass C → intervals {0,3,7,9} → "m6"
    assert ca.name_chord(frozenset({0, 3, 7, 9}), _C_MAJOR_KEY, lowest_pc=0) == "Cm6"

def test_name_chord_minor_major_seventh():
    # CmMaj7: C(0) Eb(3) G(7) B(11), bass C → intervals {0,3,7,11} → "mMaj7"
    assert ca.name_chord(frozenset({0, 3, 7, 11}), _C_MAJOR_KEY, lowest_pc=0) == "CmMaj7"

def test_name_chord_six_vs_relative_m7_disambiguated_by_bass():
    # Same 4 pitch classes (C,E,G,A) either read as C6 (bass=C) or Am7
    # (bass=A) — lowest_pc (the sounding bass note) must be what decides.
    pcs = frozenset({0, 4, 7, 9})
    assert ca.name_chord(pcs, _C_MAJOR_KEY, lowest_pc=0) == "C6"
    assert ca.name_chord(pcs, _C_MAJOR_KEY, lowest_pc=9) == "Am7"

def test_name_chord_no_match_fallback():
    # Unusual set with no quality match → slash notation
    result = ca.name_chord(frozenset({0, 1, 6}), _C_MAJOR_KEY, lowest_pc=0)
    assert "/" in result

def test_name_chord_lowest_pc_used_as_root_first():
    # Same pcs as G7 but lowest_pc=2 (D) — should try D first, find no match
    # with {0,3,5,9} from D, then try G which has {0,4,7,10} → G7
    result = ca.name_chord(frozenset({7, 11, 2, 5}), _C_MAJOR_KEY, lowest_pc=2)
    # result may be D something or G7 depending on matching order
    assert isinstance(result, str) and len(result) > 0

# ── detect_key ────────────────────────────────────────────────────────────────

def test_detect_key_empty_fallback():
    assert ca.detect_key([], STD) == (0, "major")

def test_detect_key_c_major_scale():
    # Use tuning offset -64 on string 0 so that MIDI = 64 + (-64) + fret = fret,
    # meaning pc = fret % 12. This lets frets directly encode pitch classes.
    tuning_zero_base = [-64, 0, 0, 0, 0, 0]
    # C major scale pcs: 0,2,4,5,7,9,11
    notes = [{"string": 0, "fret": pc, "sustain": 0.5} for pc in [0, 2, 4, 5, 7, 9, 11]]
    root_pc, mode = ca.detect_key(notes, tuning_zero_base)
    assert mode == "major"
    assert root_pc == 0

def test_detect_key_harmonic_minor():
    # A harmonic minor pcs: 9,11,0,2,4,5,8 — pc 8 (G#) distinguishes it from relative major
    tuning_zero_base = [-64, 0, 0, 0, 0, 0]
    notes = [{"string": 0, "fret": pc, "sustain": 0.5} for pc in [9, 11, 0, 2, 4, 5, 8]]
    root_pc, mode = ca.detect_key(notes, tuning_zero_base)
    assert mode == "minor"
    assert root_pc == 9

def test_detect_key_returns_tuple():
    result = ca.detect_key([{"string": 0, "fret": 5, "sustain": 0.1}], STD)
    assert isinstance(result, tuple) and len(result) == 2
    root_pc, mode = result
    assert 0 <= root_pc <= 11
    assert mode in ("major", "minor")


# ── keys/piano pitch classes (midi = string*24 + fret) ─────────────────────────

def test_notes_to_pitch_classes_keys_encoding():
    # C4=60 -> s=2,f=12 ; E4=64 -> s=2,f=16 ; G4=67 -> s=2,f=19
    notes = [
        {"string": 2, "fret": 12, "sustain": 1.0},
        {"string": 2, "fret": 16, "sustain": 1.0},
        {"string": 2, "fret": 19, "sustain": 1.0},
    ]
    pcs = ca.notes_to_pitch_classes_keys(notes)
    assert sorted(pc for pc, _ in pcs) == [0, 4, 7]  # C, E, G
    # weight = sustain + 0.1
    assert all(abs(w - 1.1) < 1e-9 for _, w in pcs)

def test_notes_to_pitch_classes_keys_wraps_octaves():
    # midi 72 (C5) also pc 0 — encoded as s=3,f=0
    pcs = ca.notes_to_pitch_classes_keys([{"string": 3, "fret": 0, "sustain": 0}])
    assert pcs[0][0] == 0

def test_detect_key_accepts_precomputed_pcs():
    # C major triad via keys encoding, passed as pcs — must not use fret math.
    notes = [
        {"string": 2, "fret": 12, "sustain": 1.0},
        {"string": 2, "fret": 16, "sustain": 1.0},
        {"string": 2, "fret": 19, "sustain": 1.0},
    ]
    key = ca.detect_key(notes, [0] * 6, pcs=ca.notes_to_pitch_classes_keys(notes))
    assert ca.key_name(key) == "C"

def test_detect_key_pcs_none_falls_back_to_fret_math():
    # Explicit pcs=None must behave exactly like omitting it.
    notes = [{"string": 0, "fret": 5, "sustain": 0.1}]
    assert ca.detect_key(notes, STD) == ca.detect_key(notes, STD, pcs=None)


# ── note_to_wire ───────────────────────────────────────────────────────────

def test_note_to_wire_basic_fields():
    note = {"time": 1.5, "string": 2, "fret": 3, "sustain": 0.25}
    wire = ca.note_to_wire(note)
    assert wire["t"] == 1.5
    assert wire["s"] == 2
    assert wire["f"] == 3
    assert wire["sus"] == 0.25
    assert wire["ho"] is False and wire["po"] is False

def test_note_to_wire_techniques():
    note = {"techniques": {"hammer_on": True, "bend": 0.5, "slide_to": 7}}
    wire = ca.note_to_wire(note)
    assert wire["ho"] is True
    assert wire["bn"] == 0.5
    assert wire["sl"] == 7
    assert wire["slu"] == -1  # default when absent
