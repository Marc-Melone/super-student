"""Run `superstudent sync` automatically: launchd on macOS, cron on Linux, Task Scheduler on Windows."""

from __future__ import annotations

import os
import platform
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import APP_DIR

LABEL = "com.superstudent.sync"
MARKER = "# superstudent-sync"
TASK_NAME = "SuperStudentSync"
PENDING = APP_DIR / "schedule-pending"     # a new schedule waiting for a running update to finish


def _sync_running() -> bool:
    try:
        from .config import library_path, load_config
        from .library import Library

        return Library(library_path(load_config())).sync_running()
    except Exception:
        return False


def apply_pending() -> str:
    """Put a schedule saved during an update into effect once no update is running (called by the app)."""
    try:
        hours = float(PENDING.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return ""
    if _sync_running():
        return ""
    return schedule(hours, force=True) if hours else ""


def _python() -> str:
    exe = sys.executable
    if os.name == "nt":
        pyw = Path(exe).with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return exe


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FileNotFoundError as exc:
            return f"unavailable ({Path(str(exc.filename or 'scheduler')).name} isn't installed on this computer)"
        except (OSError, subprocess.SubprocessError) as exc:
            return f"unavailable ({exc})"
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@_safe
def schedule(hours: float, force: bool = False) -> str:
    hours = max(1.0, float(hours))
    system = platform.system()
    logs = APP_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if system == "Darwin":
        plist = {
            "Label": LABEL,
            "ProgramArguments": [_python(), "-m", "superstudent", "sync", "--quiet"],
            "StartInterval": int(hours * 3600),
            "RunAtLoad": True,
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "Nice": 10,
            "StandardOutPath": str(logs / "scheduled-sync.log"),
            "StandardErrorPath": str(logs / "scheduled-sync.log"),
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                **({"SUPERSTUDENT_HOME": os.environ["SUPERSTUDENT_HOME"]} if os.environ.get("SUPERSTUDENT_HOME") else {}),
            },
        }
        path = _plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = plistlib.dumps(plist)
        uid = os.getuid()
        loaded = subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True).returncode == 0
        done = f"Scheduled: syncs every {hours:g} hours while you're logged in (and right after login)."
        if loaded and path.exists() and path.read_bytes() == data:
            PENDING.unlink(missing_ok=True)
            return done                       # already exactly this: leave the running job alone
        if loaded and not force and _sync_running():
            # Reloading would stop the update that's running (and its transcription) to restart it from scratch.
            PENDING.write_text(f"{hours:g}\n", encoding="utf-8")
            return f"Saved: updates every {hours:g} hours, starting when the update that's running now finishes."
        with open(path, "wb") as fh:
            fh.write(data)
        PENDING.unlink(missing_ok=True)
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
        res = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True)
        if res.returncode != 0:
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
            res = subprocess.run(["launchctl", "load", "-w", str(path)], capture_output=True, text=True)
            if res.returncode != 0:
                return f"Wrote {path}, but launchd refused it: {res.stderr.strip() or res.stdout.strip()}"
        return done
    if system == "Linux":
        every = max(1, int(round(hours)))
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        lines = [ln for ln in (current.stdout.splitlines() if current.returncode == 0 else []) if MARKER not in ln]
        cmd = f'{_python()} -m superstudent sync --quiet >> "{logs / "scheduled-sync.log"}" 2>&1'
        lines.append(f"17 */{every} * * * {cmd} {MARKER}")
        subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
        return f"Scheduled with cron: every {every} hours."
    if system == "Windows":
        every = max(1, int(round(hours)))
        cmd = f'"{_python()}" -m superstudent sync --quiet'
        res = subprocess.run(["schtasks", "/Create", "/F", "/SC", "HOURLY", "/MO", str(every), "/TN", TASK_NAME, "/TR", cmd],
                             capture_output=True, text=True)
        if res.returncode != 0:
            return f"Task Scheduler refused: {res.stderr.strip() or res.stdout.strip()}"
        return f"Scheduled with Task Scheduler: every {every} hours."
    return f"Automatic scheduling isn't supported on {system}; run `superstudent sync` yourself."


@_safe
def unschedule() -> str:
    system = platform.system()
    if system == "Darwin":
        PENDING.unlink(missing_ok=True)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        try:
            _plist_path().unlink()
        except OSError:
            pass
        return "Automatic sync turned off."
    if system == "Linux":
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if current.returncode == 0:
            lines = [ln for ln in current.stdout.splitlines() if MARKER not in ln]
            subprocess.run(["crontab", "-"], input="\n".join(lines) + ("\n" if lines else ""), text=True)
        return "Automatic sync turned off."
    if system == "Windows":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True)
        return "Automatic sync turned off."
    return "Nothing to turn off."


@_safe
def schedule_status() -> str:
    system = platform.system()
    if system == "Darwin":
        if not _plist_path().exists():
            return "off"
        res = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
        if res.returncode != 0:
            return "configured but not loaded (run: superstudent schedule)"
        try:
            with open(_plist_path(), "rb") as fh:
                interval = plistlib.load(fh).get("StartInterval", 0)
            return f"on, every {interval / 3600:g} hours"
        except Exception:
            return "on"
    if system == "Linux":
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        return "on (cron)" if res.returncode == 0 and MARKER in res.stdout else "off"
    if system == "Windows":
        res = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True)
        return "on (Task Scheduler)" if res.returncode == 0 else "off"
    return "unsupported"
