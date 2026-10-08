"""Keep study work made by older versions when its source hasn't changed since.

1.6.1 started fingerprinting the original file behind study notes and picture descriptions, and 1.7.0 started
binding extracted text and transcripts to the exact bytes they came from. Work saved before that has no such
record, so it was all treated as outdated: notes and descriptions had to be made again, and recordings were
transcribed again from scratch. Most of it is still right, and it is carried over here when that can be shown
from what the older versions did record:

- Study notes (document, module and course): the course text is word for word what the notes were written from
  (1.6.0 kept a fingerprint of it), and the original file hasn't been touched since the notes were saved.
- Picture descriptions: the original is the same size as when it was described (all 1.6.0 kept) and hasn't been
  touched since before the day it was described. One described on the day its file arrived stays outdated:
  a date alone can't show which came first.
- Transcripts: the recording has the same size and modification time as when it was transcribed, and hasn't
  been touched since the transcript was written. It is then fingerprinted and the transcript bound to it.

"Touched" uses the file's change time, which every write, rename or permission change updates and which apps
can't set back. Anything that doesn't meet these rules stays outdated, as before.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .library import LibraryPathError
from .util import atomic_write_json, atomic_write_text, file_digest, front_matter, parse_front_matter, relative_posix

_LEGACY_FP = re.compile(r"[0-9a-f]{16}")


def _changed_at(path: Path) -> Optional[float]:
    try:
        return path.stat().st_ctime
    except OSError:
        return None


def _local_time(value: str) -> Optional[float]:
    """A saved local time (1.6.0 notes used datetime.now().isoformat()) as a timestamp."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment.timestamp() if moment.tzinfo is None else moment.astimezone().timestamp()


def _legacy_text_fp(sidecar: Path) -> str:
    """The 1.6.0 study-notes fingerprint: the document's words only."""
    from .compact import for_index
    from .describe import strip_blocks

    _, body = parse_front_matter(sidecar.read_text(encoding="utf-8"))
    body = for_index(strip_blocks(body))
    body = "\n".join(ln for ln in body.splitlines() if not ln.startswith("# ")).strip()
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def _sidecar(lib, rel: str) -> Optional[Path]:
    path = lib.resolve(rel if rel.endswith(".md") else rel + ".md")
    return path if path is not None and path.is_file() else None


def _unchanged_since(lib, sidecar: Path, legacy_fp: str, saved_at: Optional[float]) -> bool:
    """The text is what an older version fingerprinted, and its original hasn't been touched since `saved_at`."""
    from .index import material_status

    if saved_at is None or not _LEGACY_FP.fullmatch(legacy_fp or ""):
        return False
    try:
        if _legacy_text_fp(sidecar) != legacy_fp:
            return False
        meta, _ = parse_front_matter(sidecar.read_text(encoding="utf-8"))
        original = lib.checked(sidecar.with_name(sidecar.name[:-3]))
        if original.is_file():
            changed = _changed_at(original)
            if changed is None or changed >= saved_at:
                return False
        elif meta.get("source_file"):
            return False
        return material_status(lib, relative_posix(sidecar, lib.root))["status"] == "current"
    except (OSError, UnicodeDecodeError, LibraryPathError, ValueError):
        return False


def study_notes(lib) -> int:
    """Re-stamp study notes saved before 1.6.1 whose sources are unchanged. Returns how many were carried over."""
    from . import notes

    try:
        store = notes.load(lib)
    except LibraryPathError:
        return 0
    updates: Dict[str, Dict[str, Any]] = {}
    for key, entry in store.items():
        if not isinstance(entry, dict):
            continue
        saved_at = _local_time(entry.get("date", ""))
        if entry.get("kind") == "document" and _LEGACY_FP.fullmatch(str(entry.get("fp") or "")):
            sidecar = _sidecar(lib, key)
            if sidecar and _unchanged_since(lib, sidecar, entry["fp"], saved_at):
                updates[key] = {"fp": notes.fingerprint(sidecar, lib)}
        elif entry.get("kind") in ("module", "course") and isinstance(entry.get("sources"), dict):
            sources = dict(entry["sources"])
            for rel, fp in entry["sources"].items():
                if not _LEGACY_FP.fullmatch(str(fp or "")):
                    continue
                sidecar = _sidecar(lib, rel)
                if sidecar and _unchanged_since(lib, sidecar, fp, saved_at):
                    sources[rel] = notes.fingerprint(sidecar, lib)
            if sources != entry["sources"]:
                updates[key] = {"sources": sources}
    if not updates:
        return 0
    store = notes.load(lib)            # read again just before writing, and only change what is still as read
    changed = 0
    for key, update in updates.items():
        entry = store.get(key)
        if not isinstance(entry, dict):
            continue
        if "fp" in update and _LEGACY_FP.fullmatch(str(entry.get("fp") or "")) and update["fp"]:
            entry["fp"] = update["fp"]
            changed += 1
        elif "sources" in update and isinstance(entry.get("sources"), dict) and set(entry["sources"]) == set(update["sources"]):
            entry["sources"] = {rel: (update["sources"][rel] if _LEGACY_FP.fullmatch(str(fp or "")) else fp)
                                for rel, fp in entry["sources"].items()}
            changed += 1
    if changed:
        atomic_write_json(notes._store_path(lib), store)
    return changed


