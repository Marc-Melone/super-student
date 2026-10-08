"""Pull everything a student can see in each Canvas course into the library folder.

Layout of one course (inside ~/SuperStudent/<Term>/<Course>/):

    COURSE_OVERVIEW.md   what the course is, grading, key dates, module outline, gaps
    EXAM_INTEL.md        every exam hint from announcements, syllabus, slides and lectures
    CALENDAR.md          deadlines and events, past and upcoming
    GRADES.md            scores, rubric results and instructor feedback
    LINKS.md             outside links and things Canvas won't hand over (publisher sites, Panopto…)
    Syllabus.md
    Modules/03 - Week 3/ files, pages and lecture transcripts in course order + _Module Contents.md
    Files/               course files that aren't in a module (mirrors the Canvas folders)
    Pages/ Assignments/ Quizzes/ Discussions/ Announcements/ Media/
    My Files/            drop your own notes or textbook PDFs here; they get indexed too

Every original file is kept; each gets a "<name>.md" text version next to it, and
visuals (slide images, lecture screen snapshots) go in "<name>.assets/".
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import threading
import time
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote

from .canvas import AuthError, Canvas, CanvasError, DownloadError, ForbiddenError, NotFoundError
from .content import TOKEN_RE, YOUTUBE_RE, Refs, block_editor_html, html_text, html_to_markdown, platform_name
from . import describe, exams, notes
from .extract import EXTRACTOR_VERSION, MEDIA_EXT, extract, kind_of
from .library import Library
from .media import (Segment, Transcriber, TranscriberUnavailable, extract_keyframes, fmt_ts, media_duration, parse_captions,
                    pick_media_source, transcript_markdown, youtube_title, youtube_transcript)
from .util import (REMOVED_DIR, add_suffix, atomic_write_text, day_key, file_digest, fmt_date, fmt_dt, front_matter, human_size,
                   now_iso, parse_dt, parse_front_matter, read_json, safe_name, truncate)

COURSE_INCLUDES = ["term", "teachers", "syllabus_body", "total_scores"]
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpeg", ".mpg", ".3gp"}
LINKED_DIRS = {
    "page": "Files/Linked from pages", "assignment": "Files/Linked from assignments",
    "quiz": "Files/Linked from quizzes", "discussion": "Files/Linked from discussions",
    "announcement": "Files/Linked from announcements", "syllabus": "Files/Linked from the syllabus",
}
DOC_VERSION = 2          # bump when pages/discussions should be fetched and rendered again
REFERENCE_TABS = ("modules", "pages", "assignments", "quizzes", "discussions", "announcements")
MAX_LINKED_PAGES = 300


class Syncer:
    def __init__(self, cfg: Dict[str, Any], canvas: Canvas, library: Library, *,
                 log: Callable[[str], None] = print, only: Optional[List[str]] = None, media: bool = True):
        self.cfg = cfg
        self.cv = canvas
        self.lib = library
        self.log = log
        self.only = [o.lower() for o in (only or []) if o]
        self.state = library.load_state()
        self.sync_id = now_iso()
        self.extract_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.media_jobs: List[Dict[str, Any]] = []
        self.courses: List["CourseSync"] = []
        self.media_enabled = media and str(cfg.get("transcribe", "auto")) != "off"
        self.transcriber = Transcriber(cfg.get("whisper_backend", "auto"), cfg.get("whisper_model", ""), log) \
            if self.media_enabled else None
        self.report: Dict[str, Any] = {"started": self.sync_id, "courses": [], "errors": []}

    # ------------------------------------------------------------------ main
    def run(self) -> Dict[str, Any]:
        self.lib.ensure()
        with self.lib.lock():
            started = time.time()
            self.state = self.lib.load_state()     # re-read under the lock: another sync may have just saved
            _clean_leftovers(self.lib)
            me = self.cv.get("/api/v1/users/self") or {}
            self.report["user"] = me.get("name") or me.get("short_name") or ""
            courses = self.list_courses()
            self.log(f"Syncing {len(courses)} course(s) from {self.cv.base}")
            for course in courses:
                cs = CourseSync(self, course)
                self.courses.append(cs)
                try:
                    cs.run()
                except AuthError:
                    raise
                except Exception as exc:  # one broken course never stops the others
                    if self.cv.auth_failed:
                        raise self.cv.auth_failed
                    cs.warn(f"Sync stopped early: {exc.__class__.__name__}: {exc}")
                    self.report["errors"].append(f"{cs.title}: {exc}")
                    try:
                        cs.write_docs()
                    except Exception:
                        pass
                self.lib.save_state(self.state)
            self.process_media()
            for cs in self.courses:
                try:
                    cs.finish()
                except Exception as exc:
                    self.report["errors"].append(f"{cs.title} (overview): {exc}")
                    try:
                        cs.write_docs()
                    except Exception:
                        pass
            from .guides import install_library_guides
            from .index import update_index
            from .overview import write_library_index

            install_library_guides(self.lib)
            self.report["indexed"] = update_index(self.lib, log=self.log)
            write_library_index(self.lib, self.state)
            self.lib.save_state(self.state)
            self.report["finished"] = now_iso()
            self.report["seconds"] = round(time.time() - started, 1)
            self.report["api_calls"] = self.cv.calls
            self.report["courses"] = [cs.summary() for cs in self.courses]
            from .util import atomic_write_json

            atomic_write_json(self.lib.checked(self.lib.last_sync_path), self.report)
            self.lib.log(f"sync ok: {len(self.courses)} courses, {self.cv.calls} API calls, "
                         f"{self.report['seconds']}s, errors={len(self.report['errors'])}")
        return self.report

    def list_courses(self) -> List[Dict[str, Any]]:
        wanted = self.cfg.get("courses", "active")
        courses: List[Dict[str, Any]] = []
        if isinstance(wanted, list) and wanted:
            for cid in wanted:
                try:
                    courses.append(self.cv.get(f"/api/v1/courses/{cid}", {"include[]": COURSE_INCLUDES}))
                except CanvasError as exc:
                    self.report["errors"].append(f"Course {cid}: {exc}")
        else:
            params = {"enrollment_state": "active", "include[]": COURSE_INCLUDES}
            courses = [c for c in self.cv.paginate("/api/v1/courses", params)
                       if c.get("name") and not c.get("access_restricted_by_date")]
        skip = {str(x) for x in self.cfg.get("exclude_courses") or []}
        courses = [c for c in courses if str(c.get("id")) not in skip]
        if self.only:
            courses = [c for c in courses if any(o in f"{c.get('id')} {c.get('name', '')} {c.get('course_code', '')}".lower()
                                                 for o in self.only)]
        return courses

    # ------------------------------------------------------------------ media
    def process_media(self) -> None:
        if not self.media_jobs:
            return
        budget = float(self.cfg.get("media_minutes_per_sync") or 120) * 60
        started = time.time()
        todo = [j for j in self.media_jobs if not j["course"].media_up_to_date(j)]
        if todo:
            self.log(f"Media: {len(todo)} video/audio item(s) to process")
        for job in todo:                                   # captions and YouTube: quick
            try:
                job["course"].media_quick(job)
            except AuthError:
                raise
            except Exception as exc:
                job["course"].media_failed(job, exc)
        backend_problem = ""
        for job in todo:                                   # local transcription: slow, budgeted
            if job.get("done"):
                continue
            cs = job["course"]
            if not job.get("captions") and not (self.transcriber and self.transcriber.available):
                cs.media_pending(job, "no captions on Canvas, and local transcription isn't installed "
                                      "(run: superstudent doctor)")
                continue
            if backend_problem and not job.get("captions"):
                cs.media_pending(job, backend_problem)
                continue
            if time.time() - started > budget:
                cs.media_pending(job, "this sync's transcription time budget was used up; it continues next sync")
                continue
            try:
                cs.media_transcribe(job)
            except AuthError:
                raise
            except TranscriberUnavailable as exc:        # not this video's fault: wait, don't count a failure
                backend_problem = str(exc)
                cs.media_pending(job, backend_problem)
            except (DownloadError, CanvasError) as exc:
                if "network error" in str(exc):
                    cs.media_pending(job, "network problem while downloading; it retries next sync")
                else:
                    cs.media_failed(job, exc)
            except Exception as exc:
                cs.media_failed(job, exc)
            self.lib.save_state(self.state)


class CourseSync:
    def __init__(self, syncer: Syncer, course: Dict[str, Any]):
        self.s = syncer
        self.cv = syncer.cv
        self.cfg = syncer.cfg
        self.course = course
        self.cid = str(course["id"])
        self.base = syncer.cv.base
        cstate = syncer.state["courses"].setdefault(self.cid, {})
        self.cstate = cstate
        self.items: Dict[str, Dict[str, Any]] = cstate.setdefault("items", {})
        self.title = course.get("name") or f"Course {self.cid}"
        self.folder = cstate.get("folder") or self._folder_name()
        cstate["folder"] = self.folder
        cstate["name"] = self.title
        cstate["code"] = course.get("course_code") or ""
        cstate["term"] = (course.get("term") or {}).get("name") or ""
        self.dir = syncer.lib.checked(syncer.lib.root / self.folder)
        self.old = syncer.lib.load_snapshot(self.cid)
        self.snap: Dict[str, Any] = {}
        self.stats: Counter = Counter()
        self._stats_lock = threading.Lock()
        self.warnings: List[str] = []
        self.listing_ok: Set[str] = set()
        self.visible_tabs: Optional[Set[str]] = None
        self.claims: Dict[str, str] = {}
        for key, it in self.items.items():               # reserve paths of everything already on disk
            if it.get("path"):
                self.claims.setdefault(it["path"].lower(), key)
        self.pending_docs: List[Tuple[str, str, Dict[str, Any], str]] = []
        self.file_place: Dict[str, Tuple[tuple, str, str]] = {}
        self.file_meta: Dict[str, Dict[str, Any]] = {}
        self.file_contexts: Dict[str, List[str]] = {}
        self.file_errors: Dict[str, str] = {}
        self.module_pages: Dict[str, Tuple[str, str]] = {}
        self.module_of: Dict[str, Tuple[int, str, str]] = {}
        self.module_notes: Dict[str, str] = {}
        self.page_paths: Dict[str, str] = {}
        self.media: Dict[str, Dict[str, Any]] = {}
        self.alias: Dict[str, str] = {}
        self.links: List[Dict[str, str]] = []
        self.unreachable: List[Dict[str, str]] = []
        self.teacher_ids = {str(t.get("id")) for t in course.get("teachers") or []}
        self.teacher_names = {t.get("display_name") for t in course.get("teachers") or [] if t.get("display_name")}
        self.linked_pages: Dict[str, str] = {}       # page slugs other content links to -> where the link is
        self.fetched_pages: Set[str] = set()
        self.module_fallback: Set[str] = set()       # kinds fetched one by one because their list failed
        self.cache_dir = syncer.lib.checked(syncer.lib.meta / "cache" / self.cid)
        self.completed = False
        self.written_docs: List[Tuple[str, str, Dict[str, Any], str]] = []

    # ------------------------------------------------------------------ helpers
    def _write(self, path: Path, text: str) -> bool:
        return atomic_write_text(self.s.lib.checked(path), text)

    def _folder_name(self) -> str:
        term = (self.course.get("term") or {}).get("name") or ""
        if not term or term.lower() in ("default term", "default"):
            term = "Other"
        code = (self.course.get("course_code") or "").strip()
        name = self.title.strip()
        title = name if (not code or code.lower() in name.lower()) else f"{code} - {name}"
        folder = f"{safe_name(term, 50)}/{safe_name(title, 90)}"
        used = {c.get("folder", "").lower() for k, c in self.s.state["courses"].items() if k != self.cid}
        if folder.lower() in used:
            folder = f"{folder} ({self.cid})"
        return folder

    def bump(self, name: str, n: int = 1) -> None:
        with self._stats_lock:
            self.stats[name] += n

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self.s.log(f"  ! {message}")

    def tab_hidden(self, tab: str) -> bool:
        return self.visible_tabs is not None and tab not in self.visible_tabs

    def listing_failed(self, what: str, exc: Exception, tab: Optional[str] = None) -> None:
        if tab and self.tab_hidden(tab):
            return  # instructor hid it from students: expected, not an error
        if isinstance(exc, (ForbiddenError, NotFoundError)) or getattr(exc, "status", 0) == 401:
            self.warn(f"{what}: Canvas says you don't have access ({getattr(exc, 'status', '')}); kept what was synced before")
        else:
            self.warn(f"{what} failed ({exc}); kept what was synced before")

    def item(self, key: str) -> Dict[str, Any]:
        with self.s.state_lock:
            return self.items.setdefault(key, {})

    def seen(self, key: str) -> Dict[str, Any]:
        it = self.item(key)
        it["seen"] = self.s.sync_id
        it.pop("removed", None)
        it.pop("removed_on", None)
        return it

    def claim(self, key: str, rel: str) -> str:
        rel = rel.replace("\\", "/").strip("/")
        owner = self.claims.get(rel.lower())
        if owner and owner != key:
            rel = add_suffix(rel, key.split(":")[-1][-20:])
        self.claims[rel.lower()] = key
        return rel

    def relocate(self, key: str, new_rel: str) -> None:
        prev = self.items.get(key, {}).get("path")
        if not prev or prev == new_rel:
            return
        self._move_files(prev, new_rel)
        if prev.startswith(REMOVED_DIR + "/"):          # it's back on Canvas
            it = self.items.get(key) or {}
            text = new_rel if new_rel.endswith(".md") and not it.get("text") else new_rel + ".md"
            _unstamp_removed(self.s.lib.checked(self.dir / text))

    def _move_files(self, prev: str, new_rel: str) -> None:
        """Move an item's original, text version, pictures and unzipped folder, and what the AI wrote about it."""
        for suffix in ("", ".md", ".assets", " (unzipped)"):
            old = self.s.lib.checked(self.dir / (prev + suffix))
            new = self.s.lib.checked(self.dir / (new_rel + suffix))
            if (suffix == "" or not prev.endswith(".md")) and old.exists() and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, new)
        if prev.endswith(".md") and new_rel.endswith(".md"):
            old_assets = self.s.lib.checked(self.dir / (prev[:-3] + ".assets"))
            new_assets = self.s.lib.checked(self.dir / (new_rel[:-3] + ".assets"))
            if old_assets.exists() and not new_assets.exists():
                os.replace(old_assets, new_assets)
        lib_old, lib_new = f"{self.folder}/{prev}", f"{self.folder}/{new_rel}"
        try:
            describe.move(self.s.lib, lib_old, lib_new)
            notes.move(self.s.lib, lib_old, lib_new)
            if not prev.endswith(".md"):     # notes on a recording are kept under its transcript, 'X.mp4.md'
                notes.move(self.s.lib, lib_old + ".md", lib_new + ".md")
        except Exception as exc:          # bookkeeping only; never stop a sync over it
            self.s.lib.log(f"couldn't move notes/descriptions for {lib_old}: {exc}")
        try:
            exams.move(self.s.lib, lib_old, lib_new)     # exam practice citing it follows it too
        except Exception as exc:
            self.s.lib.log(f"couldn't move exam practice for {lib_old}: {exc}")
        self._prune(self.dir / prev)

    def _prune(self, path: Path) -> None:
        parent = path.parent
        while parent != self.dir and self.dir in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    # ------------------------------------------------------------------ rendered-doc cache
    def _cache_file(self, key: str) -> Path:
        return self.s.lib.checked(self.cache_dir / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:24] + ".json"))

    def cache_save(self, key: str, meta: Dict[str, Any], body: str) -> None:
        """Keep a page's or discussion's Markdown (with its Canvas link placeholders) so an unchanged one can be
        re-linked on later syncs without asking Canvas for it again."""
        try:
            text = json.dumps({"key": key, "meta": meta, "body": body}, ensure_ascii=False, sort_keys=True)
            self._write(self._cache_file(key), text)
        except OSError:
            pass

    def cache_load(self, key: str) -> Optional[Tuple[Dict[str, Any], str]]:
        data = read_json(self._cache_file(key), None)
        if isinstance(data, dict) and data.get("key") == key and isinstance(data.get("body"), str):
            return dict(data.get("meta") or {}), data["body"]
        return None

    def canvas_url(self, kind: str, ident: str) -> str:
        paths = {"file": f"files/{ident}", "page": f"pages/{ident}", "assignment": f"assignments/{ident}",
                 "quiz": f"quizzes/{ident}", "discussion": f"discussion_topics/{ident}"}
        return f"{self.base}/courses/{self.cid}/{paths.get(kind, '')}"

    def meta(self, title: str, kind: str, **extra: Any) -> Dict[str, Any]:
        out = {"title": title, "course": self.course_label(), "type": kind}
        out.update(extra)
        return out

    def course_label(self) -> str:
        code = self.course.get("course_code") or ""
        term = (self.course.get("term") or {}).get("name") or ""
        label = self.title if not code or code.lower() in self.title.lower() else f"{code} - {self.title}"
        return f"{label} ({term})" if term and term.lower() != "default term" else label

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        self.s.log(f"• {self.course_label()}")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.s.lib.checked(self.dir / "My Files").mkdir(exist_ok=True)
        self.s.lib.checked(self.dir / "_Study").mkdir(exist_ok=True)   # where the AI saves study guides it makes; sync never touches it
        steps = [self.load_tabs, self.crawl_modules, self.crawl_front_and_syllabus, self.crawl_pages,
                 self.crawl_assignments, self.crawl_quizzes, self.crawl_discussions, self.crawl_announcements,
                 self.crawl_linked_pages, self.crawl_calendar, self.crawl_files, self.crawl_media_listing,
                 self.plan_media, self.process_files, self.process_my_files, self.write_module_maps, self.write_docs]
        for step in steps:
            step()
            if self.cv.auth_failed:        # Canvas stopped accepting the token: stop instead of failing every call
                raise self.cv.auth_failed
        self.completed = True

    def load_tabs(self) -> None:
        try:
            tabs = self.cv.get_all(f"/api/v1/courses/{self.cid}/tabs")
        except CanvasError:
            self.snap["tabs"] = self.old.get("tabs", [])
            return
        self.visible_tabs = {t.get("id") for t in tabs if not t.get("hidden")}
        self.snap["tabs"] = [{"id": t.get("id"), "label": t.get("label"), "type": t.get("type"),
                              "hidden": bool(t.get("hidden")), "url": t.get("full_url") or t.get("html_url")} for t in tabs]
        for t in tabs:
            if str(t.get("id", "")).startswith("context_external_tool") and not t.get("hidden"):
                url = t.get("full_url") or t.get("html_url") or ""
                self.unreachable.append({"what": "Course menu tool", "title": t.get("label") or "External tool",
                                         "url": url, "platform": t.get("label") or platform_name(url),
                                         "where": "Course navigation"})

    # ------------------------------------------------------------------ files: placement
    def place_file(self, fid: str, priority: tuple, rel_dir: str, context: str, label: str = "") -> None:
        current = self.file_place.get(fid)
        if current is None or priority < current[0]:
            self.file_place[fid] = (priority, rel_dir, label)
        ctx = self.file_contexts.setdefault(fid, [])
        if context and context not in ctx:
            ctx.append(context)

    def absorb(self, refs: Refs, rel_dir: str, context: str, module_pos: Optional[int] = None) -> None:
        """Register what a page/assignment/announcement links to."""
        kind = context.split(":")[0].strip().lower()
        attach_dir = rel_dir if module_pos is not None else LINKED_DIRS.get(kind, "Files/Linked from other pages")
        for fid, label in refs.files.items():
            priority = (2, module_pos) if module_pos is not None else (3,)
            self.place_file(str(fid), priority, attach_dir, context, label)
        for m in refs.media:
            self.add_media(m["kind"], m["id"], m.get("title") or "", rel_dir if module_pos is not None else "Media", context)
        for vid, label in refs.youtube.items():
            self.add_youtube(vid, label, rel_dir if module_pos is not None else "Media", context)
        for link in refs.links:
            self.links.append({"url": link["url"], "text": link.get("text") or "", "where": context})
        for emb in refs.embeds:
            self.unreachable.append({"what": "Embedded content", "title": emb.get("title") or emb["url"],
                                     "url": emb["url"], "platform": emb.get("platform") or "", "where": context})
        for slug in refs.pages:
            self.linked_pages.setdefault(slug, context)

    def add_media(self, kind: str, ident: str, title: str, rel_dir: str, context: str, **extra: Any) -> None:
        key = f"media:{kind}:{ident}"
        entry = self.media.get(key)
        if entry is None:
            entry = {"key": key, "kind": kind, "id": ident, "title": title, "rel_dir": rel_dir, "contexts": []}
            self.media[key] = entry
        if title and not entry.get("title"):
            entry["title"] = title
        if rel_dir.startswith("Modules/") and not entry["rel_dir"].startswith("Modules/"):
            entry["rel_dir"] = rel_dir
        if context and context not in entry["contexts"]:
            entry["contexts"].append(context)
        entry.update({k: v for k, v in extra.items() if v})

    def add_youtube(self, vid: str, title: str, rel_dir: str, context: str) -> None:
        self.add_media("yt", vid, title, rel_dir, context)

    # ------------------------------------------------------------------ modules
    def crawl_modules(self) -> None:
        try:
            modules = self.cv.get_all(f"/api/v1/courses/{self.cid}/modules",
                                      {"include[]": ["items", "content_details"]})
        except CanvasError as exc:
            self.listing_failed("Modules", exc, tab="modules")
            self.snap["modules"] = self.old.get("modules", [])
            self._replay_modules(self.snap["modules"])
            return
        modules.sort(key=lambda m: (m.get("position") or 0, m.get("id") or 0))
        for m in modules:
            items = m.get("items")
            if items is None or (m.get("items_count") and len(items) < m["items_count"]):
                try:
                    m["items"] = self.cv.get_all(f"/api/v1/courses/{self.cid}/modules/{m['id']}/items",
                                                 {"include[]": ["content_details"]})
                except CanvasError as exc:
                    self.warn(f"Module '{m.get('name')}' items: {exc}")
                    m["items"] = items or []
        self.listing_ok.add("modules")
        slim_modules = []
        for mpos, m in enumerate(modules, 1):
            name = m.get("name") or f"Module {mpos}"
            mdir = f"Modules/{mpos:02d} - {safe_name(name, 80)}"
            ctx = f"Module {mpos}: {name}"
            slim_items = []
            for ipos, it in enumerate(sorted(m.get("items") or [], key=lambda i: (i.get("position") or 0)), 1):
                itype = it.get("type")
                title = it.get("title") or ""
                cd = it.get("content_details") or {}
                ref = {"type": itype, "title": title, "indent": it.get("indent") or 0, "url": it.get("html_url"),
                       "due_at": cd.get("due_at"), "points": cd.get("points_possible"),
                       "locked": bool(cd.get("locked_for_user")), "lock_explanation": cd.get("lock_explanation"),
                       "unlock_at": cd.get("unlock_at")}
                if itype == "File" and it.get("content_id"):
                    fid = str(it["content_id"])
                    self.place_file(fid, (0, mpos, ipos), mdir, ctx, title)
                    ref["key"] = f"file:{fid}"
                elif itype == "Page" and it.get("page_url"):
                    self.module_pages.setdefault(it["page_url"], (mdir, ctx, mpos))
                    ref["key"] = f"page:{it['page_url']}"
                elif itype == "ExternalUrl":
                    url = it.get("external_url") or ""
                    ref["url"] = url
                    yt = YOUTUBE_RE.search(url)
                    if yt:
                        self.add_youtube(yt.group(1), title, mdir, ctx)
                        ref["key"] = f"media:yt:{yt.group(1)}"
                    elif url:
                        self.links.append({"url": url, "text": title, "where": ctx})
                elif itype == "ExternalTool":
                    url = it.get("external_url") or it.get("html_url") or ""
                    self.unreachable.append({"what": "Module tool link", "title": title, "url": it.get("html_url") or url,
                                             "platform": platform_name(url), "where": ctx})
                elif itype in ("Assignment", "Quiz", "Discussion") and it.get("content_id"):
                    key = {"Assignment": "assignment", "Quiz": "quiz", "Discussion": "discussion"}[itype]
                    key = f"{key}:{it['content_id']}"
                    self.module_of.setdefault(key, (mpos, name, mdir))
                    ref["key"] = key
                slim_items.append(ref)
            slim_modules.append({"id": m.get("id"), "name": name, "position": mpos, "dir": mdir,
                                 "unlock_at": m.get("unlock_at"), "state": m.get("state"),
                                 "require_sequential_progress": m.get("require_sequential_progress"),
                                 "prerequisite_module_ids": m.get("prerequisite_module_ids") or [],
                                 "items": slim_items})
        self.snap["modules"] = slim_modules

    def _replay_modules(self, modules: List[Dict[str, Any]]) -> None:
        """Modules couldn't be listed this time: keep everything where the last good listing put it, so a
        hiccup doesn't shuffle files out of their module folders and back."""
        for m in modules:
            mpos, mdir, name = m.get("position"), m.get("dir"), m.get("name") or ""
            if not mdir:
                continue
            ctx = f"Module {mpos}: {name}"
            for ipos, ref in enumerate(m.get("items") or [], 1):
                key = ref.get("key") or ""
                kind, _, ident = key.partition(":")
                if kind == "file" and ident:
                    self.place_file(ident, (0, mpos, ipos), mdir, ctx, ref.get("title") or "")
                elif kind == "page" and ident:
                    self.module_pages.setdefault(ident, (mdir, ctx, mpos))
                elif kind in ("assignment", "quiz", "discussion") and ident:
                    self.module_of.setdefault(key, (mpos, name, mdir))
                elif key.startswith("media:yt:"):
                    self.add_youtube(key.split(":", 2)[2], ref.get("title") or "", mdir, ctx)
                elif ref.get("type") == "ExternalUrl" and ref.get("url"):
                    self.links.append({"url": ref["url"], "text": ref.get("title") or "", "where": ctx})

    # ------------------------------------------------------------------ pages & syllabus
    def crawl_front_and_syllabus(self) -> None:
        body = self.course.get("syllabus_body")
        if body and body.strip():
            md, refs = html_to_markdown(body, self.base, self.cid)
            key = "syllabus"
            rel = self.claim(key, "Syllabus.md")
            self.relocate(key, rel)
            it = self.seen(key)
            it.update({"path": rel, "title": "Syllabus", "kind": "syllabus"})
            self.pending_docs.append((key, rel, self.meta("Syllabus", "syllabus",
                                                             canvas_url=f"{self.base}/courses/{self.cid}/assignments/syllabus"), md))
            self.absorb(refs, "Files/Linked from syllabus", "Syllabus")
        if self.course.get("default_view") == "wiki":
            try:
                front = self.cv.get(f"/api/v1/courses/{self.cid}/front_page")
                if front and front.get("url"):
                    self.module_pages.setdefault(front["url"], ("Pages", "Course home page", None))
                    self.snap["front_page"] = front["url"]
            except CanvasError:
                pass

    def crawl_pages(self) -> None:
        listed: Dict[str, Dict[str, Any]] = {}
        if not self.tab_hidden("pages"):
            try:
                for p in self.cv.paginate(f"/api/v1/courses/{self.cid}/pages", {"sort": "title"}):
                    if p.get("url"):
                        listed[p["url"]] = p
                self.listing_ok.add("pages")
            except CanvasError as exc:
                self.listing_failed("Pages", exc, tab="pages")
        for url in self.module_pages:
            listed.setdefault(url, {"url": url})
        self.snap["pages"] = []
        for url, meta in listed.items():
            self._sync_page(url, meta)

    def crawl_linked_pages(self) -> None:
        """Pages that aren't in a module or the Pages list but are linked from other course content (common
        when the instructor hides the Pages menu). They're fetched too, and so are the pages they link to."""
        count = 0
        while count < MAX_LINKED_PAGES:
            todo = [slug for slug in self.linked_pages if slug not in self.page_paths and slug not in self.fetched_pages]
            if not todo:
                break
            for slug in todo[: MAX_LINKED_PAGES - count]:
                count += 1
                self._sync_page(slug, {"url": slug})
        if count:
            self.bump("linked_pages_checked", count)

    def _sync_page(self, url: str, meta: Dict[str, Any]) -> None:
        self.fetched_pages.add(url)
        key = f"page:{url}"
        prev = self.items.get(key) or {}
        placement = self.module_pages.get(url)
        rel_dir = placement[0] if placement else "Pages"
        mpos = placement[2] if placement else None
        module_label = placement[1] if placement and placement[2] is not None else ""
        context = f"Page: {meta.get('title') or prev.get('title') or url}"
        fresh = (prev.get("updated_at") and meta.get("updated_at") == prev.get("updated_at")
                 and not prev.get("locked") and not meta.get("locked_for_user")
                 and not prev.get("stale") and not prev.get("removed")
                 and prev.get("path") and (self.dir / prev["path"]).exists()
                 and prev.get("doc_version") == DOC_VERSION)
        cached = self.cache_load(key) if fresh else None
        if cached:                     # unchanged on Canvas: re-render from the saved copy (links may have moved)
            doc_meta, body = cached
            title = prev.get("title") or url
            rel = self.claim(key, f"{rel_dir}/{safe_name(title)}.md")
            self.relocate(key, rel)
            it = self.seen(key)
            it["path"] = rel
            self.page_paths[url] = rel
            doc_meta["module"] = module_label
            self.pending_docs.append((key, rel, doc_meta, body))
            self.absorb(Refs.from_json(prev.get("refs")), rel_dir, context, mpos)
            self.snap["pages"].append({"url": url, "title": title, "path": rel, "updated_at": prev.get("updated_at")})
            return
        try:
            page = self.cv.get(f"/api/v1/courses/{self.cid}/pages/{quote(url, safe='')}")
        except CanvasError as exc:
            gone = isinstance(exc, NotFoundError)
            if prev.get("path") and not gone:          # can't read it right now: keep what we have
                it = self.seen(key)
                restricted = isinstance(exc, ForbiddenError)
                it.update(stale=True, locked=restricted)
                self._mark_file_status(self.s.lib.checked(self.dir / prev["path"]),
                                       "restricted" if restricted else "stale")
                self.page_paths[url] = prev["path"]
                self.absorb(Refs.from_json(prev.get("refs")), rel_dir, context, mpos)
            self.bump("pages_unavailable")
            if not isinstance(exc, (NotFoundError, ForbiddenError)):
                self.warn(f"Page '{url}': {exc}")
            return
        canonical = str(page.get("url") or url)
        if canonical != url:          # reached through an older or differently written link
            self.fetched_pages.add(canonical)
            if canonical in self.page_paths:
                self.page_paths[url] = self.page_paths[canonical]
                return
            alias, url, key = url, canonical, f"page:{canonical}"
            prev = self.items.get(key) or {}
        else:
            alias = ""
        title = page.get("title") or url
        locked = bool(page.get("locked_for_user"))
        html = page.get("body") or ""
        if not html.strip() and page.get("block_editor_attributes"):
            html = block_editor_html(page["block_editor_attributes"])
        md, refs = html_to_markdown(html, self.base, self.cid)
        if locked:
            lock_note = f"_Locked: {page.get('lock_explanation') or 'not available yet'}_"
            md = lock_note + ("\n\n" + md if md else "")
        rel = self.claim(key, f"{rel_dir}/{safe_name(title)}.md")
        self.relocate(key, rel)
        it = self.seen(key)
        it.update({"path": rel, "title": title, "kind": "page", "updated_at": page.get("updated_at"),
                   "refs": refs.to_json(), "doc_version": DOC_VERSION, "locked": locked})
        it.pop("stale", None)
        self.page_paths[url] = rel
        if alias:
            self.page_paths[alias] = rel
        self.pending_docs.append((key, rel, self.meta(title, "page", module=module_label,
                                                        sync_status="restricted" if locked else "current",
                                                        updated=fmt_dt(page.get("updated_at")),
                                                        canvas_url=page.get("html_url") or self.canvas_url("page", url)), md))
        self.absorb(refs, rel_dir, f"Page: {title}", mpos)
        self.snap["pages"].append({"url": url, "title": title, "path": rel, "updated_at": page.get("updated_at")})

    # ------------------------------------------------------------------ assignments, grades, feedback
    def crawl_assignments(self) -> None:
        keep_snapshot = False
        try:
            assignments = self.cv.get_all(f"/api/v1/courses/{self.cid}/assignments",
                                          {"include[]": ["submission", "checkpoints"], "order_by": "due_at"})
            self.listing_ok.add("assignments")
        except CanvasError as exc:
            self.listing_failed("Assignments", exc, tab="assignments")
            for key in ("assignments", "groups", "submissions"):
                self.snap[key] = self.old.get(key, [] if key != "submissions" else {})
            assignments = self._module_items("assignment", "assignments", {"include[]": ["submission"]})
            keep_snapshot = True
            if not assignments:
                return
        subs: Dict[str, Dict[str, Any]] = {}
        try:
            for sub in self.cv.paginate(f"/api/v1/courses/{self.cid}/students/submissions",
                                        {"include[]": ["submission_comments", "rubric_assessment"]}):
                subs[str(sub.get("assignment_id"))] = sub
        except CanvasError:
            pass
        try:
            groups = self.cv.get_all(f"/api/v1/courses/{self.cid}/assignment_groups")
        except CanvasError:
            groups = self.old.get("groups", [])
        group_by_id = {g.get("id"): g for g in groups}
        weighted = bool(self.course.get("apply_assignment_group_weights"))
        slim_list: List[Dict[str, Any]] = []
        slim_subs: Dict[str, Any] = {}
        for a in assignments:
            self._sync_assignment(a, subs, group_by_id, weighted, slim_list, slim_subs)
        if keep_snapshot:
            return
        self.snap["assignments"] = slim_list
        self.snap["submissions"] = slim_subs
        self.snap["groups"] = [{"id": g.get("id"), "name": g.get("name"), "weight": g.get("group_weight"),
                                "rules": g.get("rules")} for g in groups]
        self.snap["weighted"] = weighted

    def _module_items(self, kind: str, endpoint: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """A list failed (often because the instructor hid that menu): fetch the items the modules link to."""
        out: List[Dict[str, Any]] = []
        for key in list(self.module_of):
            k, _, ident = key.partition(":")
            if k != kind:
                continue
            try:
                item = self.cv.get(f"/api/v1/courses/{self.cid}/{endpoint}/{ident}", params)
            except CanvasError:
                prev = self.items.get(key) or {}
                if prev.get("path") and not prev.get("removed"):
                    self.seen(key)               # can't read it right now: keep the copy we have
                continue
            if isinstance(item, dict) and item.get("id") is not None:
                out.append(item)
        self.module_fallback.add(kind)
        return out

    def _sync_assignment(self, a: Dict[str, Any], subs: Dict[str, Dict[str, Any]], group_by_id: Dict[Any, Any],
                         weighted: bool, slim_list: List[Dict[str, Any]], slim_subs: Dict[str, Any]) -> None:
        aid = str(a.get("id"))
        key = f"assignment:{aid}"
        sub = subs.get(aid) or a.get("submission") or {}
        group = group_by_id.get(a.get("assignment_group_id")) or {}
        module = self.module_of.get(key)
        title = a.get("name") or f"Assignment {aid}"
        md_desc, refs = html_to_markdown(a.get("description"), self.base, self.cid)
        body = self.render_assignment(a, sub, group, weighted, md_desc, module)
        rel = self.claim(key, f"Assignments/{safe_name(title)}.md")
        self.relocate(key, rel)
        it = self.seen(key)
        it.update({"path": rel, "title": title, "kind": "assignment"})
        self.pending_docs.append((key, rel, self.meta(title, "assignment", due=fmt_dt(a.get("due_at")),
                                                        date=a.get("due_at"),
                                                        points=a.get("points_possible"),
                                                        module=module[1] if module else "",
                                                        canvas_url=a.get("html_url")), body))
        self.absorb(refs, module[2] if module else "Assignments", f"Assignment: {title}",
                    module[0] if module else None)
        if self.cfg.get("download_my_submissions", True):
            for att in (sub.get("attachments") or []):
                if att.get("id"):
                    fid = str(att["id"])
                    att = dict(att, _submission_id=sub.get("id"))
                    self.file_meta[fid] = att
                    self.place_file(fid, (1,), f"Assignments/My Submissions/{safe_name(title, 70)}",
                                    f"My submission: {title}", att.get("display_name") or "")
        tool_url = (a.get("external_tool_tag_attributes") or {}).get("url") or ""
        if a.get("is_quiz_lti_assignment") or "quiz-lti" in tool_url or "quiz_lti" in tool_url:
            self.unreachable.append({"what": "New Quiz", "title": title, "url": a.get("html_url") or "",
                                     "platform": "Canvas New Quizzes", "where": "Assignments"})
        elif "external_tool" in (a.get("submission_types") or []):
            self.unreachable.append({"what": "Assignment in an outside tool", "title": title,
                                     "url": a.get("html_url") or "", "platform": platform_name(tool_url),
                                     "where": "Assignments"})
        checkpoints = [{"tag": c.get("tag"), "name": c.get("name"), "due_at": c.get("due_at"),
                        "points": c.get("points_possible")} for c in a.get("checkpoints") or [] if isinstance(c, dict)]
        slim_list.append({
            "id": aid, "name": title, "due_at": a.get("due_at"), "unlock_at": a.get("unlock_at"),
            "lock_at": a.get("lock_at"), "points": a.get("points_possible"), "group_id": a.get("assignment_group_id"),
            "submission_types": a.get("submission_types"), "path": rel, "html_url": a.get("html_url"),
            "is_quiz": bool(a.get("quiz_id")) or bool(a.get("is_quiz_assignment")), "quiz_id": a.get("quiz_id"),
            "grading_type": a.get("grading_type"), "omit_from_final_grade": a.get("omit_from_final_grade"),
            "checkpoints": checkpoints,
        })
        if sub:
            slim_subs[aid] = {
                "score": sub.get("score"), "grade": sub.get("grade"), "submitted_at": sub.get("submitted_at"),
                "graded_at": sub.get("graded_at"), "late": sub.get("late"), "missing": sub.get("missing"),
                "excused": sub.get("excused"), "workflow_state": sub.get("workflow_state"),
                "comments": [{"author": c.get("author_name"), "comment": _comment_summary(c), "at": c.get("created_at")}
                             for c in sub.get("submission_comments") or [] if _comment_summary(c)],
                "rubric": self._rubric_feedback(a, sub),
            }

    @staticmethod
    def _rubric_feedback(a: Dict[str, Any], sub: Dict[str, Any]) -> List[Dict[str, Any]]:
        assessment = sub.get("rubric_assessment") or {}
        out = []
        for crit in a.get("rubric") or []:
            res = assessment.get(crit.get("id")) or {}
            if not res:
                continue
            rating = next((r for r in crit.get("ratings") or [] if r.get("id") == res.get("rating_id")), {})
            label = rating.get("description") or ""
            if rating.get("long_description"):
                label += f" ({_one_line(rating['long_description'])})"
            out.append({"criterion": _one_line(crit.get("description")), "points": res.get("points"),
                        "max": crit.get("points"), "rating": label, "comments": _one_line(res.get("comments"))})
        return out

    def render_assignment(self, a: Dict[str, Any], sub: Dict[str, Any], group: Dict[str, Any], weighted: bool,
                          md_desc: str, module: Optional[Tuple[int, str, str]]) -> str:
        title = a.get("name") or f"Assignment {a.get('id')}"
        lines = []
        facts = []
        if a.get("due_at"):
            facts.append(f"- **Due:** {fmt_dt(a['due_at'])}")
        for c in a.get("checkpoints") or []:
            if isinstance(c, dict) and (c.get("due_at") or c.get("points_possible") is not None):
                what = c.get("name") or str(c.get("tag") or "checkpoint").replace("_", " ")
                pts = f" ({c['points_possible']:g} pts)" if isinstance(c.get("points_possible"), (int, float)) else ""
                facts.append(f"- **{what[:1].upper() + what[1:]}:** due {fmt_dt(c.get('due_at')) or 'no date'}{pts}")
        if a.get("unlock_at") or a.get("lock_at"):
            facts.append(f"- **Available:** {fmt_dt(a.get('unlock_at')) or 'now'} → {fmt_dt(a.get('lock_at')) or 'no end date'}")
        pts = a.get("points_possible")
        if pts is not None:
            facts.append(f"- **Points:** {pts:g}" if isinstance(pts, (int, float)) else f"- **Points:** {pts}")
        if group:
            weight = f" ({group.get('group_weight'):g}% of the course grade)" if weighted and group.get("group_weight") else ""
            facts.append(f"- **Grade category:** {group.get('name')}{weight}")
        stypes = [t.replace("_", " ") for t in a.get("submission_types") or [] if t != "none"]
        if stypes:
            facts.append(f"- **Submit as:** {', '.join(stypes)}"
                         + (f" ({', '.join(a['allowed_extensions'])})" if a.get("allowed_extensions") else ""))
        if a.get("allowed_attempts") and a["allowed_attempts"] > 0:
            facts.append(f"- **Attempts allowed:** {a['allowed_attempts']}")
        if module:
            facts.append(f"- **Module:** {module[1]}")
        if a.get("html_url"):
            facts.append(f"- **Canvas:** {a['html_url']}")
        lines += facts
        if a.get("locked_for_user") and a.get("lock_explanation"):
            lines += ["", f"_Locked: {a['lock_explanation']}_"]
        lines += ["", "## Instructions", "", md_desc or "_No description on Canvas._"]
        rubric = a.get("rubric") or []
        if rubric:
            lines += ["", "## Rubric", "", "| Criterion | Ratings | Points |", "|---|---|---|"]
            for crit in rubric:
                ratings = "; ".join(_rating_text(r) for r in crit.get("ratings") or [] if isinstance(r, dict))
                desc = crit.get("description") or ""
                if crit.get("long_description"):
                    desc += f" — {crit['long_description']}"
                pts_txt = f"{crit.get('points'):g}" if isinstance(crit.get("points"), (int, float)) else ""
                lines.append(f"| {_cell(desc)} | {_cell(ratings)} | {pts_txt} |")
        if sub:
            lines += ["", "## My submission", ""]
            state = sub.get("workflow_state") or "unsubmitted"
            lines.append(f"- **Status:** {state}" + (" (late)" if sub.get("late") else "") +
                         (" (missing)" if sub.get("missing") else "") + (" (excused)" if sub.get("excused") else ""))
            if sub.get("submitted_at"):
                lines.append(f"- **Submitted:** {fmt_dt(sub['submitted_at'])}")
            if sub.get("score") is not None:
                out_of = f" / {pts:g}" if isinstance(pts, (int, float)) else ""
                grade = f" ({sub['grade']})" if sub.get("grade") and str(sub.get("grade")) != str(sub.get("score")) else ""
                lines.append(f"- **Score:** {sub['score']:g}{out_of}{grade}" if isinstance(sub["score"], (int, float))
                             else f"- **Score:** {sub['score']}")
            for att in sub.get("attachments") or []:
                if att.get("id"):
                    lines.append(f"- **File I submitted:** [{att.get('display_name') or 'file'}](canvas-file:{att['id']})")
            if sub.get("body"):
                text_md, _ = html_to_markdown(sub["body"], self.base, self.cid)
                lines += ["", "What I submitted (text entry):", "", text_md]
            feedback = self._rubric_feedback(a, sub)
            feedback_dir = f"Assignments/Feedback/{safe_name(title, 70)}"
            comments = [(c, self._comment_text(c, title, feedback_dir, sub)) for c in sub.get("submission_comments") or []]
            comments = [(c, text) for c, text in comments if text]
            if feedback or comments:
                lines += ["", "## Feedback", ""]
                for fb in feedback:
                    score = f"{fb['points']:g}" if isinstance(fb.get("points"), (int, float)) else "?"
                    mx = f"{fb['max']:g}" if isinstance(fb.get("max"), (int, float)) else "?"
                    lines.append(f"- **{fb['criterion']}:** {score}/{mx}" + (f" — {fb['rating']}" if fb.get("rating") else "") +
                                 (f" — \"{fb['comments']}\"" if fb.get("comments") else ""))
                for c, text in comments:
                    who = c.get("author_name") or "Comment"
                    tag = " (Instructor)" if who in self.teacher_names else ""
                    head = f"- **{who}{tag}** ({fmt_date(c.get('created_at'))}):"
                    body_lines = text.splitlines()
                    lines.append(f"{head} {body_lines[0]}" if body_lines else head)
                    lines += [f"  {ln}" if ln.strip() else "" for ln in body_lines[1:]]
        return "\n".join(lines).strip()

    def _comment_text(self, c: Dict[str, Any], title: str, feedback_dir: str, sub: Dict[str, Any]) -> str:
        """A feedback comment with its attached files and recorded audio/video, which get synced too."""
        parts = [(c.get("comment") or "").strip()]
        for att in c.get("attachments") or []:
            if not att.get("id"):
                continue
            fid = str(att["id"])
            name = att.get("display_name") or att.get("filename") or "attachment"
            self.file_meta.setdefault(fid, dict(att, _submission_id=sub.get("id")))
            self.place_file(fid, (1,), feedback_dir, f"Feedback on: {title}", name)
            parts.append(f"[Attached file: {name}](canvas-file:{fid})")
        mc = c.get("media_comment") or {}
        if mc.get("media_id"):
            mid = str(mc["media_id"])
            mtype = mc.get("media_type") or ("audio" if "audio" in str(mc.get("content-type") or "") else "video")
            sources = [{"url": mc["url"], "content_type": mc.get("content-type") or ""}] if mc.get("url") else None
            self.add_media("obj", mid, f"{str(mtype).title()} feedback on {title}", feedback_dir, f"Feedback on: {title}",
                           media_id=mid, media_type=mtype, sources=sources)
            parts.append(f"[Recorded {mtype} comment (transcript)](canvas-media:obj-{mid})")
        return "\n".join(p for p in parts if p)

    # ------------------------------------------------------------------ quizzes (classic)
    def crawl_quizzes(self) -> None:
        try:
            quizzes = self.cv.get_all(f"/api/v1/courses/{self.cid}/quizzes")
            self.listing_ok.add("quizzes")
            fallback = False
        except CanvasError as exc:
            self.listing_failed("Quizzes", exc, tab="quizzes")
            self.snap["quizzes"] = self.old.get("quizzes", [])
            quizzes = self._module_items("quiz", "quizzes")
            fallback = True
            if not quizzes:
                return
        subs = self.snap.get("submissions") or {}
        slim: List[Dict[str, Any]] = []
        for q in quizzes:
            self._sync_quiz(q, subs, slim)
        if not fallback:
            self.snap["quizzes"] = slim

    def _sync_quiz(self, q: Dict[str, Any], subs: Dict[str, Any], slim: List[Dict[str, Any]]) -> None:
        qid = str(q.get("id"))
        key = f"quiz:{qid}"
        title = q.get("title") or f"Quiz {qid}"
        module = self.module_of.get(key)
        md_desc, refs = html_to_markdown(q.get("description"), self.base, self.cid)
        facts = []
        if q.get("due_at"):
            facts.append(f"- **Due:** {fmt_dt(q['due_at'])}")
        if q.get("unlock_at") or q.get("lock_at"):
            facts.append(f"- **Available:** {fmt_dt(q.get('unlock_at')) or 'now'} → {fmt_dt(q.get('lock_at')) or 'no end date'}")
        facts.append(f"- **Type:** {(q.get('quiz_type') or 'quiz').replace('_', ' ')}")
        if q.get("question_count"):
            facts.append(f"- **Questions:** {q['question_count']}")
        if q.get("points_possible") is not None:
            facts.append(f"- **Points:** {q['points_possible']}")
        if q.get("time_limit"):
            facts.append(f"- **Time limit:** {q['time_limit']} minutes")
        attempts = q.get("allowed_attempts")
        facts.append(f"- **Attempts:** {'unlimited' if attempts == -1 else attempts or 1}")
        if q.get("one_question_at_a_time"):
            facts.append("- One question at a time" + (" (no going back)" if q.get("cant_go_back") else ""))
        if module:
            facts.append(f"- **Module:** {module[1]}")
        if q.get("html_url"):
            facts.append(f"- **Canvas:** {q['html_url']}")
        sub = subs.get(str(q.get("assignment_id"))) if q.get("assignment_id") else None
        body = "\n".join(facts) + "\n\n## Instructions\n\n" + (md_desc or "_No description._")
        if q.get("locked_for_user") and q.get("lock_explanation"):
            body += f"\n\n_Locked: {q['lock_explanation']}_"
        if sub and sub.get("score") is not None:
            body += f"\n\n## My result\n\n- **Score:** {sub['score']} / {q.get('points_possible')}"
        review = self._quiz_review(q)
        if review:
            body += "\n\n" + review
        rel = self.claim(key, f"Quizzes/{safe_name(title)}.md")
        self.relocate(key, rel)
        it = self.seen(key)
        it.update({"path": rel, "title": title, "kind": "quiz"})
        self.pending_docs.append((key, rel, self.meta(title, "quiz", due=fmt_dt(q.get("due_at")),
                                                        module=module[1] if module else "",
                                                        canvas_url=q.get("html_url")), body))
        self.absorb(refs, module[2] if module else "Quizzes", f"Quiz: {title}", module[0] if module else None)
        slim.append({"id": qid, "title": title, "due_at": q.get("due_at"), "unlock_at": q.get("unlock_at"),
                     "points": q.get("points_possible"), "questions": q.get("question_count"),
                     "time_limit": q.get("time_limit"), "path": rel, "assignment_id": q.get("assignment_id")})

    def _quiz_review(self, q: Dict[str, Any]) -> str:
        """The questions of a quiz you've taken, as Canvas shows them when you review it (with the correct
        answers and your answers when your instructor allows that). Empty if there's nothing to review."""
        qid = str(q.get("id"))
        if q.get("quiz_type") == "survey" or q.get("hide_results") == "always":
            return ""
        try:
            taken = self.cv.get_all(f"/api/v1/courses/{self.cid}/quizzes/{qid}/submissions", key="quiz_submissions")
        except CanvasError:
            return ""
        done = [t for t in taken if t.get("workflow_state") in ("complete", "pending_review") and t.get("attempt")]
        if not done:
            return ""
        last = max(done, key=lambda t: (int(t.get("attempt") or 0), str(t.get("finished_at") or "")))
        stamp = f"{last.get('id')}|{last.get('attempt')}|{last.get('finished_at')}|{q.get('updated_at')}|{q.get('show_correct_answers')}"
        cache_key = f"quizreview:{qid}"
        cached = self.cache_load(cache_key)
        if cached and cached[0].get("stamp") == stamp:
            return cached[1]
        try:
            questions = self.cv.get_all(f"/api/v1/courses/{self.cid}/quizzes/{qid}/questions",
                                        {"quiz_submission_id": last.get("id"), "quiz_submission_attempt": last.get("attempt")})
        except (ForbiddenError, NotFoundError):
            return ("## Question review\n\n_Your instructor hasn't made this quiz's questions available for review "
                    "(Canvas only shares them when results are released)._")
        except CanvasError:
            return ""
        answers: Dict[str, Any] = {}
        try:
            for row in self.cv.paginate(f"/api/v1/quiz_submissions/{last.get('id')}/questions", key="quiz_submission_questions"):
                if isinstance(row, dict) and row.get("id") is not None:
                    answers[str(row["id"])] = row.get("answer")
        except CanvasError:
            pass
        text = _render_quiz_questions(questions, answers, last, self.base, self.cid)
        self.cache_save(cache_key, {"stamp": stamp}, text)
        return text

    # ------------------------------------------------------------------ discussions
    def crawl_discussions(self) -> None:
        try:
            topics = self.cv.get_all(f"/api/v1/courses/{self.cid}/discussion_topics")
            self.listing_ok.add("discussions")
            fallback = False
        except CanvasError as exc:
            self.listing_failed("Discussions", exc, tab="discussions")
            self.snap["discussions"] = self.old.get("discussions", [])
            topics = self._module_items("discussion", "discussion_topics")
            fallback = True
            if not topics:
                return
        slim: List[Dict[str, Any]] = []
        for t in topics:
            self._sync_discussion(t, slim)
        if not fallback:
            self.snap["discussions"] = slim

    def _sync_discussion(self, t: Dict[str, Any], slim: List[Dict[str, Any]]) -> None:
        tid = str(t.get("id"))
        key = f"discussion:{tid}"
        title = t.get("title") or f"Discussion {tid}"
        prev = self.items.get(key) or {}
        module = self.module_of.get(key)
        rel_dir = "Discussions"
        locked = bool(t.get("locked_for_user"))
        # Topics have no "updated" time, so an edited prompt is noticed by its text.
        digest = hashlib.sha1((t.get("message") or "").encode("utf-8")).hexdigest()[:12]
        stamp = (f"{t.get('last_reply_at')}|{t.get('posted_at')}|{t.get('discussion_subentry_count')}|{digest}"
                 f"|{t.get('lock_at')}|{(t.get('assignment') or {}).get('due_at')}")
        rel = self.claim(key, f"{rel_dir}/{safe_name(title)}.md")
        context = f"Discussion: {title}"
        cached = None
        if (not locked and prev.get("stamp") == stamp and prev.get("path") and (self.dir / prev["path"]).exists()
                and prev.get("doc_version") == DOC_VERSION and not prev.get("removed")):
            cached = self.cache_load(key)
        if cached:
            doc_meta, body = cached
            self.relocate(key, rel)
            it = self.seen(key)
            it["path"] = rel
            doc_meta["module"] = module[1] if module else ""
            self.pending_docs.append((key, rel, doc_meta, body))
            self.absorb(Refs.from_json(prev.get("refs")), module[2] if module else rel_dir, context,
                        module[0] if module else None)
            slim.append({"id": tid, "title": title, "path": rel, "posted_at": t.get("posted_at"),
                         "replies": t.get("discussion_subentry_count")})
            return
        md_prompt, refs = html_to_markdown(t.get("message"), self.base, self.cid)
        for att in t.get("attachments") or []:
            if att.get("id"):
                refs.files.setdefault(str(att["id"]), att.get("display_name") or "attachment")
        lines = [f"- **Started by:** {t.get('user_name') or (t.get('author') or {}).get('display_name') or 'instructor'}",
                 f"- **Posted:** {fmt_dt(t.get('posted_at'))}"]
        if t.get("assignment_id"):
            lines.append("- **Graded discussion**" + (f" · due {fmt_dt((t.get('assignment') or {}).get('due_at'))}"
                                                      if (t.get("assignment") or {}).get("due_at") else ""))
        if module:
            lines.append(f"- **Module:** {module[1]}")
        if t.get("html_url"):
            lines.append(f"- **Canvas:** {t['html_url']}")
        complete = True
        if locked:
            explanation = t.get("lock_explanation") or "not available yet"
            lines += ["", f"_Locked: {html_text(explanation) or explanation}_"]
            if md_prompt and html_text(t.get("message")) != html_text(t.get("lock_explanation")):
                lines += ["", "## Prompt", "", md_prompt]
        else:
            lines += ["", "## Prompt", "", md_prompt or "_No prompt text._"]
        if t.get("discussion_subentry_count") and not locked:
            lines += ["", f"## Replies ({t.get('discussion_subentry_count')})", ""]
            thread, complete = self.render_thread(tid, refs)
            lines.append(thread)
        it = self.seen(key)
        self.relocate(key, rel)
        it.update({"path": rel, "title": title, "kind": "discussion", "refs": refs.to_json(), "doc_version": DOC_VERSION,
                   # a locked topic or a thread that didn't load is fetched again next time
                   "stamp": stamp if complete and not locked else None})
        self.pending_docs.append((key, rel, self.meta(title, "discussion", posted=fmt_dt(t.get("posted_at")),
                                                        date=t.get("posted_at"),
                                                        module=module[1] if module else "",
                                                        canvas_url=t.get("html_url")), "\n".join(lines)))
        self.absorb(refs, module[2] if module else rel_dir, context, module[0] if module else None)
        slim.append({"id": tid, "title": title, "path": rel, "posted_at": t.get("posted_at"),
                     "replies": t.get("discussion_subentry_count")})

    def render_thread(self, tid: str, refs: Refs) -> Tuple[str, bool]:
        """The replies as a nested list (formatting kept). Returns (text, loaded completely)."""
        try:
            view = self.cv.get(f"/api/v1/courses/{self.cid}/discussion_topics/{tid}/view",
                               {"include_new_entries": 1})
        except ForbiddenError as exc:
            if "require_initial_post" in (exc.message or "") or "initial" in (exc.message or ""):
                return "_Canvas only shows replies after you post in this discussion._", False
            return "_Replies aren't visible to you._", False
        except CanvasError as exc:
            return f"_Couldn't load replies ({exc}); they'll be fetched again next sync._", False
        view = view or {}
        people = {str(p.get("id")): p.get("display_name") or "Someone" for p in view.get("participants") or []}
        out: List[str] = []

        def walk(entries: List[Dict[str, Any]], depth: int) -> None:
            for e in entries or []:
                indent = "  " * depth
                if e.get("deleted"):
                    out.append(f"{indent}- _(deleted reply)_")
                else:
                    uid = str(e.get("user_id"))
                    who = people.get(uid, "Someone")
                    tag = " (Instructor)" if uid in self.teacher_ids else ""
                    msg, sub_refs = html_to_markdown(e.get("message"), self.base, self.cid)
                    for att in ([e["attachment"]] if isinstance(e.get("attachment"), dict) else []) + list(e.get("attachments") or []):
                        if isinstance(att, dict) and att.get("id"):
                            sub_refs.files.setdefault(str(att["id"]), att.get("display_name") or "attachment")
                            msg += f"\n\n[Attached file: {att.get('display_name') or 'attachment'}](canvas-file:{att['id']})"
                    refs.merge(sub_refs)
                    when = fmt_date(e.get("created_at"))
                    head = f"{indent}- **{who}{tag}**{' · ' + when if when else ''}:"
                    body = [ln.rstrip() for ln in (msg or "").strip().splitlines()]
                    if len(body) <= 1:
                        out.append(head + (f" {body[0].strip()}" if body and body[0].strip() else ""))
                    else:
                        out.append(head)
                        out.extend(f"{indent}  {ln}" if ln.strip() else "" for ln in body)
                walk(e.get("replies") or [], depth + 1)

        walk(view.get("view") or [], 0)
        seen_ids = set()

        def collect(entries):
            for e in entries or []:
                seen_ids.add(e.get("id"))
                collect(e.get("replies"))

        collect(view.get("view"))
        extra = [e for e in view.get("new_entries") or [] if e.get("id") not in seen_ids]
        walk(extra, 0)
        return "\n".join(out) or "_No replies yet._", True

    # ------------------------------------------------------------------ announcements
    def crawl_announcements(self) -> None:
        anns: Optional[List[Dict[str, Any]]] = None
        try:
            anns = self.cv.get_all(f"/api/v1/courses/{self.cid}/discussion_topics", {"only_announcements": "true"})
        except CanvasError:
            try:
                end = (datetime.now(timezone.utc) + timedelta(days=366)).strftime("%Y-%m-%d")
                anns = self.cv.get_all("/api/v1/announcements", {"context_codes[]": f"course_{self.cid}",
                                                                   "start_date": "2000-01-01", "end_date": end})
            except CanvasError as exc:
                self.listing_failed("Announcements", exc, tab="announcements")
                self.snap["announcements"] = self.old.get("announcements", [])
                return
        self.listing_ok.add("announcements")
        slim = []
        for a in anns or []:
            aid = str(a.get("id"))
            key = f"announcement:{aid}"
            when = a.get("posted_at") or a.get("delayed_post_at") or a.get("created_at")
            title = a.get("title") or "Announcement"
            md, refs = html_to_markdown(a.get("message"), self.base, self.cid)
            for att in a.get("attachments") or []:
                if att.get("id"):
                    refs.files.setdefault(str(att["id"]), att.get("display_name") or "attachment")
            author = a.get("user_name") or (a.get("author") or {}).get("display_name") or ""
            body = f"- **Posted:** {fmt_dt(when)}" + (f"\n- **By:** {author}" if author else "") + \
                   (f"\n- **Canvas:** {a['html_url']}" if a.get("html_url") else "") + "\n\n" + (md or "_No text._")
            rel = self.claim(key, f"Announcements/{day_key(when) or 'undated'} - {safe_name(title, 90)}.md")
            self.relocate(key, rel)
            it = self.seen(key)
            it.update({"path": rel, "title": title, "kind": "announcement", "date": when})
            self.pending_docs.append((key, rel, self.meta(title, "announcement", posted=fmt_dt(when), date=when,
                                                            canvas_url=a.get("html_url")), body))
            self.absorb(refs, "Announcements", f"Announcement: {title}")
            slim.append({"id": aid, "title": title, "posted_at": when, "path": rel,
                         "preview": truncate(html_text(a.get("message")), 400)})
        slim.sort(key=lambda x: x.get("posted_at") or "", reverse=True)
        self.snap["announcements"] = slim

    # ------------------------------------------------------------------ calendar
    def crawl_calendar(self) -> None:
        try:
            events = self.cv.get_all("/api/v1/calendar_events", {"context_codes[]": f"course_{self.cid}",
                                                                   "all_events": "true", "type": "event"})
        except CanvasError as exc:
            self.listing_failed("Calendar", exc)
            self.snap["calendar"] = self.old.get("calendar", [])
            return
        self.snap["calendar"] = [{"title": e.get("title"), "start_at": e.get("start_at"), "end_at": e.get("end_at"),
                                  "all_day": e.get("all_day"), "location": e.get("location_name"),
                                  "description": truncate(html_text(e.get("description")), 500),
                                  "url": e.get("html_url")} for e in events if e.get("workflow_state") != "deleted"]

    # ------------------------------------------------------------------ files: listing + metadata
    def crawl_files(self) -> None:
        if not self.tab_hidden("files"):
            try:
                folders = {f.get("id"): f.get("full_name") or "" for f in self.cv.paginate(f"/api/v1/courses/{self.cid}/folders")}
                for f in self.cv.paginate(f"/api/v1/courses/{self.cid}/files"):
                    fid = str(f.get("id"))
                    self.file_meta[fid] = f
                    folder = folders.get(f.get("folder_id"), "")
                    parts = [safe_name(p, 60) for p in folder.split("/")[1:] if p]
                    self.place_file(fid, (1,), "/".join(["Files"] + parts), "Course files", f.get("display_name") or "")
                self.listing_ok.add("files")
            except CanvasError as exc:
                self.listing_failed("Files", exc, tab="files")
        for fid in list(self.file_place):
            if fid in self.file_meta:
                continue
            for path in (f"/api/v1/courses/{self.cid}/files/{fid}", f"/api/v1/files/{fid}"):
                try:
                    self.file_meta[fid] = self.cv.get(path)
                    break
                except CanvasError as exc:
                    self.file_errors[fid] = str(exc)
            if fid not in self.file_meta:
                label = self.file_place[fid][2] or f"file {fid}"
                self.unreachable.append({"what": "File you can't open (locked, hidden or deleted)", "title": label,
                                         "url": self.canvas_url("file", fid), "platform": "Canvas",
                                         "where": ", ".join(self.file_contexts.get(fid, [])[:2])})

    def crawl_media_listing(self) -> None:
        for endpoint in (f"/api/v1/courses/{self.cid}/media_attachments", f"/api/v1/courses/{self.cid}/media_objects"):
            try:
                listed = self.cv.get_all(endpoint)
            except CanvasError:
                continue
            self.listing_ok.add("media")
            for mo in listed:
                media_id = mo.get("media_id") or mo.get("id")
                if not media_id:
                    continue
                att_id = mo.get("attachment_id")
                kind, ident = ("att", str(att_id)) if att_id else ("obj", str(media_id))
                self.add_media(kind, ident, mo.get("user_entered_title") or mo.get("title") or "", "Media",
                               "Course media", sources=mo.get("media_sources"), tracks=mo.get("media_tracks"),
                               media_id=str(media_id), media_type=mo.get("media_type"))

    # ------------------------------------------------------------------ files: download + extract
    def plan_media(self) -> None:
        """Turn video/audio files and embedded videos into media jobs; placement is final by now."""
        for fid, (priority, rel_dir, label) in list(self.file_place.items()):
            meta = self.file_meta.get(fid)
            if not meta:
                continue
            name = meta.get("display_name") or meta.get("filename") or label or f"file-{fid}"
            if kind_of(Path(name), meta.get("content-type") or meta.get("content_type")) == "media":
                key = f"file:{fid}"
                rel = self.claim(key, f"{rel_dir}/{safe_name(name)}")
                self.relocate(key, rel)
                it = self.seen(key)
                it.update({"path": rel, "text": rel + ".md", "title": name, "kind": "media"})
                self.s.media_jobs.append({
                    "course": self, "key": key, "type": "file", "title": name, "rel_md": rel + ".md",
                    "file_id": fid, "rel_media": rel, "stamp": f"{meta.get('updated_at') or meta.get('modified_at')}|{meta.get('size')}",
                    "size": meta.get("size"), "contexts": self.file_contexts.get(fid, []),
                    "is_video": str(meta.get("content-type") or "").startswith("video/") or Path(name).suffix.lower() in VIDEO_EXT,
                    "tracks_url": f"/api/v1/media_attachments/{fid}/media_tracks" if meta.get("media_entry_id") else None,
                })
                del self.file_place[fid]
        video_files = {j["file_id"]: j for j in self.s.media_jobs if j.get("course") is self and j["type"] == "file"}
        entry_ids = {}
        for fid, job in video_files.items():
            eid = (self.file_meta.get(fid) or {}).get("media_entry_id")
            if eid:
                entry_ids[str(eid)] = job
        for key, entry in list(self.media.items()):
            twin = video_files.get(entry["id"]) if entry["kind"] == "att" else None
            media_id = entry.get("media_id") or (entry["id"] if entry["kind"] == "obj" else None)
            if not twin and media_id:
                twin = entry_ids.get(str(media_id))
            if twin:
                twin["contexts"] = list(dict.fromkeys(list(twin.get("contexts") or []) + entry["contexts"]))
                if entry.get("tracks") and not twin.get("tracks"):
                    twin["tracks"] = entry["tracks"]
                self.alias[key] = twin["key"]
                del self.media[key]
        by_media_id = {e.get("media_id"): k for k, e in self.media.items() if e.get("media_id")}
        for key, entry in list(self.media.items()):
            if entry["kind"] == "yt":
                vid = entry["id"]
                title = entry.get("title") or f"YouTube {vid}"
                rel = self.claim(key, f"{entry['rel_dir']}/YouTube - {safe_name(title, 80)}.md")
                self.relocate(key, rel)
                it = self.seen(key)
                it.update({"path": rel, "title": title, "kind": "transcript"})
                self.s.media_jobs.append({"course": self, "key": key, "type": "youtube", "video_id": vid, "title": title,
                                          "rel_md": rel, "stamp": "yt1", "contexts": entry["contexts"]})
                continue
            if entry["kind"] == "att":
                meta = self.file_meta.get(entry["id"])
                if meta is None:
                    try:
                        meta = self.cv.get(f"/api/v1/files/{entry['id']}")
                        self.file_meta[entry["id"]] = meta
                    except CanvasError:
                        meta = {}
                twin = by_media_id.get(meta.get("media_entry_id")) if meta else None
                if twin and twin != key and twin in self.media:      # same video listed twice: keep one
                    other = self.media.pop(twin)
                    entry.setdefault("sources", other.get("sources"))
                    entry.setdefault("tracks", other.get("tracks"))
                    entry["contexts"] += [c for c in other["contexts"] if c not in entry["contexts"]]
                entry["file_meta"] = meta
        for key, entry in self.media.items():
            if entry["kind"] == "yt":
                continue
            meta = entry.get("file_meta") or {}
            title = entry.get("title") or meta.get("display_name") or f"Video {entry['id']}"
            base_title = safe_name(Path(title).stem if Path(title).suffix.lower() in MEDIA_EXT else title, 80)
            label = "recording" if "feedback" in title.lower() else "lecture video"
            rel = self.claim(key, f"{entry['rel_dir']}/{base_title} ({label}).md")
            self.relocate(key, rel)
            it = self.seen(key)
            it.update({"path": rel, "title": title, "kind": "transcript",
                       "from_listing": entry["contexts"] == ["Course media"]})
            if entry["kind"] == "att":
                tracks_url = f"/api/v1/media_attachments/{entry['id']}/media_tracks"
            else:
                tracks_url = f"/api/v1/media_objects/{entry.get('media_id') or entry['id']}/media_tracks"
            self.s.media_jobs.append({
                "course": self, "key": key, "type": entry["kind"], "title": title, "rel_md": rel,
                "file_id": entry["id"] if entry["kind"] == "att" else None, "sources": entry.get("sources"),
                "tracks": entry.get("tracks"), "tracks_url": tracks_url, "contexts": entry["contexts"],
                "stamp": f"{meta.get('updated_at') or ''}|{meta.get('size') or ''}", "size": meta.get("size"),
                "is_video": entry.get("media_type") != "audio",
            })

    def process_files(self) -> None:
        jobs = []
        for fid, (priority, rel_dir, label) in sorted(self.file_place.items(), key=lambda kv: (kv[1][0], kv[0])):
            meta = self.file_meta.get(fid)
            if not meta:
                continue
            name = safe_name(meta.get("display_name") or meta.get("filename") or label or f"file-{fid}")
            key = f"file:{fid}"
            rel = self.claim(key, f"{rel_dir}/{name}")
            prev = dict(self.items.get(key) or {})
            self.relocate(key, rel)
            it = self.seen(key)
            it.update({"path": rel, "text": rel + ".md", "title": meta.get("display_name") or name,
                       "contexts": self.file_contexts.get(fid, [])[:5]})
            jobs.append((fid, meta, rel, prev))
        if not jobs:
            return
        workers = max(1, min(4, int(self.cfg.get("download_workers") or 4)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._process_one, fid, meta, rel, prev) for fid, meta, rel, prev in jobs]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except AuthError:
                    raise
                except Exception as exc:
                    self.warn(f"File processing error: {exc}")

    def _extraction_current(self, original: Path, sidecar: Path) -> bool:
        """Legacy text and text extracted from different bytes must be rebuilt, even with the same stamp."""
        try:
            original, sidecar = self.s.lib.checked(original), self.s.lib.checked(sidecar)
            meta, _ = parse_front_matter(sidecar.read_text(encoding="utf-8"))
            expected = meta.get("source_sha256")
            # Reuses the fingerprint while the original is untouched (any write changes its signature).
            if not expected or expected != self.s.lib.digest(original):
                return False
            if meta.get("type") == "archive":
                directory = self.s.lib.checked(original.parent / (original.name + " (unzipped)"))
                total = 0
                with zipfile.ZipFile(original) as archive:
                    for member in [m for m in archive.infolist() if not m.is_dir()][:2000]:
                        name = member.filename.replace("\\", "/")
                        if name.startswith("__MACOSX/") or name.split("/")[-1].startswith("."):
                            continue
                        total += member.file_size
                        if total > 1024 * 1024 * 1024:
                            break
                        parts = [safe_name(part, 80) for part in name.split("/") if part not in ("", ".", "..")]
                        if not parts:
                            continue
                        child = self.s.lib.checked(directory.joinpath(*parts))
                        if not child.is_file():
                            return False
                        if kind_of(child) in ("media", "archive", "unknown"):
                            continue
                        child_sidecar = self.s.lib.checked(child.with_name(child.name + ".md"))
                        child_meta, _ = parse_front_matter(child_sidecar.read_text(encoding="utf-8"))
                        if (child_meta.get("source_archive") != original.relative_to(self.s.lib.root).as_posix()
                                or child_meta.get("source_archive_sha256") != expected
                                or not self._extraction_current(child, child_sidecar)):
                            return False
            return True
        except (OSError, UnicodeError, zipfile.BadZipFile):
            return False

    def _extract_bound(self, original: Path, content_type: Optional[str] = None):
        """Bind new extracted text to the original read by the extractor, never to a later replacement."""
        with self.s.extract_lock:  # PyMuPDF isn't thread-safe
            original = self.s.lib.checked(original)
            before = self._source_stamp(original)
            digest = file_digest(original)
            result = extract(original, content_type)
            after = self._source_stamp(original)
            if (before != after or file_digest(self.s.lib.checked(original)) != digest):
                raise OSError("The source changed during text extraction; retry the update.")
            return result, digest

    def _source_stamp(self, path: Path):
        st = self.s.lib.checked(path).stat()
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns

    def _process_one(self, fid: str, meta: Dict[str, Any], rel: str, prev: Dict[str, Any]) -> None:
        key = f"file:{fid}"
        it = self.item(key)
        original = self.s.lib.checked(self.dir / rel)
        sidecar = self.s.lib.checked(self.dir / (rel + ".md"))
        self.s.lib.checked(original.with_name(original.name + ".assets"))
        updated = meta.get("modified_at") or meta.get("updated_at") or ""
        size = meta.get("size")
        stamp = f"{updated}|{size}"
        it["attempted_stamp"] = stamp
        if prev.get("status") == "ok":
            it.setdefault("successful_stamp", prev.get("stamp"))
            it.setdefault("last_successful_sync", self.cstate.get("last_sync") or "")
        have_file = prev.get("stamp") == stamp and prev.get("status") == "ok" and original.exists()
        title = meta.get("display_name") or original.name
        context = ", ".join(self.file_contexts.get(fid, [])[:3])
        doc_meta = self.meta(title, "file", source_file=original.name, found_in=context,
                             canvas_url=self.canvas_url("file", fid), canvas_updated=fmt_dt(updated),
                             size=human_size(size))
        if meta.get("locked_for_user") or not meta.get("url"):
            reason = meta.get("lock_explanation") or "Canvas didn't provide a download link (locked or hidden)"
            it.update({"status": "locked", "error": reason, "stale": True})
            if not original.exists():
                self._write(sidecar, front_matter(doc_meta) + f"# {title}\n\n_Not downloaded: {reason}_\n")
            self._mark_file_status(sidecar, "restricted")
            self.bump("files_locked")
            return
        if (have_file and prev.get("path") == rel and sidecar.exists() and prev.get("extractor") == EXTRACTOR_VERSION
                and self._extraction_current(original, sidecar)):
            self.bump("files_unchanged")
            return
        max_mb = float(self.cfg.get("max_file_mb") or 400)
        if not have_file and size and size > max_mb * 1024 * 1024:
            it.update({"status": "too_large", "error": f"{human_size(size)} is over the {max_mb:g} MB limit", "stale": True})
            if not sidecar.exists():
                self._write(sidecar, front_matter(doc_meta) + f"# {title}\n\n_Not downloaded: {human_size(size)} is over "
                                                                    f"the size limit (max_file_mb in settings)._\n")
            self._mark_file_status(sidecar, "stale")
            self.bump("files_skipped")
            return
        if not have_file:
            try:
                info = self.cv.download(meta.get("url") or "", original, max_bytes=int(max_mb * 1024 * 1024),
                                        expect_html=original.suffix.lower() in (".html", ".htm"),
                                        file_id=fid, submission_id=meta.get("_submission_id"))
                self.bump("files_downloaded")
                it["content_type"] = info.get("content_type")
            except (DownloadError, CanvasError) as exc:
                it.update({"status": "failed", "error": str(exc), "attempts": int(it.get("attempts") or 0) + 1,
                           "stale": True})
                self.bump("files_failed")
                if not sidecar.exists():
                    self._write(sidecar, front_matter(doc_meta) + f"# {title}\n\n_Download failed: {_reason(exc)}. "
                                                                        f"It will be retried next sync._\n")
                self._mark_file_status(sidecar, "stale")
                return
        it.update(successful_stamp=stamp, last_successful_sync=now_iso())
        it.pop("stale", None)
        it.pop("error", None)
        try:
            result, source_sha256 = self._extract_bound(original, meta.get("content-type") or it.get("content_type"))
        except Exception as exc:
            result = None
            error = f"{exc.__class__.__name__}: {exc}"
        if result is None:
            it.update({"status": "ok", "extract_error": error, "stamp": stamp, "extractor": EXTRACTOR_VERSION})
            self._write(sidecar, front_matter(doc_meta) + f"# {title}\n\n_Couldn't extract text ({error}). "
                                                                f"Open the original: {original.name}_\n")
            return
        if result.kind == "archive":
            self._unpack(original, doc_meta, title)
            it.update({"status": "ok", "stamp": stamp, "extractor": EXTRACTOR_VERSION, "kind": "archive"})
            return
        doc_meta.update({"type": result.kind, "length": result.units, "source_sha256": source_sha256,
                         "visual_content": ", ".join(result.visual_units[:40]) + ("…" if len(result.visual_units) > 40 else ""),
                         "notes": "; ".join(result.notes)})
        body = describe.merge_into(self.s.lib, original, result.body or "_No text found in this file._")
        header = f"# {title}\n\nOriginal file: {original.name}"
        if result.visual_units:
            header += "\n\n> Some pages or slides are visual (see the '> Visual content' markers). " \
                      "Open those as images before relying on the text for charts, diagrams or equations."
        self._write(sidecar, front_matter(doc_meta) + header + "\n\n" + body + "\n")
        it.update({"status": "ok", "stamp": stamp, "extractor": EXTRACTOR_VERSION, "kind": result.kind,
                   "visual": len(result.visual_units)})
        it.pop("error", None)
        self.bump("files_extracted")

    def _mark_file_status(self, sidecar: Path, status: str) -> None:
        """Keep the previous text as a labeled historical copy, including when opened directly."""
        from .util import parse_front_matter
        if not sidecar.is_file():
            return
        meta, body = parse_front_matter(sidecar.read_text(encoding="utf-8"))
        body = re.sub(r"^> \*\*Source warning:\*\*.*\n\n?", "", body, flags=re.M)
        meta["sync_status"] = status
        message = ("Canvas now marks this file locked or restricted; this is a historical copy."
                   if status == "restricted" else "The latest source update did not complete; this is an older copy.")
        self._write(sidecar, front_matter(meta) + "> **Source warning:** " + message + "\n\n" + body)

    def process_my_files(self) -> None:
        """Index whatever the student drops into '<course>/My Files' (notes, textbook PDFs, recordings)."""
        root = self.s.lib.checked(self.dir / "My Files")
        if not root.exists():
            return
        present = set()
        for path in sorted(root.rglob("*")):
            parts = path.relative_to(root).parts
            if path.is_dir() or any(p.startswith(".") for p in parts):
                continue
            if any(p.endswith((".assets", "(unzipped)")) for p in parts[:-1]) or path.suffix.lower() == ".md":
                continue
            rel = path.relative_to(self.dir).as_posix()
            key = f"mine:{rel}"
            present.add(key)
            path = self.s.lib.checked(path)
            self.s.lib.checked(path.with_name(path.name + ".assets"))
            st = path.stat()
            stamp = f"{int(st.st_mtime)}|{st.st_size}"
            it = self.item(key)
            sidecar = self.s.lib.checked(path.with_name(path.name + ".md"))
            kind = kind_of(path)
            if kind == "media":
                it.update({"path": rel, "text": rel + ".md", "title": path.name, "kind": "media"})
                self.s.media_jobs.append({"course": self, "key": key, "type": "local", "title": path.name,
                                          "rel_md": rel + ".md", "local_path": str(path), "stamp": stamp,
                                          "size": st.st_size, "contexts": ["My Files"],
                                          "is_video": path.suffix.lower() in VIDEO_EXT})
                continue
            if (it.get("stamp") == stamp and sidecar.exists() and it.get("extractor") == EXTRACTOR_VERSION
                    and self._extraction_current(path, sidecar)):
                continue
            doc_meta = self.meta(path.name, "file", source_file=path.name, found_in="My Files")
            if kind == "archive":
                self._unpack(path, doc_meta, path.name)
                it.update({"path": rel, "text": rel + ".md", "title": path.name, "stamp": stamp, "status": "ok",
                           "extractor": EXTRACTOR_VERSION, "kind": "archive"})
                continue
            try:
                result, source_sha256 = self._extract_bound(path)
                error = ""
            except Exception as exc:
                result, error = None, f"{exc.__class__.__name__}: {exc}"
            if result is None:
                body = f"_Couldn't extract text ({error})._"
            else:
                doc_meta.update({"type": result.kind, "length": result.units, "source_sha256": source_sha256,
                                 "notes": "; ".join(result.notes),
                                 "visual_content": ", ".join(result.visual_units[:40])})
                body = describe.merge_into(self.s.lib, path, result.body or "_No text found in this file._")
            self._write(sidecar, front_matter(doc_meta) + f"# {path.name}\n\nOriginal file: {path.name}\n\n{body}\n")
            it.update({"path": rel, "text": rel + ".md", "title": path.name, "stamp": stamp, "status": "ok",
                       "extractor": EXTRACTOR_VERSION, "kind": result.kind if result else "file",
                       "visual": len(result.visual_units) if result else 0})
            self.bump("my_files_indexed")
        for key in [k for k in list(self.items) if k.startswith("mine:") and k not in present]:
            it = self.items.pop(key)
            if it.get("text"):
                side = self.s.lib.checked(self.dir / it["text"])
                if side.exists():
                    side.unlink()
                shutil.rmtree(self.s.lib.checked(self.dir / (it["text"][:-3] + ".assets")), ignore_errors=True)
                shutil.rmtree(self.s.lib.checked(self.dir / (it.get("path", "") + ".assets")), ignore_errors=True)

    def _unpack(self, archive: Path, doc_meta: Dict[str, Any], title: str) -> None:
        import zipfile

        archive = self.s.lib.checked(archive)
        archive_digest = file_digest(archive)
        target = self.s.lib.checked(archive.parent / (archive.name + " (unzipped)"))
        listing = []
        total = 0
        complete = True
        try:
            with zipfile.ZipFile(archive) as zf:
                members = [m for m in zf.infolist() if not m.is_dir()][:2000]
                for m in members:
                    name = m.filename.replace("\\", "/")
                    if name.startswith("__MACOSX/") or name.split("/")[-1].startswith("."):
                        continue
                    total += m.file_size
                    if total > 1024 * 1024 * 1024:
                        listing.append("(stopped: archive too large to unpack fully)")
                        break
                    parts = [safe_name(p, 80) for p in name.split("/") if p not in ("", ".", "..")]
                    if not parts:
                        continue
                    out = self.s.lib.checked(target.joinpath(*parts))
                    self.s.lib.checked(out.with_name(out.name + ".assets"))
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    listing.append("/".join(parts))
                    kind = kind_of(out)
                    if kind not in ("media", "archive", "unknown"):
                        try:
                            res, source_sha256 = self._extract_bound(out)
                        except Exception:
                            complete = False
                            continue
                        inner_meta = dict(doc_meta)
                        inner_meta.update({"title": out.name, "type": res.kind, "source_file": out.name,
                                           "source_sha256": source_sha256,
                                           "source_archive": archive.relative_to(self.s.lib.root).as_posix(),
                                           "source_archive_sha256": archive_digest,
                                           "found_in": f"{title} (zip)", "length": res.units})
                        self._write(out.parent / (out.name + ".md"),
                                          front_matter(inner_meta) + f"# {out.name}\n\n" + (res.body or "_No text._") + "\n")
        except (zipfile.BadZipFile, OSError) as exc:
            complete = False
            listing.append(f"(couldn't unpack: {exc})")
        if file_digest(self.s.lib.checked(archive)) != archive_digest:
            raise OSError("The archive changed while unpacking; retry the update.")
        doc_meta.update({"type": "archive"})
        doc_meta.pop("source_sha256", None)
        if complete:
            doc_meta["source_sha256"] = archive_digest
        self._write(archive.parent / (archive.name + ".md"), front_matter(doc_meta) + f"# {title}\n\n"
                          f"Unpacked into '{target.name}/':\n\n" + "\n".join(f"- {x}" for x in listing[:500]) + "\n")

    # ------------------------------------------------------------------ media jobs (called by Syncer)
    def media_up_to_date(self, job: Dict[str, Any]) -> bool:
        it = self.item(job["key"])
        out = self.s.lib.checked(self.dir / job["rel_md"])
        it["attempted_stamp"] = job.get("stamp")
        if it.get("status") == "ok":
            it.setdefault("successful_stamp", it.get("stamp"))
            it.setdefault("last_successful_sync", self.cstate.get("last_sync") or "")
        meta = self.file_meta.get(str(job.get("file_id"))) or {}
        if meta.get("locked_for_user") or job.get("locked_for_user"):
            it.update(status="locked", stale=True)
            if not out.exists():
                self._write(out, front_matter(self.meta(job["title"], "transcript", sync_status="restricted")) +
                            f"# {job['title']}\n\n_Not downloaded: Canvas marks this recording locked or restricted._\n")
            self._mark_file_status(out, "restricted")
            job["done"] = True
            return True
        original = self.s.lib.checked(Path(job["local_path"]) if job.get("local_path") else out.with_name(out.name[:-3]))
        needs_binding = bool(job.get("local_path") or original.is_file())
        if out.is_file() and not needs_binding:
            out_meta, _ = parse_front_matter(out.read_text(encoding="utf-8"))
            needs_binding = bool(out_meta.get("source_file") or out_meta.get("source_sha256"))
        binding_current = not needs_binding or self._extraction_current(original, out)
        if (it.get("status") == "ok" and not it.get("stale") and out.exists()
                and it.get("stamp") == job.get("stamp") and binding_current):
            job["done"] = True
            return True
        if job["type"] == "youtube" and it.get("status") == "failed" and int(it.get("attempts") or 0) >= 3:
            job["done"] = True
            return True
        if it.get("status") == "failed" and int(it.get("attempts") or 0) >= 3 and it.get("stamp") == job.get("stamp"):
            job["done"] = True   # gave up on this version after 3 tries; a new upload resets it
            return True
        return False

    def _vocabulary(self) -> str:
        names = [m.get("name") for m in self.snap.get("modules") or [] if m.get("name")]
        text = f"{self.course_label()}. Topics: " + "; ".join(names)
        return text[:700]

    def _write_transcript(self, job: Dict[str, Any], segments: List[Segment], source: str,
                          frames: Optional[list] = None, duration: Optional[float] = None, extra: str = "",
                          source_file: str = "", source_sha256: str = "") -> None:
        out = self.s.lib.checked(self.dir / job["rel_md"])
        feedback = all(str(c).startswith("Feedback on:") for c in job.get("contexts") or ["x"])
        meta = self.meta(job["title"], "feedback" if feedback else "transcript", source=source,
                         duration=fmt_ts(duration or (segments[-1].end if segments else 0)),
                         found_in=", ".join(job.get("contexts") or [])[:300],
                         screen_snapshots=len(frames or []) or None,
                         canvas_url=self.canvas_url("file", job["file_id"]) if job.get("file_id") else None)
        if source_file and source_sha256:
            meta.update(source_file=source_file, source_sha256=source_sha256)
        header = f"# {job['title']} (transcript)\n\nSource: {source}."
        if frames:
            header += (" Screen snapshots are linked where the picture changed; open them to see the slide, "
                       "whiteboard or worked example being discussed.")
        if extra:
            header += "\n\n" + extra
        body = transcript_markdown(segments, frames or [])
        self._write(out, front_matter(meta) + header + "\n\n" + (body or "_No speech detected._") + "\n")
        it = self.item(job["key"])
        it.update({"status": "ok", "stamp": job.get("stamp"), "source": source, "frames": len(frames or []),
                   "successful_stamp": job.get("stamp"), "attempted_stamp": job.get("stamp"),
                   "last_successful_sync": now_iso()})
        it.pop("error", None)
        it.pop("stale", None)
        it.pop("note", None)
        job["done"] = True
        self.bump("media_done")

    def _captions(self, job: Dict[str, Any]) -> List[Segment]:
        def order(t: Dict[str, Any]) -> tuple:
            return (0 if str(t.get("locale", "")).lower().startswith("en") else 1,
                    0 if t.get("kind") in ("subtitles", "captions") else 1)

        listed = [t for t in job.get("tracks") or [] if isinstance(t, dict)]
        tracks = [t for t in listed if t.get("content") or t.get("webvtt_content")]
        if not tracks and job.get("tracks_url"):
            try:
                fetched = self.cv.get_all(job["tracks_url"], {"include[]": ["content", "webvtt_content"]})
                tracks = [t for t in fetched if isinstance(t, dict) and (t.get("webvtt_content") or t.get("content"))]
                listed = listed or [t for t in fetched if isinstance(t, dict)]
            except CanvasError:
                pass
        if not tracks:
            # Media-object listings link each caption track instead of including it: read the caption file.
            for t in sorted((t for t in listed if t.get("url")), key=order)[:3]:
                try:
                    text = self.cv.get_text(t["url"])
                except (CanvasError, ValueError):
                    continue
                if "-->" in text:
                    tracks = [dict(t, content=text)]
                    break
        if not tracks:
            return []
        best = sorted(tracks, key=order)[0]
        return parse_captions(best.get("webvtt_content") or best.get("content") or "")

    def media_quick(self, job: Dict[str, Any]) -> None:
        if job["type"] == "youtube":
            if not self.cfg.get("youtube_transcripts", True):
                job["done"] = True
                return
            vid = job["video_id"]
            title, channel = youtube_title(vid)
            if title:
                job["title"] = title
            try:
                segments = youtube_transcript(vid)
            except Exception as exc:
                it = self.item(job["key"])
                it.update({"status": "failed", "error": f"No YouTube transcript ({exc.__class__.__name__})",
                           "attempts": int(it.get("attempts") or 0) + 1})
                self._write(self.dir / job["rel_md"], front_matter(self.meta(job["title"], "transcript",
                                  url=f"https://www.youtube.com/watch?v={vid}")) +
                                  f"# {job['title']}\n\nYouTube video: https://www.youtube.com/watch?v={vid}\n\n"
                                  f"_No transcript available from YouTube._\n")
                job["done"] = True
                return
            extra = f"YouTube: https://www.youtube.com/watch?v={vid}" + (f" · Channel: {channel}" if channel else "")
            self._write_transcript(job, segments, "YouTube captions", extra=extra)
            return
        segments = self._captions(job)
        if segments:
            job["captions"] = segments
            adjacent = self.s.lib.checked(self.dir / job["rel_md"][:-3])
            if not adjacent.is_file() and not (self.cfg.get("screen_snapshots", True) and job.get("is_video")):
                self._write_transcript(job, segments, "Canvas captions")

    def media_pending(self, job: Dict[str, Any], reason: str) -> None:
        if job.get("captions"):
            self._write_transcript(job, job["captions"], "Canvas captions")
            return
        out = self.s.lib.checked(self.dir / job["rel_md"])
        it = self.item(job["key"])
        if it.get("status") == "ok" and out.exists():
            it["note"] = f"newer version waiting: {reason}"   # keep the old transcript until the new one is ready
            it.setdefault("successful_stamp", it.get("stamp"))
            it["attempted_stamp"] = job.get("stamp")
            if it.get("stamp") != job.get("stamp"):
                it["stale"] = True
                self._mark_file_status(out, "stale")
            self.bump("media_pending")
            return
        it.update({"status": "pending", "error": reason})
        self._write(out, front_matter(self.meta(job["title"], "transcript")) +
                          f"# {job['title']} (transcript pending)\n\n_Not transcribed yet: {reason}._\n")
        self.bump("media_pending")

    def media_failed(self, job: Dict[str, Any], exc: Exception) -> None:
        if job.get("captions"):          # the video couldn't be fetched, but Canvas's captions are the lecture's words
            self._write_transcript(job, job["captions"], "Canvas captions",
                                   extra=f"_The video file itself couldn't be downloaded ({_reason(exc)}), so there "
                                         "are no screen snapshots._")
            return
        it = self.item(job["key"])
        attempts = int(it.get("attempts") or 0) + 1 if it.get("stamp") == job.get("stamp") else 1
        it.update({"status": "failed", "error": f"{exc.__class__.__name__}: {exc}", "attempts": attempts,
                   "stamp": job.get("stamp")})
        out = self.s.lib.checked(self.dir / job["rel_md"])
        if not out.exists():             # say so where the transcript would be, so nothing silently goes missing
            self._write(out, front_matter(self.meta(job["title"], "transcript")) +
                              f"# {job['title']} (no transcript yet)\n\n_Couldn't make a transcript: {_reason(exc)}. "
                              f"Super Student tries again on the next syncs._\n")
        self.warn(f"Media '{job['title']}': {exc}")
        job["done"] = True
        self.bump("media_failed")

    def media_transcribe(self, job: Dict[str, Any]) -> None:
        if job.get("local_path"):
            self._transcribe_file(job, Path(job["local_path"]), keep=True)
            return
        url = None
        if job.get("file_id"):
            meta = self.file_meta.get(job["file_id"]) or {}
            if not meta.get("url"):
                try:
                    meta = self.cv.get(f"/api/v1/files/{job['file_id']}")
                except CanvasError:
                    meta = {}
            url = meta.get("url")
        if not url and job.get("sources"):
            src = pick_media_source(job["sources"])
            url = src.get("url") if src else None
        if not url:
            if job.get("captions"):
                self._write_transcript(job, job["captions"], "Canvas captions")
                return
            self.media_pending(job, "Canvas didn't provide a download link for this recording")
            return
        max_mb = float(self.cfg.get("max_media_mb") or 3000)
        size = job.get("size")
        if isinstance(size, (int, float)) and size > max_mb * 1024 * 1024:
            if job.get("captions"):
                self._write_transcript(job, job["captions"], "Canvas captions",
                                       extra=f"_The video is {human_size(size)}, over the {max_mb:g} MB download limit "
                                             "(max_media_mb in settings), so there are no screen snapshots._")
                return
            raise DownloadError(f"too large to download ({human_size(size)}; the limit is max_media_mb = {max_mb:g} MB)")
        paired = self.s.lib.checked(self.dir / job["rel_md"][:-3])
        keep = bool(self.cfg.get("keep_media_files") or paired.is_file()) and job.get("rel_media")
        tmp_dir = self.s.lib.checked(self.s.lib.meta / "tmp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target = (self.dir / job["rel_media"]) if keep else tmp_dir / f"media-{abs(hash(job['key']))}{Path(job.get('rel_media') or job['title']).suffix or '.mp4'}"
        target = self.s.lib.checked(target)
        self.s.log(f"  ⤓ {job['title']} ({human_size(job.get('size'))})")
        self.cv.download(url, target, max_bytes=int(max_mb * 1024 * 1024), file_id=job.get("file_id"))
        self._transcribe_file(job, target, keep=bool(keep))

    def _transcribe_file(self, job: Dict[str, Any], target: Path, keep: bool) -> None:
        target = self.s.lib.checked(target)
        try:
            before = self._source_stamp(target)
            source_sha256 = file_digest(target)
            duration = media_duration(target)
            segments = job.get("captions")
            source = "Canvas captions"
            if not segments:
                self.s.log(f"  ✎ transcribing {job['title']} with {self.s.transcriber.describe()}…")
                t0 = time.time()
                segments = self.s.transcriber.transcribe(target, prompt=self._vocabulary())
                source = f"Whisper transcription ({self.s.transcriber.describe()}), {int(time.time() - t0)}s"
            frames = []
            if job.get("is_video") and self.cfg.get("screen_snapshots", True):
                rel_md = job["rel_md"]
                assets = self.s.lib.checked(self.dir / (rel_md[:-3] + ".assets"))
                shutil.rmtree(assets, ignore_errors=True)
                frames = extract_keyframes(target, assets)
            if before != self._source_stamp(target) or file_digest(self.s.lib.checked(target)) != source_sha256:
                raise OSError("The recording changed during transcription; retry the update.")
            self._write_transcript(job, segments, source, frames=frames, duration=duration,
                                   source_file=target.name if keep else "",
                                   source_sha256=source_sha256 if keep else "")
        finally:
            if not keep:
                try:
                    target.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------------ output
    def resolve_token(self, kind: str, ident: str) -> Tuple[bool, str]:
        if kind == "file":
            it = self.items.get(f"file:{ident}") or {}
            if it.get("path") and (self.dir / it["path"]).exists():
                return True, it["path"]
            if it.get("text") and (self.dir / it["text"]).exists():
                return True, it["text"]
            return False, self.canvas_url("file", ident)
        if kind == "page":
            slug = unquote(ident)
            rel = (self.page_paths.get(ident) or self.page_paths.get(slug)
                   or (self.items.get(f"page:{ident}") or self.items.get(f"page:{slug}") or {}).get("path"))
            return (True, rel) if rel else (False, self.canvas_url("page", ident))
        if kind in ("assignment", "quiz", "discussion"):
            rel = (self.items.get(f"{kind}:{ident}") or {}).get("path")
            if not rel and kind == "discussion":                   # announcements share discussion links
                rel = (self.items.get(f"announcement:{ident}") or {}).get("path")
            return (True, rel) if rel else (False, self.canvas_url(kind, ident))
        if kind == "media":
            k, _, i = ident.partition("-")
            key = self.alias.get(f"media:{k}:{i}", f"media:{k}:{i}")
            it = self.items.get(key) or {}
            target = it.get("text") if key.startswith("file:") else it.get("path")
            return (True, target) if target else (False, "#")
        if kind == "youtube":
            it = self.items.get(f"media:yt:{ident}") or {}
            return (True, it["path"]) if it.get("path") else (False, f"https://www.youtube.com/watch?v={ident}")
        return False, "#"

    def rewrite_tokens(self, body: str, doc_rel: str) -> str:
        doc_dir = posixpath.dirname(doc_rel) or "."

        def repl(match) -> str:
            local, target = self.resolve_token(match.group(2), match.group(3))
            if local:
                target = posixpath.relpath(target, doc_dir)
                target = target.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            return f"({target})"

        return TOKEN_RE.sub(repl, body)

    def write_docs(self, docs: Optional[List[Tuple[str, str, Dict[str, Any], str]]] = None) -> None:
        rerender = docs is not None
        for key, rel, meta, body in (docs if rerender else self.pending_docs):
            if rerender:
                rel = (self.items.get(key) or {}).get("path") or rel
            text = front_matter(meta) + f"# {meta.get('title')}\n\n" + self.rewrite_tokens(body, rel).strip() + "\n"
            try:
                if self._write(self.dir / rel, text):
                    self.bump("docs_written")
            except OSError as exc:            # e.g. a name the disk won't take: skip this one, keep going
                self.warn(f"Couldn't save '{rel}': {exc}")
                continue
            if not rerender:
                if key.startswith(("page:", "discussion:")):
                    self.cache_save(key, meta, body)
                self.written_docs.append((key, rel, meta, body))
        if not rerender:
            self.pending_docs = []

    def write_module_maps(self) -> None:
        current = {m["dir"] for m in self.snap.get("modules") or [] if m.get("dir")}
        if "modules" in self.listing_ok:
            old_dirs = {m.get("id"): m.get("dir") for m in self.old.get("modules") or [] if m.get("id") is not None}
            for m in self.snap.get("modules") or []:
                before = old_dirs.get(m.get("id"))
                if before and before != m["dir"]:            # renamed or moved module: its notes follow it
                    try:
                        notes.move(self.s.lib, f"{self.folder}/{before}", f"{self.folder}/{m['dir']}", exact=True)
                    except Exception:
                        pass
            modules_root = self.s.lib.checked(self.dir / "Modules")
            for stale in (modules_root.glob("*/_Module Contents.md") if modules_root.is_dir() else []):
                stale = self.s.lib.checked(stale)
                if f"Modules/{stale.parent.name}" not in current:
                    try:
                        stale.unlink()
                    except OSError:
                        continue
                    try:
                        stale.parent.rmdir()          # only if nothing else is left in it
                    except OSError:
                        pass
        for m in self.snap.get("modules") or []:
            lines = [f"Module {m['position']} of the course, in the order your instructor arranged it."]
            if m.get("unlock_at"):
                lines.append(f"Unlocks: {fmt_dt(m['unlock_at'])}.")
            if m.get("state") in ("locked",):
                lines.append("Currently locked for you.")
            lines.append("")
            doc_rel = f"{m['dir']}/_Module Contents.md"
            for ref in m.get("items") or []:
                indent = "  " * int(ref.get("indent") or 0)
                itype = ref.get("type")
                title = ref.get("title") or ""
                if itype == "SubHeader":
                    lines.append(f"\n### {title}\n")
                    continue
                target = text_target = None
                key = ref.get("key")
                if key:
                    key = self.alias.get(key, key)
                    it = self.items.get(key) or {}
                    target = it.get("path")
                    if it.get("text"):
                        text_target = it["text"]
                        if not (target and (self.dir / target).exists()):
                            target, text_target = text_target, None
                label = {"File": "File", "Page": "Page", "Assignment": "Assignment", "Quiz": "Quiz",
                         "Discussion": "Discussion", "ExternalUrl": "Link", "ExternalTool": "Outside tool"}.get(itype, itype or "Item")

                def rel_link(path: str) -> str:
                    return posixpath.relpath(path, m["dir"]).replace(" ", "%20").replace("(", "%28").replace(")", "%29")

                if target:
                    entry = f"{indent}- {label}: [{title}]({rel_link(target)})"
                    if text_target and (self.dir / text_target).exists():
                        entry += f" · [text]({rel_link(text_target)})"
                elif ref.get("url"):
                    entry = f"{indent}- {label}: [{title}]({ref['url']})"
                else:
                    entry = f"{indent}- {label}: {title}"
                extras = []
                if ref.get("due_at"):
                    extras.append(f"due {fmt_dt(ref['due_at'])}")
                if ref.get("points") is not None:
                    extras.append(f"{ref['points']} pts")
                if ref.get("locked"):
                    extras.append("locked" + (f": {ref['lock_explanation']}" if ref.get("lock_explanation") else ""))
                if itype == "ExternalTool":
                    extras.append("opens in an outside tool; not in this library")
                lines.append(entry + (f" ({'; '.join(extras)})" if extras else ""))
            text = front_matter(self.meta(f"Module {m['position']}: {m['name']}", "module")) + \
                f"# Module {m['position']}: {m['name']}\n\n" + "\n".join(lines).strip() + "\n"
            self._write(self.dir / doc_rel, text)

    # ------------------------------------------------------------------ finish
    def finish(self) -> None:
        self.write_docs()
        if not self.completed:            # the course stopped early: keep the last good view of what didn't run
            for k, v in self.old.items():
                self.snap.setdefault(k, v)
        if self.completed and self.retire_removed():
            self.write_docs(self.written_docs)     # links to moved items now point at the kept copies
            self.write_module_maps()
        self.snap["course"] = {
            "id": self.cid, "name": self.title, "code": self.course.get("course_code"),
            "term": (self.course.get("term") or {}).get("name"), "start_at": self.course.get("start_at"),
            "end_at": self.course.get("end_at"), "teachers": [t.get("display_name") for t in self.course.get("teachers") or []],
            "html_url": f"{self.base}/courses/{self.cid}", "time_zone": self.course.get("time_zone"),
            "enrollments": [{"type": e.get("type"), "current_score": e.get("computed_current_score"),
                             "current_grade": e.get("computed_current_grade"), "final_score": e.get("computed_final_score")}
                            for e in self.course.get("enrollments") or []],
        }
        self.snap["links"] = _dedupe(self.links, "url")
        self.snap["unreachable"] = _dedupe(self.unreachable, "url", "title")
        self.snap["warnings"] = self.warnings
        self.snap["folder"] = self.folder
        self.snap["synced_at"] = self.s.sync_id
        self.snap["stats"] = dict(self.stats)
        self.s.lib.save_snapshot(self.cid, self.snap)
        self.cstate["last_sync"] = self.s.sync_id
        from .overview import write_course_files

        write_course_files(self.s.lib, self.cid, self.snap, self.items, self.dir)
        from .notes import status_fn
        from .outline import write_outline

        write_outline(self.s.lib, self.dir, self.course_label(), status_fn(self.s.lib))

    def retire_removed(self) -> int:
        """Content that's gone from Canvas (deleted, replaced or hidden) leaves the course folders: it moves to
        '_Removed from Canvas/', marked with the date, so nothing current is confused with it and its name is free
        for whatever replaced it. Only when this sync saw everything that could still use it."""
        def complete(tab: str) -> bool:
            return tab in self.listing_ok or self.tab_hidden(tab)

        sources = all(complete(t) for t in REFERENCE_TABS)
        rules = {
            "file": sources and complete("files"),
            "media": sources,
            "page": complete("modules") and complete("pages"),
            "assignment": complete("assignments"), "quiz": complete("quizzes"),
            "discussion": complete("discussions"), "announcement": complete("announcements"),
        }
        today = datetime.now().strftime("%Y-%m-%d")
        moved = 0
        for key, it in list(self.items.items()):
            kind = key.split(":")[0]
            if not rules.get(kind) or it.get("seen") == self.s.sync_id:
                continue
            if kind == "media" and it.get("from_listing") and "media" not in self.listing_ok:
                continue
            if not it.get("removed"):
                it["removed"] = self.s.sync_id
                it["removed_on"] = today
            path = it.get("path") or ""
            if not path or path.startswith(REMOVED_DIR + "/"):
                continue
            target = f"{REMOVED_DIR}/{path}"
            if any((self.dir / (target + sfx)).exists() for sfx in ("", ".md")):
                target = add_suffix(target, key.split(":")[-1][-20:])
            try:
                self._move_files(path, target)
            except OSError as exc:
                self.warn(f"Couldn't move '{path}' (removed from Canvas): {exc}")
                continue
            it["path"] = target
            if it.get("text"):
                it["text"] = target + ".md"
            _stamp_removed(self.s.lib.checked(self.dir / (it.get("text") or target)), it.get("removed_on") or today)
            moved += 1
        if moved:
            self.bump("removed_from_canvas", moved)
            self.s.log(f"  {moved} item(s) no longer on Canvas moved to '{REMOVED_DIR}'")
        return moved

    def summary(self) -> Dict[str, Any]:
        return {"id": self.cid, "name": self.course_label(), "folder": self.folder, "stats": dict(self.stats),
                "warnings": self.warnings[:20]}


def _render_quiz_questions(questions: List[Dict[str, Any]], answers: Dict[str, Any], attempt: Dict[str, Any],
                           base: str, cid: str) -> str:
    """Past quiz questions as study material: each question, its choices, which is correct (when Canvas says),
    what you answered, and the instructor's answer comments."""
    if not questions:
        return ""
    score = attempt.get("kept_score") if attempt.get("kept_score") is not None else attempt.get("score")
    lines = ["## Question review", "",
             f"Your attempt {attempt.get('attempt')}" + (f", scored {score:g}" if isinstance(score, (int, float)) else "")
             + (f", finished {fmt_dt(attempt.get('finished_at'))}" if attempt.get("finished_at") else "") + "."]
    for n, qq in enumerate(sorted(questions, key=lambda x: (x.get("position") or 0, x.get("id") or 0)), 1):
        qtype = str(qq.get("question_type") or "").replace("_question", "").replace("_", " ")
        if qtype == "text only":
            text, _ = html_to_markdown(qq.get("question_text"), base, cid)
            lines += ["", text]
            continue
        pts = qq.get("points_possible")
        head = f"### Question {n}" + (f": {qq['question_name']}" if qq.get("question_name") and
                                       not re.fullmatch(r"(?i)question( \d+)?", qq["question_name"].strip()) else "")
        meta = ", ".join(x for x in (qtype, f"{pts:g} pts" if isinstance(pts, (int, float)) else "") if x)
        lines += ["", head + (f" ({meta})" if meta else ""), ""]
        text, _ = html_to_markdown(qq.get("question_text"), base, cid)
        lines.append(text or "_(no question text)_")
        choices = [a for a in qq.get("answers") or [] if isinstance(a, dict)]
        any_weight = any(isinstance(a.get("answer_weight", a.get("weight")), (int, float)) and
                         (a.get("answer_weight", a.get("weight")) or 0) > 0 for a in choices)
        by_id = {str(a.get("id")): a for a in choices}
        if choices:
            lines.append("")
            for a in choices:
                label = _answer_label(a)
                if not label:
                    continue
                weight = a.get("answer_weight", a.get("weight"))
                mark = " **(correct)**" if any_weight and isinstance(weight, (int, float)) and weight > 0 else ""
                lines.append(f"- {label}{mark}")
                if a.get("comments") or a.get("answer_comments") or a.get("comments_html"):
                    lines.append(f"  - Comment: {_one_line(html_text(a.get('comments_html')) or a.get('comments') or a.get('answer_comments'))}")
        given = answers.get(str(qq.get("id")))
        if given not in (None, "", [], {}):
            lines += ["", f"**Your answer:** {_answer_given(given, by_id)}"]
        remarks = []
        for key, label in (("correct_comments", "If correct"), ("incorrect_comments", "If incorrect"),
                           ("neutral_comments", "Note")):
            value = html_text(qq.get(key + "_html")) or qq.get(key)
            if value:
                remarks.append(f"- _{label}:_ {_one_line(value)}")
        if remarks:
            lines += [""] + remarks
    return "\n".join(lines).strip()


def _answer_label(a: Dict[str, Any]) -> str:
    if a.get("answer_match_left") or a.get("left"):
        return f"{a.get('answer_match_left') or a.get('left')} → {a.get('answer_match_right') or a.get('right') or ''}"
    text = html_text(a.get("html") or a.get("answer_html")) or a.get("text") or a.get("answer_text") or ""
    if not text and a.get("exact") is not None:
        text = f"{a['exact']:g}" + (f" ± {a['margin']:g}" if isinstance(a.get("margin"), (int, float)) and a["margin"] else "") \
            if isinstance(a.get("exact"), (int, float)) else str(a["exact"])
    if not text and a.get("start") is not None and a.get("end") is not None:
        text = f"between {a['start']} and {a['end']}"
    if not text and a.get("approximate") is not None:
        text = f"about {a['approximate']}"
    blank = a.get("blank_id")
    return (f"[{blank}] " if blank else "") + _one_line(text)


def _answer_given(given: Any, by_id: Dict[str, Dict[str, Any]]) -> str:
    def one(v: Any) -> str:
        if isinstance(v, dict):
            if "answer_id" in v and "match_id" in v:
                left = by_id.get(str(v.get("answer_id")), {})
                return f"{left.get('answer_match_left') or left.get('left') or v.get('answer_id')} → {v.get('match_id')}"
            return "; ".join(f"{k}: {one(x)}" for k, x in v.items())
        if isinstance(v, (list, tuple)):
            return "; ".join(one(x) for x in v)
        hit = by_id.get(str(v))
        if hit:
            return _answer_label(hit) or str(v)
        return _one_line(html_text(str(v)) if isinstance(v, str) and "<" in v else v)
    return one(given)


REMOVED_NOTE = "> **Removed from Canvas**"


def _stamp_removed(path: Path, when: str) -> None:
    """Mark a text version as no longer on Canvas (in its front matter and right under its title)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if REMOVED_NOTE in text:
        return
    note = (f"{REMOVED_NOTE} on {when} (deleted, replaced or hidden by the instructor). Kept for reference; "
            "it may be out of date, so prefer what's currently in the course.")
    head, body = "", text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            cut = text.find("\n", end + 4)
            cut = len(text) if cut == -1 else cut + 1
            head = text[:end] + f"\nremoved_from_canvas: {when}" + text[end:cut]
            body = text[cut:]
    lines = body.split("\n")
    title = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), -1)
    if title >= 0:
        lines[title + 1:title + 1] = ["", note]
    else:
        lines[0:0] = [note, ""]
    atomic_write_text(path, head + "\n".join(lines))


def _unstamp_removed(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if REMOVED_NOTE not in text and "removed_from_canvas:" not in text:
        return
    text = re.sub(r"\nremoved_from_canvas: [^\n]*", "", text, count=1)
    text = re.sub(r"\n\n" + re.escape(REMOVED_NOTE) + r"[^\n]*", "", text, count=1)
    atomic_write_text(path, text)


def _reason(exc: Exception) -> str:
    """A download or transcription problem in words a student can act on (the full error stays in the log)."""
    text = str(exc)
    status = getattr(exc, "status", None)
    match = re.search(r"HTTP (\d{3})", text)
    code = status or (int(match.group(1)) if match else 0)
    if "sign-in page" in text or "web page instead" in text or "too large" in text or "network error" in text:
        return text.split(" for http")[0]
    if code == 404:
        return "Canvas says the file isn't there any more"
    if code in (401, 403):
        return "Canvas didn't allow the download (the file may be locked or restricted)"
    if code >= 500:
        return f"Canvas had a server error ({code})"
    return re.sub(r"https?://\S+", "", text).strip() or exc.__class__.__name__


def _one_line(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _cell(text: Any) -> str:
    """Text that's safe inside one Markdown table cell (no line breaks or column bars)."""
    return _one_line(text).replace("|", "/")


def _rating_text(r: Dict[str, Any]) -> str:
    text = r.get("description") or ""
    if isinstance(r.get("points"), (int, float)):
        text += f" ({r['points']:g})"
    if r.get("long_description"):
        text += f": {r['long_description']}"
    return text


def _comment_summary(c: Dict[str, Any]) -> str:
    text = _one_line(c.get("comment"))
    extras = [f"attached file: {a.get('display_name') or a.get('filename') or 'file'}"
              for a in c.get("attachments") or [] if a.get("id")]
    mc = c.get("media_comment") or {}
    if mc.get("media_id"):
        extras.append(f"recorded {mc.get('media_type') or 'media'} comment")
    return (text + (" " if text and extras else "") + "; ".join(f"({e})" for e in extras)).strip()


def _clean_leftovers(lib: Library) -> None:
    """Remove half-downloaded files left by a sync that was interrupted (the lock says none is running now)."""
    shutil.rmtree(lib.meta / "tmp", ignore_errors=True)
    cutoff = time.time() - 3600
    try:
        for path in lib.root.rglob(".*.part"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _dedupe(rows: List[Dict[str, str]], *keys: str) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for row in rows:
        k = tuple(row.get(key) for key in keys)
        if k in seen:
            continue
        seen.add(k)
        out.append(row)
    return out
