"""Tests for the piano/keys difficulty-generation helpers in routes.py.

The scoring/grouping/thinning helpers are nested inside ``setup(app, context)``
(the plugin has no importable pipeline module — see CLAUDE.md "Known open
issues"). Rather than stand up the full FastAPI + slopsmith ``lib.*`` stack, we
extract the self-contained keys helpers straight from the routes.py source via
AST and exercise the real code. Guitar helpers are intentionally not covered
here — this file targets the piano path that ``test_chord_analysis.py`` can't
reach.
"""

import ast
import textwrap
from pathlib import Path

_ROUTES = Path(__file__).parent.parent / "routes.py"

# Self-contained nested helpers that make up the keys difficulty pipeline.
_WANTED = {
    "_note_midi",
    "_note_to_wire",
    "_group_notes_keys",
    "_score_groups_keys",
    "_assign_levels",
    "_notes_for_level_keys",
}


def _load_keys_helpers():
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


H = _load_keys_helpers()


def _kn(string, fret, sustain=0.0, time=0.0):
    """Build a keys note (midi = string*24 + fret)."""
    return {"string": string, "fret": fret, "sustain": sustain, "time": time}


# ── _note_midi ─────────────────────────────────────────────────────────────

def test_note_midi_encoding():
    assert H["_note_midi"](_kn(2, 12)) == 60   # C4
    assert H["_note_midi"](_kn(3, 0)) == 72     # C5


# ── grouping: block chords cluster by onset ──────────────────────────────────

def test_group_notes_keys_clusters_simultaneous():
    notes = [_kn(2, 12, time=0.0), _kn(2, 16, time=0.0), _kn(2, 19, time=0.01)]
    groups = H["_group_notes_keys"](notes, [])
    assert len(groups) == 1
    assert groups[0]["type"] == "chord"
    assert len(groups[0]["notes"]) == 3


def test_group_notes_keys_separates_by_time():
    notes = [_kn(2, 12, time=0.0), _kn(2, 16, time=0.5)]
    groups = H["_group_notes_keys"](notes, [])
    assert len(groups) == 2
    assert all(g["type"] == "note" for g in groups)


def test_group_notes_keys_keeps_explicit_chords():
    chords = [{"time": 1.0, "notes": [_kn(2, 12), _kn(2, 19)]}]
    groups = H["_group_notes_keys"]([], chords)
    assert len(groups) == 1
    assert groups[0]["type"] == "chord"
    assert groups[0]["chord"] is chords[0]


# ── scoring: finite, in-range, polyphony/span sensitive ──────────────────────

def test_score_groups_keys_in_range():
    notes = [_kn(2, 12, time=t * 0.1) for t in range(20)]
    groups = H["_group_notes_keys"](notes, [])
    H["_score_groups_keys"](groups)
    assert all(0.0 <= g["score"] <= 1.0 for g in groups)


def test_score_groups_keys_polyphony_harder_than_single():
    single = H["_group_notes_keys"]([_kn(2, 12, time=0.0)], [])
    H["_score_groups_keys"](single)
    big = H["_group_notes_keys"](
        [_kn(2, 0, time=0.0), _kn(2, 7, time=0.0), _kn(2, 12, time=0.0),
         _kn(2, 16, time=0.0), _kn(2, 24, time=0.0)],
        [],
    )
    H["_score_groups_keys"](big)
    assert big[0]["score"] > single[0]["score"]


# ── thinning: melody + bass first, then inner voices ─────────────────────────

def _one_chord_at_levels(midis, n_levels=5):
    """Return {level: set(midi kept)} for a single chord assigned across levels."""
    notes = [_kn(2, m - 48, time=0.0) for m in midis]  # midi = 2*24 + f => f = m-48
    groups = H["_group_notes_keys"](notes, [])
    H["_score_groups_keys"](groups)
    H["_assign_levels"](groups, n_levels)
    # Force the chord to top level so it's present at every requested level view.
    for g in groups:
        g["level"] = 0
    out = {}
    for lvl in range(n_levels):
        wire, _ = H["_notes_for_level_keys"](groups, lvl)
        out[lvl] = {w["s"] * 24 + w["f"] for w in wire}
    return out


def test_thinning_level0_keeps_melody_and_bass():
    # C E G B (60 64 67 71) — level 0 should keep only 60 (bass) + 71 (melody).
    per = _one_chord_at_levels([60, 64, 67, 71])
    assert per[0] == {60, 71}


def test_thinning_grows_monotonically():
    per = _one_chord_at_levels([60, 64, 67, 71])
    prev = 0
    for lvl in range(5):
        cur = len(per[lvl])
        assert cur >= prev
        prev = cur
    # Top level keeps the full voicing.
    assert per[4] == {60, 64, 67, 71}


def test_thinning_level1_adds_one_inner_voice():
    # 5-voice chord: level 1 keeps bass + one middle + melody (3 notes).
    per = _one_chord_at_levels([60, 64, 67, 70, 74])
    assert per[0] == {60, 74}
    assert len(per[1]) == 3
    assert 60 in per[1] and 74 in per[1]


def test_thinning_single_note_untouched():
    notes = [_kn(2, 12, time=0.0)]
    groups = H["_group_notes_keys"](notes, [])
    H["_score_groups_keys"](groups)
    H["_assign_levels"](groups, 5)
    wire, chords = H["_notes_for_level_keys"](groups, 0)
    assert chords == []
    assert len(wire) == 1
    assert wire[0]["s"] * 24 + wire[0]["f"] == 60
