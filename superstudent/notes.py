"""Study notes: the student's own AI studies every document once, in course order, and saves notes.

A person who studied well has notes on everything; answering "review everything for the midterm" from search
hits alone can miss slides. So the AI works through the course (read every page, look at every picture, save
notes that cite each slide or page), and Super Student keeps track: what's been studied, which slides or pages
the notes skipped, and which documents changed since. Module and course notes tie it together.

Notes live in <course>/Study Notes/ (searchable like everything else); the record is
<library>/.superstudent/notes.json.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .describe import strip_blocks
from .outline import NOTES_DIR, Doc, _load, course_documents, write_outline
from .util import atomic_write_json, atomic_write_text, file_digest, front_matter, parse_front_matter, read_json
from .library import Library, LibraryPathError

MIN_DOC, MIN_SUMMARY, MAX_CHARS = 150, 300, 200000
NUM = r"\d+(?:\s*(?:-|–|—|to)\s*\d+)?"
REF = re.compile(rf"\b(slides?|pages?|pp\.|p\.)\s*({NUM}(?:\s*(?:,|and|&|/)\s*{NUM})*)", re.I)
SHORT = re.compile(r"\[\s*([SP])\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*\]")


def _store_path(lib) -> Path:
    return lib.checked(lib.meta / "notes.json")


def load(lib) -> Dict[str, Dict[str, Any]]:
    return read_json(_store_path(lib), {}) or {}


def fingerprint(sidecar: Path, lib=None) -> str:
    """The document's content, ignoring its file name, title line and saved picture descriptions, so the same
    handout posted twice (or in two sections of a course) is recognised as the same."""
    from .compact import for_index

    try:
        sidecar = lib.checked(sidecar) if lib else sidecar
        meta, body = parse_front_matter(sidecar.read_text(encoding="utf-8"))
        original = sidecar.with_name(sidecar.name[:-3])
        original = lib.checked(original) if lib else original
        source = file_digest(original) if original.suffix and original.is_file() else ""
        if meta.get("source_file") and not source:
            source = "original missing"
        availability = meta.get("sync_status") or "current"
        if lib:
            from .index import material_status
            status = material_status(lib, sidecar.relative_to(lib.root).as_posix())["status"]
            if status != "current":
                availability = status
    except (OSError, UnicodeDecodeError, LibraryPathError):
        return ""
    body = for_index(strip_blocks(body))
    body = "\n".join(ln for ln in body.splitlines() if not ln.startswith("# ")).strip()
    return hashlib.sha256((body + "\n" + source + "\n" + availability).encode("utf-8")).hexdigest()


def _doc_fp(doc: Doc) -> str:
    return fingerprint(doc.sidecar, Library(doc.root) if doc.root else None)


def _sources(docs: List[Doc]) -> Dict[str, str]:
    return {d.rel: _doc_fp(d) for d in docs}


def _studied_copies(store: Dict[str, Any]) -> Dict[str, str]:
    """fingerprint -> path of a fully studied document with that content."""
    return {e["fp"]: rel for rel, e in store.items()
            if e.get("kind") == "document" and e.get("fp") and not e.get("missed")}


def course_dirs(lib, course: str = "") -> List[Tuple[Path, str]]:
    """[(course folder, label)] for courses in the library, optionally filtered by name, code or folder."""
    wanted = (course or "").strip().lower()
    out = []
    for c in lib.load_state().get("courses", {}).values():
        folder = c.get("folder")
        cdir = lib.resolve(folder) if folder else None
        if cdir is None or not cdir.is_dir():
            continue
        label = " - ".join(x for x in (c.get("code"), c.get("name")) if x) or folder
        if wanted and not any(wanted in (x or "").lower() for x in (folder, c.get("code"), c.get("name"))):
            continue
        out.append((cdir, label))
    return sorted(out, key=lambda t: str(t[0]))


def _course_of(lib, path: Path) -> Optional[Tuple[Path, str]]:
    for cdir, label in course_dirs(lib):
        if path == cdir or cdir in path.parents:
            return cdir, label
    return None


# ---------------------------------------------------------------- coverage

def referenced(notes: str) -> Dict[str, Set[int]]:
    """Slide and page numbers the notes cite: 'Slide 3', 'Slides 4-6, 9', 'p. 12', 'pp. 3–7', '[S12]'."""
    refs: Dict[str, Set[int]] = {"Slide": set(), "Page": set()}
    for word, nums in REF.findall(notes):
        kind = "Slide" if word.lower().startswith("slide") else "Page"
        for part in re.split(r"\s*(?:,|and|&|/)\s*", nums):
            m = re.match(r"(\d+)(?:\s*(?:-|–|—|to)\s*(\d+))?", part.strip())
            if m:
                a, b = int(m.group(1)), int(m.group(2) or m.group(1))
                if b >= a and b - a <= 500:
                    refs[kind].update(range(a, b + 1))
    for letter, a, b in SHORT.findall(notes):
        kind = "Slide" if letter.upper() == "S" else "Page"
        refs[kind].update(range(int(a), int(b or a) + 1))
    return refs


def content_units(doc: Doc) -> List[str]:
    return [u.name for u in doc.units if u.name.startswith(("Slide ", "Page ")) and (u.has_text or u.visual)]


def missed_units(doc: Doc, notes: str) -> List[str]:
    refs = referenced(notes)
    out = []
    for name in content_units(doc):
        kind, num = name.split()
        if int(num) not in refs[kind]:
            out.append(name)
    return out


def _compact(names: List[str]) -> str:
    if not names:
        return ""
    kind = names[0].split()[0]
    nums = sorted(int(n.split()[1]) for n in names)
    parts, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return f"{kind}s " + ", ".join(parts) if len(nums) > 1 else f"{kind} {nums[0]}"


# ---------------------------------------------------------------- status

def doc_status(store: Dict[str, Any], doc: Doc, copies: Optional[Dict[str, str]] = None) -> str:
    entry = store.get(doc.rel)
    if not entry:
        twin = (copies if copies is not None else _studied_copies(store)).get(_doc_fp(doc))
        return f"same content as {twin}, already studied" if twin and twin != doc.rel else "not studied"
    if not entry.get("fp") or entry["fp"] != _doc_fp(doc):
        return "changed since studied"
    if entry.get("missed"):
        return "skips " + _compact(entry["missed"])
    return "done"


def _summary_status(store: Dict[str, Any], key: str, docs: List[Doc]) -> str:
    entry = store.get(key)
    if not entry:
        return "not yet"
    if entry.get("sources") != _sources(docs):
        return "changed since studied"
    newest = max((store.get(d.rel, {}).get("date", "") for d in docs), default="")
    if newest > entry.get("date", "") or len(docs) > int(entry.get("docs") or 0):
        return "older than some document notes"
    if any(d.missing for d in docs):
        return "incomplete: some sources unavailable"
    return "done"


def progress(lib, course: str = "", limit: int = 8) -> Dict[str, Any]:
    store = load(lib)
    courses = []
    for cdir, label in course_dirs(lib, course):
        all_docs = course_documents(lib, cdir, include_light=True)
        docs = [d for d in course_documents(lib, cdir) if not d.missing]   # nothing to study in a locked file
        copies = _studied_copies(store)
        status = {d.rel: doc_status(store, d, copies) for d in docs}
        status = {rel: ("done" if st.startswith("same content") else st) for rel, st in status.items()}
        modules: Dict[str, List[Doc]] = {}
        for d in docs:
            if d.section.startswith("Modules/"):
                modules.setdefault(d.section, []).append(d)
        todo = [{"path": d.rel, "title": d.title, "status": status[d.rel], "section": d.section,
                 "units": len(content_units(d)), "pictures": len(d.visuals)}
                for d in docs if status[d.rel] != "done"]
        module_rows = []
        for section, mdocs in modules.items():
            key = f"{cdir.relative_to(lib.root.resolve()).as_posix()}/{section}"
            ready = all(status[d.rel] == "done" for d in mdocs)
            dependencies = [d for d in all_docs if d.section == section]
            module_rows.append({"path": key, "status": _summary_status(store, key, dependencies), "ready": ready})
        course_key = cdir.relative_to(lib.root.resolve()).as_posix()
        course_status = _summary_status(store, course_key, all_docs)
        courses.append({
            "folder": course_key, "label": label, "docs": len(docs),
            "done": sum(1 for s in status.values() if s == "done"),
            "partial": sum(1 for s in status.values() if s.startswith("skips")),
            "changed": sum(1 for s in status.values() if s.startswith("changed")),
            "todo": todo[:limit], "todo_total": len(todo), "modules": module_rows,
            "course_notes": course_status,
            "course_ready": not todo and all(m["status"] == "done" for m in module_rows),
        })
    return {"courses": courses,
            "docs": sum(c["docs"] for c in courses), "done": sum(c["done"] for c in courses)}


def status_fn(lib):
    store = load(lib)
    copies = _studied_copies(store)

    def status(rel: str) -> str:
        entry = store.get(rel)
        sidecar = lib.root.resolve() / rel
        sidecar = sidecar if sidecar.suffix == ".md" else sidecar.with_name(sidecar.name + ".md")
        try:
            sidecar = lib.checked(sidecar)
        except LibraryPathError:
            return "source unavailable"
        if not entry:
            twin = copies.get(fingerprint(sidecar, lib)) if sidecar.exists() else None
            return f"see notes for {twin} (same content)" if twin and twin != rel else "not studied"
        if not sidecar.exists() or not entry.get("fp") or entry["fp"] != fingerprint(sidecar, lib):
            return "changed since studied"
        return "skips " + _compact(entry["missed"]) if entry.get("missed") else "studied"
    return status


# ---------------------------------------------------------------- saving

def _clean(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text or "").replace("\r\n", "\n").strip()


def _notes_file(cdir: Path, rel_in_course: str, kind: str) -> Path:
    base = cdir / NOTES_DIR
    if kind == "course":
        return base / "_Course notes.md"
    if kind == "module":
        return base / rel_in_course / "_Module notes.md"
    return base / (rel_in_course + " - notes.md")


def save(lib, path: str, notes: str, by: str = "") -> Dict[str, Any]:
    try:
        return _save(lib, path, notes, by)
    except LibraryPathError as exc:
        return {"ok": False, "message": str(exc)}


def _save(lib, path: str, notes: str, by: str = "") -> Dict[str, Any]:
    from .index import update_index

    target = lib.resolve(path)
    if target is None or not target.exists():
        return {"ok": False, "message": f"'{path}' isn't in the library. Use a path from study_progress."}
    found = _course_of(lib, target)
    if not found:
        return {"ok": False, "message": "Study notes go with a course document, a module folder or a course folder."}
    cdir, label = found
    rel_in_course = target.relative_to(cdir).as_posix() if target != cdir else ""
    top = rel_in_course.split("/")[0] if rel_in_course else ""
    if top in (NOTES_DIR, "_Study", "_Exam Packs"):
        return {"ok": False, "message": "That's generated study material, not a course document."}
    text = _clean(notes)
    if len(text) > MAX_CHARS:     # never cut notes short silently: the coverage check would then be wrong
        return {"ok": False, "message": f"These notes are {len(text):,} characters; the most one save can hold is "
                                        f"{MAX_CHARS:,}. Tighten them (keep every slide or page reference) and save again."}
    by = re.sub(r"[^\w .'-]", "", by or "")[:40].strip() or "your AI"
    when = datetime.now().isoformat(timespec="microseconds")
    store = load(lib)
    missed: List[str] = []
    unavailable: List[str] = []
    if target.is_dir():
        kind = "course" if target == cdir else ("module" if target.parent.name == "Modules" else "")
        if not kind:
            return {"ok": False, "message": "Folders can only get module notes (Modules/NN - Name) or course notes."}
        if len(text) < MIN_SUMMARY:
            return {"ok": False, "message": "Module and course notes should tie everything together; these are too short."}
        docs = [d for d in course_documents(lib, cdir, include_light=True)
                if kind == "course" or d.section == rel_in_course]
        key = target.relative_to(lib.root.resolve()).as_posix()
        title = f"{label}: course notes" if kind == "course" else f"{target.name}: module notes"
        source_link = ""
        entry: Dict[str, Any] = {"kind": kind, "docs": len(docs), "sources": _sources(docs)}
        unavailable = [d.rel for d in docs if d.missing]
    else:
        sidecar = target if target.suffix == ".md" else target.with_name(target.name + ".md")
        sidecar = lib.checked(sidecar)
        if target.name in ("OUTLINE.md", "COURSE_OVERVIEW.md", "EXAM_INTEL.md", "CALENDAR.md", "GRADES.md",
                           "LINKS.md", "_Module Contents.md") or not sidecar.is_file():
            return {"ok": False, "message": "Save notes for a course document (a file, page, assignment or lecture)."}
        doc = _load(lib, sidecar, "")
        if doc is None:
            return {"ok": False, "message": "Couldn't read that document."}
        if doc.missing:
            return {"ok": False, "message": "This source is unavailable or stale. Update it before saving current study notes."}
        if len(text) < MIN_DOC:
            return {"ok": False, "message": "These notes are too short to stand in for having studied the document."}
        missed = missed_units(doc, text)
        key = doc.rel
        rel_in_course = Path(doc.rel).relative_to(cdir.relative_to(lib.root.resolve())).as_posix()
        kind = "document"
        title = f"Study notes: {doc.title}"
        source_link = f"[{Path(doc.rel).name}]({_relative_link(_notes_file(cdir, rel_in_course, kind), lib.root.resolve() / doc.rel)})"
        entry = {"kind": kind, "fp": fingerprint(sidecar, lib), "units": len(content_units(doc)), "missed": missed}
    out = lib.checked(_notes_file(cdir, rel_in_course, kind))
    _store_path(lib)  # reject an unsafe metadata destination before writing the visible notes
    intro = (f"_Written by {by} on {when[:10]}"
             + (f" after studying {source_link}" if source_link else "")
             + ". These are study notes, not course material: cite the original slides or pages, and check them "
               "before relying on a detail._")
    meta = {"title": title, "course": label, "type": "study_notes", "source": key, "written_by": by, "date": when[:10]}
    gaps = ("> **Source warning:** This summary is incomplete: some sources are unavailable or stale. "
            "Check Canvas before relying on it. Sources: " + ", ".join(unavailable) + "\n\n") if unavailable else ""
    atomic_write_text(out, front_matter(meta) + f"# {title}\n\n{intro}\n\n" + gaps + f"{text}\n")
    entry.update({"file": out.relative_to(lib.root.resolve()).as_posix(), "by": by, "date": when})
    store[key] = entry
    lib.ensure()
    atomic_write_json(_store_path(lib), store)
    try:
        write_outline(lib, cdir, label, status_fn(lib))
        update_index(lib)
    except Exception:
        pass
    message = f"Saved {kind} notes to {entry['file']}."
    if unavailable:
        message += " This summary is incomplete because some sources are unavailable or stale."
    if missed:
        message += (f" The notes don't mention {_compact(missed)}. Look at those, then save the notes again with "
                    "them included (cite each as 'Slide N' or 'Page N'), or say why they don't matter.")
    return {"ok": True, "file": entry["file"], "missed": missed, "kind": kind, "message": message}


def move(lib, old_rel: str, new_rel: str, exact: bool = False) -> None:
    """Keep study notes attached when sync moves a document or a module folder (library-relative paths)."""
    store = load(lib)
    changed = False
    root = lib.root.resolve()
    for key in list(store):
        if key != old_rel and (exact or not key.startswith(old_rel + "/")):
            continue
        entry = store.pop(key)
        new_key = new_rel + key[len(old_rel):]
        old_file = lib.checked(root / entry["file"]) if entry.get("file") else None
        new_target = lib.checked(root / new_key)
        found = _course_of(lib, new_target) if new_target.exists() else None
        if old_file is not None and old_file.is_file() and found:
            cdir, _ = found
            rel_in_course = (root / new_key).relative_to(cdir).as_posix()
            top = rel_in_course.split("/")[0]
            kind = entry.get("kind") or "document"
            if top not in (NOTES_DIR, "_Study", "_Exam Packs") and kind in ("document", "module"):
                new_file = lib.checked(_notes_file(cdir, rel_in_course, kind))
                if new_file != old_file and not new_file.exists():
                    new_file.parent.mkdir(parents=True, exist_ok=True)
                    old_file.replace(new_file)
                    entry["file"] = new_file.relative_to(root).as_posix()
                    parent = old_file.parent
                    while parent != cdir and cdir in parent.parents:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
        store[new_key] = entry
        changed = True
    if changed:
        atomic_write_json(_store_path(lib), store)


def _relative_link(from_file: Path, to_file: Path) -> str:
    import os

    rel = os.path.relpath(to_file, from_file.parent).replace(os.sep, "/")
    return rel.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
