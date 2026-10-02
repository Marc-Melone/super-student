"""Super Student app: a private local interface for setup and everyday use.

The interface is one HTML page (index.html) shown in its own Mac window (pywebview) or, as a fallback,
in the browser. It talks to a small server bound to 127.0.0.1 that only answers requests carrying this
session's secret token and addressed to this exact host and port, so web pages can't reach it.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

from .. import __version__
from ..config import (APP_DIR, DEFAULTS, IS_MAC, delete_token, get_token, library_path, load_config,
                      normalize_canvas_url, save_config, set_token)
from ..library import Library
from ..util import atomic_write_json, fmt_dt, human_size, parse_dt, read_json

HERE = Path(__file__).resolve().parent
INFO_FILE = APP_DIR / "app-window.json"
UPDATED_FILE = APP_DIR / "just-updated"
# Opened with a double-click equivalent; anything else from a course is revealed in Finder instead.
SAFE_TO_OPEN = {".md", ".txt", ".pdf", ".pptx", ".ppt", ".key", ".odp", ".docx", ".doc", ".odt", ".rtf", ".pages",
                ".xlsx", ".xls", ".csv", ".numbers", ".ods", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic",
                ".tif", ".tiff", ".bmp", ".svg", ".mp4", ".mov", ".m4v", ".mp3", ".m4a", ".wav", ".aac", ".webm",
                ".epub", ".ipynb", ".py", ".r", ".sql", ".json", ".xml", ".html", ".htm", ".tex", ".zip"}
SYNC_LOG = APP_DIR / "logs" / "app-sync.log"
ACCOUNT_SEARCH = os.environ.get("SUPERSTUDENT_ACCOUNT_SEARCH", "https://canvas.instructure.com/api/v1/accounts/search")
DRY_RUN = os.environ.get("SUPERSTUDENT_APP_DRYRUN") == "1"   # tests: record open/launch actions instead of doing them

APPS = {
    "openai": {"name": "ChatGPT", "bundle": "ChatGPT.app", "download": "https://chatgpt.com/download"},
    "claude": {"name": "Claude", "bundle": "Claude.app", "download": "https://claude.ai/download"},
}
INTERVALS = [3, 6, 12, 24]
MARK_ON, MARK_OFF = "\x02", "\x03"   # search-match markers (the page highlights text between them)
SUMMARY_KINDS = {"overview", "module", "links", "calendar", "grades"}


def _app_installed(bundle: str) -> bool:
    return any((base / bundle).exists() for base in (Path("/Applications"), Path.home() / "Applications"))


class App:
    def __init__(self, native: bool = False):
        self.token = secrets.token_urlsafe(24)
        self.native = native
        self.window = None
        self.port = 0
        self.last_ping = 0.0
        self.started = time.time()
        self.sync_proc: Optional[subprocess.Popen] = None
        self.actions: List[Dict[str, Any]] = []   # dry-run record
        self.server: Optional[ThreadingHTTPServer] = None
        self.lock = threading.Lock()
        self.uninstalled = False
        self.stopping = False

    # ------------------------------------------------------------------ helpers
    def cfg(self) -> Dict[str, Any]:
        return load_config()

    def lib(self) -> Library:
        return Library(library_path(self.cfg()))

    def run_open(self, args: List[str]) -> None:
        if DRY_RUN:
            self.actions.append({"open": args})
            return
        if IS_MAC:
            subprocess.Popen(["open", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif args and not args[0].startswith("-"):
            opener = shutil.which("xdg-open")
            if opener:
                subprocess.Popen([opener, args[-1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                webbrowser.open(args[-1])

    # ------------------------------------------------------------------ state
    def state(self) -> Dict[str, Any]:
        from ..assistants import claude_status, openai_status
        from ..extract import soffice_path, tesseract_available
        from ..ocr import engine_label as ocr_label
        from ..media import detect_backend
        from ..scheduler import PENDING, apply_pending, schedule_status

        cfg = self.cfg()
        lib = Library(library_path(cfg))
        token = get_token(cfg)
        if PENDING.exists() and not DRY_RUN:
            apply_pending()
        sched = schedule_status()
        interval = int(cfg.get("sync_interval_hours") or 6)
        state = lib.load_state()
        last = lib.last_sync()
        courses = [self._course_summary(lib, cid, c) for cid, c in state.get("courses", {}).items()
                   if c.get("folder") and (lib.root / c["folder"]).exists()]
        courses.sort(key=lambda c: (c["term"], c["code"] or c["name"]))
        backend = detect_backend(cfg.get("whisper_backend", "auto"))
        return {
            "version": __version__,
            "mac": IS_MAC,
            "native": self.native,
            "configured": bool(cfg.get("canvas_url") and token),
            "setup_complete": bool(cfg.get("setup_complete")) or (bool(cfg.get("canvas_url") and token) and bool(courses)),
            "canvas": {"url": cfg.get("canvas_url") or "", "host": urlparse(cfg.get("canvas_url") or "").hostname or "",
                       "user": cfg.get("canvas_user_name") or "", "token_saved": bool(token),
                       "rejected": (lib.meta / "auth_error").exists()},
            "library": {"path": str(lib.root), "exists": lib.root.exists(), "home": str(Path.home())},
            "settings": {
                "auto_sync": sched.startswith("on"), "schedule_text": sched, "interval": interval,
                "intervals": INTERVALS, "transcribe": str(cfg.get("transcribe", "auto")) != "off",
                "accuracy": "higher" if str(cfg.get("whisper_model") or "") in ("turbo", "large-v3-turbo", "distil-large-v3")
                else "standard", "keep_videos": bool(cfg.get("keep_media_files")),
                "excluded": [str(x) for x in cfg.get("exclude_courses") or []],
            },
            "assistants": {
                "openai": {"status": _status_key(openai_status()), "app_installed": _app_installed(APPS["openai"]["bundle"]),
                           "download": APPS["openai"]["download"]},
                "claude": {"status": _status_key(claude_status()), "app_installed": _app_installed(APPS["claude"]["bundle"]),
                           "download": APPS["claude"]["download"]},
            },
            "transcription": {"available": bool(backend), "backend": backend or ""},
            "extras": {"libreoffice": bool(soffice_path()), "ocr": tesseract_available(), "ocr_label": ocr_label()},
            "visuals": self._visuals(lib),
            "study": self._study(lib),
            "sync": {"running": lib.sync_running() or self._our_sync_running(),
                     "last_finished": last.get("finished"), "last_text": _ago(last.get("finished")),
                     "errors": last.get("errors") or []},
            "courses": courses,
            "attention": self._attention(lib, state, last, bool(backend), cfg),
        }

    def _visuals(self, lib: Library) -> Dict[str, int]:
        """How many pictures are worth describing and how many are (cached until the library changes)."""
        from ..describe import survey

        def mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        key = (str(lib.root), mtime(lib.index_path), mtime(lib.meta / "descriptions.json"))
        if getattr(self, "_visuals_key", None) != key:
            try:
                info = survey(lib)
                self._visuals_cache = {"visuals": info["visuals"], "described": info["described"]}
            except Exception:
                self._visuals_cache = {"visuals": 0, "described": 0}
            self._visuals_key = key
        return self._visuals_cache

    def _study(self, lib: Library) -> Dict[str, int]:
        """Study-pass progress (cached until the library or the notes change)."""
        from ..notes import progress

        def mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        key = (str(lib.root), mtime(lib.index_path), mtime(lib.meta / "notes.json"))
        if getattr(self, "_study_key", None) != key:
            try:
                info = progress(lib, limit=1)
                self._study_cache = {"docs": info["docs"], "done": info["done"],
                                     "courses": [{"label": c["label"], "docs": c["docs"], "done": c["done"],
                                                  "course_notes": c["course_notes"]} for c in info["courses"]]}
            except Exception:
                self._study_cache = {"docs": 0, "done": 0, "courses": []}
            self._study_key = key
        return self._study_cache

    def _our_sync_running(self) -> bool:
        return self.sync_proc is not None and self.sync_proc.poll() is None

    def _course_summary(self, lib: Library, cid: str, c: Dict[str, Any]) -> Dict[str, Any]:
        from ..overview import dated_items

        snap = lib.load_snapshot(cid)
        counts = {"documents": 0, "transcripts": 0, "waiting": 0, "assignments": 0, "announcements": 0, "pages": 0}
        for key, it in (c.get("items") or {}).items():
            if it.get("removed"):
                continue
            kind = key.split(":")[0]
            if kind in ("file", "mine"):
                if it.get("kind") == "media":
                    counts["transcripts"] += it.get("status") == "ok"
                    counts["waiting"] += it.get("status") in ("pending", "failed")
                elif it.get("status") == "ok":
                    counts["documents"] += 1
            elif kind == "media":
                counts["transcripts"] += it.get("status") == "ok"
                counts["waiting"] += it.get("status") == "pending"
            elif kind in ("assignment", "announcement", "page"):
                counts[kind + "s"] += 1
        upcoming = None
        now = time.time()
        for row in dated_items(snap):
            when = parse_dt(row["when"])
            if row["kind"] != "Module opens" and when and when.timestamp() >= now:
                upcoming = {"title": row["title"], "kind": row["kind"], "when": fmt_dt(row["when"])}
                break
        modules = [{"position": m.get("position"), "name": m.get("name")} for m in snap.get("modules") or []]
        return {"id": cid, "folder": c.get("folder"), "name": c.get("name") or "", "code": c.get("code") or "",
                "term": c.get("term") or "", "last_sync": _ago(c.get("last_sync")), "counts": counts,
                "next": upcoming, "modules": modules, "warnings": (snap.get("warnings") or [])[:5]}

    def _attention(self, lib: Library, state: Dict[str, Any], last: Dict[str, Any], have_backend: bool,
                   cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if UPDATED_FILE.exists():
            items.append({"kind": "updated", "action": "dismiss-update",
                          "text": f"Super Student was updated to version {__version__}. If ChatGPT or Claude is open, "
                                  "quit it and open it again so it uses the new version."})
        if (lib.meta / "auth_error").exists():
            items.append({"kind": "token", "action": "reconnect",
                          "text": "Canvas stopped accepting Super Student's access (the token expired or was deleted). "
                                  "Reconnect Canvas to keep your courses up to date."})
        waiting: List[str] = []
        failed: List[str] = []
        reasons: Dict[str, int] = {}
        for cid, c in state.get("courses", {}).items():
            for key, it in (c.get("items") or {}).items():
                if it.get("removed"):
                    continue
                name = it.get("title") or key
                if it.get("status") == "pending":
                    waiting.append(f"{c.get('code') or c.get('name')}: {name}")
                    reason = str(it.get("error") or "")
                    reasons[reason] = reasons.get(reason, 0) + 1
                elif it.get("status") == "failed":
                    failed.append(f"{c.get('code') or c.get('name')}: {name}: {_plain_error(str(it.get('error') or ''))}")
        if waiting:
            if not have_backend and str(cfg.get("transcribe", "auto")) != "off":
                items.append({"kind": "transcription", "action": "install-transcription",
                              "text": f"{len(waiting)} lecture recording{'s' if len(waiting) != 1 else ''} can't be turned "
                                      "into text yet because the transcription part isn't installed.", "details": waiting})
            else:
                why = max(reasons, key=reasons.get) if reasons else ""
                items.append({"kind": "waiting", "action": "sync",
                              "text": f"{len(waiting)} lecture recording{'s are' if len(waiting) != 1 else ' is'} waiting to "
                                      f"be turned into text. {_plain_reason(why)}", "details": waiting})
        if failed:
            items.append({"kind": "failed", "action": "details",
                          "text": f"{len(failed)} item{'s' if len(failed) != 1 else ''} couldn't be copied from Canvas. "
                                  "They're retried on every update.", "details": failed})
        for err in (last.get("errors") or [])[:3]:
            items.append({"kind": "error", "text": _plain_error(err)})
        return items

    # ------------------------------------------------------------------ Canvas
    def find_school(self, query: str) -> Dict[str, Any]:
        query = (query or "").strip()
        if len(query) < 3:
            return {"ok": False, "message": "Type at least three letters of your school's name."}
        try:
            resp = requests.get(ACCOUNT_SEARCH, params={"name": query, "per_page": 10}, timeout=12,
                                headers={"Accept": "application/json"})
            rows = resp.json() if resp.ok else None
        except (requests.RequestException, ValueError):
            rows = None
        if not isinstance(rows, list):
            return {"ok": False, "message": "School search isn't available right now. Type your Canvas address instead."}
        schools = [{"name": r.get("name"), "domain": r.get("domain")} for r in rows if r.get("domain")]
        if not schools:
            return {"ok": False, "message": "No schools matched. Try a shorter name, or type your Canvas address."}
        return {"ok": True, "schools": schools[:10]}

    def check_canvas(self, address: str) -> Dict[str, Any]:
        try:
            url = normalize_canvas_url(address)
        except ValueError:
            return {"ok": False, "message": "Enter the address you see in your browser when you're signed in to Canvas, "
                                            "for example yourschool.instructure.com."}
        try:
            resp = requests.get(url + "/api/v1/users/self", timeout=15, headers={"Accept": "application/json"})
        except requests.RequestException:
            return {"ok": False, "message": f"Couldn't reach {urlparse(url).hostname}. Check the spelling and your "
                                            "internet connection."}
        is_json = "json" in (resp.headers.get("Content-Type") or "")
        if resp.status_code in (200, 401) and is_json:
            final = urlparse(resp.url)
            base = f"{final.scheme}://{final.netloc}" if final.netloc else url
            return {"ok": True, "url": base, "host": final.hostname or urlparse(url).hostname}
        return {"ok": False, "message": f"{urlparse(url).hostname} doesn't look like a Canvas site. Copy the address "
                                        "from your browser while you're signed in to Canvas."}

    def connect_canvas(self, url: str, token: str) -> Dict[str, Any]:
        from ..canvas import AuthError, Canvas, CanvasError

        token = (token or "").strip().strip('"').strip()
        if len(token) < 20 or " " in token:
            return {"ok": False, "message": "That doesn't look like a whole token. Copy it again from Canvas."}
        try:
            url = normalize_canvas_url(url)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        try:
            me = Canvas(url, token).get("/api/v1/users/self") or {}
        except AuthError:
            return {"ok": False, "message": "Canvas didn't accept that token. Copy it again, or make a new one "
                                            "(Canvas shows each token only once)."}
        except CanvasError as exc:
            return {"ok": False, "message": f"Couldn't check the token with Canvas ({exc})."}
        cfg = self.cfg()
        cfg["canvas_url"] = url
        cfg["canvas_user_name"] = me.get("name") or me.get("short_name") or ""
        where = set_token(cfg, token)
        save_config(cfg)
        try:
            (self.lib().meta / "auth_error").unlink()
        except OSError:
            pass
        return {"ok": True, "name": cfg["canvas_user_name"], "stored": where}

    def canvas_courses(self) -> Dict[str, Any]:
        from ..canvas import Canvas, CanvasError

        cfg = self.cfg()
        token = get_token(cfg)
        if not cfg.get("canvas_url") or not token:
            return {"ok": False, "message": "Connect Canvas first."}
        try:
            rows = [c for c in Canvas(cfg["canvas_url"], token).paginate(
                "/api/v1/courses", {"enrollment_state": "active", "include[]": ["term"]})
                    if c.get("name") and not c.get("access_restricted_by_date")]
        except CanvasError as exc:
            return {"ok": False, "message": f"Couldn't load your courses ({exc})."}
        excluded = {str(x) for x in cfg.get("exclude_courses") or []}
        return {"ok": True, "courses": [{"id": str(c["id"]), "name": c.get("name"), "code": c.get("course_code") or "",
                                         "term": (c.get("term") or {}).get("name") or "",
                                         "included": str(c["id"]) not in excluded} for c in rows]}

    # ------------------------------------------------------------------ setup + settings
    def finish_setup(self, body: Dict[str, Any]) -> Dict[str, Any]:
        from ..assistants import install_claude, install_openai
        from ..scheduler import schedule, unschedule

        cfg = self.cfg()
        steps = []
        cfg["exclude_courses"] = [str(x) for x in body.get("excluded") or []]
        cfg["courses"] = "active"
        cfg["transcribe"] = "auto" if body.get("transcribe", True) else "off"
        interval = int(body.get("interval") or 6)
        cfg["sync_interval_hours"] = interval if interval in INTERVALS else 6
        if body.get("library_dir"):
            cfg["library_dir"] = str(Path(os.path.expanduser(body["library_dir"])))
        cfg["setup_complete"] = True
        save_config(cfg)
        Library(library_path(cfg)).ensure()
        steps.append({"id": "settings", "label": "Saved your settings", "ok": True})
        wanted = body.get("assistants") or {}
        if wanted.get("openai"):
            steps.append(_step("openai", "Connected ChatGPT", install_openai))
        if wanted.get("claude"):
            steps.append(_step("claude", "Connected Claude", lambda: install_claude(desktop=True, code=True)))
        if body.get("start_sync", True):
            # Start the first update here, before automatic updates are switched on (switching them on
            # starts one too), so this window can show its progress.
            self.start_sync()
            lib = self.lib()
            deadline = time.time() + 8
            while time.time() < deadline and self._our_sync_running() and not lib.sync_running():
                time.sleep(0.2)
        if body.get("auto_sync", True):
            result = schedule(cfg["sync_interval_hours"])
            ok = result.startswith("Scheduled")
            steps.append({"id": "schedule", "label": "Turned on automatic updates" if ok else "Automatic updates aren't "
                          "available on this computer", "ok": ok, "detail": "" if ok else "Use Update now instead."})
        else:
            unschedule()
            steps.append({"id": "schedule", "label": "Automatic updates are off", "ok": True})
        return {"ok": True, "steps": steps}

    def save_settings(self, body: Dict[str, Any]) -> Dict[str, Any]:
        from ..scheduler import schedule, schedule_status, unschedule

        cfg = self.cfg()
        if "interval" in body:
            interval = int(body["interval"])
            cfg["sync_interval_hours"] = interval if interval in INTERVALS else 6
        if "transcribe" in body:
            cfg["transcribe"] = "auto" if body["transcribe"] else "off"
        if "accuracy" in body:
            cfg["whisper_model"] = "turbo" if body["accuracy"] == "higher" else ""
        if "keep_videos" in body:
            cfg["keep_media_files"] = bool(body["keep_videos"])
        if "excluded" in body:
            cfg["exclude_courses"] = [str(x) for x in body["excluded"] or []]
        save_config(cfg)
        message = ""
        problem = False
        if "auto_sync" in body or "interval" in body:
            on = body.get("auto_sync", schedule_status().startswith("on"))
            message = schedule(cfg["sync_interval_hours"]) if on else unschedule()
            problem = message.startswith("unavailable") or "refused" in message
        return {"ok": True, "message": message, "problem": problem}

    def connect_assistant(self, which: str) -> Dict[str, Any]:
        from ..assistants import install_claude, install_openai

        if which == "openai":
            return {"ok": True, "notes": install_openai()}
        if which == "claude":
            return {"ok": True, "notes": install_claude(desktop=True, code=True)}
        return {"ok": False, "message": "Unknown assistant."}

    def sign_out(self) -> Dict[str, Any]:
        cfg = self.cfg()
        delete_token(cfg)
        cfg["canvas_user_name"] = ""
        save_config(cfg)
        return {"ok": True}

    # ------------------------------------------------------------------ sync
    def start_sync(self, course: str = "") -> Dict[str, Any]:
        lib = self.lib()
        if lib.sync_running() or self._our_sync_running():
            return {"ok": True, "message": "An update is already running."}
        SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, "-m", "superstudent", "sync"] + (["--course", course] if course else [])
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        log = open(SYNC_LOG, "w", encoding="utf-8")
        kwargs: Dict[str, Any] = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "env": env}
        if os.name != "nt":
            kwargs["start_new_session"] = True     # keeps going if the window closes
        self.sync_proc = subprocess.Popen(args, **kwargs)
        log.close()
        return {"ok": True, "message": "Update started."}

    def sync_progress(self) -> Dict[str, Any]:
        lib = self.lib()
        ours = self._our_sync_running()
        running = ours or lib.sync_running()
        lines: List[str] = []
        if SYNC_LOG.exists():
            try:
                lines = SYNC_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            except OSError:
                lines = []
        phase = _phase(lines) if (ours or (self.sync_proc and not running)) else (
            "Updating in the background" if running else "")
        exit_code = self.sync_proc.poll() if self.sync_proc is not None else None
        if exit_code is not None and any("Another sync is already running" in line for line in lines[-5:]):
            # An automatic update got there first (turning updates on starts one); follow that one instead.
            self.sync_proc = None
            ours, exit_code = False, None
            running = lib.sync_running()
            phase = "Updating in the background" if running else ""
        return {"running": running, "ours": ours, "phase": phase, "exit_code": exit_code,
                "failed": exit_code not in (None, 0), "lines": lines[-12:],
                "auth_error": (lib.meta / "auth_error").exists()}

    # ------------------------------------------------------------------ library actions
    def resolve(self, rel: str) -> Optional[Path]:
        lib = self.lib()
        path = lib.resolve(rel)
        if path is None or not path.exists():
            return None
        return path

    def open_item(self, rel: str, reveal: bool = False) -> Dict[str, Any]:
        path = self.resolve(rel)
        if path is None:
            return {"ok": False, "message": "That file isn't in your library anymore."}
        if path.suffix.lower() == ".md":
            original = path.with_name(path.name[:-3])
            if original.exists() and not reveal:
                path = original
        if not reveal and path.is_file() and path.suffix.lower() not in SAFE_TO_OPEN:
            reveal = True      # a course file that could run something (.command, .pkg, .app…): show it, don't run it
        self.run_open(["-R", str(path)] if reveal else [str(path)])
        return {"ok": True}

    def open_place(self, which: str, folder: str = "") -> Dict[str, Any]:
        lib = self.lib()
        if which == "library":
            lib.ensure()
            self.run_open([str(lib.root)])
        elif which == "course":
            target = self.resolve(folder)
            if not target:
                return {"ok": False, "message": "That course folder isn't there yet."}
            self.run_open([str(target)])
        elif which == "logs":
            (APP_DIR / "logs").mkdir(parents=True, exist_ok=True)
            self.run_open([str(APP_DIR / "logs")])
        elif which in ("chatgpt", "claude"):
            name = "ChatGPT" if which == "chatgpt" else "Claude"
            self.run_open(["-a", name])
        else:
            return {"ok": False, "message": "Unknown place."}
        return {"ok": True}

    def open_url(self, url: str) -> Dict[str, Any]:
        if not re.match(r"^(https://[A-Za-z0-9.-]+|http://(127\.0\.0\.1|localhost))(:\d+)?(/[^\s]*)?$", url or ""):
            return {"ok": False, "message": "Only secure web links can be opened."}
        if DRY_RUN:
            self.actions.append({"url": url})
        elif IS_MAC:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(url)
        return {"ok": True}

    def search(self, q: str, course: str = "", kind: str = "") -> Dict[str, Any]:
        from ..index import search

        hits = search(self.lib(), q, course=course, kind=kind, limit=40, markers=(MARK_ON, MARK_OFF))
        if not kind:   # the summary files repeat what's in the materials; the AI uses them, people don't need them here
            hits = [h for h in hits if h.get("kind") not in SUMMARY_KINDS]
        for h in hits:
            h["snippet"] = _plain_snippet(h.get("snippet") or "")
        # the notes the converter adds for the AI ("Original file: …", "Visual content: …") aren't useful here
        hits = [h for h in hits if not h["snippet"].replace(MARK_ON, "").replace(MARK_OFF, "").startswith(
            ("Original file:", "Visual content:"))]
        return {"ok": True, "hits": hits[:25]}

    def make_pack(self, folder: str, modules: List[int]) -> Dict[str, Any]:
        from ..packs import make_pack

        lib = self.lib()
        if not folder or lib.resolve(folder) is None or not (lib.root / folder).exists():
            return {"ok": False, "message": "Pick a course first."}
        result = make_pack(lib, folder, [int(m) for m in modules] if modules else None, to_pdf=True)
        result["size"] = human_size(result["bytes"])
        self.run_open(["-R", str(result["path"])])
        return {"ok": True, **result}

    def choose_folder(self) -> Dict[str, Any]:
        if not (self.native and self.window is not None):
            return {"ok": False, "message": "Folder picking needs the app window."}
        try:
            import webview

            dialog = getattr(webview, "FileDialog", None)
            kind = dialog.FOLDER if dialog is not None else webview.FOLDER_DIALOG
            chosen = self.window.create_file_dialog(kind, directory=str(Path.home()))
        except Exception as exc:
            return {"ok": False, "message": f"Couldn't open the folder picker ({exc})."}
        if not chosen:
            return {"ok": False, "message": ""}
        from ..cli import _tcc_warning

        folder = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
        target = folder / "SuperStudent" if folder.name != "SuperStudent" else folder
        return {"ok": True, "path": str(target), "warning": _tcc_warning(target)}

    def install_transcription(self) -> Dict[str, Any]:
        log = APP_DIR / "logs" / "install-transcription.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        cmd = transcription_install_command()
        if DRY_RUN:
            self.actions.append({"install": cmd})
            return {"ok": True, "message": "Installing the transcription part in the background (a few minutes)."}
        if cmd is None:
            return {"ok": False, "message": "Couldn't find an installer for this copy of Python. Reinstall Super Student "
                                           "to get lecture transcription."}
        with open(log, "w") as fh:
            subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "message": "Installing the transcription part in the background (a few minutes). "
                                       "Recordings are transcribed on the next update."}

    def uninstall(self, remove_library: bool) -> Dict[str, Any]:
        from ..uninstall import uninstall

        if DRY_RUN:
            self.actions.append({"uninstall": bool(remove_library)})
            return {"ok": True, "notes": [], "canvas_url": self.cfg().get("canvas_url") or ""}
        if self._our_sync_running():
            try:
                self.sync_proc.terminate()
                self.sync_proc.wait(timeout=15)
            except Exception:
                try:
                    self.sync_proc.kill()
                except Exception:
                    pass
        result = uninstall(remove_library=bool(remove_library))
        self.uninstalled = True
        return result

    def dismiss_update(self) -> Dict[str, Any]:
        try:
            UPDATED_FILE.unlink()
        except OSError:
            pass
        return {"ok": True}

    def quit(self) -> Dict[str, Any]:
        def later():
            time.sleep(0.5)
            if self.native and self.window is not None:
                try:
                    self.window.destroy()
                except Exception:
                    pass
            self.stopping = True
        threading.Thread(target=later, daemon=True).start()
        return {"ok": True}

    def focus(self) -> Dict[str, Any]:
        if self.native and self.window is not None:
            try:
                self.window.restore()
                self.window.show()
                from AppKit import NSApplication  # type: ignore

                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:
                pass
        else:
            webbrowser.open(self.url())
        return {"ok": True}

    # ------------------------------------------------------------------ server
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def routes(self) -> Dict[str, Callable[[Dict[str, Any], Dict[str, List[str]]], Dict[str, Any]]]:
        return {
            "GET /api/state": lambda b, q: self.state(),
            "POST /api/canvas/find": lambda b, q: self.find_school(b.get("query", "")),
            "POST /api/canvas/check": lambda b, q: self.check_canvas(b.get("address", "")),
            "POST /api/canvas/connect": lambda b, q: self.connect_canvas(b.get("url", ""), b.get("token", "")),
            "GET /api/canvas/courses": lambda b, q: self.canvas_courses(),
            "POST /api/setup/finish": lambda b, q: self.finish_setup(b),
            "POST /api/settings": lambda b, q: self.save_settings(b),
            "POST /api/assistant/connect": lambda b, q: self.connect_assistant(b.get("which", "")),
            "POST /api/signout": lambda b, q: self.sign_out(),
            "POST /api/sync/start": lambda b, q: self.start_sync(b.get("course", "")),
            "GET /api/sync/progress": lambda b, q: self.sync_progress(),
            "GET /api/search": lambda b, q: self.search((q.get("q") or [""])[0], (q.get("course") or [""])[0],
                                                       (q.get("kind") or [""])[0]),
            "POST /api/open": lambda b, q: self.open_item(b.get("path", ""), bool(b.get("reveal"))),
            "POST /api/open-place": lambda b, q: self.open_place(b.get("which", ""), b.get("folder", "")),
            "POST /api/open-url": lambda b, q: self.open_url(b.get("url", "")),
            "POST /api/pack": lambda b, q: self.make_pack(b.get("folder", ""), b.get("modules") or []),
            "POST /api/choose-folder": lambda b, q: self.choose_folder(),
            "POST /api/install-transcription": lambda b, q: self.install_transcription(),
            "POST /api/ping": lambda b, q: self._ping(),
            "POST /api/focus": lambda b, q: self.focus(),
            "POST /api/uninstall": lambda b, q: self.uninstall(bool(b.get("remove_library"))),
            "POST /api/quit": lambda b, q: self.quit(),
            "POST /api/dismiss-update": lambda b, q: self.dismiss_update(),
            "GET /api/test/actions": lambda b, q: {"actions": self.actions} if DRY_RUN else {"ok": False},
        }

    def _ping(self) -> Dict[str, Any]:
        self.last_ping = time.time()
        return {"ok": True}

    def serve(self, port: int = 0) -> None:
        app = self
        page = (HERE / "index.html").read_text(encoding="utf-8")
        routes = self.routes()

        class Handler(BaseHTTPRequestHandler):
            server_version = "SuperStudent"
            sys_version = ""

            def log_message(self, *args):  # keep quiet
                pass

            def _allowed_host(self) -> bool:
                host = self.headers.get("Host", "")
                return host in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: Any) -> None:
                self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if not self._allowed_host():
                    return self._json(403, {"ok": False, "message": "forbidden"})
                if method == "GET" and parsed.path == "/":
                    if not secrets.compare_digest((query.get("t") or [""])[0], app.token):
                        return self._send(403, b"This page only opens from the Super Student app.", "text/plain")
                    html = page.replace("__SS_TOKEN__", app.token).replace("__SS_NATIVE__", "true" if app.native else "false")
                    return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                if not parsed.path.startswith("/api/"):
                    return self._json(404, {"ok": False})
                if not secrets.compare_digest(self.headers.get("X-SS-Token", ""), app.token):
                    return self._json(403, {"ok": False, "message": "forbidden"})
                handler = routes.get(f"{method} {parsed.path}")
                if handler is None:
                    return self._json(404, {"ok": False, "message": "not found"})
                body: Dict[str, Any] = {}
                if method == "POST":
                    if "application/json" not in (self.headers.get("Content-Type") or ""):
                        return self._json(415, {"ok": False, "message": "JSON only"})
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > 1_000_000:
                        return self._json(413, {"ok": False})
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        return self._json(400, {"ok": False, "message": "bad JSON"})
                try:
                    result = handler(body if isinstance(body, dict) else {}, query)
                except Exception as exc:  # show the problem in the app instead of a blank failure
                    result = {"ok": False, "message": f"Something went wrong: {exc.__class__.__name__}: {exc}"}
                self._json(200, result)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def shutdown(self) -> None:
        if self.server:
            self.server.shutdown()
        try:
            info = read_json(INFO_FILE, {}) or {}
            if info.get("pid") == os.getpid():
                INFO_FILE.unlink()
        except OSError:
            pass

    def write_info(self) -> None:
        if self.uninstalled:
            return
        APP_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(INFO_FILE, {"port": self.port, "token": self.token, "pid": os.getpid(), "version": __version__})
        try:
            os.chmod(INFO_FILE, 0o600)
        except OSError:
            pass


# ---------------------------------------------------------------- small helpers

def _step(step_id: str, label: str, fn: Callable[[], str]) -> Dict[str, Any]:
    try:
        detail = fn()
        ok = "couldn't" not in detail.lower() and "won't edit" not in detail.lower()
        return {"id": step_id, "label": label if ok else label.replace("Connected", "Couldn't fully connect"),
                "ok": ok, "detail": detail}
    except Exception as exc:
        return {"id": step_id, "label": label.replace("Connected", "Couldn't connect"), "ok": False, "detail": str(exc)}


def _plain_snippet(text: str) -> str:
    """Search snippets come from Markdown; show them as plain sentences."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)       # [label](link) -> label
    text = re.sub(r"\*\*|__|`", "", text)                          # bold / code markers
    text = re.sub(r"^[\s…]*[-*>#]+\s+", "", text)                   # a leading bullet, quote or heading mark
    text = re.sub(r"(\s-\s)?(\b\w+:\s*)?https?://\S+", "", text)          # links, with a "Canvas:" style label
    return re.sub(r"\s{2,}", " ", text).strip()


