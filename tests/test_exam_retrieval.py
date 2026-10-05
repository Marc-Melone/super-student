"""Course retrieval and quoted-source evidence checks; temporary libraries, no Canvas or AI access.

Run: python tests/test_exam_retrieval.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from superstudent.evidence import recheck_evidence, source_fingerprint, validate_evidence
from superstudent.index import search, update_index
from superstudent.library import Library
from superstudent.util import REMOVED_DIR, file_digest, front_matter


class CourseFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ss-exam-retrieval-")
        self.scratch = Path(self.temp.name)
        self.lib = Library(self.scratch / "library")
        self.lib.ensure()
        self.folder = "Fall/Finance_A"
        self.other = "Fall/FinanceXA"
        for folder in (self.folder, self.other):
            (self.lib.root / folder / "Files").mkdir(parents=True)
        self.lib.save_state({"courses": {"1": {"folder": self.folder, "name": "Finance", "items": {}},
                                         "2": {"folder": self.other, "name": "Other Finance", "items": {}}}})

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, body, *, folder=None, meta=None):
        path = self.lib.root / (folder or self.folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(front_matter(meta or {"type": "page", "title": path.stem}) + body, encoding="utf-8")
        return path.relative_to(self.lib.root).as_posix()


class RetrievalTests(CourseFixture):
    def test_supplied_alternate_finds_course_term_beyond_primary_partial_hits(self):
        distractor = self.write("Files/market.md", "# Markets\n\n## Trading\n\nBond markets trade bonds every day.")
        target = self.write("Files/duration.md", "# Interest risk\n\n## Definition\n\nModified duration estimates percentage price change for a yield change.")
        update_index(self.lib)
        self.assertEqual(search(self.lib, "bond vulnerability", course=self.folder, limit=1)[0]["path"], distractor)
        hits = search(self.lib, "bond vulnerability", course=self.folder, alternate_queries=["modified duration"], limit=1)
        self.assertEqual(hits[0]["path"], target)
        self.assertEqual(hits[0]["match"], "all words")
        self.assertEqual(hits[0]["matched_queries"], ["modified duration"])

    def test_fusion_prioritizes_source_relevant_to_both_phrasings(self):
        self.write("Files/duration.md", "# Duration\n\n## Definition\n\nDuration duration duration measures rate risk.")
        self.write("Files/convexity.md", "# Convexity\n\n## Definition\n\nConvexity convexity convexity measures curvature.")
        shared = self.write("Files/risk.md", "# Combined risk\n\n## Compare\n\nDuration measures the first derivative and convexity the second derivative.")
        update_index(self.lib)
        hits = search(self.lib, "duration", course=self.folder, alternate_queries=["convexity"])
        self.assertEqual(hits[0]["path"], shared)
        self.assertEqual(set(hits[0]["matched_queries"]), {"duration", "convexity"})
        self.assertGreater(hits[0]["retrieval_score"], hits[1]["retrieval_score"])

    def test_duplicate_query_does_not_inflate_rank(self):
        self.write("Files/risk.md", "# Risk\n\n## Duration\n\nDuration measures rate risk.")
        update_index(self.lib)
        once = search(self.lib, "duration")
        twice = search(self.lib, "duration", alternate_queries=["duration", " DURATION "])
        self.assertEqual(once, twice)

    def test_empty_primary_can_use_alternate(self):
        rel = self.write("Files/risk.md", "# Risk\n\n## Definition\n\nConvexity measures curvature.")
        update_index(self.lib)
        self.assertEqual(search(self.lib, "", alternate_queries=["convexity"])[0]["path"], rel)

    def test_alternates_are_bounded(self):
        for queries in (["a"] * 5, [""], [42], ["x" * 513], ("duration",)):
            with self.subTest(queries=queries), self.assertRaises(ValueError):
                search(self.lib, "duration", alternate_queries=queries)

    def test_per_document_cap_applies_after_fusion(self):
        rel = self.write("Files/risk.md", "# Risk\n\n## Duration\n\nDuration risk.\n\n## Convexity\n\nConvexity risk.")
        update_index(self.lib)
        hits = search(self.lib, "duration", alternate_queries=["convexity"], per_doc=1)
        self.assertEqual(sum(h["path"] == rel for h in hits), 1)

    def test_current_material_precedes_removed_exact_alternate(self):
        current = self.write("Files/current.md", "# Current\n\n## Trading\n\nBond markets trade daily.")
        self.write(f"{REMOVED_DIR}/old.md", "# Old\n\n## Definition\n\nModified duration estimates rate risk.")
        update_index(self.lib)
        hits = search(self.lib, "bond vulnerability", alternate_queries=["modified duration"], limit=1)
        self.assertEqual(hits[0]["path"], current)
        self.assertFalse(hits[0]["removed"])

    def test_freshness_information_is_retained(self):
        self.write("Files/locked.md", "# Risk\n\n## Definition\n\nDuration estimates rate risk.",
                   meta={"type": "page", "sync_status": "restricted"})
        update_index(self.lib)
        hit = search(self.lib, "duration", alternate_queries=["rate risk"])[0]
        self.assertTrue(hit["stale"])
        self.assertEqual(hit["status"], "restricted")

    def test_exact_course_with_sql_wildcard_name_is_contained(self):
        rel = self.write("Files/ours.md", "# Ours\n\n## Risk\n\nDuration risk.")
        self.write("Files/theirs.md", "# Theirs\n\n## Risk\n\nDuration risk.", folder=self.other)
        update_index(self.lib)
        self.assertEqual([h["path"] for h in search(self.lib, "duration", course=self.folder)], [rel])


class EvidenceTests(CourseFixture):
    def setUp(self):
        super().setUp()
        self.path = self.write("Files/lesson.md", "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.\n\n## [Page 2]\n\nConvexity measures curvature.")

    def citation(self, **changes):
        value = {"path": self.path, "locator": "Page 1", "quote": "Modified duration estimates rate risk."}
        value.update(changes)
        return value

    def validate(self, **changes):
        return validate_evidence(self.lib, self.folder, [self.citation(**changes)])

    def saved(self):
        result = self.validate()
        self.assertTrue(result["valid"], result)
        return result["records"]

    def test_exact_quote_normalizes_case_and_whitespace(self):
        result = self.validate(locator="[PAGE 1]", quote="  modified\nDURATION estimates   RATE risk. ")
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["records"][0]["locator"], "Page 1")
        self.assertEqual(result["records"][0]["source_status"], "current")
        self.assertEqual(len(result["records"][0]["source_fingerprint"]), 64)
        self.assertTrue(recheck_evidence(self.lib, self.folder, result["records"])["valid"])

    def test_fabricated_quote_rejected(self):
        self.assertEqual(self.validate(quote="Duration makes every investment risk free.")["errors"][0]["code"], "quote_mismatch")

    def test_quote_from_other_section_rejected(self):
        self.assertFalse(self.validate(locator="Page 2")["valid"])

    def test_locator_requires_exact_match(self):
        self.path = self.write("Files/titled.md", "# Lesson\n\n## [Page 1] Duration\n\nModified duration estimates rate risk.")
        self.assertEqual(self.validate()["errors"][0]["code"], "missing_locator")
        self.assertTrue(self.validate(locator="Page 1 Duration")["valid"])

    def test_canonical_locator_collision_is_ambiguous(self):
        self.path = self.write("Files/repeated.md", "# Lesson\n\n## Example\n\nFirst.\n\n## Example\n\nSecond.\n\n## Example (2)\n\nThird.")
        result = self.validate(locator="Example (2)", quote="Second.")
        self.assertEqual(result["errors"][0]["code"], "ambiguous_locator")

    def test_ordinary_numbered_duplicate_is_precise(self):
        self.path = self.write("Files/repeated.md", "# Lesson\n\n## Example\n\nFirst.\n\n## Example\n\nSecond.")
        self.assertTrue(self.validate(locator="Example (2)", quote="Second.")["valid"])
        self.assertFalse(self.validate(locator="Example", quote="Second.")["valid"])

    def test_pre_heading_text_uses_empty_locator(self):
        self.path = self.write("Files/plain.md", "Duration estimates risk.")
        self.assertTrue(self.validate(locator="", quote="Duration estimates risk.")["valid"])
        self.assertFalse(self.validate(locator="start", quote="Duration estimates risk.")["valid"])

    def test_out_of_course_citation_rejected(self):
        outside = self.write("Files/other.md", "# Other\n\n## [Page 1]\n\nModified duration estimates rate risk.", folder=self.other)
        self.assertEqual(self.validate(path=outside)["errors"][0]["code"], "outside_course")

    def test_absolute_and_traversal_paths_rejected(self):
        for path in (str(self.lib.root / self.path), "../outside.md", self.folder + "/../FinanceXA/Files/x.md"):
            with self.subTest(path=path):
                self.assertEqual(self.validate(path=path)["errors"][0]["code"], "invalid_path")

    def test_course_must_be_exact_registered_folder(self):
        for course in ("Finance", "Fall", self.folder + "/Files"):
            with self.subTest(course=course):
                result = validate_evidence(self.lib, course, [self.citation()])
                self.assertEqual(result["errors"][0]["code"], "invalid_course")

    def test_symlink_to_another_course_rejected(self):
        target = self.lib.root / self.write("Files/other.md", "# Other\n\n## [Page 1]\n\nModified duration estimates rate risk.", folder=self.other)
        link = self.lib.root / self.folder / "Files/alias.md"
        link.symlink_to(target)
        self.assertEqual(self.validate(path=link.relative_to(self.lib.root).as_posix())["errors"][0]["code"], "outside_course")

    def test_symlink_outside_library_never_returns_private_text(self):
        target = self.scratch / "private.md"
        target.write_text("PRIVATE_OUTSIDE_CONTENT")
        link = self.lib.root / self.folder / "Files/alias.md"
        link.symlink_to(target)
        result = self.validate(path=link.relative_to(self.lib.root).as_posix())
        self.assertFalse(result["valid"])
        self.assertNotIn("PRIVATE_OUTSIDE_CONTENT", json.dumps(result))

    def test_adjacent_original_link_outside_course_rejected(self):
        self.path = self.write("Files/lesson.pdf.md", "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.")
        original = self.lib.root / self.path[:-3]
        outside = self.scratch / "private.pdf"
        outside.write_bytes(b"PRIVATE_BINARY")
        original.symlink_to(outside)
        self.assertFalse(self.validate()["valid"])

    def test_original_and_sidecar_have_same_fingerprint(self):
        self.path = self.write("Files/lesson.pdf.md", "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.",
                               meta={"type": "pdf", "source_file": "lesson.pdf"})
        (self.lib.root / self.path[:-3]).write_bytes(b"ORIGINAL")
        a = source_fingerprint(self.lib, self.path, self.folder)
        b = source_fingerprint(self.lib, self.path[:-3], self.folder)
        self.assertEqual(a, b)
        self.assertEqual(a, source_fingerprint(self.lib, self.path))
        self.assertTrue(self.validate(path=self.path[:-3])["valid"])

    def test_generated_notes_and_historical_copies_rejected(self):
        for name in ("Study Notes/lesson.md", "_Study/lesson.md", f"{REMOVED_DIR}/lesson.md", "EXAM_INTEL.md", "OUTLINE.md"):
            with self.subTest(name=name):
                rel = self.write(name, "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.")
                self.assertEqual(self.validate(path=rel)["errors"][0]["code"], "generated_source")

    def test_renamed_generated_summary_is_rejected_by_type(self):
        self.path = self.write("Files/renamed.md", "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.", meta={"type": "study_notes"})
        self.assertEqual(self.validate()["errors"][0]["code"], "generated_source")

    def test_excluded_directory_alias_cannot_bypass_source_rule(self):
        link = self.lib.root / self.folder / "Study Notes/alias.md"
        link.parent.mkdir()
        link.symlink_to(self.lib.root / self.path)
        self.assertEqual(self.validate(path=link.relative_to(self.lib.root).as_posix())["errors"][0]["code"], "generated_source")

    def test_generated_visual_description_is_not_original_evidence(self):
        self.path = self.write("Files/image.md", "# Image\n\n## [Image]\n\n> **What this shows** (described by AI): A price curve always rises.\n")
        self.assertEqual(self.validate(locator="Image", quote="A price curve always rises.")["errors"][0]["code"], "quote_mismatch")

    def test_missing_source_or_original_rejected(self):
        self.assertEqual(self.validate(path=self.folder + "/Files/missing.md")["errors"][0]["code"], "missing_source")
        self.path = self.write("Files/missing.pdf.md", "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.",
                               meta={"type": "pdf", "source_file": "missing.pdf"})
        self.assertEqual(self.validate()["errors"][0]["code"], "missing_original")

    def test_unavailable_sidecar_markers_rejected(self):
        for status in ("stale", "restricted"):
            with self.subTest(status=status):
                self.path = self.write("Files/lesson.md", "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.",
                                       meta={"type": "page", "sync_status": status})
                self.assertEqual(self.validate()["errors"][0]["code"], "unavailable_source")

    def test_canvas_state_restriction_rejected(self):
        state = self.lib.load_state()
        state["courses"]["1"]["items"] = {"page:1": {"path": "Files/lesson.md", "status": "locked"}}
        self.lib.save_state(state)
        self.assertEqual(self.validate()["errors"][0]["code"], "unavailable_source")

    def test_failed_extraction_placeholder_rejected(self):
        self.path = self.write("Files/failed.md", "# Failed\n\n_Couldn't extract text (error)._\n")
        self.assertEqual(self.validate(locator="Failed", quote="error")["errors"][0]["code"], "unavailable_source")

    def test_recheck_detects_same_size_same_mtime_text_change(self):
        records = self.saved()
        path = self.lib.root / self.path
        st = path.stat()
        path.write_text(path.read_text().replace("curvature", "curvatura"))
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(path.stat().st_size, st.st_size)
        self.assertEqual(recheck_evidence(self.lib, self.folder, records)["errors"][0]["code"], "changed_source")

    def test_recheck_detects_original_change_even_if_quote_unchanged(self):
        self.path = self.write("Files/source.pdf.md", "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.",
                               meta={"type": "pdf", "source_file": "source.pdf"})
        original = self.lib.root / self.path[:-3]
        original.write_bytes(b"FIRST")
        records = self.saved()
        st = original.stat()
        original.write_bytes(b"OTHER")
        os.utime(original, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(recheck_evidence(self.lib, self.folder, records)["errors"][0]["code"], "changed_source")

    def test_recheck_rejects_source_replaced_with_cross_course_symlink(self):
        records = self.saved()
        target = self.lib.root / self.write("Files/same.md", (self.lib.root / self.path).read_text(), folder=self.other)
        (self.lib.root / self.path).unlink()
        (self.lib.root / self.path).symlink_to(target)
        self.assertEqual(recheck_evidence(self.lib, self.folder, records)["errors"][0]["code"], "outside_course")

    def test_recheck_detects_locator_or_title_change(self):
        records = self.saved()
        path = self.lib.root / self.path
        path.write_text(path.read_text().replace("# Lesson", "# Lesson revised"))
        self.assertEqual(recheck_evidence(self.lib, self.folder, records)["errors"][0]["code"], "changed_source")

    def test_recheck_requires_saved_hash(self):
        self.assertEqual(recheck_evidence(self.lib, self.folder, [self.citation()])["errors"][0]["code"], "missing_fingerprint")

    def test_partial_validation_must_not_be_accepted(self):
        result = validate_evidence(self.lib, self.folder, [self.citation(), self.citation(quote="made up")])
        self.assertFalse(result["valid"])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["errors"][0]["index"], 1)

    def test_input_bounds(self):
        for citations in ([], [self.citation()] * 31, "not a list"):
            with self.subTest(citations=citations):
                self.assertFalse(validate_evidence(self.lib, self.folder, citations)["valid"])
        for value in (None, {}, ""):
            with self.subTest(value=value):
                self.assertFalse(self.validate(quote=value)["valid"])

    def binary_source(self):
        self.path = self.write("Files/large.pdf.md", "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.\n" + "Long original text. " * 10000,
                               meta={"type": "pdf", "source_file": "large.pdf"})
        original = self.lib.root / self.path[:-3]
        original.write_bytes(b"binary-data-" * 10000)
        return original

    def test_operation_cache_hashes_shared_original_once_across_helpers(self):
        self.binary_source()
        cache = {}
        citations = [self.citation() for _ in range(30)]
        with patch("superstudent.evidence.file_digest", wraps=file_digest) as digest:
            result = validate_evidence(self.lib, self.folder, citations, source_cache=cache)
            self.assertTrue(result["valid"], result)
            self.assertTrue(recheck_evidence(self.lib, self.folder, result["records"], source_cache=cache)["valid"])
            self.assertEqual(source_fingerprint(self.lib, self.path, self.folder, source_cache=cache),
                             result["records"][0]["source_fingerprint"])
            self.assertEqual(digest.call_count, 1)

    def test_default_cache_does_not_survive_between_operations(self):
        self.binary_source()
        with patch("superstudent.evidence.file_digest", wraps=file_digest) as digest:
            first = self.validate()
            second = self.validate()
            self.assertTrue(first["valid"] and second["valid"])
            self.assertEqual(digest.call_count, 2)

    def test_cache_stat_guard_detects_original_bytes_with_restored_mtime(self):
        original = self.binary_source()
        cache = {}
        records = validate_evidence(self.lib, self.folder, [self.citation()], source_cache=cache)["records"]
        old = original.stat()
        original.write_bytes(b"Binary-data-" * 10000)
        os.utime(original, ns=(old.st_atime_ns, old.st_mtime_ns))
        result = recheck_evidence(self.lib, self.folder, records, source_cache=cache)
        self.assertEqual(result["errors"][0]["code"], "changed_source")

    def test_cache_guard_detects_sidecar_change(self):
        cache = {}
        records = validate_evidence(self.lib, self.folder, [self.citation()], source_cache=cache)["records"]
        path = self.lib.root / self.path
        old = path.stat()
        path.write_text(path.read_text().replace("curvature", "curvatura"))
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        result = recheck_evidence(self.lib, self.folder, records, source_cache=cache)
        self.assertEqual(result["errors"][0]["code"], "changed_source")

    def test_cache_guard_reloads_restricted_sync_state(self):
        cache = {}
        records = validate_evidence(self.lib, self.folder, [self.citation()], source_cache=cache)["records"]
        state = self.lib.load_state()
        state["courses"]["1"]["items"] = {"page:1": {"path": "Files/lesson.md", "status": "locked"}}
        self.lib.save_state(state)
        result = recheck_evidence(self.lib, self.folder, records, source_cache=cache)
        self.assertEqual(result["errors"][0]["code"], "unavailable_source")

    def test_cache_guard_rejects_replaced_source_symlink(self):
        cache = {}
        records = validate_evidence(self.lib, self.folder, [self.citation()], source_cache=cache)["records"]
        original = self.lib.root / self.path
        other = self.lib.root / self.write("Files/other.md", original.read_text(), folder=self.other)
        original.unlink()
        original.symlink_to(other)
        result = recheck_evidence(self.lib, self.folder, records, source_cache=cache)
        self.assertEqual(result["errors"][0]["code"], "outside_course")

    def test_source_change_during_hash_is_rejected_and_not_cached(self):
        self.binary_source()
        path = self.lib.root / self.path
        cache = {}
        def replacing_digest(original):
            value = file_digest(original)
            path.write_text(path.read_text().replace("# Lesson", "# Changed"))
            return value
        with patch("superstudent.evidence.file_digest", side_effect=replacing_digest):
            result = validate_evidence(self.lib, self.folder, [self.citation()], source_cache=cache)
        self.assertEqual(result["errors"][0]["code"], "changed_during_read")
        self.assertFalse(any(key[0] == "source" for key in cache))


if __name__ == "__main__":
    unittest.main(verbosity=2)
