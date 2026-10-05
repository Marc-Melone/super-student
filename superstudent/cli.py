"""superstudent command line."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import (APP_DIR, CONFIG_FILE, DEFAULTS, IS_MAC, delete_token, get_token, library_path, load_config,
                     normalize_canvas_url, save_config, set_token)
from .library import Library, SyncLocked
from .util import fmt_dt, human_size, now_iso

TOKEN_HELP = """How to get a Canvas access token (takes a minute):
  1. Sign in to Canvas in your browser.
  2. Click Account (your picture, top left) > Settings.
  3. Scroll to "Approved Integrations" and click "+ New Access Token".
  4. Purpose: Super Student. Expiry: leave blank or pick the end of the semester. Click Generate Token.
  5. Copy the token (Canvas shows it only once) and paste it below. It won't be shown as you paste.
If there's no "+ New Access Token" button, your school has turned tokens off for students."""


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or default


def _yes(prompt: str, default: bool = True) -> bool:
    answer = _ask(prompt + (" (Y/n)" if default else " (y/N)")).lower()
    if not answer:
        return default
    return answer.startswith("y")


def _canvas(cfg):
    from .canvas import Canvas

    token = get_token(cfg)
    if not cfg.get("canvas_url") or not token:
        raise SystemExit("Super Student isn't set up yet. Run: superstudent setup")
    return Canvas(cfg["canvas_url"], token, log=_print)


# ---------------------------------------------------------------- setup

def cmd_setup(args) -> int:
    from .canvas import AuthError, Canvas, CanvasError

    cfg = load_config()
    _print(f"Super Student {__version__} setup\n")
    _print("Personal use only while Canvas integration approval is unresolved. Use only your own account.\n"
           "Onboarding other users requires an institution-approved OAuth developer key and integration approval.\n")
    url = args.url or _ask("Your Canvas web address (e.g. yourschool.instructure.com)", cfg.get("canvas_url", ""))
    try:
        cfg["canvas_url"] = normalize_canvas_url(url)
    except ValueError as exc:
        _print(str(exc))
        return 1
    token = None
    if args.token_stdin:
        token = sys.stdin.readline().strip()
    else:
        existing = get_token(cfg)
        if existing and _yes("A Canvas token is already saved. Keep using it?"):
            token = existing
        else:
            _print("\n" + TOKEN_HELP + "\n")
            token = getpass.getpass("Canvas access token: ").strip()
    if not token:
        _print("No token entered; setup stopped.")
        return 1
    cv = Canvas(cfg["canvas_url"], token)
    try:
        me = cv.get("/api/v1/users/self")
    except AuthError:
        _print("Canvas rejected that token. Make a new one and run setup again.")
        return 1
    except CanvasError as exc:
        _print(f"Couldn't reach Canvas at {cfg['canvas_url']}: {exc}")
        return 1
    _print(f"\nConnected as {me.get('name')}.")
    where = set_token(cfg, token)
    _print(f"Token saved in {where}.")

    lib_default = cfg.get("library_dir") or DEFAULTS["library_dir"]
    lib_dir = args.library or (lib_default if args.yes else _ask("Folder for your course library", lib_default))
    cfg["library_dir"] = str(Path(os.path.expanduser(lib_dir)))
    warn = _tcc_warning(Path(cfg["library_dir"]))
    if warn:
        _print(warn)

    try:
        courses = [c for c in cv.paginate("/api/v1/courses", {"enrollment_state": "active", "include[]": ["term"]})
                   if c.get("name") and not c.get("access_restricted_by_date")]
    except CanvasError as exc:
        _print(f"Couldn't list courses: {exc}")
        courses = []
    if courses:
        _print("\nYour active courses:")
        for i, c in enumerate(courses, 1):
            _print(f"  {i}. {c.get('course_code') or ''} {c.get('name')} ({(c.get('term') or {}).get('name') or 'no term'}) [id {c['id']}]")
        if args.all_courses or args.yes:
            choice = "all"
        else:
            choice = _ask("Which to sync? 'all', or numbers like 1,3", "all")
        if choice.lower() == "all":
            cfg["courses"] = "active"
        else:
            picked = []
            for part in choice.replace(" ", "").split(","):
                if part.isdigit() and 1 <= int(part) <= len(courses):
                    picked.append(str(courses[int(part) - 1]["id"]))
            cfg["courses"] = picked or "active"
    else:
        _print("No active courses found right now; everything active will sync once it appears.")

    from .media import detect_backend

    backend = detect_backend(cfg.get("whisper_backend", "auto"))
    if backend:
        _print(f"\nLecture transcription: ready ({backend}). The first transcription downloads a speech model (~0.5-1.5 GB).")
    else:
        _print("\nLecture transcription isn't installed, so videos without captions will wait. "
               "Install later with: superstudent doctor")
    save_config(cfg)
    _print(f"Settings saved to {CONFIG_FILE}.")

    if not args.no_schedule and (args.yes or _yes(f"\nSync automatically every {cfg['sync_interval_hours']} hours?")):
        from .scheduler import schedule

        _print(schedule(cfg["sync_interval_hours"]))
    want_claude, want_openai = _pick_assistants(args)
    if want_claude:
        from .assistants import install_claude

        _print("\n" + install_claude(desktop=True, code=True))
    if want_openai:
        from .assistants import install_openai

        _print("\n" + install_openai())
    if args.yes or _yes("\nRun the first sync now? (It can take a while for big courses.)"):
        return cmd_sync(argparse.Namespace(course=[], no_media=False, quiet=False, reindex=False))
    _print("\nDone. Run `superstudent sync` whenever you like.")
    return 0


