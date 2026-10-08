"""Study work saved by older versions is kept when its source provably hasn't changed since.

Run: python tests/test_carry_over.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from superstudent import carry_over, describe, notes
from superstudent.library import Library
from superstudent.outline import _load
from superstudent.util import file_digest, front_matter, parse_front_matter


class CarryOverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ss-carry-")
        self.lib = Library(Path(self.tmp.name) / "library")
        self.lib.ensure()
        self.folder = "Fall/Finance"
        self.files = self.lib.root / self.folder / "Files"
        self.files.mkdir(parents=True)
        self.lib.save_state({"courses": {"1": {"folder": self.folder, "name": "Finance", "code": "FIN101", "items": {}}}})
        self.original = self.files / "slides.pdf"
        self.original.write_bytes(b"%PDF original bytes" * 100)
        self.sidecar = self.files / "slides.pdf.md"
        self.sidecar.write_text(front_matter({"title": "slides.pdf", "type": "pdf", "source_file": "slides.pdf",
                                              "source_sha256": file_digest(self.original)})
                                + "# slides.pdf\n\n## [Slide 1] Capital budgeting\n\nNPV accepts positive projects.\n\n"
                                  "## [Slide 2] IRR\n\nIRR is the discount rate where NPV is zero.\n")
        self.rel = f"{self.folder}/Files/slides.pdf"
        time.sleep(0.05)

    def tearDown(self):
        self.tmp.cleanup()

    # -- study notes
    def legacy_notes(self, saved_at=None, fp=None):
        store = {self.rel: {"kind": "document", "fp": fp or carry_over._legacy_text_fp(self.sidecar), "units": 2,
                            "missed": [], "file": f"{self.folder}/Study Notes/Files/slides.pdf - notes.md",
                            "by": "Claude", "date": (saved_at or datetime.now()).isoformat(timespec="microseconds")}}
        (self.lib.meta / "notes.json").write_text(json.dumps(store))

    def status(self):
        doc = _load(self.lib, self.sidecar, "Files")
        return notes.doc_status(notes.load(self.lib), doc)

    def test_notes_on_unchanged_text_and_original_are_kept(self):
        self.legacy_notes()
        self.assertEqual(self.status(), "changed since notes saved")          # how 1.6.1 to 1.7.0 saw them
        self.assertEqual(carry_over.study_notes(self.lib), 1)
        self.assertEqual(self.status(), "done")
        self.assertEqual(carry_over.study_notes(self.lib), 0)                 # nothing left to carry over

    def test_notes_on_changed_text_stay_outdated(self):
        self.legacy_notes()
        self.sidecar.write_text(self.sidecar.read_text().replace("positive", "negative"))
        self.assertEqual(carry_over.study_notes(self.lib), 0)
        self.assertEqual(self.status(), "changed since notes saved")

    def test_notes_older_than_the_original_stay_outdated(self):
        self.legacy_notes(saved_at=datetime.now() - timedelta(hours=1))      # the file arrived after the notes
        self.assertEqual(carry_over.study_notes(self.lib), 0)

    def test_notes_whose_original_was_replaced_since_stay_outdated(self):
        self.legacy_notes()
        time.sleep(0.05)
        self.original.write_bytes(self.original.read_bytes())               # same bytes, but touched afterwards
        self.assertEqual(carry_over.study_notes(self.lib), 0)

    def test_notes_on_unavailable_sources_stay_outdated(self):
        self.legacy_notes()
        self.lib.save_state({"courses": {"1": {"folder": self.folder, "name": "Finance", "items": {
            "file:1": {"path": "Files/slides.pdf", "text": "Files/slides.pdf.md", "status": "locked"}}}}})
        self.assertEqual(carry_over.study_notes(self.lib), 0)

    def test_new_format_and_damaged_entries_are_left_alone(self):
        store = {self.rel: {"kind": "document", "fp": "f" * 64, "date": datetime.now().isoformat()},
                 "x": "not an entry", "y": {"kind": "document", "fp": "abc", "date": "garbage"}}
        (self.lib.meta / "notes.json").write_text(json.dumps(store))
        self.assertEqual(carry_over.study_notes(self.lib), 0)
        self.assertEqual(json.loads((self.lib.meta / "notes.json").read_text()), store)

    def test_module_notes_sources_are_carried_over(self):
        module = self.lib.root / self.folder / "Modules/01 - Week 1"
        module.mkdir(parents=True)
        page = module / "lesson.md"
        page.write_text("---\ntype: page\n---\n# Lesson\n\nDiscounting converts future cash flows.\n")
        time.sleep(0.05)
        rel = page.relative_to(self.lib.root).as_posix()
        key = module.relative_to(self.lib.root).as_posix()
        legacy = carry_over._legacy_text_fp(page)
        (self.lib.meta / "notes.json").write_text(json.dumps({key: {
            "kind": "module", "docs": 1, "sources": {rel: legacy}, "date": datetime.now().isoformat()}}))
        self.assertEqual(carry_over.study_notes(self.lib), 1)
        saved = json.loads((self.lib.meta / "notes.json").read_text())[key]["sources"][rel]
        self.assertEqual(saved, notes.fingerprint(page, self.lib))

    # -- picture descriptions
    def legacy_description(self, described, size=None):
        (self.lib.meta / "descriptions.json").write_text(json.dumps({self.rel: {"Slide 1": {
            "text": "A chart of NPV against the discount rate, crossing zero at the IRR.", "by": "Claude",
            "date": described.isoformat(), "size": size or self.original.stat().st_size}}}))

    def test_description_made_after_the_file_arrived_is_kept_and_shown_again(self):
        self.legacy_description(date.today() + timedelta(days=1))           # described on a later day
        self.assertEqual(describe.current(self.lib, self.rel, self.original), {})
        self.assertEqual(carry_over.descriptions(self.lib), 1)
        self.assertIn("Slide 1", describe.current(self.lib, self.rel, self.original))
        self.assertIn("crossing zero at the IRR", self.sidecar.read_text())

    def test_description_with_a_different_size_or_same_day_stays_outdated(self):
        self.legacy_description(date.today() + timedelta(days=1), size=5)
        self.assertEqual(carry_over.descriptions(self.lib), 0)
        self.legacy_description(date.today())          # a date can't show whether it came before the file
        self.assertEqual(carry_over.descriptions(self.lib), 0)
        self.assertNotIn("crossing zero", self.sidecar.read_text())

    # -- transcripts
    def recording(self, written_after=True):
        folder = self.lib.root / self.folder / "My Files"
        folder.mkdir()
        video = folder / "lecture.m4a"
        video.write_bytes(b"audio" * 1000)
        time.sleep(0.05)
        transcript = folder / "lecture.m4a.md"
        transcript.write_text(front_matter({"title": "lecture.m4a", "type": "transcript", "source": "local Whisper"})
                              + "# lecture.m4a (transcript)\n\n## [00:00:00]\n\nToday we cover NPV.\n")
        if not written_after:
            time.sleep(0.05)
            video.write_bytes(video.read_bytes())
        st = video.stat()
        item = {"status": "ok", "stamp": f"{int(st.st_mtime)}|{st.st_size}"}
        return video, transcript, item

    def test_transcript_of_untouched_recording_is_bound_not_redone(self):
        video, transcript, item = self.recording()
        self.assertTrue(carry_over.bind_transcript(self.lib, video, transcript, item, item["stamp"]))
        meta, body = parse_front_matter(transcript.read_text())
        self.assertEqual(meta["source_sha256"], file_digest(video))
        self.assertEqual(meta["source_file"], "lecture.m4a")
        self.assertEqual(meta["type"], "transcript")
        self.assertIn("Today we cover NPV.", body)
        self.assertFalse(carry_over.bind_transcript(self.lib, video, transcript, item, item["stamp"]))   # already bound

    def test_transcript_is_not_bound_to_a_recording_touched_later(self):
        video, transcript, item = self.recording(written_after=False)
        self.assertFalse(carry_over.bind_transcript(self.lib, video, transcript, item, item["stamp"]))
        self.assertNotIn("source_sha256", transcript.read_text())

    def test_transcript_is_not_bound_when_the_recording_differs_or_failed(self):
        video, transcript, item = self.recording()
        self.assertFalse(carry_over.bind_transcript(self.lib, video, transcript, item, "123|5"))
        self.assertFalse(carry_over.bind_transcript(self.lib, video, transcript, dict(item, status="failed"), item["stamp"]))
        self.assertFalse(carry_over.bind_transcript(self.lib, video, transcript, dict(item, stale=True), item["stamp"]))
        self.assertNotIn("source_sha256", transcript.read_text())

    def test_sync_keeps_an_old_my_files_transcript(self):
        """The sync path: a 1.6 transcript of an untouched recording counts as up to date."""
        from superstudent.sync import CourseSync, Syncer
        video, transcript, item = self.recording()
        state = self.lib.load_state()
        state["courses"]["1"]["items"]["mine:My Files/lecture.m4a"] = dict(item, path="My Files/lecture.m4a")
        self.lib.save_state(state)
        class Offline:
            base, calls, auth_failed = "https://canvas.example.edu", 0, None
        syncer = Syncer({"transcribe": "off"}, canvas=Offline(), library=self.lib, log=lambda *_: None)
        course = CourseSync(syncer, {"id": 1, "name": "Finance", "course_code": "FIN101"})
        job = {"course": course, "key": "mine:My Files/lecture.m4a", "type": "local", "title": "lecture.m4a",
               "rel_md": "My Files/lecture.m4a.md", "local_path": str(video), "stamp": item["stamp"]}
        self.assertTrue(course.media_up_to_date(job))
        self.assertIn("source_sha256", transcript.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
