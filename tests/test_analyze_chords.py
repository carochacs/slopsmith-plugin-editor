"""Tests for the analyze-chords route's logic in routes.py.

Mirrors test_difficulty_keys.py's approach: the note-grouping helper is
nested inside ``setup(app, context)`` (the plugin has no importable
pipeline module — see CLAUDE.md "Known open issues"), so rather than stand
up the full FastAPI + slopsmith ``lib.*`` stack we extract it from the
routes.py source via AST and exercise the real code. chord_analysis.py is
a normal importable sibling module, so it's imported directly.
"""

import ast
import sys
import textwrap
from pathlib import Path

_ROUTES = Path(__file__).parent.parent / "routes.py"

_here = Path(__file__).parent
sys.path.insert(0, str(_here.parent))
import chord_analysis as ca

_WANTED = {"_group_notes_impl", "_is_keys_arr"}


def _load_helpers():
    """Exec the named nested functions from routes.py:setup into a namespace."""
    src = _ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(src)
    setup_fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "setup"
    )
    ns = {}
    for node in setup_fn.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED:
            code = textwrap.dedent(ast.get_source_segment(src, node))
            exec(compile(code, str(_ROUTES), "exec"), ns)
    missing = _WANTED - ns.keys()
    assert not missing, f"helpers not found in routes.py: {missing}"
    return ns


H = _load_helpers()
STD = [0] * 6  # standard guitar tuning, no offset


def _analyze(notes, chords, handshapes, tuning, is_keys=False):
    """Reimplements analyze-chords' _run() body against the real helpers —
    same key-detection + grouping + naming steps the route performs."""
    all_notes = list(notes)
    for ch in chords:
        for cn in ch.get("notes", []):
            all_notes.append({
                "string": cn.get("string", 0),
                "fret": cn.get("fret", 0),
                "sustain": cn.get("sustain", ch.get("sustain", 0)),
            })
    if is_keys:
        key = ca.detect_key(all_notes, tuning, pcs=ca.notes_to_pitch_classes_keys(all_notes))
    else:
        key = ca.detect_key(all_notes, tuning)
    detected_key_name = ca.key_name(key)

    groups = H["_group_notes_impl"](notes, chords, handshapes, is_keys=is_keys)
    chords_out = []
    for g in groups:
        g_notes = g.get("notes") or []
        if len(g_notes) < 2:
            continue
        if is_keys:
            midis = [int(n.get("string", 0)) * 24 + int(n.get("fret", 0)) for n in g_notes]
        else:
            midis = [ca.fret_to_midi(n.get("string", 0), n.get("fret", 0), tuning) for n in g_notes]
        if not midis:
            continue
        pcs = frozenset(m % 12 for m in midis)
        lowest_pc = min(midis) % 12
        chords_out.append({
            "time": round(float(g.get("time", 0)), 3),
            "name": ca.name_chord(pcs, key, lowest_pc),
        })
    chords_out.sort(key=lambda c: c["time"])
    return {"key": detected_key_name, "chords": chords_out}


def _note(string, fret, time=0.0, sustain=0.3):
    return {"string": string, "fret": fret, "time": time, "sustain": sustain}


# ── chord grouping + naming ─────────────────────────────────────────────────

def test_simultaneous_triad_is_named():
    # C major triad, standard tuning: low-E str5 fret8 (C, midi48, lowest),
    # A str4 fret7 (E, midi52), D str3 fret5 (G, midi55) — all at t=1.0 so
    # the time-proximity clustering step in _group_notes_impl groups them.
    notes = [
        _note(5, 8, time=1.0),
        _note(4, 7, time=1.0),
        _note(3, 5, time=1.0),
    ]
    result = _analyze(notes, [], [], STD)
    assert len(result["chords"]) == 1
    assert result["chords"][0]["name"] == "C"
    assert result["chords"][0]["time"] == 1.0


def test_lone_note_is_not_a_chord():
    notes = [_note(5, 8, time=1.0)]
    result = _analyze(notes, [], [], STD)
    assert result["chords"] == []


def test_notes_far_apart_in_time_are_separate_and_excluded():
    # 500ms apart — well outside the 150ms default clustering window —
    # so each lands in its own single-note group and neither is a "chord".
    notes = [_note(5, 8, time=0.0), _note(4, 7, time=0.5)]
    result = _analyze(notes, [], [], STD)
    assert result["chords"] == []


def test_explicit_chord_object_is_named():
    chords = [{
        "time": 2.0,
        "notes": [
            {"string": 5, "fret": 8},  # C
            {"string": 4, "fret": 7},  # E
            {"string": 3, "fret": 5},  # G
        ],
    }]
    result = _analyze([], chords, [], STD)
    assert len(result["chords"]) == 1
    assert result["chords"][0]["name"] == "C"
    assert result["chords"][0]["time"] == 2.0


def test_mixed_chord_and_solo_notes():
    notes = [
        _note(5, 8, time=1.0),
        _note(4, 7, time=1.0),
        _note(3, 5, time=1.0),
        _note(0, 3, time=5.0),  # solo note, far from the triad
    ]
    result = _analyze(notes, [], [], STD)
    assert len(result["chords"]) == 1
    assert result["chords"][0]["time"] == 1.0


# ── key detection plumbing ──────────────────────────────────────────────────

def test_key_detection_matches_chord_analysis_directly():
    # tuning_zero_base trick from test_chord_analysis.py: fret encodes pc
    # directly. Full C major scale, well-separated in time so nothing
    # clusters into a chord — only key detection is under test here.
    tuning_zero_base = [-64, 0, 0, 0, 0, 0]
    notes = [
        _note(0, pc, time=float(i))
        for i, pc in enumerate([0, 2, 4, 5, 7, 9, 11])
    ]
    result = _analyze(notes, [], [], tuning_zero_base)
    assert result["key"] == ca.key_name(ca.detect_key(notes, tuning_zero_base))
    assert result["key"] == "C"


def test_empty_arrangement_falls_back_to_c_major_with_no_chords():
    result = _analyze([], [], [], STD)
    assert result["key"] == "C"
    assert result["chords"] == []
