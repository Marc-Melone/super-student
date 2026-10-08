"""Personal exam plans and source-backed practice, without claiming a mastery score.

An instructor's actual exam scope is not inferred here. Scope is selected by the student.
Quotes are checked against current course originals, but a quote does not prove that an
AI-written answer is correct. Multiple-choice scoring is mechanical; written-answer
ratings are explicitly self-reported. All state stays in the local library.

A workspace follows the course as it changes: selected modules are kept by their Canvas id (an
inserted or renamed module doesn't change what was selected), material added to them later can be
practiced right away, and practice follows its sources when sync moves files. What changed since
the student last reviewed the workspace's sources is listed until they mark it reviewed.
"""
from __future__ import annotations

import contextlib
import copy
import functools
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from .library import LibraryPathError, _try_lock, _unlock
from .outline import GENERATED, LIGHT_SECTIONS, LINK, SKIP_PARTS, STUDY_SECTIONS, _has_review, _load
from .util import atomic_write_bytes, is_within, one_action, relative_posix

VERSION = 1
MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_PLANS, MAX_QUESTIONS, MAX_ATTEMPTS = 100, 500, 2000
MAX_BATCH = 50
MAX_REMOVE = 500
EVIDENCE_LABEL = "Quote checked against the source; answer not independently verified."
SCOPE_LABEL = "Student-selected scope; confirm the covered material and exam format with your instructor."
# Course references kept in a workspace for scope and exam hints (dates, coverage, rubric feedback) but not
# counted as study material that should have practice questions: announcements, assignments, discussions,
# the syllabus, calendar events, stand-alone pages and quizzes without past questions.
REFERENCE_KINDS = {"announcement", "assignment", "discussion", "syllabus", "event"}
REFERENCE_SECTIONS = {"Announcements", "Assignments", "Discussions", "Syllabus", "Pages", "Quizzes"}
_thread_locks: Dict[str, threading.RLock] = {}
_thread_guard = threading.Lock()


class ExamError(ValueError):
    pass


def _api(fn):
    @functools.wraps(fn)
    def call(*args, **kwargs):
        try:
            with one_action():             # each source is read and fingerprinted once per request
                return fn(*args, **kwargs)
        except (ExamError, LibraryPathError, OSError, UnicodeError) as exc:
            return {"ok": False, "error": str(exc), "message": str(exc)}
        except (TypeError, KeyError):
            message = "Invalid exam data or a damaged saved record. The exam store was left unchanged."
            return {"ok": False, "error": message, "message": message}
    return call


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _messages(errors):
    return [str(e.get("message") or "Invalid source evidence.") if isinstance(e, dict) else str(e)
            for e in errors]


