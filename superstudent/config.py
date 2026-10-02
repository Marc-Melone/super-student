"""Settings file and Canvas token storage (macOS Keychain, or a private file elsewhere)."""

from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .util import atomic_write_json, read_json

APP_DIR = Path(os.environ.get("SUPERSTUDENT_HOME") or (Path.home() / ".superstudent")).expanduser()
CONFIG_FILE = APP_DIR / "config.json"
TOKEN_FILE = APP_DIR / "token"
KEYCHAIN_SERVICE = "superstudent-canvas"

DEFAULTS: Dict[str, Any] = {
    "canvas_url": "",
    "library_dir": str(Path.home() / "SuperStudent"),
    # "active" = every course you're currently enrolled in; or a list of course ids.
    "courses": "active",
    "exclude_courses": [],
    # Lecture video/audio -> text. "auto" = use captions when Canvas has them,
    # otherwise transcribe locally if a Whisper backend is installed.
    "transcribe": "auto",
    "whisper_backend": "auto",   # auto | faster-whisper | mlx
    "whisper_model": "",         # blank = backend default (small.en / whisper-large-v3-turbo)
    "media_minutes_per_sync": 120,  # time budget for transcription per sync run
    "keep_media_files": False,   # keep downloaded videos after transcribing
    "screen_snapshots": True,    # save a frame whenever a lecture video's picture changes
    "max_file_mb": 400,          # skip non-media files bigger than this
    "max_media_mb": 3000,        # skip videos bigger than this
    "youtube_transcripts": True,
    "download_my_submissions": True,
    "download_workers": 4,
    "sync_interval_hours": 6,
    "canvas_user_name": "",       # shown in the app ("Connected as …")
    "setup_complete": False,      # the app's setup wizard finished
}

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    stored = read_json(CONFIG_FILE, {}) or {}
    cfg.update({k: v for k, v in stored.items() if v is not None})
    if os.environ.get("SUPERSTUDENT_CANVAS_URL"):
        cfg["canvas_url"] = os.environ["SUPERSTUDENT_CANVAS_URL"]
    if os.environ.get("SUPERSTUDENT_LIBRARY"):
        cfg["library_dir"] = os.environ["SUPERSTUDENT_LIBRARY"]
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(CONFIG_FILE, {k: cfg[k] for k in cfg if k in DEFAULTS})


def library_path(cfg: Dict[str, Any]) -> Path:
    return Path(os.path.expanduser(cfg.get("library_dir") or DEFAULTS["library_dir"])).resolve()


def normalize_canvas_url(value: str) -> str:
    """'yourschool.instructure.com/courses/1' -> 'https://yourschool.instructure.com'."""
    text = (value or "").strip().rstrip("/")
    if not text:
        raise ValueError("Enter your Canvas web address, for example school.instructure.com")
    if "://" not in text:
        text = "https://" + text
    parts = urlparse(text)
    host = parts.hostname or ""
    if not host or "." not in host and host not in ("localhost",):
        raise ValueError(f"That doesn't look like a Canvas address: {value!r}")
    scheme = parts.scheme.lower()
    local = host in ("localhost", "127.0.0.1", "::1")
    if scheme != "https" and not local:
        scheme = "https"
    netloc = parts.netloc.split("@")[-1]
    return f"{scheme}://{netloc}"


def _host(cfg: Dict[str, Any]) -> str:
    return urlparse(cfg.get("canvas_url") or "").hostname or "canvas"


def _keychain_available() -> bool:
    return IS_MAC and shutil.which("security") is not None


def get_token(cfg: Dict[str, Any]) -> Optional[str]:
    env = os.environ.get("CANVAS_TOKEN") or os.environ.get("SUPERSTUDENT_TOKEN")
    if env:
        return env.strip()
    if _keychain_available():
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _host(cfg), "-w"],
                capture_output=True, text=True, timeout=20,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        text = TOKEN_FILE.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def _keychain_read(cfg: Dict[str, Any]) -> Optional[str]:
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _host(cfg), "-w"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _keychain_write(cfg: Dict[str, Any], token: str) -> bool:
    """Save to the Keychain without putting the token on a command line (where other programs on the Mac
    could see it): the command goes to `security -i` through its standard input."""
    host = _host(cfg)
    if re.fullmatch(r"[A-Za-z0-9~._+/=-]+", token) and re.fullmatch(r"[A-Za-z0-9._-]+", host):
        command = (f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {host} -l SuperStudent-Canvas-token "
                   f"-T /usr/bin/security -w {token}\n")
        try:
            subprocess.run(["security", "-i"], input=command, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
        if _keychain_read(cfg) == token:
            return True
    return False


def set_token(cfg: Dict[str, Any], token: str) -> str:
    """Store the token. Returns a short description of where it went."""
    token = token.strip()
    if _keychain_available() and _keychain_write(cfg, token):
        try:
            TOKEN_FILE.unlink()
        except OSError:
            pass
        return "your macOS Keychain"
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    try:
        os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return str(TOKEN_FILE) + " (readable only by you)"


def delete_token(cfg: Dict[str, Any]) -> None:
    if _keychain_available():
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _host(cfg)],
                       capture_output=True, text=True)
    try:
        TOKEN_FILE.unlink()
    except OSError:
        pass
