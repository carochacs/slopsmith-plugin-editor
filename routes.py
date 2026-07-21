"""Arrangement Editor plugin — backend routes."""

import asyncio
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
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

# Sentinel object used to distinguish "drum_tab key absent from JSON body"
# from an explicit None (removal) or a dict (new payload).  Using an object
# rather than a string avoids a spoofing vector where a client sends the
# sentinel string value and accidentally (or maliciously) trips the no-op path.
_DRUM_TAB_ABSENT = object()

_sessions = None
_build_jobs: dict = {}  # build_id -> {status, message, path?, format?}


def setup(app, context):
    config_dir = context["config_dir"]
    get_dlc_dir = context["get_dlc_dir"]
    log = context["log"]

    from lib.song import load_song, phrase_to_wire
    from lib.psarc import unpack_psarc
    from lib.patcher import pack_psarc
    from lib.audio import find_wem_files, convert_wem
    from lib import sloppak as sloppak_mod

    _chord_analysis = context["load_sibling"]("chord_analysis")

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

    # Active editing sessions: session_id -> {dir, audio_file, filename, song_data, _version}
    sessions = {}

    global _sessions
    _sessions = sessions

    @app.on_event("startup")
    async def _start_session_cleanup():
        async def _cleanup_loop():
            TTL = 3600  # 1 hour of inactivity
            while True:
                await asyncio.sleep(300)  # check every 5 minutes
                now = time.time()
                stale = [sid for sid, s in list(sessions.items())
                         if now - s.get("last_touched", 0) > TTL]
                for sid in stale:
                    s = sessions.pop(sid, None)
                    if s and s.get("format") != "sloppak":
                        shutil.rmtree(s.get("dir", ""), ignore_errors=True)
                    if s:
                        log.info("evicted stale session %r", sid)
        asyncio.ensure_future(_cleanup_loop())

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
                    log.warning("[Editor] Audio conversion failed: %s", e)

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
            # Surface the parsed drum_tab (if any) so the editor frontend can
            # show a "drums present" indicator and the +Drums modal can offer
            # Replace vs Cancel rather than silently overwriting. getattr
            # guard: an older Slopsmith core whose LoadedSloppak predates
            # the drum_tab field would otherwise raise AttributeError and
            # 500 the whole load.
            _loaded_drum_tab = getattr(loaded, "drum_tab", None)
            if _loaded_drum_tab is not None:
                result["drum_tab"] = _loaded_drum_tab
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
            log.exception("load session failed for %r", filename)
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
            "_version": 0,
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

        expected_version = data.get("expected_version")
        if expected_version is not None:
            try:
                expected_version = int(expected_version)
            except (TypeError, ValueError):
                pass
            else:
                if expected_version != session.get("_version", 0):
                    return JSONResponse({
                        "error": "Conflict: session was modified in another tab",
                        "current_version": session.get("_version", 0),
                    }, 409)

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

        # Drum-tab payload: when the +Drums modal added a drum_tab.json on top
        # of the song, the frontend ships the dict here. The three values
        # have distinct meanings:
        #   - dict  → persist alongside the manifest under `drum_tab.json`
        #             and set `manifest['drum_tab'] = 'drum_tab.json'`.
        #   - key absent → _DRUM_TAB_ABSENT sentinel → no change;
        #             existing drum_tab passes through via the manifest,
        #             untouched. This is what the editor frontend sends
        #             unless the user imported/edited a drum tab this session.
        #   - None  → explicit removal — unlinks drum_tab.json and clears the
        #             manifest key. Supported by the API for completeness;
        #             the current editor UI has no remove-drums control.
        drum_tab_payload = data.get("drum_tab", _DRUM_TAB_ABSENT)
        if drum_tab_payload is not _DRUM_TAB_ABSENT and not isinstance(
            drum_tab_payload, (dict, type(None))
        ):
            return JSONResponse(
                {"error": "drum_tab must be a JSON object or null"},
                status_code=400,
            )
        # drum_tab.json is a sloppak-format artifact — _save_psarc() can't
        # carry it. Reject a drum_tab on a non-sloppak session rather than
        # silently dropping it, so the client doesn't get a 200 and assume
        # the drum tab persisted.
        if (
            drum_tab_payload is not _DRUM_TAB_ABSENT
            and session.get("format") != "sloppak"
        ):
            return JSONResponse(
                {"error": "drum_tab can only be saved to sloppak-format songs"},
                status_code=400,
            )
        # Schema-validate a dict payload at the request boundary, using the
        # SAME validator the sloppak loader applies on the next song load.
        # Without this a structurally-invalid drum_tab (bad version type,
        # non-list hits, etc.) could be written to disk and silently dropped
        # by the loader, leaving a manifest `drum_tab:` key pointing at an
        # unloadable file. Per-hit junk is still cleaned by the dedup pass in
        # _save_sloppak; this catches the top-level schema.
        if isinstance(drum_tab_payload, dict):
            from lib.drums import validate_drum_tab as _validate_drum_tab
            _dt_ok, _dt_reason = _validate_drum_tab(drum_tab_payload)
            if not _dt_ok:
                return JSONResponse(
                    {"error": f"invalid drum_tab: {_dt_reason}"},
                    status_code=400,
                )

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
                arr_notes = arr_dict.get("notes", [])
                arr_chords = arr_dict.get("chords", [])
                arr_tuning = arr_dict.get("tuning", [0]*6)
                arr_cts = arr_dict.get("chord_templates", [])

                # Auto-name unnamed chord templates on save
                if any(not ct.get("name") for ct in arr_cts):
                    try:
                        detect_key = _chord_analysis.detect_key
                        all_ns = list(arr_notes)
                        for ch in arr_chords:
                            for cn in ch.get("notes", []):
                                all_ns.append({"string": cn.get("string", 0), "fret": cn.get("fret", 0), "sustain": cn.get("sustain", 0)})
                        _key = detect_key(all_ns, arr_tuning)
                        arr_cts = _name_chord_templates(arr_cts, arr_notes, arr_chords, arr_tuning, _key)
                    except Exception:
                        pass

                wire = _arr_dict_to_wire(
                    arr_dict.get("name", "arr"),
                    arr_tuning,
                    int(arr_dict.get("capo", 0)),
                    arr_notes,
                    arr_chords,
                    arr_cts,
                )
                wire["anchors"] = list(arr_dict.get("anchors") or [])
                # Auto-generate handshapes when absent
                saved_hs = list(arr_dict.get("handshapes") or [])
                if not saved_hs and (arr_notes or arr_chords):
                    try:
                        groups = _group_notes_impl(arr_notes, arr_chords, [])
                        saved_hs = _groups_to_handshapes(groups, arr_cts)
                    except Exception:
                        pass
                wire["handshapes"] = saved_hs
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

            # Drum-tab persist: write/update/remove drum_tab.json alongside the
            # manifest per sloppak-spec §5.3.
            #   missing key  → _DRUM_TAB_ABSENT sentinel → no-op (leave as-is)
            #   explicit null → None               → remove drum_tab.json
            #   dict          →                    → write/replace the file
            # Non-dict/non-null payloads are rejected with a 400 early in
            # save_cdlc before this closure is reached.
            if drum_tab_payload is not _DRUM_TAB_ABSENT:
                drum_tab_path = (source_dir / "drum_tab.json").resolve()
                # Constrain writes to source_dir — defends against a malformed
                # session that escaped its sandbox.
                try:
                    drum_tab_path.relative_to(source_dir)
                except ValueError:
                    raise RuntimeError("drum_tab path escapes sandbox")
                if isinstance(drum_tab_payload, dict):
                    # Defensive dedup: drop near-equal (t, p) duplicates before
                    # writing. Local testing showed earlier corruption could
                    # quietly survive load→edit→save round-trips, with each
                    # save persisting the same junk forever. Server-side
                    # dedup breaks that cycle so the next save heals the
                    # file even if the client somehow held duplicates.
                    _hits_raw = drum_tab_payload.get("hits", [])
                    if not isinstance(_hits_raw, list):
                        import logging as _log_hits
                        _log_hits.getLogger("slopsmith.plugin.editor").warning(
                            "drum_tab.hits is not a list (got %s) — treating as empty",
                            type(_hits_raw).__name__,
                        )
                        _hits_raw = []
                    import math as _math
                    _hits_in: list = _hits_raw or []
                    _seen: set = set()
                    _deduped: list[dict] = []
                    _malformed_count: int = 0
                    _dup_count: int = 0
                    for _h in _hits_in:
                        if not isinstance(_h, dict):
                            _malformed_count += 1
                            continue
                        # Require a valid non-negative finite numeric t and a
                        # non-empty piece id — hits missing either field are
                        # malformed and would fail schema validation on next load.
                        try:
                            _t = float(_h.get("t"))  # type: ignore[arg-type]
                            _p = str(_h.get("p") or "")
                        except (TypeError, ValueError):
                            _malformed_count += 1
                            continue
                        if not _math.isfinite(_t) or _t < 0:
                            _malformed_count += 1
                            continue
                        if not _p:
                            _malformed_count += 1
                            continue
                        _t_rounded = round(_t, 3)
                        _key = (_t_rounded, _p)
                        if _key in _seen:
                            _dup_count += 1
                            continue
                        _seen.add(_key)
                        # Build a sanitized hit: coerce both t and p so
                        # drum_tab.json always stores a numeric timestamp and a
                        # string piece-id regardless of what the client sent.
                        _clean_h = dict(_h)
                        _clean_h["t"] = _t_rounded
                        _clean_h["p"] = _p
                        _deduped.append(_clean_h)
                    import logging as _logging
                    _dtlog = _logging.getLogger("slopsmith.plugin.editor")
                    if _malformed_count:
                        _dtlog.warning(
                            "drum_tab: dropped %d malformed hits during save",
                            _malformed_count,
                        )
                    if _dup_count:
                        _dtlog.warning(
                            "drum_tab: dropped %d duplicate (t, piece) hits during save",
                            _dup_count,
                        )
                    # Sort by t so drum_tab.json is always time-ordered;
                    # the frontend binary-search and drag-snap code depends
                    # on this invariant.
                    _deduped.sort(key=lambda _h2: _h2["t"])
                    # Write a shallow copy so we don't mutate the body dict
                    # parsed by FastAPI. Reassigning `drum_tab_payload` would
                    # make the whole closure treat the name as local and
                    # break the earlier `drum_tab_payload is not _DRUM_TAB_ABSENT`
                    # read (UnboundLocalError).
                    _persisted = dict(drum_tab_payload)
                    _persisted["hits"] = _deduped
                    drum_tab_path.write_text(
                        json.dumps(_persisted, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    manifest["drum_tab"] = "drum_tab.json"
                else:
                    drum_tab_path.unlink(missing_ok=True)
                    manifest.pop("drum_tab", None)

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

            # Propagate the freshly-written manifest back into the in-memory
            # session. Without this, a later save in the same session that
            # omits `drum_tab` (the no-op path) would re-serialise the STALE
            # cached manifest — silently dropping the `drum_tab:` key and
            # un-linking drum_tab.json. Keeping the session manifest in sync
            # with disk makes every subsequent save start from current state.
            if isinstance(session.get("sloppak_state"), dict):
                session["sloppak_state"]["manifest"] = manifest

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
            log.exception("save_cdlc failed for session %r", session_id)
            return JSONResponse({"error": str(e)}, 500)

        session["_version"] = session.get("_version", 0) + 1
        return {"success": True, "path": output, "version": session["_version"]}

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
            log.exception("save_as_sloppak failed for session %r", session_id)
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
        start_time = data.get("start_time")
        end_time = data.get("end_time")
        if start_time is not None:
            try:
                start_time = float(start_time)
            except (ValueError, TypeError):
                start_time = None
        if end_time is not None:
            try:
                end_time = float(end_time)
            except (ValueError, TypeError):
                end_time = None
        if start_time is not None and end_time is not None and end_time <= start_time:
            return JSONResponse({"error": "end_time must be greater than start_time"}, 400)

        def _download():
            tmp = tempfile.mkdtemp(prefix="slopsmith_yt_")
            out_template = os.path.join(tmp, "audio.%(ext)s")
            try:
                import yt_dlp
                import subprocess
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
                    thumbnail_url = info.get("thumbnail")

                # Find the downloaded audio file
                downloaded = None
                for f in Path(tmp).iterdir():
                    if f.suffix in (".mp3", ".m4a", ".ogg", ".wav"):
                        downloaded = f
                        break
                if downloaded is None:
                    raise RuntimeError("No audio file produced")

                # Trim to specified time range using ffmpeg
                audio_id = re.sub(r"[^a-zA-Z0-9_-]", "_", title)[:60]
                ext = downloaded.suffix
                trimmed = Path(tmp) / f"audio_trimmed{ext}"
                if start_time is not None or end_time is not None:
                    print(f"[Editor] Trimming YouTube audio: start={start_time}s, end={end_time}s")
                    cmd = ["ffmpeg", "-y", "-i", str(downloaded)]
                    if start_time is not None:
                        cmd.extend(["-ss", str(start_time)])
                    if end_time is not None:
                        duration = end_time - start_time if start_time is not None else end_time
                        cmd.extend(["-to", str(duration)])
                    cmd.append(str(trimmed))
                    subprocess.run(cmd, check=True, capture_output=True, text=True)
                    downloaded = trimmed
                    print(f"[Editor] Trim complete: {trimmed.stat().st_size} bytes")

                # Move trimmed file to storage
                dest = STORAGE_DIR / f"editor_audio_{audio_id}{ext}"
                shutil.copy2(downloaded, dest)

                result = {
                    "audio_url": f"{STORAGE_URL}/editor_audio_{audio_id}{ext}",
                    "title": title,
                }

                # Download thumbnail if available
                if thumbnail_url:
                    try:
                        import urllib.request
                        thumb_dest = STORAGE_DIR / f"editor_thumb_{audio_id}.jpg"
                        urllib.request.urlretrieve(thumbnail_url, thumb_dest)
                        result["thumbnail_url"] = f"{STORAGE_URL}/editor_thumb_{audio_id}.jpg"
                    except Exception as e:
                        print(f"[Editor] Failed to download thumbnail: {e}")

                shutil.rmtree(tmp, ignore_errors=True)
                return result

                raise RuntimeError("Unreachable")
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
                log.exception("replace-audio sloppak persist failed")
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
        except struct.error as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return JSONResponse({"error": f"Truncated or malformed GP file: {e}"}, 400)
        except UnicodeDecodeError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return JSONResponse({"error": f"GP file has invalid text encoding: {e}"}, 400)
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            kind = type(e).__name__
            log.warning("GP parse failed (%s): %s", kind, e)
            return JSONResponse({"error": f"Could not parse GP file ({kind}): {e}"}, 400)

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
            log.exception("import_keys_midi convert failed")
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
            log.exception("convert-gp failed for %r", data.get("gp_path", ""))
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
            "_version": 0,
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
            log.exception("import-keys GP convert failed")
            return JSONResponse({"error": str(e)}, 500)

        return {"arrangement": arr_data, "tmp_dir": tmp_dir, "xml_path": xml_path}

    # ── Import guitar/bass tracks from a GP file ──────────────────────

    @app.post("/api/plugins/editor/import-guitar")
    async def import_guitar_track(data: dict):
        """Import a guitar/bass track from a GP file and return as an arrangement."""
        from lib.gp2rs import convert_track
        from lib.song import parse_arrangement
        import guitarpro

        gp_path_raw = data.get("gp_path", "")
        track_index = data.get("track_index")
        arrangement_name = data.get("arrangement_name", "Lead").strip() or "Lead"
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
            xml_str = convert_track(song, track_index, audio_offset, arrangement_name)

            tmp = tempfile.mkdtemp(prefix="slopsmith_guitar_")
            xml_path = os.path.join(tmp, f"{arrangement_name}.xml")
            Path(xml_path).write_text(xml_str, encoding="utf-8")

            arr = parse_arrangement(xml_path)
            arr_data = {
                "name": arrangement_name,
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
            log.exception("import-guitar GP convert failed")
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
            log.exception("import-drums GP convert failed")
            return JSONResponse({"error": str(e)}, 500)

        return {"arrangement": arr_data, "tmp_dir": tmp_dir, "xml_path": xml_path}

    # ── Import drum track → drum_tab.json (GP file) ──────────────────
    #
    # Sibling to `import-drums` above. That endpoint returns a guitar-style
    # arrangement dict (drums MIDI-encoded via string*24+fret) for the legacy
    # drums plugin path. The new endpoint returns the canonical
    # `drum_tab.json` shape documented in `docs/sloppak-spec.md` §5.3, ready
    # to be persisted via /save_cdlc's new `drum_tab:` body field.

    @app.post("/api/plugins/editor/import-drums-tab")
    async def import_drums_tab(data: dict):
        """Import a GP drum track and return it as a drum_tab.json dict."""
        from lib.gp2rs import convert_drum_track_to_drumtab
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

        arr_name = str(data.get("arrangement_name") or "Drums") or "Drums"

        def _convert():
            song = guitarpro.parse(gp_path)
            return convert_drum_track_to_drumtab(
                song, track_index, audio_offset, arr_name,
            )

        try:
            drum_tab = await asyncio.get_event_loop().run_in_executor(None, _convert)
        except IndexError:
            # song.tracks[track_index] out of range — a client input error,
            # not a server fault. Leave the upload dir for a retry.
            return JSONResponse(
                {"error": f"track_index {track_index} out of range"}, 400
            )
        except Exception as e:
            log.exception("import-drums GP convert failed")
            # Leave the upload temp dir on failure so the user can retry.
            return JSONResponse({"error": str(e)}, 500)

        # Clean up the GP upload temp dir now that conversion succeeded —
        # mirrors import_keys_midi. Without this, slopsmith_gp_* dirs would
        # accumulate in the system temp dir indefinitely.
        try:
            shutil.rmtree(Path(gp_path).parent)
        except OSError as _cleanup_err:
            import warnings
            warnings.warn(f"Could not clean up GP temp dir: {_cleanup_err}")

        return {"drum_tab": drum_tab}

    # ── MIDI drum import: list channel 10 (index 9) tracks ──────────

    @app.post("/api/plugins/editor/import-drums-midi-list")
    async def import_drums_midi_list(file: UploadFile = File(...)):
        """Upload a MIDI file and list channel 10 (drum) tracks for the picker.

        MIDI channel 10 is the General MIDI percussion channel; in 0-based
        wire encoding this is channel index 9 — `list_drum_tracks` filters
        on `channel == 9`. Returns `{midi_path, tracks: [...]}` so the
        frontend can show a track-picker modal identical to the keys flow.
        """
        from lib.midi_import import list_drum_tracks

        orig_suffix = Path(file.filename or "").suffix.lower()
        if orig_suffix not in (".mid", ".midi"):
            return JSONResponse(
                {"error": "Only .mid/.midi files are accepted"}, 400
            )

        # Opportunistic TTL cleanup of stale upload sandboxes (30 min).
        # Matches the keys-midi path so unclaimed uploads don't accumulate.
        _ttl_secs = 30 * 60
        tmp_root = Path(tempfile.gettempdir())
        for _stale in tmp_root.glob("slopsmith_drums_midi_*"):
            try:
                if _stale.is_dir():
                    age = time.time() - _stale.stat().st_mtime
                    if age > _ttl_secs:
                        shutil.rmtree(_stale, ignore_errors=True)
            except OSError:
                pass

        suffix = orig_suffix or ".mid"
        tmp = tempfile.mkdtemp(prefix="slopsmith_drums_midi_")
        midi_path = os.path.join(tmp, "upload" + suffix)
        content = await file.read()
        Path(midi_path).write_bytes(content)

        def _list():
            return list_drum_tracks(midi_path)

        try:
            tracks = await asyncio.get_event_loop().run_in_executor(None, _list)
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return JSONResponse({"error": f"Failed to parse MIDI file: {e}"}, 500)

        if not tracks:
            shutil.rmtree(tmp, ignore_errors=True)
            return JSONResponse(
                {"error": "No drum (channel-10) tracks found in MIDI file"}, 400
            )

        return {"midi_path": midi_path, "tracks": tracks}

    # ── MIDI drum import: convert a track → drum_tab.json ──────────

    @app.post("/api/plugins/editor/import-drums-midi")
    async def import_drums_midi(data: dict):
        """Convert a MIDI drum track to a drum_tab.json dict."""
        from lib.midi_import import convert_drum_track_from_midi

        midi_path_raw = data.get("midi_path", "")
        track_index = data.get("track_index")
        try:
            audio_offset = float(data.get("audio_offset", 0.0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "audio_offset must be a number"}, 400)

        validated = _validate_editor_upload_path(midi_path_raw, "slopsmith_drums_midi_")
        if not validated:
            return JSONResponse({"error": "MIDI file not found"}, 400)
        midi_path = str(validated)
        if track_index is None:
            return JSONResponse({"error": "track_index required"}, 400)
        try:
            track_index = int(track_index)
        except (TypeError, ValueError):
            return JSONResponse({"error": "track_index must be an integer"}, 400)

        arr_name = str(data.get("arrangement_name") or "Drums") or "Drums"

        def _convert():
            return convert_drum_track_from_midi(
                midi_path, track_index, audio_offset, arr_name,
            )

        _midi_tmp_dir = Path(midi_path).parent
        try:
            drum_tab = await asyncio.get_event_loop().run_in_executor(None, _convert)
        except ValueError as e:
            # convert_drum_track_from_midi raises ValueError for client input
            # errors (track_index out of range, non-finite audio_offset) —
            # surface those as 400, not a 500. Upload dir left for a retry.
            return JSONResponse({"error": str(e)}, 400)
        except Exception as e:
            log.exception("import-drums MIDI convert failed")
            # Leave the upload temp dir in place on failure — the frontend
            # keeps `_addDrumsFile` and re-enables the Import button, so the
            # user can retry the same upload. Deleting the dir here would
            # make that retry 404 with "MIDI file not found". Stale dirs are
            # swept by the opportunistic TTL cleanup, same as import_keys_midi.
            return JSONResponse({"error": str(e)}, 500)

        # Clean up the MIDI temp dir now that conversion is complete — mirrors
        # import_keys_midi which also rmtrees after a successful conversion so
        # temp dirs don't accumulate between TTL cleanup runs.
        try:
            shutil.rmtree(_midi_tmp_dir)
        except OSError as _cleanup_err:
            import warnings
            warnings.warn(f"Could not clean up drums MIDI temp dir: {_cleanup_err}")

        return {"drum_tab": drum_tab}

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

    # ── Generate difficulty levels for sloppak arrangement ───────────

    @app.post("/api/plugins/editor/generate-difficulties")
    async def generate_difficulties(data: dict):
        """Generate multi-difficulty phrase ladders for a sloppak arrangement."""
        session_id = data.get("session_id", "")
        session = sessions.get(session_id)
        if not session:
            return JSONResponse({"error": "No active session"}, 400)
        if session.get("format") != "sloppak":
            return JSONResponse({"error": "generate-difficulties only applies to sloppak sessions"}, 400)
        session["last_touched"] = time.time()

        arrangement_index = data.get("arrangement_index", 0)
        try:
            arrangement_index = int(arrangement_index)
        except (TypeError, ValueError):
            return JSONResponse({"error": "arrangement_index must be an integer"}, 400)

        n_levels = data.get("n_levels", 5)
        try:
            n_levels = max(2, min(int(n_levels), 10))
        except (TypeError, ValueError):
            n_levels = 5

        # The frontend sends the full arrangement state (which may be dirty)
        arr_data = data.get("arrangement")
        if not arr_data:
            return JSONResponse({"error": "arrangement data required"}, 400)

        notes = arr_data.get("notes", [])
        chords = arr_data.get("chords", [])
        handshapes = arr_data.get("handshapes", [])
        chord_templates = list(arr_data.get("chord_templates", []))
        tuning = arr_data.get("tuning", [0] * 6)
        beats = arr_data.get("beats", []) or data.get("beats", [])
        sections = arr_data.get("sections", []) or data.get("sections", [])
        is_keys = _is_keys_arr(arr_data.get("name", ""))

        def _run():
            detect_key = _chord_analysis.detect_key
            key_name = _chord_analysis.key_name

            # ── Key detection ─────────────────────────────────────────
            all_notes = list(notes)
            for ch in chords:
                for cn in ch.get("notes", []):
                    all_notes.append({
                        "string": cn.get("string", 0),
                        "fret": cn.get("fret", 0),
                        "sustain": cn.get("sustain", ch.get("sustain", 0)),
                    })
            if is_keys:
                key = detect_key(all_notes, tuning,
                                 pcs=_chord_analysis.notes_to_pitch_classes_keys(all_notes))
            else:
                key = detect_key(all_notes, tuning)
            detected_key_name = key_name(key)

            # ── Group notes ───────────────────────────────────────────
            groups = _group_notes_impl(notes, chords, handshapes, is_keys=is_keys)

            # ── Score groups ──────────────────────────────────────────
            _score_groups(groups, tuning, is_keys=is_keys)

            # ── Build phrase windows ──────────────────────────────────
            duration = 0.0
            for n in notes:
                end = float(n.get("time", 0)) + float(n.get("sustain", 0))
                if end > duration:
                    duration = end
            for ch in chords:
                end = float(ch.get("time", 0)) + 0.1
                if end > duration:
                    duration = end
            if duration == 0.0:
                duration = 30.0

            phrase_windows = []
            if sections:
                sorted_secs = sorted(sections, key=lambda s: float(s.get("time", s.get("start_time", 0))))
                for i, sec in enumerate(sorted_secs):
                    t_start = float(sec.get("time", sec.get("start_time", 0)))
                    t_end = float(sorted_secs[i + 1].get("time", sorted_secs[i + 1].get("start_time", 0))) if i + 1 < len(sorted_secs) else duration
                    phrase_windows.append((t_start, t_end))
            else:
                window = 30.0
                t = 0.0
                while t < duration:
                    phrase_windows.append((t, min(t + window, duration)))
                    t += window
            if not phrase_windows:
                phrase_windows = [(0.0, duration)]

            # ── Assign levels per phrase ──────────────────────────────
            beat_times = [float(b.get("time", 0)) for b in beats]
            phrases_out = []

            for phrase_idx, (t_start, t_end) in enumerate(phrase_windows):
                phrase_groups = [g for g in groups if t_start <= g["time"] < t_end]
                if not phrase_groups:
                    continue
                _assign_levels(phrase_groups, n_levels, ramp_up=(phrase_idx == 0))
                levels_out = []
                for lvl in range(n_levels):
                    lvl_notes, lvl_chords = _notes_for_level(phrase_groups, lvl, tuning, is_keys=is_keys)
                    # Handshapes and fret anchors are fretboard concepts the piano
                    # renderer never consumes — leave them empty for keys.
                    lvl_anchors = [] if is_keys else _generate_anchors(lvl_notes, beat_times)
                    lvl_handshapes = [] if is_keys else _groups_to_handshapes(
                        [g for g in phrase_groups if g["level"] <= lvl],
                        chord_templates,
                    )
                    levels_out.append({
                        "difficulty": lvl,
                        "notes": lvl_notes,
                        "chords": lvl_chords,
                        "anchors": lvl_anchors,
                        "handshapes": lvl_handshapes,
                    })
                phrases_out.append({
                    "start_time": round(t_start, 3),
                    "end_time": round(t_end, 3),
                    "max_difficulty": n_levels - 1,
                    "levels": levels_out,
                })

            # ── Auto-generate base handshapes (max difficulty) ────────
            top_handshapes = [] if is_keys else _groups_to_handshapes(groups, chord_templates)

            # ── Name unnamed chord templates ──────────────────────────
            # Keys arrangements carry no fret-based chord templates to name.
            named_templates = (
                list(chord_templates) if is_keys
                else _name_chord_templates(chord_templates, notes, chords, tuning, key)
            )

            return {
                "key": detected_key_name,
                "phrases": phrases_out,
                "handshapes": top_handshapes,
                "chord_templates": named_templates,
            }

        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as e:
            log.exception("generate-difficulties failed for session %r", data.get("session_id", ""))
            return JSONResponse({"error": str(e)}, 500)

        return result

    # ── Library: check if a sloppak already has phrase data ──────────

    @app.get("/api/plugins/editor/sloppak-has-phrases")
    async def sloppak_has_phrases(filename: str = ""):
        """Return {has_phrases: bool} — fast check without loading the full song."""
        if not filename.lower().endswith(".sloppak"):
            return JSONResponse({"error": "Not a sloppak"}, 400)
        dlc_dir = get_dlc_dir()
        filepath = (dlc_dir / filename).resolve()
        dlc_resolved = dlc_dir.resolve()
        if not filepath.exists() or not str(filepath).startswith(str(dlc_resolved)):
            return JSONResponse({"error": "File not found"}, 400)

        # Drums have no pitch-based difficulty model; keys/piano flow through the
        # piano-aware difficulty path below.
        SKIP_NAMES = {"drum"}

        def _check():
            source_dir = Path(sloppak_mod.resolve_source_dir(filename, dlc_dir, SLOPPAK_CACHE))
            arr_dir = source_dir / "arrangements"
            if not arr_dir.exists():
                return True
            for arr_path in sorted(arr_dir.glob("*.json")):
                if any(s in arr_path.stem.lower() for s in SKIP_NAMES):
                    continue
                try:
                    arr_json = json.loads(arr_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not arr_json.get("phrases"):
                    return False
            return True

        try:
            has = await asyncio.get_event_loop().run_in_executor(None, _check)
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)
        return {"has_phrases": has}

    # ── Library: batch-generate difficulties for a sloppak from disk ──

    @app.post("/api/plugins/editor/fix-difficulties")
    async def fix_difficulties(data: dict):
        """Generate phrase difficulty ladders for all guitar/bass arrangements in a
        sloppak file, writing the result back to disk without opening an editor session.
        """
        filename = data.get("filename", "")
        if not filename.lower().endswith(".sloppak"):
            return JSONResponse({"error": "Not a sloppak"}, 400)
        dlc_dir = get_dlc_dir()
        filepath = (dlc_dir / filename).resolve()
        dlc_resolved = dlc_dir.resolve()
        if not filepath.exists() or not str(filepath).startswith(str(dlc_resolved)):
            return JSONResponse({"error": "File not found"}, 400)

        n_levels = data.get("n_levels", 5)
        try:
            n_levels = max(2, min(int(n_levels), 10))
        except (TypeError, ValueError):
            n_levels = 5

        # Drums have no pitch-based difficulty model; keys/piano flow through the
        # piano-aware difficulty path below.
        SKIP_NAMES = {"drum"}

        def _run():
            source_dir = Path(sloppak_mod.resolve_source_dir(filename, dlc_dir, SLOPPAK_CACHE))
            arr_dir = source_dir / "arrangements"
            if not arr_dir.exists():
                return {"updated": 0, "skipped": 0, "key": ""}

            updated = 0
            skipped = 0
            last_key = ""

            def _wire_note_to_editor(wn, time_override=None):
                t = float(time_override if time_override is not None else wn.get("t", 0))
                return {
                    "time": t,
                    "string": int(wn.get("s", 0)),
                    "fret": int(wn.get("f", 0)),
                    "sustain": float(wn.get("sus", 0)),
                    "techniques": {
                        "slide_to": int(wn.get("sl", -1)),
                        "slide_unpitch_to": int(wn.get("slu", -1)),
                        "bend": float(wn.get("bn", 0) or 0),
                        "hammer_on": bool(wn.get("ho", False)),
                        "pull_off": bool(wn.get("po", False)),
                        "harmonic": bool(wn.get("hm", False)),
                        "harmonic_pinch": bool(wn.get("hp", False)),
                        "palm_mute": bool(wn.get("pm", False)),
                        "mute": bool(wn.get("mt", False)),
                        "tremolo": bool(wn.get("tr", False)),
                        "accent": bool(wn.get("ac", False)),
                        "tap": bool(wn.get("tp", False)),
                    },
                }

            def _wire_chord_to_editor(wc):
                t = float(wc.get("t", 0))
                return {
                    "time": t,
                    "chord_id": int(wc.get("id", -1)),
                    "high_density": bool(wc.get("hd", False)),
                    "notes": [_wire_note_to_editor(cn, time_override=t) for cn in wc.get("notes", [])],
                }

            for arr_path in sorted(arr_dir.glob("*.json")):
                if any(s in arr_path.stem.lower() for s in SKIP_NAMES):
                    skipped += 1
                    continue

                try:
                    arr_json = json.loads(arr_path.read_text(encoding="utf-8"))
                except Exception:
                    skipped += 1
                    continue

                if arr_json.get("phrases"):
                    skipped += 1
                    continue

                # Keys/piano arrangements use the pitch-based difficulty path
                # (no fretboard). Detected from the arrangement file stem, matching
                # the substring convention the SKIP_NAMES check uses.
                is_keys = any(
                    s in arr_path.stem.lower()
                    for s in ("key", "piano", "keyboard", "synth")
                )

                tuning = arr_json.get("tuning", [0] * 6)
                notes = [_wire_note_to_editor(wn) for wn in arr_json.get("notes", [])]
                chords = [_wire_chord_to_editor(wc) for wc in arr_json.get("chords", [])]
                wire_handshapes = arr_json.get("handshapes", [])
                # chord templates: convert "arp" → "arpeggio" for _groups_to_handshapes lookup
                chord_templates = [
                    {**ct, "arpeggio": bool(ct.get("arp", False))}
                    for ct in arr_json.get("templates", [])
                ]

                # Key detection
                all_notes_kd = list(notes)
                for ch in chords:
                    for cn in ch.get("notes", []):
                        all_notes_kd.append({
                            "string": cn.get("string", 0),
                            "fret": cn.get("fret", 0),
                            "sustain": cn.get("sustain", 0),
                        })
                if is_keys:
                    key = _chord_analysis.detect_key(
                        all_notes_kd, tuning,
                        pcs=_chord_analysis.notes_to_pitch_classes_keys(all_notes_kd),
                    )
                else:
                    key = _chord_analysis.detect_key(all_notes_kd, tuning)
                last_key = _chord_analysis.key_name(key)

                groups = _group_notes_impl(notes, chords, wire_handshapes, is_keys=is_keys)
                _score_groups(groups, tuning, is_keys=is_keys)

                # Phrase windows: 30s slices (no beat/section data on disk)
                duration = max(
                    (float(n["time"]) + float(n["sustain"]) for n in notes),
                    default=0.0,
                )
                for ch in chords:
                    duration = max(duration, float(ch["time"]) + 0.1)
                if duration == 0.0:
                    duration = 30.0

                phrase_windows = []
                t = 0.0
                while t < duration:
                    phrase_windows.append((t, min(t + 30.0, duration)))
                    t += 30.0
                if not phrase_windows:
                    phrase_windows = [(0.0, duration)]

                phrases_out = []
                for phrase_idx, (t_start, t_end) in enumerate(phrase_windows):
                    phrase_groups = [g for g in groups if t_start <= g["time"] < t_end]
                    if not phrase_groups:
                        continue
                    _assign_levels(phrase_groups, n_levels, ramp_up=(phrase_idx == 0))
                    levels_out = []
                    for lvl in range(n_levels):
                        lvl_notes, lvl_chords = _notes_for_level(phrase_groups, lvl, tuning, is_keys=is_keys)
                        lvl_anchors = [] if is_keys else _generate_anchors(lvl_notes, [])
                        lvl_handshapes = [] if is_keys else _groups_to_handshapes(
                            [g for g in phrase_groups if g["level"] <= lvl],
                            chord_templates,
                        )
                        levels_out.append({
                            "difficulty": lvl,
                            "notes": lvl_notes,
                            "chords": lvl_chords,
                            "anchors": lvl_anchors,
                            "handshapes": lvl_handshapes,
                        })
                    phrases_out.append({
                        "start_time": round(t_start, 3),
                        "end_time": round(t_end, 3),
                        "max_difficulty": n_levels - 1,
                        "levels": levels_out,
                    })

                if not phrases_out:
                    skipped += 1
                    continue

                arr_json["phrases"] = phrases_out
                arr_path.write_text(json.dumps(arr_json, ensure_ascii=False), encoding="utf-8")
                updated += 1

            # Re-zip if the sloppak is file-form (zip archive)
            if filepath.is_file() and updated > 0:
                tmp_zip = filepath.with_suffix(filepath.suffix + ".tmp")
                with zipfile.ZipFile(str(tmp_zip), "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in source_dir.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(source_dir).as_posix())
                tmp_zip.replace(filepath)

            return {"updated": updated, "skipped": skipped, "key": last_key}

        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as e:
            log.exception("library difficulty generation failed")
            return JSONResponse({"error": str(e)}, 500)

        return result

    # ── Build CDLC from create-mode session ──────────────────────────

    @app.post("/api/plugins/editor/build")
    async def build_cdlc_endpoint(data: dict):
        """Start a CDLC build from the current create-mode session.

        Returns immediately with a ``build_id``; the caller polls
        ``GET /api/plugins/editor/build-progress/{build_id}`` for status.

        Writes a ``.sloppak`` when any arrangement uses extended-range strings
        (7/8-string guitar or 5/6-string bass) — RS2014's SNG binary format
        is hard-locked to 6/4 strings, so a regular PSARC build via RsCli
        would crash inside ``ConvertInstrumental.xmlToSng``. Falls back to the
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
        thumbnail_url = data.get("thumbnail_url", "")

        needs_sloppak = any(_is_extended_range(a) for a in arrangements_data)

        build_id = uuid.uuid4().hex[:10]
        _build_jobs[build_id] = {"status": "running", "message": "Starting build…"}

        def _progress(msg: str) -> None:
            _build_jobs[build_id]["message"] = msg

        def _build():
            xml_files = session["xml_files"]
            log.info("[Build] Session has %d XML files", len(xml_files))
            log.info("[Build] Received %d arrangements from frontend", len(arrangements_data))

            _progress("Writing arrangement XML…")
            for i, xml_path in enumerate(xml_files):
                tree = ET.parse(xml_path)
                old_root = tree.getroot()

                if i < len(arrangements_data):
                    arr = arrangements_data[i]
                    arr_notes = arr.get("notes", [])
                    arr_chords = arr.get("chords", [])
                    arr_templates = arr.get("chord_templates", [])
                    log.info("[Build] Arr %d: %d notes, %d chords", i, len(arr_notes), len(arr_chords))
                else:
                    arr_notes, arr_chords, arr_templates = [], [], []
                    log.warning("[Build] Arr %d: no data received from frontend", i)

                xml_str = _build_arrangement_xml(
                    old_root, arr_notes, arr_chords, arr_templates,
                    beats, sections, meta,
                )
                Path(xml_path).write_text(xml_str, encoding="utf-8")

            _progress("Resolving audio…")
            resolved = _resolve_storage_url(audio_url) if audio_url else None
            audio_file = str(resolved) if resolved else ""

            if not audio_file or not Path(audio_file).exists():
                raise RuntimeError(f"No audio file available for build (url={audio_url}, resolved={audio_file})")

            # Deduplicate arrangement names from XMLs
            arr_names = []
            name_counts: dict = {}
            for xp in xml_files:
                root = ET.parse(xp).getroot()
                el = root.find("arrangement")
                name = el.text if el is not None and el.text else "Lead"
                name_counts[name] = name_counts.get(name, 0) + 1
                if name_counts[name] > 1:
                    name = f"{name}{name_counts[name]}"
                arr_names.append(name)
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

            final_art_path = ""
            if art_path and Path(art_path).exists():
                final_art_path = art_path
            elif thumbnail_url:
                resolved_thumb = _resolve_storage_url(thumbnail_url) if thumbnail_url else None
                if resolved_thumb and resolved_thumb.exists():
                    final_art_path = str(resolved_thumb)
                else:
                    log.warning("[Build] Thumbnail URL could not be resolved: %s", thumbnail_url)

            _progress("Compiling CDLC…")
            result = build_cdlc(
                xml_paths=xml_files,
                arrangement_names=arr_names,
                audio_path=audio_file,
                title=title,
                artist=artist,
                album=meta.get("albumName") or meta.get("album", ""),
                year=str(meta.get("albumYear") or meta.get("year", "")),
                output_path=output,
                album_art_path=final_art_path,
            )
            log.info("[Build] build_cdlc returned: %s", result)
            return result

        def _build_sloppak_extended():
            _progress("Building sloppak…")
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

        async def _run_build():
            try:
                target = _build_sloppak_extended if needs_sloppak else _build
                output_path = await asyncio.get_event_loop().run_in_executor(None, target)
                _build_jobs[build_id] = {
                    "status": "done",
                    "message": "Build complete",
                    "path": str(output_path),
                    "format": "sloppak" if needs_sloppak else "psarc",
                }
            except IsADirectoryError as e:
                # _write_sloppak_pak refused to clobber an authoring-form
                # sloppak directory — surface as conflict so the UI can prompt.
                _build_jobs[build_id] = {
                    "status": "error",
                    "message": str(e),
                    "conflict": True,
                }
            except Exception as e:
                log.exception("build failed for session %r", session_id)
                _build_jobs[build_id] = {"status": "error", "message": str(e)}

        asyncio.ensure_future(_run_build())
        return {"build_id": build_id}

    @app.get("/api/plugins/editor/build-progress/{build_id}")
    async def build_progress(build_id: str):
        """Poll for the status of a running build started by /build."""
        job = _build_jobs.get(build_id)
        if not job:
            return JSONResponse({"error": "No such build job"}, 404)
        result = dict(job)
        if job["status"] != "running":
            _build_jobs.pop(build_id, None)
        return result

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

    # ── Difficulty generation helpers ─────────────────────────────────

    _KEYS_NAME_RE = re.compile(r"^(keys|piano|keyboard|synth)", re.IGNORECASE)

    def _is_keys_arr(name):
        """True for keys/piano arrangements (mirrors the frontend KEYS_PATTERN)."""
        return bool(_KEYS_NAME_RE.match((name or "").strip()))

    def _note_midi(n):
        """Absolute MIDI pitch for a keys note (string/fret encode midi = s*24 + f)."""
        return int(n.get("string", 0)) * 24 + int(n.get("fret", 0))

    def _note_to_wire(n):
        """Editor long-format note → sloppak wire format (t/s/f/sus/...)."""
        tech = n.get("techniques", {}) or {}
        return {
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

    def _group_notes_keys(notes, chords, *, onset_window_ms=30):
        """Group keys notes into atomic units for difficulty generation.

        No fretboard, so grouping is purely temporal: explicit chords stay chords,
        and remaining notes sharing an onset (within ``onset_window_ms``) become a
        block-chord cluster. Everything else is an individual note.
        """
        groups = []
        for ch in chords:
            groups.append({
                "type": "chord",
                "notes": list(ch.get("notes", [])),
                "chord": ch,
                "time": float(ch.get("time", 0)),
                "score": 0.0,
                "level": 0,
            })

        note_list = sorted(
            (dict(n) for n in notes),
            key=lambda n: float(n.get("time", 0)),
        )
        n_total = len(note_list)
        i = 0
        while i < n_total:
            base_t = float(note_list[i].get("time", 0))
            cluster = [note_list[i]]
            j = i + 1
            while j < n_total and (float(note_list[j].get("time", 0)) - base_t) * 1000 <= onset_window_ms:
                cluster.append(note_list[j])
                j += 1
            groups.append({
                "type": "chord" if len(cluster) > 1 else "note",
                "notes": cluster,
                "chord": None,
                "time": base_t,
                "score": 0.0,
                "level": 0,
            })
            i = j

        groups.sort(key=lambda g: g["time"])
        return groups

    def _score_groups_keys(groups):
        """Compute pitch/rhythm-based difficulty scores in-place (0–1 per group).

        Keys difficulty is driven by how many notes sound at once (polyphony), how
        wide the reach is (hand span in semitones), how dense/fast the passage is,
        and sustain ease — not fretboard position.
        """
        total = len(groups)
        for gi, g in enumerate(groups):
            notes_list = g["notes"]
            if not notes_list:
                g["score"] = 0.0
                continue

            midis = [_note_midi(n) for n in notes_list]

            # Polyphony — 1 note = 0.0, 5+ simultaneous = 1.0
            poly = min(1.0, (len(notes_list) - 1) / 4.0)

            # Hand span — semitone reach across the group; an octave = 1.0
            span = (max(midis) - min(midis)) if len(midis) > 1 else 0
            span_score = min(1.0, span / 12.0)

            # Note density — compare to nearby groups
            density_window = 5
            lo = max(0, gi - density_window)
            hi = min(total, gi + density_window + 1)
            nearby_count = sum(len(groups[k]["notes"]) for k in range(lo, hi))
            density = min(1.0, nearby_count / max(density_window * 4, 1))

            # Speed — short onset gap to the next group ramps difficulty up
            speed = 0.0
            if gi + 1 < total:
                dt = float(groups[gi + 1]["time"]) - float(g["time"])
                if dt > 0:
                    speed = min(1.0, max(0.0, (0.25 - dt) / 0.25))

            # Sustain ease — long held notes are easier
            max_sus = max(float(n.get("sustain", 0)) for n in notes_list)
            sustain_ease = min(1.0, max_sus / 2.0)

            g["score"] = (
                0.30 * poly
                + 0.25 * span_score
                + 0.20 * density
                + 0.15 * speed
                + 0.10 * (1.0 - sustain_ease)
            )

    def _notes_for_level_keys(groups, level):
        """Return (notes_wire, []) for keys notes at or below ``level``.

        Chords/clusters are thinned by pitch: level 0 keeps melody (highest MIDI) +
        bass (lowest MIDI), level 1 adds one inner voice, higher levels keep the full
        voicing — mirroring how simplified piano sheet music is arranged.
        """
        out_notes = []
        for g in groups:
            if g["level"] > level:
                continue
            g_notes = list(g["notes"])
            g_time = float(g.get("time", 0))
            # Explicit chords key note times off the chord (their notes may omit or
            # zero their own time); clustered/loose notes keep their own onset.
            is_explicit_chord = g.get("chord") is not None
            if len(g_notes) > 1:
                ranked = sorted(g_notes, key=_note_midi)
                if level == 0:
                    keep = [ranked[0], ranked[-1]]
                elif level == 1 and len(ranked) > 3:
                    keep = [ranked[0], ranked[len(ranked) // 2], ranked[-1]]
                else:
                    keep = ranked
                # Preserve order/identity, drop duplicates (bass == melody edge case)
                seen = set()
                deduped = []
                for n in keep:
                    if id(n) not in seen:
                        seen.add(id(n))
                        deduped.append(n)
                g_notes = deduped
            for n in g_notes:
                merged = dict(n)
                if is_explicit_chord or merged.get("time") is None:
                    merged["time"] = g_time
                out_notes.append(merged)

        out_notes_wire = [
            _note_to_wire(n)
            for n in sorted(out_notes, key=lambda n: float(n.get("time", 0)))
        ]
        return out_notes_wire, []

    def _group_notes_impl(notes, chords, handshapes, *, time_window_ms=150, fret_span_max=4, is_keys=False):
        """Group notes into atomic units for difficulty generation.

        Priority: chords → link_next chains → handshape windows
        → time-proximity clusters → individual notes.
        Each group: {type, notes, chord, time, score, level}.

        Keys/piano arrangements have no fretboard, so they use a purely temporal
        grouping (see ``_group_notes_keys``).
        """
        if is_keys:
            return _group_notes_keys(notes, chords)

        groups = []
        assigned_note_indices = set()
        assigned_chord_indices = set()

        # 1. Explicit chords
        for ci, ch in enumerate(chords):
            assigned_chord_indices.add(ci)
            groups.append({
                "type": "chord",
                "notes": list(ch.get("notes", [])),
                "chord": ch,
                "time": float(ch.get("time", 0)),
                "score": 0.0,
                "level": 0,
            })

        # Build index of notes by (string, time) for link_next chaining
        note_list = [dict(n, _idx=i) for i, n in enumerate(notes)]

        # 2. link_next chains among single notes
        in_chain = set()
        for i, n in enumerate(note_list):
            if i in in_chain:
                continue
            tech = n.get("techniques", {}) or {}
            if not tech.get("link_next", False):
                continue
            chain = [n]
            in_chain.add(i)
            cur = n
            # Follow chain: find next note on same string close in time
            while True:
                cur_end = float(cur.get("time", 0)) + float(cur.get("sustain", 0))
                cur_str = cur.get("string", 0)
                next_n = None
                for j, m in enumerate(note_list):
                    if j in in_chain:
                        continue
                    if m.get("string", 0) == cur_str and abs(float(m.get("time", 0)) - cur_end) < 0.05:
                        next_n = (j, m)
                        break
                if next_n is None:
                    break
                jj, mm = next_n
                chain.append(mm)
                in_chain.add(jj)
                cur = mm
                cur_tech = mm.get("techniques", {}) or {}
                if not cur_tech.get("link_next", False):
                    break
            if len(chain) > 1:
                for nn in chain:
                    assigned_note_indices.add(nn["_idx"])
                groups.append({
                    "type": "arpeggio",
                    "notes": chain,
                    "chord": None,
                    "time": float(chain[0].get("time", 0)),
                    "score": 0.0,
                    "level": 0,
                })

        # 3. Handshape windows
        if handshapes:
            sorted_hs = sorted(handshapes, key=lambda h: float(h.get("start_time", 0)))
            for hs in sorted_hs:
                t_start = float(hs.get("start_time", 0))
                t_end = float(hs.get("end_time", t_start + 0.5))
                window_notes = [
                    n for n in note_list
                    if n["_idx"] not in assigned_note_indices
                    and t_start <= float(n.get("time", 0)) < t_end
                ]
                if len(window_notes) > 1:
                    for nn in window_notes:
                        assigned_note_indices.add(nn["_idx"])
                    groups.append({
                        "type": "arpeggio",
                        "notes": window_notes,
                        "chord": None,
                        "time": t_start,
                        "score": 0.0,
                        "level": 0,
                    })

        # 4. Time-proximity clusters
        remaining = [n for n in note_list if n["_idx"] not in assigned_note_indices]
        remaining.sort(key=lambda n: float(n.get("time", 0)))
        used = set()
        for i, n in enumerate(remaining):
            if i in used:
                continue
            cluster = [n]
            cluster_strings = {n.get("string", 0)}
            n_fret = n.get("fret", 0)
            cluster_frets = [n_fret] if n_fret > 0 else []
            for j, m in enumerate(remaining[i + 1:], start=i + 1):
                if j in used:
                    continue
                dt_ms = (float(m.get("time", 0)) - float(n.get("time", 0))) * 1000
                if dt_ms > time_window_ms:
                    break
                if m.get("string", 0) in cluster_strings:
                    continue
                m_fret = m.get("fret", 0)
                all_frets = cluster_frets + ([m_fret] if m_fret > 0 else [])
                if all_frets and (max(all_frets) - min(all_frets)) > fret_span_max:
                    continue
                cluster.append(m)
                cluster_strings.add(m.get("string", 0))
                if m_fret > 0:
                    cluster_frets.append(m_fret)
                used.add(j)
            if len(cluster) > 1:
                used.add(i)
                for nn in cluster:
                    assigned_note_indices.add(nn["_idx"])
                groups.append({
                    "type": "arpeggio",
                    "notes": cluster,
                    "chord": None,
                    "time": float(cluster[0].get("time", 0)),
                    "score": 0.0,
                    "level": 0,
                })
            else:
                used.add(i)

        # 5. Remaining individual notes
        for n in note_list:
            if n["_idx"] not in assigned_note_indices:
                groups.append({
                    "type": "note",
                    "notes": [n],
                    "chord": None,
                    "time": float(n.get("time", 0)),
                    "score": 0.0,
                    "level": 0,
                })

        groups.sort(key=lambda g: g["time"])
        return groups

    def _score_groups(groups, tuning, *, is_keys=False):
        """Compute composite difficulty scores in-place (0–1 per group)."""
        if is_keys:
            _score_groups_keys(groups)
            return
        n_strings = len(tuning)

        def _fret_score(fret):
            if fret == 0:
                return 0.0
            return min(1.0, fret / 22.0)

        def _span_score(notes_list):
            frets = [n.get("fret", 0) for n in notes_list if n.get("fret", 0) > 0]
            if len(frets) < 2:
                return 0.0
            return min(1.0, (max(frets) - min(frets)) / 6.0)

        def _tech_score(note):
            tech = note.get("techniques", {}) or {}
            score = 0.0
            if tech.get("bend", 0):
                score += 0.4
            if tech.get("hammer_on") or tech.get("pull_off"):
                score += 0.25
            if tech.get("tap"):
                score += 0.5
            if tech.get("slide_to", -1) >= 0 or tech.get("slide_unpitch_to", -1) >= 0:
                score += 0.2
            if tech.get("tremolo"):
                score += 0.3
            if tech.get("harmonic") or tech.get("harmonic_pinch"):
                score += 0.15
            return min(1.0, score)

        total = len(groups)
        for gi, g in enumerate(groups):
            notes_list = g["notes"]
            if not notes_list:
                g["score"] = 0.0
                continue

            # Fretting complexity
            lead_note = notes_list[0]
            avg_fret = sum(n.get("fret", 0) for n in notes_list) / len(notes_list)
            fretting = (
                0.4 * _fret_score(avg_fret)
                + 0.35 * _span_score(notes_list)
                + 0.25 * min(1.0, (len(notes_list) - 1) / max(n_strings - 1, 1))
            )

            # Technique difficulty
            technique = max(_tech_score(n) for n in notes_list)

            # Note density — compare to nearby groups
            density_window = 5
            lo = max(0, gi - density_window)
            hi = min(total, gi + density_window + 1)
            nearby_count = sum(len(groups[k]["notes"]) for k in range(lo, hi))
            density = min(1.0, nearby_count / max(density_window * 4, 1))

            # Sustain ease — long sustained notes are easier
            max_sus = max(float(n.get("sustain", 0)) for n in notes_list)
            sustain_ease = min(1.0, max_sus / 2.0)

            g["score"] = (
                0.35 * fretting
                + 0.30 * technique
                + 0.20 * density
                + 0.15 * (1.0 - sustain_ease)
            )

    def _assign_levels(groups, n_levels, ramp_up=False):
        """Assign difficulty levels 0..n_levels-1 to groups using per-phrase percentiles."""
        if not groups:
            return
        scores = [g["score"] for g in groups]
        scores_sorted = sorted(scores)
        total = len(scores_sorted)
        thresholds = [
            scores_sorted[min(int((i + 1) / n_levels * total), total - 1)]
            for i in range(n_levels - 1)
        ]
        max_lvl = n_levels - 1
        if ramp_up:
            max_lvl = min(max_lvl, 2)
        for g in groups:
            lvl = 0
            for t in thresholds:
                if g["score"] > t:
                    lvl += 1
            g["level"] = min(lvl, max_lvl)

    def _notes_for_level(groups, level, tuning, *, is_keys=False):
        """Return (notes_list, chords_list) for notes at or below the given level.

        Chord groups are flattened to individual notes so phrase levels have no
        chord_id references. This avoids stale chord_id / chord-template index
        mismatches when reconstructChords() reindexes templates before save.

        Keys/piano arrangements thin chords by pitch instead of string index
        (see ``_notes_for_level_keys``).
        """
        if is_keys:
            return _notes_for_level_keys(groups, level)

        out_notes = []
        for g in groups:
            if g["level"] > level:
                continue
            if g["type"] == "chord" and g["chord"] is not None:
                ch = g["chord"]
                ch_time = float(ch.get("time", 0))
                ch_notes = list(ch.get("notes", []))
                # For low levels, reduce chord voicing
                if level == 0 and len(ch_notes) > 1:
                    # Keep only root (lowest pitch = highest string index)
                    ch_notes = [max(ch_notes, key=lambda n: n.get("string", 0))]
                elif level == 1 and len(ch_notes) > 2:
                    # Keep root + 5th (2 lowest-pitched strings)
                    ch_notes = sorted(ch_notes, key=lambda n: n.get("string", 0))[-2:]
                # Flatten chord notes into individual notes using the chord's time
                for cn in ch_notes:
                    merged = dict(cn)
                    merged["time"] = ch_time  # ensure time is set (chord notes may omit it)
                    out_notes.append(merged)
            else:
                if level == 0 and g["type"] == "arpeggio":
                    if g["notes"]:
                        out_notes.append(g["notes"][0])
                elif level == 1 and g["type"] == "arpeggio" and len(g["notes"]) > 2:
                    out_notes.extend(g["notes"][:2])
                else:
                    out_notes.extend(g["notes"])

        # Convert editor long-format → sloppak wire format so phrase notes
        # match what the server streams (t/s/f/sus/... not time/string/fret/...).
        def _to_wire(n):
            tech = n.get("techniques", {}) or {}
            return {
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

        out_notes_wire = [_to_wire(n) for n in sorted(out_notes, key=lambda n: float(n.get("time", 0)))]
        return out_notes_wire, []

    def _generate_anchors(notes, beat_times, *, default_width=4):
        """Generate anchor list from notes, grouped by beat window."""
        if not notes:
            return []
        anchors = []
        prev_fret = None
        prev_width = None
        for i, bt in enumerate(beat_times):
            bt_end = beat_times[i + 1] if i + 1 < len(beat_times) else bt + 2.0
            window_notes = [
                n for n in notes
                if bt <= float(n.get("t", n.get("time", 0))) < bt_end and n.get("f", n.get("fret", 0)) >= 1
            ]
            if not window_notes:
                continue
            frets = [n.get("f", n.get("fret", 0)) for n in window_notes]
            min_fret = max(1, min(frets))
            max_fret = max(frets)
            width = max(default_width, max_fret - min_fret + 3)
            if min_fret != prev_fret or width != prev_width:
                anchors.append({"time": round(bt, 3), "fret": min_fret, "width": width})
                prev_fret = min_fret
                prev_width = width
        return anchors

    def _groups_to_handshapes(groups, chord_templates):
        """Generate handshape list from note groups."""
        handshapes = []
        chord_template_map = {i: ct for i, ct in enumerate(chord_templates)}
        for g in groups:
            if g["type"] == "chord" and g["chord"] is not None:
                ch = g["chord"]
                t = float(ch.get("time", 0))
                chord_id = ch.get("chord_id", -1)
                # Estimate end time from note sustains
                note_ends = [
                    t + float(cn.get("sustain", 0))
                    for cn in ch.get("notes", [])
                ]
                t_end = max(note_ends) if note_ends else t + 0.1
                is_arp = chord_template_map.get(chord_id, {}).get("arpeggio", False)
                handshapes.append({
                    "chord_id": chord_id,
                    "start_time": round(t, 3),
                    "end_time": round(max(t + 0.05, t_end), 3),
                    "arp": bool(is_arp),
                })
            elif g["type"] == "arpeggio" and len(g["notes"]) > 1:
                ns = g["notes"]
                t_start = float(ns[0].get("time", 0))
                t_end = max(float(n.get("time", 0)) + float(n.get("sustain", 0)) for n in ns)
                handshapes.append({
                    "chord_id": -1,
                    "start_time": round(t_start, 3),
                    "end_time": round(max(t_start + 0.05, t_end), 3),
                    "arp": True,
                })
        return handshapes

    def _name_chord_templates(chord_templates, notes, chords, tuning, key):
        """Infer names for chord templates that have empty names."""
        fret_to_midi = _chord_analysis.fret_to_midi
        name_chord = _chord_analysis.name_chord
        result = []
        for i, ct in enumerate(chord_templates):
            if ct.get("name"):
                result.append(dict(ct))
                continue
            frets = ct.get("frets", [])
            fingers = ct.get("fingers", [])
            pitch_classes = set()
            lowest_pc = None
            lowest_string = len(frets)
            for si, fr in enumerate(frets):
                finger = fingers[si] if si < len(fingers) else -1
                if fr < 0 or finger == -1:
                    continue
                midi = fret_to_midi(si, fr, tuning)
                pc = midi % 12
                pitch_classes.add(pc)
                if si > lowest_string or lowest_pc is None:
                    lowest_string = si
                    lowest_pc = pc
            new_ct = dict(ct)
            if pitch_classes:
                new_ct["name"] = name_chord(frozenset(pitch_classes), key, lowest_pc)
            result.append(new_ct)
        return result

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

    # ── Transcribe audio: stem separation + note detection ───────────
    @app.post("/api/plugins/editor/transcribe-audio")
    async def transcribe_audio(data: dict):
        """Transcribe audio into arrangements using stem separation and basic-pitch."""
        from lib.song import Song, Beat, Section, Arrangement, Note, Chord
        from lib.sloppak_convert import split_sloppak_stems, demucs_available

        def _transcribe_with_basic_pitch(stem_path: str) -> list:
            """Transcribe a single audio file with basic-pitch, return note events."""
            from basic_pitch.inference import predict, ICASSP_2022_MODEL_PATH
            model_output, midi_data, note_events = predict(
                str(stem_path),
                ICASSP_2022_MODEL_PATH
            )
            return note_events

        def _midi_events_to_notes(note_events: list, string_count: int) -> list:
            """Convert MIDI note events to Rocksmith Note objects."""
            # Standard tunings (MIDI note numbers)
            if string_count == 4:
                open_strings = [28, 33, 38, 43]  # E1, A1, D2, G2 (bass)
            else:
                open_strings = [40, 45, 50, 55, 59, 64]  # E2, A2, D3, G3, B3, E4 (guitar)

            notes = []

            for start_time, end_time, pitch, velocity, pitch_bend in note_events:
                # Find best string and fret
                best_string = 0
                best_fret = 0
                min_distance = float('inf')

                for s, open_pitch in enumerate(open_strings):
                    fret = pitch - open_pitch
                    if 0 <= fret <= 24:
                        # Prefer lower frets on higher strings
                        distance = abs(fret - 5) + s * 0.5
                        if distance < min_distance:
                            min_distance = distance
                            best_string = s
                            best_fret = fret

                if min_distance == float('inf'):
                    continue

                duration = end_time - start_time
                sustain = float(duration) if duration > 0.1 else 0.0

                note = Note(
                    time=float(start_time),
                    string=best_string,
                    fret=int(best_fret),
                    sustain=sustain
                )

                notes.append(note)

            notes.sort(key=lambda n: n.time)
            return notes

        def _midi_events_to_piano_notes(note_events: list) -> list:
            """Convert MIDI note events to piano/keys Rocksmith Note objects using MIDI encoding."""
            notes = []
            for start_time, end_time, pitch, velocity, pitch_bend in note_events:
                rs_string = pitch // 24
                rs_fret = pitch % 24
                duration = end_time - start_time
                sustain = float(duration) if duration > 0.1 else 0.0
                note = Note(
                    time=float(start_time),
                    string=rs_string,
                    fret=rs_fret,
                    sustain=sustain
                )
                notes.append(note)
            notes.sort(key=lambda n: n.time)
            return notes

        def _transcribe_stems_to_arrangements(stem_files: list) -> list:
            """Transcribe audio stems to arrangements using basic-pitch."""
            try:
                from basic_pitch.inference import predict
                from basic_pitch import ICASSP_2022_MODEL_PATH
            except ImportError:
                print("[Editor] basic-pitch not installed, creating empty arrangements")
                arrangements = []
                for stem_path, stem_name in stem_files:
                    arr_name = stem_name.replace(".ogg", "")
                    string_count = 4 if arr_name == "bass" else 6
                    tuning = [-4, -9, -14, -19] if string_count == 4 else [0, 0, 0, 0, 0, 0]
                    arr = Arrangement(
                        name=arr_name.capitalize(),
                        tuning=tuning,
                        capo=0
                    )
                    arrangements.append(arr)
                return arrangements

            arrangements = []

            for stem_path, stem_name in stem_files:
                arr_name = stem_name.replace(".ogg", "")
                if arr_name not in ["guitar", "bass", "full"]:
                    continue

                string_count = 4 if arr_name == "bass" else 6

                try:
                    print(f"Predicting MIDI for {stem_path}...")
                    # Verify file exists before basic-pitch
                    if not Path(stem_path).exists():
                        raise FileNotFoundError(f"Stem file does not exist: {stem_path}")
                    print(f"[Editor] File verified, size: {Path(stem_path).stat().st_size} bytes")
                    # Run basic-pitch
                    model_output, midi_data, note_events = predict(
                        str(stem_path),
                        ICASSP_2022_MODEL_PATH
                    )

                    # Convert to notes
                    notes = _midi_events_to_notes(note_events, string_count)

                    name = arr_name.capitalize() if arr_name != "full" else "Lead"
                    tuning = [-4, -9, -14, -19] if string_count == 4 else [0, 0, 0, 0, 0, 0]
                    arr = Arrangement(
                        name=name,
                        tuning=tuning,
                        capo=0,
                        notes=notes
                    )
                    arrangements.append(arr)

                    print(f"[Editor] Transcribed {len(notes)} notes from {stem_name}")
                except Exception as e:
                    print(f"[Editor] Failed to transcribe {stem_name}: {e}")

            return arrangements

        audio_url = data.get("audio_url", "")
        split_stems = data.get("split_stems", True)
        transcribe_notes = data.get("transcribe_notes", True)
        title = data.get("title", "Untitled")
        artist = data.get("artist", "Unknown")
        album = data.get("album", "")
        year_str = data.get("year", "")
        start_time = data.get("start_time")
        end_time = data.get("end_time")
        if start_time is not None:
            try:
                start_time = float(start_time)
            except (ValueError, TypeError):
                start_time = None
        if end_time is not None:
            try:
                end_time = float(end_time)
            except (ValueError, TypeError):
                end_time = None
        if start_time is not None and end_time is not None and end_time <= start_time:
            return JSONResponse({"error": "end_time must be greater than start_time"}, 400)

        if not audio_url:
            return JSONResponse({"error": "Audio URL required"}, 400)

        # Resolve audio URL to local path
        audio_path = _resolve_storage_url(audio_url)
        if not audio_path or not audio_path.exists():
            return JSONResponse({"error": "Audio file not found"}, 400)

        def _transcribe():
            import subprocess  # Import at function start to avoid UnboundLocalError

            tmp = tempfile.mkdtemp(prefix="slopsmith_editor_transcribe_")

            # Create a temporary sloppak structure for stem splitting
            stems_dir = Path(tmp) / "stems"
            stems_dir.mkdir()

            # Copy audio to stems/full.ogg
            full_audio = stems_dir / "full.ogg"

            # Convert to OGG if needed, with optional time trimming
            try:
                if audio_path.suffix.lower() != ".ogg":
                    print(f"[Editor] Converting {audio_path} to OGG...")
                    cmd = ['ffmpeg', '-y', '-i', str(audio_path), '-c:a', 'libvorbis', '-q:a', '5']
                    if start_time is not None:
                        cmd.extend(['-ss', str(start_time)])
                    if end_time is not None:
                        duration = end_time - start_time if start_time is not None else end_time
                        cmd.extend(['-t', str(duration)])
                    cmd.append(str(full_audio))
                    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
                    print(f"[Editor] Conversion complete")
                else:
                    print(f"[Editor] Processing OGG file {audio_path}...")
                    # Already OGG — may need trimming
                    if start_time is not None or end_time is not None:
                        print(f"[Editor] Trimming OGG: start={start_time}s, end={end_time}s")
                        cmd = ['ffmpeg', '-y', '-i', str(audio_path), '-c:a', 'libvorbis', '-q:a', '5']
                        if start_time is not None:
                            cmd.extend(['-ss', str(start_time)])
                        if end_time is not None:
                            duration = end_time - start_time if start_time is not None else end_time
                            cmd.extend(['-t', str(duration)])
                        cmd.append(str(full_audio))
                        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
                    else:
                        shutil.copy2(audio_path, full_audio)
                    print(f"[Editor] OGG processing complete")

                if not full_audio.exists():
                    raise FileNotFoundError(f"Audio file was not created at {full_audio}")

                print(f"[Editor] Audio file ready at {full_audio} ({full_audio.stat().st_size} bytes)")
            except subprocess.CalledProcessError as e:
                print(f"[Editor] FFmpeg failed: {e.stderr}")
                raise
            except Exception as e:
                print(f"[Editor] Audio preparation failed: {e}")
                raise

            # Split stems if requested
            stem_files = []
            if split_stems and demucs_available():
                try:
                    # Create minimal manifest.yaml for stem splitting
                    import yaml
                    manifest = {
                        "title": title,
                        "artist": artist,
                        "stems": [{"id": "full", "file": "stems/full.ogg"}]
                    }
                    manifest_path = Path(tmp) / "manifest.yaml"
                    manifest_path.write_text(
                        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
                        encoding="utf-8"
                    )
                    print(f"[Editor] Created manifest.yaml for stem splitting")

                    # Split stems using Demucs (no progress callback needed)
                    split_sloppak_stems(Path(tmp))
                    # Collect all stem files that Demucs produced
                    # htdemucs_6s can produce: guitar, bass, drums, vocals, piano, other
                    for stem_name in ["guitar.ogg", "bass.ogg", "piano.ogg", "drums.ogg", "vocals.ogg", "other.ogg"]:
                        stem_path = stems_dir / stem_name
                        if stem_path.exists():
                            stem_files.append((stem_path, stem_name))
                except Exception as e:
                    print(f"[Editor] Stem splitting failed: {e}")
                    # Fall back to full audio only
                    stem_files = [(full_audio, "full.ogg")]
            else:
                stem_files = [(full_audio, "full.ogg")]

            # Verify stem files exist after collection
            print(f"[Editor] Stem files collected: {len(stem_files)} files")
            for stem_path, stem_name in stem_files:
                exists = Path(stem_path).exists()
                print(f"[Editor] - {stem_name}: {'EXISTS' if exists else 'MISSING'} at {stem_path}")

            # Get audio duration
            try:
                result = subprocess.run([
                    'ffprobe', '-v', 'error',
                    '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1',
                    str(full_audio)
                ], capture_output=True, text=True, check=True)
                duration = float(result.stdout.strip())
            except Exception:
                duration = 180.0  # Fallback

            # Generate Rocksmith XML files directly from transcribed notes
            from lib.gp2rs import convert_file
            import xml.etree.ElementTree as XET

            # Default BPM for beat generation
            bpm = 120
            beat_duration = 60.0 / bpm

            def _detect_chords_from_notes(notes: list, chord_time_window: float = 0.05) -> list:
                """Cluster notes by temporal proximity into chords.
                Notes within `chord_time_window` of the first note in their cluster
                are grouped into the same chord.
                """
                sorted_notes = sorted(notes, key=lambda n: n.time)
                chords: list[list] = []
                for note in sorted_notes:
                    placed = False
                    for chord_notes in chords:
                        if abs(note.time - chord_notes[0].time) <= chord_time_window:
                            chord_notes.append(note)
                            placed = True
                            break
                    if not placed:
                        chords.append([note])
                # Convert to Chord objects (imported at module level)
                return [Chord(time=ch[0].time, chord_id=i, notes=ch, high_density=len(ch) >= 4)
                        for i, ch in enumerate(chords)]

            xml_paths = []

            # ── Pass 1: transcribe stems, collect only those with detected notes ──
            transcribed_stems = []  # [(stem_path, stem_name, arr_name, rs_notes, string_count, tuning, arr_lower, rs_chords)]

            for stem_idx, (stem_path, stem_name) in enumerate(stem_files):
                arr_name = stem_name.replace(".ogg", "")
                arr_lower = arr_name.lower()

                # Skip drums — percussive transcription needs different approach
                if arr_lower == "drums":
                    print(f"[Editor] Skipping drums stem (not yet supported for transcription)")
                    continue

                # Only transcribe piano when the piano.ogg stem is actually available
                if arr_lower == "piano" and not (stems_dir / "piano.ogg").exists():
                    print(f"[Editor] Skipping piano: piano.ogg not available from stem split")
                    continue

                # Determine instrument type and string count
                if arr_lower == "bass":
                    string_count = 4
                    tuning = [-4, -9, -14, -19]  # 4-string bass E standard
                elif arr_lower == "piano":
                    string_count = 6  # Piano uses MIDI encoding (string = midi//24)
                    tuning = [0, 0, 0, 0, 0, 0]
                else:  # guitar and other melodic instruments
                    string_count = 6
                    tuning = [0, 0, 0, 0, 0, 0]

                # Transcribe notes for this stem
                rs_notes = []
                rs_chords: list = []
                if transcribe_notes:
                    print(f"[Editor] Transcribing {stem_name}...")
                    note_events = _transcribe_with_basic_pitch(str(stem_path))
                    if arr_lower == "piano":
                        # Piano uses MIDI encoding: string=midi//24, fret=midi%24
                        rs_notes = _midi_events_to_piano_notes(note_events)
                    else:
                        rs_notes = _midi_events_to_notes(note_events, string_count)
                    print(f"[Editor] Transcribed {len(rs_notes)} notes from {stem_name}")

                    # Detect chords from guitar stem (rhythm guitar) only
                    if arr_lower == "guitar" and rs_notes:
                        rs_chords = _detect_chords_from_notes(rs_notes)
                        print(f"[Editor] Detected {len(rs_chords)} chords from {stem_name}")

                # Only keep stems where transcription actually detected notes
                if not rs_notes:
                    print(f"[Editor] No notes detected from {stem_name}, skipping")
                    continue

                transcribed_stems.append((stem_path, stem_name, arr_name, rs_notes, string_count, tuning, arr_lower, rs_chords))

            # ── Pass 2: generate XML only for stems with detected notes ──
            for stem_path, stem_name, arr_name, rs_notes, string_count, tuning, arr_lower, rs_chords in transcribed_stems:
                # Guitar → "Rhythm" (RS convention: rhythm guitar = rhythm arrangement)
                display_name = "Rhythm" if arr_lower == "guitar" else arr_name.capitalize()

                # Generate beats (4/4 time)
                num_beats = max(4, int(duration / beat_duration) + 1)
                beats = []
                measure_num = 1
                for i in range(num_beats):
                    t = 0.5 + i * beat_duration  # Start at 0.5 like RS XMLs
                    is_measure_start = (i % 4 == 0)
                    beats.append(Beat(time=t, measure=measure_num if is_measure_start else -1))
                    if is_measure_start and i > 0:
                        measure_num += 1

                # Create XML
                root = XET.Element("song", version="7")
                XET.SubElement(root, "title").text = title
                XET.SubElement(root, "arrangement").text = display_name
                XET.SubElement(root, "offset").text = "0.000"
                XET.SubElement(root, "songLength").text = f"{duration:.3f}"
                XET.SubElement(root, "startBeat").text = "0.500"
                XET.SubElement(root, "averageTempo").text = str(bpm)
                XET.SubElement(root, "artistName").text = artist
                XET.SubElement(root, "albumName").text = album or ""
                XET.SubElement(root, "albumYear").text = year_str or ""

                # Tuning
                tuning_elem = XET.SubElement(root, "tuning")
                for i in range(6):  # Always 6 strings for RS XML
                    tuning_elem.set(f"string{i}", str(tuning[i] if i < len(tuning) else 0))
                XET.SubElement(root, "capo").text = "0"

                # Beats
                ebeats = XET.SubElement(root, "ebeats", count=str(len(beats)))
                for beat in beats:
                    XET.SubElement(ebeats, "ebeat", time=f"{beat.time:.3f}", measure=str(beat.measure))

                # Sections
                sections_elem = XET.SubElement(root, "sections", count="1")
                XET.SubElement(sections_elem, "section", name="default", number="1", startTime="0.000")

                # Phrases
                phrases_elem = XET.SubElement(root, "phrases", count="1")
                XET.SubElement(phrases_elem, "phrase", disparity="0", ignore="0", maxDifficulty="0", name="default", solo="0")

                # Phrase iterations
                pi_elem = XET.SubElement(root, "phraseIterations", count="1")
                XET.SubElement(pi_elem, "phraseIteration", time="0.000", phraseId="0")

                # Chord templates (empty for now)
                XET.SubElement(root, "chordTemplates", count="0")

                # Levels with notes
                levels_elem = XET.SubElement(root, "levels", count="1")
                level = XET.SubElement(levels_elem, "level", difficulty="0")

                # Notes
                notes_elem = XET.SubElement(level, "notes", count=str(len(rs_notes)))
                for note in rs_notes:
                    XET.SubElement(notes_elem, "note",
                                   time=f"{note.time:.3f}",
                                   string=str(note.string),
                                   fret=str(note.fret),
                                   sustain=f"{note.sustain:.3f}")

                # Chords
                if rs_chords:
                    chords_elem = XET.SubElement(level, "chords", count=str(len(rs_chords)))
                    for ch_idx, chord in enumerate(rs_chords):
                        chord_el = XET.SubElement(chords_elem, "chord",
                                                   time=f"{chord.time:.3f}",
                                                   chordId=str(ch_idx),
                                                   highDensity="1" if chord.high_density else "0",
                                                   strum="down")
                        for cn in chord.notes:
                            XET.SubElement(chord_el, "chordNote",
                                           time=f"{cn.time:.3f}",
                                           string=str(cn.string),
                                           fret=str(cn.fret),
                                           sustain=f"{cn.sustain:.3f}",
                                           bend="0",
                                           hammerOn="0", pullOff="0",
                                           slideTo="-1", slideUnpitchTo="-1",
                                           harmonic="0", harmonicPinch="0",
                                           palmMute="1" if cn.palm_mute else "0",
                                           mute="1" if cn.mute else "0",
                                           accent="1" if cn.accent else "0",
                                           tap="1" if cn.tap else "0",
                                           tremolo="1" if cn.tremolo else "0",
                                           pullOffGraceNotes="-1",
                                           slideGraceNotes="-1")
                else:
                    XET.SubElement(level, "chords", count="0")

                # Anchors (minimal)
                anchors_elem = XET.SubElement(level, "anchors", count="1")
                XET.SubElement(anchors_elem, "anchor", time="0.000", fret="1", width="4")

                # Hand shapes (empty)
                XET.SubElement(level, "handShapes", count="0")

                # Write XML
                xml_filename = f"{display_name}_arr.xml"
                # Internal name: guitar → Rhythm (matching display name)
                internal_name = display_name
                xml_path = Path(tmp) / xml_filename
                tree = XET.ElementTree(root)
                XET.indent(tree, space="  ")
                tree.write(xml_path, encoding="utf-8", xml_declaration=True)
                xml_paths.append(str(xml_path))
                print(f"[Editor] Generated XML: {xml_path} with {len(rs_notes)} notes")

            # Parse XMLs into Song object for the editor
            from lib.song import parse_arrangement
            song = Song()
            song.title = title
            song.artist = artist
            song.album = album
            if year_str:
                try:
                    song.year = int(year_str)
                except ValueError:
                    pass
            song.song_length = duration
            song.beats = beats
            song.sections = [Section(name="default", number=1, start_time=0.0)]

            # Parse the XML files we just created
            for xml_path in xml_paths:
                arr = parse_arrangement(xml_path)
                song.arrangements.append(arr)

            result = _song_to_dict(song, audio_url)
            return result, tmp, xml_paths

        try:
            result, session_dir, xml_files = await asyncio.get_event_loop().run_in_executor(None, _transcribe)
        except Exception as e:
            log.exception("transcribe failed for %r", data.get("filename", ""))
            return JSONResponse({"error": str(e)}, 500)

        session_id = f"transcribe_{re.sub(r'[^a-z0-9]', '', (title or 'new').lower())[:30]}"
        if session_id in sessions:
            old = sessions[session_id]
            shutil.rmtree(old["dir"], ignore_errors=True)

        sessions[session_id] = {
            "dir": session_dir,
            "audio_file": None,
            "filename": "",
            "xml_files": xml_files,  # Now includes the XMLs!
            "create_mode": True,
            "gp_path": str(Path(session_dir) / "transcribed.gp5"),  # Store GP path
            "metadata": {
                "title": title, "artist": artist,
                "album": album, "year": year_str,
            },
            "last_touched": time.time(),
            "_version": 0,
        }

        result["session_id"] = session_id
        result["create_mode"] = True
        return result
