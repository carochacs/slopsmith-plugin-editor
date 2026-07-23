"""Tests for _resolve_sandboxed_path, the traversal guard shared by the
lyrics.json read (load) and write (save) paths in routes.py.

Same AST-extraction approach as test_difficulty_keys.py / test_analyze_chords.py
— the helper is a plain nested function inside setup(app, context), so it's
extracted from source rather than standing up the full plugin + slopsmith
lib.* stack (not importable from this repo — see CLAUDE.md).
"""

import ast
import tempfile
import textwrap
from pathlib import Path

_ROUTES = Path(__file__).parent.parent / "routes.py"


def _load_helper():
    src = _ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(src)
    setup_fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "setup"
    )
    node = next(
        n for n in setup_fn.body
        if isinstance(n, ast.FunctionDef) and n.name == "_resolve_sandboxed_path"
    )
    code = textwrap.dedent(ast.get_source_segment(src, node))
    ns = {"Path": Path}
    exec(compile(code, str(_ROUTES), "exec"), ns)
    return ns["_resolve_sandboxed_path"]


resolve = _load_helper()


def test_none_when_rel_is_empty():
    with tempfile.TemporaryDirectory() as d:
        assert resolve(d, "") is None
        assert resolve(d, None) is None


def test_resolves_plain_relative_path():
    with tempfile.TemporaryDirectory() as d:
        result = resolve(d, "lyrics.json")
        assert result == (Path(d).resolve() / "lyrics.json")


def test_resolves_nested_relative_path():
    with tempfile.TemporaryDirectory() as d:
        result = resolve(d, "sub/lyrics.json")
        assert result == (Path(d).resolve() / "sub" / "lyrics.json")


def test_rejects_parent_traversal():
    with tempfile.TemporaryDirectory() as d:
        assert resolve(d, "../lyrics.json") is None
        assert resolve(d, "../../etc/passwd") is None
        assert resolve(d, "sub/../../lyrics.json") is None


def test_rejects_absolute_path_outside_base():
    with tempfile.TemporaryDirectory() as d:
        assert resolve(d, "/etc/passwd") is None
