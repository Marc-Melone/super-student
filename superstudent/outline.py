"""A course's complete table of contents: every module, document, slide and page in the instructor's order.

OUTLINE.md lets an AI see everything a review has to cover before it starts, so broad questions ("everything
for the midterm") are answered from the whole course rather than from whatever a search turned up.
The same walk (course_documents) tells the study pass which documents to study and in what order.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

from .util import front_matter, parse_front_matter
from .util import atomic_write_text

NOTES_DIR = "Study Notes"
HEADING = re.compile(r"^## \[(Slide \d+|Page \d+|Sheet: [^\]]+|\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$")
VISUAL = re.compile(r"^> Visual content: (.+?)\. View")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
GENERATED = {"COURSE_OVERVIEW.md", "EXAM_INTEL.md", "CALENDAR.md", "GRADES.md", "LINKS.md", "OUTLINE.md",
             "_Module Contents.md", "INDEX.md", "CLAUDE.md", "AGENTS.md", "README.md"}
STUDY_SECTIONS = ("Modules", "Syllabus", "Files", "Pages", "Assignments", "Media", "My Files")
LIGHT_SECTIONS = ("Announcements", "Discussions", "Quizzes")   # listed in the outline, studied at course level


@dataclass
class Unit:
    name: str            # "Slide 3", "Page 12", "Sheet: Model", "00:12:30"
    title: str = ""
    visual: str = ""     # reasons, if flagged as visual
    described: bool = False
    has_text: bool = True


@dataclass
class Doc:
    rel: str             # library-relative id: the original file, or the .md when there's no original
    sidecar: Path
    title: str
    section: str         # module folder ("Modules/03 - Week 3") or top folder ("Files", "Pages", ...)
    kind: str = ""
    units: List[Unit] = field(default_factory=list)
    missing: str = ""    # why there's nothing to read (not downloaded, locked, couldn't be read)

    @property
    def visuals(self) -> List[Unit]:
        return [u for u in self.units if u.visual]


def _doc_for(lib, sidecar: Path) -> Tuple[str, Path]:
    sidecar = sidecar.resolve()
    original = sidecar.with_name(sidecar.name[:-3])
    target = original if original.suffix and original.is_file() else sidecar
    return target.relative_to(lib.root.resolve()).as_posix(), sidecar


def parse_units(text: str) -> List[Unit]:
    units: List[Unit] = []
    lines = text.splitlines()
    first_lines: Dict[int, str] = {}
    for i, line in enumerate(lines):
        m = HEADING.match(line)
        if not m:
            continue
        unit = Unit(m.group(1), m.group(2).strip())
        body: List[str] = []
        for nxt in lines[i + 1:i + 40]:
            if nxt.startswith("## ["):
                break
            v = VISUAL.match(nxt)
            if v:
                unit.visual = v.group(1)
            elif nxt.startswith("> **What this shows**"):
                unit.described = True
            elif nxt.strip() and not nxt.startswith(">"):
                body.append(nxt.strip())
        body = [b for b in body if b not in ("(no text on this slide)", "(no extractable text on this page)")]
        unit.has_text = len(" ".join(body)) >= 25
        if unit.name.startswith("Page ") and body:
            first_lines[len(units)] = next((b for b in body if len(b.split()) >= 3), body[0])[:90]
        units.append(unit)
    # Pages have no titles: use each page's first line, skipping running headers repeated on many pages.
    if first_lines:
        counts = Counter(first_lines.values())
        repeated = {t for t, n in counts.items() if n >= 3 and n >= 0.3 * len(first_lines)}
        for idx, text_line in first_lines.items():
            if text_line not in repeated:
                units[idx].title = text_line
    return units


def _load(lib, sidecar: Path, section: str) -> Optional[Doc]:
    try:
        text = sidecar.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, body = parse_front_matter(text)
    rel, _ = _doc_for(lib, sidecar)
    title = meta.get("title") or sidecar.name[:-3]
    missing = ""
    m = re.search(r"^_(Not downloaded|Download failed|Couldn't extract text)[:.]?\s*(.*?)_?$", body, re.M)
    if m and len(body) < 2000:
        missing = (m.group(1) + (": " + m.group(2).rstrip("._") if m.group(2) else "")).strip()
    return Doc(rel, sidecar, title, section, (meta.get("type") or "").lower(), parse_units(body), missing)


QUIZ_REVIEW = "## Question review"
SKIP_PARTS = ("My Submissions",)       # the student's own work: searchable, but not course material to study


def _has_review(sidecar: Path) -> bool:
    """A quiz whose past questions came through: course material worth studying like a document."""
    try:
        return QUIZ_REVIEW in sidecar.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def course_documents(lib, course_dir: Path, include_light: bool = False) -> List[Doc]:
    """Every document in the course: module by module in the instructor's order, then everything else."""
    docs: List[Doc] = []
    seen = set()
    course_dir = course_dir.resolve()
    root = lib.root.resolve()

    def add(sidecar: Path, section: str) -> None:
        key = sidecar.resolve()
        if key in seen or not sidecar.is_file() or sidecar.name in GENERATED or root not in key.parents:
            return
        if any(p in SKIP_PARTS for p in sidecar.parts):
            return
        seen.add(key)
        try:
            doc = _load(lib, sidecar, section)
        except ValueError:        # a link pointing outside the library
            return
        if doc:
            docs.append(doc)

    def wanted(sidecar: Path, top: str) -> bool:
        return top in STUDY_SECTIONS or (include_light and top in LIGHT_SECTIONS) or (top == "Quizzes" and _has_review(sidecar))

    modules_dir = course_dir / "Modules"
    for mdir in sorted(p for p in modules_dir.iterdir() if p.is_dir()) if modules_dir.exists() else []:
        section = f"Modules/{mdir.name}"
        contents = mdir / "_Module Contents.md"
        if contents.exists():
            for _, target in LINK.findall(contents.read_text(encoding="utf-8")):
                if "://" in target:
                    continue
                path = (mdir / unquote(target)).resolve()
                sidecar = path if path.suffix == ".md" else path.with_name(path.name + ".md")
                try:
                    top = sidecar.relative_to(course_dir).parts[0]
                except ValueError:
                    continue
                inside = mdir in sidecar.parents
                if inside or wanted(sidecar, top):
                    add(sidecar, section)
        for sidecar in sorted(mdir.rglob("*.md")):      # anything in the module folder not linked above
            if not any(p.endswith(".assets") for p in sidecar.relative_to(mdir).parts[:-1]):
                add(sidecar, section)
    sections = STUDY_SECTIONS[1:] + (LIGHT_SECTIONS if include_light else ("Quizzes",))
    for top in sections:
        if top == "Syllabus":
            add(course_dir / "Syllabus.md", "Syllabus")
            continue
        folder = course_dir / top
        for sidecar in sorted(folder.rglob("*.md")) if folder.exists() else []:
            parts = sidecar.relative_to(folder).parts
            if any(p.endswith(".assets") or p.startswith(".") for p in parts[:-1]):
                continue
            if wanted(sidecar, top):
                add(sidecar, top)
    return docs