def _text(value: Any, name: str, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ExamError(f"{name} must be text.")
    value = value.strip()
    if len(value) > limit or any(ord(c) < 32 and c not in "\n\t\r" for c in value):
        raise ExamError(f"{name} is too long or contains unsupported characters.")
    if required and not value:
        raise ExamError(f"{name} is required.")
    return value


def _store_path(lib):
    return lib.checked(lib.meta / "exams.json")


def _load_store(lib) -> dict:
    path = _store_path(lib)
    if not path.exists():
        return {"version": VERSION, "plans": {}}
    if path.stat().st_size > MAX_STORE_BYTES:
        raise ExamError("The exam store is too large; it was left unchanged.")
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ExamError("The saved exam store is unreadable. It was left unchanged; restore it from a backup.") from exc
    if (not isinstance(store, dict) or store.get("version") != VERSION
            or not isinstance(store.get("plans"), dict) or len(store["plans"]) > MAX_PLANS):
        raise ExamError("The saved exam store has an unsupported format. It was left unchanged.")
    for key, plan in store["plans"].items():
        if (not isinstance(plan, dict) or plan.get("id") != key
                or not isinstance(plan.get("questions"), list)
                or not isinstance(plan.get("attempts"), list)
                or not isinstance(plan.get("inventory"), list)
                or len(plan["questions"]) > MAX_QUESTIONS or len(plan["attempts"]) > MAX_ATTEMPTS):
            raise ExamError("A saved exam plan is damaged. The store was left unchanged.")
        for question in plan["questions"]:
            if (not isinstance(question, dict) or not isinstance(question.get("id"), str)
                    or question.get("type") not in ("mcq", "short_answer")
                    or not isinstance(question.get("citations"), list)):
                raise ExamError("A saved practice question is damaged. The store was left unchanged.")
        if any(not isinstance(a, dict) for a in plan["attempts"]):
            raise ExamError("A saved practice attempt is damaged. The store was left unchanged.")
        _validate_saved_plan(plan)
    return store


def _validate_saved_plan(plan):
    """Reject damaged records before any mutation, including fields used to build responses."""
    try:
        for field in ("id", "title", "course_id", "course_folder", "course_label", "exam_date", "format", "scope_note", "created_at", "updated_at"):
            if not isinstance(plan[field], str):
                raise ValueError
        if (not isinstance(plan["selected_modules"], list)
                or any(not isinstance(n, int) or isinstance(n, bool) for n in plan["selected_modules"])):
            raise ValueError
        if not isinstance(plan["available_modules"], list):
            raise ValueError
        for module in plan["available_modules"]:
            if (not isinstance(module, dict) or not isinstance(module["position"], int)
                    or isinstance(module["position"], bool) or not isinstance(module["name"], str)
                    or not isinstance(module["dir"], str) or not _valid_module_id(module.get("id"))):
                raise ValueError
        ids = plan.get("selected_module_ids")      # absent in plans made by 1.7.0
        if ids is not None and (not isinstance(ids, list) or len(ids) != len(plan["selected_modules"])
                                or any(not _valid_module_id(i) for i in ids)):
            raise ValueError
        if not isinstance(plan.get("scope_reviewed_at", ""), str):
            raise ValueError
        for source in plan["inventory"]:
            if any(not isinstance(source[field], str) for field in ("path", "original_path", "title", "section", "kind", "missing", "source_fingerprint")):
                raise ValueError
            if not isinstance(source["locators"], list) or any(not isinstance(l, str) for l in source["locators"]):
                raise ValueError
        ids = set()
        for question in plan["questions"]:
            if question["id"] in ids:
                raise ValueError
            ids.add(question["id"])
            for field in ("id", "topic", "prompt", "answer", "explanation", "difficulty", "next_review_at", "created_at", "dedup_key"):
                if not isinstance(question[field], str):
                    raise ValueError
            if (question["difficulty"] not in ("recall", "application", "transfer")
                    or not isinstance(question["revealed"], bool)
                    or not isinstance(question["success_streak"], int) or isinstance(question["success_streak"], bool)
                    or question["success_streak"] < 0 or not isinstance(question["choices"], list)
                    or any(not isinstance(c, str) for c in question["choices"])):
                raise ValueError
            if question["type"] == "mcq" and (not 2 <= len(question["choices"]) <= 8
                    or not isinstance(question["correct_index"], int) or isinstance(question["correct_index"], bool)
                    or not 0 <= question["correct_index"] < len(question["choices"])):
                raise ValueError
            if not 1 <= len(question["citations"]) <= 8:
                raise ValueError
            for citation in question["citations"]:
                if (not isinstance(citation, dict)
                        or any(not isinstance(citation[field], str) for field in ("path", "locator", "quote", "source_fingerprint", "source_status"))):
                    raise ValueError
        for attempt in plan["attempts"]:
            if (attempt["question_id"] not in ids or attempt["mode"] not in ("mcq", "self_report")
                    or not isinstance(attempt["assisted"], bool)
                    or any(not isinstance(attempt[field], str) for field in ("id", "at", "answer", "self_rating", "next_review_at"))):
                raise ValueError
            if attempt["mode"] == "mcq":
                if (not isinstance(attempt["correct"], bool) or not isinstance(attempt["choice_index"], int)
                        or isinstance(attempt["choice_index"], bool)):
                    raise ValueError
            elif attempt["correct"] is not None or attempt["self_rating"] not in ("again", "hard", "good"):
                raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ExamError("A saved exam record is damaged. The store was left unchanged; restore it from a backup.") from exc


def _save_store(lib, store):
    data = (json.dumps(store, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_STORE_BYTES:
        raise ExamError("The exam store is full. The previous saved data was left unchanged.")
    atomic_write_bytes(_store_path(lib), data)       # the same bytes atomic_write_json would write
    try:
        os.chmod(_store_path(lib), 0o600)
    except OSError:
        pass


@contextlib.contextmanager
def _locked(lib, wait: float = 10):
    """A separate advisory process lock plus an in-process lock protects read/modify/write."""
    lib.ensure()
    lock_path = lib.checked(lib.meta / "exams.lock")
    with _thread_guard:
        thread_lock = _thread_locks.setdefault(str(lock_path), threading.RLock())
    with thread_lock:
        with open(lock_path, "a+") as fh:
            acquired = False
            deadline = time.monotonic() + wait
            try:
                while not _try_lock(fh):
                    if time.monotonic() >= deadline:
                        raise ExamError("Another exam update is running. Try again in a moment.")
                    time.sleep(0.02)
                acquired = True
                yield _load_store(lib)
            finally:
                if acquired:
                    _unlock(fh)


def _course(lib, wanted):
    wanted = _text(wanted, "Course", 500).casefold()
    courses = []
    for cid, entry in lib.state_view().get("courses", {}).items():
        folder = entry.get("folder") or ""
        if not isinstance(folder, str) or not folder:
            continue
        cdir = lib.resolve(folder)
        if (cdir is None or cdir == lib.root or not cdir.is_dir()
                or relative_posix(cdir, lib.root) != folder):
            continue
        courses.append((str(cid), entry, cdir))
    if wanted:
        matches = [c for c in courses if wanted in (c[0].casefold(), c[1]["folder"].casefold())]
        if not matches:
            matches = [c for c in courses if wanted in (str(c[1].get("code") or "").casefold(),
                                                       str(c[1].get("name") or "").casefold())]
    else:
        matches = courses
    if len(matches) != 1:
        raise ExamError("Choose one exact course folder, course code, name or ID; this selection is missing or ambiguous.")
    cid, entry, cdir = matches[0]
    label = " - ".join(str(entry.get(k) or "") for k in ("code", "name") if entry.get(k)) or entry["folder"]
    return cid, entry["folder"], label, cdir


def _valid_module_id(value) -> bool:
    return value is None or (isinstance(value, (int, str)) and not isinstance(value, bool))


def _module_rows(lib, cid):
    rows = []
    for item in lib.load_snapshot(cid).get("modules", []):
        pos, folder, mid = item.get("position"), item.get("dir"), item.get("id")
        if isinstance(pos, int) and not isinstance(pos, bool) and pos > 0 and isinstance(folder, str):
            rows.append({"position": pos, "name": str(item.get("name") or f"Module {pos}"), "dir": folder,
                         "id": mid if _valid_module_id(mid) else None})
    return rows


def _selection(plan, rows):
    """The selected modules in the course as it is now: (current positions, or None for the whole course;
    names of selected modules that are gone). Modules are followed by their Canvas id, so an instructor
    inserting, reordering or renaming modules doesn't change what was selected. Plans made by 1.7.0 kept
    positions only: those are matched by the module's folder, then its name, then its position."""
    selected = plan["selected_modules"]
    if not selected:
        return None, []
    ids = plan.get("selected_module_ids") or [None] * len(selected)
    saved = {m["position"]: m for m in plan["available_modules"]}
    by_id = {r["id"]: r for r in rows if r.get("id") is not None}
    positions, missing = set(), []
    for pos, mid in zip(selected, ids):
        before = saved.get(pos) or {}
        mid = mid if mid is not None else before.get("id")
        if mid is not None and by_id:                    # the course's modules have ids: an unknown id is gone
            row = by_id.get(mid)
        else:
            row = (next((r for r in rows if before.get("dir") and r["dir"] == before["dir"]), None)
                   or next((r for r in rows if before.get("name") and r["name"] == before["name"]), None)
                   or next((r for r in rows if r["position"] == pos), None))
        if row is None:
            missing.append(before.get("name") or f"Module {pos}")
        else:
            positions.add(row["position"])
    return sorted(positions), missing


def _inventory(lib, cdir, modules, module_rows, source_cache=None):
    """Sources in scope: the selected modules (`modules` is a list of current positions, or None for the
    whole course) plus course references outside modules, such as the syllabus and announcements."""
    from .evidence import source_fingerprints
    cdir = lib.checked(cdir)
    out, seen = [], set()
    memberships = {}
    whole = modules is None
    modules = set(modules or ())

    def add(path, section):
        # Resolve both files before reading. A link to a different registered course is
        # inside the library but must never supply this course's titles or evidence.
        try:
            sidecar = lib.checked(path if path.suffix == ".md" else path.with_name(path.name + ".md"))
            original = lib.checked(sidecar.with_name(sidecar.name[:-3]))
            if (not is_within(sidecar, cdir) or not is_within(original, cdir) or not sidecar.is_file()
                    or sidecar in seen or sidecar.name in GENERATED):
                return
            if (not whole and not section.startswith("Modules/") and memberships.get(sidecar)
                    and not memberships[sidecar].intersection(modules)):
                return
            parts = tuple(relative_posix(sidecar, cdir).split("/"))
            if any(p in SKIP_PARTS or p in ("Study Notes", "_Removed from Canvas") or p.startswith(".") or p.endswith(".assets") for p in parts[:-1]):
                return
            doc = _load(lib, sidecar, section)
            if doc is None:
                return
        except (LibraryPathError, OSError, ValueError):
            return
        seen.add(sidecar)
        canonical = relative_posix(sidecar, lib.root)
        try:
            fp, legacy = source_fingerprints(lib, canonical, relative_posix(cdir, lib.root),
                                             source_cache=source_cache)
            missing = doc.missing
        except ValueError as exc:
            fp, legacy, missing = "", "", doc.missing or str(exc)
        if doc.kind == "quiz" or section == "Quizzes":
            role = "study" if _has_review(sidecar) else "reference"
        else:
            role = "reference" if doc.kind in REFERENCE_KINDS or section in REFERENCE_SECTIONS else "study"
        out.append({"path": canonical, "original_path": doc.rel, "title": doc.title, "section": doc.section,
                    "kind": doc.kind, "role": role, "missing": missing,
                    "source_fingerprint": fp, "_legacy": legacy,
                    "locators": [u.name for u in doc.units]})

    rows = module_rows
    if not rows:
        directory = lib.checked(cdir / "Modules")
        rows = [{"position": i, "dir": "Modules/" + p.name} for i, p in enumerate(sorted(directory.iterdir()), 1)
                if p.is_dir()] if directory.is_dir() else []
    # Source placement and source membership differ: a Files reading may be linked
    # from two modules. Collect every membership before applying the selected scope.
    module_paths = []
    for module in rows:
        try:
            mdir = lib.checked(cdir / module["dir"])
            if not is_within(mdir, cdir) or not mdir.is_dir():
                continue
            contents = lib.checked(mdir / "_Module Contents.md")
            if not is_within(contents, cdir):
                continue
            paths = []
            if contents.is_file():
                for _, target in LINK.findall(contents.read_text(encoding="utf-8")):
                    if "://" not in target:
                        paths.append(mdir / unquote(target))
            paths.extend(sorted(mdir.rglob("*.md")))
            module_paths.append((module, paths))
            for path in paths:
                try:
                    sidecar = lib.checked(path if path.suffix == ".md" else path.with_name(path.name + ".md"))
                    original = lib.checked(sidecar.with_name(sidecar.name[:-3]))
                    if is_within(sidecar, cdir) and is_within(original, cdir) and sidecar.is_file():
                        memberships.setdefault(sidecar, set()).add(module["position"])
                except (LibraryPathError, OSError):
                    continue
        except (LibraryPathError, OSError, UnicodeError):
            continue
    for module, paths in module_paths:
        if whole or module["position"] in modules:
            for path in paths:
                add(path, module["dir"])
    for top in STUDY_SECTIONS[1:] + LIGHT_SECTIONS:
        if top == "Syllabus":
            add(cdir / "Syllabus.md", top)
            continue
        try:
            directory = lib.checked(cdir / top)
            if not is_within(directory, cdir):
                continue
            for sidecar in sorted(directory.rglob("*.md")) if directory.is_dir() else []:
                add(sidecar, top)
        except (LibraryPathError, OSError):
            continue
    return out


def _plan(store, plan_id):
    if not isinstance(plan_id, str) or plan_id not in store["plans"]:
        raise ExamError("That exam plan was not found.")
    return store["plans"][plan_id]


def _question(plan, question_id):
    for question in plan["questions"]:
        if question.get("id") == question_id:
            return question
    raise ExamError("That practice question was not found in this exam plan.")


def _question_status(lib, plan, question, source_cache=None):
    """Current only if every saved quotation still occurs at its locator and each source's fingerprint still
    matches the one saved with the question (recheck_evidence also accepts the 1.7.0 fingerprint format)."""
    from .evidence import recheck_evidence
    result = recheck_evidence(lib, plan["course_folder"], question["citations"], source_cache=source_cache)
    if not result.get("valid"):
        return "stale", _messages(result.get("errors") or ["The cited source is unavailable or changed."])
    return "current", []


def _require_current_question(public_plan, question_id):
    """Honor the final source check before committing a change or returning an answer."""
    question = _question(public_plan, question_id)
    if question.get("status") != "current":
        errors = question.get("status_errors") or ["The cited source is unavailable or changed."]
        raise ExamError("This question's source changed during the update. Regenerate it before continuing: "
                        + "; ".join(_messages(errors)))
    return question


def _current_scope(lib, plan, source_cache=None) -> Dict[str, Any]:
    """The workspace's sources as the course is now: its selected modules (followed by Canvas id) and the
    course references outside modules."""
    cid, _, _, cdir = _course(lib, plan["course_folder"])
    rows = _module_rows(lib, cid)
    positions, gone = _selection(plan, rows)
    return {"rows": rows, "positions": positions, "gone": gone,
            "inventory": _inventory(lib, cdir, positions, rows, source_cache)}


def _public_rows(rows):
    return [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows]


def _named(rows, limit=3):
    names = [r.get("title") or r["path"].rsplit("/", 1)[-1] for r in rows[:limit]]
    return ", ".join(names) + (f" and {len(rows) - limit} more" if len(rows) > limit else "")


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _scope_changes(lib, plan, source_cache=None, scope=None):
    """What changed in the workspace's sources since the student last reviewed them (or created the
    workspace): (changed, warnings, current sources, details, scope used). Moved files were already carried
    along by sync, and a picture description saved later doesn't count as a change."""
    try:
        scope = scope or _current_scope(lib, plan, source_cache)
    except (ExamError, LibraryPathError, OSError):
        return True, ["The course or its sources are unavailable. Review this exam plan's scope."], [], {}, None
    now = scope["inventory"]
    reviewed = {r["path"]: r for r in plan["inventory"]}
    current = {r["path"]: r for r in now}
    added = [r for r in now if r["path"] not in reviewed]
    removed = [r for r in plan["inventory"] if r["path"] not in current]
    changed = [r for r in now if r["path"] in reviewed and (
        reviewed[r["path"]].get("missing") != r.get("missing")
        or reviewed[r["path"]].get("source_fingerprint") not in (r.get("source_fingerprint"), r.get("_legacy")))]
    warnings = []
    if added:
        warnings.append(f"{_plural(len(added), 'new source')} in this workspace's scope since its sources were "
                        f"last reviewed: {_named(added)}. Ask your AI for practice on them.")
    if changed:
        warnings.append(f"{_plural(len(changed), 'source')} changed since the sources were last reviewed: "
                        f"{_named(changed)}. Questions citing them need regenerating.")
    if removed:
        warnings.append(f"{_plural(len(removed), 'source')} left this workspace's scope (removed from Canvas, "
                        f"hidden, or moved out of the selected modules): {_named(removed)}.")
    if scope["gone"] and scope["positions"]:
        warnings.append("No longer in the course: selected module " + ", ".join(scope["gone"]) + ". Marking the "
                        "sources as reviewed drops it from this workspace.")
    elif scope["gone"]:
        warnings.append("None of this workspace's selected modules are in the course any more ("
                        + ", ".join(scope["gone"]) + "). Delete it, or create a new workspace.")
    details = {"added": [{"path": r["path"], "title": r.get("title", "")} for r in added],
               "changed": [{"path": r["path"], "title": r.get("title", "")} for r in changed],
               "removed": [{"path": r["path"], "title": r.get("title", "")} for r in removed],
               "missing_modules": list(scope["gone"])}
    has_changes = bool(warnings)
    missing = sum(bool(r.get("missing")) for r in now)
    if missing:
        warnings.append(f"{_plural(missing, 'source')} {'is' if missing == 1 else 'are'} unavailable; practice "
                        "cannot establish complete exam coverage.")
    return has_changes, warnings, now, details, scope


def _scope_for_write(lib, plan, source_cache):
    """The current scope for a change that's about to be saved, with 1.7.0 fingerprints brought up to date."""
    try:
        scope = _current_scope(lib, plan, source_cache)
    except (ExamError, LibraryPathError, OSError):
        return None
    _upgrade_fingerprints(lib, plan, scope, source_cache)
    return scope


def _dedup_key(row) -> str:
    payload = {k: row[k] for k in ("topic", "prompt", "type", "choices", "answer", "explanation", "difficulty", "citations")}
    payload["correct_index"] = row.get("correct_index")
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _upgrade_fingerprints(lib, plan, scope, source_cache=None) -> None:
    """Re-stamp practice and reviewed sources saved by 1.7.0 with the current fingerprint format while their
    sources are unchanged, so they keep following their sources when sync moves files."""
    from .evidence import recheck_evidence
    for question in plan["questions"]:
        result = recheck_evidence(lib, plan["course_folder"], question["citations"], source_cache=source_cache)
        if not result.get("valid"):
            continue
        updated = False
        for citation, record in zip(question["citations"], result["records"]):
            if citation["source_fingerprint"] != record["source_fingerprint"]:
                citation["source_fingerprint"] = record["source_fingerprint"]
                updated = True
        if updated:
            question["dedup_key"] = _dedup_key(question)
    current = {r["path"]: r for r in scope["inventory"]}
    for row in plan["inventory"]:
        now = current.get(row["path"])
        if now and row.get("source_fingerprint") and row["source_fingerprint"] == now.get("_legacy"):
            row["source_fingerprint"] = now["source_fingerprint"]


def _assisted(attempt) -> bool:
    """Whether the answer was shown before this attempt. 1.7.0 counted every quiz attempt after the first as
    assisted, because the app shows the worked answer after each check; its app could not show a quiz's answer
    before an attempt, so its saved quiz attempts count as unassisted (rule 2 records the actual order)."""
    if attempt.get("mode") == "mcq" and attempt.get("assist_rule") != 2:
        return False
    return bool(attempt.get("assisted"))


def _metrics(plan):
    current_ids = {q["id"] for q in plan["questions"] if q.get("status") == "current"}
    attempts = [a for a in plan["attempts"] if a["question_id"] in current_ids]
    quiz = [a for a in attempts if a.get("mode") == "mcq" and not _assisted(a)]
    first, latest = {}, {}
    for attempt in quiz:
        first.setdefault(attempt["question_id"], attempt)
        latest[attempt["question_id"]] = attempt
    recent = quiz[-20:]
    rate = lambda rows: round(sum(bool(a.get("correct")) for a in rows) / len(rows), 3) if rows else None
    ratings = {key: sum(a.get("self_rating") == key for a in attempts if a.get("mode") == "self_report")
               for key in ("again", "hard", "good")}
    return {"first_attempt_mcq_count": len(first),
            "first_attempt_mcq_accuracy": rate(list(first.values())),
            "latest_mcq_count": len(latest),
            "latest_mcq_accuracy": rate(list(latest.values())),
            "recent_mcq_count": len(recent),
            "recent_mcq_accuracy": rate(recent),
            "self_reported_ratings": ratings,
            "stale_attempts_excluded": len(plan["attempts"]) - len(attempts),
            "meaning": "Practice performance and self-reported review needs; not a measure of mastery or predicted "
                       "exam marks. First attempts show what you knew before practicing; latest attempts show where "
                       "review has got you. Attempts made after viewing the answer first are left out of both."}


def _coverage(plan, inventory):
    """Which study sources (lectures, readings, recordings, module pages, past quiz questions) have current
    practice. Course references such as announcements and the syllabus are counted apart: they set the scope,
    but a question about an office-hours reminder is not exam practice."""
    available = {r["path"]: r.get("role", "study") for r in inventory if not r.get("missing") and r.get("source_fingerprint")}
    study = {p for p, role in available.items() if role == "study"}
    current = [q for q in plan["questions"] if q.get("status") == "current"]
    cited = {c["path"] for q in current for c in q["citations"]}.intersection(available)
    return {"available_sources": len(study), "cited_sources": len(cited & study),
            "uncited_source_paths": sorted(study - cited),
            "reference_sources": len(available) - len(study), "cited_reference_sources": len(cited - study),
            "current_questions": len(current),
            "difficulty_counts": {level: sum(q["difficulty"] == level for q in current)
                                  for level in ("recall", "application", "transfer")},
            "meaning": "Practice source references for study material (lectures, readings, recordings, module "
                       "pages, past quiz questions); announcements, assignments, discussions, the syllabus and "
                       "other pages are counted separately as references. This is not exhaustive topic coverage or a "
                       "measure of mastery."}


def _public(lib, stored, include_answers, source_cache=None, scope=None):
    plan = copy.deepcopy(stored)
    source_cache = {} if source_cache is None else source_cache
    now = _iso(_now())
    queue = []
    for question in plan["questions"]:
        status, errors = _question_status(lib, plan, question, source_cache)
        question["status"], question["status_errors"] = status, errors
        attempts = [a for a in plan["attempts"] if a["question_id"] == question["id"]]
        latest = attempts[-1] if attempts else None
        weak = bool(latest and (latest.get("correct") is False or latest.get("self_rating") in ("again", "hard")))
        due = question.get("next_review_at") or ""
        if status == "current":
            priority = 0 if due and due <= now else 1 if weak else 2 if not attempts else 3
            queue.append({"question_id": question["id"], "topic": question["topic"], "priority": priority,
                          "reason": "due" if priority == 0 else "needs review" if priority == 1 else "not attempted" if priority == 2 else "scheduled",
                          "next_review_at": due})
        question["attempt_count"] = len(attempts)
        question["evidence_note"] = EVIDENCE_LABEL
        if not include_answers:
            for field in ("answer", "explanation", "correct_index"):
                question.pop(field, None)
            for citation in question["citations"]:
                citation.pop("quote", None)
        question.pop("dedup_key", None)
    queue.sort(key=lambda row: (row["priority"], row["next_review_at"], row["question_id"]))
    plan["review_queue"] = queue
    plan["metrics"] = _metrics(plan)
    (plan["scope_changed"], plan["scope_warnings"], current_inventory,
     plan["scope_changes"], scope) = _scope_changes(lib, plan, source_cache, scope)
    plan["coverage"] = _coverage(plan, current_inventory)
    # The sources and modules as the course is now. A workspace follows its modules by Canvas id, so
    # material added to them later is in scope and can be practiced right away.
    plan["inventory"] = _public_rows(current_inventory if scope is not None else plan["inventory"])
    if scope is not None and scope["positions"] is not None:
        names = {r["position"]: r["name"] for r in scope["rows"]}
        plan["selected_modules"] = scope["positions"]
        plan["selected_module_names"] = [names[p] for p in scope["positions"]]
    plan["scope_status"] = "student_selected_unconfirmed"
    plan["scope_note_label"] = SCOPE_LABEL
    plan["review_schedule"] = "Missed/again: 10 minutes; hard: 1 day; correct/good: 1, 3, 7, 14 then 30 days after successive successful reviews."
    for attempt in plan["attempts"]:
        attempt["assisted"] = _assisted(attempt)
        if not include_answers:
            attempt.pop("answer", None)
    return plan


AI_PROMPT_CHARS = 160
AI_QUEUE = 20
AI_DETAIL_LIMIT = 50


def ai_view(public, question_ids=None, include_locators=False) -> Dict[str, Any]:
    """A workspace as the connector sends it to an AI: everything needed to plan the next practice batch, without
    repeating every answer, quote and section name (a 100-question workspace was ~65,000 tokens in 1.7.0).
    Questions are listed by id, topic, status, a shortened prompt and what they cite; `question_ids` adds those
    questions in full, and `include_locators` lists every source's section locators."""
    if question_ids is not None and (not isinstance(question_ids, list) or len(question_ids) > AI_DETAIL_LIMIT
                                     or any(not isinstance(q, str) for q in question_ids)):
        raise ExamError(f"Ask for at most {AI_DETAIL_LIMIT} question ids at a time.")
    keys = ("id", "title", "course_folder", "course_label", "exam_date", "format", "scope_note", "selected_modules",
            "selected_module_names", "scope_status", "scope_note_label", "scope_changed", "scope_warnings",
            "scope_changes", "metrics", "coverage", "review_schedule", "created_at", "updated_at")
    out = {k: public[k] for k in keys if k in public}
    latest = {}
    for attempt in public["attempts"]:
        latest[attempt["question_id"]] = attempt
    rows = []
    for q in public["questions"]:
        prompt = q["prompt"]
        row = {"id": q["id"], "topic": q["topic"], "type": q["type"], "difficulty": q["difficulty"],
               "status": q["status"],
               "prompt": prompt if len(prompt) <= AI_PROMPT_CHARS else prompt[:AI_PROMPT_CHARS - 1].rstrip() + "\u2026",
               "cites": [{"path": c["path"], "locator": c["locator"]} for c in q["citations"]],
               "attempts": q.get("attempt_count", 0)}
        if q["status"] != "current":
            row["status_errors"] = q.get("status_errors") or []
        last = latest.get(q["id"])
        if last:
            row["last_result"] = (("correct" if last.get("correct") else "incorrect") if last.get("mode") == "mcq"
                                  else "self-rated " + last.get("self_rating", ""))
        rows.append(row)
    out["questions"] = rows
    out["question_count"] = len(rows)
    out["review_queue"] = [{"question_id": r["question_id"], "reason": r["reason"]}
                           for r in public.get("review_queue", [])[:AI_QUEUE]]
    out["review_queue_total"] = len(public.get("review_queue", []))
    out["sources"] = [{k: r[k] for k in (("path", "title", "role", "missing", "locators") if include_locators
                                          else ("path", "title", "role", "missing")) if k in r and (r[k] or k == "role")}
                      for r in public["inventory"]]
    if question_ids:
        by_id = {q["id"]: q for q in public["questions"]}
        unknown = [q for q in question_ids if q not in by_id]
        if unknown:
            raise ExamError("That practice question was not found in this exam plan: " + ", ".join(unknown[:3]))
        out["question_details"] = [by_id[q] for q in dict.fromkeys(question_ids)]
    out["detail_note"] = ("Questions are shortened. Pass question_ids for full questions with answers and quotes, "
                          "and include_locators for every source's section names (or read_material a source).")
    return out


def ai_save_reply(result) -> Dict[str, Any]:
    """What save_exam_questions tells the AI: what was saved and what is left to cover, not the whole workspace."""
    if not result.get("ok"):
        return result
    plan = result["plan"]
    stale = [q["id"] for q in plan["questions"] if q.get("status") != "current"]
    return {"ok": True, "added": result["added"], "skipped_duplicates": result["skipped_duplicates"],
            "question_ids": result["question_ids"], "question_count": len(plan["questions"]),
            "questions_needing_regeneration": stale, "coverage": plan["coverage"],
            "scope_warnings": plan["scope_warnings"], "evidence_note": result["evidence_note"]}


@_api
def list_plans(lib, course=""):
    folder = _course(lib, course)[1] if course else None
    with _locked(lib) as store:
        plans = []
        source_cache = {}
        for plan in store["plans"].values():
            if folder and plan["course_folder"] != folder:
                continue
            result = _public(lib, plan, False, source_cache)
            plans.append({key: result[key] for key in ("id", "title", "course_folder", "course_label", "exam_date", "format", "created_at", "updated_at", "scope_changed", "scope_warnings", "metrics")})
            plans[-1].update(question_count=len(plan["questions"]), attempt_count=len(plan["attempts"]))
        return {"ok": True, "plans": sorted(plans, key=lambda p: p["created_at"], reverse=True)}


@_api
def create_plan(lib, course, title, modules=None, exam_date="", format="", scope_note=""):
    cid, folder, label, cdir = _course(lib, course)
    title = _text(title, "Plan title", 200, True)
    exam_date = _text(exam_date, "Exam date", 10)
    if exam_date:
        try:
            if date.fromisoformat(exam_date).isoformat() != exam_date:
                raise ValueError
        except ValueError as exc:
            raise ExamError("Use an exam date in YYYY-MM-DD format.") from exc
    format = _text(format, "Exam format", 1000)
    scope_note = _text(scope_note, "Scope note", 5000)
    modules = [] if modules is None else modules
    rows = _module_rows(lib, cid)
    allowed = {m["position"] for m in rows}
    if (not isinstance(modules, list) or len(modules) > 200
            or any(not isinstance(m, int) or isinstance(m, bool) or m not in allowed for m in modules)):
        raise ExamError("Choose valid module positions from this course's current module list.")
    modules = sorted(set(modules))
    by_position = {r["position"]: r for r in rows}
    source_cache = {}
    inventory = _inventory(lib, cdir, modules or None, rows, source_cache)
    if not inventory:
        raise ExamError("This course selection has no local source documents. Sync or add course material first.")
    stamp = _iso(_now())
    plan = {"id": uuid.uuid4().hex, "title": title, "course_id": cid, "course_folder": folder,
            "course_label": label, "exam_date": exam_date, "format": format, "scope_note": scope_note,
            "selected_modules": modules, "selected_module_ids": [by_position[p]["id"] for p in modules],
            "available_modules": rows, "inventory": _public_rows(inventory),
            "questions": [], "attempts": [], "created_at": stamp, "updated_at": stamp, "scope_reviewed_at": stamp}
    scope = {"rows": rows, "positions": modules or None, "gone": [], "inventory": inventory}
    with _locked(lib) as store:
        if len(store["plans"]) >= MAX_PLANS:
            raise ExamError("You have reached the limit of 100 exam plans. Delete one you no longer need.")
        store["plans"][plan["id"]] = plan
        public = _public(lib, plan, False, source_cache, scope=scope)
        _save_store(lib, store)
        return {"ok": True, "plan": public}


@_api
def get_plan(lib, plan_id, include_answers=True):
    if not isinstance(include_answers, bool):
        raise ExamError("include_answers must be true or false.")
    with _locked(lib) as store:
        return {"ok": True, "plan": _public(lib, _plan(store, plan_id), include_answers)}


_FOLD = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                       "\u2212": "-", "\u00a0": " "})   # curly quotes, dashes, minus sign, no-break space
_LETTER = re.compile(r"^\(?([A-H])[).:](?:\s|$)|^([A-H])$")
_LABELLED = re.compile(r"^(?:the\s+)?(?:correct\s+)?(?:answer|option|choice)\s*(?:is\s*)?[:\-]?\s*\(?([a-h])\)?(?:[\s.,:;)]|$)",
                       re.I)


def _key_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).translate(_FOLD).casefold()
    return re.sub(r"\s+", " ", value).strip().strip(" .,;:!?")