def _status_key(text: str) -> str:
    """'connected' | 'partial' | 'off' from the wording the command line uses."""
    low = (text or "").lower()
    if low.startswith("connected") and "only" not in low:
        return "connected"
    if low.startswith("connected") or low.startswith("partly"):
        return "partial"
    return "off"


def _ago(value: Any) -> str:
    dt = parse_dt(value)
    if not dt:
        return ""
    seconds = max(0, time.time() - dt.timestamp())
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 36 * 3600:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return fmt_dt(value)


_PHASES = [
    (re.compile(r"^Done in"), lambda m, line: "Finished"),
    (re.compile(r"^Search index"), lambda m, line: "Updating the search index"),
    (re.compile(r"✎ transcribing (.+?) with"), lambda m, line: f"Turning {m.group(1)} into text"),
    (re.compile(r"⤓ (.+?) \("), lambda m, line: f"Downloading the recording {m.group(1)}"),
    (re.compile(r"^Media:"), lambda m, line: "Checking lecture recordings"),
    (re.compile(r"^• (.+)$"), lambda m, line: f"Copying {m.group(1)}"),
    (re.compile(r"^Syncing (\d+) course"), lambda m, line: f"Connecting to Canvas ({m.group(1)} courses)"),
]


def _phase(lines: List[str]) -> str:
    for line in reversed(lines):
        text = line.strip()
        for pattern, fmt in _PHASES:
            m = pattern.search(text)
            if m:
                return fmt(m, text)
        if "rejected the saved token" in text:
            return "Canvas didn't accept the saved access token"
        if "Another sync is already running" in text:
            return "Another update is already running"
    return "Starting" if not lines else ""


