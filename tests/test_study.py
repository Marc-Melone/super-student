"""Broad review: whole-slide rendering without LibreOffice, labels laid over pictures, the course outline, and
the study pass (AI notes with coverage tracking), through the library, the connector and the command line.

Run:  python tests/test_study.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

WORK = Path(os.environ.get("SS_TEST_DIR") or tempfile.mkdtemp(prefix="ss-study-"))
for sub in ("app", "lib", "files"):
    shutil.rmtree(WORK / sub, ignore_errors=True)
os.environ.update(SUPERSTUDENT_HOME=str(WORK / "app"), SUPERSTUDENT_LIBRARY=str(WORK / "lib"),
                  CANVAS_TOKEN="test-token-123", SUPERSTUDENT_NO_SOFFICE="1")

PASSED = []


def check(cond, label, detail=""):
    if not cond:
        raise AssertionError(f"FAILED: {label} {detail}")
    PASSED.append(label)
    print(f"  ok  {label}")


def main() -> None:
    from fake_canvas import FakeCanvas
    from make_fixtures import build_all, make_anatomy_pptx
    from superstudent.cli import main as cli
    from superstudent.library import Library

    fake = FakeCanvas(build_all(WORK / "fixtures"))
    os.environ["SUPERSTUDENT_CANVAS_URL"] = fake.start()
    lib = Library(WORK / "lib")
    check(cli(["sync", "--no-media", "--quiet"]) == 0, "first sync")
    fin = next(lib.root.glob("*/FIN 6100*"))
    (WORK / "files").mkdir(parents=True, exist_ok=True)
    deck_src = make_anatomy_pptx(WORK / "files" / "Shoulder anatomy.pptx", WORK / "files")
    (fin / "My Files").mkdir(exist_ok=True)
    shutil.copy2(deck_src, fin / "My Files" / "Shoulder anatomy.pptx")
    check(cli(["sync", "--no-media", "--quiet"]) == 0, "sync picks up a deck added to My Files")
    deck_rel = (fin / "My Files" / "Shoulder anatomy.pptx").relative_to(lib.root).as_posix()

    print("\n== Labels and arrows laid over pictures")
    text = (lib.root / (deck_rel + ".md")).read_text()
    check("Labels on this picture: Supraspinatus (arrow to the upper left of the picture)" in text
          and "Humerus (arrow to the lower right of the picture)" in text, "each label says where its arrow points",
          [ln for ln in text.splitlines() if ln.startswith("Labels")])

    print("\n== Whole slides, drawn without LibreOffice")
    from PIL import Image
    from superstudent.render import DRAWN_SUFFIX, render

    out = render(lib, deck_rel, page=1)
    check(len(out) == 1 and str(out[0]).endswith(DRAWN_SUFFIX), "a slide renders as one whole image", out)
    im = Image.open(out[0]).convert("RGB")
    check(im.size == (1500, 1125), "sized for the AI to read (a 4:3 deck)", im.size)
    # the picture sits where the instructor put it (3.0-7.2in across a 10in slide), with labels around it
    px = im.getpixel((round(0.5 * im.width), round(0.30 * im.height)))
    check(px[0] > 180 and px[1] < 160, "the picture is drawn in its place", px)
    dark = sum(1 for x in range(0, round(0.25 * im.width), 3) for y in range(round(0.2 * im.height), round(0.3 * im.height), 3)
               if sum(im.getpixel((x, y))) < 200)
    check(dark > 30, "label text drawn to the left of the picture", dark)
    out2 = render(lib, deck_rel, page=2)
    bg = Image.open(out2[0]).convert("RGB").getpixel((5, 5))
    check(bg == (20, 30, 60), "slide backgrounds kept", bg)
    check(render(lib, deck_rel, page=1) == out, "renders are cached")
    try:
        render(lib, deck_rel, page=9)
        check(False, "missing slide refused")
    except Exception as exc:
        check("doesn't exist" in str(exc), "missing slide refused", exc)

    print("\n== Course outline")
    outline = (fin / "OUTLINE.md").read_text()
    check("## Module 1:" in outline and "## Module 2:" in outline, "modules in order")
    m1, m2 = outline.index("## Module 1:"), outline.index("## Module 2:")
    check(m1 < outline.index("Lecture 1 - Bond Basics") < m2, "documents under their module")
    check("Slide 1: Price-Yield Relationship [chart]" in outline and "Slide 3: Yield to Maturity [picture]" in outline,
          "every slide listed, in presentation order, with pictures marked")
    check("Locked answers.pdf** (Not downloaded" in outline, "files that couldn't be copied say why")
    check("(no extractable text" not in outline, "no placeholder text used as page titles")
    check("Slide 1: Posterior shoulder muscles [picture]" in outline and "## My Files" in outline, "student's own files too")
    check("notes: no current notes" in outline, "notes status shown")
    from superstudent.index import search

    check(not any(h["path"].endswith("OUTLINE.md") for h in search(lib, "Bond Basics", limit=20)),
          "the outline doesn't crowd search results")

    print("\n== The study pass")
    from superstudent import notes

    info = notes.progress(lib, course="FIN")
    course = info["courses"][0]
    check(course["docs"] >= 6 and course["done"] == 0, "documents to study counted", course["docs"])
    first = course["todo"][0]
    check(first["section"].startswith("Modules/01"), "study order follows the modules", first)
    check(not any("/Announcements/" in t["path"] for t in notes.progress(lib, course="FIN", limit=100)["courses"][0]["todo"]),
          "announcements left to the course-level pass")

    partial = ("## Summary\nThe rotator cuff (Slide 1) stabilizes the shoulder. Tears are the most common injury "
               "(Slide 2). Muscle actions and nerves are tabulated on Slide 3.\n\n## Figures\nSlide 1: posterior view with "
               "supraspinatus (upper left), infraspinatus, deltoid and humerus labeled by arrows.")
    r = notes.save(lib, deck_rel, partial, by="Claude")
    check(r["ok"] and r["missed"] == ["Slide 4", "Slide 5"] and "Slides 4-5" in r["message"],
          "notes that skip slides are saved but flagged", r)
    check(notes.doc_status(notes.load(lib), notes._load(lib, lib.root / (deck_rel + ".md"), "")) == "missing references: Slides 4-5",
          "status shows what was skipped")
    full = partial + ("\n\n## Nerve pathway\nC5 root to upper trunk to suprascapular nerve (Slide 4).\n\n## Summary slide\n"
                      "SITS mnemonic; deltoid is not part of the cuff (Slide 5).\n\n## Likely exam questions\n"
                      "- Name the four rotator cuff muscles (Slides 1, 5).")
    r = notes.save(lib, deck_rel + ".md", full, by="Claude")
    check(r["ok"] and not r["missed"], "complete notes accepted", r)
    notes_file = lib.root / r["file"]
    check(notes_file.exists() and notes_file.relative_to(fin).parts[:2] == ("Study Notes", "My Files"),
          "notes saved under Study Notes, mirroring the course", r["file"])
    body = notes_file.read_text()
    check("Written by Claude" in body and "not course material" in body and "Shoulder%20anatomy.pptx" in body,
          "notes say who wrote them and link the source")
    hits = search(lib, "SITS mnemonic deltoid cuff")
    check(any(h["path"] == r["file"] for h in hits), "notes are searchable", [h["path"] for h in hits[:3]])
    check("notes: current referenced notes" in (fin / "OUTLINE.md").read_text(), "outline updated with the notes status")

    for bad, why in (("../../etc/passwd", "outside"), (fin.relative_to(lib.root).as_posix() + "/OUTLINE.md", "generated"),
                     (deck_rel, "short")):
        r = notes.save(lib, bad, "too short" if why == "short" else full, by="x")
        check(not r["ok"], f"refused: {why}", r)

    # module and course notes
    mod1 = next((fin / "Modules").glob("01 - *"))
    mod_rel = mod1.relative_to(lib.root).as_posix()
    summary = "Module notes. " * 40
    r = notes.save(lib, mod_rel, summary, by="ChatGPT")
    check(r["ok"] and r["kind"] == "module" and (mod1.parent.parent / "Study Notes" / "Modules" / mod1.name / "_Module notes.md").exists(),
          "module notes saved", r)
    status = {m["path"]: m["status"] for m in notes.progress(lib, course="FIN")["courses"][0]["modules"]}
    check(status[mod_rel] == "incomplete: some sources unavailable", "module notes disclose unavailable sources", status)
    first_doc = notes.progress(lib, course="FIN")["courses"][0]["todo"][0]
    lib_doc = notes._load(lib, lib.root / (first_doc["path"] + ("" if first_doc["path"].endswith(".md") else ".md")), "")
    cites = ", ".join(lib_doc.units[i].name for i in range(len(lib_doc.units))) or "Page 1"
    r = notes.save(lib, first_doc["path"], ("Notes on the first document, covering " + cites + ". ") * 4, by="ChatGPT")
    check(r["ok"], "notes for a module document", r)
    status = {m["path"]: m["status"] for m in notes.progress(lib, course="FIN")["courses"][0]["modules"]}
    check(status[mod_rel] == "older than some document notes", "module notes flagged when newer document notes arrive", status)
    r = notes.save(lib, fin.relative_to(lib.root).as_posix(), "Course notes. " * 40, by="Claude")
    check(r["ok"] and r["kind"] == "course" and (fin / "Study Notes" / "_Course notes.md").exists(), "course notes saved")

    # a document that changes after it was studied
    side = lib.root / (deck_rel + ".md")
    side.write_text(side.read_text().replace("SITS muscles", "SITS muscles (updated)"))
    check(notes.doc_status(notes.load(lib), notes._load(lib, side, "")) == "changed since notes saved",
          "documents that change are flagged for another look")

    print("\n== Exam packs carry slides as PDFs and the notes")
    from superstudent.packs import make_pack

    pack = make_pack(lib, fin.relative_to(lib.root).as_posix(), [1], to_pdf=True)
    root = Path(pack["path"])
    check((root / "Course notes (study pass).md").exists(), "course notes in the pack")
    check(any(p.name == "_Module notes.md" for p in root.rglob("*.md")), "module notes in the pack")
    pdfs = [p for p in root.rglob("*.pdf") if p.stem.startswith("Lecture 1")]
    import pymupdf

    check(pdfs and pymupdf.open(str(pdfs[0])).page_count == 3, "slide deck turned into a PDF without LibreOffice", pdfs)

    print("\n== Leaner, same content")
    from superstudent.compact import compact
    import re as _re

    boiler = _re.compile(r"^(Original file: |> Some pages or slides are visual|> Visual content: |---$|\w[\w_]*: )")
    checked = 0
    for sidecar in sorted(fin.rglob("*.md")):
        raw = sidecar.read_text()
        lean = compact(raw)
        body = raw.split("\n---\n", 1)[1] if raw.startswith("---\n") else raw
        for line in body.splitlines():
            line = line.strip()
            if not line or boiler.match(line):
                continue
            line = _re.sub(r"\[(Slide \d+ image \d+[^\]]*)\]\([^)]*\)", r"[\1]", line)
            if line not in lean:
                check(False, "every content line survives the reading view", (sidecar.name, line))
            checked += 1
    check(checked > 100, f"every content line survives the reading view ({checked} lines in {fin.name})")
    raw = (lib.root / (deck_rel + ".md")).read_text()
    check(len(compact(raw)) < 0.9 * len(raw), "the reading view is smaller", (len(raw), len(compact(raw))))
    from superstudent.mcp_server import format_hits

    hits = search(lib, "duration", limit=12)
    text = format_hits("duration", hits)
    paths = [h["path"] for h in hits]
    check(all(text.count("path: " + p) <= 1 for p in set(paths)), "each document's path appears once in results")
    check("same text also in:" in text and "Old exam 2025.pdf" in text, "identical text in two files shown once, both listed")
    check("Original file:" not in text and "Some pages or slides are visual" not in text, "no boilerplate hits")
    # the same deck posted again under another name is recognised as already studied
    twin = fin / "Files" / "Shoulder anatomy (copy).pptx"
    shutil.copy2(fin / "My Files" / "Shoulder anatomy.pptx", fin / "My Files" / "Shoulder anatomy (copy).pptx")
    check(cli(["sync", "--no-media", "--quiet"]) == 0, "sync picks up the copy")
    twin_rel = (fin / "My Files" / "Shoulder anatomy (copy).pptx").relative_to(lib.root).as_posix()
    side = lib.root / (deck_rel + ".md")
    side.write_text(side.read_text().replace("SITS muscles (updated)", "SITS muscles"))   # undo the earlier edit
    notes.save(lib, deck_rel, full, by="Claude")
    todo = [t["path"] for t in notes.progress(lib, course="FIN", limit=100)["courses"][0]["todo"]]
    check(twin_rel not in todo, "a copy of a studied document isn't studied again", todo[:3])
    from superstudent.outline import write_outline

    write_outline(lib, fin, "FIN", notes.status_fn(lib))
    check("see notes for" in (fin / "OUTLINE.md").read_text(), "the outline points the copy at the existing notes")
    big = Image.new("RGB", (3200, 2400), "white")
    (fin / "My Files" / "photo.jpg").parent.mkdir(exist_ok=True)
    big.save(fin / "My Files" / "photo.jpg")
    shown = render(lib, (fin / "My Files" / "photo.jpg").relative_to(lib.root).as_posix())
    check(max(Image.open(shown[0]).size) == 1568, "big photos sent at the size the AI actually looks at", shown)

    print("\n== Connector and command line")
    asyncio.run(mcp_check(lib, deck_rel))
    notes_md = WORK / "files" / "n.md"
    notes_md.write_text(full.replace("SITS", "SITS again"))
    check(cli(["save-notes", deck_rel, "--file", str(notes_md), "--by", "Codex"]) == 0, "superstudent save-notes")
    check(cli(["study", "--course", "FIN"]) == 0, "superstudent study")

    fake.stop()
    print(f"\nALL {len(PASSED)} STUDY CHECKS PASSED  (work dir: {WORK})")


async def mcp_check(lib, deck_rel: str) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "superstudent", "mcp"], env=dict(os.environ),
                                   cwd=str(ROOT))
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            check({"study_progress", "save_study_notes"} <= names, "connector offers the study tools")
            res = await session.call_tool("study_progress", {"course": "FIN", "limit": 3})
            text = res.content[0].text
            check("Reference coverage does not verify" in text and "path:" in text and "Course notes:" in text,
                  "study_progress lists documents and limits the coverage claim", text[:400])
            res = await session.call_tool("course_file", {"course": "FIN 6100", "file": "outline"})
            check("everything in the course" in res.content[0].text, "outline through course_file")
            res = await session.call_tool("course_file", {"course": "FIN 6100", "file": "notes"})
            check("Course notes." in res.content[0].text, "course notes through course_file")
            res = await session.call_tool("view_page", {"path": deck_rel, "page": 1})
            kinds = [getattr(c, "type", "") for c in res.content]
            note = " ".join(getattr(c, "text", "") for c in res.content)
            check("image" in kinds and "drawn by super student" in note.lower(), "view_page shows the whole drawn slide", kinds)
            res = await session.call_tool("view_page", {"path": deck_rel, "pages": "1-3"})
            kinds = [getattr(c, "type", "") for c in res.content]
            labels = [getattr(c, "text", "") for c in res.content if getattr(c, "type", "") == "text"]
            check(kinds.count("image") == 3 and "Slide 2:" in labels, "several slides in one call, each labeled", labels)
            tools = (await session.list_tools()).tools
            size = sum(len(json.dumps({"n": t.name, "d": t.description,
                                       "s": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)}))
                       for t in tools) // 4
            check(size < 3400, "tool definitions including exam prep stay bounded", size)
            res = await session.call_tool("save_study_notes", {"path": deck_rel, "notes": "Short.", "described_by": "Claude"})
            check("too short" in res.content[0].text, "connector passes on refusals")


if __name__ == "__main__":
    main()
