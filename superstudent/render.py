"""Show the AI the real page: render a PDF page or slide as an image on demand."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Optional

from .extract import IMAGE_EXT, PDFLIKE_EXT, VIEWABLE_IMAGE_EXT, assets_dir_for, office_pdf, soffice_path
from .library import LibraryPathError
from .util import file_digest

OFFICE_EXT = {".pptx", ".ppt", ".pps", ".ppsx", ".odp", ".key", ".docx", ".doc", ".odt", ".rtf", ".xlsx", ".xls", ".ods"}
TARGET_LONG_EDGE = 1500  # Claude works best with images up to roughly this size


class RenderError(Exception):
    pass


def _original_for(path: Path) -> Path:
    if path.suffix.lower() == ".md":
        candidate = path.with_name(path.name[:-3])
        if candidate.exists():
            return candidate
    return path


def _converted_pdf(lib, source: Path) -> Optional[Path]:
    digest = hashlib.sha1(f"{source}|{file_digest(source)}|v2".encode()).hexdigest()[:16]
    cache = lib.checked(lib.meta / "converted")
    target = lib.checked(cache / f"{digest}.pdf")
    if target.exists():
        return target
    failed = lib.checked(cache / f"{digest}.failed")
    if failed.exists():                     # already tried: don't make every look wait for LibreOffice again
        return None
    conversion_dir = lib.checked(cache / digest)
    lib.checked(conversion_dir / (source.stem + ".pdf"))
    produced = office_pdf(source, conversion_dir)
    if not produced:
        cache.mkdir(parents=True, exist_ok=True)
        failed.write_text("LibreOffice couldn't make a matching PDF\n", encoding="utf-8")
        return None
    lib.checked(produced).replace(target)
    try:
        (cache / digest).rmdir()
    except OSError:
        pass
    return target


MODEL_LONG_EDGE = 1568   # AI models shrink bigger images to about this anyway; sending more only adds bytes


def _fit_image(lib, source: Path) -> Path:
    """The picture as the AI should see it: turned upright (phone photos are often stored sideways), in a format
    every AI app accepts, and no bigger than what the AI actually looks at (same detail, far fewer bytes)."""
    from PIL import Image, ImageOps

    ext = source.suffix.lower()
    try:
        im = Image.open(source)
    except Exception:
        converted = _with_sips(lib, source)       # HEIC photos from an iPhone, on a Mac
        if converted is None:
            raise RenderError(f"Can't show '{ext}' pictures here; open the original instead.")
        return converted
    with im:
        orientation = im.getexif().get(0x0112, 1) if hasattr(im, "getexif") else 1
        if ext in VIEWABLE_IMAGE_EXT and max(im.size) <= MODEL_LONG_EDGE and orientation in (0, 1):
            return source
        key = hashlib.sha1(f"{source}|{file_digest(source)}|fit2".encode()).hexdigest()[:16]
        jpeg = ext in (".jpg", ".jpeg", ".heic", ".heif") or im.mode == "CMYK"
        out = lib.checked(lib.renders / f"{key}-fit{'.jpg' if jpeg else '.png'}")
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            im.seek(0)
            pic = ImageOps.exif_transpose(im)
            pic = pic.convert("RGB") if jpeg else (pic.convert("RGBA") if pic.mode not in ("RGB", "RGBA", "L", "LA") else pic)
            pic.thumbnail((MODEL_LONG_EDGE, MODEL_LONG_EDGE))
            pic.save(out, quality=92) if jpeg else pic.save(out)
        return out


def _with_sips(lib, source: Path) -> Optional[Path]:
    import shutil
    import subprocess

    if not shutil.which("sips"):
        return None
    key = hashlib.sha1(f"{source}|{file_digest(source)}|sips".encode()).hexdigest()[:16]
    out = lib.checked(lib.renders / f"{key}-fit.jpg")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(MODEL_LONG_EDGE), str(source), "--out", str(out)],
                             capture_output=True, timeout=60)
        if res.returncode != 0 or not out.exists():
            return None
    return out


def locate(lib, rel_path: str) -> Optional[Path]:
    """A library file from a path as the AI may give it: library-relative, %-encoded as in a Markdown link, or a
    link copied out of a document ('Week%202.mp4.assets/screen-00-12-30.jpg'), matched by its ending."""
    from urllib.parse import unquote

    for candidate in (rel_path, unquote(rel_path)):
        path = lib.resolve(candidate)
        if path is not None and path.exists():
            return path
    tail = unquote(rel_path).replace("\\", "/").lstrip("./").strip("/")
    if "/" not in tail or ".." in tail.split("/"):
        return None
    name = tail.rsplit("/", 1)[1]
    root = lib.root.resolve()
    matches = []
    for hit in lib.root.rglob(name):
        rel = hit.relative_to(lib.root).as_posix()
        if rel.endswith("/" + tail) and not any(p.startswith(".") for p in hit.relative_to(lib.root).parts):
            if root in hit.resolve().parents:
                matches.append(hit)
        if len(matches) > 1:
            break
    return matches[0] if len(matches) == 1 else None


DRAWN_SUFFIX = "-drawn.png"   # slides drawn by Super Student itself (no LibreOffice): layout exact, styling approximate


def _drawn_slide(lib, source: Path, page: int, long_edge: int) -> Path:
    from .slide_render import render_slide

    key = hashlib.sha1(f"{source}|{file_digest(source)}|{page}|{long_edge}|v1".encode()).hexdigest()[:16]
    out = lib.checked(lib.renders / f"{key}-s{page}{DRAWN_SUFFIX}")
    if out.exists():
        return out
    try:
        return render_slide(source, page, out, long_edge=long_edge)
    except ValueError as exc:
        raise RenderError(str(exc))
    except Exception as exc:
        raise RenderError(f"Couldn't draw that slide ({exc.__class__.__name__}). The text version and the slide's "
                          "saved pictures are still available.")


def render(lib, rel_path: str, page: int = 1, long_edge: int = TARGET_LONG_EDGE) -> List[Path]:
    """Return image file(s) showing page/slide `page` (1-based) of the given library file."""
    try:
        return [lib.checked(p) for p in _render(lib, rel_path, page, long_edge)]
    except LibraryPathError as exc:
        raise RenderError(str(exc)) from exc


def _render(lib, rel_path: str, page: int, long_edge: int) -> List[Path]:
    path = locate(lib, rel_path)
    if path is None or not path.exists():
        raise RenderError(f"Not found in the library: {rel_path}")
    source = lib.checked(_original_for(path))
    ext = source.suffix.lower()
    if ext == ".svg":
        raise RenderError("Can't show SVG drawings as a picture here; its text version has any words in it.")
    if ext in IMAGE_EXT:
        try:
            return [_fit_image(lib, source)]
        except LibraryPathError:
            raise
        except RenderError:
            raise
        except Exception as exc:
            if ext in VIEWABLE_IMAGE_EXT:
                return [source]
            raise RenderError(f"Couldn't open that picture ({exc.__class__.__name__}); open the original instead.")
    if ext == ".md":
        raise RenderError("That's a text file with no original to render. For lectures, open the 'Screen at …' "
                          "images linked in the transcript.")
    pdf: Optional[Path] = None
    if ext in PDFLIKE_EXT:
        pdf = source
    elif ext in OFFICE_EXT:
        pdf = _converted_pdf(lib, source) if soffice_path() else None
        if pdf is None and ext == ".pptx":
            return [_drawn_slide(lib, source, page, long_edge)]
        if pdf is None:
            images = sorted(lib.checked(assets_dir_for(source)).glob(f"slide-{page:02d}-*")) if ext in (".ppt",) else []
            if images:
                return images
            raise RenderError("Rendering this file type needs LibreOffice (free: brew install --cask libreoffice). "
                              "The text version and any saved images are still available.")
    else:
        raise RenderError(f"Can't render '{ext}' files as images.")
    key = hashlib.sha1(f"{source}|{file_digest(source)}|{page}|{long_edge}".encode()).hexdigest()[:16]
    out = lib.checked(lib.renders / f"{key}-p{page}.png")
    if out.exists():
        return [out]
    import pymupdf

    with pymupdf.open(str(pdf)) as doc:
        if page < 1 or page > doc.page_count:
            raise RenderError(f"Page {page} doesn't exist (this file has {doc.page_count}).")
        pg = doc[page - 1]
        longest_pt = max(pg.rect.width, pg.rect.height) or 1
        zoom = max(0.5, min(4.0, long_edge / longest_pt))
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out))
        return [out]


def page_count(lib, rel_path: str) -> Optional[int]:
    path = lib.resolve(rel_path)
    if path is None:
        return None
    try:
        source = lib.checked(_original_for(path))
    except LibraryPathError:
        return None
    if source.suffix.lower() in PDFLIKE_EXT:
        import pymupdf

        with pymupdf.open(str(source)) as doc:
            return doc.page_count
    return None
