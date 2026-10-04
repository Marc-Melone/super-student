"""Descriptions of diagrams, figures and pictures, written by the student's own AI (ChatGPT or Claude) and
kept with the library, so searches find a picture by what it shows even when it has no words in it.

The AI looks at a flagged page, slide or image (view_page), then saves a short description with
save_visual_description. Descriptions live in <library>/.superstudent/descriptions.json and are written into
the document's text version under that page or slide, so they're searched and read like any other text.
They survive re-syncs, and are set aside if the original file changes.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .util import REMOVED_DIR, atomic_write_json, atomic_write_text, file_digest, read_json
from .library import LibraryPathError

MARK = "> **What this shows**"
HEADING = re.compile(r"^## \[(Page \d+|Slide \d+|Image)\]")
VISUAL = re.compile(r"^> Visual content: (.+?)\. View")
WORTH = ("figure or picture", "chart or diagram", "no text layer", "scanned page", "this file is an image")
SKIP_DIRS = ("_Exam Packs", "_Study", "Study Notes", REMOVED_DIR)
MAX_CHARS = 2500


def _store_path(lib) -> Path:
    return lib.checked(lib.meta / "descriptions.json")


def load(lib) -> Dict[str, Dict[str, Dict[str, Any]]]:
    return read_json(_store_path(lib), {}) or {}


def _files(lib, rel: str) -> Optional[Tuple[str, Path, Path]]:
    """(library-relative original path, original, text version) for a file or its .md, inside the library."""
    path = lib.resolve(rel)
    if path is None:
        return None
    if path.suffix.lower() == ".md":
        original, sidecar = path.with_name(path.name[:-3]), path
    else:
        original, sidecar = path, path.with_name(path.name + ".md")
    try:
        original, sidecar = lib.checked(original), lib.checked(sidecar)
    except LibraryPathError:
        return None
    if not original.is_file() or not sidecar.is_file():
        return None
    return original.relative_to(lib.root.resolve()).as_posix(), original, sidecar


def visual_units(text: str) -> List[Tuple[str, str]]:
    """[(unit, reasons)] for each page, slide or image the text version flags as visual."""
    out: List[Tuple[str, str]] = []
    unit, since = "", 99
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            unit, since = m.group(1), 0
            continue
        since += 1
        if unit and since <= 3:
            v = VISUAL.match(line)
            if v:
                out.append((unit, v.group(1)))
                unit = ""
    return out


def worth_describing(reasons: str) -> bool:
    return any(w in reasons for w in WORTH)


def normalize_unit(where: str, original: Path) -> str:
    text = (where or "").strip().lower()
    ext = original.suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif") or text in ("", "image"):
        return "Image" if ext not in (".pdf", ".pptx") else ""
    num = re.search(r"\d+", text)
    if not num:
        return ""
    kind = "Slide" if (ext == ".pptx" or "slide" in text) else "Page"
    return f"{kind} {int(num.group())}"


def _clean(description: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", description or "")
    text = re.sub(r"\s+", " ", text).strip().lstrip("#>").strip()
    return text[:MAX_CHARS]


def strip_blocks(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith(MARK)) + ("\n" if text.endswith("\n") else "")


def apply(text: str, entries: Dict[str, Dict[str, Any]]) -> str:
    """Put each description right under its page/slide heading (after the visual-content note if there is one)."""
    lines = strip_blocks(text).splitlines()
    inserts: Dict[int, str] = {}
    for i, line in enumerate(lines):
        m = HEADING.match(line)
        if not m or m.group(1) not in entries:
            continue
        at = i + 1
        for j in range(i + 1, min(i + 4, len(lines))):
            if VISUAL.match(lines[j]):
                at = j + 1
                break
        e = entries[m.group(1)]
        inserts[at] = f"{MARK} (described by {e.get('by') or 'your AI'}, {e.get('date') or ''}): {e['text']}"
    out: List[str] = []
    for i, line in enumerate(lines):
        if i in inserts:
            out.append(inserts[i])
        out.append(line)
    if len(lines) in inserts:
        out.append(inserts[len(lines)])
    return "\n".join(out) + "\n"


def current(lib, rel_original: str, original: Path) -> Dict[str, Dict[str, Any]]:
    """Descriptions for a file that still match it (a changed file's old descriptions are set aside)."""
    entries = load(lib).get(rel_original) or {}
    try:
        digest = file_digest(lib.checked(original))
    except (OSError, LibraryPathError):
        return {}
    return {u: e for u, e in entries.items() if e.get("sha256") == digest and e.get("text")}


def reading_text(lib, sidecar: Path, text: str) -> str:
    """Hide obsolete descriptions even before the next sync rewrites a text version."""
    if MARK not in text:
        return text
    found = _files(lib, sidecar.relative_to(lib.root).as_posix())
    return apply(text, current(lib, found[0], found[1]) if found else {})


def move(lib, old_rel: str, new_rel: str) -> None:
    """Keep a file's descriptions when sync moves it (a renamed module, or content removed from Canvas)."""
    store = load(lib)
    moved = False
    for key in list(store):
        if key == old_rel or key.startswith(old_rel + "/"):      # the file, or files inside an unzipped folder
            store[new_rel + key[len(old_rel):]] = store.pop(key)
            moved = True
    if moved:
        atomic_write_json(_store_path(lib), store)


def merge_into(lib, original: Path, body: str) -> str:
    """Used by sync when it (re)writes a file's text version."""
    try:
        rel = original.resolve().relative_to(lib.root.resolve()).as_posix()
    except ValueError:
        return body
    entries = current(lib, rel, original)
    return apply(body, entries)


def save(lib, rel: str, where: str, description: str, by: str = "") -> Dict[str, Any]:
    try:
        return _save(lib, rel, where, description, by)
    except LibraryPathError as exc:
        return {"ok": False, "message": str(exc)}


def _save(lib, rel: str, where: str, description: str, by: str = "") -> Dict[str, Any]:
    from .index import update_index

    found = _files(lib, rel)
    if not found:
        return {"ok": False, "message": f"'{rel}' isn't a document in the library (use the path from search or "
                                        "list_undescribed_visuals)."}
    rel_original, original, sidecar = found
    unit = normalize_unit(where, original)
    text = sidecar.read_text(encoding="utf-8")
    units = {u for u, _ in visual_units(text)} | {m.group(1) for m in map(HEADING.match, text.splitlines()) if m}
    if not unit or unit not in units:
        return {"ok": False, "message": f"'{where}' isn't a page, slide or image of {original.name}. Use the unit "
                                        "exactly as listed, e.g. 'Slide 12', 'Page 3' or 'Image'."}
    clean = _clean(description)
    if len(clean) < 20:
        return {"ok": False, "message": "The description is too short to be useful. Say what the picture shows "
                                        "and name every labeled part."}
    store = load(lib)
    store.setdefault(rel_original, {})[unit] = {
        "text": clean, "by": _clean(by)[:40] or "your AI", "date": date.today().isoformat(),
        "size": original.stat().st_size, "sha256": file_digest(original),
    }
    lib.ensure()
    atomic_write_json(_store_path(lib), store)
    atomic_write_text(sidecar, apply(text, current(lib, rel_original, original)))
    try:
        update_index(lib)
    except Exception:
        pass
    return {"ok": True, "path": rel_original, "unit": unit}


def _sidecars(lib, course: str = "") -> Iterable[Tuple[str, Path, Path]]:
    root = lib.root
    wanted = (course or "").strip().lower()
    for sidecar in sorted(root.rglob("*.md")):
        rel = sidecar.relative_to(root).as_posix()
        parts = rel.split("/")
        if parts[0].startswith(".") or any(p in SKIP_DIRS or p.endswith(".assets") for p in parts[:-1]):
            continue
        if wanted and wanted not in "/".join(parts[:2]).lower():
            continue
        found = _files(lib, rel)
        if found and found[1].suffix and found[1].suffix.lower() != ".svg":
            yield found


def survey(lib, course: str = "", limit: int = 0) -> Dict[str, Any]:
    """How many pictures are worth describing, how many are described, and (with limit) the next ones to do."""
    store = load(lib)
    total = described = 0
    todo: List[Dict[str, str]] = []
    for rel_original, original, sidecar in _sidecars(lib, course):
        try:
            text = sidecar.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "> Visual content:" not in text:
            continue
        units = [(u, r) for u, r in visual_units(text) if worth_describing(r)]
        if not units:
            continue
        try:
            digest = file_digest(original)
        except OSError:
            continue
        done = {u for u, e in (store.get(rel_original) or {}).items()
                if e.get("sha256") == digest and e.get("text")}
        for unit, reasons in units:
            total += 1
            if unit in done:
                described += 1
            elif limit and len(todo) < limit:
                todo.append({"path": rel_original, "unit": unit, "why": reasons})
    return {"visuals": total, "described": described, "todo": todo}
