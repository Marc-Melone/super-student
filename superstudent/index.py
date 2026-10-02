"""Full-text search over the whole library (SQLite FTS5, BM25 ranking, English stemming).

Every Markdown file is split at its locator headings ([Page 12], [Slide 7], [00:32:10],
section titles), so each hit can be cited precisely and read in full afterwards.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .util import REMOVED_DIR, parse_front_matter

INDEX_VERSION = 3
SKIP_NAMES = {"INDEX.md", "CLAUDE.md", "AGENTS.md", "README.md"}
STOPWORDS = set("""a an and are as at be by can could did do does for from had has have how i if in into is it its
me my of on or our please should so than that the their them then there these this those to was were what when where
which who why will with would you your about explain tell show find give list does did""".split())
KIND_ALIASES = {
    "lecture": ["transcript"], "lectures": ["transcript"], "video": ["transcript"], "transcript": ["transcript"],
    "slide": ["slides"], "slides": ["slides"], "deck": ["slides"],
    "reading": ["pdf", "document", "web"], "readings": ["pdf", "document", "web"], "pdf": ["pdf"],
    "doc": ["document"], "document": ["document"], "spreadsheet": ["spreadsheet"], "excel": ["spreadsheet"],
    "assignment": ["assignment"], "homework": ["assignment"], "quiz": ["quiz"], "page": ["page"],
    "announcement": ["announcement"], "announcements": ["announcement"], "discussion": ["discussion"],
    "syllabus": ["syllabus"], "overview": ["overview", "exam_intel", "calendar", "grades", "links", "module"],
    "grades": ["grades"], "feedback": ["grades", "assignment"], "exam": ["exam_intel"],
    "notes": ["study_notes", "notes"], "study": ["study_notes"], "image": ["image"], "code": ["text", "notebook"],
}
KIND_ALIASES["feedback"] = ["grades", "assignment", "feedback"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS docs(path TEXT PRIMARY KEY, course TEXT, kind TEXT, title TEXT, mtime REAL, size INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    text, title, locator UNINDEXED, path UNINDEXED, course UNINDEXED, kind UNINDEXED, chunk UNINDEXED,
    doc_title UNINDEXED, tokenize = 'porter unicode61 remove_diacritics 2'
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    if not row or row[0] != str(INDEX_VERSION):
        conn.executescript("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS docs;")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('version', ?)", (str(INDEX_VERSION),))
        conn.commit()
    return conn


def _course_of(rel: str) -> str:
    parts = rel.split("/")
    return "/".join(parts[:2]) if len(parts) >= 3 else ""


def iter_markdown(root: Path) -> Iterable[Path]:
    real_root = root.resolve()
    for path in root.rglob("*.md"):
        rel_parts = path.relative_to(root).parts
        if path.is_symlink() and real_root not in path.resolve().parents:   # never index files outside the library
            continue
        if any(p.startswith(".") for p in rel_parts) or rel_parts[0] in ("_Exam Packs",) or "_Study" in rel_parts[:-1]:
            continue
        if any(p.endswith(".assets") for p in rel_parts[:-1]):
            continue
        if len(rel_parts) == 1 and path.name in SKIP_NAMES:
            continue
        yield path


LINK_MD = re.compile(r"!?\[([^\]]*)\]\([^)\s]*(?:\s+\"[^\"]*\")?\)")


def heading_locator(heading: str) -> str:
    """'[Slide 7] Title' -> 'Slide 7 Title'; '[Lecture slides](https://…)' -> 'Lecture slides'."""
    text = LINK_MD.sub(r"\1", heading.strip())
    return re.sub(r"^\[([^\]]+)\]\s*", r"\1 ", text).strip()


def heading_title(heading: str) -> str:
    """The words of a heading worth searching: 'Slide 7 Immunization' -> 'Immunization'; '00:12:30' -> ''."""
    text = re.sub(r"^\[[^\]]+\]\s*", "", LINK_MD.sub(r"\1", heading.strip()))
    return "" if re.fullmatch(r"(?:Slide|Page)\s+\d+|\d{1,2}:\d{2}(?::\d{2})?|Sheet: .*|Image", text) else text


def unique_locators(locators: List[str]) -> List[str]:
    """A heading used twice in one document ('Example') becomes 'Example', 'Example (2)', so every locator
    opens exactly one section."""
    seen: Dict[str, int] = {}
    out = []
    for loc in locators:
        key = _norm_loc(loc)
        seen[key] = seen.get(key, 0) + 1
        out.append(loc if seen[key] == 1 or not loc else f"{loc} ({seen[key]})")
    return out


def chunk_markdown(body: str, max_chars: int = 1800, target: int = 1200) -> List[Tuple[str, str]]:
    chunks: List[Tuple[str, str]] = []
    sections: List[Tuple[str, str, List[str], int]] = []   # (locator, heading words, lines, heading level)
    locator, title, level = "", "", 0
    buf: List[str] = []
    for line in body.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            sections.append((locator, title, buf, level))
            buf = []
            locator, title, level = heading_locator(m.group(2)), heading_title(m.group(2)), len(m.group(1))
            continue
        buf.append(line)
    sections.append((locator, title, buf, level))
    names = unique_locators([loc for loc, _, _, _ in sections])

    def flush(locator: str, title: str, buf: List[str], level: int) -> None:
        text = "\n".join(buf).strip()
        if not text and (not title or level == 1):     # a document's own title line is no hit on its own
            return
        # The heading's words are searchable with the section (a slide's title, a page's heading).
        text = f"{title}\n{text}".strip() if title else text
        if len(text) <= max_chars:
            chunks.append((locator, text))
            return
        paras = re.split(r"\n\s*\n", text)
        cur = ""
        for para in paras:
            while len(para) > max_chars:          # very long paragraph (e.g. a transcript block)
                cut = para.rfind(". ", 0, target)
                cut = cut + 1 if cut > target // 2 else target
                piece, para = para[:cut], para[cut:].lstrip()
                if cur:
                    chunks.append((locator, cur))
                    cur = ""
                chunks.append((locator, piece))
            if len(cur) + len(para) + 2 > target and cur:
                chunks.append((locator, cur))
                cur = cur[-150:] + "\n\n" + para if len(cur) > 150 else para
            else:
                cur = (cur + "\n\n" + para) if cur else para
        if cur.strip():
            chunks.append((locator, cur))

    for name, (_, title, lines, level) in zip(names, sections):
        flush(name, title, lines, level)
    return chunks


TEXT_VERSION = "4"   # bump when the indexed text changes shape, so every document is re-read once


def update_index(lib, log: Optional[Callable[[str], None]] = None, full: bool = False) -> Dict[str, int]:
    from .compact import for_index

    conn = connect(lib.index_path)
    row = conn.execute("SELECT value FROM meta WHERE key = 'text_version'").fetchone()
    if not row or row[0] != TEXT_VERSION:
        full = True
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('text_version', ?)", (TEXT_VERSION,))
    known = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT path, mtime, size FROM docs")}
    present = set()
    added = updated = removed = 0
    with conn:
        for path in iter_markdown(lib.root):
            rel = path.relative_to(lib.root).as_posix()
            present.add(rel)
            try:
                st = path.stat()
            except OSError:
                continue
            if not full and rel in known and known[rel] == (st.st_mtime, st.st_size):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, body = parse_front_matter(text)
            if meta.get("type") == "outline":      # a table of contents: read it, but don't let it crowd searches
                continue
            title = meta.get("title") or path.stem
            kind = meta.get("type") or ("notes" if "/My Files/" in f"/{rel}" else "file")
            course = _course_of(rel)
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            # The title is searchable once per document (on its first piece), so a title match doesn't make every
            # section of that document a hit; every piece still carries it for display.
            rows = [(chunk, title if i == 0 else "", loc, rel, course, kind, i, title)
                    for i, (loc, chunk) in enumerate(chunk_markdown(for_index(body)))]
            conn.executemany("INSERT INTO chunks(text, title, locator, path, course, kind, chunk, doc_title) "
                             "VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.execute("INSERT OR REPLACE INTO docs(path, course, kind, title, mtime, size) VALUES (?,?,?,?,?,?)",
                         (rel, course, kind, title, st.st_mtime, st.st_size))
            if rel in known:
                updated += 1
            else:
                added += 1
        for rel in set(known) - present:
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            conn.execute("DELETE FROM docs WHERE path = ?", (rel,))
            removed += 1
    total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    conn.close()
    if log and (added or updated or removed):
        log(f"Search index: {added} new, {updated} updated, {removed} removed ({total} documents)")
    return {"added": added, "updated": updated, "removed": removed, "documents": total}


def _terms(query: str) -> Tuple[List[str], List[str]]:
    phrases = re.findall(r'"([^"]+)"', query)
    rest = re.sub(r'"[^"]+"', " ", query)
    words = [w for w in re.findall(r"[\w'.$%-]+\*?", rest, flags=re.UNICODE)]
    cleaned = []
    for w in words:
        w = w.strip("'.-")
        if not w or (w.lower() in STOPWORDS and len(words) > 1):
            continue
        cleaned.append(w)
    fts = []
    for p in phrases:
        toks = re.findall(r"\w+", p)
        if toks:
            fts.append('"' + " ".join(toks) + '"')
    for w in cleaned:
        star = w.endswith("*")
        toks = re.findall(r"\w+", w.rstrip("*"))
        if not toks:
            continue
        term = '"' + " ".join(toks) + '"'
        fts.append(term + ("*" if star and len(toks) == 1 else ""))
    return fts, cleaned


def search(lib, query: str, course: str = "", kind: str = "", limit: int = 10, per_doc: int = 3,
           markers: tuple = ("**", "**")) -> List[Dict[str, Any]]:
    if not lib.index_path.exists():
        return []
    fts, _ = _terms(query)
    if not fts:
        return []
    conn = connect(lib.index_path)
    filters = []
    params: List[Any] = []
    if course:
        folders = course_folders(lib, course)
        if folders:
            filters.append("(" + " OR ".join("path LIKE ?" for _ in folders) + ")")
            params += [_like_prefix(f) for f in folders]
        else:
            filters.append("(course LIKE ? OR path LIKE ?)")
            params += [f"%{course}%", f"%{course}%"]
    kinds: List[str] = []
    for k in [k.strip().lower() for k in re.split(r"[,\s]+", kind or "") if k.strip()]:
        kinds += KIND_ALIASES.get(k, [k])
    if kinds:
        filters.append("kind IN (%s)" % ",".join("?" * len(kinds)))
        params += kinds
    where = (" AND " + " AND ".join(filters)) if filters else ""
    sql = ("SELECT path, doc_title, locator, course, kind, snippet(chunks, 0, ?, ?, ' … ', 48), "
           "bm25(chunks, 1.0, 3.0), chunk FROM chunks WHERE chunks MATCH ?" + where +
           " ORDER BY bm25(chunks, 1.0, 3.0) LIMIT ?")
    results: List[Dict[str, Any]] = []
    seen = set()
    per_path: Dict[str, int] = {}
    strategies = [" AND ".join(fts)]
    if len(fts) > 1:
        strategies.append(" OR ".join(fts))
    for match in strategies:
        try:
            rows = conn.execute(sql, [markers[0], markers[1], match] + params + [limit * 4]).fetchall()
        except sqlite3.OperationalError:
            continue
        for path, title, locator, crs, knd, snip, score, chunk in rows:
            key = (path, locator, chunk)
            if key in seen or per_path.get(path, 0) >= per_doc:
                continue
            seen.add(key)
            per_path[path] = per_path.get(path, 0) + 1
            results.append({"path": path, "title": title, "locator": locator, "course": crs, "kind": knd,
                            "snippet": re.sub(r"\s+", " ", snip).strip(), "score": round(-score, 3),
                            "match": "all words" if match == strategies[0] else "some words",
                            "removed": is_removed(path)})
        if sum(1 for r in results if not r["removed"]) >= limit:
            break
    conn.close()
    # What's currently in the course comes first; copies of things removed from Canvas come after, labeled.
    results.sort(key=lambda r: r["removed"])
    return results[:limit]


def is_removed(path: str) -> bool:
    return f"/{REMOVED_DIR}/" in f"/{path}"


def _simple(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).strip()


def course_folders(lib, course: str) -> List[str]:
    """Course folders whose name, code, Canvas id or folder matches (punctuation and case ignored)."""
    want = _simple(course)
    if not want:
        return []
    out = []
    for cid, c in lib.load_state().get("courses", {}).items():
        folder = c.get("folder") or ""
        if folder and want in _simple(f"{cid} {c.get('name', '')} {c.get('code', '')} {folder}"):
            out.append(folder)
    return out


def _like_prefix(folder: str) -> str:
    return folder + "/%"


def list_documents(lib, course: str = "", kind: str = "") -> List[Dict[str, Any]]:
    if not lib.index_path.exists():
        return []
    conn = connect(lib.index_path)
    sql = "SELECT path, course, kind, title FROM docs WHERE 1=1"
    params: List[Any] = []
    if course:
        folders = course_folders(lib, course)
        if folders:
            sql += " AND (" + " OR ".join("path LIKE ?" for _ in folders) + ")"
            params += [_like_prefix(f) for f in folders]
        else:
            sql += " AND (course LIKE ? OR path LIKE ?)"
            params += [f"%{course}%", f"%{course}%"]
    if kind:
        kinds = KIND_ALIASES.get(kind.lower(), [kind.lower()])
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        params += kinds
    rows = conn.execute(sql + " ORDER BY path", params).fetchall()
    conn.close()
    return [{"path": r[0], "course": r[1], "kind": r[2], "title": r[3]} for r in rows]


BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".mp4", ".mov",
              ".m4v", ".mp3", ".m4a", ".wav", ".aac", ".webm", ".zip", ".pdf", ".pptx", ".docx", ".xlsx"}
MAX_NEIGHBORS = 3


def read_document(lib, rel_path: str, locator: str = "", start: int = 0, max_chars: int = 16000,
                  neighbors: int = 0, lean: bool = False) -> str:
    """Return a document (or just one located section) from the library as text. `lean` gives the connector's
    reading view (same content, without repeated boilerplate); paging offsets then refer to that view, or to
    the located sections when there's a locator."""
    path = lib.resolve(rel_path)
    if path is None:
        raise FileNotFoundError("That path is outside the library.")
    if path.suffix.lower() != ".md":
        sidecar = path.with_name(path.name + ".md")
        if sidecar.exists():
            path = sidecar
    if not path.exists():
        raise FileNotFoundError(f"Not found: {rel_path}")
    root = lib.root.resolve()
    if root not in path.resolve().parents:          # e.g. a text version that's a link to somewhere else
        raise FileNotFoundError("That path is outside the library.")
    if path.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir() if not p.name.startswith("."))
        return "Folder contents:\n" + "\n".join(entries)
    if path.suffix.lower() != ".md" and (path.suffix.lower() in BINARY_EXT or not _looks_like_text(path)):
        raise FileNotFoundError(f"{path.name} is a picture, recording or other file without a text version here. "
                                "Use view_page to look at pictures, slides and PDF pages.")
    text = path.read_text(encoding="utf-8", errors="replace")
    if lean:
        from .compact import compact

        text = compact(text)
    if locator:
        meta, body = parse_front_matter(text)
        sections = _split_sections(body)
        names = unique_locators([loc for loc, _, _ in sections])
        keys = [_norm_loc(n) for n in names]
        want = _norm_loc(heading_locator(locator))
        idx = next((i for i, k in enumerate(keys) if k == want), None)
        if idx is None and want:
            pattern = re.compile(re.escape(want) + r"(?![\w(])")     # 'Slide 7' opens 'Slide 7 Title', not 'Slide 70'
            idx = next((i for i, k in enumerate(keys) if pattern.match(k)), None)
        if idx is None and want:
            pattern = re.compile(r"(?<!\w)" + re.escape(want) + r"(?!\w)")
            idx = next((i for i, k in enumerate(keys) if pattern.search(k)), None)
        if idx is None:
            options = ", ".join(n for n in names[:40] if n)
            return f"No section '{locator}' in {rel_path}. Sections include: {options}"
        around = min(max(int(neighbors or 0), 0), MAX_NEIGHBORS)
        lo, hi = max(0, idx - around), min(len(sections), idx + around + 1)
        level = sections[idx][2]
        if level and not sections[idx][1].strip():   # an empty parent heading ('Chapter 1'): include what's under it
            while hi < len(sections) and sections[hi][2] > level and hi - idx < 60:
                hi += 1
        head = f"{meta.get('title') or path.stem} ({path.relative_to(root).as_posix()})\n\n"
        text = head + "\n\n".join((f"## {names[i]}\n\n" if names[i] else "") + sections[i][1].strip()
                                  for i in range(lo, hi))
    if start and start >= len(text):
        return f"[Nothing more: start={start} is past the end ({len(text)} characters).]"
    chunk = text[start:start + max_chars]
    if start + max_chars < len(text):
        chunk += f"\n\n[… {len(text) - start - max_chars} more characters. Read on with start={start + max_chars}.]"
    return chunk


def _looks_like_text(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        return exc.start > len(head) - 4      # only a letter cut off at the edge of the sample


def _norm_loc(value: str) -> str:
    value = (value or "").lower().strip().strip("[]#").strip()
    value = re.sub(r"^\[([^\]]+)\]", r"\1", value)
    return re.sub(r"\s+", " ", value.replace("[", "").replace("]", "")).strip()


def _split_sections(body: str) -> List[Tuple[str, str, int]]:
    """[(locator, text, heading level)]. Empty sections are kept, so a parent heading can be opened and repeated
    headings are numbered the same way the search index numbers them."""
    sections: List[Tuple[str, str, int]] = []
    loc, level = "", 0
    buf: List[str] = []
    for line in body.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            if loc or "\n".join(buf).strip():
                sections.append((loc, "\n".join(buf), level))
            loc, level = heading_locator(m.group(2)), len(m.group(1))
            buf = []
        else:
            buf.append(line)
    if loc or "\n".join(buf).strip():
        sections.append((loc, "\n".join(buf), level))
    return sections
