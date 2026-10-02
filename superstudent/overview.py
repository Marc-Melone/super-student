"""Generated study files for each course, plus the library-wide INDEX.md."""

from __future__ import annotations

import posixpath
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .describe import strip_blocks
from .util import REMOVED_DIR, atomic_write_text, fmt_date, fmt_dt, front_matter, parse_dt, parse_front_matter, truncate

GENERATED = {"COURSE_OVERVIEW.md", "EXAM_INTEL.md", "CALENDAR.md", "GRADES.md", "LINKS.md", "_Module Contents.md",
             "OUTLINE.md"}

# Words that mean an exam, quiz or test, but not everyday course vocabulary that happens to share them
# ("the final value", "the test statistic", "comprehensive income", "examination of the data").
NOT_AN_EXAM = (r"value|values|answer|answers|step|steps|result|results|price|prices|payment|payments|cash|product|"
               r"products|stage|state|version|draft|grade|grades|score|scores|round|output|outcome|amount|balance|"
               r"year|period|day|days|week|section|chapter|part|project|paper|report|presentation|copy|form|"
               r"equation|formula|figure|number|total|line|point|decision|statistic|statistics|case|cases|set|"
               r"data|sample|samples|tube|run|suite|of|for|drive|subject|subjects|group|condition|tests")
EXAM_WORDS = re.compile(
    r"\b(mid-?terms?|final exam(?:s|ination)?|"
    rf"(?:the|our|your|this|next|upcoming|each) final\b(?!\s+(?:{NOT_AN_EXAM})\b)|"
    r"exams?\b|examinations?\b(?!\s+of\b)|quiz(?:zes)?\b|"
    rf"(?:the|next|this|upcoming|our|in-class|unit|first|second|third) test\b(?!\s+(?:{NOT_AN_EXAM})\b)|"
    r"test\s*#?\s*\d\b|study guide|review (?:session|sheet|packet|guide)|"
    r"practice (?:exam|test|midterm|final|problems?|questions?|set)|"
    r"formula sheet|cheat ?sheet|crib sheet|note ?card|index card|open[- ]book|closed[- ]book|open[- ]notes?|"
    r"(?:cumulative|comprehensive)\s+(?:final|exam|midterm|test|quiz)|"
    r"(?:is|are|will be|be)\s+(?:cumulative|comprehensive)\b(?!\s+(?:income|review of the literature)))",
    re.I,
)
EMPHASIS = re.compile(
    r"\b(will be on the|won'?t be on|will not be on|not be tested|(?:is|are) (?:fair game|testable)|"
    r"you (?:need|have|want|'ll need) to (?:know|memorize|understand|be able to)|make sure (?:you|to) (?:know|understand|can|review|practice)|"
    r"(?:very|really|super|extremely) important|this is (?:important|key|critical)|key (?:concept|takeaway|point|idea|formula)s?|"
    r"pay (?:close |special )?attention|remember (?:this|that)|(?:i'?ll|i will|we'?ll|going to) (?:test|ask) you|"
    r"expect (?:a|an|to see) (?:question|problem)|(?:on|for) the (?:exam|test|midterm|final))",
    re.I,
)
SECTION_ORDER = [
    ("announcement", "From announcements"),
    ("syllabus", "From the syllabus"),
    ("transcript", "From lectures (what was said in class)"),
    ("slides", "From slides"),
    ("pdf", "From PDFs and readings"),
    ("page", "From course pages"),
    ("assignment", "From assignments and quizzes"),
    ("discussion", "From discussions (instructor posts and prompts)"),
    ("other", "From other files"),
]


