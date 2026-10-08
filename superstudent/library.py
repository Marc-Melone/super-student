"""Where things live: the library folder, its hidden state, and the one-sync-at-a-time lock."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .util import (action_memo, atomic_write_json, cached_digest, change_times_tracked, is_within, now_iso, read_json,
                   read_json_view)

STATE_VERSION = 1


class SyncLocked(Exception):
    pass


class LibraryPathError(ValueError):
    """A final input or output resolves outside the course library."""


class Library:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.meta = self.root / ".superstudent"
        self.state_path = self.meta / "state.json"
        self.index_path = self.meta / "search.db"
        self.lock_path = self.meta / "sync.lock"
        self.log_path = self.meta / "sync.log"
        self.last_sync_path = self.meta / "last_sync.json"
        self.renders = self.meta / "renders"
        self._disk_checked = False
        self._check_disk()

    def _check_disk(self) -> None:
        """Find out once whether this library's disk keeps real change times, which reusing fingerprints and
        parsed files between actions relies on (see util.change_times_tracked)."""
        if self._disk_checked:
            return
        try:
            meta = self.checked(self.meta)
        except LibraryPathError:
            return
        self._disk_checked = change_times_tracked(meta) is not None

    def ensure(self) -> None:
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.root, 0o700)  # grades and feedback live here: keep it private to this account
            except OSError:
                pass
        self.checked(self.meta).mkdir(parents=True, exist_ok=True)

    def checked(self, path: Path) -> Path:
        """Check the final path, including existing symlinked parents, before using it.

        Call again after deriving an adjacent original, text version or generated output.
        Nonexistent outputs are allowed only when their existing parents remain in the library.
        """
        # Within one action (see util.one_action) a path is resolved once; the next action resolves it again. Links
        # inside the library can only come from the student or another program on the Mac: course files (including
        # unzipped ones) are written as plain files, and the connector never makes links.
        memo = action_memo()
        key = ("checked", str(self.root), os.fspath(path))
        if memo is not None and key in memo:
            return memo[key]
        try:
            candidate = Path(path).resolve()
            if candidate == self.root or is_within(candidate, self.root):
                if memo is not None:
                    memo[key] = candidate
                return candidate
        except (OSError, RuntimeError):
            pass
        raise LibraryPathError("That path is outside the library.")

    def fingerprint_store(self) -> Optional[Path]:
        """Where fingerprints of untouched files are kept between app launches (see util.cached_digest)."""
        self._check_disk()
        try:
            return self.checked(self.meta / "fingerprints.json")
        except LibraryPathError:
            return None

    def digest(self, path: Path) -> str:
        """The sha256 of a library file, reused while the file is untouched, across app launches too."""
        return cached_digest(path, store=self.fingerprint_store())

    # -- state
    def load_state(self) -> Dict[str, Any]:
        state = read_json(self.checked(self.state_path), None) or {}
        state.setdefault("version", STATE_VERSION)
        state.setdefault("courses", {})
        return state

    def state_view(self) -> Dict[str, Any]:
        """The course record for code that only reads it: parsed once and reused until it changes. It's shared
        by every caller, so never modify it; use load_state() for a copy to change and save."""
        self._check_disk()
        state = read_json_view(self.checked(self.state_path), None)
        if not isinstance(state, dict):
            state = {}
        state.setdefault("version", STATE_VERSION)      # the same defaults load_state adds
        state.setdefault("courses", {})
        return state

    def save_state(self, state: Dict[str, Any]) -> None:
        self.ensure()
        atomic_write_json(self.checked(self.state_path), state)

    def snapshot_path(self, course_id: str) -> Path:
        return self.checked(self.meta / "courses" / f"{course_id}.json")

    def load_snapshot(self, course_id: str) -> Dict[str, Any]:
        return read_json(self.snapshot_path(course_id), {}) or {}

    def save_snapshot(self, course_id: str, snap: Dict[str, Any]) -> None:
        atomic_write_json(self.snapshot_path(course_id), snap)

    def last_sync(self) -> Dict[str, Any]:
        return read_json(self.checked(self.last_sync_path), {}) or {}

    def log(self, line: str) -> None:
        self.ensure()
        with open(self.checked(self.log_path), "a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {line}\n")
        try:
            if self.log_path.stat().st_size > 5 * 1024 * 1024:
                old = self.checked(self.log_path.with_suffix(".log.1"))
                os.replace(self.log_path, old)
        except OSError:
            pass

    # -- lock: only one sync at a time (scheduled + manual + Claude-triggered)
    @contextlib.contextmanager
    def lock(self, wait_seconds: float = 0) -> Iterator[None]:
        self.ensure()
        fh = open(self.checked(self.lock_path), "a+")
        deadline = time.time() + wait_seconds
        try:
            while True:
                if _try_lock(fh):
                    break
                if time.time() >= deadline:
                    fh.close()
                    raise SyncLocked("Another sync is already running.")
                time.sleep(1)
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()} {now_iso()}\n")
            fh.flush()
            yield
        finally:
            try:
                _unlock(fh)
            finally:
                fh.close()

    def sync_running(self) -> bool:
        if not self.lock_path.exists():
            return False
        with open(self.checked(self.lock_path), "a+") as fh:
            if _try_lock(fh):
                _unlock(fh)
                return False
            return True

    def resolve(self, relative: str) -> Optional[Path]:
        """Resolve a path inside the library; refuse anything outside it."""
        if not relative:
            return None
        try:
            return self.checked(self.root / relative.lstrip("/"))
        except LibraryPathError:
            return None


def _try_lock(fh) -> bool:
    try:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    except ImportError:  # Windows
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False


def _unlock(fh) -> None:
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except ImportError:
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    except OSError:
        pass
