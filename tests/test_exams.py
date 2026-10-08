"""Exam scope, evidence, practice and safe persistence; no account or network access.

Run: python tests/test_exams.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from superstudent import exams
from superstudent.library import Library
from superstudent.util import file_digest, front_matter


class ExamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ss-exams-")
        self.lib = Library(Path(self.tmp.name) / "library")
        self.lib.ensure()
        self.folder = "Fall/Finance"
        self.other = "Fall/Economics"
        for folder in (self.folder, self.other):
            (self.lib.root / folder / "Modules/01 - Time value").mkdir(parents=True)
            (self.lib.root / folder / "Modules/02 - Risk").mkdir(parents=True)
            (self.lib.root / folder / "Assignments").mkdir(parents=True)
        self.lib.save_state({"courses": {
            "1": {"folder": self.folder, "name": "Finance", "code": "FIN101", "items": {}},
            "2": {"folder": self.other, "name": "Economics", "code": "ECON101", "items": {}}}})
        self.lib.save_snapshot("1", {"modules": [
            {"id": 101, "name": "Time value", "position": 1, "dir": "Modules/01 - Time value", "items": []},
            {"id": 102, "name": "Risk", "position": 2, "dir": "Modules/02 - Risk", "items": []}]})
        self.path = self.folder + "/Modules/01 - Time value/lesson.md"
        self.path2 = self.folder + "/Modules/02 - Risk/risk.md"
        self.other_path = self.other + "/Modules/01 - Time value/lesson.md"
        self.source = self.lib.root / self.path
        self.source.write_text("# Time value\n\n## [Page 1]\n\nDiscounting converts future cash flows to present value.\n\n## [Page 2]\n\nCompounding converts present cash flows to future value.\n")
        (self.lib.root / self.path2).write_text("# Risk\n\n## [Page 1]\n\nDiversification reduces firm-specific risk.\n")
        (self.lib.root / self.other_path).write_text(self.source.read_text())
        (self.lib.root / self.folder / "Syllabus.md").write_text("# Syllabus\n\n## Exam\n\nThe instructor confirms exam coverage in class.\n")
        self.at = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        self.clock = patch.object(exams, "_now", side_effect=lambda: self.at)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.tmp.cleanup()

    def plan(self, **kwargs):
        result = exams.create_plan(self.lib, self.folder, "Midterm", **kwargs)
        self.assertTrue(result["ok"], result)
        return result["plan"]

    def question(self, **kwargs):
        q = {"topic": "Time value", "prompt": "What does discounting do?", "type": "mcq",
             "choices": ["Computes present value", "Computes future value"], "correct_index": 0,
             "answer": "EXPECTED_ANSWER present value", "explanation": "EXPECTED_EXPLANATION discounting reverses compounding.",
             "difficulty": "application", "citations": [{"path": self.path, "locator": "Page 1",
                 "quote": "Discounting converts future cash flows to present value."}]}
        q.update(kwargs)
        return q

    def saved(self, plan=None, question=None):
        plan = plan or self.plan()
        saved = exams.save_questions(self.lib, plan["id"], [question or self.question()])
        self.assertTrue(saved["ok"], saved)
        return plan["id"], saved["question_ids"][0]

    def test_exact_course_resolution_and_ambiguity(self):
        self.assertFalse(exams.create_plan(self.lib, "Fin", "Exam")["ok"])
        self.assertFalse(exams.create_plan(self.lib, "", "Exam")["ok"])
        for selector in ("1", "FIN101", "Finance", self.folder):
            result = exams.create_plan(self.lib, selector, "Exam")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["plan"]["course_folder"], self.folder)

    def test_duplicate_exact_names_require_disambiguation(self):
        state = self.lib.load_state()
        state["courses"]["2"]["name"] = "Finance"
        self.lib.save_state(state)
        self.assertFalse(exams.create_plan(self.lib, "Finance", "Exam")["ok"])
        self.assertTrue(exams.create_plan(self.lib, self.folder, "Exam")["ok"])

    def test_module_selection_keeps_reference_scope_and_excludes_other_module(self):
        plan = self.plan(modules=[1, 1])
        paths = {r["path"] for r in plan["inventory"]}
        self.assertIn(self.path, paths)
        self.assertIn(self.folder + "/Syllabus.md", paths)
        self.assertNotIn(self.path2, paths)
        self.assertEqual(plan["selected_modules"], [1])
        self.assertEqual(plan["scope_status"], "student_selected_unconfirmed")
        self.assertIn("confirm", plan["scope_note_label"].lower())

    def test_shared_reading_linked_in_later_selected_module_is_in_inventory(self):
        reading = self.lib.root / self.folder / "Modules/01 - Time value/shared.md"
        reading.write_text(self.source.read_text())
        for name in ("01 - Time value", "02 - Risk"):
            contents = self.lib.root / self.folder / "Modules" / name / "_Module Contents.md"
            contents.write_text("# Module\n\n[Shared reading](../01%20-%20Time%20value/shared.md)\n")
        plan = self.plan(modules=[2])
        shared = reading.relative_to(self.lib.root).as_posix()
        self.assertIn(shared, {r["path"] for r in plan["inventory"]})
        row = next(r for r in plan["inventory"] if r["path"] == shared)
        self.assertEqual(row["section"], "Modules/02 - Risk")
        q = self.question(citations=[{"path": shared, "locator": "Page 1", "quote": "Discounting"}])
        self.assertTrue(exams.save_questions(self.lib, plan["id"], [q])["ok"])

    def test_sibling_course_directory_symlink_never_exposes_inventory(self):
        other_files = self.lib.root / self.other / "Files"
        other_files.mkdir()
        (other_files / "private.md").write_text("# PRIVATE_OTHER_COURSE_TITLE\n\n## PRIVATE_LOCATOR\n\nPrivate other course content.\n")
        (self.lib.root / self.folder / "Files").symlink_to(other_files, target_is_directory=True)
        plan = self.plan()
        self.assertNotIn("PRIVATE_", json.dumps(plan))
        self.assertFalse(any(r["path"].startswith(self.other + "/") for r in plan["inventory"]))

    def test_global_reading_exclusive_to_unselected_module_is_excluded(self):
        files = self.lib.root / self.folder / "Files"
        files.mkdir()
        reading = files / "only-risk.md"
        reading.write_text("# Risk reading\n\n## [Page 1]\n\nDiversification reduces firm-specific risk.\n")
        contents = self.lib.root / self.folder / "Modules/02 - Risk/_Module Contents.md"
        contents.write_text("[Risk reading](../../Files/only-risk.md)\n")
        plan = self.plan(modules=[1])
        self.assertNotIn(reading.relative_to(self.lib.root).as_posix(), {r["path"] for r in plan["inventory"]})
        all_course = self.plan()
        self.assertIn(reading.relative_to(self.lib.root).as_posix(), {r["path"] for r in all_course["inventory"]})

    def test_invalid_modules_and_exam_dates(self):
        for modules in ([0], [3], [True], ["1"], "1"):
            self.assertFalse(exams.create_plan(self.lib, self.folder, "Exam", modules=modules)["ok"])
        for value in ("tomorrow", "2026-02-30", "2026-1-2"):
            self.assertFalse(exams.create_plan(self.lib, self.folder, "Exam", exam_date=value)["ok"])
        self.assertEqual(self.plan(exam_date="2026-10-15")["exam_date"], "2026-10-15")

    def test_cross_course_and_out_of_selected_module_citations_rejected(self):
        plan = self.plan(modules=[1])
        for path, quote in ((self.other_path, "Discounting converts future cash flows to present value."),
                            (self.path2, "Diversification reduces firm-specific risk.")):
            question = self.question(citations=[{"path": path, "locator": "Page 1", "quote": quote}])
            result = exams.save_questions(self.lib, plan["id"], [question])
            self.assertFalse(result["ok"], result)
        self.assertEqual(exams.get_plan(self.lib, plan["id"])["plan"]["questions"], [])

    def test_exact_quote_and_locator_checked_without_answer_verification_claim(self):
        plan = self.plan()
        for locator, quote in (("Page 2", "Discounting converts future cash flows to present value."),
                               ("Page 1", "Discounting always earns more money."),
                               ("Page 100", "Discounting")):
            question = self.question(citations=[{"path": self.path, "locator": locator, "quote": quote}])
            result = exams.save_questions(self.lib, plan["id"], [question])
            self.assertFalse(result["ok"], result)
            self.assertIn("evidence", result["message"].lower())
        saved = exams.save_questions(self.lib, plan["id"], [self.question()])
        self.assertTrue(saved["ok"], saved)
        self.assertIn("answer not independently verified", saved["evidence_note"].lower())

    def test_generated_study_notes_cannot_be_cited(self):
        notes = self.lib.root / self.folder / "Study Notes" / "summary.md"
        notes.parent.mkdir()
        notes.write_text(self.source.read_text())
        plan = self.plan()
        question = self.question(citations=[{"path": notes.relative_to(self.lib.root).as_posix(), "locator": "Page 1", "quote": "Discounting"}])
        self.assertFalse(exams.save_questions(self.lib, plan["id"], [question])["ok"])

    def test_all_or_nothing_batch_does_not_write_partial_questions(self):
        plan = self.plan()
        question = self.question(citations=[])
        result = exams.save_questions(self.lib, plan["id"], [self.question(), question])
        self.assertFalse(result["ok"])
        self.assertEqual(exams.get_plan(self.lib, plan["id"])["plan"]["questions"], [])

    def test_duplicates_preserve_ids_and_progress(self):
        plan_id, qid = self.saved()
        attempt = exams.record_attempt(self.lib, plan_id, qid, choice_index=0)
        self.assertTrue(attempt["ok"], attempt)
        saved = exams.save_questions(self.lib, plan_id, [self.question(), self.question()])
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["added"], 0)
        self.assertEqual(saved["skipped_duplicates"], 2)
        self.assertEqual(saved["plan"]["questions"][0]["id"], qid)
        self.assertEqual(len(saved["plan"]["attempts"]), 1)

    def test_public_redaction_always_and_reveal_only_response(self):
        plan_id, qid = self.saved()
        public = exams.get_plan(self.lib, plan_id, include_answers=False)
        question = public["plan"]["questions"][0]
        for field in ("answer", "explanation", "correct_index"):
            self.assertNotIn(field, question)
        self.assertNotIn("quote", question["citations"][0])
        self.assertNotIn("EXPECTED_", json.dumps(public))
        revealed = exams.reveal_question(self.lib, plan_id, qid)
        self.assertTrue(revealed["ok"], revealed)
        self.assertIn("EXPECTED_ANSWER", revealed["question"]["answer"])
        public = exams.get_plan(self.lib, plan_id, include_answers=False)
        self.assertNotIn("EXPECTED_", json.dumps(public))
        self.assertTrue(public["plan"]["questions"][0]["revealed"])

    def test_multiple_choice_server_score_cannot_be_self_forged(self):
        plan_id, qid = self.saved()
        bad = exams.record_attempt(self.lib, plan_id, qid, choice_index=1, self_rating="good")
        self.assertFalse(bad["ok"])
        bad = exams.record_attempt(self.lib, plan_id, qid, choice_index=1, correct=True)
        self.assertFalse(bad["ok"])
        attempt = exams.record_attempt(self.lib, plan_id, qid, choice_index=1)
        self.assertTrue(attempt["ok"], attempt)
        self.assertFalse(attempt["attempt"]["correct"])
        self.assertEqual(attempt["plan"]["metrics"]["first_attempt_mcq_accuracy"], 0)
        self.assertNotIn("mastery", attempt["plan"]["metrics"])

    def test_invalid_choice_indices_and_booleans(self):
        plan_id, qid = self.saved()
        for index in (-1, 2, True, "0", 0.0):
            self.assertFalse(exams.record_attempt(self.lib, plan_id, qid, choice_index=index)["ok"])
        self.assertEqual(exams.get_plan(self.lib, plan_id)["plan"]["attempts"], [])

    def test_short_answer_requires_reveal_and_self_report(self):
        plan_id, qid = self.saved(question=self.question(type="short_answer"))
        self.assertFalse(exams.record_attempt(self.lib, plan_id, qid, answer="present value", self_rating="good")["ok"])
        self.assertTrue(exams.reveal_question(self.lib, plan_id, qid)["ok"])
        for rating in ("", "correct", "mastered"):
            self.assertFalse(exams.record_attempt(self.lib, plan_id, qid, answer="my explanation", self_rating=rating)["ok"])
        attempt = exams.record_attempt(self.lib, plan_id, qid, answer="MY_PRIVATE_WRITTEN_ANSWER", self_rating="hard")
        self.assertTrue(attempt["ok"], attempt)
        self.assertIsNone(attempt["attempt"]["correct"])
        self.assertEqual(attempt["attempt"]["mode"], "self_report")
        self.assertNotIn("MY_PRIVATE_WRITTEN_ANSWER", json.dumps(attempt))
        stored = exams.get_plan(self.lib, plan_id)["plan"]
        self.assertEqual(stored["attempts"][0]["answer"], "MY_PRIVATE_WRITTEN_ANSWER")
        self.assertIsNone(stored["metrics"]["first_attempt_mcq_accuracy"])

    def test_assisted_mcq_excluded_from_unassisted_accuracy(self):
        plan_id, qid = self.saved()
        exams.reveal_question(self.lib, plan_id, qid)
        result = exams.record_attempt(self.lib, plan_id, qid, choice_index=0)
        self.assertTrue(result["attempt"]["assisted"])
        self.assertEqual(result["plan"]["metrics"]["first_attempt_mcq_count"], 0)
        self.assertIsNone(result["plan"]["metrics"]["recent_mcq_accuracy"])

    def test_spaced_review_schedule_and_due_priority(self):
        plan_id, qid = self.saved()
        missed = exams.record_attempt(self.lib, plan_id, qid, choice_index=1)
        self.assertEqual(missed["attempt"]["next_review_at"], "2026-10-05T12:10:00Z")
        self.at += timedelta(minutes=10)
        plan = exams.get_plan(self.lib, plan_id)["plan"]
        self.assertEqual(plan["review_queue"][0]["reason"], "due")
        right = exams.record_attempt(self.lib, plan_id, qid, choice_index=0)
        self.assertEqual(right["attempt"]["next_review_at"], "2026-10-06T12:10:00Z")
        self.at += timedelta(days=1)
        right = exams.record_attempt(self.lib, plan_id, qid, choice_index=0)
        self.assertEqual(right["attempt"]["next_review_at"], "2026-10-09T12:10:00Z")

    def test_rapid_retries_do_not_advance_review_streak(self):
        plan_id, qid = self.saved()
        for _ in range(10):
            self.assertTrue(exams.record_attempt(self.lib, plan_id, qid, choice_index=0)["ok"])
        plan = exams.get_plan(self.lib, plan_id)["plan"]
        self.assertEqual(plan["questions"][0]["success_streak"], 1)
        self.assertEqual(plan["questions"][0]["next_review_at"], "2026-10-06T12:00:00Z")

    def test_source_changes_invalidate_question_and_allow_new_regeneration(self):
        plan_id, qid = self.saved()
        exams.record_attempt(self.lib, plan_id, qid, choice_index=0)
        self.source.write_text(self.source.read_text() + "\nNew instructor example.\n")
        plan = exams.get_plan(self.lib, plan_id)["plan"]
        self.assertEqual(plan["questions"][0]["status"], "stale")
        self.assertTrue(plan["scope_changed"])
        self.assertEqual(plan["review_queue"], [])
        self.assertIsNone(plan["metrics"]["first_attempt_mcq_accuracy"])
        self.assertEqual(plan["metrics"]["stale_attempts_excluded"], 1)
        self.assertFalse(exams.record_attempt(self.lib, plan_id, qid, choice_index=0)["ok"])
        self.assertFalse(exams.reveal_question(self.lib, plan_id, qid)["ok"])
        regenerated = exams.save_questions(self.lib, plan_id, [self.question()])
        self.assertTrue(regenerated["ok"], regenerated)
        self.assertNotEqual(regenerated["question_ids"][0], qid)
        self.assertEqual(len(regenerated["plan"]["attempts"]), 1)
        self.assertEqual([q["status"] for q in regenerated["plan"]["questions"]], ["stale", "current"])

    def test_source_change_during_reveal_returns_no_answer_and_preserves_store(self):
        plan_id, qid = self.saved()
        store = self.lib.meta / "exams.json"
        before = store.read_bytes()
        real_public = exams._public

        def update_source_then_check(*args, **kwargs):
            self.source.write_text(self.source.read_text() + "\nNew instructor example.\n")
            return real_public(*args, **kwargs)

        with patch.object(exams, "_public", side_effect=update_source_then_check):
            result = exams.reveal_question(self.lib, plan_id, qid)
        self.assertFalse(result["ok"], result)
        self.assertNotIn("question", result)
        self.assertNotIn("EXPECTED_", json.dumps(result))
        self.assertEqual(store.read_bytes(), before)
        self.assertFalse(exams.get_plan(self.lib, plan_id)["plan"]["questions"][0]["revealed"])

    def test_source_change_during_attempt_does_not_commit_score_or_review_date(self):
        source_text = self.source.read_text()
        real_public = exams._public
        for typ in ("mcq", "short_answer"):
            with self.subTest(type=typ):
                self.source.write_text(source_text)
                plan_id, qid = self.saved(question=self.question(type=typ))
                kwargs = {"choice_index": 0}
                if typ == "short_answer":
                    self.assertTrue(exams.reveal_question(self.lib, plan_id, qid)["ok"])
                    kwargs = {"answer": "My explanation", "self_rating": "good"}
                store = self.lib.meta / "exams.json"
                before = store.read_bytes()

                def update_source_then_check(*args, **kwargs):
                    self.source.write_text(source_text + "\nNew instructor example.\n")
                    return real_public(*args, **kwargs)

                with patch.object(exams, "_public", side_effect=update_source_then_check):
                    result = exams.record_attempt(self.lib, plan_id, qid, **kwargs)
                self.assertFalse(result["ok"], result)
                self.assertNotIn("attempt", result)
                self.assertNotIn("feedback", result)
                self.assertEqual(store.read_bytes(), before)

    def test_source_change_during_question_batch_does_not_save_any_questions(self):
        plan = self.plan()
        store = self.lib.meta / "exams.json"
        before = store.read_bytes()
        other = self.question(prompt="What does diversification reduce?", citations=[{
            "path": self.path2, "locator": "Page 1", "quote": "Diversification reduces firm-specific risk."}])
        real_public = exams._public

        def update_source_then_check(*args, **kwargs):
            self.source.write_text(self.source.read_text() + "\nNew instructor example.\n")
            return real_public(*args, **kwargs)

        with patch.object(exams, "_public", side_effect=update_source_then_check):
            result = exams.save_questions(self.lib, plan["id"], [self.question(), other])
        self.assertFalse(result["ok"], result)
        self.assertNotIn("question_ids", result)
        self.assertEqual(store.read_bytes(), before)

    def test_source_deletion_invalidates_question(self):
        plan_id, qid = self.saved()
        self.source.unlink()
        plan = exams.get_plan(self.lib, plan_id)["plan"]
        self.assertTrue(plan["scope_changed"])
        self.assertEqual(plan["questions"][0]["status"], "stale")
        self.assertFalse(exams.record_attempt(self.lib, plan_id, qid, choice_index=0)["ok"])

    def test_source_restriction_invalidates_question(self):
        plan_id, qid = self.saved()
        self.source.write_text("---\nsync_status: restricted\n---\n" + self.source.read_text())
        plan = exams.get_plan(self.lib, plan_id)["plan"]
        self.assertEqual(plan["questions"][0]["status"], "stale")
        self.assertFalse(exams.save_questions(self.lib, plan_id, [self.question()])["ok"])

    def test_original_binary_change_invalidates_even_unchanged_sidecar(self):
        original = self.source.with_name("lecture.pdf")
        original.write_bytes(b"first version")
        sidecar = original.with_name(original.name + ".md")
        sidecar.write_text(front_matter({"source_file": original.name, "source_sha256": file_digest(original)})
                           + self.source.read_text())
        path = sidecar.relative_to(self.lib.root).as_posix()
        question = self.question(citations=[{"path": path, "locator": "Page 1", "quote": "Discounting"}])
        plan_id, qid = self.saved(question=question)
        original.write_bytes(b"later version")
        self.assertEqual(exams.get_plan(self.lib, plan_id)["plan"]["questions"][0]["status"], "stale")

    def test_batch_and_public_request_share_source_hash_but_next_request_rechecks(self):
        from superstudent import evidence
        original = self.source.with_name("lecture.pdf")
        original.write_bytes(b"first version")
        sidecar = original.with_name(original.name + ".md")
        sidecar.write_text(front_matter({"source_file": original.name, "source_sha256": file_digest(original)})
                           + self.source.read_text())
        path = sidecar.relative_to(self.lib.root).as_posix()
        plan = self.plan()
        q = self.question(citations=[{"path": path, "locator": "Page 1", "quote": "Discounting"}])
        other = dict(q, prompt="A second time value question?")
        with patch.object(evidence, "file_digest", wraps=evidence.file_digest) as digest:
            saved = exams.save_questions(self.lib, plan["id"], [q, other])
            self.assertTrue(saved["ok"], saved)
            self.assertEqual(digest.call_count, 1)
            public = exams.get_plan(self.lib, plan["id"], include_answers=False)
            self.assertTrue(public["ok"], public)
            self.assertEqual(digest.call_count, 2)
        original.write_bytes(b"later version")
        public = exams.get_plan(self.lib, plan["id"], include_answers=False)
        self.assertEqual([q["status"] for q in public["plan"]["questions"]], ["stale", "stale"])

    def test_new_material_warns_scope_review_without_invalidating_other_source(self):
        plan_id, qid = self.saved()
        added = self.source.with_name("added.md")
        added.write_text("# Added\n\n## [Page 1]\n\nNew lecture material.\n")
        plan = exams.get_plan(self.lib, plan_id)["plan"]
        self.assertTrue(plan["scope_changed"])
        self.assertEqual(plan["questions"][0]["status"], "current")
        q = self.question(citations=[{"path": added.relative_to(self.lib.root).as_posix(), "locator": "Page 1", "quote": "New lecture material."}])
        self.assertFalse(exams.save_questions(self.lib, plan_id, [q])["ok"])

    def test_source_reference_coverage_identifies_uncited_and_stale_sources(self):
        plan_id, qid = self.saved()
        public = exams.get_plan(self.lib, plan_id, include_answers=False)["plan"]
        coverage = public["coverage"]
        self.assertEqual(coverage["available_sources"], 3)
        self.assertEqual(coverage["cited_sources"], 1)
        self.assertIn(self.path2, coverage["uncited_source_paths"])
        self.assertNotIn(self.path, coverage["uncited_source_paths"])
        self.assertEqual(coverage["difficulty_counts"], {"recall": 0, "application": 1, "transfer": 0})
        self.assertIn("not exhaustive topic coverage", coverage["meaning"])
        self.source.write_text(self.source.read_text() + "\nUpdated example.\n")
        changed = exams.get_plan(self.lib, plan_id, include_answers=False)["plan"]
        self.assertEqual(changed["coverage"]["cited_sources"], 0)
        self.assertEqual(changed["coverage"]["current_questions"], 0)
        self.assertIn(self.path, changed["coverage"]["uncited_source_paths"])
        self.assertEqual(changed["coverage"]["difficulty_counts"]["application"], 0)
        self.assertTrue(changed["scope_changed"])

    def test_unavailable_sources_excluded_from_coverage_denominator(self):
        plan = self.plan()
        (self.lib.root / self.path2).write_text("---\nsync_status: restricted\n---\n# Risk\n\n## [Page 1]\n\nDiversification reduces risk.\n")
        public = exams.get_plan(self.lib, plan["id"])["plan"]
        self.assertEqual(public["coverage"]["available_sources"], 2)
        self.assertNotIn(self.path2, public["coverage"]["uncited_source_paths"])
        self.assertTrue(public["scope_warnings"])

    def test_module_snapshot_change_warns_scope_review(self):
        plan = self.plan(modules=[1])
        snap = self.lib.load_snapshot("1")
        snap["modules"][0]["name"] = "Revised name"
        self.lib.save_snapshot("1", snap)
        self.assertTrue(exams.get_plan(self.lib, plan["id"])["plan"]["scope_changed"])

    def test_list_plans_is_scoped_and_never_contains_answers(self):
        self.saved()
        self.plan()
        self.assertTrue(exams.create_plan(self.lib, self.other, "Other exam")["ok"])
        all_plans = exams.list_plans(self.lib)
        self.assertEqual(len(all_plans["plans"]), 3)
        finance = exams.list_plans(self.lib, "FIN101")
        self.assertEqual(len(finance["plans"]), 2)
        self.assertNotIn("EXPECTED_", json.dumps(all_plans))
        self.assertNotIn("questions", all_plans["plans"][0])

    def test_thread_concurrency_preserves_every_attempt(self):
        plan_id, qid = self.saved()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: exams.record_attempt(self.lib, plan_id, qid, choice_index=0), range(32)))
        self.assertTrue(all(r["ok"] for r in results), results)
        attempts = exams.get_plan(self.lib, plan_id)["plan"]["attempts"]
        self.assertEqual(len(attempts), 32)
        self.assertEqual(len({a["id"] for a in attempts}), 32)

    def test_process_concurrency_preserves_every_plan(self):
        repo = Path(__file__).resolve().parents[1]
        code = "import sys; from pathlib import Path; from superstudent.exams import create_plan; from superstudent.library import Library; r=create_plan(Library(Path(sys.argv[1])),sys.argv[2],sys.argv[3]); sys.exit(0 if r['ok'] else 1)"
        env = dict(os.environ, PYTHONPATH=str(repo))
        procs = [subprocess.Popen([sys.executable, "-c", code, str(self.lib.root), self.folder, f"Exam {i}"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for i in range(6)]
        for proc in procs:
            _, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, err.decode())
        self.assertEqual(len(exams.list_plans(self.lib)["plans"]), 6)

    def test_corrupt_json_is_not_silently_discarded(self):
        store = self.lib.meta / "exams.json"
        for content in ("{broken", "[]", '{"version":999,"plans":{}}', '{"version":1,"plans":{"x":{}}}'):
            store.write_text(content)
            result = exams.create_plan(self.lib, self.folder, "New exam")
            self.assertFalse(result["ok"], result)
            self.assertEqual(store.read_text(), content)

    def test_damaged_question_fields_rejected_before_any_write(self):
        plan_id, qid = self.saved()
        store = self.lib.meta / "exams.json"
        data = json.loads(store.read_text())
        del data["plans"][plan_id]["questions"][0]["topic"]
        store.write_text(json.dumps(data))
        before = store.read_bytes()
        result = exams.save_questions(self.lib, plan_id, [self.question(prompt="A different question?")])
        self.assertFalse(result["ok"], result)
        self.assertEqual(store.read_bytes(), before)

    def test_response_failure_does_not_commit_mutation(self):
        plan = self.plan()
        store = self.lib.meta / "exams.json"
        before = store.read_bytes()
        with patch.object(exams, "_public", side_effect=KeyError("damaged response")):
            result = exams.save_questions(self.lib, plan["id"], [self.question()])
        self.assertFalse(result["ok"])
        self.assertEqual(store.read_bytes(), before)

    def test_outside_store_and_lock_symlinks_are_rejected(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("OUTSIDE_PRIVATE")
        for name in ("exams.json", "exams.lock"):
            link = self.lib.meta / name
            if link.exists():
                link.unlink()
            link.symlink_to(outside)
            result = exams.create_plan(self.lib, self.folder, "Exam")
            self.assertFalse(result["ok"], result)
            self.assertEqual(outside.read_text(), "OUTSIDE_PRIVATE")
            link.unlink()

    def test_course_and_citation_path_escapes_fail(self):
        self.assertFalse(exams.create_plan(self.lib, "../../outside", "Exam")["ok"])
        plan = self.plan()
        for path in ("../../outside.md", "/etc/passwd", self.path.replace("/", "\\")):
            q = self.question(citations=[{"path": path, "locator": "Page 1", "quote": "Discounting"}])
            self.assertFalse(exams.save_questions(self.lib, plan["id"], [q])["ok"])

    def test_cross_course_symlink_cannot_supply_scope_or_evidence(self):
        target = self.source.with_name("cross.md")
        target.symlink_to(self.lib.root / self.other_path)
        plan = self.plan()
        q = self.question(citations=[{"path": target.relative_to(self.lib.root).as_posix(), "locator": "Page 1", "quote": "Discounting"}])
        self.assertFalse(exams.save_questions(self.lib, plan["id"], [q])["ok"])

    def test_bounded_input_and_store_fail_without_overwriting(self):
        plan = self.plan()
        stored = (self.lib.meta / "exams.json").read_bytes()
        for qs in ([], [self.question()] * 51, [self.question(prompt="X" * 6001)],
                   [self.question(correct_index=True)], [self.question(choices=["same", "Same"])],
                   [self.question(difficulty="mastered")]):
            self.assertFalse(exams.save_questions(self.lib, plan["id"], qs)["ok"])
            self.assertEqual((self.lib.meta / "exams.json").read_bytes(), stored)
        with patch.object(exams, "MAX_STORE_BYTES", len(stored) + 5):
            self.assertFalse(exams.save_questions(self.lib, plan["id"], [self.question()])["ok"])
        self.assertEqual((self.lib.meta / "exams.json").read_bytes(), stored)

    def test_attempt_limit_does_not_discard_existing_history(self):
        plan_id, qid = self.saved()
        self.assertTrue(exams.record_attempt(self.lib, plan_id, qid, choice_index=1)["ok"])
        before = (self.lib.meta / "exams.json").read_bytes()
        with patch.object(exams, "MAX_ATTEMPTS", 1):
            self.assertFalse(exams.record_attempt(self.lib, plan_id, qid, choice_index=0)["ok"])
        self.assertEqual((self.lib.meta / "exams.json").read_bytes(), before)

    def test_unknown_ids_and_wrong_question_plan_fail(self):
        plan_id, qid = self.saved()
        other = self.plan()
        self.assertFalse(exams.get_plan(self.lib, "missing")["ok"])
        self.assertFalse(exams.reveal_question(self.lib, other["id"], qid)["ok"])
        self.assertFalse(exams.record_attempt(self.lib, other["id"], qid, choice_index=0)["ok"])
        self.assertFalse(exams.get_plan(self.lib, plan_id, include_answers="true")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
