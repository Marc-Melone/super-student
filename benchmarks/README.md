# Course exam retrieval benchmark

Run from the repository root in the project's Python environment:

```bash
python benchmarks/exam_retrieval.py --require-improvement --output /tmp/exam-retrieval.json
```

The script prints JSON and optionally saves the same report. It creates and removes a temporary library;
it does not read real courses, use a Canvas token, make network requests, or call an AI provider.
The fixture is original synthetic material authored for this repository.

## What it measures

The three courses contain finance, cell biology, and historical methods sources, plus distracting and
unavailable material. Twelve answerable questions specify an exact expected file and section locator.
Three questions use exact phrases absent from the selected course as no-evidence controls. Search is
limited to five results and the selected course.

The paired comparison uses the same app and library for both runs:

1. `index.search` with only the literal question.
2. `index.search` with that question and the fixture's curated `alternate_queries`.

The second run measures the app's ability to combine alternative wording **supplied by the caller**.
These variants are manually authored, often using the source's own terms. This does not measure automatic
query rewriting, embeddings, or neural semantic search. Some questions deliberately avoid the source's
terminology to exercise the known keyword-search weakness. The dataset is small and is not held out from
development; its results must not be described as typical student performance.

`recall_at_5` is the fraction of expected relevant file/locator pairs found in the first five results.
`top1_accuracy` is the fraction of answerable questions whose first result is an expected pair. Missing
information controls are excluded from those denominators and reported separately. The report includes
every question, returned locator, per-course totals, and paired improvements and regressions, so totals
can be inspected rather than taken on trust. Fixture SHA-256 identifies the exact authored dataset.

## Evidence and availability controls

Search controls require stale, restricted, and removed copies to retain the appropriate visible warning.
Every returned hit is also checked against the requested course folder.

When the app exposes `superstudent.evidence`, the script checks a valid quotation and rejects empty
evidence, missing sources, missing/empty locators, empty or fabricated quotations, another course's
source, unavailable/historical sources, and traversal paths. It rechecks saved evidence against
unchanged, changed, and deleted files and missing or altered fingerprints.

A deliberately irrelevant but real quotation is reported as `entailment_limit_example`: mechanical
validation succeeds even though an NPV decision rule cannot answer a WACC calculation question. A
source locator and matching quotation prove occurrence and freshness, **not that an explanation is
correct, relevant, complete, or sufficient**. An assistant or reviewer still needs to check that the
source supports each substantive claim and the question's answer/rubric.

The three no-evidence controls only test absent exact phrases. They do not prove reliable abstention
when related passages are retrieved but do not actually contain the answer.

## Exit status and interpretation

By default, an available control failure returns exit status 1; missing optional capabilities are reported
as unavailable. `--require-improvement` additionally requires the app's variant-search and evidence APIs,
higher Recall@5 than the literal-query run, and no questions losing their expected evidence.

On the initial development run, the authored questions yielded:

| Measurement | Literal question | Supplied alternate wording |
| --- | ---: | ---: |
| Expected evidence in first five results | 9/12 (75%) | 12/12 (100%) |
| Expected evidence as first result | 4/12 (33.3%) | 12/12 (100%) |
| Absent-phrase controls returning no evidence | 3/3 | 3/3 |
| Hits from another course | 0 | 0 |

Rerun against any changed app or fixture rather than treating these historical numbers as a guarantee.
This is not a NotebookLM comparison, a live AI answer-quality benchmark, an exam prediction test, or
evidence of student learning gains.

## Acceptance checks for the complete exam workflow

Beyond retrieval, changes to the exam workflow should verify that:

- An exam saves one exact course folder and explicit student/instructor scope. Dates and topic emphasis
  carry their source or are visibly labeled student-entered/inferred. Unsupported predictions about what
  will be on an exam are not presented as established course requirements.
- Practice prompts, worked solutions, answer criteria, and citations remain within that scope; current
  original material supports the expected answer. Saved notes and generated summaries are not used to
  authenticate their own generated answers.
- Changing, removing, restricting, or failing to update a supporting source invalidates the saved practice
  evidence before answer reveal or grading. Problems cannot continue to appear current because another
  cited source is still valid.
- The student answers before seeing the solution; exact answers, reveal events, attempts, and review dates
  survive a restart. A failed attempt brings the topic back for practice, and a later successful attempt
  does not erase the earlier error history.
- Self-assessment is labeled as such. Viewing an answer, writing notes, or self-rating "correct" is not
  called independently measured mastery. Objective scoring handles alternate valid answers only when
  explicit criteria are available; a generated rubric itself still needs source and quality review.
- Nonliteral course questions, ambiguous wording, changed lecture definitions, conflicting sources,
  diagrams/equations, and genuinely missing information receive separate held-out evaluation.

To assess answer quality, add unseen public or permitted course sources and independent gold answers,
run fixed questions through each real assistant, and have blinded reviewers score correctness, exact
citation support, omissions, and appropriate abstention. A learning claim needs a separate student
pre/post assessment with unfamiliar transfer problems. Those evaluations remain future work.