def _link(target: str, from_dir: str = ".") -> str:
    rel = posixpath.relpath(target, from_dir) if from_dir not in ("", ".") else target
    return rel.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pct(score: Optional[float], out_of: Optional[float]) -> str:
    try:
        return f"{100.0 * float(score) / float(out_of):.1f}%" if out_of else ""
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def _num(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return "" if value is None else str(value)


# ---------------------------------------------------------------- dated items

def dated_items(snap: Dict[str, Any]) -> List[Dict[str, Any]]:
    subs = snap.get("submissions") or {}
    rows: List[Dict[str, Any]] = []
    quiz_assignments = set()
    for q in snap.get("quizzes") or []:
        if q.get("assignment_id"):
            quiz_assignments.add(str(q["assignment_id"]))
        if q.get("due_at"):
            rows.append({"when": q["due_at"], "kind": "Quiz", "title": q["title"], "path": q.get("path"),
                         "detail": " · ".join(x for x in [f"{_num(q.get('points'))} pts" if q.get("points") is not None else "",
                                                          f"{q['time_limit']} min" if q.get("time_limit") else "",
                                                          f"{q['questions']} questions" if q.get("questions") else ""] if x)})
    for a in snap.get("assignments") or []:
        if str(a.get("id")) in quiz_assignments:
            continue
        if not a.get("due_at"):
            for c in a.get("checkpoints") or []:      # graded discussions with separate reply deadlines
                if c.get("due_at"):
                    what = c.get("name") or str(c.get("tag") or "checkpoint").replace("_", " ")
                    rows.append({"when": c["due_at"], "kind": "Due", "title": f"{a['name']}: {what}",
                                 "path": a.get("path"),
                                 "detail": f"{_num(c.get('points'))} pts" if c.get("points") is not None else ""})
            continue
        sub = subs.get(str(a.get("id"))) or {}
        status = "graded" if sub.get("score") is not None else (sub.get("workflow_state") or "")
        if sub.get("missing"):
            status = "missing"
        rows.append({"when": a["due_at"], "kind": "Due", "title": a["name"], "path": a.get("path"),
                     "detail": " · ".join(x for x in [f"{_num(a.get('points'))} pts" if a.get("points") is not None else "",
                                                      status] if x)})
    for e in snap.get("calendar") or []:
        if e.get("start_at"):
            rows.append({"when": e["start_at"], "kind": "Event", "title": e.get("title") or "Event", "path": None,
                         "detail": " · ".join(x for x in [e.get("location") or "", truncate(e.get("description") or "", 160)] if x)})
    for m in snap.get("modules") or []:
        if m.get("unlock_at"):
            rows.append({"when": m["unlock_at"], "kind": "Module opens", "title": m["name"],
                         "path": f"{m['dir']}/_Module Contents.md", "detail": ""})
    rows.sort(key=lambda r: parse_dt(r["when"]) or _now())
    return rows


def _is_exam(title: str) -> bool:
    return bool(re.search(r"\b(mid-?term|final|exam|test\b|quiz)", title or "", re.I))


# ---------------------------------------------------------------- course files

def write_course_files(lib, cid: str, snap: Dict[str, Any], items: Dict[str, Dict[str, Any]], course_dir: Path) -> None:
    course = snap.get("course") or {}
    label = course.get("name") or "Course"
    if course.get("code") and course["code"].lower() not in label.lower():
        label = f"{course['code']} - {label}"
    if course.get("term") and str(course.get("term")).lower() != "default term":
        label = f"{label} ({course['term']})"
    synced = fmt_dt(snap.get("synced_at"))
    base_meta = {"course": label, "synced": synced}
    rows = dated_items(snap)
    _write_calendar(course_dir, label, base_meta, rows)
    _write_grades(course_dir, label, base_meta, snap)
    _write_exam_intel(course_dir, label, base_meta, snap, rows)
    _write_links(course_dir, label, base_meta, snap)
    _write_overview(course_dir, label, base_meta, snap, items, rows)


def _write_overview(course_dir: Path, label: str, base_meta: Dict[str, Any], snap: Dict[str, Any],
                    items: Dict[str, Dict[str, Any]], rows: List[Dict[str, Any]]) -> None:
    course = snap.get("course") or {}
    now = _now()
    out: List[str] = [f"# {label}", ""]
    if course.get("teachers"):
        out.append(f"- **Instructor(s):** {', '.join(t for t in course['teachers'] if t)}")
    if course.get("start_at") or course.get("end_at"):
        out.append(f"- **Course dates:** {fmt_date(course.get('start_at')) or '?'} – {fmt_date(course.get('end_at')) or '?'}")
    for e in course.get("enrollments") or []:
        if e.get("current_score") is not None:
            grade = f" ({e['current_grade']})" if e.get("current_grade") else ""
            out.append(f"- **Current grade on Canvas:** {e['current_score']}%{grade}")
            break
    out.append(f"- **Canvas:** {course.get('html_url', '')}")
    out.append(f"- **Last synced:** {base_meta['synced']}")
    out += ["", "## Start here", "",
            "- [EXAM_INTEL.md](EXAM_INTEL.md): everything said about exams and quizzes, with sources",
            "- [CALENDAR.md](CALENDAR.md): every deadline and event, past and upcoming",
            "- [GRADES.md](GRADES.md): scores, rubric results and instructor feedback",
            "- [LINKS.md](LINKS.md): outside links, and course content that Canvas doesn't hand over"]
    if (course_dir / "Syllabus.md").exists():
        out.append("- [Syllabus.md](Syllabus.md)")
    upcoming = [r for r in rows if (parse_dt(r["when"]) or now) >= now][:10]
    if upcoming:
        out += ["", "## Coming up", ""]
        for r in upcoming:
            title = f"[{r['title']}]({_link(r['path'])})" if r.get("path") else r["title"]
            out.append(f"- **{fmt_dt(r['when'])}**: {r['kind']}: {title}" + (f" ({r['detail']})" if r.get("detail") else ""))
    exams = [r for r in rows if _is_exam(r["title"]) and r["kind"] != "Module opens"]
    if exams:
        out += ["", "## Exams and quizzes on the calendar", ""]
        for r in exams:
            past = (parse_dt(r["when"]) or now) < now
            title = f"[{r['title']}]({_link(r['path'])})" if r.get("path") else r["title"]
            out.append(f"- {fmt_dt(r['when'])}: {title}" + (" (past)" if past else ""))
    groups = snap.get("groups") or []
    if groups:
        out += ["", "## How the grade is calculated", ""]
        if snap.get("weighted"):
            out += ["| Category | Weight |", "|---|---|"]
            out += [f"| {g.get('name')} | {_num(g.get('weight'))}% |" for g in groups]
        else:
            out.append("Points-based (categories aren't weighted): " + ", ".join(g.get("name") or "" for g in groups))
    modules = snap.get("modules") or []
    if modules:
        out += ["", "## Course outline (modules, in order)", ""]
        for m in modules:
            when = f" (opens {fmt_date(m['unlock_at'])})" if m.get("unlock_at") else ""
            out.append(f"### [{m['position']}. {m['name']}]({_link(m['dir'] + '/_Module Contents.md')}){when}")
            titles = [i.get("title") for i in m.get("items") or [] if i.get("type") not in ("SubHeader",) and i.get("title")]
            if titles:
                out.append("")
                out.append("; ".join(titles[:25]) + (f"; … ({len(titles) - 25} more)" if len(titles) > 25 else ""))
            out.append("")
    docs: Counter = Counter()
    course_items: Counter = Counter()
    transcripts = waiting = no_transcript = not_downloaded = visual = 0
    removed = 0
    for key, it in items.items():
        if it.get("removed"):
            removed += str(it.get("path") or "").startswith(REMOVED_DIR + "/")
            continue
        kind = key.split(":")[0]
        if kind in ("file", "mine"):
            if it.get("kind") == "media":
                transcripts += it.get("status") == "ok"
                waiting += it.get("status") == "pending"
                no_transcript += it.get("status") == "failed"
            elif it.get("status") in ("locked", "failed", "too_large"):
                not_downloaded += 1
            else:
                docs[it.get("kind") or "file"] += 1
                visual += int(it.get("visual") or 0)
        elif kind == "media":
            transcripts += it.get("status") == "ok"
            waiting += it.get("status") == "pending"
            no_transcript += it.get("status") == "failed"
        elif kind != "syllabus":
            course_items[kind] += 1
    names = {"pdf": ("PDF", "PDFs"), "slides": ("slide deck", "slide decks"), "document": ("Word document", "Word documents"),
             "spreadsheet": ("spreadsheet", "spreadsheets"), "image": ("image", "images"), "text": ("text/code file", "text/code files"),
             "notebook": ("notebook", "notebooks"), "web": ("web page", "web pages"), "archive": ("zip archive", "zip archives"),
             "unsupported": ("file without a text version", "files without a text version"), "file": ("file", "files"),
             "page": ("course page", "course pages"), "assignment": ("assignment", "assignments"), "quiz": ("quiz", "quizzes"),
             "discussion": ("discussion", "discussions"), "announcement": ("announcement", "announcements")}

    def plural(n: int, key: str) -> str:
        one, many = names.get(key, (key, key + "s"))
        return f"{n} {one if n == 1 else many}"

    lines = []
    if docs:
        lines.append("- **Documents:** " + ", ".join(plural(n, k) for k, n in docs.most_common()))
    if transcripts or waiting or no_transcript:
        bits = [f"{transcripts} transcribed"] + ([f"{waiting} waiting for a transcript"] if waiting else []) + \
               ([f"{no_transcript} without a transcript"] if no_transcript else [])
        lines.append("- **Lectures and videos:** " + ", ".join(bits))
    if course_items:
        lines.append("- **Course items:** " + ", ".join(plural(n, k) for k, n in course_items.most_common()))
    if not_downloaded:
        lines.append(f"- **Not downloaded:** {not_downloaded} file{'s' if not_downloaded != 1 else ''} "
                     "(locked, blocked or too large; each has a note explaining why)")
    if removed:
        lines.append(f"- **No longer on Canvas:** {removed} item{'s' if removed != 1 else ''} the instructor deleted, "
                     f"replaced or hid, kept in `{REMOVED_DIR}/` and marked with the date (may be out of date)")
    if lines:
        out += ["", "## What's in this folder", ""] + lines
        if visual:
            out.append(f"\n{visual} pages or slides are flagged as visual (charts, diagrams, equations or scans): "
                       "look at those as images for exact details.")
    gaps = snap.get("unreachable") or []
    if gaps:
        out += ["", "## Not in this library", "",
                "These live in outside tools or are locked, so they aren't in this folder and answers can't draw on them:", ""]
        for g in gaps[:15]:
            out.append(f"- {g.get('what')}: {g.get('title')}" + (f" ({g['platform']})" if g.get("platform") else ""))
        if len(gaps) > 15:
            out.append(f"- …and {len(gaps) - 15} more in [LINKS.md](LINKS.md)")
    tabs = snap.get("tabs") or []
    shown = {t.get("id") for t in tabs if not t.get("hidden")}
    labels = {"pages": "Pages", "files": "Files", "assignments": "Assignments", "quizzes": "Quizzes",
              "discussions": "Discussions", "announcements": "Announcements", "modules": "Modules"}
    hidden = [t.get("label") for t in tabs if t.get("hidden")] or \
        ([name for tab, name in labels.items() if tab not in shown] if tabs else [])
    if snap.get("warnings") or hidden:
        out += ["", "## Sync notes", ""]
        if hidden:
            out.append(f"- Not in this course's menu for students: {', '.join(h for h in hidden if h)}. Everything "
                       "the modules and other course content link to is still included.")
        out += [f"- {w}" for w in (snap.get("warnings") or [])[:15]]
    meta = dict(base_meta, title="Course overview", type="overview")
    atomic_write_text(course_dir / "COURSE_OVERVIEW.md", front_matter(meta) + "\n".join(out).rstrip() + "\n")


def _write_calendar(course_dir: Path, label: str, base_meta: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    now = _now()
    upcoming = [r for r in rows if (parse_dt(r["when"]) or now) >= now]
    past = [r for r in rows if (parse_dt(r["when"]) or now) < now]
    out = [f"# Calendar: {label}", "", f"Times are in this computer's time zone. Synced {base_meta['synced']}.", ""]

    def fmt(r: Dict[str, Any]) -> str:
        title = f"[{r['title']}]({_link(r['path'])})" if r.get("path") else r["title"]
        return f"- **{fmt_dt(r['when'])}**: {r['kind']}: {title}" + (f" ({r['detail']})" if r.get("detail") else "")

    out += ["## Upcoming", ""] + ([fmt(r) for r in upcoming] or ["_Nothing dated coming up._"])
    out += ["", "## Past", ""] + ([fmt(r) for r in reversed(past)] or ["_Nothing yet._"])
    meta = dict(base_meta, title="Calendar", type="calendar")
    atomic_write_text(course_dir / "CALENDAR.md", front_matter(meta) + "\n".join(out) + "\n")


def _write_grades(course_dir: Path, label: str, base_meta: Dict[str, Any], snap: Dict[str, Any]) -> None:
    course = snap.get("course") or {}
    subs = snap.get("submissions") or {}
    assignments = snap.get("assignments") or []
    groups = {g.get("id"): g for g in snap.get("groups") or []}
    out = [f"# Grades and feedback: {label}", ""]
    for e in course.get("enrollments") or []:
        if e.get("current_score") is not None:
            out.append(f"**Canvas's current grade:** {e['current_score']}%" +
                       (f" ({e['current_grade']})" if e.get("current_grade") else "") +
                       " (counts only graded work).")
            break
    per_group: Dict[Any, List[float]] = defaultdict(lambda: [0.0, 0.0, 0])
    graded_rows = []
    for a in assignments:
        sub = subs.get(str(a.get("id"))) or {}
        if sub.get("score") is None or sub.get("excused") or a.get("omit_from_final_grade"):
            continue
        pts = a.get("points") or 0
        acc = per_group[a.get("group_id")]
        acc[0] += float(sub["score"])
        acc[1] += float(pts or 0)
        acc[2] += 1
        graded_rows.append((a, sub))
    if groups:
        out += ["", "## By category", "", "| Category | Weight | Graded items | Score | % |", "|---|---|---|---|---|"]
        for gid, g in groups.items():
            s, p, n = per_group.get(gid, [0.0, 0.0, 0])
            out.append(f"| {g.get('name')} | {_num(g.get('weight')) + '%' if snap.get('weighted') else '—'} | {n} | "
                       f"{_num(round(s, 2))} / {_num(round(p, 2))} | {_pct(s, p)} |")
        out.append("\n_Category percentages are raw totals; Canvas may also drop lowest scores or apply other rules._")
    out += ["", "## Graded work", "", "| Item | Due | Score | % | Notes |", "|---|---|---|---|---|"]
    if not graded_rows:
        out.append("| _No graded work yet_ | | | | |")
    for a, sub in sorted(graded_rows, key=lambda x: x[0].get("due_at") or ""):
        notes = ", ".join(x for x in ["late" if sub.get("late") else "", "missing" if sub.get("missing") else ""] if x)
        name = f"[{a['name']}]({_link(a['path'])})" if a.get("path") else a["name"]
        out.append(f"| {name} | {fmt_date(a.get('due_at'))} | {_num(sub.get('score'))} / {_num(a.get('points'))} | "
                   f"{_pct(sub.get('score'), a.get('points'))} | {notes} |")
    weak = sorted([(float(sub["score"]) / float(a["points"]), a, sub) for a, sub in graded_rows
                   if a.get("points") and float(a["points"]) > 0 and float(sub["score"]) / float(a["points"]) < 0.9],
                  key=lambda x: x[0])
    if weak:
        out += ["", "## Where points were lost (lowest first)", ""]
        for ratio, a, sub in weak[:15]:
            lost = [f"{r['criterion']} ({_num(r.get('points'))}/{_num(r.get('max'))})" for r in sub.get("rubric") or []
                    if isinstance(r.get("points"), (int, float)) and isinstance(r.get("max"), (int, float)) and r["points"] < r["max"]]
            out.append(f"- **{a['name']}**: {ratio * 100:.0f}%" + (f"; rubric: {', '.join(lost)}" if lost else ""))
    feedback = [(a, sub) for a in assignments for sub in [subs.get(str(a.get("id"))) or {}]
                if sub.get("comments") or sub.get("rubric")]
    if feedback:
        out += ["", "## Instructor feedback", ""]
        for a, sub in feedback:
            out.append(f"### {a['name']}" + (f" ({_num(sub.get('score'))}/{_num(a.get('points'))})" if sub.get("score") is not None else ""))
            for r in sub.get("rubric") or []:
                out.append(f"- {r.get('criterion')}: {_num(r.get('points'))}/{_num(r.get('max'))}" +
                           (f" — {r['rating']}" if r.get("rating") else "") + (f" — \"{r['comments']}\"" if r.get("comments") else ""))
            for c in sub.get("comments") or []:
                out.append(f"- {c.get('author') or 'Comment'} ({fmt_date(c.get('at'))}): {c.get('comment', '').strip()}")
            out.append("")
    meta = dict(base_meta, title="Grades and feedback", type="grades")
    atomic_write_text(course_dir / "GRADES.md", front_matter(meta) + "\n".join(out).rstrip() + "\n")


def _write_links(course_dir: Path, label: str, base_meta: Dict[str, Any], snap: Dict[str, Any]) -> None:
    out = [f"# Links and unreachable content: {label}", ""]
    gaps = snap.get("unreachable") or []
    out += ["## Content that isn't in this library", "",
            "Canvas doesn't give this to students through its API (outside tools, New Quizzes, locked items). "
            "If something here matters for an exam, download or print it to PDF and drop it into `My Files/`.", ""]
    if gaps:
        out += ["| What | Title | Where it appears | Tool |", "|---|---|---|---|"]
        for g in gaps:
            title = f"[{g.get('title')}]({g['url']})" if g.get("url") else g.get("title")
            out.append(f"| {g.get('what')} | {title} | {g.get('where') or ''} | {g.get('platform') or ''} |")
    else:
        out.append("_Nothing found._")
    links = snap.get("links") or []
    out += ["", "## Outside links", ""]
    out += [f"- [{truncate(l.get('text') or l['url'], 120)}]({l['url']}) (in {l.get('where') or 'course'})" for l in links] or ["_None._"]
    meta = dict(base_meta, title="Links and unreachable content", type="links")
    atomic_write_text(course_dir / "LINKS.md", front_matter(meta) + "\n".join(out) + "\n")


# ---------------------------------------------------------------- exam intelligence

def _sections(body: str) -> Iterable[Tuple[str, str]]:
    locator = ""
    buf: List[str] = []
    for line in body.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            if buf:
                yield locator, "\n".join(buf)
            locator = re.sub(r"^\[([^\]]+)\]", r"\1", m.group(1).strip())
            buf = []
        else:
            buf.append(line)
    if buf:
        yield locator, "\n".join(buf)


BOILERPLATE = re.compile(
    r"^\s*(-\s+\*\*[^*]{1,40}:\*\*|Original file:|Source:|> Visual content:|> Some pages or slides are visual|"
    r"> Canvas doesn't give students|YouTube:|Unpacked into)"
)


def _sentences(text: str) -> List[str]:
    text = "\n".join(ln for ln in text.splitlines() if not BOILERPLATE.match(ln))
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)            # drop image links
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)           # keep link text
    pieces = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n\s*[-*•]\s+|\n{2,}", text)
    return [re.sub(r"\s+", " ", p).strip(" -*•>") for p in pieces if p and p.strip()]


