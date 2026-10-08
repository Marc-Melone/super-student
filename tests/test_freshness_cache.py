"""The faster freshness checks: just as strict as re-reading everything, and the repeated work stays gone.

Files are trusted by their signature (device, file id, size, modification and change time) only if they had
settled when they were read, and only on a disk that keeps real change times; these tests set the settle time to
zero where they check reuse, and wait briefly before changing a file so its change time moves. Temporary libraries
only; no Canvas or network access.

Run: python tests/test_freshness_cache.py
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from superstudent import evidence, exams, index, notes, outline, util
from superstudent.index import material_status, search, update_index
from superstudent.library import Library, LibraryPathError
from superstudent.util import file_digest, front_matter, one_action


def settle_now():
    """Treat every file as settled (as if it was last changed more than a few seconds ago)."""
    return patch.object(util, "SETTLE_NS", 0)


def touch_later(path: Path, data: bytes, keep_mtime: bool = True) -> None:
    """Rewrite a file in place with same-size content, after its change time can move; optionally put back its
    modification time, as a copy tool or a deliberate replacement might."""
    before = path.stat()
    time.sleep(0.02)
    path.write_bytes(data)
    if keep_mtime:
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert after.st_size == before.st_size and (not keep_mtime or after.st_mtime_ns == before.st_mtime_ns)


def old_material_status(state, rel):
    """The record scan used before the lookup table (1.7.0), for comparison."""
    result = {}
    for course in state.get("courses", {}).values():
        folder = course.get("folder") or ""
        for item in (course.get("items") or {}).values():
            paths = {folder + "/" + p for p in (item.get("path"), item.get("text")) if p}
            paths.update(p + ".md" for p in list(paths) if not p.endswith(".md"))
            assets = {p[:-3] + ".assets/" if p.endswith(".md") else p + ".assets/" for p in paths}
            if rel not in paths and not any(rel.startswith(p) for p in assets):
                continue
            result.update(last_successful_sync=item.get("last_successful_sync") or "",
                          last_successful_version=item.get("successful_stamp") or item.get("stamp") or "")
            status = item.get("status")
            if status == "locked" or item.get("locked"):
                result.update(status="restricted")
            elif status in ("failed", "too_large", "pending") or item.get("stale"):
                result.update(status="stale")
            break
    return result


class FakeClock:
    """Stands in for util's time module, so a test can say how long ago a file was changed."""

    def __init__(self, now_ns: int):
        self.now = now_ns

    def time_ns(self) -> int:
        return self.now

    def time(self) -> float:
        return self.now / 1e9


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ss-fresh-")
        self.addCleanup(self.tmp.cleanup)
        self.lib = Library(Path(self.tmp.name) / "library")
        self.lib.ensure()
        self.assertTrue(util.change_times_tracked(self.lib.meta), "these tests need a disk that keeps change times")
        self.folder = "Fall/Finance"
        self.module = self.lib.root / self.folder / "Modules/01 - Valuation"
        self.module.mkdir(parents=True)
        self.docs = []
        items = {}
        for i in range(4):
            original = self.module / f"Lecture {i}.pdf"
            original.write_bytes(f"original lecture {i} bytes ".encode() * 50)
            sidecar = original.with_name(original.name + ".md")
            sidecar.write_text(front_matter({"title": original.name, "type": "pdf", "source_file": original.name,
                                             "source_sha256": file_digest(original)})
                               + f"# {original.name}\n\n## [Page 1]\n\nNet present value lesson {i} discounts cash flows.\n\n"
                                 f"## [Page 2]\n\nPayback ignores timing in lesson {i}.\n")
            self.docs.append((original, sidecar))
            items[f"file:{i}"] = {"path": f"Modules/01 - Valuation/{original.name}",
                                  "text": f"Modules/01 - Valuation/{original.name}.md", "status": "ok"}
        self.lib.save_state({"version": 1, "courses": {"1": {"folder": self.folder, "name": "Finance",
                                                               "code": "FIN101", "items": items}}})
        self.lib.save_snapshot("1", {"modules": [{"id": 7, "name": "Valuation", "position": 1,
                                                  "dir": "Modules/01 - Valuation", "items": []}]})

    def rel(self, path: Path) -> str:
        return path.relative_to(self.lib.root).as_posix()


