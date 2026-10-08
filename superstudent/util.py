"""Small shared helpers: safe file names, atomic writes, dates, front matter."""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import functools
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

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
    forget(path)


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


# ---------------------------------------------------------------- reusing what hasn't changed
#
# The freshness checks must notice any change to a file, including a same-size replacement whose modification
# date was put back. Re-reading and re-hashing every file on every check made the app slow, so a file is identified
# by its signature instead: device, file id, size, modification time and change time. The system sets the change
# time on every write (and whenever the dates are set), and apps can't set it back. Timestamps move in small steps,
# though, so two writes moments apart can leave the same signature. Anything worked out from a file (its
# fingerprint, a parsed record) is therefore reused between actions only while the signature is unchanged, and only
# if the file had already been still for SETTLE_NS when it was read: every later write then gets a later change
# time. A file written moments ago is always read again. Disks that don't keep a real change time are never trusted
# this way (see change_times_tracked). Within one action (see one_action), a file read moments ago is reused too,
# unless its signature changes: the rule the per-request caches already used.

SETTLE_NS = 3_000_000_000
_MEMO_LOCK = threading.Lock()
_DIGESTS: Dict[str, Tuple[tuple, str]] = {}           # path -> (signature, sha256) of files read after settling
_VIEWS: Dict[Tuple[str, str], Tuple[tuple, bytes, Any, bool]] = {}  # (kind, path) -> (signature, content hash,
                                                                    #   value, read after settling)
_PENDING: Dict[str, Dict[str, list]] = {}             # fingerprint store -> {path: [*signature, sha256]} to save
_CHANGE_TIMES: Dict[int, bool] = {}                   # device -> whether it keeps real change times
_CHECKED_FOLDERS: Dict[Tuple[int, int], bool] = {}    # (device, folder id) -> the same, per folder checked
_ACTION: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar("superstudent_action", default=None)
MAX_STORED_FINGERPRINTS = 50000


def stat_signature(path) -> Optional[Tuple[int, int, int, int, int]]:
    """(device, file id, size, modification time, change time) of a file, or None if it can't be read."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def settled(signature: Optional[tuple]) -> bool:
    """Whether a file was last changed long enough ago, on a disk that keeps real change times, that any later
    write will change its signature. Judge this before reading the file, so what is read matches the signature."""
    return (signature is not None and _CHANGE_TIMES.get(signature[0]) is True
            and time.time_ns() - signature[4] >= SETTLE_NS)


def change_times_tracked(directory) -> Optional[bool]:
    """Whether the disk holding `directory` gives a file a new change time whenever it's written or its dates are
    set, by this computer's clock (APFS, HFS+ and most Linux disks do). Some disks report the modification time
    instead, which apps can set back (FAT and exFAT on a Mac), and a network disk's clock can be off; fingerprints
    and parsed files aren't reused between actions there, so every check reads the file again, as before. Found
    out once per folder by setting a scratch file's dates back; None if `directory` can't be checked."""
    if os.name == "nt":            # Windows reports a file's creation time as its change time
        return False
    try:
        folder = os.stat(directory)
    except (OSError, ValueError):
        return None
    key = (folder.st_dev, folder.st_ino)
    known = _CHECKED_FOLDERS.get(key)
    if known is not None:
        return known
    try:
        fd, probe = tempfile.mkstemp(dir=os.fspath(directory), prefix=".change-time-", suffix=".part")
    except (OSError, ValueError):
        return None
    try:
        try:
            os.write(fd, b"x")
        finally:
            os.close(fd)
        before = time.time_ns()
        old = (before // 1_000_000_000 - 400 * 86400) // 2 * 2 * 1_000_000_000   # over a year ago, on a 2 s step
        os.utime(probe, ns=(old, old))
        after = time.time_ns()
        changed = os.stat(probe).st_ctime_ns
    except OSError:
        return None
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass
    tracked = before - 2_000_000_000 <= changed <= after + 2_000_000_000
    with _MEMO_LOCK:
        _CHECKED_FOLDERS[key] = tracked
        _CHANGE_TIMES[folder.st_dev] = tracked
    return tracked


def is_within(path, directory) -> bool:
    """Whether a normalized path is strictly inside a normalized directory (both resolved, or both built from the
    same root with library-relative parts). The same answer as `directory in path.parents`, without building
    every parent path."""
    path, directory = os.fspath(path), os.fspath(directory).rstrip(os.sep)
    return path.startswith(directory + os.sep) and len(path) > len(directory) + 1


def relative_posix(path, directory) -> str:
    """A normalized path relative to a normalized directory that contains it, '/'-separated (Path.relative_to(...)
    .as_posix() without building every parent path). ValueError if the path isn't inside the directory."""
    path, directory = os.fspath(path), os.fspath(directory).rstrip(os.sep)
    if path.rstrip(os.sep) == directory:
        return "."
    if not is_within(path, directory):
        raise ValueError(f"{path!r} is not inside {directory!r}")
    rel = path[len(directory) + 1:]
    return rel if os.sep == "/" else rel.replace(os.sep, "/")


