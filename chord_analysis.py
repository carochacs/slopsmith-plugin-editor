"""Pitch utilities, key detection, and chord naming for sloppak arrangements."""

# Standard 6-string open MIDI (E2 A2 D3 G3 B3 E4).
# Index 0 = highest string (e-string), index 5 = lowest (E-string).
OPEN_MIDI = [64, 59, 55, 50, 45, 40]

# Krumhansl-Schmuckler key profiles (C-rooted).
KS_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KS_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

# Keys that use sharp spelling (root_pc, mode) — all others use flat.
SHARP_KEYS = {
    (0, "major"), (7, "major"), (2, "major"), (9, "major"), (4, "major"),
    (11, "major"), (6, "major"), (1, "major"),
    (9, "minor"), (4, "minor"), (11, "minor"), (6, "minor"),
    (1, "minor"), (8, "minor"), (3, "minor"),
}

NOTE_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_NAMES_FLAT  = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# Chord quality table: (interval frozenset relative to root, suffix)
CHORD_QUALITIES = [
    (frozenset({0, 4, 7, 11}), "maj7"),
    (frozenset({0, 4, 7, 10}), "7"),
    (frozenset({0, 3, 7, 10}), "m7"),
    (frozenset({0, 3, 6, 9}),  "dim7"),
    (frozenset({0, 3, 6, 10}), "m7b5"),
    (frozenset({0, 4, 8}),     "aug"),
    (frozenset({0, 3, 6}),     "dim"),
    (frozenset({0, 5, 7}),     "sus4"),
    (frozenset({0, 2, 7}),     "sus2"),
    (frozenset({0, 4, 7}),     ""),
    (frozenset({0, 3, 7}),     "m"),
    (frozenset({0, 4}),        "5"),
    (frozenset({0, 7}),        "5"),
]


def fret_to_midi(string_idx: int, fret: int, tuning: list[int]) -> int:
    """Convert string/fret to MIDI note number.

    string_idx 0 = highest string (e-string in standard tuning).
    tuning is an array of semitone offsets (length = string count).
    For bass (4 strings), open strings are E1 A1 D2 G2 (indices 0–3 from high).
    """
    n = len(tuning)
    if n == 4:
        # Bass: G2 D2 A1 E1 from high to low, MIDI 55 50 45 40
        open_midi_bass = [55, 50, 45, 40]
        base = open_midi_bass[string_idx] if string_idx < 4 else 40
    else:
        # Guitar (6) or extended-range: trim from low end of OPEN_MIDI
        open_midi_trimmed = OPEN_MIDI[:n]
        base = open_midi_trimmed[string_idx] if string_idx < n else OPEN_MIDI[0]
    return base + tuning[string_idx] + fret


def notes_to_pitch_classes(notes: list[dict], tuning: list[int]) -> list[tuple[int, float]]:
    """Return (pitch_class, weight) pairs for each note (weight = sustain + 0.1)."""
    result = []
    for n in notes:
        midi = fret_to_midi(n.get("string", 0), n.get("fret", 0), tuning)
        weight = float(n.get("sustain", 0.0)) + 0.1
        result.append((midi % 12, weight))
    return result


def notes_to_pitch_classes_keys(notes: list[dict]) -> list[tuple[int, float]]:
    """(pitch_class, weight) pairs for keys/piano notes.

    Keys arrangements encode absolute MIDI pitch in the string/fret fields as
    ``midi = string * 24 + fret`` (there is no fretboard), so pitch classes come
    straight from that encoding rather than from tuning-relative fret math.
    """
    result = []
    for n in notes:
        midi = int(n.get("string", 0)) * 24 + int(n.get("fret", 0))
        weight = float(n.get("sustain", 0.0)) + 0.1
        result.append((midi % 12, weight))
    return result


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    den_a = sum((a[i] - mean_a) ** 2 for i in range(n)) ** 0.5
    den_b = sum((b[i] - mean_b) ** 2 for i in range(n)) ** 0.5
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


def detect_key(notes: list[dict], tuning: list[int], pcs=None) -> tuple[int, str]:
    """Detect key using Krumhansl-Schmuckler.

    Returns (root_pc, mode) where root_pc is 0–11 (C=0) and mode is 'major'|'minor'.
    Falls back to (0, 'major') when there are no notes.

    ``pcs`` optionally supplies precomputed (pitch_class, weight) pairs — used by
    keys/piano callers that derive pitch classes from the MIDI encoding via
    ``notes_to_pitch_classes_keys`` instead of tuning-relative fret math.
    """
    if pcs is None:
        pcs = notes_to_pitch_classes(notes, tuning)
    if not pcs:
        return (0, "major")

    # Accumulate weighted pitch class histogram
    histogram = [0.0] * 12
    for pc, w in pcs:
        histogram[pc] += w

    best_r = -2.0
    best_key = (0, "major")
    for root in range(12):
        for mode, profile in (("major", KS_MAJOR), ("minor", KS_MINOR)):
            # Rotate profile so root aligns with C slot
            rotated = [profile[(i - root) % 12] for i in range(12)]
            r = _pearson(histogram, rotated)
            if r > best_r:
                best_r = r
                best_key = (root, mode)
    return best_key


def pc_to_note_name(pc: int, key: tuple[int, str]) -> str:
    """Return enharmonically correct note name for a pitch class in the given key."""
    if key in SHARP_KEYS:
        return NOTE_NAMES_SHARP[pc % 12]
    return NOTE_NAMES_FLAT[pc % 12]


def key_name(key: tuple[int, str]) -> str:
    """Return human-readable key string, e.g. 'A minor', 'C# major'."""
    root_pc, mode = key
    root = pc_to_note_name(root_pc, key)
    if mode == "major":
        return root
    return f"{root}m"


def name_chord(
    pitch_classes: frozenset[int],
    key: tuple[int, str],
    lowest_pc: int | None = None,
) -> str:
    """Return chord name string (e.g. 'Am', 'C#m7').

    Tries lowest_pc as root first; falls back to all other pitch classes.
    Single pitch class → note name only.
    """
    if not pitch_classes:
        return "?"
    if len(pitch_classes) == 1:
        return pc_to_note_name(next(iter(pitch_classes)), key)

    candidates = []
    if lowest_pc is not None and lowest_pc in pitch_classes:
        candidates.append(lowest_pc)
    for pc in sorted(pitch_classes):
        if pc != lowest_pc:
            candidates.append(pc)

    for root_pc in candidates:
        intervals = frozenset((pc - root_pc) % 12 for pc in pitch_classes)
        for quality_set, suffix in CHORD_QUALITIES:
            if intervals == quality_set:
                root_name = pc_to_note_name(root_pc, key)
                return f"{root_name}{suffix}"

    # No match — list all note names
    names = [pc_to_note_name(pc, key) for pc in sorted(pitch_classes)]
    return "/".join(names)