class RecordLookupTests(Fixture):
    def test_lookup_table_gives_the_same_answers_as_the_full_scan(self):
        rng = random.Random(11)
        folders = ["Fall/A", "Fall/B", "Fall/A"]           # two courses sharing a folder: later courses still apply
        names = [f"Files/doc{i}.pdf" for i in range(6)] + ["Pages/p.md", "Media/talk.mp4", "Modules/01 - X/deck.pptx"]
        for trial in range(40):
            courses = {}
            for c, folder in enumerate(folders):
                items = {}
                for i in range(rng.randint(0, 10)):
                    name = rng.choice(names)
                    item = {"path": name, "status": rng.choice(["ok", "locked", "failed", "pending", "too_large", None])}
                    if rng.random() < 0.5:
                        item["text"] = name if name.endswith(".md") else name + ".md"
                    if rng.random() < 0.2:
                        item["stale"] = True
                    if rng.random() < 0.2:
                        item["locked"] = True
                    item["last_successful_sync"] = f"2026-10-0{rng.randint(1, 9)}"
                    item["stamp"] = f"s{trial}-{c}-{i}"
                    items[f"k{i}"] = item
                courses[str(c)] = {"folder": folder, "items": items}
            state = {"version": 1, "courses": courses}
            self.lib.save_state(state)
            probes = set()
            for folder in folders:
                for name in names:
                    base = f"{folder}/{name}"
                    stem = base[:-3] if base.endswith(".md") else base
                    probes.update({base, base + ".md", stem + ".assets/img.png", stem + ".assets/sub/x.png",
                                   base + ".assets.assets/y.png", folder + "/Files/unrelated.pdf"})
            for rel in sorted(probes):
                expected = old_material_status(state, rel)
                for given in (None, state):
                    got = material_status(self.lib, rel, given)
                    with self.subTest(trial=trial, rel=rel, given=given is not None):
                        self.assertEqual(got["status"], expected.get("status", "current"))
                        self.assertEqual(got["last_successful_sync"], expected.get("last_successful_sync", ""))
                        self.assertEqual(got["last_successful_version"], expected.get("last_successful_version", ""))

    def test_a_record_change_is_seen_at_once_even_inside_one_action(self):
        rel = self.rel(self.docs[0][0])
        with one_action():
            self.assertEqual(material_status(self.lib, rel)["status"], "current")
            state = self.lib.load_state()
            state["courses"]["1"]["items"]["file:0"]["status"] = "locked"
            self.lib.save_state(state)
            self.assertEqual(material_status(self.lib, rel)["status"], "restricted")
        with settle_now():
            self.assertEqual(material_status(self.lib, rel)["status"], "restricted")
            time.sleep(0.02)
            state["courses"]["1"]["items"]["file:0"]["status"] = "ok"
            self.lib.save_state(state)
            self.assertEqual(material_status(self.lib, rel)["status"], "current")

    def test_a_study_fingerprint_follows_a_record_change_inside_one_action(self):
        sidecar = self.docs[0][1]
        with one_action():
            before = notes.fingerprint(sidecar, self.lib)
            self.assertEqual(notes.fingerprint(sidecar, self.lib), before)
            state = self.lib.load_state()
            state["courses"]["1"]["items"]["file:0"]["status"] = "locked"
            self.lib.save_state(state)
            self.assertNotEqual(notes.fingerprint(sidecar, self.lib), before)

    def test_the_record_is_read_once_per_version(self):
        with settle_now():
            material_status(self.lib, self.rel(self.docs[0][0]))
            with patch.object(index, "_build_lookup", wraps=index._build_lookup) as build, \
                    patch.object(util.Path, "read_bytes", autospec=True, side_effect=util.Path.read_bytes) as reads:
                for original, _ in self.docs * 5:
                    material_status(self.lib, self.rel(original))
                self.assertEqual(build.call_count, 0)
                self.assertFalse(any(Path(call.args[0]).name == "state.json" for call in reads.call_args_list))