def _pick_assistants(args) -> tuple:
    """Which assistants to connect: Claude, ChatGPT/Codex, or both."""
    if getattr(args, "assistants", None):
        choice = {"claude": "1", "openai": "2", "chatgpt": "2", "codex": "2", "both": "3", "none": "0"}[args.assistants]
    elif args.yes:
        choice = "3"
    else:
        _print("\nWhich AI will you study with?\n  1. Claude (desktop app and Claude Code)\n"
               "  2. ChatGPT (desktop app: Work, Chat and Codex)\n  3. Both\n  0. Neither for now")
        choice = _ask("Choose", "3")
    want_claude = choice in ("1", "3") and not getattr(args, "no_claude", False)
    want_openai = choice in ("2", "3") and not getattr(args, "no_openai", False)
    return want_claude, want_openai


def _tcc_warning(path: Path) -> str:
    if not IS_MAC:
        return ""
    home = Path.home()
    protected = [home / "Documents", home / "Desktop", home / "Downloads", home / "Library" / "Mobile Documents",
                 home / "Library" / "CloudStorage", Path("/Volumes")]
    if any(p == path or p in path.parents for p in protected):
        return ("Note: macOS limits background access to Documents, Desktop, Downloads, iCloud Drive, cloud folders "
                "(OneDrive, Google Drive, Dropbox) and external or network drives, so scheduled updates may be blocked "
                "there. A folder directly in your home folder (the default) avoids that.")
    return ""


# ---------------------------------------------------------------- sync / status

def cmd_sync(args) -> int:
    from . import load_everything
    from .canvas import AuthError
    from .sync import Syncer

    load_everything()      # an update replacing files on disk mid-sync can't then mix old and new code

    cfg = load_config()
    cv = _canvas(cfg)
    lib = Library(library_path(cfg))
    log = (lambda m: None) if args.quiet else _print
    syncer = Syncer(cfg, cv, lib, log=log, only=args.course, media=not args.no_media)
    try:
        report = syncer.run()
    except SyncLocked:
        _print("Another sync is already running; try again when it finishes.")
        return 0
    except AuthError:
        lib.log("sync failed: Canvas rejected the token")
        try:
            lib.ensure()
            lib.checked(lib.meta / "auth_error").write_text(now_iso())
        except OSError:
            pass
        _print("Canvas rejected the saved token (expired or revoked). Make a new one and run: superstudent setup")
        return 2
    try:
        (lib.meta / "auth_error").unlink()
    except OSError:
        pass
    if args.reindex:
        from .index import update_index

        update_index(lib, log=log, full=True)
    if not args.quiet:
        _print(f"\nDone in {report.get('seconds')}s ({report.get('api_calls')} Canvas requests). Library: {lib.root}")
        for c in report.get("courses", []):
            st = c.get("stats", {})

            def n(count: int, one: str, many: str) -> str:
                return f"{count} {one if count == 1 else many}"

            bits = [n(st.get("files_downloaded", 0), "file downloaded", "files downloaded"),
                    n(st.get("docs_written", 0), "page/item updated", "pages/items updated")]
            if st.get("media_done"):
                bits.append(n(st["media_done"], "transcript", "transcripts"))
            if st.get("media_pending"):
                bits.append(n(st["media_pending"], "video waiting for a transcript", "videos waiting for transcripts"))
            if st.get("files_failed"):
                bits.append(n(st["files_failed"], "file failed", "files failed") + " (see: superstudent status)")
            _print(f"  {c['name']}: " + ", ".join(bits))
        for err in report.get("errors", []):
            _print(f"  ! {err}")
    return 0


