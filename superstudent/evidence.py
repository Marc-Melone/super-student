"""Verify exam citations against current, original course text.

This establishes that a quoted passage exists at a precise source locator. It does not establish that an
answer follows from that passage, or that a model-generated explanation is correct. Generated study notes
and visual descriptions cannot serve as original evidence. Extracted text must be bound to the original
bytes used for extraction. Source fingerprints cover the course text, its adjacent original and source
archive when present, and availability, so saved practice is invalidated by source changes. Where a file
sits is not part of the fingerprint: sync carries saved citations along when it moves files.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from .describe import strip_blocks
from .index import _norm_loc, _split_sections, heading_locator, material_status, unique_locators
from .library import LibraryPathError
from .outline import GENERATED
from .util import (REMOVED_DIR, as_one_action, file_digest, is_within, known_digest, parse_front_matter, relative_posix,
                   remember_digest, settled)

MAX_CITATIONS = 30
MAX_REMEMBERED_SOURCES = 2000
# Parsed sources kept between requests. An entry is used only while every file it was read from (the text, its
# original, the course record and any source archive) has exactly the same signature, and only files that had
# settled are trusted this way (see util.stat_signature): the same rule the per-request cache already used.
_SOURCES: Dict[tuple, Dict[str, Any]] = {}
_SOURCES_LOCK = threading.Lock()
MAX_QUOTE_CHARS = 12000
EXCLUDED_PARTS = {"Study Notes", "_Study", "_Exam Packs", REMOVED_DIR}
EXCLUDED_TYPES = {"study_notes", "outline", "overview", "exam_intel", "calendar", "module", "links", "grades"}


class EvidenceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\\" in value or "\x00" in value:
        raise EvidenceError("invalid_path", "Use a nonempty library-relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in (".", "..") for p in value.split("/")):
        raise EvidenceError("invalid_path", "Use a library-relative path without traversal.")
    return path.as_posix()


def _stamp(lib, path: Path) -> tuple:
    checked = lib.checked(path)
    try:
        st = checked.stat()
    except FileNotFoundError:
        return (str(checked), None)
    return (str(checked), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _digest(lib, path: Path, source_cache=None) -> str:
    """Share a guarded digest when several sources came from the same archive. A digest from an earlier request
    is reused only while the file's signature (including the change time, which every write updates) is the
    same, and only if the file had settled when it was read (see util.settled)."""
    signature = _stamp(lib, path)
    key = ("digest", str(lib.root), str(path))
    cached = source_cache.get(key) if source_cache is not None else None
    if cached and cached["signature"] == signature:
        return cached["digest"]
    checked = lib.checked(path)
    store = lib.fingerprint_store()
    digest = known_digest(checked, signature[1:], store) if len(signature) > 2 else None
    if digest is None:
        ready = len(signature) > 2 and settled(signature[1:])       # judged before reading
        digest = file_digest(checked)
        if _stamp(lib, path) != signature:
            raise EvidenceError("changed_during_read", "The source changed during verification; retry using the latest course text.")
        if len(signature) > 2:
            remember_digest(checked, signature[1:], digest, store, read_settled=ready)
    if source_cache is not None:
        source_cache[key] = {"signature": signature, "digest": digest}
    return digest


def _state(lib, source_cache=None) -> Dict[str, Any]:
    """The shared course record view (reused until the record changes; never modified here)."""
    return lib.state_view()


def _course_dir(lib, course_folder: str, source_cache=None) -> Path:
    folder = _relative(course_folder)
    registered = {c.get("folder") for c in _state(lib, source_cache).get("courses", {}).values()}
    if folder not in registered:
        raise EvidenceError("invalid_course", "Choose the exact folder of one course in this library.")
    directory = lib.resolve(folder)
    if directory is None or not directory.is_dir() or relative_posix(directory, lib.root) != folder:
        raise EvidenceError("invalid_course", "That course folder is missing or resolves to another location.")
    return directory


def _within(path: Path, directory: Path) -> bool:
    return is_within(path, directory)


def _source(lib, course_folder: str, value: str, source_cache=None) -> Dict[str, Any]:
    directory = _course_dir(lib, course_folder, source_cache)
    rel = _relative(value)
    lexical = lib.root / rel
    if not _within(lexical, lib.root / course_folder):
        raise EvidenceError("outside_course", "The citation must belong to the selected course.")
    supplied_parts = tuple(relative_posix(lexical, lib.root / course_folder).split("/"))
    if any(p in EXCLUDED_PARTS or p.startswith(".") or p.endswith(".assets") for p in supplied_parts) or lexical.name in GENERATED:
        raise EvidenceError("generated_source", "Use original course material rather than notes, summaries or historical copies.")
    target = lib.resolve(rel)
    if target is None or not _within(target, directory):
        raise EvidenceError("outside_course", "The citation resolves outside the selected course.")
    if target.suffix.lower() != ".md":
        target = lib.resolve(rel + ".md")
    if target is None or not _within(target, directory):
        raise EvidenceError("outside_course", "The text version resolves outside the selected course.")
    if not target.is_file():
        raise EvidenceError("missing_source", "The original course text is missing.")
    canonical = relative_posix(target, lib.root)
    parts = tuple(relative_posix(target, directory).split("/"))
    if any(p in EXCLUDED_PARTS or p.startswith(".") or p.endswith(".assets") for p in parts) or target.name in GENERATED:
        raise EvidenceError("generated_source", "Use original course material rather than notes, summaries or historical copies.")
    unpacked = any(p.endswith(" (unzipped)") for p in supplied_parts[:-1] + parts[:-1])
    try:
        original_path = target.with_name(target.name[:-3])
        original = lib.checked(original_path)
        if not _within(original, directory):
            raise EvidenceError("outside_course", "The adjacent original resolves outside the selected course.")
        signature = (_stamp(lib, target), _stamp(lib, original_path), _stamp(lib, lib.state_path))
        # Whether this result may be reused by later requests: judged now, before anything is read.
        ready = all(len(stamp) == 2 or settled(stamp[1:]) for stamp in signature)
        cache_key = ("source", str(lib.root), course_folder, canonical)
        cached = source_cache.get(cache_key) if source_cache is not None else None
        shared = not cached and ready
        if shared:
            cached = _SOURCES.get(cache_key)        # from an earlier request, while every file is unchanged
        if cached and cached["signature"][:3] == signature and (not unpacked or cached.get("archive_path") is not None):
            archive_path = cached.get("archive_path")
            archive_stamp = (_stamp(lib, archive_path),) if archive_path is not None else ()
            if cached["signature"] == signature + archive_stamp and not (
                    shared and any(len(stamp) > 2 and not settled(stamp[1:]) for stamp in archive_stamp)):
                return cached["source"]
        raw = target.read_text(encoding="utf-8")
        meta, body = parse_front_matter(raw)
        if (meta.get("type") or "").lower() in EXCLUDED_TYPES:
            raise EvidenceError("generated_source", "A generated summary cannot serve as original evidence.")
        if unpacked and not (meta.get("source_archive") and meta.get("source_archive_sha256")):
            raise EvidenceError("unbound_extraction", "This unpacked file has no verified link to its source archive. Use its converted text version, or refresh the course to unpack it again before using this evidence.")
        has_original = original.is_file()
        if meta.get("source_file") and not has_original:
            raise EvidenceError("missing_original", "The original file is missing; refresh the course before using this evidence.")
        status = material_status(lib, canonical, state=_state(lib, source_cache))
        if status["status"] != "current" or status["stale"]:
            raise EvidenceError("unavailable_source", status["message"] or "The course source is not current.")
        if re.search(r"^_(?:Not downloaded|Download failed|Couldn't extract text)\b", body, re.M):
            raise EvidenceError("unavailable_source", "The source has no usable original text; refresh it first.")
        original_digest = ""
        if has_original:
            bound_digest = meta.get("source_sha256") or ""
            if not re.fullmatch(r"[0-9a-fA-F]{64}", bound_digest):
                raise EvidenceError("unbound_extraction", "The extracted text was not verified against its original file. Refresh the course to rebuild the text before using this evidence.")
            original_digest = _digest(lib, original, source_cache)
            if bound_digest.lower() != original_digest:
                raise EvidenceError("stale_extraction", "The original file changed after this text was extracted. Refresh the course to rebuild the text before using this evidence.")
        archive_path = None
        archive_digest = ""
        archive_status = {}
        if meta.get("source_archive") or meta.get("source_archive_sha256"):
            archive_rel = _relative(meta.get("source_archive"))
            archive_path = lib.root / archive_rel
            if not _within(archive_path, lib.root / course_folder):
                raise EvidenceError("outside_course", "The source archive must belong to the selected course.")
            if any(p in EXCLUDED_PARTS or p.startswith(".") or p.endswith(".assets") for p in relative_posix(archive_path, directory).split("/")):
                raise EvidenceError("generated_source", "Use a current original archive rather than notes or historical copies.")
            archive = lib.checked(archive_path)
            if not _within(archive, directory):
                raise EvidenceError("outside_course", "The source archive resolves outside the selected course.")
            if any(p in EXCLUDED_PARTS or p.startswith(".") or p.endswith(".assets") for p in relative_posix(archive, directory).split("/")):
                raise EvidenceError("generated_source", "Use a current original archive rather than notes or historical copies.")
            if not archive.is_file():
                raise EvidenceError("missing_archive", "The source archive is missing. Refresh the course to restore and unpack it before using this evidence.")
            bound_archive = meta.get("source_archive_sha256") or ""
            if not re.fullmatch(r"[0-9a-fA-F]{64}", bound_archive):
                raise EvidenceError("unbound_extraction", "The extracted file was not verified against its source archive. Refresh the course to unpack it again before using this evidence.")
            archive_stamp = _stamp(lib, archive_path)
            if archive_stamp[0] != str(archive):
                raise EvidenceError("changed_during_read", "The source archive changed during verification; retry using the latest course text.")
            ready = ready and (len(archive_stamp) == 2 or settled(archive_stamp[1:]))
            signature += (archive_stamp,)
            archive_status = material_status(lib, archive_rel, state=_state(lib, source_cache))
            if archive_status["status"] != "current" or archive_status["stale"]:
                raise EvidenceError("unavailable_source", archive_status["message"] or "The source archive is not current.")
            archive_digest = _digest(lib, archive, source_cache)
            if bound_archive.lower() != archive_digest:
                raise EvidenceError("stale_extraction", "The source archive changed after this file was unpacked. Refresh the course to unpack it again before using this evidence.")
        # The fingerprint covers what the evidence depends on: every line of the course text (headings and
        # locators included), the original's exact bytes (which can change without the text version changing),
        # the source archive, and availability. It leaves out where the file sits and descriptive front matter
        # (title, module, dates), so a renamed or reordered module, or a picture description saved by the
        # study pass, doesn't invalidate practice; sync carries citations along when it moves files.
        content = {"v": 2, "text": hashlib.sha256(strip_blocks(body).encode("utf-8")).hexdigest(),
                   "original": original_digest, "status": status["status"]}
        # 1.7.0 fingerprints also covered the path, the whole file and the Canvas version stamp. They're still
        # accepted while nothing they covered has changed, so existing practice survives the upgrade.
        legacy = {"path": canonical, "text": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                  "original_path": relative_posix(original, lib.root) if has_original else "",
                  "original": original_digest,
                  "status": status["status"], "version": status["last_successful_version"]}
        if archive_path is not None:
            content.update(archive=archive_digest, archive_status=archive_status["status"])
            legacy.update(archive_path=archive_rel, archive=archive_digest,
                          archive_status=archive_status["status"], archive_version=archive_status["last_successful_version"])
        fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode("utf-8")).hexdigest()
        legacy_fingerprint = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode("utf-8")).hexdigest()
        sections = _split_sections(strip_blocks(body))
        names = unique_locators([name for name, _, _ in sections])
        locators: Dict[str, List[int]] = {}
        for i, name in enumerate(names):
            locators.setdefault(_norm_loc(name), []).append(i)
        source = {"path": canonical, "sections": sections, "names": names, "locators": locators,
                  "source_fingerprint": fingerprint, "legacy_fingerprint": legacy_fingerprint,
                  "source_status": status["status"]}
        after = (_stamp(lib, target), _stamp(lib, original_path), _stamp(lib, lib.state_path))
        if archive_path is not None:
            after += (_stamp(lib, archive_path),)
        if after != signature:
            raise EvidenceError("changed_during_read", "The source changed during verification; retry using the latest course text.")
        entry = {"signature": signature, "archive_path": archive_path, "source": source}
        if source_cache is not None:
            source_cache[cache_key] = entry
        if ready:
            with _SOURCES_LOCK:
                _SOURCES[cache_key] = entry
                while len(_SOURCES) > MAX_REMEMBERED_SOURCES:
                    _SOURCES.pop(next(iter(_SOURCES)))
    except LibraryPathError as exc:
        raise EvidenceError("outside_course", "The source resolves outside the library.") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceError("unreadable_source", "The course source cannot be read.") from exc
    return source


def source_fingerprint(lib, path: str, course_folder: str = "", source_cache: Optional[dict] = None) -> str:
    """Hash a current original course source; raise ValueError if it cannot support evidence.

    Pass the exact course folder when available. Otherwise the path must belong to exactly one registered
    course. Both an original filename and its Markdown text filename produce the same fingerprint. Pass a
    fresh ``source_cache={}`` for a single operation to hash/read shared sources once; never persist it.
    Cache entries are guarded by file identity, size, mtime and ctime, plus the current sync-state file
    and any source archive recorded when the original was unpacked.
    """
    return source_fingerprints(lib, path, course_folder, source_cache)[0]


def source_fingerprints(lib, path: str, course_folder: str = "", source_cache: Optional[dict] = None) -> tuple:
    """(current fingerprint, 1.7.0-format fingerprint) of a current original source; see source_fingerprint."""
    if not course_folder:
        rel = _relative(path)
        folders = {c.get("folder") for c in _state(lib, source_cache).get("courses", {}).values() if c.get("folder")}
        matches = [f for f in folders if rel.startswith(f + "/")]
        if len(matches) != 1:
            raise EvidenceError("invalid_course", "The source must belong to exactly one registered course.")
        course_folder = matches[0]
    source = _source(lib, course_folder, path, source_cache)
    return source["source_fingerprint"], source["legacy_fingerprint"]


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _validate(lib, course_folder: str, citations: List[Dict[str, Any]], recheck: bool,
              source_cache: Optional[dict] = None) -> Dict[str, Any]:
    # The implicit cache is scoped to this call; a caller may share one across an outer exam/list request.
    source_cache = {} if source_cache is None else source_cache
    result: Dict[str, Any] = {"valid": False, "course_folder": course_folder, "records": [], "errors": []}
    try:
        _course_dir(lib, course_folder, source_cache)
        if not isinstance(citations, list) or not citations or len(citations) > MAX_CITATIONS:
            raise EvidenceError("invalid_citations", f"Provide between one and {MAX_CITATIONS} citations.")
    except (EvidenceError, LibraryPathError) as exc:
        result["errors"].append({"index": None, "code": getattr(exc, "code", "invalid_course"), "message": str(exc)})
        return result
    for i, citation in enumerate(citations):
        try:
            if not isinstance(citation, dict):
                raise EvidenceError("invalid_citation", "Each citation must contain path, locator and quote.")
            path, locator, quote = (citation.get(k) for k in ("path", "locator", "quote"))
            if not isinstance(locator, str) or not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
                raise EvidenceError("invalid_citation", "Provide a locator and a nonempty quoted passage of at most 12000 characters.")
            source = _source(lib, course_folder, path, source_cache)
            if recheck:
                expected = citation.get("source_fingerprint")
                if not isinstance(expected, str) or not expected:
                    raise EvidenceError("missing_fingerprint", "Saved evidence is missing its original source fingerprint.")
                if expected not in (source["source_fingerprint"], source["legacy_fingerprint"]):
                    raise EvidenceError("changed_source", "The source changed after this evidence was saved; regenerate the practice item.")
            sections, names = source["sections"], source["names"]
            wanted = _norm_loc(heading_locator(locator))
            matches = source["locators"].get(wanted, [])
            if len(matches) != 1:
                raise EvidenceError("ambiguous_locator" if matches else "missing_locator",
                                    "Use one exact, unique section locator from the source.")
            j = matches[0]
            section = sections[j][1]
            if _normal(quote) not in _normal(section):
                raise EvidenceError("quote_mismatch", "The quoted passage does not occur in that source section.")
            result["records"].append({"path": source["path"], "locator": names[j], "quote": quote.strip(),
                                      "source_fingerprint": source["source_fingerprint"], "source_status": source["source_status"]})
        except (EvidenceError, LibraryPathError) as exc:
            result["errors"].append({"index": i, "code": getattr(exc, "code", "outside_course"), "message": str(exc)})
    result["valid"] = not result["errors"]
    return result


@as_one_action
def validate_evidence(lib, course_folder: str, citations: List[Dict[str, Any]],
                      source_cache: Optional[dict] = None) -> Dict[str, Any]:
    """Validate exact quoted citations against original, currently available text in one course.

    Validation is all-or-nothing via ``valid``. ``records`` holds individually valid citations and ``errors``
    identifies rejected indexes; callers must not accept a partial list when ``valid`` is false. A supplied
    cache must be a fresh dictionary for this operation; repeated sources are read/hashed/sectioned once.
    """
    return _validate(lib, course_folder, citations, recheck=False, source_cache=source_cache)


@as_one_action
def recheck_evidence(lib, course_folder: str, records: List[Dict[str, Any]],
                     source_cache: Optional[dict] = None) -> Dict[str, Any]:
    """Check saved validated records again, also requiring the original source fingerprints to match."""
    return _validate(lib, course_folder, records, recheck=True, source_cache=source_cache)