def _mcq_key_problem(choices: List[str], correct: int, answer: str) -> Optional[str]:
    """Why a multiple-choice answer key looks inconsistent, or None. The worked answer and correct_index come
    from the AI separately; a miscount (correct_index counts from 0) would mark the right choice wrong at
    every review. The answer must not name or plainly restate a different choice."""
    stated = unicodedata.normalize("NFKC", answer).strip()
    label = _LETTER.match(stated) or _LABELLED.match(stated)
    if label:
        letter = next((g for g in label.groups() if g), "").upper()
        index = ord(letter) - ord("A") if letter else -1
        if 0 <= index < len(choices):
            return None if index == correct else f"the answer starts with option {letter}, which is choice {index}"
    text = _key_text(answer)
    keys = [_key_text(c) for c in choices]
    named = {i for i, key in enumerate(keys) if key and re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", text)}
    named = {i for i in named if not any(j != i and keys[i] in keys[j] and keys[i] != keys[j] for j in named)}
    if named == {correct}:
        return None
    if named and correct not in named:
        other = min(named)
        return f"the answer names choice {other} ({choices[other]!r})"
    from .index import STOPWORDS
    words = lambda value: {w for w in re.findall(r"\w+", _key_text(value)) if w not in STOPWORDS}
    said = words(answer)
    scores = [(len(w & said) / len(w)) if w else 0.0 for w in map(words, choices)]
    best = max(range(len(choices)), key=lambda i: scores[i])
    if best != correct and scores[best] >= 0.6 and scores[best] - scores[correct] >= 0.3:
        return f"the answer reads like choice {best} ({choices[best]!r})"
    return None


def _validated_question(lib, plan, question, scope_rows, source_cache=None):
    from .evidence import validate_evidence
    if not isinstance(question, dict):
        raise ExamError("Every question must be an object.")
    typ = question.get("type")
    if typ not in ("mcq", "short_answer"):
        raise ExamError("Question type must be mcq or short_answer.")
    row = {"id": uuid.uuid4().hex, "topic": _text(question.get("topic", ""), "Topic", 200, True),
           "prompt": _text(question.get("prompt", ""), "Question", 6000, True), "type": typ,
           "answer": _text(question.get("answer", ""), "Answer", 12000, True),
           "explanation": _text(question.get("explanation", ""), "Explanation", 12000, True),
           "difficulty": question.get("difficulty", "recall"), "revealed": False,
           "success_streak": 0, "next_review_at": "", "created_at": _iso(_now())}
    if row["difficulty"] not in ("recall", "application", "transfer"):
        raise ExamError("Difficulty must be recall, application or transfer.")
    if typ == "mcq":
        choices = question.get("choices")
        correct = question.get("correct_index")
        if (not isinstance(choices, list) or not 2 <= len(choices) <= 8
                or not isinstance(correct, int) or isinstance(correct, bool) or not 0 <= correct < len(choices)):
            raise ExamError("A multiple-choice question needs 2–8 choices and a valid correct_index.")
        row["choices"] = [_text(c, "Choice", 2000, True) for c in choices]
        if len(set(c.casefold() for c in row["choices"])) != len(row["choices"]):
            raise ExamError("Multiple-choice choices must be distinct.")
        problem = _mcq_key_problem(row["choices"], correct, row["answer"])
        if problem:
            raise ExamError(f"Answer key mismatch: correct_index counts from 0, so correct_index {correct} is "
                            f"{row['choices'][correct]!r}, but {problem}. Fix correct_index, or restate the correct "
                            "choice in the answer.")
        row["correct_index"] = correct
    else:
        row["choices"] = []
    citations = question.get("citations")
    if not isinstance(citations, list) or not 1 <= len(citations) <= 8:
        raise ExamError("Every question needs 1–8 exact source quotes with a path and locator.")
    result = validate_evidence(lib, plan["course_folder"], citations, source_cache=source_cache)
    if not result.get("valid"):
        raise ExamError("Question evidence was rejected: " + "; ".join(_messages(result.get("errors") or ["Invalid citation."])))
    scoped = {r["path"]: r for r in scope_rows}
    for record in result["records"]:
        source = scoped.get(record["path"])
        if source is None:
            raise ExamError(f"A citation is outside this plan's selected source inventory: {record['path']}. "
                            "Cite a source listed in the workspace's sources.")
        if record.get("source_status") != "current":
            raise ExamError("A selected source is unavailable or outdated. Regenerate from a current source.")
    row["citations"] = result["records"]
    row["dedup_key"] = _dedup_key(row)
    return row


@_api
def save_questions(lib, plan_id, questions):
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_BATCH:
        raise ExamError(f"Send between 1 and {MAX_BATCH} questions at a time.")
    if len(json.dumps(questions, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
        raise ExamError("This question batch is too large.")
    with _locked(lib) as store:
        plan = _plan(store, plan_id)
        source_cache = {}
        scope = _current_scope(lib, plan, source_cache)
        validated = [_validated_question(lib, plan, q, scope["inventory"], source_cache) for q in questions]
        _upgrade_fingerprints(lib, plan, scope, source_cache)
        seen = {q.get("dedup_key") for q in plan["questions"]}
        added, skipped = [], 0
        for question in validated:
            if question["dedup_key"] in seen:
                skipped += 1
                continue
            seen.add(question["dedup_key"])
            added.append(question)
        if len(plan["questions"]) + len(added) > MAX_QUESTIONS:
            raise ExamError("This exam plan has reached its limit of 500 practice questions.")
        plan["questions"].extend(added)
        plan["updated_at"] = _iso(_now())
        public = _public(lib, plan, False, source_cache, scope=scope)
        for question in added:
            _require_current_question(public, question["id"])
        _save_store(lib, store)
        return {"ok": True, "added": len(added), "skipped_duplicates": skipped,
                "question_ids": [q["id"] for q in added], "plan": public,
                "evidence_note": EVIDENCE_LABEL}


@_api
def reveal_question(lib, plan_id, question_id):
    with _locked(lib) as store:
        plan = _plan(store, plan_id)
        question = _question(plan, question_id)
        source_cache = {}
        status, errors = _question_status(lib, plan, question, source_cache)
        if status != "current":
            raise ExamError("This question needs regeneration: " + "; ".join(errors))
        # Viewing the worked answer after checking a quiz answer reviews that attempt; viewing it before answering
        # (always the case for a written answer, which is compared with it) makes the next attempt assisted.
        latest = next((a for a in reversed(plan["attempts"]) if a["question_id"] == question["id"]), None)
        if question["type"] == "mcq" and latest and latest["id"] != question.get("reviewed_attempt_id"):
            question["reviewed_attempt_id"] = latest["id"]
        else:
            question["answer_seen_first"] = True
        question["revealed"] = True
        question["revealed_at"] = _iso(_now())
        plan["updated_at"] = question["revealed_at"]
        public = _public(lib, plan, False, source_cache, scope=_scope_for_write(lib, plan, source_cache))
        current = _require_current_question(public, question_id)
        revealed = copy.deepcopy(question)
        revealed.pop("dedup_key", None)
        revealed.update(status=current["status"], status_errors=current["status_errors"], evidence_note=EVIDENCE_LABEL)
        _save_store(lib, store)
        return {"ok": True, "question": revealed, "plan": public}


@_api
def record_attempt(lib, plan_id, question_id, answer="", choice_index=-1, self_rating=""):
    answer = _text(answer, "Your answer", 12000)
    self_rating = _text(self_rating, "Self-rating", 20)
    with _locked(lib) as store:
        plan = _plan(store, plan_id)
        question = _question(plan, question_id)
        source_cache = {}
        status, errors = _question_status(lib, plan, question, source_cache)
        if status != "current":
            raise ExamError("This question needs regeneration before it can be scored: " + "; ".join(errors))
        if question["type"] == "mcq":
            if (not isinstance(choice_index, int) or isinstance(choice_index, bool)
                    or not 0 <= choice_index < len(question["choices"])):
                raise ExamError("Choose a valid answer choice.")
            if self_rating:
                raise ExamError("Multiple-choice questions are scored from the chosen answer, not a self-rating.")
            correct = choice_index == question["correct_index"]
            rating, mode = "good" if correct else "again", "mcq"
        else:
            if self_rating not in ("again", "hard", "good"):
                raise ExamError("Written answers need your self-rating: again, hard or good.")
            if not question.get("revealed"):
                raise ExamError("Reveal the expected answer before comparing it and self-rating your written answer.")
            if not answer:
                raise ExamError("Write your answer before recording a self-rating.")
            correct, rating, mode = None, self_rating, "self_report"
        if len(plan["attempts"]) >= MAX_ATTEMPTS:
            raise ExamError("This exam plan has reached its limit of 2,000 attempts; create a new plan to continue.")
        now = _now()
        if rating == "good":
            previous_due = question.get("next_review_at") or ""
            previous_streak = question.get("success_streak", 0)
            streak = previous_streak + 1 if not previous_due or previous_due <= _iso(now) else max(1, previous_streak)
            delay = timedelta(days=(1, 3, 7, 14, 30)[min(streak - 1, 4)])
        elif rating == "hard":
            streak, delay = 0, timedelta(days=1)
        else:
            streak, delay = 0, timedelta(minutes=10)
        question["success_streak"] = streak
        question["next_review_at"] = _iso(now + delay)
        attempt = {"id": uuid.uuid4().hex, "question_id": question["id"], "at": _iso(now),
                   "mode": mode, "answer": answer, "correct": correct,
                   "self_rating": self_rating if mode == "self_report" else "",
                   "assisted": bool(question.get("answer_seen_first")), "assist_rule": 2,
                   "next_review_at": question["next_review_at"]}
        question["answer_seen_first"] = False
        if mode == "mcq":
            attempt["choice_index"] = choice_index
        plan["attempts"].append(attempt)
        plan["updated_at"] = attempt["at"]
        public = _public(lib, plan, False, source_cache, scope=_scope_for_write(lib, plan, source_cache))
        _require_current_question(public, question_id)
        _save_store(lib, store)
        return {"ok": True, "attempt": public["attempts"][-1], "plan": public,
                "feedback": "Your self-rating was saved; it is not automatic marking." if mode == "self_report" else "Correct choice." if correct else "Incorrect choice; review the explanation and cited source."}


@_api
def review_scope(lib, plan_id):
    """The student has looked over the workspace's current sources: they become the new reviewed list, so
    the changes listed since the last review are cleared."""
    with _locked(lib) as store:
        plan = _plan(store, plan_id)
        source_cache = {}
        scope = _current_scope(lib, plan, source_cache)
        _upgrade_fingerprints(lib, plan, scope, source_cache)
        positions = scope["positions"]
        if positions:                    # if every selected module is gone, keep the selection (not whole course)
            by_position = {r["position"]: r for r in scope["rows"]}
            plan["selected_modules"] = positions
            plan["selected_module_ids"] = [by_position[p]["id"] for p in positions]
            scope = dict(scope, gone=[])                 # modules no longer in the course are dropped
        if positions or not plan["selected_modules"]:
            plan["available_modules"] = scope["rows"]
        plan["inventory"] = _public_rows(scope["inventory"])
        plan["scope_reviewed_at"] = plan["updated_at"] = _iso(_now())
        public = _public(lib, plan, False, source_cache, scope=scope)
        _save_store(lib, store)
        return {"ok": True, "plan": public}


@_api
def delete_plan(lib, plan_id):
    """Delete an exam workspace with its questions and practice history."""
    with _locked(lib) as store:
        plan = _plan(store, plan_id)
        del store["plans"][plan["id"]]
        _save_store(lib, store)
        return {"ok": True, "deleted": plan["id"], "title": plan["title"],
                "questions_removed": len(plan["questions"]), "attempts_removed": len(plan["attempts"])}


@_api
def remove_questions(lib, plan_id, question_ids):
    """Remove practice questions (a wrong answer key, a poor question, or one replaced after its source
    changed) together with their attempts."""
    if (not isinstance(question_ids, list) or not 1 <= len(question_ids) <= MAX_REMOVE
            or any(not isinstance(q, str) for q in question_ids)):
        raise ExamError(f"Send between 1 and {MAX_REMOVE} question ids.")
    with _locked(lib) as store:
        plan = _plan(store, plan_id)
        wanted = set(question_ids)
        if wanted - {q["id"] for q in plan["questions"]}:
            raise ExamError("That practice question was not found in this exam plan.")
        attempts = sum(a["question_id"] in wanted for a in plan["attempts"])
        plan["questions"] = [q for q in plan["questions"] if q["id"] not in wanted]
        plan["attempts"] = [a for a in plan["attempts"] if a["question_id"] not in wanted]
        plan["updated_at"] = _iso(_now())
        _save_store(lib, store)
        return {"ok": True, "removed": len(wanted), "attempts_removed": attempts,
                "question_count": len(plan["questions"])}


def move(lib, old_rel: str, new_rel: str) -> None:
    """Keep saved practice attached to its sources when sync moves them (a renamed or reordered module, or
    content removed from or returned to Canvas), as picture descriptions and study notes are. Covers the
    original, its text version and anything unpacked from it."""
    if not old_rel or old_rel == new_rel or not _store_path(lib).exists():
        return

    def moved(path):
        if path in (old_rel, old_rel + ".md") or path.startswith((old_rel + "/", old_rel + " (unzipped)/")):
            return new_rel + path[len(old_rel):]
        return None

    with _locked(lib, wait=60) as store:
        changed = False
        for plan in store["plans"].values():
            for question in plan["questions"]:
                cited = [moved(c["path"]) for c in question["citations"]]
                for citation, new in zip(question["citations"], cited):
                    if new is not None:
                        citation["path"] = new
                if any(new is not None for new in cited):
                    question["dedup_key"] = _dedup_key(question)
                    changed = True
            for row in plan["inventory"]:
                for field in ("path", "original_path"):
                    new = moved(row[field])
                    if new is not None:
                        row[field] = new
                        changed = True
        if changed:
            _save_store(lib, store)