def _ranges(nums: List[int]) -> str:
    out: List[str] = []
    start = prev = None
    for n in sorted(set(nums)):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def _flag(unit: Unit) -> str:
    if not unit.visual:
        return ""
    reasons = unit.visual
    kind = "scan" if "scan" in reasons or "no text layer" in reasons else (
        "picture" if "picture" in reasons or "image" in reasons else (
            "chart" if "chart" in reasons else ("diagram" if "diagram" in reasons else "equations")))
    return f" [{kind}{', described' if unit.described else ''}]"


def write_outline(lib, course_dir: Path, label: str, notes_status=None) -> Path:
    """Write <course>/OUTLINE.md. `notes_status(rel) -> str` adds whether each document has study notes."""
    docs = course_documents(lib, course_dir, include_light=True)
    lines = [f"# {label}: everything in the course", "",
             "Every module, document, slide and page, in your instructor's order. Pictures are marked "
             "[picture], [chart], [diagram], [scan] or [equations] (look at those as images). Use this to make sure "
             "a review covers everything in scope. Study notes, where they exist: a document's are in "
             "`Study Notes/<its path in the course> - notes.md`, a module's in "
             "`Study Notes/Modules/<module folder>/_Module notes.md`, the course's in `Study Notes/_Course notes.md`.", ""]
    total_units = sum(len(d.units) for d in docs)
    total_visual = sum(len(d.visuals) for d in docs)
    lines.append(f"{len(docs)} documents, {total_units} slides/pages/sections, {total_visual} with pictures.")
    section = None
    for doc in docs:
        if doc.section != section:
            section = doc.section
            heading = section
            if section.startswith("Modules/"):
                name = section.split("/", 1)[1]
                heading = "Module " + re.sub(r"^0*(\d+) - ", r"\1: ", name)
            lines += ["", f"## {heading}", ""]
        status = "" if doc.missing else (notes_status(doc.rel) if notes_status else "")
        slides = [u for u in doc.units if u.name.startswith("Slide ")]
        pages = [u for u in doc.units if u.name.startswith("Page ")]
        stamps = [u for u in doc.units if re.match(r"\d", u.name)]
        size = (f"{len(slides)} slides" if slides else f"{len(pages)} pages" if pages else
                f"lecture, {len(stamps)} sections" if stamps else "")
        extra = ", ".join(x for x in (size, f"notes: {status}" if status else "", doc.missing) if x)
        lines.append(f"- **{doc.title}**" + (f" ({extra})" if extra else "") + f"  \n  `{doc.rel}`")
        if slides and len(slides) <= 80:
            for u in slides:
                lines.append(f"  - {u.name}{': ' + u.title if u.title else ''}{_flag(u)}")
        elif pages and len(pages) <= 40:
            for u in pages:
                lines.append(f"  - {u.name}{': ' + u.title if u.title else ''}{_flag(u)}")
        elif slides or pages:
            vis = [int(u.name.split()[1]) for u in (slides or pages) if u.visual]
            if vis:
                lines.append(f"  - {'Slides' if slides else 'Pages'} with pictures: {_ranges(vis)}")
    path = course_dir / "OUTLINE.md"
    meta = {"title": f"{label}: outline", "course": label, "type": "outline"}
    atomic_write_text(path, front_matter(meta) + "\n".join(lines) + "\n")
    return path
