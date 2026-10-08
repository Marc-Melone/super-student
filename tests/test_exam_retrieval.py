"""Course retrieval and quoted-source evidence checks; temporary libraries, no Canvas or AI access.

Run: python tests/test_exam_retrieval.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from superstudent.evidence import recheck_evidence, source_fingerprint, validate_evidence
from superstudent.index import search, update_index
from superstudent.library import Library
from superstudent.util import REMOVED_DIR, file_digest, front_matter, parse_front_matter


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

    def write_extracted(self, name, original_bytes, body):
        original = self.lib.root / self.folder / name
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(original_bytes)
        return self.write(name + ".md", body, meta={"type": "file", "source_file": original.name,
                                                   "source_sha256": file_digest(original)})


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
        self.path = self.write_extracted("Files/lesson.pdf", b"ORIGINAL",
                                         "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.")
        a = source_fingerprint(self.lib, self.path, self.folder)
        b = source_fingerprint(self.lib, self.path[:-3], self.folder)
        self.assertEqual(a, b)
        self.assertEqual(a, source_fingerprint(self.lib, self.path))
        self.assertTrue(self.validate(path=self.path[:-3])["valid"])

    def test_legacy_extraction_without_binding_requires_refresh(self):
        original = self.lib.root / self.folder / "Files/legacy.pdf"
        original.write_bytes(b"ORIGINAL")
        for source_file in (None, original.name):
            with self.subTest(source_file=source_file):
                self.path = self.write("Files/legacy.pdf.md",
                                       "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.",
                                       meta={"type": "pdf", "source_file": source_file})
                sidecar = self.lib.root / self.path
                before = sidecar.read_bytes()
                result = self.validate()
                self.assertFalse(result["valid"])
                self.assertEqual(result["errors"][0]["code"], "unbound_extraction")
                self.assertIn("Refresh the course", result["errors"][0]["message"])
                self.assertEqual(sidecar.read_bytes(), before)
                self.assertEqual(original.read_bytes(), b"ORIGINAL")

    def test_malformed_extraction_binding_is_rejected(self):
        original = self.lib.root / self.folder / "Files/lesson.pdf"
        original.write_bytes(b"ORIGINAL")
        for bound in ("0" * 63, "0" * 65, "g" * 64, "not a hash"):
            with self.subTest(bound=bound):
                self.path = self.write("Files/lesson.pdf.md",
                                       "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.",
                                       meta={"type": "pdf", "source_file": original.name, "source_sha256": bound})
                self.assertEqual(self.validate()["errors"][0]["code"], "unbound_extraction")

    def test_adjacent_extensionless_file_also_requires_binding(self):
        original = self.lib.root / self.path[:-3]
        original.write_bytes(b"ORIGINAL WITHOUT EXTENSION")
        self.assertEqual(self.validate()["errors"][0]["code"], "unbound_extraction")
        self.path = self.write_extracted("Files/lesson", original.read_bytes(),
                                         "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.")
        self.assertTrue(self.validate()["valid"])

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
        self.path = self.write_extracted("Files/source.pdf", b"FIRST",
                                         "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.")
        original = self.lib.root / self.path[:-3]
        records = self.saved()
        st = original.stat()
        original.write_bytes(b"OTHER")
        os.utime(original, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(recheck_evidence(self.lib, self.folder, records)["errors"][0]["code"], "stale_extraction")

    def test_original_change_cannot_generate_new_evidence_from_old_text(self):
        self.path = self.write_extracted("Files/source.pdf", b"FIRST",
                                         "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.")
        self.assertTrue(self.validate()["valid"])
        (self.lib.root / self.path[:-3]).write_bytes(b"OTHER")
        result = self.validate()
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "stale_extraction")
        self.assertIn("Refresh the course", result["errors"][0]["message"])
        with self.assertRaisesRegex(ValueError, "original file changed"):
            source_fingerprint(self.lib, self.path, self.folder)

    def test_refreshed_extraction_accepts_new_evidence_but_invalidates_old_records(self):
        self.path = self.write_extracted("Files/source.pdf", b"FIRST",
                                         "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.")
        records = self.saved()
        self.path = self.write_extracted("Files/source.pdf", b"OTHER",
                                         "# Lesson revised\n\n## [Page 1]\n\nModified duration estimates rate risk.\nNew extracted text.")
        result = self.validate()
        self.assertTrue(result["valid"], result)
        self.assertNotEqual(result["records"][0]["source_fingerprint"], records[0]["source_fingerprint"])
        self.assertTrue(recheck_evidence(self.lib, self.folder, result["records"])["valid"])
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
        self.path = self.write_extracted("Files/large.pdf", b"binary-data-" * 10000,
                                         "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.\n" + "Long original text. " * 10000)
        return self.lib.root / self.path[:-3]

    def update_metadata(self, **changes):
        path = self.lib.root / self.path
        meta, body = parse_front_matter(path.read_text())
        meta.update(changes)
        path.write_text(front_matter(meta) + body)

    def archive_source(self, name="lesson.pdf"):
        archive = self.lib.root / self.folder / "Files/lessons.zip"
        archive.write_bytes(b"FIRST-ARCHIVE")
        self.path = self.write_extracted("Files/lessons.zip (unzipped)/" + name, b"ORIGINAL-MEMBER",
                                         "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.")
        self.update_metadata(source_archive=archive.relative_to(self.lib.root).as_posix(),
                             source_archive_sha256=file_digest(archive))
        return archive

    def test_bound_archive_member_accepts_current_evidence(self):
        self.archive_source()
        records = self.saved()
        self.assertTrue(recheck_evidence(self.lib, self.folder, records)["valid"])
        self.assertEqual(source_fingerprint(self.lib, self.path[:-3], self.folder), records[0]["source_fingerprint"])

    def test_archive_change_invalidates_cached_and_new_evidence(self):
        archive = self.archive_source()
        cache = {}
        records = validate_evidence(self.lib, self.folder, [self.citation()], source_cache=cache)["records"]
        self.assertEqual(len(records), 1)
        old = archive.stat()
        archive.write_bytes(b"OTHER-ARCHIVE")
        os.utime(archive, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(archive.stat().st_size, old.st_size)
        self.assertEqual(recheck_evidence(self.lib, self.folder, records, source_cache=cache)["errors"][0]["code"], "stale_extraction")
        self.assertEqual(self.validate()["errors"][0]["code"], "stale_extraction")

    def test_missing_archive_invalidates_cached_evidence(self):
        archive = self.archive_source()
        cache = {}
        records = validate_evidence(self.lib, self.folder, [self.citation()], source_cache=cache)["records"]
        archive.unlink()
        result = recheck_evidence(self.lib, self.folder, records, source_cache=cache)
        self.assertEqual(result["errors"][0]["code"], "missing_archive")
        self.assertIn("Refresh the course", result["errors"][0]["message"])

    def test_archive_binding_requires_both_relative_path_and_valid_hash(self):
        archive = self.archive_source()
        rel = archive.relative_to(self.lib.root).as_posix()
        for path, bound, code in ((rel, "", "unbound_extraction"), (rel, "g" * 64, "unbound_extraction"),
                                  ("", file_digest(archive), "unbound_extraction"),
                                  (str(archive), file_digest(archive), "invalid_path"),
                                  (self.folder + "/../FinanceXA/Files/lessons.zip", file_digest(archive), "invalid_path")):
            with self.subTest(path=path, bound=bound):
                self.update_metadata(source_archive=path, source_archive_sha256=bound)
                self.assertEqual(self.validate()["errors"][0]["code"], code)

    def test_source_archive_path_and_symlinks_cannot_escape_course(self):
        archive = self.archive_source()
        elsewhere = self.lib.root / self.other / "Files/lessons.zip"
        elsewhere.write_bytes(archive.read_bytes())
        self.update_metadata(source_archive=elsewhere.relative_to(self.lib.root).as_posix())
        self.assertEqual(self.validate()["errors"][0]["code"], "outside_course")
        self.update_metadata(source_archive=archive.relative_to(self.lib.root).as_posix())
        cache = {}
        records = validate_evidence(self.lib, self.folder, [self.citation()], source_cache=cache)["records"]
        self.assertEqual(len(records), 1)
        archive.unlink()
        archive.symlink_to(elsewhere)
        self.assertEqual(recheck_evidence(self.lib, self.folder, records, source_cache=cache)["errors"][0]["code"], "outside_course")
        archive.unlink()
        private = self.scratch / "private.zip"
        private.write_bytes(b"PRIVATE_ARCHIVE_CONTENT")
        archive.symlink_to(private)
        with patch("superstudent.evidence.file_digest", wraps=file_digest) as digest:
            result = self.validate()
        self.assertEqual(result["errors"][0]["code"], "outside_course")
        self.assertNotIn("PRIVATE_ARCHIVE_CONTENT", json.dumps(result))
        self.assertNotIn(private, [call.args[0] for call in digest.call_args_list])

    def test_locked_archive_invalidates_cached_member_evidence(self):
        self.archive_source()
        cache = {}
        records = validate_evidence(self.lib, self.folder, [self.citation()], source_cache=cache)["records"]
        state = self.lib.load_state()
        state["courses"]["1"]["items"] = {"file:1": {"path": "Files/lessons.zip", "status": "locked"}}
        self.lib.save_state(state)
        self.assertEqual(recheck_evidence(self.lib, self.folder, records, source_cache=cache)["errors"][0]["code"], "unavailable_source")

    def test_operation_cache_hashes_shared_archive_once_for_multiple_members(self):
        archive = self.archive_source()
        first = self.citation()
        self.archive_source("second.pdf")
        second = self.citation()
        cache = {}
        with patch("superstudent.evidence.file_digest", wraps=file_digest) as digest:
            result = validate_evidence(self.lib, self.folder, [first, second] * 15, source_cache=cache)
            self.assertTrue(result["valid"], result)
            self.assertTrue(recheck_evidence(self.lib, self.folder, result["records"], source_cache=cache)["valid"])
            self.assertEqual(digest.call_count, 3)
            self.assertEqual(sum(call.args[0] == archive for call in digest.call_args_list), 1)

    def test_freshly_unpacked_archive_requires_new_saved_evidence(self):
        archive = self.archive_source()
        records = self.saved()
        archive.write_bytes(b"OTHER-ARCHIVE")
        self.update_metadata(source_archive_sha256=file_digest(archive))
        result = self.validate()
        self.assertTrue(result["valid"], result)
        self.assertNotEqual(result["records"][0]["source_fingerprint"], records[0]["source_fingerprint"])
        self.assertEqual(recheck_evidence(self.lib, self.folder, records)["errors"][0]["code"], "changed_source")

    def test_archive_change_during_hash_is_rejected_and_not_cached(self):
        archive = self.archive_source()
        cache = {}
        def replacing_digest(path):
            value = file_digest(path)
            if path == archive:
                archive.write_bytes(b"OTHER-ARCHIVE")
            return value
        with patch("superstudent.evidence.file_digest", side_effect=replacing_digest):
            result = validate_evidence(self.lib, self.folder, [self.citation()], source_cache=cache)
        self.assertEqual(result["errors"][0]["code"], "changed_during_read")
        self.assertFalse(any(key[0] == "source" for key in cache))

    def test_raw_zip_markdown_cannot_bypass_bound_text_after_member_removal(self):
        archive = self.archive_source("lesson.md")
        original = self.lib.root / self.path[:-3]
        body = "# Lesson\n\n## [Page 1]\n\nModified duration estimates rate risk.\n"
        original.write_text(body)
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("lesson.md", body)
        self.update_metadata(source_sha256=file_digest(original), source_archive_sha256=file_digest(archive))
        cache = {}
        raw = self.citation(path=self.path[:-3])
        converted = self.citation()
        records = validate_evidence(self.lib, self.folder, [converted], source_cache=cache)["records"]
        self.assertEqual(len(records), 1)
        rejected = validate_evidence(self.lib, self.folder, [raw], source_cache=cache)
        self.assertEqual(rejected["errors"][0]["code"], "unbound_extraction")
        self.assertIn("converted text version", rejected["errors"][0]["message"])
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("replacement.md", "A new course file.")
        self.assertTrue(original.is_file())
        rejected = validate_evidence(self.lib, self.folder, [raw], source_cache=cache)
        self.assertEqual(rejected["errors"][0]["code"], "unbound_extraction")
        self.assertEqual(recheck_evidence(self.lib, self.folder, records, source_cache=cache)["errors"][0]["code"], "stale_extraction")

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
        self.assertEqual(result["errors"][0]["code"], "stale_extraction")

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