def descriptions(lib) -> int:
    """Re-stamp picture descriptions saved before 1.6.1 whose originals are unchanged, and put them back into the
    text versions. Returns how many were carried over."""
    from . import describe

    try:
        store = describe.load(lib)
    except LibraryPathError:
        return 0
    adopted: Dict[str, Dict[str, str]] = {}
    for rel, entries in store.items():
        if not isinstance(entries, dict):
            continue
        legacy = {unit: e for unit, e in entries.items()
                  if isinstance(e, dict) and not e.get("sha256") and e.get("text") and isinstance(e.get("size"), int)}
        if not legacy:
            continue
        try:
            original = lib.checked(lib.root / rel)
            if not original.is_file():
                continue
            st = original.stat()
            touched = date.fromtimestamp(st.st_ctime)
            digest = ""
            for unit, entry in legacy.items():
                try:
                    described = date.fromisoformat(entry.get("date", ""))
                except (TypeError, ValueError):
                    continue
                if entry["size"] == st.st_size and touched < described:
                    digest = digest or lib.digest(original)
                    adopted.setdefault(rel, {})[unit] = digest
        except (OSError, LibraryPathError, ValueError):
            continue
    if not adopted:
        return 0
    store = describe.load(lib)
    count = 0
    for rel, units in adopted.items():
        for unit, digest in units.items():
            entry = (store.get(rel) or {}).get(unit)
            if isinstance(entry, dict) and not entry.get("sha256"):
                entry["sha256"] = digest
                count += 1
    if count:
        atomic_write_json(describe._store_path(lib), store)
        for rel in adopted:
            try:
                original = lib.checked(lib.root / rel)
                sidecar = lib.checked(original.with_name(original.name + ".md"))
                if sidecar.is_file():
                    text = sidecar.read_text(encoding="utf-8")
                    atomic_write_text(sidecar, describe.apply(text, describe.current(lib, rel, original)))
            except (OSError, UnicodeDecodeError, LibraryPathError):
                continue
    return count


def bind_transcript(lib, recording: Path, transcript: Path, item: Dict[str, Any], stamp: Any) -> bool:
    """Bind a transcript made before 1.7.0 to its recording instead of transcribing it again, when the
    recording is provably the one that was transcribed. Returns True when the transcript was bound."""
    try:
        recording, transcript = lib.checked(recording), lib.checked(transcript)
        if (item.get("status") != "ok" or item.get("stale") or not stamp or item.get("stamp") != stamp
                or not recording.is_file() or not transcript.is_file()):
            return False
        text = transcript.read_text(encoding="utf-8")
        meta, _ = parse_front_matter(text)
        if meta.get("source_sha256") or meta.get("type") not in ("transcript", "feedback") or not text.startswith("---\n"):
            return False
        if re.search(r"^_(?:Not downloaded|Download failed|Couldn't)", text, re.M):
            return False
        written = transcript.stat().st_mtime
        recorded = _local_time(item.get("last_successful_sync", "").replace("Z", "+00:00"))
        written = min(written, recorded) if recorded else written
        before = recording.stat()
        if before.st_ctime >= written:
            return False
        digest = file_digest(recording)
        after = recording.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return False
        extra = front_matter({"source_file": recording.name, "source_sha256": digest})
        lines = extra.split("\n")[1:-3]               # the key lines, without the --- markers
        end = text.find("\n---", 4)
        atomic_write_text(transcript, text[:end] + "\n" + "\n".join(lines) + text[end:])
        return True
    except (OSError, UnicodeDecodeError, LibraryPathError):
        return False