def _plain_error(err: str) -> str:
    low = err.lower()
    if "sign-in page" in low:
        return "Canvas asked for a login instead of sending the file."
    if "locked" in low:
        return "Locked by your instructor for now."
    if "too large" in low or "size limit" in low:
        return "Too large to download (you can raise the limit in settings)."
    if "network" in low or "connection" in low or "proxy" in low:
        return "A network problem interrupted the download."
    if "youtube" in low:
        return "YouTube doesn't have a transcript for this video."
    return err[:200]


def _plain_reason(reason: str) -> str:
    low = reason.lower()
    if "time budget" in low:
        return "Each update spends up to two hours on this; the rest continue on the next update."
    if "speech model" in low:
        return "The speech model couldn't download; it retries when you're online."
    if "download link" in low:
        return "Canvas doesn't offer these recordings as downloads."
    if "network" in low:
        return "A network problem interrupted it; it retries on the next update."
    return ""


# ---------------------------------------------------------------- launch

def _focus_existing() -> bool:
    """Bring an open Super Student window forward instead of opening a second one. A window left open from an
    older version is closed instead, so the new version opens."""
    info = read_json(INFO_FILE, {}) or {}
    if not info.get("port") or not info.get("token"):
        return False
    base = f"http://127.0.0.1:{info['port']}"
    headers = {"X-SS-Token": info["token"]}
    try:
        if info.get("version") != __version__:
            requests.post(f"{base}/api/quit", json={}, headers=headers, timeout=2)
            for _ in range(20):
                if not INFO_FILE.exists():
                    break
                time.sleep(0.25)
            return False
        resp = requests.post(f"{base}/api/focus", json={}, headers=headers, timeout=2)
        return resp.ok and resp.json().get("ok")
    except (requests.RequestException, ValueError):
        return False


