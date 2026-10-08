"""Regression checks for evidence bound to the exact original that produced extracted text.

Uses real TXT/ZIP extraction, mocked Canvas downloads and mocked audio transcription. No network,
native apps, account access or speech-model downloads. Run: python tests/test_extraction_evidence.py
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from superstudent.canvas import Canvas
from superstudent.config import DEFAULTS
from superstudent.evidence import validate_evidence
from superstudent.extract import EXTRACTOR_VERSION, extract as real_extract
from superstudent.library import Library
from superstudent.media import Segment
from superstudent.sync import CourseSync, Syncer
from superstudent.util import front_matter, parse_front_matter


OLD = "Cash flows are discounted using the old rate."
NEW = "Cash flows are discounted using the new rate."


class ExtractionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ss-extraction-evidence-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lib = Library(self.root / "library")
        self.lib.ensure()
        env = patch.dict(os.environ, {
            "SUPERSTUDENT_HOME": str(self.root / "app"),
            "SUPERSTUDENT_LIBRARY": str(self.lib.root),
            "SUPERSTUDENT_TOKEN": "dummy-extraction-test-token",
            "SUPERSTUDENT_NO_SOFFICE": "1",
            "SUPERSTUDENT_NO_APPLE_OCR": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        network = patch.object(Canvas, "_get", side_effect=AssertionError("Network is forbidden in this suite"))
        network.start()
        self.addCleanup(network.stop)
        self.cv = Canvas("https://school.instructure.com", "dummy-extraction-test-token")
        self.remote_bytes = OLD.encode()

        def download(url, target, **kwargs):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.remote_bytes)
            return {"content_type": "text/plain"}

        self.cv.download = Mock(side_effect=download)
        self.syncer = Syncer(dict(DEFAULTS, library_dir=str(self.lib.root), transcribe="off"),
                             self.cv, self.lib, log=lambda _: None, media=False)
        self.cs = CourseSync(self.syncer, {"id": 1, "name": "Finance", "course_code": "FIN101",
                                           "term": {"name": "Fall"}})
        self.cs.dir.mkdir(parents=True)
        self.lib.save_state(self.syncer.state)
        self.remote_meta = {"id": 7, "display_name": "Lecture.txt", "modified_at": "2026-10-01T12:00:00Z",
                            "size": len(self.remote_bytes), "url": "https://school.instructure.com/files/7/download"}

    @staticmethod
    def sidecar(original):
        return original.with_name(original.name + ".md")

    @staticmethod
    def digest(original):
        return hashlib.sha256(original.read_bytes()).hexdigest()

    def metadata(self, original):
        return parse_front_matter(self.sidecar(original).read_text(encoding="utf-8"))

    def assert_bound(self, original, quote=OLD, locator=None):
        self.assertTrue(self.sidecar(original).is_file(), "A successful refresh must produce usable extracted text")
        meta, body = self.metadata(original)
        self.assertEqual(meta.get("source_sha256"), self.digest(original), meta)
        self.assertIn(quote, body)
        result = self.evidence(original, quote, locator)
        self.assertTrue(result["valid"], result)

    def evidence(self, original, quote=OLD, locator=None):
        self.lib.save_state(self.syncer.state)
        return validate_evidence(self.lib, self.cs.folder, [{
            "path": original.relative_to(self.lib.root).as_posix(),
            "locator": locator or original.name, "quote": quote,
        }])

    def local_file(self, name="Notes.txt", content=OLD):
        original = self.cs.dir / "My Files" / name
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(content, encoding="utf-8")
        return original

    def canvas_file(self):
        rel = "Files/Lecture.txt"
        prev = dict(self.cs.items.get("file:7") or {})
        self.cs.item("file:7").update(path=rel, text=rel + ".md", title="Lecture.txt")
        self.cs._process_one("7", dict(self.remote_meta), rel, prev)
        return self.cs.dir / rel

    def replace_same_stamp(self, original, content):
        before = original.stat()
        original.write_bytes(content.encode() if isinstance(content, str) else content)
        os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = original.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def remove_binding(self, original, digest=None):
        meta, body = self.metadata(original)
        meta.pop("source_sha256", None)
        if digest is not None:
            meta["source_sha256"] = digest
        self.sidecar(original).write_text(front_matter(meta) + body, encoding="utf-8")

    def assert_old_quote_rejected(self, original, locator=None):
        if self.sidecar(original).exists():
            meta, body = self.metadata(original)
            if OLD in body:
                self.assertNotEqual(meta.get("source_sha256"), self.digest(original),
                                    "Old extracted text must never be bound to the replacement original")
        result = self.evidence(original, OLD, locator)
        self.assertFalse(result["valid"], result)

    def extraction_that_changes_original(self, path, content_type=None):
        result = real_extract(path, content_type)
        if path.suffix == ".txt":
            self.replace_same_stamp(path, NEW)
        return result

    @staticmethod
    def run_change_detection(operation):
        # A sync stage may reject a concurrent edit by returning without a sidecar or by raising a
        # retryable error; both are safe. The caller below verifies the evidence and the next refresh.
        try:
            operation()
        except (OSError, RuntimeError, ValueError):
            pass

    def test_canvas_download_binds_the_real_extracted_bytes(self):
        original = self.canvas_file()
        self.cv.download.assert_called_once()
        self.assert_bound(original)

    def test_canvas_legacy_sidecar_is_reextracted_at_the_same_remote_stamp(self):
        original = self.canvas_file()
        self.remove_binding(original)
        self.assertFalse(self.evidence(original)["valid"])
        with patch("superstudent.sync.extract", wraps=real_extract) as extraction:
            self.canvas_file()
        self.assertEqual(extraction.call_count, 1)
        self.assertEqual(self.cv.download.call_count, 1, "The unchanged downloaded original can be reused")
        self.assert_bound(original)

    def test_canvas_rechecks_bytes_despite_unchanged_remote_stamp(self):
        original = self.canvas_file()
        self.replace_same_stamp(original, NEW)
        self.assertFalse(self.evidence(original)["valid"])
        self.canvas_file()
        self.assertEqual(self.cv.download.call_count, 1)
        self.assert_bound(original, NEW)
        self.assertFalse(self.evidence(original, OLD)["valid"])

    def test_exam_practice_follows_a_file_that_sync_moves(self):
        from superstudent import exams
        original = self.canvas_file()
        self.lib.save_state(self.syncer.state)
        plan = exams.create_plan(self.lib, self.cs.folder, "Midterm")
        self.assertTrue(plan["ok"], plan)
        old_rel = original.relative_to(self.lib.root).as_posix() + ".md"
        question = {"topic": "Rates", "prompt": "Which rate discounts the cash flows?", "type": "mcq",
                    "choices": ["The old rate", "A market rate"], "correct_index": 0, "answer": "The old rate",
                    "explanation": "The lecture says so.", "difficulty": "recall",
                    "citations": [{"path": old_rel, "locator": original.name, "quote": OLD}]}
        saved = exams.save_questions(self.lib, plan["plan"]["id"], [question])
        self.assertTrue(saved["ok"], saved)
        qid = saved["question_ids"][0]
        self.assertTrue(exams.record_attempt(self.lib, plan["plan"]["id"], qid, choice_index=0)["ok"])
        new_rel = "Modules/01 - Week 1/Lecture.txt"         # the instructor put it in a module
        self.cs.relocate("file:7", new_rel)
        self.cs.items["file:7"].update(path=new_rel, text=new_rel + ".md")
        self.lib.save_state(self.syncer.state)
        public = exams.get_plan(self.lib, plan["plan"]["id"])["plan"]
        self.assertEqual(public["questions"][0]["status"], "current", public["questions"][0]["status_errors"])
        self.assertEqual(public["questions"][0]["citations"][0]["path"], f"{self.cs.folder}/{new_rel}.md")
        self.assertEqual(public["metrics"]["first_attempt_mcq_count"], 1)
        self.assertFalse(public["scope_changed"], public["scope_warnings"])

    def test_canvas_valid_binding_reuses_existing_extraction(self):
        original = self.canvas_file()
        with patch("superstudent.sync.extract", wraps=real_extract) as extraction:
            self.canvas_file()
        extraction.assert_not_called()
        self.assert_bound(original)

    def test_local_same_size_same_mtime_change_requires_refresh(self):
        original = self.local_file()
        self.cs.process_my_files()
        self.assert_bound(original)
        self.replace_same_stamp(original, NEW)
        self.assertFalse(self.evidence(original)["valid"])
        self.cs.process_my_files()
        self.assert_bound(original, NEW)
        self.assertFalse(self.evidence(original, OLD)["valid"])

    def test_local_legacy_or_malformed_binding_is_reextracted_with_same_stamp(self):
        original = self.local_file()
        self.cs.process_my_files()
        self.assertEqual(self.cs.items["mine:My Files/Notes.txt"]["extractor"], EXTRACTOR_VERSION)
        for binding in (None, "invalid", "0" * 64):
            with self.subTest(binding=binding):
                self.remove_binding(original, binding)
                self.assertFalse(self.evidence(original)["valid"])
                with patch("superstudent.sync.extract", wraps=real_extract) as extraction:
                    self.cs.process_my_files()
                self.assertEqual(extraction.call_count, 1)
                self.assert_bound(original)

    def test_local_valid_binding_reuses_existing_extraction(self):
        original = self.local_file()
        self.cs.process_my_files()
        with patch("superstudent.sync.extract", wraps=real_extract) as extraction:
            self.cs.process_my_files()
        extraction.assert_not_called()
        self.assert_bound(original)

    def make_archive(self):
        archive = self.cs.dir / "My Files" / "Lessons.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("unit/Lecture.txt", OLD)
        member = archive.parent / (archive.name + " (unzipped)") / "unit/Lecture.txt"
        return archive, member

    def test_zip_member_uses_its_own_digest_and_validates_as_evidence(self):
        archive, member = self.make_archive()
        self.cs.process_my_files()
        self.assert_bound(member)
        meta, _ = self.metadata(member)
        self.assertNotEqual(meta["source_sha256"], self.digest(archive))
        self.assertEqual(meta.get("source_archive"), archive.relative_to(self.lib.root).as_posix())
        self.assertEqual(meta.get("source_archive_sha256"), self.digest(archive))

    def test_zip_member_legacy_text_is_replaced_on_next_sync(self):
        archive, member = self.make_archive()
        self.cs.process_my_files()
        self.remove_binding(member)
        self.assertFalse(self.evidence(member)["valid"])
        self.cs.process_my_files()
        self.assert_bound(member)

    def test_changed_archive_invalidates_member_until_unpacking_new_contents(self):
        archive, member = self.make_archive()
        self.cs.process_my_files()
        self.assert_bound(member)
        before = archive.stat()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("unit/Lecture.txt", NEW)
        self.assertEqual(archive.stat().st_size, before.st_size)
        os.utime(archive, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(member.read_text(), OLD)
        self.assertFalse(self.evidence(member)["valid"])
        self.cs.process_my_files()
        self.assert_bound(member, NEW)

    def test_missing_archive_invalidates_its_unchanged_member(self):
        archive, member = self.make_archive()
        self.cs.process_my_files()
        self.assert_bound(member)
        archive.unlink()
        self.assertEqual(self.metadata(member)[0]["source_sha256"], self.digest(member))
        self.assertFalse(self.evidence(member)["valid"])

    def test_canvas_original_changed_during_extraction_cannot_support_old_quote(self):
        original = self.cs.dir / "Files/Lecture.txt"
        with patch("superstudent.sync.extract", side_effect=self.extraction_that_changes_original):
            self.run_change_detection(self.canvas_file)
        self.assert_old_quote_rejected(original)
        self.canvas_file()
        self.assert_bound(original, original.read_text().strip())

    def test_local_original_changed_during_extraction_cannot_support_old_quote(self):
        original = self.local_file()
        with patch("superstudent.sync.extract", side_effect=self.extraction_that_changes_original):
            self.run_change_detection(self.cs.process_my_files)
        self.assert_old_quote_rejected(original)
        self.cs.process_my_files()
        self.assert_bound(original, NEW)

    def test_zip_member_changed_during_extraction_cannot_support_old_quote(self):
        archive, member = self.make_archive()
        with patch("superstudent.sync.extract", side_effect=self.extraction_that_changes_original):
            self.run_change_detection(self.cs.process_my_files)
        self.assert_old_quote_rejected(member)
        self.cs.process_my_files()
        self.assert_bound(member)

    def test_archive_changed_while_unpacking_cannot_support_old_member_quote(self):
        archive, member = self.make_archive()

        def change_archive(path, content_type=None):
            result = real_extract(path, content_type)
            if path.suffix == ".txt":
                with zipfile.ZipFile(archive, "w") as output:
                    output.writestr("unit/Lecture.txt", NEW)
            return result

        with patch("superstudent.sync.extract", side_effect=change_archive):
            self.run_change_detection(self.cs.process_my_files)
        self.assertFalse(self.evidence(member)["valid"])
        self.cs.process_my_files()
        self.assert_bound(member, NEW)

    def media_job(self, local=True):
        rel = ("My Files" if local else "Media") + "/Lecture.wav"
        original = self.cs.dir / rel
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"audio clip old")
        key = "mine:" + rel if local else "file:8"
        self.cs.item(key).update(path=rel, text=rel + ".md", title=original.name, kind="media")
        job = {"course": self.cs, "key": key, "type": "local" if local else "file", "title": original.name,
               "rel_md": rel + ".md", "rel_media": rel, "stamp": "same-media-stamp", "is_video": False,
               "contexts": ["My Files" if local else "Module 1"]}
        if local:
            job["local_path"] = str(original)
        else:
            job["file_id"] = "8"
        self.syncer.transcriber = Mock()
        self.syncer.transcriber.describe.return_value = "test transcriber"
        self.syncer.transcriber.transcribe.return_value = [Segment(0, 3, OLD)]
        return original, job

    def transcribe(self, original, job):
        with patch("superstudent.sync.media_duration", return_value=3):
            self.cs._transcribe_file(job, original, keep=True)

    def test_local_transcription_binds_the_recording_bytes(self):
        original, job = self.media_job()
        self.transcribe(original, job)
        self.syncer.transcriber.transcribe.assert_called_once()
        self.assert_bound(original, locator="00:00:00")

    def test_retained_canvas_transcription_binds_the_recording_bytes(self):
        original, job = self.media_job(local=False)
        self.transcribe(original, job)
        self.assert_bound(original, locator="00:00:00")

    def test_legacy_retained_recording_refreshes_from_captions_without_whisper(self):
        original, job = self.media_job(local=False)
        self.cs._write_transcript(job, [Segment(0, 3, OLD)], "Canvas captions")
        job.pop("done", None)
        self.assertFalse(self.evidence(original, locator="00:00:00")["valid"])
        self.syncer.transcriber = None
        self.syncer.media_jobs = [job]
        self.cs.cfg["keep_media_files"] = False
        self.cs.file_meta["8"] = {"url": "https://school.instructure.com/files/8/download"}
        self.remote_bytes = b"updated recording with current Canvas captions"
        with patch.object(self.cs, "_captions", return_value=[Segment(0, 3, NEW)]), \
                patch("superstudent.sync.media_duration", return_value=3):
            self.syncer.process_media()
        self.cv.download.assert_called_once()
        self.assertTrue(job.get("done"))
        self.assertEqual(original.read_bytes(), self.remote_bytes)
        self.assert_bound(original, NEW, "00:00:00")

    def test_missing_retained_recording_is_downloaded_again_before_evidence_recovers(self):
        original, job = self.media_job(local=False)
        self.transcribe(original, job)
        self.assert_bound(original, locator="00:00:00")
        original.unlink()
        job.pop("done", None)
        self.assertFalse(self.cs.media_up_to_date(job), "The old transcript cannot replace its missing original")
        self.assertFalse(self.evidence(original, locator="00:00:00")["valid"])
        self.cs.cfg["keep_media_files"] = True
        self.cs.file_meta["8"] = {"url": "https://school.instructure.com/files/8/download"}
        self.remote_bytes = b"replacement audio recording"
        with patch("superstudent.sync.media_duration", return_value=3):
            self.cs.media_transcribe(job)
        self.cv.download.assert_called_once()
        self.assertEqual(original.read_bytes(), self.remote_bytes)
        self.assert_bound(original, locator="00:00:00")

    def test_media_freshness_rejects_legacy_or_mismatched_binding_at_same_stamp(self):
        for local in (True, False):
            with self.subTest(local=local):
                original, job = self.media_job(local)
                for binding in (None, "0" * 64):
                    with self.subTest(binding=binding):
                        self.transcribe(original, job)
                        self.remove_binding(original, binding)
                        job.pop("done", None)
                        self.assertFalse(self.cs.media_up_to_date(job))
                        self.assertFalse(self.evidence(original, locator="00:00:00")["valid"])
                        self.transcribe(original, job)
                        self.assert_bound(original, locator="00:00:00")

    def test_media_same_size_same_mtime_change_requires_new_transcription(self):
        original, job = self.media_job()
        self.transcribe(original, job)
        self.replace_same_stamp(original, b"audio clip new")
        job.pop("done", None)
        self.assertFalse(self.cs.media_up_to_date(job))
        self.assertFalse(self.evidence(original, locator="00:00:00")["valid"])
        self.syncer.transcriber.transcribe.return_value = [Segment(0, 3, NEW)]
        self.transcribe(original, job)
        self.assert_bound(original, NEW, "00:00:00")

    def test_media_valid_binding_reuses_transcript(self):
        original, job = self.media_job()
        self.transcribe(original, job)
        job.pop("done", None)
        self.assertTrue(self.cs.media_up_to_date(job))
        self.assert_bound(original, locator="00:00:00")

    def test_recording_changed_during_transcription_cannot_support_old_quote(self):
        for local in (True, False):
            with self.subTest(local=local):
                original, job = self.media_job(local)

                def changed(path, **kwargs):
                    self.replace_same_stamp(path, b"audio clip new")
                    return [Segment(0, 3, OLD)]

                self.syncer.transcriber.transcribe.side_effect = changed
                self.run_change_detection(lambda: self.transcribe(original, job))
                self.assert_old_quote_rejected(original, "00:00:00")
                self.syncer.transcriber.transcribe.side_effect = None
                self.syncer.transcriber.transcribe.return_value = [Segment(0, 3, NEW)]
                self.transcribe(original, job)
                self.assert_bound(original, NEW, "00:00:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
