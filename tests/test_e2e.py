"""End-to-end test: sync two fake courses, check every output, sync again, search, render, MCP.

Run:  python tests/test_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

WORK = Path(os.environ.get("SS_TEST_DIR") or tempfile.mkdtemp(prefix="ss-e2e-"))
shutil.rmtree(WORK / "app", ignore_errors=True)
shutil.rmtree(WORK / "lib", ignore_errors=True)
os.environ["SUPERSTUDENT_HOME"] = str(WORK / "app")
os.environ["SUPERSTUDENT_LIBRARY"] = str(WORK / "lib")
os.environ["CANVAS_TOKEN"] = "test-token-123"

from fake_canvas import FakeCanvas  # noqa: E402
from make_fixtures import build_all  # noqa: E402

PASSED = []


def check(cond, label, detail=""):
    if not cond:
        raise AssertionError(f"FAILED: {label} {detail}")
    PASSED.append(label)
    print(f"  ok  {label}")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def main() -> None:
    fixtures = build_all(WORK / "fixtures")
    fake = FakeCanvas(fixtures)
    base = fake.start()
    os.environ["SUPERSTUDENT_CANVAS_URL"] = base

    import superstudent.media as media
    from superstudent.media import Segment

    # No speech model can be downloaded here, so stand in for Whisper.
    media.detect_backend = lambda preference="auto": "faster-whisper"
    calls = []

    def fake_transcribe(self, path, prompt=""):
        calls.append((str(path), prompt))
        return [Segment(0.5, 6.0, "Welcome back. Today is all about modified duration."),
                Segment(12.0, 18.0, "Make sure you know the convexity adjustment, it is very important."),
                Segment(21.0, 28.0, "Here is the worked example on the board.")]

    media.Transcriber.transcribe = fake_transcribe

    from superstudent.cli import main as cli

    print("\n== First sync")
    t0 = time.time()
    rc = cli(["sync"])
    check(rc == 0, "first sync exits cleanly", rc)
    print(f"  (took {time.time() - t0:.1f}s)")
    lib = WORK / "lib"
    fin = lib / "Fall 2026" / "FIN 6100 - Fixed Income"
    econ = lib / "Fall 2026" / "ECON 5200 - Econometrics"
    check(fin.is_dir() and econ.is_dir(), "course folders by term and code")
    check(not any("303" in p.name for p in (lib / "Fall 2026").iterdir()), "date-restricted course skipped")
    for name in ("COURSE_OVERVIEW.md", "EXAM_INTEL.md", "CALENDAR.md", "GRADES.md", "LINKS.md", "Syllabus.md"):
        check((fin / name).exists(), f"{name} written")
    check((lib / "INDEX.md").exists() and (lib / "CLAUDE.md").exists(), "library INDEX.md and CLAUDE.md")
    check((lib / ".claude/skills/super-student/SKILL.md").exists(), "skill installed in library")
    check((lib / "AGENTS.md").exists() and (lib / ".agents/skills/super-student/SKILL.md").exists(),
          "AGENTS.md and skill for ChatGPT/Codex in library")

    m1 = fin / "Modules" / "01 - Week 1 - Bond Basics"
    m2 = fin / "Modules" / "02 - Week 2 - Duration"
    deck = m1 / "Lecture 1 - Bond Basics.pptx"
    check(deck.exists() and Path(str(deck) + ".md").exists(), "module file downloaded with text version")
    deck_md = read(Path(str(deck) + ".md"))
    check("## [Slide 1] Price-Yield Relationship" in deck_md, "slides follow presentation order, not file order")
    check("Speaker notes:" in deck_md and "emphasize this for the midterm" in deck_md, "speaker notes on the right slide")
    check("Chart (line)" in deck_md and "| 10Y |" in deck_md, "chart data extracted as a table")
    assets = Path(str(deck) + ".assets")
    check(assets.is_dir() and any(assets.glob("slide-*.png")), "slide picture saved for viewing")
    check("> Visual content:" in deck_md, "visual slides flagged")

    pdf_md = read(m2 / "Duration Notes.pdf.md")
    check("## [Page 1]" in pdf_md and "## [Page 3]" in pdf_md, "PDF split by page")
    check("equations" in pdf_md and "chart or diagram" in pdf_md, "PDF chart/equation page flagged visual")
    check("no text layer" in pdf_md, "image-only page flagged")
    xlsx_md = read(m2 / "Bond Model.xlsx.md")
    check("G2: `=SUM(D2:D6)`" in xlsx_md and "| Row | A |" in xlsx_md, "spreadsheet keeps cell addresses and formulas")

    rec = m2 / "Week 2 Lecture Recording.mp4.md"
    check(rec.exists(), "lecture video transcribed")
    rec_md = read(rec)
    check("## [00:00:00]" in rec_md and "modified duration" in rec_md, "transcript has timestamps")
    check("Screen at" in rec_md, "screen snapshots pinned into transcript", rec_md[:600])
    frames = list(Path(str(m2 / "Week 2 Lecture Recording.mp4") + ".assets").glob("screen-*.jpg"))
    check(len(frames) >= 2, f"{len(frames)} screen snapshots saved")
    check(not (m2 / "Week 2 Lecture Recording.mp4").exists(), "video removed after transcription (keep_media_files off)")
    check(any("Fixed Income" in c[1] for c in calls), "course vocabulary passed to Whisper")

    embedded = list(m2.glob("*(lecture video).md"))
    check(len(embedded) == 1, "embedded Canvas video gets its own transcript", [p.name for p in m2.iterdir()])
    emb_md = read(embedded[0])
    check("This will be on the midterm" in emb_md and "Canvas captions" in emb_md, "captions used before Whisper")

    trap = fin / "Files" / "Exams" / "Login trap.pdf"
    check(not trap.exists(), "sign-in page not saved as a PDF")
    check("sign-in page" in read(Path(str(trap) + ".md")), "sign-in failure explained")
    check("Not downloaded" in read(fin / "Files" / "Exams" / "Locked answers.pdf.md"), "locked file explained")
    check((fin / "Files" / "Exams" / "Old exam 2025.pdf").exists(), "non-module files mirror Canvas folders")
    check(fake.token_on_storage == 0 and fake.storage_hits,
          f"token never sent to the file-storage host ({fake.token_on_storage} of {len(fake.storage_hits)})")

    syl = read(fin / "Syllabus.md")
    check("$P=\\sum_{t=1}^{T} \\frac{C}{(1+y)^t}$" in syl, "equation editor math kept as LaTeX")
    check("Files/Problem%20Set%20Handout.docx" in syl, "syllabus link rewritten to local file", syl[-400:])
    guide = read(m1 / "Reading Guide Week 1.md")
    check("../02%20-%20Week%202%20-%20Duration/Week%202%20Recording.md" in guide, "page-to-page link made local")
    check("investopedia" in read(fin / "LINKS.md").lower(), "outside links collected")
    links = read(fin / "LINKS.md")
    check("Pearson MyLab" in links and "Panopto" in links, "unreachable tools listed honestly")
    check("Case Study (MyLab)" in links, "assignments in outside tools listed")

    ps1 = read(fin / "Assignments" / "Problem Set 1.md")
    check("## Rubric" in ps1 and "Show the discounting steps." in ps1 and "Watch the compounding frequency." in ps1,
          "assignment has rubric, rubric comments and instructor comments")
    check((fin / "Assignments" / "My Submissions" / "Problem Set 1" / "PS1 Marc.docx").exists(), "my submitted file saved")
    grades = read(fin / "GRADES.md")
    check("Problem Set 1" in grades and "90.0%" in grades and "Where points were lost" in grades, "grades summary")
    intel = read(fin / "EXAM_INTEL.md")
    for needle in ("cumulative", "This will be on the midterm", "Exam tip", "Make sure you know the convexity",
                   "Midterm Exam"):
        check(needle in intel, f"exam intel includes '{needle}'")
    check("This will be on the exam, so practice it" in intel.split("From discussions")[-1], "instructor discussion reply in exam intel")
    check("Grade category" not in intel and "Original file:" not in intel, "no metadata noise in exam intel")
    check("- Sun Sep 20, 2026: \"The midterm" in intel, "announcement hints dated", intel[intel.find("From announcements"):][:300])
    disc = read(fin / "Discussions" / "Duration intuition.md")
    check("(Instructor)" in disc, "instructor replies labeled (after a 503 retry)")
    check("only shows replies after you post" in read(fin / "Discussions" / "Introduce yourself.md"), "post-first discussion explained")
    ann = list((fin / "Announcements").glob("2026-09-20 - Midterm review session.md"))
    check(ann, "announcement saved with date prefix")
    cal = read(fin / "CALENDAR.md")
    check("Midterm Exam" in cal and "Room 101" in cal, "calendar events")
    overview = read(fin / "COURSE_OVERVIEW.md")
    check("Prof. Rivera" in overview and "Week 2 - Duration" in overview and "Not in this library" in overview,
          "overview has instructor, outline and gaps")
    check("**Documents:** 2 PDFs" in overview and "**Lectures and videos:** 2 transcribed" in overview
          and "**Not downloaded:** 2 files" in overview, "folder inventory", overview[overview.find("## What's"):][:500])
    check(oct(lib.stat().st_mode & 0o777) == "0o700", "library folder private to this account")
    check("_Module%20Contents.md" in overview, "overview links module maps")
    mc = read(m2 / "_Module Contents.md")
    check("Duration%20Notes.pdf" in mc and "[text](" in mc, "module map links originals and text versions")

    # Hidden Files/Pages tabs: module content still arrives, no scary warnings.
    unit = econ / "Modules" / "01 - Unit 1 Regression"
    check((unit / "Duration Notes.pdf").exists(), "hidden Files tab: module file still synced")
    check((unit / "OLS assumptions.md").exists(), "hidden Pages tab: module page still synced")
    last = json.loads((lib / ".superstudent" / "last_sync.json").read_text())
    econ_warn = [c for c in last["courses"] if "ECON" in c["name"]][0]["warnings"]
    check(not econ_warn, f"hidden tabs produce no warnings {econ_warn}")
    check("Know all five assumptions" in read(econ / "EXAM_INTEL.md"), "exam intel from pages")

    print("\n== Search, read, render")
    from superstudent.index import read_document, search
    from superstudent.library import Library
    from superstudent.render import render

    L = Library(lib)
    hits = search(L, "modified duration")
    kinds = {h["kind"] for h in hits}
    check(hits and "pdf" in kinds and "transcript" in kinds, f"search finds PDF and lecture hits {kinds}")
    check(all(h["locator"] for h in hits[:3]), "hits carry locators")
    hits = search(L, "convexity", kind="lecture")
    check(hits and all(h["kind"] == "transcript" for h in hits), "kind filter (lecture -> transcripts)")
    hits = search(L, "assumptions", course="ECON")
    check(hits and all("ECON" in h["path"] for h in hits), "course filter")
    sec = read_document(L, str(deck.relative_to(lib)), locator="Slide 1")
    check("Price-Yield Relationship" in sec and "Slide 2" not in sec, "read one slide by locator (original path)")
    pngs = render(L, str((m2 / "Duration Notes.pdf").relative_to(lib)), page=2)
    check(pngs and pngs[0].exists() and pngs[0].stat().st_size > 1000, "render PDF page to PNG")
    pngs = render(L, str(deck.relative_to(lib)), page=2)
    check(pngs and pngs[0].suffix == ".png", "slide without LibreOffice falls back to saved slide images")

    print("\n== Second sync (incremental)")
    fake.extra_announcement = True
    before = {p: p.stat().st_mtime for p in lib.rglob("*.pdf")}
    rc = cli(["sync", "--quiet"])
    last = json.loads((lib / ".superstudent" / "last_sync.json").read_text())
    fin_stats = [c for c in last["courses"] if "FIN" in c["name"]][0]["stats"]
    check(rc == 0 and fin_stats.get("files_downloaded", 0) == 0, f"nothing re-downloaded {fin_stats}")
    check(all(p.stat().st_mtime == t for p, t in before.items() if p.exists()), "files untouched")
    check(list((fin / "Announcements").glob("*Formula sheet posted.md")), "new announcement picked up")
    check(len(calls) == 1, "lecture not transcribed twice")

    print("\n== Offline speech model: lectures wait instead of failing")
    from superstudent.media import TranscriberUnavailable

    def offline(self, path, prompt=""):
        raise TranscriberUnavailable("couldn't load the speech model (ConnectionError)")

    media.Transcriber.transcribe = offline
    fake.video_reuploaded = True
    rc = cli(["sync", "--quiet"])
    state = json.loads((lib / ".superstudent" / "state.json").read_text())
    item = state["courses"]["101"]["items"]["file:1004"]
    check(rc == 0 and item["status"] == "ok" and "welcome back" in read(rec).lower(), "old transcript kept while the model is unavailable")
    check(not item.get("attempts"), "no failed attempt counted for an offline model")
    media.Transcriber.transcribe = fake_transcribe
    rc = cli(["sync", "--quiet"])
    state = json.loads((lib / ".superstudent" / "state.json").read_text())
    check(rc == 0 and len(calls) == 2 and state["courses"]["101"]["items"]["file:1004"]["stamp"].startswith("2026-09-02"),
          "re-uploaded lecture transcribed once the model is back")

    print("\n== Claude connector (MCP over stdio)")
    asyncio.run(mcp_check(lib))

    print("\n== Exam pack")
    rc = cli(["pack", "--course", "FIN 6100", "--modules", "2"])
    pack = lib / "_Exam Packs" / "FIN 6100 - Fixed Income - Modules 2"
    check(rc == 0 and (pack / "02 - Week 2 - Duration" / "Duration Notes.pdf").exists() and (pack / "EXAM_INTEL.md").exists(),
          "exam pack bundles originals + intel")

    fake.stop()
    print(f"\nALL {len(PASSED)} CHECKS PASSED  (work dir: {WORK})")


async def mcp_check(lib: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    params = StdioServerParameters(command=os.environ.get("SS_MCP_PYTHON", sys.executable),
                                   args=["-m", "superstudent", "mcp"], env=env, cwd=str(ROOT))
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            check({"search_course_materials", "read_material", "view_page", "course_file", "start_sync"} <= names,
                  f"connector tools listed {sorted(names)}")
            def hint(t):
                a = t.annotations
                if a is None:
                    return None
                for attr in ("readOnlyHint", "read_only_hint"):
                    if getattr(a, attr, None) is not None:
                        return getattr(a, attr)
                return None

            ro = {t.name: hint(t) for t in tools.tools}
            check(ro.get("search_course_materials") is True and ro.get("start_sync") is False, f"read-only hints {ro}")
            res = await session.call_tool("search_course_materials", {"query": "modified duration", "course": "FIN"})
            text = res.content[0].text
            check("Duration Notes" in text and "Page" in text, "search tool returns cited hits")
            res = await session.call_tool("course_file", {"course": "FIN 6100", "file": "exam_intel"})
            check("cumulative" in res.content[0].text, "course_file returns exam intel")
            res = await session.call_tool("read_material", {"path": "Fall 2026/FIN 6100 - Fixed Income/Modules/02 - Week 2 - Duration/Duration Notes.pdf",
                                                            "at": "Page 2"})
            check("Price sensitivity" in res.content[0].text, "read_material jumps to a page")
            res = await session.call_tool("view_page", {"path": "Fall 2026/FIN 6100 - Fixed Income/Modules/02 - Week 2 - Duration/Duration Notes.pdf",
                                                        "page": 2})
            check(any(getattr(c, "type", "") == "image" for c in res.content), "view_page returns an image")
            res = await session.call_tool("read_material", {"path": "../../etc/passwd"})
            check("outside the library" in res.content[0].text, "paths outside the library refused")


if __name__ == "__main__":
    main()
