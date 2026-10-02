"""Remove Super Student from this computer: automatic updates, AI connectors, the saved token and its own
files. The course library is kept unless asked for (and then it goes to the Trash on a Mac, not deleted)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from .config import APP_DIR, IS_MAC, delete_token, library_path, load_config


def running_from_app_dir() -> bool:
    try:
        return APP_DIR.resolve() in Path(sys.prefix).resolve().parents
    except OSError:
        return False


def move_to_trash(path: Path) -> bool:
    if not IS_MAC or not path.exists():
        return False
    script = 'on run argv\ntell application "Finder" to delete (POSIX file (item 1 of argv) as alias)\nend run'
    res = subprocess.run(["osascript", "-e", script, str(path)], capture_output=True, text=True)
    return res.returncode == 0


def stop_running_sync(lib) -> bool:
    """Stop an update that's running (started by the app, the schedule or an AI), so nothing keeps writing into
    the library while Super Student is being removed. True if one was stopped."""
    import signal
    import time

    if not lib.sync_running():
        return False
    try:
        pid = int((lib.lock_path.read_text(encoding="utf-8").split() or ["0"])[0])
    except (OSError, ValueError):
        return False
    if pid <= 1 or pid == os.getpid():
        return False
    for sig, wait in ((signal.SIGTERM, 15), (getattr(signal, "SIGKILL", signal.SIGTERM), 5)):
        try:
            os.kill(pid, sig)
        except OSError:
            return True
        deadline = time.time() + wait
        while time.time() < deadline:
            if not lib.sync_running():
                return True
            time.sleep(0.3)
    return True


def uninstall(remove_library: bool = False) -> Dict[str, object]:
    from .assistants import remove_claude, remove_openai
    from .library import Library
    from .scheduler import unschedule

    notes: List[str] = []
    notes.append(unschedule())
    if stop_running_sync(Library(library_path(load_config()))):
        notes.append("Stopped the update that was running.")
    for fn in (remove_openai, remove_claude):
        try:
            notes.append(fn())
        except Exception as exc:  # keep going: remove as much as possible
            notes.append(f"{fn.__name__}: {exc}")
    cfg = load_config()
    delete_token(cfg)
    notes.append("Removed the saved Canvas access token from this computer.")
    lib = library_path(cfg)
    library_note = f"Your courses are still in {lib}."
    if remove_library and lib.exists():
        if move_to_trash(lib):
            library_note = f"Moved {lib} to the Trash."
        else:
            library_note = f"Couldn't move {lib} to the Trash; delete it yourself if you don't need it."
    notes.append(library_note)
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    old_models = [p for p in hub.glob("models--*whisper*")] if hub.is_dir() else []
    if old_models:
        notes.append(f"Speech models used for lecture transcription may still be in {hub} (other apps can share them); "
                     "delete the folders with 'whisper' in their names if nothing else needs them.")
    link = Path.home() / ".local" / "bin" / "superstudent"   # made by install.sh
    try:
        if link.is_symlink() and APP_DIR.resolve() in Path(os.readlink(link)).resolve().parents:
            link.unlink()
    except OSError:
        pass
    # Super Student's own files. If it runs from a private copy inside APP_DIR (the Mac app), remove the whole
    # folder once this process has exited; otherwise remove just the settings.
    if running_from_app_dir():
        script = 'while kill -0 "$1" 2>/dev/null; do sleep 1; done; rm -rf "$2"'
        subprocess.Popen(["/bin/sh", "-c", script, "sh", str(os.getpid()), str(APP_DIR)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        notes.append(f"{APP_DIR} will be removed when Super Student closes.")
    else:
        for name in ("config.json", "token", "app-window.json"):
            try:
                (APP_DIR / name).unlink()
            except OSError:
                pass
        shutil.rmtree(APP_DIR / "logs", ignore_errors=True)
    return {"ok": True, "notes": notes, "library": str(lib), "library_kept": not (remove_library and not lib.exists()),
            "canvas_url": cfg.get("canvas_url") or ""}
