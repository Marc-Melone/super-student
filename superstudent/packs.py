"""Exam packs: copy the original slides, PDFs and lecture transcripts for some modules into one folder
that can be dragged into any Claude or ChatGPT chat or project."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Dict, Iterable, Optional, Set
from urllib.parse import unquote

from .extract import office_pdf, soffice_path
from .library import Library

ORIGINAL_EXT = (".pdf", ".pptx", ".docx", ".xlsx", ".png", ".jpg", ".jpeg")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")


def parse_range(text: str) -> Set[int]:
    nums: Set[int] = set()
    for part in (text or "").replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                nums.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            nums.add(int(part))
    return nums


def _href(rel: str) -> str:
    return rel.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def make_pack(lib, folder: str, modules: Optional[Iterable[int]] = None, name: str = "",
              out: Optional[Path] = None, to_pdf: bool = False) -> Dict[str, object]:
    course_dir = lib.checked(lib.root / folder)
    wanted = set(modules) if modules is not None else None
    if not name:
        if wanted:
            nums = sorted(wanted)
            name = f"Module {nums[0]}" if len(nums) == 1 else (
                f"Modules {nums[0]}-{nums[-1]}" if nums == list(range(nums[0], nums[-1] + 1))
                else "Modules " + ", ".join(str(n) for n in nums))
        else:
            name = "Whole course"
    target_root = Path(os.path.expanduser(str(out))) if out else lib.root / "_Exam Packs" / f"{Path(folder).name} - {name}"
    target_root = target_root.resolve() if out else lib.checked(target_root)
    export = Library(target_root)
    target_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    size = 0
    for extra in ("COURSE_OVERVIEW.md", "EXAM_INTEL.md", "Syllabus.md", "CALENDAR.md"):
        src = lib.checked(course_dir / extra)
        if src.exists():
            shutil.copy2(src, export.checked(target_root / extra))
            copied += 1
    notes_dir = lib.checked(course_dir / "Study Notes")
    course_notes = lib.checked(notes_dir / "_Course notes.md")
    if course_notes.exists():
        shutil.copy2(course_notes, export.checked(target_root / "Course notes (study pass).md"))
        copied += 1

    def put(src: Path, dest: Path) -> Path:
        """Copy one file into the pack (slides and Word files as PDFs when asked). Returns where it went."""
        nonlocal copied, size
        src, dest = lib.checked(src), export.checked(dest)
        export.checked(dest.with_suffix(".pdf"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        ext = src.suffix.lower()
        if to_pdf and ext in (".pptx", ".docx") and soffice_path():
            export.checked(dest.parent / (src.stem + ".pdf"))
            converted = office_pdf(src, dest.parent)
            if converted:
                size += converted.stat().st_size
                copied += 1
                return converted
        if to_pdf and ext == ".pptx":   # no LibreOffice (or an old one): draw the slides ourselves
            try:
                from .slide_render import deck_to_pdf

                made = deck_to_pdf(src, dest.with_suffix(".pdf"))
                size += made.stat().st_size
                copied += 1
                return made
            except Exception:
                pass
        shutil.copy2(src, dest)
        size += src.stat().st_size
        copied += 1
        return dest

    modules_dir = lib.checked(course_dir / "Modules")
    for mdir in sorted(modules_dir.iterdir()) if modules_dir.exists() else []:
        if not mdir.is_dir():
            continue
        mdir = lib.checked(mdir)
        num = mdir.name.split(" - ")[0]
        if wanted is not None and (not num.isdigit() or int(num) not in wanted):
            continue
        target = export.checked(target_root / mdir.name)
        module_notes = lib.checked(notes_dir / "Modules" / mdir.name)
        for src in sorted(module_notes.rglob("*.md")) if module_notes.exists() else []:
            dest = export.checked(target / "Study notes" / src.relative_to(module_notes))
            src = lib.checked(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            size += src.stat().st_size
            copied += 1
        for src in sorted(mdir.rglob("*")):
            if src.is_dir() or any(p.endswith(".assets") for p in src.parts[len(mdir.parts):-1]):
                continue
            ext = src.suffix.lower()
            keep = ext in ORIGINAL_EXT or (
                ext == ".md" and (src.name.endswith(("(lecture video).md", "(recording).md")) or src.name.startswith("YouTube - ")
                                  or src.name == "_Module Contents.md" or not src.with_suffix("").exists()))
            if keep:
                put(src, target / src.relative_to(mdir))
        _bring_linked_items(course_dir, mdir, target, put)
    return {"path": str(target_root), "name": target_root.name, "files": copied, "bytes": size}


def _bring_linked_items(course_dir: Path, mdir: Path, target: Path, put) -> None:
    """A module's contents list also links things kept elsewhere in the course (its quiz, assignment, a page or
    file in another folder). Copy those into the pack's 'Linked' folder and point the list at the copies, so
    nothing the module links to is missing from the pack."""
    contents = target / "_Module Contents.md"
    if not contents.exists():
        return
    course = course_dir.resolve()
    module = mdir.resolve()
    copies: Dict[Path, str] = {}

    def fix(m: "re.Match[str]") -> str:
        label, href = m.group(1), m.group(2)
        if "://" in href or href.startswith(("#", "mailto:")):
            return m.group(0)
        src = (mdir / unquote(href)).resolve()
        if module in src.parents or course not in src.parents or not src.is_file():
            return m.group(0)
        if src not in copies:
            made = put(src, target / "Linked" / src.name)
            copies[src] = made.relative_to(target).as_posix()
        return f"[{label}]({_href(copies[src])})"

    text = contents.read_text(encoding="utf-8")
    new = LINK.sub(fix, text)
    if new != text:
        contents.write_text(new, encoding="utf-8")