def cmd_status(args) -> int:
    from .mcp_server import _status_text
    from .scheduler import schedule_status

    cfg = load_config()
    lib = Library(library_path(cfg))
    _print(f"Canvas: {cfg.get('canvas_url') or '(not set up)'}")
    _print(f"Library: {lib.root}")
    _print(f"Automatic sync: {schedule_status()}")
    _print(_status_text(lib))
    return 0


def cmd_courses(args) -> int:
    cfg = load_config()
    cv = _canvas(cfg)
    for c in cv.paginate("/api/v1/courses", {"enrollment_state": args.state, "include[]": ["term"]}):
        if c.get("name"):
            _print(f"{c['id']:>10}  {(c.get('term') or {}).get('name') or '':<16} {c.get('course_code') or ''}  {c['name']}")
    return 0


# ---------------------------------------------------------------- search / read / render

def cmd_search(args) -> int:
    from .index import search

    lib = Library(library_path(load_config()))
    hits = search(lib, " ".join(args.query), course=args.course or "", kind=args.kind or "", limit=args.n)
    if args.json:
        _print(json.dumps(hits, indent=1))
        return 0
    if not hits:
        _print("No matches. Try other words or drop --course/--kind.")
        return 1
    for i, h in enumerate(hits, 1):
        gone = "  (removed from Canvas)" if h.get("removed") else ""
        _print(f"{i}. {h['title']}  [{h['locator'] or 'start'}]  ({h['kind']}){gone}\n   {h['path']}\n   {h['snippet']}\n")
        if h.get("message"):
            _print("   Source warning: " + h["message"] + "\n")
    return 0


def cmd_read(args) -> int:
    from .index import read_document

    lib = Library(library_path(load_config()))
    try:
        _print(read_document(lib, args.path, locator=args.at or "", start=args.start, max_chars=args.max,
                             neighbors=args.neighbors))
    except FileNotFoundError as exc:
        _print(str(exc))
        return 1
    return 0


def cmd_render(args) -> int:
    from .render import RenderError, render

    lib = Library(library_path(load_config()))
    try:
        for path in render(lib, args.path, page=args.page):
            _print(str(path))
    except RenderError as exc:
        _print(str(exc))
        return 1
    return 0


def cmd_pack(args) -> int:
    """Copy the originals for some modules into one folder you can drag into a Claude or ChatGPT chat."""
    from .mcp_server import _find_course
    from .packs import make_pack, parse_range

    lib = Library(library_path(load_config()))
    folder = _find_course(lib, args.course)
    if not folder:
        _print(f"No course matching {args.course!r}.")
        return 1
    result = make_pack(lib, folder, parse_range(args.modules) if args.modules else None,
                       name=args.name or (f"Modules {args.modules}" if args.modules else ""),
                       out=Path(args.out) if args.out else None, to_pdf=args.pdf)
    _print(f"Exam pack ready: {result['path']}\n{result['files']} files, {human_size(result['bytes'])}. Drag the folder's "
           f"files into a Claude or ChatGPT chat or project for a deep session where the AI reads the original slides "
           f"and PDFs visually.")
    if not args.pdf:
        _print("Tip: add --pdf to turn slide decks into PDFs (needs LibreOffice), so each slide is seen exactly as it looks.")
    return 0


# ---------------------------------------------------------------- schedule / claude / config