@contextlib.contextmanager
def one_action() -> Iterator[None]:
    """Group the reads of one action (a search, a progress report, an exam request): a file it has already read
    or fingerprinted is reused for the rest of the action while its signature is unchanged. Nested actions share
    the outer one. Fingerprints worked out during the action are saved when it ends (see cached_digest)."""
    if _ACTION.get() is not None:
        yield
        return
    token = _ACTION.set({})
    try:
        yield
    finally:
        _ACTION.reset(token)
        save_fingerprints()


def as_one_action(fn):
    """Run a function as one action (see one_action)."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with one_action():
            return fn(*args, **kwargs)
    return run


def action_memo() -> Optional[dict]:
    """The current action's scratch space for results that are only valid while the action runs, or None."""
    return _ACTION.get()


def known_digest(path, signature: Optional[tuple] = None, store=None) -> Optional[str]:
    """The sha256 of a file worked out earlier (in this process, or saved in `store` by any process), if the
    file hasn't changed since; otherwise None."""
    key = os.fspath(path)
    signature = signature or stat_signature(key)
    if signature is None:
        return None
    trusted = settled(signature)          # only fingerprints of files read after settling are kept for later
    hit = _DIGESTS.get(key)
    if hit is not None and hit[0] == signature and trusted:
        return hit[1]
    memo = _ACTION.get()
    hit = memo.get(("digest", key)) if memo is not None else None
    if hit is not None and hit[0] == signature:
        return hit[1]
    if store is not None and trusted:
        files = read_json_view(store, {}) or {}
        saved = files.get("files", {}).get(key) if isinstance(files, dict) and isinstance(files.get("files"), dict) else None
        if (isinstance(saved, list) and len(saved) == 6 and tuple(saved[:5]) == tuple(signature)
                and isinstance(saved[5], str) and re.fullmatch(r"[0-9a-f]{64}", saved[5])):
            with _MEMO_LOCK:
                _DIGESTS[key] = (tuple(signature), saved[5])
            return saved[5]
    return None


def remember_digest(path, signature: Optional[tuple], digest: str, store=None, *, read_settled: bool) -> None:
    """Keep a sha256 computed while the file had `signature` (checked again afterwards by the caller).
    `read_settled`: whether the file had settled when the read began (see settled). Only then does the
    fingerprint outlive the action, and only then is it saved to `store` for later processes."""
    if signature is None:
        return
    key = os.fspath(path)
    if read_settled:
        with _MEMO_LOCK:
            _DIGESTS[key] = (tuple(signature), digest)
            if store is not None:
                _PENDING.setdefault(os.fspath(store), {})[key] = [*signature, digest]
    else:
        memo = _ACTION.get()
        if memo is not None:
            memo[("digest", key)] = (tuple(signature), digest)


