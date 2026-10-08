"""Full-text search over the whole library (SQLite FTS5, BM25 ranking, English stemming).

Every Markdown file is split at its locator headings ([Page 12], [Slide 7], [00:32:10],
section titles), so each hit can be cited precisely and read in full afterwards.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .util import (REMOVED_DIR, as_one_action, cached_digest, front_matter_view, parse_front_matter, settled,
                   stat_signature)

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
CREATE TABLE IF NOT EXISTS docs(path TEXT PRIMARY KEY, course TEXT, kind TEXT, title TEXT, mtime REAL, size INTEGER,
                               source_version TEXT, source_stat TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    text, title, locator UNINDEXED, path UNINDEXED, course UNINDEXED, kind UNINDEXED, chunk UNINDEXED,
    doc_title UNINDEXED, tokenize = 'porter unicode61 remove_diacritics 2'
);
"""


def connect(db_path: Path, timeout: float = 30) -> sqlite3.Connection:
    """A connection for updating the index. `timeout` is how long to wait while another process writes."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    if not row or row[0] != str(INDEX_VERSION):
        conn.executescript("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS docs;")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('version', ?)", (str(INDEX_VERSION),))
        conn.commit()
    columns = {r[1] for r in conn.execute("PRAGMA table_info(docs)")}
    for column in ("source_version", "source_stat"):
        if column not in columns:
            conn.execute(f"ALTER TABLE docs ADD COLUMN {column} TEXT")
            conn.commit()
    return conn


def _index_paths(lib) -> Path:
    for suffix in ("", "-wal", "-shm", "-journal"):
        lib.checked(Path(str(lib.index_path) + suffix))
    return lib.checked(lib.index_path)


def _connect(lib, timeout: float = 30) -> sqlite3.Connection:
    return connect(_index_paths(lib), timeout=timeout)


def _reader(lib) -> Optional[sqlite3.Connection]:
    """A connection that only reads. Searching and listing never change the index, so they never wait for
    (or hold up) a sync that is updating it: readers see the last saved version while a sync writes."""
    path = _index_paths(lib)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path), timeout=5)
    try:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    except sqlite3.DatabaseError:
        conn.close()
        return None
    if not row or row[0] != str(INDEX_VERSION):      # an older index: the next sync rebuilds it
        conn.close()
        return None
    return conn




def _described_original(lib, sidecar: Path, descriptions: Dict[str, Any]) -> Tuple[bool, Optional[Path]]:
    """(whether the text version's original has saved picture descriptions, the original if it exists)."""
    original_rel = lib.checked(sidecar).relative_to(lib.root).as_posix()[:-3]
    original = lib.resolve(original_rel)
    if original is not None:
        original_rel = original.relative_to(lib.root).as_posix()
    return original_rel in descriptions, original


def _source_version(original: Optional[Path], previous: Tuple[str, str] = ("", ""), lib=None) -> Tuple[str, str]:
    """(digest, signature) of a described original. A digest recorded with the same signature is reused: the
    file hasn't been written since it was read (see util.settled). A signature is only recorded if the file had
    settled when it was read, so a file written moments ago is checked again next time."""
    if original is None or not original.is_file():
        return "missing", ""
    signature = stat_signature(original)
    if signature is None:
        return "unavailable", ""
    text = ":".join(str(part) for part in signature)
    ready = settled(signature)                         # judged before reading
    if ready and previous[0] and previous[1] == text and previous[0] not in ("missing", "unavailable"):
        return previous[0], text
    try:
        digest = lib.digest(original) if lib is not None else cached_digest(original)
    except OSError:
        return "unavailable", ""
    if stat_signature(original) != signature:          # written while it was read: don't trust either
        return "unavailable", ""
    return digest, (text if ready else "")


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


def update_index(lib, log: Optional[Callable[[str], None]] = None, full: bool = False,
                 wait: float = 30) -> Dict[str, int]:
    """Bring the index up to date with the library. `wait` is how long to wait for a sync that is updating it
    at the same time; a quick refresh after saving notes passes a short wait and skips if the index is busy."""
    from .compact import for_index
    from . import describe

    conn = _connect(lib, timeout=wait)
    descriptions = describe.load_view(lib)
    row = conn.execute("SELECT value FROM meta WHERE key = 'text_version'").fetchone()
    if not row or row[0] != TEXT_VERSION:
        full = True
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('text_version', ?)", (TEXT_VERSION,))
    known = {row[0]: (row[1], row[2], row[3] or "", row[4] or "")
             for row in conn.execute("SELECT path, mtime, size, source_version, source_stat FROM docs")}
    present = set()
    added = updated = removed = 0
    with conn:
        for path in iter_markdown(lib.root):
            rel = path.relative_to(lib.root).as_posix()
            present.add(rel)
            try:
                resolved = lib.checked(path)
                st = path.stat()
                # Picture descriptions are tied to the original's exact bytes, which can change without the text
                # version changing: record which original the indexed descriptions were checked against.
                source_version = source_stat = ""
                described, original = _described_original(lib, resolved, descriptions)
                if described:
                    previous = ("", "") if full else known.get(rel, (0, 0, "", ""))[2:]
                    source_version, source_stat = _source_version(original, previous, lib)
            except (OSError, ValueError):
                continue
            if not full and rel in known and known[rel][:3] == (st.st_mtime, st.st_size, source_version):
                if known[rel][3] != source_stat:
                    conn.execute("UPDATE docs SET source_stat = ? WHERE path = ?", (source_stat, rel))
                continue
            try:
                text = describe.reading_text(lib, path, path.read_text(encoding="utf-8"))
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
            conn.execute("INSERT OR REPLACE INTO docs(path, course, kind, title, mtime, size, source_version, source_stat) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (rel, course, kind, title, st.st_mtime, st.st_size, source_version, source_stat))
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


@as_one_action
def search(lib, query: str, course: str = "", kind: str = "", limit: int = 10, per_doc: int = 3,
           markers: tuple = ("**", "**"), alternate_queries: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Search literal course text, optionally combining up to four alternate phrasings.

    Alternates are supplied by the caller; this is not embedding or semantic search. Each phrasing gets its
    own candidate pool before reciprocal-rank fusion, so a broad partial match cannot fill the result limit
    before a useful alternate is searched. Complete-term matches rank before partial matches, with the
    existing current-before-removed ordering and per-document cap applied after fusion.

    Search only reads the index, so it never waits for (or holds up) a sync that is updating it. A document
    that changed after it was indexed, including an original whose saved picture descriptions no longer
    match it, is checked against its current reading text: the hit is kept, with a fresh snippet, only if
    that section still matches.
    """
    if alternate_queries is not None and (not isinstance(alternate_queries, list) or len(alternate_queries) > 4 or
            any(not isinstance(q, str) or not q.strip() or len(q) > 512 for q in alternate_queries)):
        raise ValueError("Provide at most four nonempty alternate queries, each at most 512 characters.")
    if limit <= 0 or per_doc <= 0:
        return []
    if not lib.index_path.exists():
        return []
    from . import describe
    query_terms = []
    seen_terms = set()
    for q in [query] + (alternate_queries or []):
        terms, _ = _terms(q)
        key = tuple(sorted(t.casefold() for t in terms))
        if terms and key not in seen_terms:
            query_terms.append((q, terms))
            seen_terms.add(key)
    if not query_terms:
        return []
    conn = _reader(lib)
    if conn is None:
        return []
    descriptions = describe.load_view(lib)
    current_text = _CurrentText(lib, markers)
    state = lib.state_view()
    filters = []
    params: List[Any] = []
    if course:
        folders = course_folders(lib, course)
        if folders:
            filters.append("(" + " OR ".join("path LIKE ? ESCAPE '\\'" for _ in folders) + ")")
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
    candidates: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    candidate_limit = min(800, max(32, limit * 8))
    results: List[Dict[str, Any]] = []
    try:
        for phrasing, fts in query_terms:
            strategies = [(" AND ".join(fts), True)]
            if len(fts) > 1:
                strategies.append((" OR ".join(fts), False))
            query_seen = set()
            for match, complete in strategies:
                try:
                    rows = conn.execute(sql, [markers[0], markers[1], match] + params + [candidate_limit]).fetchall()
                except sqlite3.OperationalError:
                    continue
                for rank, (path, title, locator, crs, knd, snip, score, chunk) in enumerate(rows, 1):
                    key = (path, locator, chunk)
                    if key in query_seen:
                        continue
                    query_seen.add(key)
                    hit = candidates.get(key)
                    if hit is None:
                        hit = {"path": path, "title": title, "locator": locator, "course": crs, "kind": knd,
                               "snippet": re.sub(r"\s+", " ", snip).strip(), "score": round(-score, 3),
                               "match": "all words" if complete else "some words", "removed": is_removed(path),
                               "retrieval_score": 0.0, "matched_queries": [], "_matches": set()}
                        candidates[key] = hit
                    elif complete and hit["match"] != "all words":
                        hit.update(match="all words", snippet=re.sub(r"\s+", " ", snip).strip(),
                                   score=round(-score, 3))
                    hit["retrieval_score"] += (1.0 if complete else 0.35) / (60 + rank)
                    hit["matched_queries"].append(phrasing)
                    hit["_matches"].add((match, complete))
        # What's currently in the course comes first; copies of things removed from Canvas come after, labeled.
        ranked = sorted(candidates.values(), key=lambda r: (r["removed"], r["match"] != "all words",
                                                           -r["retrieval_score"]))
        entries = _IndexEntries(conn)
        status: Dict[str, Tuple[Optional[Path], bool]] = {}     # path -> (current text version, index current)
        per_path: Dict[str, int] = {}
        for hit in ranked:
            path = hit["path"]
            if per_path.get(path, 0) >= per_doc:
                continue
            if path not in status:
                sidecar = lib.resolve(path)
                if sidecar is None or not sidecar.is_file():
                    status[path] = (None, False)
                else:
                    try:
                        status[path] = (sidecar, _entry_current(lib, sidecar, entries.get(path), descriptions))
                    except (OSError, ValueError):
                        status[path] = (None, False)
            sidecar, fresh = status[path]
            if sidecar is None or (not fresh and not current_text.matches(sidecar, path, hit)):
                continue
            per_path[path] = per_path.get(path, 0) + 1
            hit.pop("_matches", None)
            hit["retrieval_score"] = round(hit["retrieval_score"], 6)
            hit.update(material_status(lib, path, state))
            results.append(hit)
            if len(results) >= limit:
                break
    finally:
        conn.close()
        current_text.close()
    return results


class _IndexEntries:
    """What the index recorded about each document when it was last indexed."""

    def __init__(self, conn: sqlite3.Connection):
        columns = {r[1] for r in conn.execute("PRAGMA table_info(docs)")}
        version = "source_version" if "source_version" in columns else "''"
        stat = "source_stat" if "source_stat" in columns else "''"
        self.sql = f"SELECT mtime, size, {version}, {stat} FROM docs WHERE path = ?"
        self.conn = conn

    def get(self, path: str) -> Optional[Tuple[float, int, str, str]]:
        row = self.conn.execute(self.sql, (path,)).fetchone()
        return (row[0], row[1], row[2] or "", row[3] or "") if row else None


def _entry_current(lib, sidecar: Path, entry, descriptions: Dict[str, Any]) -> bool:
    """Whether the indexed copy still matches the files: the text version (size and time) and, for a document
    with saved picture descriptions, the original those descriptions were checked against when indexed."""
    if entry is None:
        return False
    st = sidecar.stat()
    if (st.st_mtime, st.st_size) != (entry[0], entry[1]):
        return False
    described, original = _described_original(lib, sidecar, descriptions)
    recorded = entry[2]
    if not described:
        return not recorded                  # descriptions indexed for it no longer apply
    return bool(recorded) and _source_version(original, (recorded, entry[3]), lib)[0] == recorded


class _CurrentText:
    """The current reading text of documents whose index entry is behind the files (obsolete picture
    descriptions left out, as when reading), searchable the same way as the index."""

    def __init__(self, lib, markers: tuple):
        self.lib, self.markers = lib, markers
        self.tables: Dict[str, Optional[sqlite3.Connection]] = {}

    def _table(self, sidecar: Path, path: str) -> Optional[sqlite3.Connection]:
        if path in self.tables:
            return self.tables[path]
        from . import describe
        from .compact import for_index
        table = None
        try:
            text = describe.reading_text(self.lib, sidecar, sidecar.read_text(encoding="utf-8"))
            meta, body = parse_front_matter(text)
            if meta.get("type") != "outline":
                title = meta.get("title") or Path(path).stem
                table = sqlite3.connect(":memory:")
                table.execute("CREATE VIRTUAL TABLE t USING fts5(text, title, locator UNINDEXED, chunk UNINDEXED, "
                              "tokenize = 'porter unicode61 remove_diacritics 2')")
                table.executemany("INSERT INTO t(text, title, locator, chunk) VALUES (?,?,?,?)",
                                  [(chunk, title if i == 0 else "", loc, i)
                                   for i, (loc, chunk) in enumerate(chunk_markdown(for_index(body)))])
        except (OSError, UnicodeDecodeError, ValueError, sqlite3.Error):
            table = None
        self.tables[path] = table
        return table

    def matches(self, sidecar: Path, path: str, hit: Dict[str, Any]) -> bool:
        table = self._table(sidecar, path)
        if table is None:
            return False
        for match, complete in sorted(hit.get("_matches") or (), key=lambda m: not m[1]):
            try:
                row = table.execute("SELECT snippet(t, 0, ?, ?, ' … ', 48) FROM t WHERE t MATCH ? AND locator = ? "
                                    "ORDER BY bm25(t, 1.0, 3.0) LIMIT 1",
                                    (self.markers[0], self.markers[1], match, hit["locator"])).fetchone()
            except sqlite3.OperationalError:
                continue
            if row:
                hit.update(snippet=re.sub(r"\s+", " ", row[0]).strip(), match="all words" if complete else "some words")
                return True
        return False

    def close(self) -> None:
        for table in self.tables.values():
            if table is not None:
                table.close()


def is_removed(path: str) -> bool:
    return f"/{REMOVED_DIR}/" in f"/{path}"


_LOOKUPS: "OrderedDict[int, tuple]" = OrderedDict()     # id(course record view) -> (view, paths, pictures)
_LOOKUPS_LOCK = threading.Lock()


def _build_lookup(state) -> Tuple[Dict[str, list], Dict[str, list]]:
    """Every record item by its paths, and by its pictures folder: path -> [(course position, item position,
    item)] in record order."""
    paths_of: Dict[str, list] = {}
    assets_of: Dict[str, list] = {}
    for c_index, course in enumerate(state.get("courses", {}).values()):
        folder = course.get("folder") or ""
        for i_index, item in enumerate((course.get("items") or {}).values()):
            paths = {folder + "/" + p for p in (item.get("path"), item.get("text")) if p}
            paths.update(p + ".md" for p in list(paths) if not p.endswith(".md"))
            entry = (c_index, i_index, item)
            for path in paths:
                paths_of.setdefault(path, []).append(entry)
            for prefix in {p[:-3] + ".assets/" if p.endswith(".md") else p + ".assets/" for p in paths}:
                assets_of.setdefault(prefix, []).append(entry)
    return paths_of, assets_of


def _view_lookup(view) -> Tuple[Dict[str, list], Dict[str, list]]:
    """The lookup for the shared course record view, built once per version of the record."""
    with _LOOKUPS_LOCK:
        hit = _LOOKUPS.get(id(view))
        if hit is not None and hit[0] is view:
            return hit[1], hit[2]
    lookup = _build_lookup(view)
    with _LOOKUPS_LOCK:
        _LOOKUPS[id(view)] = (view, *lookup)       # holding the view keeps its id from being reused
        while len(_LOOKUPS) > 4:
            _LOOKUPS.popitem(last=False)
    return lookup


def _record_items(lib, rel: str, state=None) -> List[Dict[str, Any]]:
    """The record items describing a library path (the file, its text version or a picture saved from it): in
    each course, the first matching item, in record order."""
    view = lib.state_view()
    paths_of, assets_of = _view_lookup(view) if state is None or state is view else _build_lookup(state)
    matches = list(paths_of.get(rel, ()))
    at = rel.find(".assets/")
    while at >= 0:                       # a path inside a pictures folder belongs to the file the folder is for
        matches.extend(assets_of.get(rel[:at + len(".assets/")], ()))
        at = rel.find(".assets/", at + 1)
    first: Dict[int, Tuple[int, Dict[str, Any]]] = {}
    for c_index, i_index, item in matches:
        if c_index not in first or i_index < first[c_index][0]:
            first[c_index] = (i_index, item)
    return [first[c_index][1] for c_index in sorted(first)]


def material_status(lib, rel: str, state=None) -> Dict[str, Any]:
    """Carry known source availability into every reading surface, including saved notes. Without `state`, the
    shared course record view is used, looked up through a table built once per version of the record."""
    result = {"status": "current", "stale": False, "last_successful_sync": "",
              "last_successful_version": "", "message": ""}
    if is_removed(rel):
        return {**result, "status": "removed", "stale": True,
                "message": "Historical copy: removed from Canvas; check current course material."}
    sidecar_rel = rel if rel.endswith(".md") else rel + ".md"
    if ".assets/" in rel:
        sidecar_rel = rel.split(".assets/", 1)[0] + ".md"
    for item in _record_items(lib, rel, state):
        result.update(last_successful_sync=item.get("last_successful_sync") or "",
                      last_successful_version=item.get("successful_stamp") or item.get("stamp") or "")
        status = item.get("status")
        if status == "locked" or item.get("locked"):
            result.update(status="restricted", stale=True,
                          message="Historical copy: Canvas now marks this file locked or restricted. Do not treat it as current.")
        elif status in ("failed", "too_large", "pending") or item.get("stale"):
            result.update(status="stale", stale=True,
                          message="This source is not up to date: its latest download failed or is pending. Check Canvas before relying on it.")
    sidecar = lib.resolve(sidecar_rel)
    if sidecar is not None and sidecar.is_file():
        marker = front_matter_view(sidecar).get("sync_status")
        if marker == "restricted":
            result.update(status="restricted", stale=True,
                          message="Historical copy: Canvas now marks this source locked or restricted. Do not treat it as current.")
        elif marker == "stale" and result["status"] == "current":
            result.update(status="stale", stale=True,
                          message="This source is not up to date: its latest update did not complete. Check Canvas before relying on it.")
    if "/Study Notes/" in "/" + rel:
        from . import notes
        from .outline import course_documents
        store = notes.load_view(lib)
        for source, entry in store.items():
            if entry.get("file") != rel:
                continue
            if entry.get("kind") == "document":
                sidecar = lib.resolve(source if source.endswith(".md") else source + ".md")
                changed = sidecar is None or not sidecar.is_file() or entry.get("fp") != notes.fingerprint(sidecar, lib)
            else:
                target = lib.resolve(source)
                found = notes._course_of(lib, target) if target else None
                docs = course_documents(lib, found[0], include_light=True) if found else []
                if entry.get("kind") == "module" and found:
                    section = target.relative_to(found[0]).as_posix()
                    docs = [d for d in docs if d.section == section]
                changed = not found or notes._summary_status(store, source, docs) != "done"
            if changed:
                result.update(status="stale", stale=True,
                              message="These study notes need review: a source changed, disappeared, is unavailable, or has newer notes. Check the current sources before relying on them.")
            break
    return result


def freshness_warning(lib, rel: str) -> str:
    status = material_status(lib, rel)
    if not status["message"]:
        return ""
    when = status["last_successful_sync"]
    return "> **Source warning:** " + status["message"] + (f" Last successful retrieval: {when}." if when else "") + "\n\n"


def _simple(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).strip()


def course_folders(lib, course: str) -> List[str]:
    """Course folders whose name, code, Canvas id or folder matches (punctuation and case ignored)."""
    want = _simple(course)
    if not want:
        return []
    registered = lib.state_view().get("courses", {})
    exact = [c["folder"] for c in registered.values() if c.get("folder") == course]
    if exact:
        return list(dict.fromkeys(exact))
    out = []
    for cid, c in registered.items():
        folder = c.get("folder") or ""
        if folder and want in _simple(f"{cid} {c.get('name', '')} {c.get('code', '')} {folder}"):
            out.append(folder)
    return out


def _like_prefix(folder: str) -> str:
    return folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"


@as_one_action
def list_documents(lib, course: str = "", kind: str = "") -> List[Dict[str, Any]]:
    if not lib.index_path.exists():
        return []
    conn = _reader(lib)
    if conn is None:
        return []
    sql = "SELECT path, course, kind, title FROM docs WHERE 1=1"
    params: List[Any] = []
    if course:
        folders = course_folders(lib, course)
        if folders:
            sql += " AND (" + " OR ".join("path LIKE ? ESCAPE '\\'" for _ in folders) + ")"
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
    return [{"path": r[0], "course": r[1], "kind": r[2], "title": r[3], **material_status(lib, r[0])}
            for r in rows if lib.resolve(r[0]) is not None]


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
    from .describe import reading_text
    text = reading_text(lib, path, text)
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
    return freshness_warning(lib, path.relative_to(root).as_posix()) + chunk


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