class FingerprintTests(Fixture):
    def test_settled_file_is_hashed_once_and_any_change_is_noticed(self):
        original = self.docs[0][0]
        with settle_now(), patch.object(util, "file_digest", wraps=file_digest) as digest:
            first = util.cached_digest(original)
            self.assertEqual(util.cached_digest(original), first)
            self.assertEqual(digest.call_count, 1)
            touch_later(original, original.read_bytes().replace(b"lecture 0", b"lecture X"))
            changed = util.cached_digest(original)
            self.assertEqual(digest.call_count, 2)
            self.assertNotEqual(changed, first)
            self.assertEqual(changed, file_digest(original))

    def test_a_file_written_moments_ago_is_read_again_even_with_an_unchanged_signature(self):
        original = self.docs[0][0]
        fixed = (1, 2, 3, 4, time.time_ns())          # a disk whose timestamps didn't move: the same signature
        with patch.object(util, "stat_signature", return_value=fixed), patch.dict(util._CHANGE_TIMES, {1: True}):
            first = util.cached_digest(original)
            original.write_bytes(original.read_bytes().replace(b"lecture 0", b"lecture Y"))
            self.assertNotEqual(util.cached_digest(original), first)

    def test_a_file_still_settling_when_its_read_began_is_read_again_later(self):
        original = self.docs[0][0]
        changed = util.stat_signature(original)[4]
        clock = FakeClock(changed + 1_000_000_000)   # changed a second ago when the read begins...

        def slow_read(path):
            clock.now = changed + 10_000_000_000      # ...and settled by the time the read ends
            return file_digest(path)

        store = self.lib.meta / "fingerprints.json"
        with patch.object(util, "time", clock), patch.object(util, "file_digest", side_effect=slow_read) as reads:
            first = util.cached_digest(original, store=store)
            self.assertNotIn(str(original), util._DIGESTS)
            self.assertNotIn(str(original), util._PENDING.get(str(store), {}))
            self.assertEqual(util.cached_digest(original, store=store), first)     # read again, now trusted
            self.assertEqual(util.cached_digest(original, store=store), first)     # and reused
            self.assertEqual(reads.call_count, 2)
            self.assertIn(str(original), util._PENDING.get(str(store), {}))

    def test_a_source_still_settling_when_its_check_began_is_checked_again_later(self):
        original, sidecar = self.docs[3]
        newest = max(util.stat_signature(p)[4] for p in (sidecar, original, self.lib.state_path))
        clock = FakeClock(newest + 1_000_000_000)
        split = evidence._split_sections

        def slow_split(text):
            clock.now = newest + 10_000_000_000      # settled by the time the check ends
            return split(text)

        with patch.object(util, "time", clock), \
                patch.object(evidence, "_split_sections", side_effect=slow_split) as parses:
            fingerprints = {evidence.source_fingerprint(self.lib, self.rel(sidecar), self.folder) for _ in range(3)}
        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(parses.call_count, 2)       # checked again once, then trusted

    def test_a_document_still_settling_when_it_was_read_is_read_again_later(self):
        course = self.lib.root / self.folder
        newest = max(util.stat_signature(p)[4] for p in [self.lib.state_path, *(p for pair in self.docs for p in pair)])
        clock = FakeClock(newest + 1_000_000_000)
        read_doc = outline._read_doc

        def slow_read(*args):
            clock.now = newest + 10_000_000_000      # settled by the time the first document has been read
            return read_doc(*args)

        with patch.object(util, "time", clock), patch.object(outline, "_read_doc", side_effect=slow_read) as reads:
            titles = [d.title for d in outline.course_documents(self.lib, course)]
            self.assertEqual(reads.call_count, len(self.docs))
            self.assertEqual([d.title for d in outline.course_documents(self.lib, course)], titles)
            self.assertEqual(reads.call_count, len(self.docs) + 1)      # only the one read too early, again
            outline.course_documents(self.lib, course)
            self.assertEqual(reads.call_count, len(self.docs) + 1)

    def test_a_described_original_still_settling_is_hashed_again_later(self):
        original = self.docs[0][0]
        changed = util.stat_signature(original)[4]
        clock = FakeClock(changed + 1_000_000_000)

        def slow_read(path):
            clock.now = changed + 10_000_000_000
            return file_digest(path)

        with patch.object(util, "time", clock), patch.object(util, "file_digest", side_effect=slow_read) as reads:
            digest, recorded = index._source_version(original, lib=self.lib)
            self.assertEqual(recorded, "")                 # read too early to be trusted next time
            again = index._source_version(original, (digest, recorded), lib=self.lib)
            self.assertEqual((again[0], reads.call_count), (digest, 2))
            self.assertNotEqual(again[1], "")
            self.assertEqual(index._source_version(original, again, lib=self.lib), again)
            self.assertEqual(reads.call_count, 2)

    def test_a_record_still_settling_when_it_was_read_is_read_again_later(self):
        state_path = self.lib.state_path
        changed = util.stat_signature(state_path)[4]
        clock = FakeClock(changed + 1_000_000_000)
        util._VIEWS.clear()
        with patch.object(util, "time", clock), \
                patch.object(util.Path, "read_bytes", autospec=True, side_effect=util.Path.read_bytes) as reads:
            first = util.read_json_view(state_path)
            clock.now = changed + 10_000_000_000
            self.assertIs(util.read_json_view(state_path), first)       # read again (same bytes, same result)
            self.assertIs(util.read_json_view(state_path), first)       # then trusted without reading
            self.assertEqual(sum(Path(c.args[0]) == state_path for c in reads.call_args_list), 2)

    def test_saved_fingerprints_are_reused_after_a_restart_and_dropped_after_a_change(self):
        original = self.docs[1][0]
        with settle_now():
            with one_action():
                digest = self.lib.digest(original)
            store = self.lib.meta / "fingerprints.json"
            self.assertTrue(store.is_file())
            util._DIGESTS.clear()
            util._VIEWS.clear()                        # as if the app had been closed and opened again
            with patch.object(util, "file_digest", side_effect=AssertionError("re-read an unchanged file")):
                self.assertEqual(self.lib.digest(original), digest)
            touch_later(original, original.read_bytes().replace(b"lecture 1", b"lecture Z"))
            util._DIGESTS.clear()
            self.assertEqual(self.lib.digest(original), file_digest(original))
            self.assertNotEqual(self.lib.digest(original), digest)

    def test_damaged_or_forged_fingerprint_file_is_ignored(self):
        original = self.docs[2][0]
        signature = list(util.stat_signature(original))
        store = self.lib.meta / "fingerprints.json"
        for content in ("{broken", '{"files": []}',
                        json.dumps({"files": {str(original): signature + ["not-a-sha256"]}}),
                        json.dumps({"files": {str(original): [0, 0, 0, 0, 0, "0" * 64]}})):
            with self.subTest(content=content[:30]), settle_now():
                store.write_text(content)
                util._DIGESTS.clear()
                util._VIEWS.clear()
                self.assertEqual(self.lib.digest(original), file_digest(original))

    def test_fingerprints_are_never_written_into_a_removed_library(self):
        original = self.docs[3][0]
        with settle_now():
            util._PENDING[str(self.lib.meta / "fingerprints.json")] = {str(original): [*util.stat_signature(original), "0" * 64]}
        gone = Path(self.tmp.name) / "removed"
        util._PENDING[str(gone / ".superstudent" / "fingerprints.json")] = {"x": [0, 0, 0, 0, 0, "0" * 64]}
        util.save_fingerprints()
        self.assertFalse(gone.exists())
        self.assertTrue((self.lib.meta / "fingerprints.json").is_file())