def transcription_install_command() -> Optional[List[str]]:
    """How to add lecture transcription to this Python: the app's own uv (its private Python has no pip), with
    the app's pinned versions when there are any; otherwise pip."""
    uv = APP_DIR / "bin" / "uv"
    packages = ["faster-whisper>=1.1", "av>=11"]
    if uv.exists():
        cmd = [str(uv), "pip", "install", "--python", sys.executable, "--only-binary", ":all:"]
        pins = APP_DIR / "constraints.txt"
        if pins.exists():
            if " " in str(pins):           # uv splits this option's value at spaces: use a copy without any
                import tempfile

                copy = Path(tempfile.gettempdir()) / "superstudent-constraints.txt"
                if " " not in str(copy):
                    shutil.copyfile(pins, copy)
                    pins = copy
            if " " not in str(pins):
                cmd += ["--constraint", str(pins)]
        return cmd + packages
    try:
        import pip  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "pip", "install", "--prefer-binary"] + packages


def _mac_branding(icon: Path) -> None:
    try:
        from Foundation import NSBundle  # type: ignore

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Super Student"
        from AppKit import NSApplication, NSImage  # type: ignore

        image = NSImage.alloc().initWithContentsOfFile_(str(icon))
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        pass


def run(browser: bool = False, open_ui: bool = True, port: int = 0) -> int:
    if open_ui and _focus_existing():
        return 0
    from .. import load_everything

    load_everything()      # an update replacing files on disk can't then mix versions in this running window
    webview = None
    if not browser and open_ui:
        try:
            import webview  # type: ignore
        except Exception:
            webview = None
    app = App(native=webview is not None)
    app.serve(port)
    app.write_info()
    url = app.url()
    try:
        if webview is not None:
            try:
                _mac_branding(HERE / "icon.png")
                app.window = webview.create_window("Super Student", url, width=1100, height=780,
                                                   min_size=(860, 640), background_color="#F5F7FA")
                webview.start()
                return 0
            except Exception as exc:  # no usable native window: fall back to the browser
                print(f"Opening in the browser instead ({exc.__class__.__name__}).", flush=True)
                app.native = False
        if open_ui:
            webbrowser.open(url)
        print(f"Super Student is open at {url}", flush=True)
        # Browser mode: stop when the page has been closed for a while.
        while True:
            time.sleep(1)
            idle = time.time() - (app.last_ping or app.started)
            if app.stopping or (open_ui and ((app.last_ping and idle > 45) or (not app.last_ping and idle > 180))):
                break
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0
