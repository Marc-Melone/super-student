#!/usr/bin/env python3
"""Offline, authored retrieval measurement. Does not call AI, Canvas, or NotebookLM."""
from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from superstudent import index
from superstudent.library import Library


LIMITATIONS = [
    "This small authored fixture measures exact source-locator retrieval, not live AI answer correctness, citation entailment, student learning, or NotebookLM performance.",
    "Alternate query wording is supplied by the fixture author. Its improvement does not establish automatic synonym generation or neural semantic search.",
    "Questions and expected evidence are curated. Add independently authored held-out courses before generalizing these results.",
    "No-evidence controls use absent exact phrases. They do not establish that an assistant abstains when plausible but insufficient passages are retrieved.",
    "Evidence controls check source existence, course scope, quoted text, availability, and change detection. They cannot prove that a quoted passage supports an answer or an exam prediction.",
]


def _write_library(root: Path, fixture: dict[str, Any]) -> Library:
    lib = Library(root)
    lib.ensure()
    lib.save_state({"courses": {
        c["id"]: {"folder": c["folder"], "name": c["name"], "code": c["code"], "items": {}}
        for c in fixture["courses"]
    }})
    for doc in fixture["documents"]:
        path = lib.checked(root / doc["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"type": doc["type"], "title": doc["title"]}
        if doc.get("sync_status"):
            meta["sync_status"] = doc["sync_status"]
        header = "---\n" + "\n".join(f"{key}: {value}" for key, value in meta.items()) + "\n---\n\n"
        body = f"# {doc['title']}\n\n" + "\n\n".join(
            f"## [{s['locator']}]\n\n{s['text']}" for s in doc["sections"]
        )
        path.write_text(header + body + "\n", encoding="utf-8")
    index.update_index(lib, full=True)
    return lib


def _hit_key(hit: dict[str, Any]) -> tuple[str, str]:
    return hit["path"], hit["locator"]


def _score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [r for r in rows if r["expected"]]
    missing = [r for r in rows if not r["expected"]]
    relevant = sum(len(r["expected"]) for r in answerable)
    retrieved = sum(r["relevant_found_at_5"] for r in answerable)
    top1 = sum(r["top1_correct"] for r in answerable)
    return {
        "questions": len(rows),
        "answerable_questions": len(answerable),
        "expected_relevant_locators": relevant,
        "relevant_locators_found_at_5": retrieved,
        "recall_at_5": round(retrieved / relevant, 4) if relevant else None,
        "top1_correct": top1,
        "top1_accuracy": round(top1 / len(answerable), 4) if answerable else None,
        "no_evidence_controls": len(missing),
        "no_evidence_controls_passed": sum(not r["hits"] for r in missing),
        "wrong_course_hits": sum(r["wrong_course_hits"] for r in rows),
    }


def _measure(lib: Library, fixture: dict[str, Any], fused: bool) -> dict[str, Any]:
    rows = []
    for question in fixture["questions"]:
        kwargs: dict[str, Any] = {"course": question["course"], "limit": 5, "per_doc": 5}
        if fused:
            kwargs["alternate_queries"] = question.get("alternate_queries", [])
        hits = index.search(lib, question["query"], **kwargs)
        expected = {_hit_key(h) for h in question["expected"]}
        found = {_hit_key(h) for h in hits}
        rows.append({
            "id": question["id"], "course": question["course"], "query": question["query"],
            "alternate_queries_used": question.get("alternate_queries", []) if fused else [],
            "expected": question["expected"],
            "relevant_found_at_5": len(expected & found),
            "top1_correct": bool(hits and _hit_key(hits[0]) in expected),
            "wrong_course_hits": sum(not h["path"].startswith(question["course"] + "/") for h in hits),
            "hits": [{k: h.get(k) for k in ("path", "locator", "status", "stale", "match")}
                     for h in hits],
        })
    return {
        "available": True, "metrics": _score(rows),
        "per_course": {course["folder"]: _score([r for r in rows if r["course"] == course["folder"]])
                       for course in fixture["courses"]},
        "questions": rows,
    }


def _availability_controls(lib: Library) -> list[dict[str, Any]]:
    cases = [
        ("stale", "Synthetic/Finance", "Legacyexerciseprotocol", "Synthetic/Finance/Files/Old instructions.md"),
        ("restricted", "Synthetic/Science", "Restrictedreadingprotocol", "Synthetic/Science/Files/Restricted reading.md"),
        ("removed", "Synthetic/Humanities", "Removedscopeprotocol", "Synthetic/Humanities/_Removed from Canvas/Old scope.md"),
    ]
    out = []
    for expected_status, course, query, path in cases:
        hits = index.search(lib, query, course=course, limit=5)
        matching = [h for h in hits if h["path"] == path]
        passed = bool(matching and all(h.get("stale") and h.get("status") == expected_status for h in matching))
        out.append({"id": f"search-{expected_status}-warning", "passed": passed,
                    "expected_status": expected_status,
                    "matching_hits": [{"path": h["path"], "status": h.get("status"), "stale": h.get("stale")}
                                      for h in matching]})
    return out


def _evidence_controls(lib: Library) -> dict[str, Any]:
    try:
        from superstudent import evidence
    except ImportError:
        return {"available": False, "reason": "The app has no superstudent.evidence API yet.", "cases": []}
    if not all(hasattr(evidence, name) for name in ("validate_evidence", "recheck_evidence")):
        return {"available": False, "reason": "Evidence validation APIs are unavailable.", "cases": []}
    course = "Synthetic/Finance"
    good = {"path": "Synthetic/Finance/Files/Capital decisions.md", "locator": "Slide 1",
            "quote": "Accept an independent project when its net present value is positive."}
    invalid_cases = [
        ("empty-evidence", []),
        ("missing-source", [{**good, "path": "Synthetic/Finance/Files/Missing.md"}]),
        ("missing-locator", [{**good, "locator": "Slide 999"}]),
        ("empty-locator", [{**good, "locator": ""}]),
        ("empty-quote", [{**good, "quote": ""}]),
        ("wrong-quote", [{**good, "quote": "Accept every project regardless of its present value."}]),
        ("wrong-course", [{"path": "Synthetic/Science/Files/Cells.md", "locator": "Page 2",
                            "quote": "An enzyme is not consumed by its reaction."}]),
        ("stale-source", [{"path": "Synthetic/Finance/Files/Old instructions.md", "locator": "Page 1",
                            "quote": "Legacyexerciseprotocol: this cached instruction is from an update that failed."}]),
        ("restricted-source", [{"path": "Synthetic/Science/Files/Restricted reading.md", "locator": "Page 1",
                                 "quote": "Restrictedreadingprotocol: this historical copy cannot establish the currently accessible class content."}]),
        ("removed-source", [{"path": "Synthetic/Humanities/_Removed from Canvas/Old scope.md", "locator": "Page 1",
                              "quote": "Removedscopeprotocol: this reading was removed and cannot establish the current exam scope."}]),
        ("outside-library", [{**good, "path": "../outside.md"}]),
    ]
    rows = []

    def record(name: str, result: dict[str, Any], expected_valid: bool) -> None:
        rows.append({"id": name, "expected_valid": expected_valid, "actual_valid": result.get("valid"),
                     "passed": result.get("valid") is expected_valid, "errors": result.get("errors", [])})

    good_result = evidence.validate_evidence(lib, course, [good])
    record("valid-source-excerpt", good_result, True)
    for name, citations in invalid_cases:
        case_course = "Synthetic/Science" if name == "restricted-source" else (
            "Synthetic/Humanities" if name == "removed-source" else course)
        record(name, evidence.validate_evidence(lib, case_course, citations), False)
    records = good_result.get("records") or []
    if good_result.get("valid") and records:
        record("unchanged-source", evidence.recheck_evidence(lib, course, records), True)
        forged = copy.deepcopy(records)
        forged[0]["source_fingerprint"] = "0" * 64
        record("forged-fingerprint", evidence.recheck_evidence(lib, course, forged), False)
        unhashed = copy.deepcopy(records)
        unhashed[0].pop("source_fingerprint", None)
        record("missing-fingerprint", evidence.recheck_evidence(lib, course, unhashed), False)
        source = lib.checked(lib.root / good["path"])
        original = source.read_text(encoding="utf-8")
        try:
            source.write_text(original + "\nThe instructor has revised the lecture.\n", encoding="utf-8")
            record("source-changed-after-validation", evidence.recheck_evidence(lib, course, records), False)
            source.unlink()
            record("source-deleted-after-validation", evidence.recheck_evidence(lib, course, records), False)
        finally:
            source.write_text(original, encoding="utf-8")
    else:
        rows.append({"id": "source-recheck-prerequisite", "passed": False,
                     "errors": ["A valid citation did not produce records to recheck."]})
    # This deliberately shows a limit, not an expected failure: a real NPV quotation does not
    # answer a WACC question. The validator can prove occurrence, not semantic relevance.
    return {
        "available": True, "cases": rows, "passed": sum(r["passed"] for r in rows), "count": len(rows),
        "entailment_limit_example": {
            "question": "How is weighted average cost of capital calculated?",
            "citation": good,
            "mechanical_validation_valid": good_result.get("valid"),
            "supports_answer_to_this_question": False,
            "explanation": "A real NPV decision-rule quotation does not explain the WACC calculation. Semantic support needs separate review.",
        },
    }


def run(fixture_path: Path) -> dict[str, Any]:
    raw = fixture_path.read_bytes()
    fixture = json.loads(raw)
    supports_variants = "alternate_queries" in inspect.signature(index.search).parameters
    with tempfile.TemporaryDirectory(prefix="superstudent-exam-benchmark-") as scratch:
        lib = _write_library(Path(scratch) / "Library", fixture)
        baseline = _measure(lib, fixture, fused=False)
        fused = _measure(lib, fixture, fused=True) if supports_variants else {
            "available": False, "reason": "index.search does not accept alternate_queries yet."
        }
        availability = _availability_controls(lib)
        evidence = _evidence_controls(lib)
    paired = {}
    if fused["available"]:
        before = {r["id"]: r for r in baseline["questions"] if r["expected"]}
        after = {r["id"]: r for r in fused["questions"] if r["expected"]}
        paired = {
            "recall_rescued_question_ids": [qid for qid in before if not before[qid]["relevant_found_at_5"] and after[qid]["relevant_found_at_5"]],
            "recall_regressed_question_ids": [qid for qid in before if before[qid]["relevant_found_at_5"] and not after[qid]["relevant_found_at_5"]],
            "top1_improved_question_ids": [qid for qid in before if not before[qid]["top1_correct"] and after[qid]["top1_correct"]],
            "top1_regressed_question_ids": [qid for qid in before if before[qid]["top1_correct"] and not after[qid]["top1_correct"]],
        }
    engine_controls_passed = all(
        engine["metrics"]["wrong_course_hits"] == 0 and
        engine["metrics"]["no_evidence_controls_passed"] == engine["metrics"]["no_evidence_controls"]
        for engine in (baseline, fused) if engine["available"]
    )
    return {
        "schema_version": 1,
        "benchmark": "authored offline course retrieval and mechanical evidence validation",
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "dataset": {"courses": len(fixture["courses"]), "documents": len(fixture["documents"]),
                    "questions": len(fixture["questions"]),
                    "questions_with_curated_alternate_queries": sum(bool(q.get("alternate_queries")) for q in fixture["questions"])},
        "baseline_literal_query": baseline,
        "app_with_curated_alternate_queries": fused,
        "paired_comparison": paired,
        "source_availability_controls": availability,
        "evidence_controls": evidence,
        "available_controls_passed": engine_controls_passed and all(r["passed"] for r in availability)
                                     and (not evidence["available"] or evidence["passed"] == evidence["count"]),
        "limitations": LIMITATIONS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Also save the JSON report to this path.")
    parser.add_argument("--require-improvement", action="store_true",
                        help="Fail unless the app supports variants, improves Recall@5, and passes every evidence control.")
    args = parser.parse_args()
    report = run(Path(__file__).with_name("exam_fixture.json"))
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    if not report["available_controls_passed"]:
        return 1
    if args.require_improvement:
        fused = report["app_with_curated_alternate_queries"]
        evidence = report["evidence_controls"]
        if not fused["available"] or not evidence["available"]:
            return 1
        if fused["metrics"]["recall_at_5"] <= report["baseline_literal_query"]["metrics"]["recall_at_5"]:
            return 1
        if report["paired_comparison"]["recall_regressed_question_ids"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