class DiskCheckTests(Fixture):
    def test_each_folder_is_checked_once_and_nothing_is_left_behind(self):
        folder = Path(self.tmp.name) / "probe"
        folder.mkdir()
        with patch.dict(util._CHECKED_FOLDERS), patch.dict(util._CHANGE_TIMES):
            self.assertIs(util.change_times_tracked(folder), True)
            self.assertEqual(list(folder.iterdir()), [])
            with patch.object(util.tempfile, "mkstemp", side_effect=AssertionError("checked twice")):
                self.assertIs(util.change_times_tracked(folder), True)
        self.assertIsNone(util.change_times_tracked(Path(self.tmp.name) / "missing"))
        self.assertFalse((Path(self.tmp.name) / "missing").exists())

    def test_a_disk_reporting_the_modification_time_as_change_time_is_not_trusted(self):
        folder = Path(self.tmp.name) / "fat"
        folder.mkdir()
        real_stat = os.stat

        class FatStat:                 # FAT and exFAT on a Mac: no change time, the modification time instead
            def __init__(self, st):
                self.st = st

            def __getattr__(self, name):
                return getattr(self.st, {"st_ctime_ns": "st_mtime_ns", "st_ctime": "st_mtime"}.get(name, name))

        with patch.dict(util._CHECKED_FOLDERS), patch.dict(util._CHANGE_TIMES), \
                patch.object(util.os, "stat", side_effect=lambda p, *a, **k: FatStat(real_stat(p, *a, **k))):
            self.assertIs(util.change_times_tracked(folder), False)
            self.assertIs(util._CHANGE_TIMES[real_stat(folder).st_dev], False)
        self.assertEqual(list(folder.iterdir()), [])

    def test_windows_change_times_are_not_trusted(self):
        with patch.object(util.os, "name", "nt"):
            self.assertIs(util.change_times_tracked(self.lib.meta), False)

    def test_nothing_is_reused_between_actions_on_an_untrusted_disk(self):
        original = self.docs[0][0]
        device = util.stat_signature(original)[0]
        store = self.lib.meta / "fingerprints.json"
        with settle_now():
            notes.progress(self.lib)                       # remembered while the disk was trusted
            digest, recorded = index._source_version(original, lib=self.lib)
            self.assertNotEqual(recorded, "")
            util.save_fingerprints()
            saved = store.read_bytes()
            with patch.dict(util._CHANGE_TIMES, {device: False}), \
                    patch.object(util, "file_digest", wraps=file_digest) as digests, \
                    patch.object(outline, "_read_doc", wraps=outline._read_doc) as reads:
                with one_action():
                    self.lib.digest(original)
                    self.lib.digest(original)
                self.assertEqual(digests.call_count, 1)    # reused within one action, as before
                self.lib.digest(original)
                self.assertEqual(digests.call_count, 2)    # read again by the next action
                self.assertEqual(index._source_version(original, (digest, recorded), lib=self.lib), (digest, ""))
                self.assertEqual(digests.call_count, 3)
                notes.progress(self.lib)
                first = reads.call_count
                self.assertGreaterEqual(first, len(self.docs))
                notes.progress(self.lib)
                self.assertEqual(reads.call_count, 2 * first)
            self.assertEqual(store.read_bytes(), saved)    # nothing new saved for later either

    def test_exam_sources_are_checked_again_by_every_request_on_an_untrusted_disk(self):
        plan = exams.create_plan(self.lib, self.folder, "Midterm")
        _, sidecar = self.docs[2]
        question = {"topic": "NPV", "prompt": "What does lesson 2 say NPV does?", "type": "short_answer",
                    "answer": "Discounts cash flows", "explanation": "Page 1.", "difficulty": "recall",
                    "citations": [{"path": self.rel(sidecar), "locator": "Page 1",
                                   "quote": "Net present value lesson 2 discounts cash flows."}]}
        self.assertTrue(exams.save_questions(self.lib, plan["plan"]["id"], [question])["ok"])
        device = util.stat_signature(sidecar)[0]
        with settle_now(), patch.dict(util._CHANGE_TIMES, {device: False}), \
                patch.object(evidence, "_split_sections", wraps=evidence._split_sections) as parses, \
                patch.object(evidence, "file_digest", wraps=file_digest) as digests:
            work = []
            for _ in range(2):
                public = exams.get_plan(self.lib, plan["plan"]["id"])["plan"]
                self.assertEqual(public["questions"][0]["status"], "current")
                work.append((parses.call_count, digests.call_count))
            self.assertGreater(min(work[0]), 0)
            self.assertEqual(work[1], (2 * work[0][0], 2 * work[0][1]))     # the same work again, nothing reused