def _window(sentence: str, match: "re.Match[str]", limit: int = 320) -> str:
    if len(sentence) <= limit:
        return sentence
    start = max(0, match.start() - limit // 2)
    end = min(len(sentence), start + limit)
    return ("…" if start else "") + sentence[start:end].strip() + ("…" if end < len(sentence) else "")


def _instructor_replies(section: str) -> str:
    """Only the instructor's replies in a discussion thread, each in full (replies can run several lines)."""
    keep, out = False, []
    for line in section.splitlines():
        if re.match(r"^\s*- (\*\*|_\(deleted)", line):
            keep = "(Instructor)" in line
        if keep:
            out.append(line.strip())
    return "\n".join(out)


def _doc_bucket(meta: Dict[str, str], path: Path) -> str:
    kind = (meta.get("type") or "").lower()
    if kind in ("announcement", "syllabus", "transcript", "slides", "page", "discussion"):
        return kind
    if kind in ("assignment", "quiz", "feedback"):
        return "assignment"
    if kind in ("pdf", "document", "ebook"):
        return "pdf"
    return "other"


def _write_exam_intel(course_dir: Path, label: str, base_meta: Dict[str, Any], snap: Dict[str, Any],
                      rows: List[Dict[str, Any]]) -> None:
    found: Dict[str, List[Tuple[str, str, str, str, int]]] = defaultdict(list)   # bucket -> (date, excerpt, where, link, weight)
    seen = set()
    teachers = {t for t in (snap.get("course") or {}).get("teachers") or [] if t}
    for path in sorted(course_dir.rglob("*.md")):
        parts = path.relative_to(course_dir).parts
        if path.name in GENERATED or path.name.startswith(".") or "_Study" in parts[:-1] \
                or "Study Notes" in parts[:-1] or REMOVED_DIR in parts[:-1] or any(p.endswith(".assets") for p in parts[:-1]):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = parse_front_matter(text)
        body = strip_blocks(body)          # AI-written picture descriptions aren't the instructor's words
        bucket = _doc_bucket(meta, path)
        date = meta.get("date") or ""
        rel = path.relative_to(course_dir).as_posix()
        title = meta.get("title") or path.stem
        started_by = (re.search(r"^- \*\*Started by:\*\* (.+)$", body, re.M) or [None, ""])[1].strip()
        for locator, section in _sections(body):
            if bucket == "discussion" and not locator.lower().startswith("prompt"):
                section = _instructor_replies(section)
            elif bucket == "discussion" and teachers and started_by not in teachers:
                continue          # a classmate's topic: their guesses about the exam aren't the instructor's word
            for sentence in _sentences(section):
                if len(sentence) < 12:
                    continue
                m = EXAM_WORDS.search(sentence)
                strong = m is not None
                if not strong and bucket in ("transcript", "announcement", "syllabus", "page", "discussion"):
                    m = EMPHASIS.search(sentence)
                if not m:
                    continue
                if bucket in ("slides", "pdf", "other") and not strong:
                    continue
                excerpt = _window(sentence, m)
                norm = re.sub(r"\W+", " ", excerpt.lower()).strip()
                if norm in seen:
                    continue
                seen.add(norm)
                where = f"{title}, {locator}" if locator and locator.lower() not in title.lower() else title
                weight = (2 if strong and EMPHASIS.search(sentence) else 1 if strong or EMPHASIS.search(sentence) else 0)
                found[bucket].append((date, excerpt, where, rel, weight))
    out = [f"# Exam intel: {label}", "",
           "Automatically collected sentences that mention exams, quizzes, study guides, or that the instructor "
           "emphasized (\"make sure you know…\", \"this will be on the exam\"). It's a starting point: open the "
           "linked source to confirm context before relying on it.", ""]
    exams = [r for r in rows if _is_exam(r["title"]) and r["kind"] != "Module opens"]
    if exams:
        out += ["## Exam and quiz dates", ""]
        for r in exams:
            title = f"[{r['title']}]({_link(r['path'])})" if r.get("path") else r["title"]
            out.append(f"- {fmt_dt(r['when'])}: {title}" + (f" ({r['detail']})" if r.get("detail") else ""))
        out.append("")
    total = 0
    for bucket, heading in SECTION_ORDER:
        entries = found.get(bucket) or []
        if not entries:
            continue
        if bucket == "announcement":
            entries.sort(key=lambda e: parse_dt(e[0]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        elif len(entries) > 150:
            # Keep the clearest hints when there are too many: stated exam facts and emphasis first, in course order.
            keep = sorted(range(len(entries)), key=lambda i: (-entries[i][4], i))[:150]
            entries = [entries[i] for i in sorted(keep)] + [e for i, e in enumerate(entries) if i not in set(keep)]
        out += [f"## {heading}", ""]
        for date, excerpt, where, rel, _ in entries[:150]:
            prefix = f"{fmt_date(date)}: " if date and bucket == "announcement" and fmt_date(date) else ""
            out.append(f"- {prefix}\"{excerpt}\" ([{where}]({_link(rel)}))")
            total += 1
        if len(entries) > 150:
            out.append(f"- …{len(entries) - 150} more; search for them with `superstudent search`.")
        out.append("")
    if not total:
        out.append("_No exam-related statements found yet._")
    meta = dict(base_meta, title="Exam intel", type="exam_intel")
    atomic_write_text(course_dir / "EXAM_INTEL.md", front_matter(meta) + "\n".join(out).rstrip() + "\n")


# ---------------------------------------------------------------- library index

def write_library_index(lib, state: Dict[str, Any]) -> None:
    out = ["# Super Student library", "",
           "Your Canvas courses, mirrored and converted so an AI assistant (Claude, ChatGPT or Codex) can search and "
           "read them. Each course folder starts with COURSE_OVERVIEW.md. Instructions for assistants are in "
           "AGENTS.md and CLAUDE.md.", "",
           "| Course | Term | Folder | Last synced |", "|---|---|---|---|"]
    rows = []
    for cid, c in state.get("courses", {}).items():
        folder = c.get("folder") or ""
        if not folder or not (lib.root / folder).exists():
            continue
        rows.append((c.get("term") or "", c.get("name") or cid, folder, c.get("last_sync")))
    for term, name, folder, last in sorted(rows, key=lambda r: (r[0], r[1])):
        out.append(f"| [{name}]({_link(folder + '/COURSE_OVERVIEW.md')}) | {term} | {folder} | {fmt_dt(last)} |")
    atomic_write_text(lib.root / "INDEX.md", "\n".join(out) + "\n")