def cached_digest(path: Path, store=None) -> str:
    """file_digest, reused while the file is untouched (see above). With `store` (a library's fingerprint file),
    fingerprints are also kept across app restarts, so opening the app doesn't re-read every course file."""
    key = os.fspath(path)
    signature = stat_signature(key)
    known = known_digest(key, signature, store)
    if known is not None:
        return known
    ready = settled(signature)            # judged before reading: any write after this changes the signature
    digest = file_digest(Path(path))
    if signature is not None and stat_signature(key) == signature:    # unchanged while it was read
        remember_digest(key, signature, digest, store, read_settled=ready)
    return digest


def save_fingerprints() -> None:
    """Add the fingerprints worked out since the last save to each library's fingerprint file. Best effort: a
    fingerprint that isn't saved is simply worked out again later. Another process saving at the same moment can
    drop some of these; that only costs recomputing them."""
    with _MEMO_LOCK:
        pending = dict(_PENDING)
        _PENDING.clear()
    for store, entries in pending.items():
        if not Path(store).parent.is_dir():          # the library was removed meanwhile: don't recreate it
            continue
        try:
            saved = read_json(Path(store), {}) or {}
            files = saved.get("files") if isinstance(saved, dict) and isinstance(saved.get("files"), dict) else {}
            files.update(entries)
            if len(files) > MAX_STORED_FINGERPRINTS:           # drop files that are gone, then the oldest
                files = {k: v for k, v in files.items() if os.path.exists(k)}
                files = dict(list(files.items())[-MAX_STORED_FINGERPRINTS:])
            atomic_write_json(Path(store), {"version": 1, "files": files})
        except (OSError, ValueError):
            continue


atexit.register(save_fingerprints)


def _read_view(kind: str, path, parse, default: Any) -> Any:
    """A file parsed once and reused until it changes, for code that only reads the result. Every caller gets the
    same object, so it must never be modified. A file that hadn't settled when it was last read is read again and
    its bytes compared instead."""
    key = os.fspath(path)
    signature = stat_signature(key)
    if signature is None:
        return default
    memo = _ACTION.get()
    if memo is not None:
        hit = memo.get((kind, key))
        if hit is not None and hit[0] == signature:
            return hit[1]
    hit = _VIEWS.get((kind, key))
    if hit is not None and hit[0] == signature and hit[3] and settled(signature):
        value = hit[2]
    else:
        ready = settled(signature)        # judged before reading (see cached_digest)
        try:
            data = Path(key).read_bytes()
        except OSError:
            return default
        check = hashlib.sha256(data).digest()
        if hit is not None and hit[1] == check:
            value = hit[2]
        else:
            try:
                value = parse(data)
            except (ValueError, UnicodeError):
                return default
        if stat_signature(key) == signature:
            with _MEMO_LOCK:
                _VIEWS[(kind, key)] = (signature, check, value, ready)
    if memo is not None:
        memo[(kind, key)] = (signature, value)
    return value


def read_json_view(path, default: Any = None) -> Any:
    """read_json for code that only reads the result: parsed once and reused until the file changes. The same
    object is shared by every caller, so never modify it (use read_json for anything that will be changed)."""
    return _read_view("json", path, lambda data: json.loads(data.decode("utf-8")), default)


def front_matter_view(path) -> Dict[str, str]:
    """The front matter of a text version, reused until the file changes. Never modify the result."""
    return _read_view("meta", path, lambda data: parse_front_matter(data.decode("utf-8"))[0], {})


def forget(path) -> None:
    """Drop what was remembered about a file that this process has just replaced."""
    keys = {os.fspath(path)}
    try:
        keys.add(os.path.realpath(path))
    except (OSError, ValueError):
        pass
    memo = _ACTION.get()
    with _MEMO_LOCK:
        for key in keys:
            _DIGESTS.pop(key, None)
            for kind in ("json", "meta"):
                _VIEWS.pop((kind, key), None)
    if memo is not None:
        for key in keys:
            for kind in ("digest", "json", "meta"):
                memo.pop((kind, key), None)


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
