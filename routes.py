"""Arrangement Editor plugin — backend routes."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.dom import minidom

import base64

from fastapi import UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse

import yaml


# Matches a plausible 4-digit album year inside free-form text — used to
# sanitize <albumYear> when it has been polluted by copyright strings from
# GP imports (RsCli parses albumYear as Int32 and rejects anything else).
_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")


_sessions = None


def setup(app, context):
    config_dir = context["config_dir"]
    get_dlc_dir = context["get_dlc_dir"]

    from lib.song import load_song, phrase_to_wire
    from lib.psarc import unpack_psarc
    from lib.patcher import pack_psarc
    from lib.audio import find_wem_files, convert_wem
    from lib import sloppak as sloppak_mod

    # The editor needs to write extracted audio / art into a directory it
    # can also serve from. On the web Docker image `slopsmith/static/` is
    # writable, so historically the plugin reused that path and surfaced
    # the files at the slopsmith core's `/static/...` mount. On desktop
    # bundles (AppImage / .app / NSIS install) `slopsmith/static/` lives
    # inside the read-only application package, so writes blow up with
    # `OSError: [Errno 30] Read-only file system`.
    #
    # Probe the legacy location at startup. If it's writable we keep the
    # old behaviour; if not we fall back to a per-user cache dir under
    # `config_dir` and serve those files via a dedicated plugin route.
    # Read-back logic accepts BOTH URL prefixes so a song frontend hands
    # back an old `/static/...` audio_url across upgrades still resolves.
    LEGACY_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
    LEGACY_STATIC_URL = "/static"
    CACHE_URL = "/api/plugins/editor/cache"

    def _legacy_static_writable() -> bool:
        # Writability alone isn't enough — when this plugin is installed
        # into the user plugins dir (e.g. `~/.config/slopsmith-desktop/
        # plugins/editor/`), `parent.parent.parent / static` resolves to
        # a writable dir under the user config that Slopsmith does NOT
        # mount as `/static`. Writing audio there would 404 on fetch.
        # Require a sentinel file that Slopsmith always ships in its
        # real static root (`app.js`) so we only short-circuit to legacy
        # mode when this is genuinely the served mount.
        if not (LEGACY_STATIC_DIR / "app.js").exists():
            return False
        try:
            probe = LEGACY_STATIC_DIR / ".editor_write_probe"
            probe.touch()
            probe.unlink()
            return True
        except (OSError, PermissionError):
            return False

    if _legacy_static_writable():
        STORAGE_DIR = LEGACY_STATIC_DIR
        STORAGE_URL = LEGACY_STATIC_URL
    else:
        STORAGE_DIR = Path(config_dir) / "editor_cache"
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        STORAGE_URL = CACHE_URL

    # Sloppak unpack cache — must NOT live under STORAGE_DIR when STORAGE_URL
    # is /static, because that directory is mounted as the public web root
    # and anything under it is downloadable by URL. Stems / manifests /
    # covers of every loaded sloppak would leak. Use the shared private
    # cache the server exposes via the plugin context (lives under
    # CONFIG_DIR), with a fall-back for any older harness that doesn't
    # surface the helper.
    _get_sloppak_cache = context.get("get_sloppak_cache_dir")
    if callable(_get_sloppak_cache):
        SLOPPAK_CACHE = Path(_get_sloppak_cache())
    else:
        SLOPPAK_CACHE = config_dir / "sloppak_cache"

    # Convenience for code that needs to resolve an audio_url back to a
    # filesystem path — accepts the legacy /static/* form so a frontend
    # session that captured an old URL still works after an upgrade.
    def _resolve_storage_url(url: str) -> Path | None:
        if not url:
            return None
        for prefix, base in (
            (LEGACY_STATIC_URL + "/", LEGACY_STATIC_DIR),
            (CACHE_URL + "/",         STORAGE_DIR if STORAGE_URL == CACHE_URL else None),
        ):
            if base is None:
                continue
            if url.startswith(prefix):
                rel = url[len(prefix):]
                # Path-traversal guard: resolved path must stay inside base.
                candidate = (base / rel).resolve()
                try:
                    candidate.relative_to(base.resolve())
                except ValueError:
                    return None
                return candidate
        return None

    # Active editing sessions: session_id -> {dir, audio_file, filename, song_data}
    sessions = {}

    global _sessions
    _sessions = sessions

    def _arrangement_id(name: str, used: set) -> str:
        """Map an arrangement name to a stable filesystem-safe id, avoiding
        collisions (suffix counter starts at 2: bass, bass2, bass3, ...)."""
        base = re.sub(r"[^a-z0-9_]", "", (name or "arr").lower().replace(" ", "_")) or "arr"
        aid = base
        i = 2
        while aid in used:
            aid = f"{base}{i}"
            i += 1
        used.add(aid)
        return aid

    def _normalize_tuning_to_count(tuning, real_count: int) -> list:
        """Slice/pad a tuning list to exactly `real_count` entries.

        Trailing zeros (RS-XML schema padding) are dropped first.
        Callers should pass a `real_count` that already accounts for
        any genuine extended-range offsets (via
        `_arrangement_string_count`), so the final hard slice only
        ever trims zeros — if a non-zero high-index offset survives
        that, it really is being truncated (treat as a caller bug
        rather than silently preserving and breaking the length
        contract).
        """
        out = list(tuning) if isinstance(tuning, list) else []
        if len(out) > real_count:
            # Drop trailing zeros until we hit `real_count` or a non-zero.
            while len(out) > real_count and out[-1] == 0:
                out.pop()
            if len(out) > real_count:
                out = out[:real_count]
        while len(out) < real_count:
            out.append(0)
        return out

    def _safe_string_index(v) -> int | None:
        """Coerce a note's `string` field to int. Returns None for
        non-numeric / null values rather than raising — older client
        payloads or corrupted manifests can ship `string: null` or
        unexpected types, and we'd rather skip those entries than
        500 the entire save/build."""
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _arrangement_string_count(arr) -> int:
        """Mirror of screen.js `_stringCountFor` — composes the same
        signals so backend writes a tuning slice that round-trips the
        editor's in-memory string count."""
        is_bass = "bass" in (arr.get("name", "") or "").lower()
        baseline = 4 if is_bass else 6
        try:
            ext = int(arr.get("_extendedStrings", 0) or 0)
        except (TypeError, ValueError):
            ext = 0
        n = baseline + max(0, ext)
        tuning = arr.get("tuning")
        if isinstance(tuning, list) and len(tuning) != 6:
            n = max(n, len(tuning))
        # Chord-template signal — count the highest *used* fret slot
        # (last non(-1) index) so RS-XML's unconditional length-6
        # frets array doesn't inflate the count for normal 4-string
        # bass arrangements.
        for ct in arr.get("chord_templates", []) or []:
            frets = ct.get("frets")
            if isinstance(frets, list):
                for i in range(len(frets) - 1, -1, -1):
                    if frets[i] != -1:
                        if i + 1 > n:
                            n = i + 1
                        break
        for note in arr.get("notes", []) or []:
            s = _safe_string_index(note.get("string", 0))
            if s is not None and s + 1 > n:
                n = s + 1
        for ch in arr.get("chords", []) or []:
            for cn in ch.get("notes", []) or []:
                s = _safe_string_index(cn.get("string", 0))
                if s is not None and s + 1 > n:
                    n = s + 1
        return max(4, min(8, n))

    def _is_extended_range(arr) -> bool:
        """True if `arr` has more strings than stock-RS PSARC supports.

        Delegates to `_arrangement_string_count` so all the same
        signals (explicit `_extendedStrings` counter, tuning length,
        chord-template highest-used-fret, max note string index) are
        composed in one place. The earlier inline version missed
        cases like a 5-string bass with tuning.length==5 — that
        unambiguous extended-range signal wasn't covered by the
        `len > 6` check.
        """
        is_bass = "bass" in (arr.get("name", "") or "").lower()
        role_limit = 4 if is_bass else 6
        return _arrangement_string_count(arr) > role_limit

    def _validate_editor_upload_path(path_str: str, prefix: str) -> Path | None:
        """Resolve a client-supplied upload path and constrain it to the
        editor's tempfile.mkdtemp(prefix=...) sandbox. Returns the resolved
        path on success, or None if the path escapes the sandbox or doesn't
        exist. Defends against import-keys / import-drums / import-keys-midi
        being pointed at arbitrary readable files via the request body.
        """
        if not path_str:
            return None
        try:
            resolved = Path(path_str).resolve()
        except Exception:
            return None
        if not resolved.exists():
            return None
        tmp_root = Path(tempfile.gettempdir()).resolve()
        try:
            rel = resolved.relative_to(tmp_root)
        except ValueError:
            return None
        # First component should be the mkdtemp dir whose name starts
        # with our prefix (e.g. slopsmith_gp_XXXX).
        if not rel.parts or not rel.parts[0].startswith(prefix):
            return None
        return resolved

    # ── Cache file server (only meaningful when STORAGE_URL == CACHE_URL,
    #    but registered unconditionally — the route 404s if a request
    #    targets the cache on a build that's still using LEGACY_STATIC_DIR).
    @app.get(CACHE_URL + "/{name:path}")
    def get_cached_file(name: str):
        if STORAGE_URL != CACHE_URL:
            return JSONResponse({"error": "cache disabled (legacy static dir is writable)"}, status_code=404)
        candidate = (STORAGE_DIR / name).resolve()
        try:
            candidate.relative_to(STORAGE_DIR.resolve())
        except ValueError:
            return JSONResponse({"error": "invalid path"}, status_code=400)
        if not candidate.exists() or not candidate.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(candidate)

    # ── List available CDLC files ────────────────────────────────────────

    @app.get("/api/plugins/editor/songs")
    async def list_songs():
        dlc_dir = get_dlc_dir()
        if not dlc_dir or not dlc_dir.exists():
            return []
        files = []
        seen: set = set()
        # Single os.walk pass so large libraries are traversed only once.
        # Sloppak has two valid forms: zip (`.sloppak` file) and authoring
        # directory (`.sloppak/`). All suffixes are lowercased so that
        # e.g. `.PSARC` / `.SLOPPAK` from older backends are handled correctly.
        _FORMATS = {".sloppak": "sloppak", ".psarc": "psarc"}
        for dirpath, dirnames, filenames in os.walk(dlc_dir):
            dirnames.sort()
            for name in filenames:
                ext = os.path.splitext(name)[1].lower()
                fmt = _FORMATS.get(ext)
                if fmt is None:
                    continue
                full = Path(dirpath) / name
                rel = str(full.relative_to(dlc_dir))
                if rel not in seen:
                    seen.add(rel)
                    files.append({"filename": rel, "format": fmt})
            # Collect authoring-form .sloppak/ dirs and prune them from
            # dirnames so os.walk won't descend into their contents.
            to_prune = []
            for name in dirnames:
                ext = os.path.splitext(name)[1].lower()
                if ext == ".sloppak":
                    full = Path(dirpath) / name
                    rel = str(full.relative_to(dlc_dir))
                    if rel not in seen:
                        seen.add(rel)
                        files.append({"filename": rel, "format": "sloppak"})
                    to_prune.append(name)
            for name in to_prune:
                dirnames.remove(name)
        files.sort(key=lambda x: x["filename"])
        return files

    # ── Load a CDLC for editing ──────────────────────────────────────────

    @app.post("/api/plugins/editor/load")
    async def load_cdlc(data: dict):
        filename = data.get("filename", "")
        if not filename:
            return JSONResponse({"error": "No filename"}, 400)

        dlc_dir = get_dlc_dir()
        if not dlc_dir:
            return JSONResponse({"error": "DLC folder not configured"}, 400)
        filepath = (dlc_dir / filename).resolve()
        # Constrain client-supplied filename to dlc_dir — defends against
        # `../` traversal and absolute paths now that filename can include
        # subdirectories.
        try:
            filepath.relative_to(dlc_dir.resolve())
        except ValueError:
            return JSONResponse({"error": "Invalid filename"}, 400)
        if filepath.suffix.lower() not in (".psarc", ".sloppak"):
            return JSONResponse({"error": "Unsupported file type"}, 400)
        if not filepath.exists():
            return JSONResponse({"error": "File not found"}, 404)

        is_sloppak = filepath.suffix.lower() == ".sloppak"

        def _load_psarc():
            tmp_dir = tempfile.mkdtemp(prefix="slopsmith_editor_")
            try:
                unpack_psarc(str(filepath), tmp_dir)
                song = load_song(tmp_dir)
            except Exception as e:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise RuntimeError(f"Failed to load: {e}")

            # Convert audio
            audio_url = None
            audio_file = None
            wem_files = find_wem_files(tmp_dir)
            if wem_files:
                try:
                    audio_path = convert_wem(
                        wem_files[0], os.path.join(tmp_dir, "audio")
                    )
                    audio_file = audio_path
                    # Sanitise the full relative path (not just .stem) so
                    # nested `foo/bar.psarc` and `baz/bar.psarc` don't
                    # overwrite each other's editor_audio_*.* file under
                    # STATIC_DIR. Matches the sloppak path's id scheme
                    # and the session_id sanitisation.
                    audio_id = filename.replace("/", "__").replace("\\", "__").replace(" ", "_")
                    ext = Path(audio_path).suffix
                    dest = STORAGE_DIR / f"editor_audio_{audio_id}{ext}"
                    shutil.copy2(audio_path, dest)
                    audio_url = f"{STORAGE_URL}/editor_audio_{audio_id}{ext}"
                except Exception as e:
                    print(f"[Editor] Audio conversion failed: {e}")

            # Find the arrangement XML files for later save
            xml_files = []
            for xf in Path(tmp_dir).rglob("*.xml"):
                try:
                    root = ET.parse(xf).getroot()
                    if root.tag == "song":
                        el = root.find("arrangement")
                        if el is not None and el.text:
                            low = el.text.lower().strip()
                            if low not in ("vocals", "showlights", "jvocals"):
                                xml_files.append(str(xf))
                except Exception:
                    continue

            result = _song_to_dict(song, audio_url)
            result["format"] = "psarc"
            return result, tmp_dir, audio_file, xml_files, None

        def _load_sloppak():
            SLOPPAK_CACHE.mkdir(parents=True, exist_ok=True)
            loaded = sloppak_mod.load_song(filename, dlc_dir, SLOPPAK_CACHE)
            song = loaded.song
            # Distinguish authoring (directory) form from distribution (zip)
            # form so save knows whether to re-zip. With dir-form, source_dir
            # *is* the original sloppak dir; rewriting the manifest +
            # arrangement files in place is the whole save.
            sloppak_form = "dir" if filepath.is_dir() else "zip"

            # Build a per-arrangement id list from the manifest so we can map
            # edits back to the correct JSON file on save.
            arrangement_ids = []
            for entry in (loaded.manifest.get("arrangements", []) or []):
                arrangement_ids.append(entry.get("id", ""))

            # Pick an audio URL: prefer the "full" stem, else the first stem.
            audio_url = None
            audio_file = None
            stem_path = None

            def _safe_stem_path(stem_entry: dict) -> "Path | None":
                """Resolve stem file path and reject traversal outside source_dir."""
                rel = stem_entry.get("file", "")
                if not rel:
                    return None
                source_resolved = loaded.source_dir.resolve()
                candidate = (loaded.source_dir / rel).resolve()
                try:
                    candidate.relative_to(source_resolved)
                except ValueError:
                    return None
                return candidate if candidate.exists() else None

            for s in loaded.stems:
                if s.get("id") == "full":
                    stem_path = _safe_stem_path(s)
                    break
            if stem_path is None and loaded.stems:
                stem_path = _safe_stem_path(loaded.stems[0])
            if stem_path and stem_path.exists():
                # Same basename-collision class as session_id: nested paths
                # like `foo/bar.psarc` and `baz/bar.sloppak` both reduce
                # to stem "bar". Use a sanitised full path so two browser
                # tabs loading distinct songs don't overwrite each other's
                # `editor_audio_*` file under STATIC_DIR.
                audio_id = filename.replace("/", "__").replace("\\", "__").replace(" ", "_")
                ext = stem_path.suffix
                dest = STORAGE_DIR / f"editor_audio_{audio_id}{ext}"
                shutil.copy2(stem_path, dest)
                audio_url = f"{STORAGE_URL}/editor_audio_{audio_id}{ext}"
                audio_file = str(stem_path)

            result = _song_to_dict(song, audio_url)
            result["format"] = "sloppak"
            # `lib/sloppak.load_song()` doesn't restore song.offset (the
            # sloppak format doesn't carry an explicit offset field today),
            # so song.offset is 0 here. If the manifest happens to surface
            # one (e.g. a forward-compat extension that mirrors PSARC's
            # song-level <offset>), pick it up so the audio_offset that
            # gets fed to the +Keys/+Drums converters matches the chart.
            try:
                manifest_offset = float(loaded.manifest.get("offset", 0) or 0)
            except (TypeError, ValueError):
                manifest_offset = 0.0
            if manifest_offset:
                result["offset"] = manifest_offset
            # Carry the manifest-derived arrangement id list onto each
            # arrangement so the frontend can round-trip it back to us.
            # Use a single `used_ids` set when generating fallback ids so two
            # nameless arrangements don't both end up as "arr".
            used_ids: set = {aid for aid in arrangement_ids if aid}
            for i, arr_data in enumerate(result.get("arrangements", [])):
                aid = arrangement_ids[i] if i < len(arrangement_ids) else ""
                if not aid:
                    aid = _arrangement_id(arr_data["name"], used_ids)
                arr_data["id"] = aid

            # Round-trip-preserve the arrangement-level arrays the editor UI
            # doesn't expose: anchors, handshapes, phrases. The save path
            # passes them straight through so the next save doesn't drop them.
            for i, arr in enumerate(song.arrangements):
                arr_data = result["arrangements"][i]
                arr_data["anchors"] = [
                    {"time": a.time, "fret": a.fret, "width": a.width}
                    for a in (arr.anchors or [])
                ]
                arr_data["handshapes"] = [
                    {"chord_id": h.chord_id, "start_time": h.start_time, "end_time": h.end_time}
                    for h in (arr.hand_shapes or [])
                ]
                if arr.phrases:
                    arr_data["phrases"] = [phrase_to_wire(p) for p in arr.phrases]

            return (
                result,
                str(loaded.source_dir),  # working dir = the unpacked sloppak cache
                audio_file,
                None,                    # no xml_files for sloppak
                {
                    "manifest": loaded.manifest,
                    "arrangement_ids": arrangement_ids,
                    "form": sloppak_form,
                    "original_path": str(filepath),
                },
            )

        try:
            if is_sloppak:
                result, session_dir, audio_file, xml_files, sloppak_state = (
                    await asyncio.get_event_loop().run_in_executor(None, _load_sloppak)
                )
            else:
                result, session_dir, audio_file, xml_files, sloppak_state = (
                    await asyncio.get_event_loop().run_in_executor(None, _load_psarc)
                )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        # Session id has to disambiguate the full relative path, not just
        # the basename — the picker now emits paths like `foo/bar.psarc`
        # and `baz/bar.sloppak` that share the same stem, and a basename-
        # keyed session would have two browser tabs collide on `bar`,
        # corrupting the second's saves into the first's working dir.
        # Sanitise path separators / spaces into a stable id (matches the
        # `lib.sloppak._safe_id` convention) and append the suffix so a
        # `.psarc` and `.sloppak` of the same name still get distinct ids.
        sanitised = filename.replace("/", "__").replace("\\", "__").replace(" ", "_")
        session_id = sanitised
        # Clean up previous PSARC session for same file (sloppak sessions
        # use the cache dir directly — never delete it on session swap).
        if session_id in sessions:
            old = sessions[session_id]
            if old.get("format") == "psarc":
                shutil.rmtree(old["dir"], ignore_errors=True)

        sessions[session_id] = {
            "dir": session_dir,
            "audio_file": audio_file,
            "filename": filename,
            "xml_files": xml_files,
            "format": "sloppak" if is_sloppak else "psarc",
            "sloppak_state": sloppak_state,
            # Stash song-level metadata so save_as_sloppak can carry
            # album/year through to the generated manifest even though
            # the frontend's currentSong state only tracks title/artist.
            "metadata": {
                "title": result.get("title", ""),
                "artist": result.get("artist", ""),
                "album": result.get("album", ""),
                "year": result.get("year", ""),
            },
            "last_touched": time.time(),
        }
        result["session_id"] = session_id
        return result

    # ── Save edited arrangement back to PSARC ────────────────────────────

    @app.post("/api/plugins/editor/save")
    async def save_cdlc(data: dict):
        session_id = data.get("session_id", "")
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "No active session"}, 400)
        session["last_touched"] = time.time()

        raw_arr_idx = data.get("arrangement_index")
        if raw_arr_idx is None:
            arrangement_index = 0
        else:
            try:
                arrangement_index = int(raw_arr_idx)
            except (TypeError, ValueError):
                return JSONResponse({"error": "arrangement_index must be an integer"}, 400)
        if arrangement_index < 0:
            return JSONResponse({"error": "arrangement_index must be non-negative"}, 400)
        notes = data.get("notes", [])
        chords = data.get("chords", [])
        chord_templates = data.get("chord_templates", [])
        beats = data.get("beats", [])
        sections = data.get("sections", [])
        # Merge session metadata (album/year captured at PSARC load
        # time) with anything the frontend sent. `_buildSaveBody` ships
        # `{title, artist}` on every save path; this merge keeps the
        # PSARC-only fields (album, year) that the frontend never
        # round-trips, so they survive a save through this endpoint.
        metadata = dict(session.get("metadata") or {})
        metadata.update(data.get("metadata") or {})

        # Sloppak save can be a full snapshot of all arrangements (needed when
        # arrangements were added). If arrangements isn't provided, save_cdlc
        # only updates the single arrangement at arrangement_index.
        all_arrangements = data.get("arrangements")

        # Explicit opt-in to lose extended-range data on a PSARC save.
        # Set by the frontend when the user picked "Save as PSARC (lose
        # extra strings)" in the format-prompt modal. No-op for sloppak
        # (sloppak preserves extended range natively).
        # Only honour `force_psarc_truncate` on PSARC-sourced sessions —
        # sloppak handles extended range natively, and silently dropping
        # data there because a buggy client / replayed request happened
        # to include the flag would be surprising.
        force_psarc_truncate = (
            bool(data.get("force_psarc_truncate", False))
            and session.get("format") == "psarc"
        )
        if force_psarc_truncate:
            arr_name = ""
            if all_arrangements and 0 <= arrangement_index < len(all_arrangements):
                arr_name = all_arrangements[arrangement_index].get("name", "")
            # PSARC saves typically don't ship `arrangements` (only sloppak
            # / full-snapshot saves do), so fall back to reading the
            # source XML's <arrangement> tag. Without this, bass charts
            # were classified as guitar (max_string=5 instead of 3) and
            # notes on string 4/5 slipped through.
            if not arr_name:
                xml_files = session.get("xml_files") or []
                if 0 <= arrangement_index < len(xml_files):
                    try:
                        _xroot = ET.parse(xml_files[arrangement_index]).getroot()
                        _atag = _xroot.find("arrangement")
                        if _atag is not None and _atag.text:
                            arr_name = _atag.text.strip()
                    except (ET.ParseError, OSError):
                        pass
            is_bass = "bass" in arr_name.lower()
            std_len = 4 if is_bass else 6
            # Truncation has to reverse the AddStringCmd shift so the
            # remaining notes stay on the right strings after the dropped
            # extensions are gone. Mirror AddStringCmd's add-at-low (and
            # 5→6-bass add-at-high) convention:
            #   - guitar: extensions are always at the low end → drop
            #     `extra_low` prefix and shift remaining notes by that
            #     amount
            #   - 5-string bass: low-B extension at index 0 → drop 1 from
            #     the low end
            #   - 6-string bass: BOTH low-B (idx 0) and high-C (last idx)
            #     extensions → drop one from each end
            # The frontend ships the explicit `_extendedStrings` counter
            # so we know how many extras to peel even when tuning.length
            # alone is ambiguous (the bass-padded-vs-real-6 case).
            # Prefer the explicit `_extendedStrings` counter — it
            # disambiguates the bass case where tuning.length==6
            # could mean either a standard 4-string bass (RS-XML
            # padding) or a genuine 6-string. Without it, a save
            # where the user is on a *standard* bass tab while
            # another arrangement is extended would catastrophically
            # drop low-E notes thinking the bass was extended too.
            try:
                ext_strings = int(data.get("_extendedStrings", 0) or 0)
            except (TypeError, ValueError):
                ext_strings = 0
            if ext_strings > 0:
                cur_len = std_len + ext_strings
            else:
                # No extensions on this arrangement → nothing to peel.
                cur_len = std_len
            if is_bass:
                # 5-string bass: low-B at idx 0. 6-string bass: low-B + high-C.
                extra_low = 1 if cur_len >= 5 else 0
                extra_high = 1 if cur_len >= 6 else 0
            else:
                extra_low = max(0, cur_len - std_len)
                extra_high = 0
            kept_min = extra_low
            kept_max = cur_len - 1 - extra_high if cur_len > 0 else std_len - 1

            def _shift_note(n):
                # Drop notes whose `string` isn't numeric — same
                # defensive coercion as `_arrangement_string_count` and
                # `_is_extended_range`. A `string: null` from an older
                # client / corrupted save shouldn't 500 the save.
                s = _safe_string_index(n.get("string", 0))
                if s is None or s < kept_min or s > kept_max:
                    return None
                new_n = dict(n)
                new_n["string"] = s - extra_low
                return new_n

            new_notes = []
            for n in notes:
                shifted = _shift_note(n)
                if shifted is not None:
                    new_notes.append(shifted)
            notes = new_notes

            trimmed_chords = []
            for ch in chords:
                kept_cns = []
                for cn in ch.get("notes", []) or []:
                    shifted = _shift_note(cn)
                    if shifted is not None:
                        kept_cns.append(shifted)
                if kept_cns:
                    new_ch = dict(ch)
                    new_ch["notes"] = kept_cns
                    trimmed_chords.append(new_ch)
            chords = trimmed_chords

            # Chord templates: slice off the matching low / high columns.
            for ct in chord_templates:
                for key in ("frets", "fingers"):
                    arr_v = ct.get(key)
                    if isinstance(arr_v, list) and len(arr_v) > std_len:
                        if extra_high:
                            ct[key] = arr_v[extra_low: len(arr_v) - extra_high]
                        else:
                            ct[key] = arr_v[extra_low:]
                        # Pad or clamp to exactly std_len so the XML
                        # builder's max_i calc stays stable.
                        if len(ct[key]) < std_len:
                            ct[key] = ct[key] + [-1] * (std_len - len(ct[key]))
                        elif len(ct[key]) > std_len:
                            ct[key] = ct[key][:std_len]

        def _save_psarc():
            xml_files = session["xml_files"]
            if arrangement_index >= len(xml_files):
                raise RuntimeError("Invalid arrangement index")

            xml_path = xml_files[arrangement_index]

            # Read existing XML for metadata we want to preserve
            tree = ET.parse(xml_path)
            old_root = tree.getroot()

            # Build new XML. When force_psarc_truncate fires, cap the
            # tuning width so a previously-saved extended-range XML
            # can't sneak `string6+` into a stock-RS-targeted PSARC.
            _force_max = None
            if force_psarc_truncate:
                _force_max = 4 if is_bass else 6
            xml_str = _build_arrangement_xml(
                old_root, notes, chords, chord_templates, beats, sections, metadata,
                force_max_strings=_force_max,
            )

            # Write XML
            Path(xml_path).write_text(xml_str, encoding="utf-8")

            # Try to compile XML -> SNG
            _compile_sng(xml_path)

            # Pack back to PSARC
            dlc_dir = get_dlc_dir()
            filename = session["filename"]
            output_path = dlc_dir / filename

            # Backup original
            backup = dlc_dir / (filename + ".bak")
            if output_path.exists() and not backup.exists():
                shutil.copy2(output_path, backup)

            pack_psarc(session["dir"], str(output_path))
            return str(output_path)

        def _save_sloppak():
            sloppak_state = session.get("sloppak_state") or {}
            manifest = dict(sloppak_state.get("manifest") or {})
            sloppak_form = sloppak_state.get("form") or "zip"
            source_dir = Path(session["dir"]).resolve()
            dlc_dir = get_dlc_dir()
            if not dlc_dir:
                raise RuntimeError("DLC folder not configured")
            filename = session["filename"]
            output_path = (dlc_dir / filename).resolve()

            # Build the wire JSON for one arrangement, preserving anchors,
            # handshapes, and phrases from the loaded session (the editor
            # UI doesn't expose them yet — pass them through verbatim).
            def _build_wire(arr_dict, is_first):
                wire = _arr_dict_to_wire(
                    arr_dict.get("name", "arr"),
                    arr_dict.get("tuning", [0]*6),
                    int(arr_dict.get("capo", 0)),
                    arr_dict.get("notes", []),
                    arr_dict.get("chords", []),
                    arr_dict.get("chord_templates", []),
                )
                wire["anchors"] = list(arr_dict.get("anchors") or [])
                wire["handshapes"] = list(arr_dict.get("handshapes") or [])
                ph = arr_dict.get("phrases")
                if ph:
                    wire["phrases"] = list(ph)
                if is_first:
                    wire["beats"] = [
                        {"time": round(float(b.get("time", 0)), 3),
                         "measure": int(b.get("measure", -1))}
                        for b in beats
                    ]
                    wire["sections"] = [
                        {"name": s.get("name", ""),
                         "number": int(s.get("number", 0)),
                         "time": round(float(s.get("start_time", 0)), 3)}
                        for s in sections
                    ]
                return wire

            # Determine the arrangement set to write. If `arrangements` was
            # provided, it's the authoritative full snapshot (handles adds,
            # removes, reorders). Otherwise we update only the single
            # arrangement at arrangement_index from notes/chords/templates.
            old_entries = list(manifest.get("arrangements", []) or [])

            if all_arrangements is None:
                if arrangement_index >= len(old_entries):
                    raise RuntimeError("Invalid arrangement index")
                # Build a synthetic edited dict using the old entry's
                # tuning/capo since the legacy save body doesn't carry them.
                old_entry = old_entries[arrangement_index]
                # Load anchors/handshapes/phrases from the existing arrangement
                # JSON on disk so they are preserved verbatim — the editor UI
                # doesn't expose them, so the save body never includes them.
                _preserved: dict = {}
                _old_rel = old_entry.get("file")
                if _old_rel:
                    _old_path = (source_dir / _old_rel).resolve()
                    # Constrain reads to source_dir/arrangements — defends against
                    # `..` traversal in a malformed or untrusted manifest.yaml.
                    _arr_dir_resolved = (source_dir / "arrangements").resolve()
                    _old_path_ok = False
                    try:
                        # Called only for the side-effect: raises ValueError
                        # if _old_path escapes _arr_dir_resolved (path traversal).
                        _old_path.relative_to(_arr_dir_resolved)
                        _old_path_ok = True
                    except ValueError:
                        pass
                    if _old_path_ok:
                        try:
                            _existing = json.loads(_old_path.read_text(encoding="utf-8"))
                            for _k in ("anchors", "handshapes", "phrases"):
                                if _k in _existing:
                                    _preserved[_k] = _existing[_k]
                        except (OSError, json.JSONDecodeError):
                            pass
                edited_dict = {
                    "name": old_entry.get("name", ""),
                    "tuning": old_entry.get("tuning", [0]*6),
                    "capo": int(old_entry.get("capo", 0)),
                    "notes": notes,
                    "chords": chords,
                    "chord_templates": chord_templates,
                    "anchors": _preserved.get("anchors", []),
                    "handshapes": _preserved.get("handshapes", []),
                    "phrases": _preserved.get("phrases"),
                }
                merged_arrangements = []
                for i, entry in enumerate(old_entries):
                    wire = _build_wire(edited_dict, i == 0) if i == arrangement_index else None
                    merged_arrangements.append({"entry": entry, "wire": wire})
            else:
                # Full snapshot path — used when arrangements were added/
                # removed or for safety on every save.
                used_ids: set = set()
                merged_arrangements = []
                for i, ad in enumerate(all_arrangements):
                    raw_id = ad.get("id") or ""
                    if raw_id and raw_id not in used_ids:
                        aid = raw_id
                    else:
                        aid = _arrangement_id(ad.get("name", "arr"), used_ids)
                    used_ids.add(aid)
                    wire = _build_wire(ad, i == 0)
                    merged_arrangements.append({
                        "entry": {
                            "id": aid,
                            "name": ad.get("name", "arr"),
                            "file": f"arrangements/{aid}.json",
                            "tuning": list(ad.get("tuning", [0]*6)),
                            "capo": int(ad.get("capo", 0)),
                        },
                        "wire": wire,
                    })

            # Write/update arrangement JSON files inside source_dir/arrangements
            arr_dir = (source_dir / "arrangements").resolve()
            arr_dir.mkdir(parents=True, exist_ok=True)
            new_manifest_arrangements = []
            kept_paths: set[Path] = set()
            for item in merged_arrangements:
                entry = item["entry"]
                wire = item["wire"]
                if wire is not None:
                    rel = entry.get("file") or f"arrangements/{entry.get('id', 'arr')}.json"
                    arr_path = (source_dir / rel).resolve()
                    # Constrain writes to the arrangements/ subdir — defends
                    # against `..` traversal in a malformed/buggy snapshot.
                    try:
                        arr_path.relative_to(arr_dir)
                    except ValueError:
                        raise RuntimeError(f"Arrangement path escapes sandbox: {rel}")
                    arr_path.parent.mkdir(parents=True, exist_ok=True)
                    arr_path.write_text(
                        json.dumps(wire, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    entry = dict(entry)
                    entry["file"] = rel
                rel_kept = entry.get("file")
                if rel_kept:
                    kept_paths.add((source_dir / rel_kept).resolve())
                new_manifest_arrangements.append(entry)
            manifest["arrangements"] = new_manifest_arrangements

            # Drop orphaned arrangement JSONs (e.g. after a remove).
            for f in arr_dir.glob("*.json"):
                if f.resolve() not in kept_paths:
                    try:
                        f.unlink()
                    except OSError:
                        pass

            # Apply edited top-level metadata (title/artist/album/year only —
            # don't let the editor overwrite stems/lyrics/cover paths).
            if metadata:
                for k in ("title", "artist", "album"):
                    if metadata.get(k) is not None:
                        manifest[k] = metadata[k]
                if metadata.get("year") is not None:
                    try:
                        manifest["year"] = int(metadata["year"])
                    except (TypeError, ValueError):
                        pass

            # Write manifest.yaml back into the source dir
            (source_dir / "manifest.yaml").write_text(
                yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

            # Directory-form sloppak: source_dir IS the sloppak — we've already
            # rewritten everything in place. Don't try to zip on top of it.
            if sloppak_form == "dir":
                return str(output_path)

            # Zip-form: back up the original and re-zip the source dir.
            if output_path.exists() and output_path.is_file():
                backup = dlc_dir / (filename + ".bak")
                if not backup.exists():
                    shutil.copy2(output_path, backup)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_zip = output_path.with_suffix(output_path.suffix + ".tmp")
            with zipfile.ZipFile(str(tmp_zip), "w", zipfile.ZIP_DEFLATED) as zf:
                for f in source_dir.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(source_dir).as_posix())
            tmp_zip.replace(output_path)
            return str(output_path)

        try:
            if session.get("format") == "sloppak":
                output = await asyncio.get_event_loop().run_in_executor(None, _save_sloppak)
            else:
                output = await asyncio.get_event_loop().run_in_executor(None, _save_psarc)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        return {"success": True, "path": output}

    # ── Save edited PSARC as Sloppak ──────────────────────────────────────
    #
    # When the user added extra strings (7/8-string guitar or 5/6-string
    # bass) to a PSARC-sourced edit, the regular PSARC save path can't
    # carry the extra strings — stock Rocksmith's SNG binary is hard-locked
    # to 6/4. This endpoint writes a new `.sloppak` next to the original
    # PSARC and updates the session so subsequent saves go through the
    # native sloppak path. The PSARC stays on disk untouched.

    @app.post("/api/plugins/editor/save_as_sloppak")
    async def save_as_sloppak(data: dict):
        session_id = data.get("session_id", "")
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "No active session"}, 400)
        if session.get("format") != "psarc":
            return JSONResponse(
                {"error": "save_as_sloppak only applies to PSARC-sourced sessions"},
                400,
            )
        session["last_touched"] = time.time()

        arrangements_data = data.get("arrangements") or []
        if not arrangements_data:
            return JSONResponse({"error": "arrangements required"}, 400)
        beats = data.get("beats", [])
        sections = data.get("sections", [])
        # Merge session metadata (loaded from the source PSARC: album,
        # year, etc.) with anything the frontend sent (title/artist that
        # the user may have edited mid-session). The frontend currently
        # only ships `{title, artist}`, so without this merge `album` and
        # `year` would be silently dropped when packaging the .sloppak.
        meta = dict(session.get("metadata") or {})
        meta.update(data.get("metadata") or {})

        audio_file = session.get("audio_file") or ""
        if not audio_file or not Path(audio_file).exists():
            return JSONResponse({"error": "session has no audio file"}, 400)

        dlc_dir = get_dlc_dir()
        if not dlc_dir:
            return JSONResponse({"error": "DLC folder not configured"}, 500)

        source_filename = session["filename"]
        source_path = (dlc_dir / source_filename).resolve()
        try:
            source_path.relative_to(dlc_dir.resolve())
        except ValueError:
            return JSONResponse({"error": "forbidden"}, 403)

        # Output sits next to the source PSARC, sharing its stem so the
        # library shows both `MySong_p.psarc` and `MySong_p.sloppak`.
        # Keep any subdirectory prefix from `filename` (the picker
        # supports nested layouts like `Artist/Song_p.psarc`); using
        # just the bare stem here would put the sloppak in the right
        # place on disk but `resolve_source_dir(new_filename, ...)`
        # downstream would later look for it at the DLC root.
        source_relpath = Path(source_filename)
        new_filename = str(source_relpath.with_suffix(".sloppak").as_posix())
        output_path = source_path.with_suffix(".sloppak")
        # Refuse to write the zip on top of an authoring-form sloppak
        # directory at the same path — the picker supports `.sloppak/`
        # directories, and `_write_sloppak_pak` would fail trying to
        # replace it. Better a clear 409 than a half-written conflict.
        if output_path.exists() and output_path.is_dir():
            return JSONResponse(
                {"error": (
                    f"A sloppak directory already exists at "
                    f"{new_filename}. Remove or rename it before "
                    "converting the PSARC."
                )},
                409,
            )

        def _do_save():
            return _write_sloppak_pak(
                audio_file=audio_file,
                art_path="",  # PSARC sessions don't extract cover to disk yet
                arrangements_data=arrangements_data,
                beats=beats,
                sections=sections,
                meta=meta,
                output_path=output_path,
            )

        def _do_save_and_repoint():
            written = _do_save()
            # Re-extract the just-written sloppak into a fresh working
            # directory so the next /save call has a real sloppak source
            # tree (`source_dir/arrangements/*.json`, `manifest.yaml`,
            # stems) to edit. Without this, `_save_sloppak` would run
            # against the PSARC unpacked dir with no manifest and emit a
            # broken .sloppak on the user's next click of Save.
            new_source_dir = sloppak_mod.resolve_source_dir(
                new_filename, dlc_dir, SLOPPAK_CACHE,
            )
            new_manifest = sloppak_mod.load_manifest(Path(written))
            return written, new_source_dir, new_manifest

        try:
            written, new_source_dir, new_manifest = (
                await asyncio.get_event_loop().run_in_executor(None, _do_save_and_repoint)
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        # Switch session into sloppak mode pointing at the new sloppak's
        # unpacked cache dir. The old PSARC working dir is unreachable
        # from the session dict after we repoint `session["dir"]`, so
        # delete it now — without this, every PSARC→Sloppak conversion
        # leaks a temp directory full of unpacked SNG/WEM/DDS bytes.
        old_psarc_dir = session.get("dir")
        session["filename"] = new_filename
        session["format"] = "sloppak"
        session["dir"] = str(new_source_dir)
        session["sloppak_state"] = {"manifest": new_manifest, "form": "zip"}
        if old_psarc_dir and old_psarc_dir != str(new_source_dir):
            shutil.rmtree(old_psarc_dir, ignore_errors=True)

        return {
            "success": True,
            "path": written,
            "filename": new_filename,
            "format": "sloppak",
        }

    # ── Upload album art ───────────────────────────────────────────────

    @app.post("/api/plugins/editor/upload-art")
    async def upload_art(file: UploadFile = File(...)):
        art_id = Path(file.filename).stem.replace(" ", "_")
        ext = Path(file.filename).suffix or ".png"
        dest = STORAGE_DIR / f"editor_art_{art_id}{ext}"
        content = await file.read()
        dest.write_bytes(content)
        return {"art_path": str(dest)}

    # ── Upload audio file ──────────────────────────────────────────────

    @app.post("/api/plugins/editor/upload-audio")
    async def upload_audio(file: UploadFile = File(...)):
        audio_id = Path(file.filename).stem.replace(" ", "_")
        ext = Path(file.filename).suffix or ".mp3"
        dest = STORAGE_DIR / f"editor_audio_{audio_id}{ext}"
        content = await file.read()
        dest.write_bytes(content)
        return {"audio_url": f"{STORAGE_URL}/editor_audio_{audio_id}{ext}"}

    # ── Download audio from YouTube ──────────────────────────────────

    @app.post("/api/plugins/editor/youtube-audio")
    async def youtube_audio(data: dict):
        url = data.get("url", "").strip()
        if not url:
            return JSONResponse({"error": "No URL provided"}, 400)

        def _download():
            tmp = tempfile.mkdtemp(prefix="slopsmith_yt_")
            out_template = os.path.join(tmp, "audio.%(ext)s")
            try:
                import yt_dlp
                opts = {
                    "format": "bestaudio/best",
                    "outtmpl": out_template,
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                    "quiet": True,
                    "no_warnings": True,
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    title = info.get("title", "audio")

                # Find the output file
                for f in Path(tmp).iterdir():
                    if f.suffix in (".mp3", ".m4a", ".ogg", ".wav"):
                        audio_id = re.sub(r"[^a-zA-Z0-9_-]", "_", title)[:60]
                        ext = f.suffix
                        dest = STORAGE_DIR / f"editor_audio_{audio_id}{ext}"
                        shutil.copy2(f, dest)
                        shutil.rmtree(tmp, ignore_errors=True)
                        return {
                            "audio_url": f"{STORAGE_URL}/editor_audio_{audio_id}{ext}",
                            "title": title,
                        }

                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError("No audio file produced")
            except Exception as e:
                shutil.rmtree(tmp, ignore_errors=True)
                raise

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _download
            )
            return result
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)

    # ── Replace audio on a loaded session ────────────────────────────

    @app.post("/api/plugins/editor/replace-audio")
    async def replace_audio(data: dict):
        """Swap the audio track for a loaded session.

        Behavior by session kind:

        - **dir-form sloppak**: copies the new audio into
          ``<source_dir>/stems/`` and rewrites ``manifest.yaml`` to a single
          ``"full"`` stem. ``source_dir`` IS the on-disk sloppak, so the
          change persists immediately (``persisted=True``, ``next_step="none"``).
          The wholesale stems-replacement is intentional — for multi-stem
          projects (guitar/bass/drums splits), merely swapping the "full"
          entry would leave other entries pointing at the now-stale mix.

        - **zip-form sloppak**: same writes, but ``source_dir`` is the
          unpack cache, so the on-disk ``.sloppak`` archive isn't touched
          until the user hits Save (which re-zips). Returned as
          ``persisted=False, next_step="save"`` so the UI can prompt.

        - **create-mode (fresh GP import)**: only ``session["audio_file"]``
          is updated. The next Build CDLC will produce a ``.psarc``
          referencing the new audio. ``persisted=False, next_step="build"``.

        - **loaded PSARC**: only ``session["audio_file"]`` is updated; the
          editor uses the new audio for playback, but there is no
          in-editor flow that repacks WEMs into the original ``.psarc``.
          ``persisted=False, next_step="rebuild"`` — the UI surfaces this
          as playback-only.
        """
        session_id = data.get("session_id", "")
        audio_url = (data.get("audio_url") or "").strip()
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "session not found"}, 404)
        src = _resolve_storage_url(audio_url)
        if src is None or not src.exists():
            return JSONResponse({"error": "invalid audio_url"}, 400)

        session["last_touched"] = time.time()
        session["audio_file"] = str(src)
        persisted = False
        # next_step tells the client which UI hint to show when not persisted.
        # "none"    — already on disk
        # "save"    — zip-form sloppak: cache updated, Save will re-zip
        # "build"   — create-mode: Build CDLC will produce a .psarc with the new audio
        # "rebuild" — loaded PSARC: no in-editor persist path (would need WEM repack)
        next_step = "rebuild"
        if session.get("create_mode"):
            next_step = "build"

        if session.get("format") == "sloppak" and session.get("sloppak_state"):
            sloppak_form = session["sloppak_state"].get("form") or "zip"
            try:
                source_dir = Path(session["dir"]).resolve()
                stems_dir = source_dir / "stems"
                stems_dir.mkdir(parents=True, exist_ok=True)
                safe_stem = re.sub(r"[^a-zA-Z0-9_-]", "_", src.stem)[:60] or "full"
                dest = (stems_dir / f"{safe_stem}{src.suffix}").resolve()
                # Path traversal guard — mirrors _safe_stem_path.
                try:
                    dest.relative_to(source_dir)
                except ValueError:
                    return JSONResponse({"error": "stem path escapes session dir"}, 400)
                shutil.copy2(src, dest)

                manifest = dict(session["sloppak_state"].get("manifest") or {})
                rel = f"stems/{dest.name}"
                manifest["stems"] = [{"id": "full", "file": rel}]
                (source_dir / "manifest.yaml").write_text(
                    yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
                session["sloppak_state"]["manifest"] = manifest
                # Only dir-form sloppaks are persisted: zip-form's source_dir is
                # the unpack cache, so the on-disk .sloppak archive isn't touched
                # until the user hits Save (which re-zips). Be honest about that
                # to the UI so the user knows whether further action is needed.
                if sloppak_form == "dir":
                    persisted = True
                    next_step = "none"
                else:
                    next_step = "save"
            except Exception as e:
                print(f"[Editor] replace-audio sloppak persist failed: {e}")
                return JSONResponse({"error": f"persist failed: {e}"}, 500)

        return {"audio_url": audio_url, "persisted": persisted, "next_step": next_step}

    # ── Import Guitar Pro file ───────────────────────────────────────

    @app.post("/api/plugins/editor/import-gp")
    async def import_gp(file: UploadFile = File(...)):
        """Upload a GP file and return track listing."""
        from lib.gp2rs import list_tracks

        tmp = tempfile.mkdtemp(prefix="slopsmith_gp_")
        gp_path = os.path.join(tmp, file.filename)
        content = await file.read()
        Path(gp_path).write_bytes(content)

        def _list():
            return list_tracks(gp_path)

        try:
            tracks = await asyncio.get_event_loop().run_in_executor(
                None, _list
            )
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return JSONResponse({"error": f"Failed to parse GP file: {e}"}, 500)

        return {"gp_path": gp_path, "tracks": tracks}

    # ── MIDI import: list tracks ─────────────────────────────────────

    @app.post("/api/plugins/editor/import-midi")
    async def import_midi(file: UploadFile = File(...)):
        """Upload a MIDI file and return track listing."""
        from lib.midi_import import list_midi_tracks

        # Validate extension — the browser accept filter is advisory only.
        orig_suffix = Path(file.filename or "").suffix.lower()
        if orig_suffix not in (".mid", ".midi"):
            return JSONResponse(
                {"error": "Only .mid/.midi files are accepted"}, 400
            )

        # Opportunistic TTL cleanup: remove any slopsmith_midi_* sandbox dirs
        # older than 30 minutes so unclaimed uploads (cancelled modals, etc.)
        # don't accumulate indefinitely on the server.
        _ttl_secs = 30 * 60
        tmp_root = Path(tempfile.gettempdir())
        for _stale in tmp_root.glob("slopsmith_midi_*"):
            try:
                if _stale.is_dir():
                    age = time.time() - _stale.stat().st_mtime
                    if age > _ttl_secs:
                        shutil.rmtree(_stale, ignore_errors=True)
            except OSError:
                pass

        suffix = orig_suffix or ".mid"
        tmp = tempfile.mkdtemp(prefix="slopsmith_midi_")
        midi_path = os.path.join(tmp, "upload" + suffix)
        content = await file.read()
        Path(midi_path).write_bytes(content)

        def _list():
            return list_midi_tracks(midi_path)

        try:
            tracks = await asyncio.get_event_loop().run_in_executor(None, _list)
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return JSONResponse({"error": f"Failed to parse MIDI file: {e}"}, 500)

        return {"midi_path": midi_path, "tracks": tracks}

    # ── MIDI import: convert a track to a Keys arrangement ────────────

    @app.post("/api/plugins/editor/import-keys-midi")
    async def import_keys_midi(data: dict):
        """Convert a MIDI track into a Keys arrangement (editor-ready dict)."""
        from lib.midi_import import convert_midi_track_to_keys_wire

        midi_path_raw = data.get("midi_path", "")
        track_index = data.get("track_index")
        try:
            audio_offset = float(data.get("audio_offset", 0.0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "audio_offset must be a number"}, 400)
        # Optional: when the picker entry came from a format-0 channel
        # split, this isolates the chosen channel out of the merged track.
        channel_filter_raw = data.get("channel_filter")
        channel_filter: int | None
        if channel_filter_raw is None or channel_filter_raw == "":
            channel_filter = None
        else:
            try:
                channel_filter = int(channel_filter_raw)
            except (TypeError, ValueError):
                channel_filter = None

        validated = _validate_editor_upload_path(midi_path_raw, "slopsmith_midi_")
        if not validated:
            return JSONResponse({"error": "MIDI file not found"}, 400)
        midi_path = str(validated)
        if track_index is None:
            return JSONResponse({"error": "track_index required"}, 400)
        try:
            track_index = int(track_index)
        except (TypeError, ValueError):
            return JSONResponse({"error": "track_index must be an integer"}, 400)

        def _convert():
            wire = convert_midi_track_to_keys_wire(
                midi_path, track_index, audio_offset, "Keys",
                channel_filter=channel_filter,
            )
            # Convert wire → editor's long-named shape so the frontend can
            # consume it identically to import-keys output.
            arr_data = {
                "name": wire["name"],
                "tuning": wire["tuning"],
                "capo": wire["capo"],
                "notes": [],
                "chords": [],
                "chord_templates": [],
            }
            for n in wire["notes"]:
                arr_data["notes"].append({
                    "time": n["t"],
                    "string": n["s"],
                    "fret": n["f"],
                    "sustain": n["sus"],
                    "techniques": {
                        "bend": n.get("bn", 0),
                        "slide_to": n.get("sl", -1),
                        "slide_unpitch_to": n.get("slu", -1),
                        "hammer_on": n.get("ho", False),
                        "pull_off": n.get("po", False),
                        "harmonic": n.get("hm", False),
                        "harmonic_pinch": n.get("hp", False),
                        "palm_mute": n.get("pm", False),
                        "mute": n.get("mt", False),
                        "tremolo": n.get("tr", False),
                        "accent": n.get("ac", False),
                        "tap": n.get("tp", False),
                        "link_next": False,
                    },
                })
            return arr_data

        try:
            arr_data = await asyncio.get_event_loop().run_in_executor(None, _convert)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        # Clean up the MIDI temp dir now that conversion is complete — the
        # client no longer needs to reference midi_path after this response.
        try:
            shutil.rmtree(Path(midi_path).parent)
        except OSError as _cleanup_err:
            import warnings
            warnings.warn(f"Could not clean up MIDI temp dir: {_cleanup_err}")

        return {"arrangement": arr_data}

    # ── Convert GP tracks to arrangement and open in editor ──────────

    @app.post("/api/plugins/editor/convert-gp")
    async def convert_gp(data: dict):
        """Convert selected GP tracks to Rocksmith arrangements."""
        from lib.gp2rs import convert_file, auto_select_tracks
        from lib.song import parse_arrangement, Song, Beat, Section

        gp_path = data.get("gp_path", "")
        audio_url = data.get("audio_url", "")
        audio_path = data.get("audio_path", "")  # local path in container
        track_indices = data.get("track_indices")  # None = auto-select
        arrangement_names = data.get("arrangement_names")  # {idx: name}
        title = data.get("title", "")
        artist = data.get("artist", "")
        album = data.get("album", "")
        year = data.get("year", "")

        validated_gp = _validate_editor_upload_path(gp_path, "slopsmith_gp_")
        if not validated_gp:
            return JSONResponse({"error": "GP file not found"}, 400)
        gp_path = str(validated_gp)

        def _convert():
            tmp = tempfile.mkdtemp(prefix="slopsmith_editor_create_")

            # Auto-select tracks if none specified
            names_map = None
            if track_indices is None:
                indices, names_map = auto_select_tracks(gp_path)
            else:
                indices = track_indices
                if arrangement_names:
                    names_map = {int(k): v for k, v in arrangement_names.items()}

            # Convert GP to XMLs
            xml_paths = convert_file(
                gp_path, tmp,
                track_indices=indices,
                arrangement_names=names_map,
            )

            # Parse the generated XMLs into a Song object
            song = Song()
            song.title = title
            song.artist = artist
            song.album = album
            if year:
                try:
                    song.year = int(year)
                except ValueError:
                    pass

            for xml_path in xml_paths:
                arr = parse_arrangement(xml_path)
                song.arrangements.append(arr)

            # Get beats and sections from first XML
            if xml_paths:
                import xml.etree.ElementTree as XET
                tree = XET.parse(xml_paths[0])
                root = tree.getroot()

                el = root.find("songLength")
                if el is not None and el.text:
                    song.song_length = float(el.text)

                container = root.find("ebeats")
                if container is not None:
                    for eb in container.findall("ebeat"):
                        t = float(eb.get("time", "0"))
                        m = int(eb.get("measure", "-1"))
                        song.beats.append(Beat(time=t, measure=m))

                container = root.find("sections")
                if container is not None:
                    for s in container.findall("section"):
                        song.sections.append(Section(
                            name=s.get("name", ""),
                            number=int(s.get("number", "1")),
                            start_time=float(s.get("startTime", "0")),
                        ))

            # If we have a local audio file path, copy to static
            nonlocal audio_url
            if audio_path and Path(audio_path).exists():
                audio_id = re.sub(r"[^a-zA-Z0-9_-]", "_", title or "gp_import")[:60]
                ext = Path(audio_path).suffix
                dest = STORAGE_DIR / f"editor_audio_{audio_id}{ext}"
                shutil.copy2(audio_path, dest)
                audio_url = f"{STORAGE_URL}/editor_audio_{audio_id}{ext}"

            result = _song_to_dict(song, audio_url)
            return result, tmp, xml_paths

        try:
            result, session_dir, xml_files = (
                await asyncio.get_event_loop().run_in_executor(None, _convert)
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        session_id = f"create_{re.sub(r'[^a-z0-9]', '', (title or 'new').lower())[:30]}"
        if session_id in sessions:
            old = sessions[session_id]
            shutil.rmtree(old["dir"], ignore_errors=True)

        sessions[session_id] = {
            "dir": session_dir,
            "audio_file": None,
            "filename": "",
            "xml_files": xml_files,
            "create_mode": True,
            "gp_path": gp_path,
            "metadata": {
                "title": title, "artist": artist,
                "album": album, "year": year,
            },
            "last_touched": time.time(),
        }
        result["session_id"] = session_id
        result["create_mode"] = True
        return result

    # ── Import piano/keyboard tracks from a GP file ────────────────────

    @app.post("/api/plugins/editor/import-keys")
    async def import_keys_track(data: dict):
        """Import a piano/keyboard track from a GP file and return as an arrangement."""
        from lib.gp2rs import (
            list_tracks, convert_piano_track, is_piano_track,
            _build_tempo_map, _tick_to_seconds, GP_TICKS_PER_QUARTER,
        )
        from lib.song import parse_arrangement, Song, Beat, Section
        import guitarpro

        gp_path_raw = data.get("gp_path", "")
        track_index = data.get("track_index")
        try:
            audio_offset = float(data.get("audio_offset", 0.0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "audio_offset must be a number"}, 400)

        validated = _validate_editor_upload_path(gp_path_raw, "slopsmith_gp_")
        if not validated:
            return JSONResponse({"error": "GP file not found"}, 400)
        gp_path = str(validated)
        if track_index is None:
            return JSONResponse({"error": "track_index required"}, 400)
        try:
            track_index = int(track_index)
        except (TypeError, ValueError):
            return JSONResponse({"error": "track_index must be an integer"}, 400)

        def _convert():
            song = guitarpro.parse(gp_path)
            track = song.tracks[track_index]

            if not is_piano_track(track):
                # Still allow manual override — user picked this track
                pass

            xml_str = convert_piano_track(
                song, track_index, audio_offset, "Keys"
            )

            # Write to temp file so we can parse it back
            tmp = tempfile.mkdtemp(prefix="slopsmith_keys_")
            xml_path = os.path.join(tmp, "Keys.xml")
            Path(xml_path).write_text(xml_str, encoding="utf-8")

            arr = parse_arrangement(xml_path)
            arr_data = {
                "name": "Keys",
                "tuning": arr.tuning,
                "capo": arr.capo,
                "notes": [],
                "chords": [],
                "chord_templates": [],
            }

            for n in arr.notes:
                arr_data["notes"].append({
                    "time": round(n.time, 3),
                    "string": n.string,
                    "fret": n.fret,
                    "sustain": round(n.sustain, 3),
                    "techniques": {
                        "bend": n.bend,
                        "slide_to": n.slide_to,
                        "slide_unpitch_to": n.slide_unpitch_to,
                        "hammer_on": n.hammer_on,
                        "pull_off": n.pull_off,
                        "harmonic": n.harmonic,
                        "harmonic_pinch": n.harmonic_pinch,
                        "palm_mute": n.palm_mute,
                        "mute": n.mute,
                        "tremolo": n.tremolo,
                        "accent": n.accent,
                        "tap": n.tap,
                        "link_next": n.link_next,
                    },
                })

            for ch in arr.chords:
                chord_data = {
                    "time": round(ch.time, 3),
                    "chord_id": ch.chord_id,
                    "high_density": ch.high_density,
                    "notes": [],
                }
                for cn in ch.notes:
                    chord_data["notes"].append({
                        "time": round(cn.time, 3),
                        "string": cn.string,
                        "fret": cn.fret,
                        "sustain": round(cn.sustain, 3),
                        "techniques": {
                            "bend": cn.bend,
                            "slide_to": cn.slide_to,
                            "slide_unpitch_to": cn.slide_unpitch_to,
                            "hammer_on": cn.hammer_on,
                            "pull_off": cn.pull_off,
                            "harmonic": cn.harmonic,
                            "palm_mute": cn.palm_mute,
                            "mute": cn.mute,
                            "tremolo": cn.tremolo,
                            "accent": cn.accent,
                            "tap": cn.tap,
                            "link_next": cn.link_next,
                        },
                    })
                arr_data["chords"].append(chord_data)

            for ct in arr.chord_templates:
                arr_data["chord_templates"].append({
                    "name": ct.name,
                    "frets": ct.frets,
                    "fingers": ct.fingers,
                })

            return arr_data, tmp, xml_path

        try:
            arr_data, tmp_dir, xml_path = (
                await asyncio.get_event_loop().run_in_executor(None, _convert)
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        return {"arrangement": arr_data, "tmp_dir": tmp_dir, "xml_path": xml_path}

    # ── Import drum/percussion tracks from a GP file ─────────────────

    @app.post("/api/plugins/editor/import-drums")
    async def import_drums_track(data: dict):
        """Import a drum/percussion track from a GP file and return as an arrangement."""
        from lib.gp2rs import (
            list_tracks, convert_drum_track, is_drum_track,
            _build_tempo_map, _tick_to_seconds, GP_TICKS_PER_QUARTER,
        )
        from lib.song import parse_arrangement, Song, Beat, Section
        import guitarpro

        gp_path_raw = data.get("gp_path", "")
        track_index = data.get("track_index")
        try:
            audio_offset = float(data.get("audio_offset", 0.0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "audio_offset must be a number"}, 400)

        validated = _validate_editor_upload_path(gp_path_raw, "slopsmith_gp_")
        if not validated:
            return JSONResponse({"error": "GP file not found"}, 400)
        gp_path = str(validated)
        if track_index is None:
            return JSONResponse({"error": "track_index required"}, 400)
        try:
            track_index = int(track_index)
        except (TypeError, ValueError):
            return JSONResponse({"error": "track_index must be an integer"}, 400)

        def _convert():
            song = guitarpro.parse(gp_path)

            xml_str = convert_drum_track(
                song, track_index, audio_offset, "Drums"
            )

            # Write to temp file so we can parse it back
            tmp = tempfile.mkdtemp(prefix="slopsmith_drums_")
            xml_path = os.path.join(tmp, "Drums.xml")
            Path(xml_path).write_text(xml_str, encoding="utf-8")

            arr = parse_arrangement(xml_path)
            arr_data = {
                "name": "Drums",
                "tuning": arr.tuning,
                "capo": arr.capo,
                "notes": [],
                "chords": [],
                "chord_templates": [],
            }

            for n in arr.notes:
                arr_data["notes"].append({
                    "time": round(n.time, 3),
                    "string": n.string,
                    "fret": n.fret,
                    "sustain": round(n.sustain, 3),
                    "techniques": {
                        "bend": n.bend,
                        "slide_to": n.slide_to,
                        "slide_unpitch_to": n.slide_unpitch_to,
                        "hammer_on": n.hammer_on,
                        "pull_off": n.pull_off,
                        "harmonic": n.harmonic,
                        "harmonic_pinch": n.harmonic_pinch,
                        "palm_mute": n.palm_mute,
                        "mute": n.mute,
                        "tremolo": n.tremolo,
                        "accent": n.accent,
                        "tap": n.tap,
                        "link_next": n.link_next,
                    },
                })

            for ch in arr.chords:
                chord_data = {
                    "time": round(ch.time, 3),
                    "chord_id": ch.chord_id,
                    "high_density": ch.high_density,
                    "notes": [],
                }
                for cn in ch.notes:
                    chord_data["notes"].append({
                        "time": round(cn.time, 3),
                        "string": cn.string,
                        "fret": cn.fret,
                        "sustain": round(cn.sustain, 3),
                        "techniques": {
                            "bend": cn.bend,
                            "slide_to": cn.slide_to,
                            "slide_unpitch_to": cn.slide_unpitch_to,
                            "hammer_on": cn.hammer_on,
                            "pull_off": cn.pull_off,
                            "harmonic": cn.harmonic,
                            "palm_mute": cn.palm_mute,
                            "mute": cn.mute,
                            "tremolo": cn.tremolo,
                            "accent": cn.accent,
                            "tap": cn.tap,
                            "link_next": cn.link_next,
                        },
                    })
                arr_data["chords"].append(chord_data)

            for ct in arr.chord_templates:
                arr_data["chord_templates"].append({
                    "name": ct.name,
                    "frets": ct.frets,
                    "fingers": ct.fingers,
                })

            return arr_data, tmp, xml_path

        try:
            arr_data, tmp_dir, xml_path = (
                await asyncio.get_event_loop().run_in_executor(None, _convert)
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        return {"arrangement": arr_data, "tmp_dir": tmp_dir, "xml_path": xml_path}

    # ── Remove arrangement from session ────────────────────────────

    @app.post("/api/plugins/editor/remove-arrangement")
    async def remove_arrangement(data: dict):
        """Remove an arrangement from the current editing session."""
        session_id = data.get("session_id", "")
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "No active session"}, 400)
        session["last_touched"] = time.time()

        raw_idx = data.get("arrangement_index")
        if raw_idx is None:
            idx = -1
        else:
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                return JSONResponse({"error": "arrangement_index must be an integer"}, 400)

        # Sloppak: nothing to remove server-side until save. The frontend
        # splices its in-memory arrangements and the next save rewrites
        # the manifest + drops the orphaned arrangement JSON.
        if session.get("format") == "sloppak":
            return {"success": True, "arrangement_count": -1, "format": "sloppak"}

        xml_files = session.get("xml_files") or []
        if not (0 <= idx < len(xml_files)):
            return JSONResponse({"error": "arrangement_index out of range"}, 400)
        removed = xml_files.pop(idx)
        # Delete the XML and every sidecar that pack_psarc would
        # otherwise repack from the session dir. The CDLC layout
        # stores per-arrangement assets keyed off the XML stem:
        #   songs/arr/<stem>.xml          (this file)
        #   songs/bin/generic/<stem>.sng  (compiled chart)
        #   manifests/songs_dlc_*/<stem>.json (RS manifest)
        # Without removing the .sng + manifest, the next save would
        # repack a CDLC that still ships the "removed" arrangement.
        xml_p = Path(removed)
        stem = xml_p.stem
        session_dir = Path(session.get("dir") or "")

        try:
            xml_p.unlink(missing_ok=True)
        except Exception:
            pass

        sng_path = xml_p.parent.parent / "bin" / "generic" / f"{stem}.sng"
        try:
            sng_path.unlink(missing_ok=True)
        except Exception:
            pass

        if session_dir and session_dir.is_dir():
            for manifest_json in session_dir.rglob(f"manifests/**/{stem}.json"):
                try:
                    manifest_json.unlink(missing_ok=True)
                except Exception:
                    pass

        return {"success": True, "arrangement_count": len(xml_files)}

    # ── Add arrangement to existing session ──────────────────────────

    @app.post("/api/plugins/editor/add-arrangement")
    async def add_arrangement(data: dict):
        """Add a new arrangement (e.g. Keys) to the current editing session."""
        session_id = data.get("session_id", "")
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "No active session"}, 400)
        session["last_touched"] = time.time()

        arrangement = data.get("arrangement")
        xml_path = data.get("xml_path", "")

        if not arrangement:
            return JSONResponse({"error": "arrangement data required"}, 400)

        # Sloppak sessions don't use XML on disk — the save endpoint writes
        # arrangement JSON files when the user commits. The frontend keeps
        # the new arrangement in S.arrangements and sends the full snapshot
        # at save time.
        if session.get("format") == "sloppak":
            return {"success": True, "arrangement_count": -1, "format": "sloppak"}

        # PSARC path: persist the XML so save can use the existing flow.
        if xml_path and Path(xml_path).exists():
            # Copy XML into session dir
            dest = os.path.join(session["dir"], f"Keys_{len(session.get('xml_files', []))}.xml")
            shutil.copy2(xml_path, dest)
            if "xml_files" not in session:
                session["xml_files"] = []
            session["xml_files"].append(dest)

        return {"success": True, "arrangement_count": len(session.get("xml_files", []))}

    # ── Build CDLC from create-mode session ──────────────────────────

    @app.post("/api/plugins/editor/build")
    async def build_cdlc_endpoint(data: dict):
        """Build a CDLC from the current create-mode session.

        Writes a `.sloppak` when any arrangement uses extended-range strings
        (7/8-string guitar or 5/6-string bass) — RS2014's SNG binary format
        is hard-locked to 6/4 strings, so a regular PSARC build via RsCli
        would crash inside `ConvertInstrumental.xmlToSng`. Falls back to the
        normal PSARC build otherwise.
        """
        from lib.cdlc_builder import build_cdlc

        session_id = data.get("session_id", "")
        session = sessions.get(session_id)
        if not session or not session.get("create_mode"):
            return JSONResponse({"error": "No active create session"}, 400)
        session["last_touched"] = time.time()

        arrangements_data = data.get("arrangements", [])
        beats = data.get("beats", [])
        sections = data.get("sections", [])
        # Merge session metadata (album/year captured at convert-gp time)
        # with anything the frontend sent (the build modal currently
        # only ships {title, artist, artistName}). Without the merge,
        # extended-range sloppak builds via _write_sloppak_pak would
        # silently drop album/year fields the user typed during import.
        meta = dict(session.get("metadata") or {})
        meta.update(data.get("metadata") or {})
        audio_url = data.get("audio_url", "")
        art_path = data.get("art_path", "")

        needs_sloppak = any(_is_extended_range(a) for a in arrangements_data)

        def _build():
            # Write each arrangement's data to its corresponding XML
            xml_files = session["xml_files"]
            for i, xml_path in enumerate(xml_files):
                tree = ET.parse(xml_path)
                old_root = tree.getroot()

                if i < len(arrangements_data):
                    arr = arrangements_data[i]
                    arr_notes = arr.get("notes", [])
                    arr_chords = arr.get("chords", [])
                    arr_templates = arr.get("chord_templates", [])
                else:
                    arr_notes, arr_chords, arr_templates = [], [], []

                xml_str = _build_arrangement_xml(
                    old_root, arr_notes, arr_chords, arr_templates,
                    beats, sections, meta,
                )
                Path(xml_path).write_text(xml_str, encoding="utf-8")

            # Resolve audio file path from URL. Handles both the legacy
            # /static/* form (web Docker) and the cache /api/plugins/editor/cache/*
            # form (desktop bundles) — see _resolve_storage_url().
            resolved = _resolve_storage_url(audio_url) if audio_url else None
            audio_file = str(resolved) if resolved else ""

            if not audio_file or not Path(audio_file).exists():
                raise RuntimeError("No audio file available for build")

            # Get arrangement names from XMLs, deduplicate
            arr_names = []
            name_counts = {}
            for xp in xml_files:
                root = ET.parse(xp).getroot()
                el = root.find("arrangement")
                name = el.text if el is not None and el.text else "Lead"
                name_counts[name] = name_counts.get(name, 0) + 1
                if name_counts[name] > 1:
                    name = f"{name}{name_counts[name]}"
                arr_names.append(name)
            # Also rename in the XMLs so manifests match
            for xp, name in zip(xml_files, arr_names):
                tree = ET.parse(xp)
                el = tree.getroot().find("arrangement")
                if el is not None:
                    el.text = name
                    tree.write(xp, xml_declaration=True, encoding="unicode")

            dlc_dir = get_dlc_dir()
            title = meta.get("title", "Untitled")
            artist = meta.get("artistName") or meta.get("artist", "Unknown")
            safe_t = re.sub(r'[<>:"/\\|?*]', '_', title)
            safe_a = re.sub(r'[<>:"/\\|?*]', '_', artist)
            output = str(dlc_dir / f"{safe_t}_{safe_a}_p.psarc")

            return build_cdlc(
                xml_paths=xml_files,
                arrangement_names=arr_names,
                audio_path=audio_file,
                title=title,
                artist=artist,
                album=meta.get("albumName") or meta.get("album", ""),
                year=str(meta.get("albumYear") or meta.get("year", "")),
                output_path=output,
                album_art_path=art_path if art_path and Path(art_path).exists() else "",
            )

        def _build_sloppak_extended():
            """Build a .sloppak for extended-range charts (>6 guitar / >4 bass).

            Output filename is derived from title/artist for create-mode
            sessions (no existing source filename to preserve).
            """
            resolved = _resolve_storage_url(audio_url) if audio_url else None
            audio_file = str(resolved) if resolved else ""
            if not audio_file or not Path(audio_file).exists():
                raise RuntimeError("No audio file available for build")

            dlc_dir = get_dlc_dir()
            if not dlc_dir:
                raise RuntimeError("DLC folder not configured")

            title = meta.get("title", "Untitled")
            artist = meta.get("artistName") or meta.get("artist", "Unknown")
            safe_t = re.sub(r'[<>:"/\\|?*]', '_', title)
            safe_a = re.sub(r'[<>:"/\\|?*]', '_', artist)
            output = dlc_dir / f"{safe_t}_{safe_a}_p.sloppak"
            return _write_sloppak_pak(
                audio_file=audio_file,
                art_path=art_path if art_path and Path(art_path).exists() else "",
                arrangements_data=arrangements_data,
                beats=beats,
                sections=sections,
                meta=meta,
                output_path=output,
            )

        try:
            target = _build_sloppak_extended if needs_sloppak else _build
            output_path = await asyncio.get_event_loop().run_in_executor(
                None, target
            )
        except IsADirectoryError as e:
            # _write_sloppak_pak refused to clobber an authoring-form
            # sloppak directory at the target path. Surface as 409 so
            # the UI can prompt the user to remove/rename it.
            return JSONResponse({"error": str(e)}, 409)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"error": str(e)}, 500)

        return {
            "success": True,
            "path": output_path,
            "format": "sloppak" if needs_sloppak else "psarc",
        }

    # ── Helpers ──────────────────────────────────────────────────────────

    def _write_sloppak_pak(*, audio_file: str, art_path: str,
                          arrangements_data: list, beats: list, sections: list,
                          meta: dict, output_path: Path) -> str:
        """Stage a sloppak at `output_path` from the in-memory edit state.

        Shared between the create-mode build path (output_path derived
        from title/artist) and the save-as-sloppak path (output_path
        derived from the source PSARC filename, so the new sloppak sits
        next to the original on disk).
        """
        if not audio_file or not Path(audio_file).exists():
            raise RuntimeError("No audio file available for sloppak write")
        # Sloppak supports a packed-zip form (foo.sloppak file) and an
        # authoring directory form (foo.sloppak/ tree). Replacing a
        # directory with a zip via tmp_zip.replace(...) would raise
        # mid-operation and surface as a 500. Refuse early with a clear
        # signal so callers can convert it into a 409.
        if output_path.exists() and output_path.is_dir():
            raise IsADirectoryError(
                f"Refusing to overwrite authoring-form sloppak directory at {output_path}"
            )

        title = meta.get("title", "Untitled")
        artist = meta.get("artistName") or meta.get("artist", "Unknown")
        album = meta.get("albumName") or meta.get("album", "")
        year_raw = str(meta.get("albumYear") or meta.get("year", ""))
        ym = _YEAR_RE.search(year_raw) if year_raw else None
        year = int(ym.group(1)) if ym else 0

        staging = Path(tempfile.mkdtemp(prefix="slopsmith_sloppak_build_"))
        try:
            arr_dir = staging / "arrangements"
            arr_dir.mkdir()
            stems_dir = staging / "stems"
            stems_dir.mkdir()

            # Single combined-audio stem — the editor only carries one
            # audio source per session (PSARC load decodes the WEM to a
            # single ogg; create-mode imports one audio file).
            audio_ext = Path(audio_file).suffix.lower() or ".ogg"
            stem_filename = f"audio{audio_ext}"
            shutil.copy2(audio_file, stems_dir / stem_filename)

            used_ids: set[str] = set()
            manifest_arrangements = []
            duration = 0.0
            for b in beats:
                try:
                    duration = max(duration, float(b.get("time", 0)))
                except (TypeError, ValueError):
                    pass

            for i, ad in enumerate(arrangements_data):
                name = ad.get("name", f"Arr{i}")
                # `_arrangement_id` already inserts into `used_ids` for us.
                aid = _arrangement_id(name, used_ids)
                # Normalize tuning to the real string count so the
                # written sloppak unambiguously reflects the editor's
                # in-memory count (the RS-XML 6-slot padding does NOT
                # round-trip through sloppak — we want length 4 for a
                # real 4-string bass, length 6 for a genuine 6-string).
                real_count = _arrangement_string_count(ad)
                normalized_tuning = _normalize_tuning_to_count(
                    ad.get("tuning", [0] * 6), real_count,
                )
                wire = _arr_dict_to_wire(
                    name,
                    normalized_tuning,
                    int(ad.get("capo", 0)),
                    ad.get("notes", []),
                    ad.get("chords", []),
                    ad.get("chord_templates", []),
                )
                if i == 0:
                    wire["beats"] = [
                        {"time": round(float(b.get("time", 0)), 3),
                         "measure": int(b.get("measure", -1))}
                        for b in beats
                    ]
                    wire["sections"] = [
                        {"name": s.get("name", ""),
                         "number": int(s.get("number", 0)),
                         "time": round(float(s.get("start_time", 0)), 3)}
                        for s in sections
                    ]
                (arr_dir / f"{aid}.json").write_text(
                    json.dumps(wire, separators=(",", ":")),
                    encoding="utf-8",
                )
                manifest_arrangements.append({
                    "id": aid,
                    "name": name,
                    "file": f"arrangements/{aid}.json",
                    "tuning": normalized_tuning,
                    "capo": int(ad.get("capo", 0)),
                })

            manifest = {
                "title": title,
                "artist": artist,
                "album": album,
                "duration": round(duration, 3),
                # `id: "full"` matches the convention the editor's load
                # path and replace-audio path already use; sloppak
                # readers prefer that id when picking the default stem.
                "stems": [
                    {"id": "full", "file": f"stems/{stem_filename}"},
                ],
                "arrangements": manifest_arrangements,
            }
            if year:
                manifest["year"] = year

            if art_path and Path(art_path).exists():
                cover_ext = Path(art_path).suffix.lower() or ".jpg"
                cover_name = f"cover{cover_ext}"
                shutil.copy2(art_path, staging / cover_name)
                manifest["cover"] = cover_name

            (staging / "manifest.yaml").write_text(
                yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

            output_path.parent.mkdir(parents=True, exist_ok=True)
            # Match the existing /save paths: keep a one-time .bak when
            # we're about to overwrite an existing sloppak so the user
            # has a recovery point.
            if output_path.exists() and output_path.is_file():
                backup = output_path.with_suffix(output_path.suffix + ".bak")
                if not backup.exists():
                    shutil.copy2(output_path, backup)
            tmp_zip = output_path.with_suffix(output_path.suffix + ".tmp")
            with zipfile.ZipFile(str(tmp_zip), "w", zipfile.ZIP_DEFLATED) as zf:
                for f in staging.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(staging).as_posix())
            tmp_zip.replace(output_path)
            return str(output_path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _arr_dict_to_wire(name, tuning, capo, notes, chords, chord_templates):
        """Convert editor's long-named arrangement dict into sloppak wire format.

        Editor uses {time, string, fret, sustain, techniques: {bend, slide_to,
        ...}}; the wire format uses {t, s, f, sus, sl, bn, ho, ...}.
        """
        def _note(n):
            tech = n.get("techniques", {}) or {}
            out = {
                "t": round(float(n.get("time", 0)), 3),
                "s": int(n.get("string", 0)),
                "f": int(n.get("fret", 0)),
                "sus": round(float(n.get("sustain", 0)), 3),
                "sl": int(tech.get("slide_to", -1)),
                "slu": int(tech.get("slide_unpitch_to", -1)),
                "bn": round(float(tech.get("bend", 0) or 0), 1),
                "ho": bool(tech.get("hammer_on", False)),
                "po": bool(tech.get("pull_off", False)),
                "hm": bool(tech.get("harmonic", False)),
                "hp": bool(tech.get("harmonic_pinch", False)),
                "pm": bool(tech.get("palm_mute", False)),
                "mt": bool(tech.get("mute", False)),
                "tr": bool(tech.get("tremolo", False)),
                "ac": bool(tech.get("accent", False)),
                "tp": bool(tech.get("tap", False)),
            }
            return out

        def _note_in_chord(n):
            # Chord-member notes share the chord's time, so we omit `t`.
            d = _note(n)
            d.pop("t", None)
            return d

        wire = {
            "name": name,
            "tuning": list(tuning),
            "capo": int(capo),
            "notes": [_note(n) for n in notes],
            "chords": [
                {
                    "t": round(float(c.get("time", 0)), 3),
                    "id": int(c.get("chord_id", -1)),
                    "hd": bool(c.get("high_density", False)),
                    "notes": [_note_in_chord(cn) for cn in c.get("notes", [])],
                }
                for c in chords
            ],
            "anchors": [],
            "handshapes": [],
            "templates": [
                {
                    "name": ct.get("name", ""),
                    "fingers": list(ct.get("fingers", [-1]*6)),
                    "frets": list(ct.get("frets", [-1]*6)),
                }
                for ct in chord_templates
            ],
        }
        return wire

    def _song_to_dict(song, audio_url):
        """Convert a Song object to JSON-serializable dict."""
        result = {
            "title": song.title,
            "artist": song.artist,
            "album": song.album,
            "year": song.year,
            "duration": song.song_length,
            "offset": song.offset,
            "audio_url": audio_url,
            "beats": [
                {"time": b.time, "measure": b.measure} for b in song.beats
            ],
            "sections": [
                {
                    "name": s.name,
                    "number": s.number,
                    "start_time": s.start_time,
                }
                for s in song.sections
            ],
            "arrangements": [],
        }

        for arr in song.arrangements:
            arr_data = {
                "name": arr.name,
                "tuning": arr.tuning,
                "capo": arr.capo,
                "notes": [],
                "chords": [],
                "chord_templates": [],
            }

            for n in arr.notes:
                arr_data["notes"].append({
                    "time": round(n.time, 3),
                    "string": n.string,
                    "fret": n.fret,
                    "sustain": round(n.sustain, 3),
                    "techniques": {
                        "bend": n.bend,
                        "slide_to": n.slide_to,
                        "slide_unpitch_to": n.slide_unpitch_to,
                        "hammer_on": n.hammer_on,
                        "pull_off": n.pull_off,
                        "harmonic": n.harmonic,
                        "harmonic_pinch": n.harmonic_pinch,
                        "palm_mute": n.palm_mute,
                        "mute": n.mute,
                        "tremolo": n.tremolo,
                        "accent": n.accent,
                        "tap": n.tap,
                        "link_next": n.link_next,
                    },
                })

            for ch in arr.chords:
                chord_data = {
                    "time": round(ch.time, 3),
                    "chord_id": ch.chord_id,
                    "high_density": ch.high_density,
                    "notes": [],
                }
                for cn in ch.notes:
                    chord_data["notes"].append({
                        "time": round(cn.time, 3),
                        "string": cn.string,
                        "fret": cn.fret,
                        "sustain": round(cn.sustain, 3),
                        "techniques": {
                            "bend": cn.bend,
                            "slide_to": cn.slide_to,
                            "slide_unpitch_to": cn.slide_unpitch_to,
                            "hammer_on": cn.hammer_on,
                            "pull_off": cn.pull_off,
                            "harmonic": cn.harmonic,
                            "palm_mute": cn.palm_mute,
                            "mute": cn.mute,
                            "tremolo": cn.tremolo,
                            "accent": cn.accent,
                            "tap": cn.tap,
                            "link_next": cn.link_next,
                        },
                    })
                arr_data["chords"].append(chord_data)

            for ct in arr.chord_templates:
                arr_data["chord_templates"].append({
                    "name": ct.name,
                    "frets": ct.frets,
                    "fingers": ct.fingers,
                })

            result["arrangements"].append(arr_data)

        return result

    def _build_arrangement_xml(
        old_root, notes, chords, chord_templates, beats, sections, metadata,
        force_max_strings=None,
    ):
        """Build a Rocksmith arrangement XML from editor data.

        `force_max_strings` caps the emitted `<tuning>` width so a
        PSARC truncate save can't carry over `string6+` slots that may
        have been written by a prior extended-range save — without
        this, RsCli's SNG compiler would still crash on the saved
        PSARC even though we trimmed notes/chords/templates first.
        """
        root = ET.Element("song", version="7")

        # Friendly key aliases the editor uses in its session metadata, mapped
        # onto the RS XML tag names. Lets convert-gp's `{title, artist, album,
        # year}` payload override the original XML even though the XML uses
        # `albumName` / `albumYear` / `artistName`.
        _META_ALIASES = {
            "title": ("title",),
            "artistName": ("artistName", "artist"),
            "albumName": ("albumName", "album"),
            "albumYear": ("albumYear", "year"),
            "arrangement": ("arrangement",),
            "offset": ("offset",),
            "songLength": ("songLength",),
            "startBeat": ("startBeat",),
            "averageTempo": ("averageTempo",),
        }

        def _text(tag, fallback=""):
            for k in _META_ALIASES.get(tag, (tag,)):
                if k in metadata and metadata[k] not in (None, ""):
                    return str(metadata[k])
            el = old_root.find(tag)
            return el.text if el is not None and el.text else fallback

        # albumYear must parse as Int32 for RsCli; sanitize away any stray
        # copyright text that earlier conversions may have written into the
        # XML, and clamp non-numeric values to empty.
        def _year_text():
            raw = _text("albumYear", "")
            m = _YEAR_RE.search(raw) if raw else None
            return m.group(1) if m else ""

        ET.SubElement(root, "title").text = _text("title", "Untitled")
        ET.SubElement(root, "arrangement").text = _text("arrangement", "Lead")
        ET.SubElement(root, "offset").text = _text("offset", "0.000")
        ET.SubElement(root, "songLength").text = _text("songLength", "0.000")
        ET.SubElement(root, "startBeat").text = _text("startBeat", "0.000")
        ET.SubElement(root, "averageTempo").text = _text("averageTempo", "120")
        ET.SubElement(root, "artistName").text = _text("artistName", "Unknown")
        ET.SubElement(root, "albumName").text = _text("albumName", "")
        ET.SubElement(root, "albumYear").text = _year_text()

        # Tuning — preserve from original. RS schema names string0..string5;
        # extended-range arrangements (7/8-string guitar imported from GP)
        # carry string6/string7 too, so copy whatever the source XML had.
        old_tuning = old_root.find("tuning")
        tuning_el = ET.SubElement(root, "tuning")
        max_i = 5
        if old_tuning is not None:
            i = 6
            while old_tuning.get(f"string{i}") is not None:
                max_i = i
                i += 1
        # PSARC truncate path passes force_max_strings so a previously
        # extended-range source XML can't carry over string6+ even
        # though we trimmed notes/chords/templates. Always emit at
        # least string0..string5 — RS XML schema requires those six
        # slots regardless of role (a 4-string bass writes the upper
        # two as 0), and dropping them breaks RsCli / downstream
        # parsers that assume they exist.
        if force_max_strings is not None:
            max_i = max(5, min(max_i, force_max_strings - 1))
        for i in range(max_i + 1):
            val = "0"
            if old_tuning is not None:
                val = old_tuning.get(f"string{i}", "0")
            tuning_el.set(f"string{i}", val)

        old_capo = old_root.find("capo")
        ET.SubElement(root, "capo").text = (
            old_capo.text if old_capo is not None and old_capo.text else "0"
        )

        # Ebeats
        ebeats_el = ET.SubElement(root, "ebeats", count=str(len(beats)))
        for b in beats:
            ET.SubElement(
                ebeats_el, "ebeat",
                time=f"{b['time']:.3f}", measure=str(b["measure"]),
            )

        # Sections
        if not sections:
            sections = [{"name": "default", "number": 1, "start_time": 0.0}]
        sections_el = ET.SubElement(root, "sections", count=str(len(sections)))
        for s in sections:
            ET.SubElement(
                sections_el, "section",
                name=s["name"], number=str(s["number"]),
                startTime=f"{s['start_time']:.3f}",
            )

        # Phrases — one per section
        phrases_el = ET.SubElement(root, "phrases", count=str(len(sections)))
        for s in sections:
            ET.SubElement(
                phrases_el, "phrase",
                disparity="0", ignore="0", maxDifficulty="0",
                name=s["name"], solo="0",
            )

        phrase_iters = ET.SubElement(
            root, "phraseIterations", count=str(len(sections))
        )
        for i, s in enumerate(sections):
            ET.SubElement(
                phrase_iters, "phraseIteration",
                time=f"{s['start_time']:.3f}", phraseId=str(i),
            )

        # Chord templates
        ct_el = ET.SubElement(
            root, "chordTemplates", count=str(len(chord_templates))
        )
        # Use the max of both `frets` and `fingers` lengths so a
        # template that has a wider fingers array than frets doesn't
        # silently drop the extra `fingerN` slots on round-trip.
        # Clamp to the extended-range ceiling (string0..string7 i.e.
        # 8-string guitar) so a malformed payload can't blow up the
        # emitted XML — `force_max_strings` is set by the truncate
        # path; otherwise use 8 as a hard upper bound matching the
        # editor's MAX_LANES.
        _CT_HARD_CAP = force_max_strings if force_max_strings is not None else 8
        ct_width = max(
            6,
            max((len(ct.get("frets", [])) for ct in chord_templates), default=6),
            max((len(ct.get("fingers", [])) for ct in chord_templates), default=6),
        )
        ct_width = min(ct_width, _CT_HARD_CAP)
        for ct in chord_templates:
            attrs = {"chordName": ct.get("name", "")}
            frets = ct.get("frets", [-1] * ct_width)
            fingers = ct.get("fingers", [-1] * ct_width)
            for i in range(ct_width):
                attrs[f"fret{i}"] = str(frets[i] if i < len(frets) else -1)
                attrs[f"finger{i}"] = str(fingers[i] if i < len(fingers) else -1)
            ET.SubElement(ct_el, "chordTemplate", **attrs)

        # Single difficulty level
        levels_el = ET.SubElement(root, "levels", count="1")
        level = ET.SubElement(levels_el, "level", difficulty="0")

        # Notes
        notes_el = ET.SubElement(level, "notes", count=str(len(notes)))
        for n in notes:
            techs = n.get("techniques", {})
            attrs = {
                "time": f"{n['time']:.3f}",
                "string": str(n["string"]),
                "fret": str(n["fret"]),
                "sustain": f"{n.get('sustain', 0.0):.3f}",
                "bend": f"{techs.get('bend', 0.0):.1f}",
                "hammerOn": "1" if techs.get("hammer_on") else "0",
                "pullOff": "1" if techs.get("pull_off") else "0",
                "slideTo": str(techs.get("slide_to", -1)),
                "slideUnpitchTo": str(techs.get("slide_unpitch_to", -1)),
                "harmonic": "1" if techs.get("harmonic") else "0",
                "harmonicPinch": "1" if techs.get("harmonic_pinch") else "0",
                "palmMute": "1" if techs.get("palm_mute") else "0",
                "mute": "1" if techs.get("mute") else "0",
                "tremolo": "1" if techs.get("tremolo") else "0",
                "accent": "1" if techs.get("accent") else "0",
                "linkNext": "1" if techs.get("link_next") else "0",
                "tap": "1" if techs.get("tap") else "0",
                "ignore": "0",
            }
            ET.SubElement(notes_el, "note", **attrs)

        # Chords
        chords_el = ET.SubElement(level, "chords", count=str(len(chords)))
        for ch in chords:
            chord_el = ET.SubElement(
                chords_el, "chord",
                time=f"{ch['time']:.3f}",
                chordId=str(ch.get("chord_id", 0)),
                highDensity="1" if ch.get("high_density") else "0",
                strum="down",
            )
            for cn in ch.get("notes", []):
                techs = cn.get("techniques", {})
                ET.SubElement(
                    chord_el, "chordNote",
                    time=f"{cn['time']:.3f}",
                    string=str(cn["string"]),
                    fret=str(cn["fret"]),
                    sustain=f"{cn.get('sustain', 0.0):.3f}",
                    bend=f"{techs.get('bend', 0.0):.1f}",
                    hammerOn="1" if techs.get("hammer_on") else "0",
                    pullOff="1" if techs.get("pull_off") else "0",
                    slideTo=str(techs.get("slide_to", -1)),
                    slideUnpitchTo=str(techs.get("slide_unpitch_to", -1)),
                    harmonic="1" if techs.get("harmonic") else "0",
                    harmonicPinch="1" if techs.get("harmonic_pinch") else "0",
                    palmMute="1" if techs.get("palm_mute") else "0",
                    mute="1" if techs.get("mute") else "0",
                    tremolo="1" if techs.get("tremolo") else "0",
                    accent="1" if techs.get("accent") else "0",
                    linkNext="1" if techs.get("link_next") else "0",
                    tap="1" if techs.get("tap") else "0",
                    ignore="0",
                )

        # Auto-generate anchors from note positions
        anchors = _compute_anchors(notes, chords)
        anchors_el = ET.SubElement(level, "anchors", count=str(len(anchors)))
        for a in anchors:
            ET.SubElement(
                anchors_el, "anchor",
                time=f"{a['time']:.3f}",
                fret=str(a["fret"]),
                width=str(a.get("width", 4)),
            )

        ET.SubElement(level, "handShapes", count="0")

        # Pretty print
        xml_str = ET.tostring(root, encoding="unicode")
        dom = minidom.parseString(xml_str)
        return dom.toprettyxml(indent="  ", encoding=None)

    def _compute_anchors(notes, chords):
        """Auto-generate anchors from note fret positions."""
        all_fretted = []
        for n in notes:
            if n["fret"] > 0:
                all_fretted.append((n["time"], n["fret"]))
        for ch in chords:
            for cn in ch.get("notes", []):
                if cn["fret"] > 0:
                    all_fretted.append((cn["time"], cn["fret"]))

        all_fretted.sort(key=lambda x: x[0])

        if not all_fretted:
            return [{"time": 0.0, "fret": 1, "width": 4}]

        anchors = [{
            "time": 0.0,
            "fret": max(1, all_fretted[0][1] - 1),
            "width": 4,
        }]

        for t, fret in all_fretted:
            a = anchors[-1]
            if fret < a["fret"] or fret > a["fret"] + a["width"]:
                new_fret = max(1, fret - 1)
                if new_fret != a["fret"]:
                    anchors.append({"time": t, "fret": new_fret, "width": 4})

        return anchors

    def _compile_sng(xml_path):
        """Try to compile XML to SNG via RsCli."""
        xml_p = Path(xml_path)
        sng_dir = xml_p.parent.parent / "bin" / "generic"
        sng_path = sng_dir / (xml_p.stem + ".sng")

        if not sng_path.exists():
            # No existing SNG to replace — CDLC may use XML directly
            return

        rscli = os.environ.get("RSCLI_PATH", "")
        if not rscli or not Path(rscli).exists():
            for p in ["/opt/rscli/RsCli", "./rscli/RsCli"]:
                if Path(p).exists():
                    rscli = p
                    break

        if not rscli:
            print("[Editor] RsCli not found, skipping SNG compilation")
            return

        try:
            result = subprocess.run(
                [rscli, "xml2sng", str(xml_path), str(sng_path), "pc"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                print(f"[Editor] xml2sng failed: {result.stderr}")
        except Exception as e:
            print(f"[Editor] xml2sng error: {e}")
