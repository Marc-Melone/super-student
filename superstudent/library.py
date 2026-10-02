"""Where things live: the library folder, its hidden state, and the one-sync-at-a-time lock."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .util import atomic_write_json, now_iso, read_json

STATE_VERSION = 1


class SyncLocked(Exception):
    pass


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.meta = self.root / ".superstudent"
        self.state_path = self.meta / "state.json"
        self.index_path = self.meta / "search.db"
        self.lock_path = self.meta / "sync.lock"
        self.log_path = self.meta / "sync.log"
        self.last_sync_path = self.meta / "last_sync.json"
        self.renders = self.meta / "renders"

    def ensure(self) -> None:
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.root, 0o700)  # grades and feedback live here: keep it private to this account
            except OSError:
                pass
        self.meta.mkdir(parents=True, exist_ok=True)

    # -- state
    def load_state(self) -> Dict[str, Any]:
        state = read_json(self.state_path, None) or {}
        state.setdefault("version", STATE_VERSION)
        state.setdefault("courses", {})
        return state

    def save_state(self, state: Dict[str, Any]) -> None:
        self.ensure()
        atomic_write_json(self.state_path, state)

    def snapshot_path(self, course_id: str) -> Path:
        return self.meta / "courses" / f"{course_id}.json"

    def load_snapshot(self, course_id: str) -> Dict[str, Any]:
        return read_json(self.snapshot_path(course_id), {}) or {}

    def save_snapshot(self, course_id: str, snap: Dict[str, Any]) -> None:
        atomic_write_json(self.snapshot_path(course_id), snap)

    def last_sync(self) -> Dict[str, Any]:
        return read_json(self.last_sync_path, {}) or {}

    def log(self, line: str) -> None:
        self.ensure()
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {line}\n")
        try:
            if self.log_path.stat().st_size > 5 * 1024 * 1024:
                old = self.log_path.with_suffix(".log.1")
                os.replace(self.log_path, old)
        except OSError:
            pass

    # -- lock: only one sync at a time (scheduled + manual + Claude-triggered)
    @contextlib.contextmanager
    def lock(self, wait_seconds: float = 0) -> Iterator[None]:
        self.ensure()
        fh = open(self.lock_path, "a+")
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
        with open(self.lock_path, "a+") as fh:
            if _try_lock(fh):
                _unlock(fh)
                return False
            return True

    def resolve(self, relative: str) -> Optional[Path]:
        """Resolve a path inside the library; refuse anything outside it."""
        if not relative:
            return None
        candidate = (self.root / relative.lstrip("/")).resolve()
        root = self.root.resolve()
        if candidate == root or root in candidate.parents:
            return candidate
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