def cmd_schedule(args) -> int:
    from .scheduler import schedule

    cfg = load_config()
    if args.every:
        cfg["sync_interval_hours"] = args.every
        save_config(cfg)
    _print(schedule(cfg["sync_interval_hours"]))
    return 0


def cmd_unschedule(args) -> int:
    from .scheduler import unschedule

    _print(unschedule())
    return 0


def cmd_install_claude(args) -> int:
    from .assistants import install_claude

    both = not args.desktop and not args.code
    _print(install_claude(desktop=args.desktop or both, code=args.code or both))
    return 0


def cmd_install_openai(args) -> int:
    from .assistants import install_openai

    _print(install_openai())
    return 0


def cmd_config(args) -> int:
    cfg = load_config()
    if not args.key:
        shown = {k: cfg.get(k) for k in DEFAULTS}
        _print(json.dumps(shown, indent=2))
        return 0
    if args.key not in DEFAULTS:
        _print(f"Unknown setting {args.key!r}. Settings: {', '.join(DEFAULTS)}")
        return 1
    if args.value is None:
        _print(json.dumps(cfg.get(args.key)))
        return 0
    default = DEFAULTS[args.key]
    value: object = args.value
    if isinstance(default, bool):
        value = args.value.lower() in ("1", "true", "yes", "on")
    elif isinstance(default, (int, float)) and not isinstance(default, bool):
        value = type(default)(float(args.value)) if isinstance(default, int) else float(args.value)
    elif isinstance(default, list) or (args.key == "courses" and args.value != "active"):
        value = [v.strip() for v in args.value.split(",") if v.strip()]
    cfg[args.key] = value
    save_config(cfg)
    _print(f"{args.key} = {json.dumps(value)}")
    return 0


def cmd_logout(args) -> int:
    cfg = load_config()
    delete_token(cfg)
    _print("Removed the saved Canvas token. Also delete it in Canvas: Account > Settings > Approved Integrations.")
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import run

    run()
    return 0


def cmd_visuals(args) -> int:
    from .describe import survey

    lib = Library(library_path(load_config()))
    info = survey(lib, course=args.course or "", limit=args.n)
    _print(f"{info['described']} of {info['visuals']} pictures have descriptions.")
    for t in info["todo"]:
        _print(f"- {t['path']}  |  {t['unit']}  |  {t['why']}")
    return 0


def cmd_describe(args) -> int:
    from .describe import save

    lib = Library(library_path(load_config()))
    result = save(lib, args.path, args.at, args.text, by=args.by or "")
    _print(f"Saved ({result['path']}, {result['unit']})." if result.get("ok") else result.get("message", "Not saved."))
    return 0 if result.get("ok") else 1


def cmd_study(args) -> int:
    from .notes import progress

    lib = Library(library_path(load_config()))
    info = progress(lib, course=args.course or "", limit=args.n)
    _print(f"Current referenced notes: {info['done']} of {info['docs']} documents.")
    for c in info["courses"]:
        _print(f"\n{c['label']}: {c['done']} of {c['docs']} with current referenced notes; course notes: {c['course_notes']}")
        _print("Page/slide references do not verify reading, understanding or mastery.")
        for t in c["todo"]:
            _print(f"- {t['path']}  |  {t['status']}")
    return 0


def cmd_save_notes(args) -> int:
    from .notes import save

    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    result = save(Library(library_path(load_config())), args.path, text, by=args.by or "")
    _print(result.get("message", "Not saved."))
    return 0 if result.get("ok") else 1