class ReuseAcrossActionsTests(Fixture):
    def notes_for(self, original):
        lines = "\n".join(f"- Page {n}: the lesson's main point, with its example and the caveat." for n in (1, 2))
        saved = notes.save(self.lib, self.rel(original), "Summary of the lecture.\n" + lines, by="Claude")
        self.assertTrue(saved["ok"], saved)

    def test_study_progress_reads_each_document_once_and_rereads_only_what_changed(self):
        self.notes_for(self.docs[0][0])
        with settle_now():
            with patch.object(outline, "_read_doc", wraps=outline._read_doc) as reads, \
                    patch.object(util, "file_digest", wraps=file_digest) as digests:
                first = notes.progress(self.lib)
                self.assertLessEqual(reads.call_count, len(self.docs) + 1)
                self.assertLessEqual(digests.call_count, len(self.docs))
                reads.reset_mock()
                digests.reset_mock()
                self.assertEqual(notes.progress(self.lib), first)
                self.assertEqual((reads.call_count, digests.call_count), (0, 0))
                original, sidecar = self.docs[0]
                touch_later(original, original.read_bytes().replace(b"lecture 0", b"lecture W"))
                changed = notes.progress(self.lib)
                self.assertEqual(reads.call_count, 1)
                self.assertEqual(digests.call_count, 1)
        self.assertEqual(first["courses"][0]["done"], 1)
        self.assertEqual(changed["courses"][0]["done"], 0)
        self.assertEqual(changed["courses"][0]["changed"], 1)

    def test_exam_requests_reuse_sources_until_a_cited_source_changes(self):
        plan = exams.create_plan(self.lib, self.folder, "Midterm")
        self.assertTrue(plan["ok"], plan)
        _, sidecar = self.docs[0]
        question = {"topic": "NPV", "prompt": "What does lesson 0 say NPV does?", "type": "mcq",
                    "choices": ["Discounts cash flows", "Ignores timing"], "correct_index": 0,
                    "answer": "Discounts cash flows", "explanation": "Page 1.", "difficulty": "recall",
                    "citations": [{"path": self.rel(sidecar), "locator": "Page 1",
                                   "quote": "Net present value lesson 0 discounts cash flows."}]}
        saved = exams.save_questions(self.lib, plan["plan"]["id"], [question])
        self.assertTrue(saved["ok"], saved)
        with settle_now():
            exams.get_plan(self.lib, plan["plan"]["id"])
            with patch.object(evidence, "_split_sections", wraps=evidence._split_sections) as parses, \
                    patch.object(evidence, "file_digest", wraps=file_digest) as digests:
                public = exams.get_plan(self.lib, plan["plan"]["id"])["plan"]
                self.assertEqual((parses.call_count, digests.call_count), (0, 0))
                self.assertEqual(public["questions"][0]["status"], "current")
                touch_later(sidecar, sidecar.read_bytes().replace(b"discounts cash", b"DISCOUNTS cash"))
                public = exams.get_plan(self.lib, plan["plan"]["id"])["plan"]
                self.assertEqual(public["questions"][0]["status"], "stale")
                self.assertGreater(parses.call_count, 0)

    def test_a_cited_original_replaced_with_same_size_and_date_is_caught_after_settling(self):
        plan = exams.create_plan(self.lib, self.folder, "Midterm")
        original, sidecar = self.docs[1]
        question = {"topic": "Payback", "prompt": "What does payback ignore?", "type": "short_answer",
                    "answer": "Timing", "explanation": "Page 2.", "difficulty": "recall",
                    "citations": [{"path": self.rel(sidecar), "locator": "Page 2",
                                   "quote": "Payback ignores timing in lesson 1."}]}
        self.assertTrue(exams.save_questions(self.lib, plan["plan"]["id"], [question])["ok"])
        with settle_now():
            self.assertEqual(exams.get_plan(self.lib, plan["plan"]["id"])["plan"]["questions"][0]["status"], "current")
            touch_later(original, original.read_bytes().replace(b"lecture 1", b"lecture V"))
            status = exams.get_plan(self.lib, plan["plan"]["id"])["plan"]["questions"][0]
        self.assertEqual(status["status"], "stale")
        self.assertTrue(any("original file changed" in e for e in status["status_errors"]), status)

    def test_search_still_hides_a_description_once_its_picture_changes(self):
        from PIL import Image
        from superstudent import describe
        picture = self.lib.root / self.folder / "Files" / "chart.png"
        picture.parent.mkdir()
        Image.new("RGB", (8, 8), "white").save(picture)
        picture.with_name("chart.png.md").write_text(
            front_matter({"title": "chart.png", "type": "image", "source_file": "chart.png",
                          "source_sha256": file_digest(picture)})
            + "# chart.png\n\n## [Image]\n> Visual content: this file is an image. View it directly.\n")
        self.assertTrue(describe.save(self.lib, self.rel(picture), "Image", "An obsolete diagram of magenta bond curves.")["ok"])
        with settle_now():
            update_index(self.lib)
            self.assertTrue(search(self.lib, "magenta bond curves"))
            Image.new("RGB", (8, 8), "black").save(picture)
            self.assertEqual(search(self.lib, "magenta bond curves"), [])

    def test_a_folder_swapped_for_a_link_outside_is_refused_by_the_next_action(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("# Secret\n\nPRIVATE_OUTSIDE_TEXT\n")
        files = self.lib.root / self.folder / "Files"
        files.mkdir()
        (files / "secret.md").write_text("# Inside\n\nInside text.\n")
        with one_action():
            self.assertEqual(self.lib.checked(files / "secret.md"), (files / "secret.md").resolve())
        (files / "secret.md").unlink()
        files.rmdir()
        files.symlink_to(outside, target_is_directory=True)
        with one_action(), self.assertRaises(LibraryPathError):
            self.lib.checked(files / "secret.md")
        docs = outline.course_documents(self.lib, self.lib.root / self.folder)
        self.assertNotIn("PRIVATE_OUTSIDE_TEXT", json.dumps([d.title for d in docs]))
        self.assertFalse(any("secret" in d.rel for d in docs))


class PathHelperTests(unittest.TestCase):
    def test_containment_and_relative_paths_match_pathlib(self):
        rng = random.Random(3)
        parts = ["a", "b", "ab", "a b", "a.assets", "..x", "x."]
        for _ in range(2000):
            directory = PurePosixPath("/", *rng.choices(parts, k=rng.randint(0, 3)))
            path = PurePosixPath("/", *rng.choices(parts, k=rng.randint(0, 5)))
            if rng.random() < 0.5:
                path = directory.joinpath(*rng.choices(parts, k=rng.randint(0, 3)))
            with self.subTest(path=str(path), directory=str(directory)):
                self.assertEqual(util.is_within(str(path), str(directory)), directory in path.parents)
                try:
                    expected = path.relative_to(directory).as_posix()
                except ValueError:
                    expected = None
                try:
                    got = util.relative_posix(str(path), str(directory))
                except ValueError:
                    got = None
                self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
