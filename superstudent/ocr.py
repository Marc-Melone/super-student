"""Reading the text in pictures (labels on diagrams, scanned pages, slides shown in lecture videos).

On a Mac this uses Apple's built-in text recognition (the Vision framework behind Live Text), so nothing
extra has to be installed. Elsewhere, or if that isn't available, it uses Tesseract when it's installed.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import List, NamedTuple, Optional

TESSERACT_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")   # Homebrew isn't on an app's PATH


class Line(NamedTuple):
    text: str
    x: float = 0.0   # left edge, 0..1 of the image width
    y: float = 0.0   # top edge, 0..1 of the image height (0 = top)
    confidence: float = 1.0
    w: float = 0.0   # width and height, 0..1 of the image
    h: float = 0.0


def tesseract_path() -> Optional[str]:
    found = shutil.which("tesseract")
    if found:
        return found
    for folder in TESSERACT_DIRS:
        candidate = Path(folder) / "tesseract"
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


@lru_cache(maxsize=1)
def apple_available() -> bool:
    if platform.system() != "Darwin" or os.environ.get("SUPERSTUDENT_NO_APPLE_OCR"):
        return False
    try:
        import Quartz  # noqa: F401
        import Vision

        return hasattr(Vision, "VNRecognizeTextRequest")
    except Exception:
        return False


def engine() -> str:
    """'apple', 'tesseract' or '' (none)."""
    if apple_available():
        return "apple"
    return "tesseract" if tesseract_path() else ""


def available() -> bool:
    return bool(engine())


def engine_label() -> str:
    return {"apple": "Apple text recognition (built into macOS)", "tesseract": "Tesseract"}.get(engine(), "")


# ---------------------------------------------------------------- Apple Vision

def _apple_lines(cgimage, orientation: int = 1) -> List[Line]:
    import Vision

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(getattr(Vision, "VNRequestTextRecognitionLevelAccurate", 0))
    request.setUsesLanguageCorrection_(True)
    # No auxiliary options. A Python dict can raise on Vision's lookup of absent keys through PyObjC.
    if orientation and orientation != 1:     # phone photos are stored sideways with an orientation tag
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_orientation_options_(cgimage, orientation, None)
    else:
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, None)
    result = handler.performRequests_error_([request], None)
    ok = result[0] if isinstance(result, tuple) else bool(result)
    if not ok:
        return []
    lines: List[Line] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        best = candidates[0]
        text = str(best.string()).strip()
        if not text:
            continue
        box = observation.boundingBox()   # normalized, origin at the bottom left
        x, y = float(box.origin.x), float(box.origin.y)
        width, height = float(box.size.width), float(box.size.height)
        lines.append(Line(text, x, max(0.0, 1.0 - y - height), float(best.confidence()), width, height))
    return lines


def _apple_read(path: Optional[Path] = None, data: Optional[bytes] = None) -> List[Line]:
    import objc
    import Quartz
    from Foundation import NSData, NSURL

    with objc.autorelease_pool():
        if path is not None:
            source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
        else:
            source = Quartz.CGImageSourceCreateWithData(NSData.dataWithBytes_length_(data, len(data)), None)
        if source is None:
            return []
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        if image is None:
            return []
        orientation = 1
        try:
            props = Quartz.CGImageSourceCopyPropertiesAtIndex(source, 0, None)
            if props is not None and props.get("Orientation") is not None:
                orientation = int(props.get("Orientation"))
        except Exception:
            orientation = 1
        return _apple_lines(image, orientation if 1 <= orientation <= 8 else 1)


# ---------------------------------------------------------------- Tesseract

def _tesseract_read(path: Path, timeout: int) -> List[Line]:
    exe = tesseract_path()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, str(path), "stdout", "-l", "eng", "--psm", "3", "tsv"],
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    width = height = 1.0
    groups: dict = {}
    for row in out.stdout.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12:
            continue
        level, left, top, w, h, conf, text = cols[0], cols[6], cols[7], cols[8], cols[9], cols[10], cols[11]
        if level == "1":
            width, height = max(1.0, float(w)), max(1.0, float(h))
        elif level == "5" and text.strip() and float(conf) >= 0:
            key = (cols[2], cols[3], cols[4])
            groups.setdefault(key, []).append((int(left), int(top), int(w), int(h), float(conf), text.strip()))
    lines: List[Line] = []
    for words in groups.values():
        words.sort()
        tall = sorted(wd[3] for wd in words)[len(words) // 2] or 1
        segment = [words[0]]
        for prev, cur in zip(words, words[1:]):
            if cur[0] - (prev[0] + prev[2]) > 1.5 * tall:   # a wide gap: separate labels on one row
                lines.append(_segment(segment, width, height))
                segment = []
            segment.append(cur)
        lines.append(_segment(segment, width, height))
    return lines


def _segment(words, width: float, height: float) -> Line:
    conf = sum(wd[4] for wd in words) / len(words) / 100.0
    x0 = words[0][0]
    x1 = max(wd[0] + wd[2] for wd in words)
    y0 = min(wd[1] for wd in words)
    y1 = max(wd[1] + wd[3] for wd in words)
    return Line(" ".join(wd[5] for wd in words), x0 / width, y0 / height, conf, (x1 - x0) / width, (y1 - y0) / height)


def _upright(path: Path) -> Optional[Path]:
    """A copy of a photo turned the way its orientation tag says, or None when it's already upright."""
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as im:
            if im.getexif().get(0x0112, 1) in (None, 1):
                return None
            fixed = ImageOps.exif_transpose(im)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                fixed.save(fh, "PNG")
                return Path(fh.name)
    except Exception:
        return None


