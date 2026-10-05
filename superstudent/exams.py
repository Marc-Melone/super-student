"""Personal exam plans and source-backed practice, without claiming a mastery score.

An instructor's actual exam scope is not inferred here. Scope is selected by the student.
Quotes are checked against current course originals, but a quote does not prove that an
AI-written answer is correct. Multiple-choice scoring is mechanical; written-answer
ratings are explicitly self-reported. All state stays in the local library.
"""
from __future__ import annotations

import contextlib
import copy
import functools
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import unquote

from .library import LibraryPathError, _try_lock, _unlock
from .outline import GENERATED, LIGHT_SECTIONS, LINK, SKIP_PARTS, STUDY_SECTIONS, _load
from .util import atomic_write_json

VERSION = 1
MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_PLANS, MAX_QUESTIONS, MAX_ATTEMPTS = 100, 500, 2000
MAX_BATCH = 50
EVIDENCE_LABEL = "Quote checked against the source; answer not independently verified."
SCOPE_LABEL = "Student-selected scope; confirm the covered material and exam format with your instructor."
_thread_locks: Dict[str, threading.RLock] = {}
_thread_guard = threading.Lock()


class ExamError(ValueError):
    pass


def _api(fn):
    @functools.wraps(fn)
    def call(*args, **kwargs):
        try:
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
                    or not isinstance(module["dir"], str)):
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
    if len((json.dumps(store, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")) > MAX_STORE_BYTES:
        raise ExamError("The exam store is full. The previous saved data was left unchanged.")
    atomic_write_json(_store_path(lib), store)
    try:
        os.chmod(_store_path(lib), 0o600)
    except OSError:
        pass


@contextlib.contextmanager
def _locked(lib):
    """A separate advisory process lock plus an in-process lock protects read/modify/write."""
    lib.ensure()
    lock_path = lib.checked(lib.meta / "exams.lock")
    with _thread_guard:
        thread_lock = _thread_locks.setdefault(str(lock_path), threading.RLock())
    with thread_lock:
        with open(lock_path, "a+") as fh:
            acquired = False
            deadline = time.monotonic() + 10
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
    for cid, entry in lib.load_state().get("courses", {}).items():
        folder = entry.get("folder") or ""
        if not isinstance(folder, str) or not folder:
            continue
        cdir = lib.resolve(folder)
        if (cdir is None or cdir == lib.root or not cdir.is_dir()
                or cdir.relative_to(lib.root).as_posix() != folder):
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


def _module_rows(lib, cid):
    rows = []
    for item in lib.load_snapshot(cid).get("modules", []):
        pos, folder = item.get("position"), item.get("dir")
        if isinstance(pos, int) and not isinstance(pos, bool) and pos > 0 and isinstance(folder, str):
            rows.append({"position": pos, "name": str(item.get("name") or f"Module {pos}"), "dir": folder})
    return rows


def _inventory(lib, cdir, modules, module_rows, source_cache=None):
    from .evidence import source_fingerprint
    cdir = lib.checked(cdir)
    out, seen = [], set()
    memberships = {}

    def add(path, section):
        # Resolve both files before reading. A link to a different registered course is
        # inside the library but must never supply this course's titles or evidence.
        try:
            sidecar = lib.checked(path if path.suffix == ".md" else path.with_name(path.name + ".md"))
            original = lib.checked(sidecar.with_name(sidecar.name[:-3]))
            if (cdir not in sidecar.parents or cdir not in original.parents or not sidecar.is_file()
                    or sidecar in seen or sidecar.name in GENERATED):
                return
            if (modules and not section.startswith("Modules/") and memberships.get(sidecar)
                    and not memberships[sidecar].intersection(modules)):
                return
            parts = sidecar.relative_to(cdir).parts
            if any(p in SKIP_PARTS or p in ("Study Notes", "_Removed from Canvas") or p.startswith(".") or p.endswith(".assets") for p in parts[:-1]):
                return
            doc = _load(lib, sidecar, section)
            if doc is None:
                return
        except (LibraryPathError, OSError, ValueError):
            return
        seen.add(sidecar)
        canonical = sidecar.relative_to(lib.root).as_posix()
        try:
            fp = source_fingerprint(lib, canonical, cdir.relative_to(lib.root).as_posix(), source_cache=source_cache)
            missing = doc.missing
        except ValueError as exc:
            fp, missing = "", doc.missing or str(exc)
        out.append({"path": canonical, "original_path": doc.rel, "title": doc.title, "section": doc.section,
                    "kind": doc.kind, "missing": missing,
                    "source_fingerprint": fp,
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
            if cdir not in mdir.parents or not mdir.is_dir():
                continue
            contents = lib.checked(mdir / "_Module Contents.md")
            if cdir not in contents.parents:
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
                    if cdir in sidecar.parents and cdir in original.parents and sidecar.is_file():
                        memberships.setdefault(sidecar, set()).add(module["position"])
                except (LibraryPathError, OSError):
                    continue
        except (LibraryPathError, OSError, UnicodeError):
            continue
    for module, paths in module_paths:
        if not modules or module["position"] in modules:
            for path in paths:
                add(path, module["dir"])
    for top in STUDY_SECTIONS[1:] + LIGHT_SECTIONS:
        if top == "Syllabus":
            add(cdir / "Syllabus.md", top)
            continue
        try:
            directory = lib.checked(cdir / top)
            if cdir not in directory.parents:
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
    from .evidence import recheck_evidence
    result = recheck_evidence(lib, plan["course_folder"], question["citations"], source_cache=source_cache)
    if not result.get("valid"):
        return "stale", _messages(result.get("errors") or ["The cited source is unavailable or changed."])
    old = {(c["path"], c["locator"]): c.get("source_fingerprint") for c in question["citations"]}
    if any(old.get((r["path"], r["locator"])) != r.get("source_fingerprint") for r in result["records"]):
        return "stale", ["A cited source changed. Generate a new question from the current source."]
    return "current", []


def _scope_changes(lib, plan, source_cache=None):
    try:
        cid, _, _, cdir = _course(lib, plan["course_folder"])
        rows = _module_rows(lib, cid)
        now = _inventory(lib, cdir, plan["selected_modules"], rows, source_cache)
    except (ExamError, LibraryPathError, OSError):
        return True, ["The course or its sources are unavailable. Review this exam plan's scope."], []
    old = {r["path"]: (r.get("source_fingerprint"), r.get("missing")) for r in plan["inventory"]}
    current = {r["path"]: (r.get("source_fingerprint"), r.get("missing")) for r in now}
    changed = old != current or plan["available_modules"] != rows
    warnings = ["Course sources or module order changed after this plan was created. Review its scope; regenerate affected questions."] if changed else []
    missing = sum(bool(r.get("missing")) for r in now)
    if missing:
        warnings.append(f"{missing} source(s) are unavailable; practice cannot establish complete exam coverage.")
    return changed, warnings, now


def _metrics(plan):
    current_ids = {q["id"] for q in plan["questions"] if q.get("status") == "current"}
    attempts = [a for a in plan["attempts"] if a["question_id"] in current_ids]
    first = {}
    for attempt in attempts:
        if attempt.get("mode") == "mcq" and not attempt.get("assisted"):
            first.setdefault(attempt["question_id"], attempt)
    recent = [a for a in attempts if a.get("mode") == "mcq" and not a.get("assisted")][-20:]
    ratings = {key: sum(a.get("self_rating") == key for a in attempts if a.get("mode") == "self_report")
               for key in ("again", "hard", "good")}
    return {"first_attempt_mcq_count": len(first),
            "first_attempt_mcq_accuracy": round(sum(bool(a.get("correct")) for a in first.values()) / len(first), 3) if first else None,
            "recent_mcq_count": len(recent),
            "recent_mcq_accuracy": round(sum(bool(a.get("correct")) for a in recent) / len(recent), 3) if recent else None,
            "self_reported_ratings": ratings,
            "stale_attempts_excluded": len(plan["attempts"]) - len(attempts),
            "meaning": "Practice performance and self-reported review needs; not a measure of mastery or predicted exam marks."}


def _coverage(plan, inventory):
    available = {r["path"] for r in inventory if not r.get("missing") and r.get("source_fingerprint")}
    current = [q for q in plan["questions"] if q.get("status") == "current"]
    cited = {c["path"] for q in current for c in q["citations"]}.intersection(available)
    return {"available_sources": len(available), "cited_sources": len(cited),
            "uncited_source_paths": sorted(available - cited), "current_questions": len(current),
            "difficulty_counts": {level: sum(q["difficulty"] == level for q in current)
                                  for level in ("recall", "application", "transfer")},
            "meaning": "Practice source references; not exhaustive topic coverage or a measure of mastery."}


def _public(lib, stored, include_answers, source_cache=None):
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
    plan["scope_changed"], plan["scope_warnings"], current_inventory = _scope_changes(lib, plan, source_cache)
    plan["coverage"] = _coverage(plan, current_inventory)
    plan["scope_status"] = "student_selected_unconfirmed"
    plan["scope_note_label"] = SCOPE_LABEL
    plan["review_schedule"] = "Missed/again: 10 minutes; hard: 1 day; correct/good: 1, 3, 7, 14 then 30 days after successive successful reviews."
    if not include_answers:
        for attempt in plan["attempts"]:
            attempt.pop("answer", None)
    return plan


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
    source_cache = {}
    inventory = _inventory(lib, cdir, modules, rows, source_cache)
    if not inventory:
        raise ExamError("This course selection has no local source documents. Sync or add course material first.")
    stamp = _iso(_now())
    plan = {"id": uuid.uuid4().hex, "title": title, "course_id": cid, "course_folder": folder,
            "course_label": label, "exam_date": exam_date, "format": format, "scope_note": scope_note,
            "selected_modules": modules, "available_modules": rows, "inventory": inventory,
            "questions": [], "attempts": [], "created_at": stamp, "updated_at": stamp}
    with _locked(lib) as store:
        if len(store["plans"]) >= MAX_PLANS:
            raise ExamError("You have reached the limit of 100 exam plans.")
        store["plans"][plan["id"]] = plan
        public = _public(lib, plan, False, source_cache)
        _save_store(lib, store)
        return {"ok": True, "plan": public}


@_api
def get_plan(lib, plan_id, include_answers=True):
    if not isinstance(include_answers, bool):
        raise ExamError("include_answers must be true or false.")
    with _locked(lib) as store:
        return {"ok": True, "plan": _public(lib, _plan(store, plan_id), include_answers)}


def _validated_question(lib, plan, question, source_cache=None):
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
        row["correct_index"] = correct
    else:
        row["choices"] = []
    citations = question.get("citations")
    if not isinstance(citations, list) or not 1 <= len(citations) <= 8:
        raise ExamError("Every question needs 1–8 exact source quotes with a path and locator.")
    result = validate_evidence(lib, plan["course_folder"], citations, source_cache=source_cache)
    if not result.get("valid"):
        raise ExamError("Question evidence was rejected: " + "; ".join(_messages(result.get("errors") or ["Invalid citation."])))
    scoped = {r["path"]: r for r in plan["inventory"]}
    for record in result["records"]:
        source = scoped.get(record["path"])
        if source is None:
            raise ExamError("A citation is outside this plan's selected source inventory.")
        if record.get("source_status") != "current":
            raise ExamError("A selected source is unavailable or outdated. Regenerate from a current source.")
    row["citations"] = result["records"]
    payload = {k: row[k] for k in ("topic", "prompt", "type", "choices", "answer", "explanation", "difficulty", "citations")}
    payload["correct_index"] = row.get("correct_index")
    row["dedup_key"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
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
        validated = [_validated_question(lib, plan, q, source_cache) for q in questions]
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
        public = _public(lib, plan, False, source_cache)
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
        question["revealed"] = True
        question["revealed_at"] = _iso(_now())
        plan["updated_at"] = question["revealed_at"]
        public = _public(lib, plan, False, source_cache)
        revealed = copy.deepcopy(question)
        revealed.pop("dedup_key", None)
        revealed.update(status="current", status_errors=[], evidence_note=EVIDENCE_LABEL)
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
                   "assisted": bool(question.get("revealed")), "next_review_at": question["next_review_at"]}
        if mode == "mcq":
            attempt["choice_index"] = choice_index
        plan["attempts"].append(attempt)
        plan["updated_at"] = attempt["at"]
        public = _public(lib, plan, False, source_cache)
        _save_store(lib, store)
        return {"ok": True, "attempt": public["attempts"][-1], "plan": public,
                "feedback": "Your self-rating was saved; it is not automatic marking." if mode == "self_report" else "Correct choice." if correct else "Incorrect choice; review the explanation and cited source."}