def cmd_uninstall(args) -> int:
    from .uninstall import uninstall

    if not args.yes:
        answer = input("Remove Super Student's automatic updates, AI connectors and saved token? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            _print("Nothing changed.")
            return 0
    result = uninstall(remove_library=args.remove_library)
    for note in result["notes"]:
        _print(note)
    _print("Also delete the access token in Canvas: Account > Settings > Approved Integrations.")
    return 0


def cmd_gui(args) -> int:
    from .gui.app import run

    return run(browser=args.browser, open_ui=not args.no_open, port=args.port)


# ---------------------------------------------------------------- doctor

def cmd_doctor(args) -> int:
    import importlib.util

    from .extract import soffice_path, tesseract_available
    from .media import detect_backend
    from .scheduler import schedule_status

    cfg = load_config()
    ok = lambda b: "ok " if b else "-- "  # noqa: E731
    _print(f"Super Student {__version__} · Python {platform.python_version()} · {platform.system()} {platform.machine()}\n")
    for mod, why in [("pymupdf", "PDFs"), ("pptx", "PowerPoint"), ("mammoth", "Word"), ("openpyxl", "Excel"),
                     ("bs4", "Canvas pages"), ("markdownify", "Canvas pages"), ("mcp", "Claude app connector")]:
        _print(f"{ok(importlib.util.find_spec(mod) is not None)} {why} ({mod})")
    backend = detect_backend(cfg.get("whisper_backend", "auto"))
    _print(f"{ok(bool(backend))} Lecture transcription: {backend or 'not installed'}")
    if not backend:
        from .gui.app import transcription_install_command

        cmd = transcription_install_command()
        _print("     install: " + (" ".join(f"'{c}'" if (" " in c or ">" in c) else c for c in cmd) if cmd
                                   else "reinstall Super Student"))
    frames = importlib.util.find_spec("av") is not None and importlib.util.find_spec("PIL") is not None
    _print(f"{ok(frames)} Lecture screen snapshots (PyAV + Pillow)")
    from . import ocr

    _print(f"{ok(ocr.available())} Reading text in pictures (labels, scans, lecture screens): "
           f"{ocr.engine_label() or 'not available (on a Mac this is built in; elsewhere: install tesseract)'}")
    _print(f"{ok(bool(soffice_path()))} Rendering slides as images, old .ppt/.doc files (optional: brew install --cask libreoffice)")
    _print(f"{ok(importlib.util.find_spec('youtube_transcript_api') is not None)} YouTube transcripts")
    token = get_token(cfg)
    _print(f"\n{ok(bool(cfg.get('canvas_url')))} Canvas address: {cfg.get('canvas_url') or 'not set (run setup)'}")
    if token and cfg.get("canvas_url"):
        from .canvas import Canvas, CanvasError

        try:
            me = Canvas(cfg["canvas_url"], token).get("/api/v1/users/self")
            _print(f"ok  Token works (signed in as {me.get('name')})")
        except CanvasError as exc:
            _print(f"--  Token check failed: {exc}")
    else:
        _print("--  No token saved (run setup)")
    lib = Library(library_path(cfg))
    _print(f"\nLibrary: {lib.root} ({'exists' if lib.root.exists() else 'not created yet'})")
    warn = _tcc_warning(lib.root)
    if warn:
        _print("!!  " + warn)
    _print(f"Automatic sync: {schedule_status()}")
    from .assistants import claude_status, openai_status

    _print(f"Claude: {claude_status()}")
    _print(f"ChatGPT and Codex: {openai_status()}")
    last = lib.last_sync()
    _print(f"Last sync: {fmt_dt(last.get('finished')) or 'never'}")
    return 0


# ---------------------------------------------------------------- entry point

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="superstudent", description="Mirror Canvas into a study library for Claude and ChatGPT.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("setup", help="connect to Canvas and choose courses")
    s.add_argument("--url")
    s.add_argument("--library")
    s.add_argument("--token-stdin", action="store_true", help="read the token from standard input")
    s.add_argument("--all-courses", action="store_true")
    s.add_argument("--yes", action="store_true", help="accept defaults without asking")
    s.add_argument("--no-schedule", action="store_true")
    s.add_argument("--assistants", choices=["claude", "openai", "chatgpt", "codex", "both", "none"],
                   help="which AI to connect (default: ask; 'both' with --yes)")
    s.add_argument("--no-claude", action="store_true")
    s.add_argument("--no-openai", action="store_true")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("sync", help="pull the latest from Canvas")
    s.add_argument("--course", action="append", default=[], help="only courses matching this (repeatable)")
    s.add_argument("--no-media", action="store_true", help="skip transcripts this time")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--reindex", action="store_true", help="rebuild the search index from scratch")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("status", help="last sync, pending transcripts, problems")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("courses", help="list your Canvas courses and their ids")
    s.add_argument("--state", default="active", choices=["active", "completed", "invited_or_pending"])
    s.set_defaults(func=cmd_courses)

    s = sub.add_parser("search", help="search all course materials")
    s.add_argument("query", nargs="+")
    s.add_argument("--course")
    s.add_argument("--kind", help="slides, lecture, reading, assignment, announcement, …")
    s.add_argument("-n", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("read", help="print a document or one section of it")
    s.add_argument("path")
    s.add_argument("--at", help="section locator, e.g. 'Slide 7', 'Page 12', '00:32'")
    s.add_argument("--neighbors", type=int, default=0)
    s.add_argument("--start", type=int, default=0)
    s.add_argument("--max", type=int, default=16000)
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("render", help="render a page or slide to a PNG and print its path")
    s.add_argument("path")
    s.add_argument("--page", type=int, default=1)
    s.set_defaults(func=cmd_render)

    s = sub.add_parser("pack", help="bundle original slides/PDFs for some modules (for a Claude chat or Project)")
    s.add_argument("--course", required=True)
    s.add_argument("--modules", help="e.g. 1-6 or 3,4,7")
    s.add_argument("--name")
    s.add_argument("--out")
    s.add_argument("--pdf", action="store_true", help="include slide decks as PDFs (Word files need LibreOffice)")
    s.set_defaults(func=cmd_pack)

    s = sub.add_parser("schedule", help="sync automatically")
    s.add_argument("--every", type=float, help="hours between syncs (default 6)")
    s.set_defaults(func=cmd_schedule)
    s = sub.add_parser("unschedule", help="stop automatic syncs")
    s.set_defaults(func=cmd_unschedule)

    s = sub.add_parser("install-claude", help="connect the library to the Claude desktop app and Claude Code")
    s.add_argument("--desktop", action="store_true")
    s.add_argument("--code", action="store_true")
    s.set_defaults(func=cmd_install_claude)

    s = sub.add_parser("install-openai", aliases=["install-chatgpt", "install-codex"],
                       help="connect the library to the ChatGPT desktop app (Work, Chat, Codex) and the Codex CLI")
    s.set_defaults(func=cmd_install_openai)

    s = sub.add_parser("config", help="show or change a setting")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("doctor", help="check what's installed and working")
    s.set_defaults(func=cmd_doctor)
    s = sub.add_parser("logout", help="remove the saved Canvas token")
    s.set_defaults(func=cmd_logout)
    s = sub.add_parser("mcp", help="run the Claude connector (Claude starts this itself)")
    s.set_defaults(func=cmd_mcp)
    s = sub.add_parser("visuals", help="pictures (diagrams, figures, scans) that don't have a description yet")
    s.add_argument("--course")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(func=cmd_visuals)
    s = sub.add_parser("describe", help="save a description of a picture so searches can find it")
    s.add_argument("path")
    s.add_argument("--at", required=True, help="'Slide 12', 'Page 3' or 'Image'")
    s.add_argument("--text", required=True)
    s.add_argument("--by", help="who described it, e.g. Claude or ChatGPT")
    s.set_defaults(func=cmd_describe)

    s = sub.add_parser("study", help="study-pass progress: which documents have AI study notes")
    s.add_argument("--course")
    s.add_argument("-n", type=int, default=10)
    s.set_defaults(func=cmd_study)
    s = sub.add_parser("save-notes", help="save study notes for a document, module folder or course folder")
    s.add_argument("path")
    s.add_argument("--file", required=True, help="Markdown file with the notes, or - for standard input")
    s.add_argument("--by", help="who wrote them, e.g. Claude or ChatGPT")
    s.set_defaults(func=cmd_save_notes)

    s = sub.add_parser("uninstall", help="remove automatic updates, AI connectors, the saved token and settings")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--remove-library", action="store_true", help="also move your course folder to the Trash (Mac)")
    s.set_defaults(func=cmd_uninstall)
    s = sub.add_parser("gui", aliases=["app"], help="open the Super Student app (setup and dashboard)")
    s.add_argument("--browser", action="store_true", help="open in your web browser instead of an app window")
    s.add_argument("--no-open", action="store_true", help="just print the address")
    s.add_argument("--port", type=int, default=0)
    s.set_defaults(func=cmd_gui)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _print("\nStopped.")
        return 130