# ---------------------------------------------------------------- public

def read_lines(path: Optional[Path] = None, data: Optional[bytes] = None, timeout: int = 90,
               mode: str = "page") -> List[Line]:
    """Text lines found in an image file or in image bytes (PNG/JPEG).

    mode="page": reading order for a page of text (a scan, a photographed handout): columns are read one at a
    time. mode="labels": scattered labels (a diagram), row by row from the top, left to right."""
    kind = engine()
    lines: List[Line] = []
    if kind == "apple":
        try:
            lines = _apple_read(path=path, data=data)
        except Exception:
            lines = []
    elif kind == "tesseract":
        if path is not None:
            upright = _upright(Path(path))
            try:
                lines = _tesseract_read(upright or Path(path), timeout)
            finally:
                if upright is not None:
                    upright.unlink(missing_ok=True)
        elif data:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
                fh.write(data)
                tmp = Path(fh.name)
            try:
                lines = _tesseract_read(tmp, timeout)
            finally:
                tmp.unlink(missing_ok=True)
    lines = [ln for ln in lines if ln.confidence >= 0.3 and re.search(r"[A-Za-z0-9]", ln.text)]
    if mode == "page":
        if kind == "tesseract":
            return lines                  # Tesseract's own layout analysis already reads columns in order
        from .layout import column_order

        boxes = [(ln.x, ln.y, ln.x + (ln.w or 0.01), ln.y + (ln.h or 0.01), ln) for ln in lines]
        return [b[4] for b in column_order(boxes, lambda b: b[2] - b[0], lambda b: b[4].text, 1.0)]
    # Labels: rows from top to bottom (labels at nearly the same height form one row), then left to right.
    rows: List[List[Line]] = []
    for ln in sorted(lines, key=lambda item: item.y):
        if rows and abs(ln.y - rows[-1][0].y) <= 0.025:
            rows[-1].append(ln)
        else:
            rows.append([ln])
    return [ln for row in rows for ln in sorted(row, key=lambda item: item.x)]


def read_text(path: Optional[Path] = None, data: Optional[bytes] = None, timeout: int = 90) -> str:
    return "\n".join(ln.text for ln in read_lines(path=path, data=data, timeout=timeout, mode="page"))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def labels_not_in(lines: List[Line], known_text: str, limit: int = 120, max_words: int = 8) -> List[str]:
    """Text read from a picture that isn't already in the page's own text: typically a figure's labels.
    Long lines are skipped; they're usually the page's own sentences, read slightly differently."""
    known = " " + _norm(known_text) + " "
    out: List[str] = []
    seen = set()
    for ln in lines:
        norm = _norm(ln.text)
        if len(norm) < 2 or norm in seen or f" {norm} " in known or len(norm.split()) > max_words:
            continue
        seen.add(norm)
        out.append(ln.text.strip())
        if len(out) >= limit:
            break
    return out


def summary(lines: List[Line], limit: int = 60) -> str:
    """One line for the text version: 'Text in the image: a; b; c'."""
    parts: List[str] = []
    seen = set()
    for ln in lines:
        norm = _norm(ln.text)
        if len(norm) < 2 or norm in seen:
            continue
        seen.add(norm)
        parts.append(re.sub(r"\s+", " ", ln.text).strip())
        if len(parts) >= limit:
            break
    return "; ".join(parts)
