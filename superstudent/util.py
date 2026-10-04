"""Small shared helpers: safe file names, atomic writes, dates, front matter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_UMASK = os.umask(0)
os.umask(_UMASK)

_INVALID = re.compile(r'[\x00-\x1f<>:"/\\|?*  ]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


# Course content that disappeared from Canvas (deleted, replaced or hidden) is moved here, kept for reference.
REMOVED_DIR = "_Removed from Canvas"
MAX_NAME_BYTES = 200   # macOS allows 255 bytes per name; leave room for ".md", ".assets", " (unzipped)", " (id)"


def _cut(text: str, max_len: int, max_bytes: int) -> str:
    text = text[:max_len]
    while len(text.encode("utf-8")) > max_bytes:      # Chinese, Japanese, Korean or emoji titles: 3-4 bytes a letter
        text = text[:-1]
    return text


def safe_name(name: Any, max_len: int = 110, default: str = "untitled", max_bytes: int = MAX_NAME_BYTES) -> str:
    """Turn any title into a file or folder name that works on macOS, Windows and Linux."""
    text = unicodedata.normalize("NFC", str(name or "")).strip()
    text = _INVALID.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = default
    if text.split(".")[0].upper() in _RESERVED:
        text = "_" + text
    if len(text) > max_len or len(text.encode("utf-8")) > max_bytes:
        stem, dot, ext = text.rpartition(".")
        if dot and 0 < len(ext) <= 8 and stem:
            room = max_bytes - len(ext.encode("utf-8")) - 1
            text = _cut(stem, max_len - len(ext) - 1, room).rstrip(" .") + "." + ext
        else:
            text = _cut(text, max_len, max_bytes).rstrip(" .")
    return text or default


def add_suffix(path: str, suffix: str) -> str:
    """'a/b/Lecture.pdf' + '123' -> 'a/b/Lecture (123).pdf'."""
    p = Path(path)
    if p.suffix and len(p.suffix) <= 9:
        return str(p.with_name(f"{p.stem} ({suffix}){p.suffix}"))
    return str(p.with_name(f"{p.name} ({suffix})"))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace a file in one step. A file that is a link is written where the link points (the link stays), and
    an existing file keeps its permissions (a private config stays private)."""
    path = Path(os.path.realpath(path)) if Path(path).is_symlink() else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0o666 & ~_UMASK
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mark_downloaded(path: Path) -> None:
    """Flag a file that came from the internet the way browsers do (macOS's quarantine flag), so macOS checks
    anything runnable in it before it runs. Documents open as usual."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        value = f"0081;{int(time.time()):x};Super Student;".encode()
        libc.setxattr(os.fsencode(str(path)), b"com.apple.quarantine", value, len(value), 0, 0)
    except Exception:
        pass


def atomic_write_text(path: Path, text: str) -> bool:
    """Write text only if it changed. Returns True when the file was (re)written."""
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    atomic_write_bytes(path, text.encode("utf-8"))
    return True


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_bytes(path, (json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def file_digest(path: Path) -> str:
    """Identify a source by its bytes, including changes that keep its size and timestamp."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_dt(value: Any, with_time: bool = True) -> str:
    """Canvas timestamp -> 'Tue Oct 14, 2026 11:59 PM' in the computer's local time zone."""
    dt = parse_dt(value) if not isinstance(value, datetime) else value
    if not dt:
        return ""
    local = dt.astimezone()
    day = local.strftime("%a %b %d, %Y").replace(" 0", " ")
    if not with_time:
        return day
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{day} {clock}"


def fmt_date(value: Any) -> str:
    return fmt_dt(value, with_time=False)


def day_key(value: Any) -> str:
    dt = parse_dt(value)
    return dt.astimezone().strftime("%Y-%m-%d") if dt else ""


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def human_size(n: Optional[float]) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


_NEEDS_QUOTES = re.compile(r"(^[\s\[\]{}&*!|>'\"%@`#,?-])|(:\s)|(\s#)|(:$)")


def front_matter(meta: Dict[str, Any]) -> str:
    """Minimal YAML front matter. Values that could confuse YAML are JSON-quoted."""
    lines = ["---"]
    for key, value in meta.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        text = re.sub(r"\s+", " ", str(value)).strip()
        if _NEEDS_QUOTES.search(text):
            text = json.dumps(text, ensure_ascii=False)
        lines.append(f"{key}: {text}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def parse_front_matter(text: str) -> Tuple[Dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    meta: Dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        meta[key.strip()] = value
    body_start = text.find("\n", end + 4)
    return meta, (text[body_start + 1:] if body_start != -1 else "")


def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def md_link(label: str, target: str) -> str:
    """Markdown link to a relative path, escaping characters that break links."""
    label = label.replace("[", "(").replace("]", ")")
    target = target.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"[{label}]({target})"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
