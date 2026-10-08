"""Exercise exam preparation through the real local HTTP and MCP interfaces with authored sources."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from superstudent.library import Library
from superstudent.util import atomic_write_text, front_matter
from superstudent import exams


class ExamWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ss-exam-flow-")
        self.root = Path(self.tmp.name)
        self.lib = Library(self.root / "library")
        self.folder = "Fall 2026/FIN 6100 - Finance"
        self.path = self.folder + "/Modules/01 - Valuation/Source.md"
        atomic_write_text(self.lib.root / self.path, front_matter({"title": "Valuation", "type": "page"}) +
                          "# Valuation\n\n## [Page 1]\nNet present value is discounted cash flows minus initial cost.\n")
        self.lib.save_state({"courses": {"1": {"folder": self.folder, "code": "FIN 6100", "name": "Finance"}}})
        self.lib.save_snapshot("1", {"modules": [{"position": 1, "name": "Valuation", "dir": "Modules/01 - Valuation"}]})
        from superstudent.index import update_index
        update_index(self.lib)
        from superstudent.gui.app import App
        self.app = App()
        self.app.lib = lambda: self.lib
        self.app.serve()
        self.base = f"http://127.0.0.1:{self.app.port}"
        self.headers = {"X-SS-Token": self.app.token}

    def tearDown(self):
        self.app.shutdown()
        self.app.server.server_close()
        self.tmp.cleanup()

    def post(self, path, **body):
        return requests.post(self.base + path, json=body, headers=self.headers, timeout=10).json()

    def get(self, path, **params):
        return requests.get(self.base + path, params=params, headers=self.headers, timeout=10).json()

    def question(self):
        return {"topic": "NPV", "prompt": "Which is the course's NPV definition?", "type": "mcq",
                "choices": ["Discounted cash flows minus initial cost", "Revenue only"], "correct_index": 0,
                "answer": "Subtract initial cost from discounted cash flows.",
                "explanation": "Revenue alone omits timing and the investment cost.", "difficulty": "recall",
                "citations": [{"path": self.path, "locator": "Page 1",
                               "quote": "Net present value is discounted cash flows minus initial cost."}]}

    def test_http_practice_and_source_change(self):
        self.assertEqual(requests.get(self.base + "/api/exams", timeout=10).status_code, 403)
        plan = self.post("/api/exam/create", course=self.folder, title="Midterm", modules=[1], exam_date="2026-10-20")
        self.assertTrue(plan["ok"], plan)
        pid = plan["plan"]["id"]
        saved = exams.save_questions(self.lib, pid, [self.question()])
        self.assertTrue(saved["ok"], saved)
        qid = saved["question_ids"][0]
        public = self.get("/api/exam", id=pid)["plan"]
        self.assertNotIn("correct_index", public["questions"][0])
        self.assertNotIn("answer", public["questions"][0])
        self.assertNotIn("quote", public["questions"][0]["citations"][0])
        wrong = self.post("/api/exam/attempt", plan_id=pid, question_id=qid, choice_index=1, correct=True)
        self.assertFalse(wrong["attempt"]["correct"])
        self.assertEqual(wrong["plan"]["metrics"]["first_attempt_mcq_accuracy"], 0)
        revealed = self.post("/api/exam/reveal", plan_id=pid, question_id=qid)
        self.assertEqual(revealed["question"]["correct_index"], 0)
        self.assertNotIn("answer", self.get("/api/exam", id=pid)["plan"]["questions"][0])
        self.assertEqual(self.get("/api/exams", course=self.folder)["plans"][0]["attempt_count"], 1)
        source = self.lib.root / self.path
        source.write_text(source.read_text().replace("minus initial cost", "less the investment cost"))
        stale = self.post("/api/exam/attempt", plan_id=pid, question_id=qid, choice_index=0)
        self.assertFalse(stale["ok"])
        self.assertIn("regeneration", stale["message"])
        self.assertFalse(self.post("/api/exam/reveal", plan_id=pid, question_id=qid)["ok"])

    def test_http_review_remove_and_delete(self):
        plan = self.post("/api/exam/create", course=self.folder, title="Final", modules=[1])
        self.assertTrue(plan["ok"], plan)
        pid = plan["plan"]["id"]
        qid = exams.save_questions(self.lib, pid, [self.question()])["question_ids"][0]
        atomic_write_text(self.lib.root / self.folder / "Modules/01 - Valuation/Added.md",
                          front_matter({"title": "Added", "type": "page"}) + "# Added\n\n## [Page 1]\nPayback ignores timing.\n")
        changed = self.get("/api/exam", id=pid)["plan"]
        self.assertTrue(changed["scope_changed"])
        self.assertEqual([r["title"] for r in changed["scope_changes"]["added"]], ["Added"])
        for route, body in (("/api/exam/review-scope", {"plan_id": pid}), ("/api/exam/delete", {"plan_id": pid}),
                            ("/api/exam/remove-questions", {"plan_id": pid, "question_ids": [qid]})):
            self.assertEqual(requests.post(self.base + route, json=body, timeout=10).status_code, 403)
        reviewed = self.post("/api/exam/review-scope", plan_id=pid)
        self.assertTrue(reviewed["ok"], reviewed)
        self.assertFalse(reviewed["plan"]["scope_changed"])
        self.assertFalse(self.post("/api/exam/remove-questions", plan_id=pid, question_ids=[])["ok"])
        removed = self.post("/api/exam/remove-questions", plan_id=pid, question_ids=[qid])
        self.assertEqual((removed["removed"], removed["question_count"]), (1, 0))
        self.assertTrue(self.post("/api/exam/delete", plan_id=pid)["ok"])
        self.assertEqual(self.get("/api/exams", course=self.folder)["plans"], [])

    def test_bad_input_returns_actionable_message(self):
        bad = self.post("/api/exam/create", course=self.folder, title="Midterm", modules=[999])
        self.assertFalse(bad["ok"])
        self.assertIn("module", bad["message"])

    def test_mcp_end_to_end(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        async def run():
            env = dict(os.environ, SUPERSTUDENT_HOME=str(self.root / "app"),
                       SUPERSTUDENT_LIBRARY=str(self.lib.root), SUPERSTUDENT_TOKEN="dummy-exam-test-token")
            env.pop("CANVAS_TOKEN", None)
            params = StdioServerParameters(command=sys.executable, args=["-m", "superstudent", "mcp"],
                                           env=env, cwd=str(Path(__file__).resolve().parents[1]))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = {t.name for t in (await session.list_tools()).tools}
                    self.assertTrue({"exam_plans", "create_exam_plan", "check_source_evidence", "save_exam_questions",
                                     "remove_exam_questions"} <= names)
                    async def call(name, args):
                        response = await session.call_tool(name, args)
                        self.assertFalse(getattr(response, "isError", getattr(response, "is_error", False)), response)
                        text = response.content[0].text
                        return json.loads(text[text.index("{"):])
                    created = await call("create_exam_plan", {"course": self.folder, "title": "MCP midterm", "modules": [1]})
                    self.assertTrue(created["ok"], created)
                    pid = created["plan"]["id"]
                    evidence = await call("check_source_evidence", {"course": self.folder, "citations": self.question()["citations"]})
                    self.assertTrue(evidence["valid"], evidence)
                    saved = await call("save_exam_questions", {"plan_id": pid, "questions": [self.question()]})
                    self.assertEqual(saved["added"], 1)
                    self.assertNotIn("plan", saved)                 # the reply no longer repeats the workspace
                    self.assertEqual(saved["question_count"], 1)
                    self.assertIn("uncited_source_paths", saved["coverage"])
                    read_plan = await call("exam_plans", {"plan_id": pid})
                    self.assertNotIn("answer", read_plan["plan"]["questions"][0])
                    self.assertEqual(read_plan["plan"]["questions"][0]["cites"][0]["locator"], "Page 1")
                    full = await call("exam_plans", {"plan_id": pid, "question_ids": saved["question_ids"]})
                    self.assertEqual(full["plan"]["question_details"][0]["answer"], self.question()["answer"])
                    located = await call("exam_plans", {"plan_id": pid, "include_locators": True})
                    self.assertTrue(any(s.get("locators") for s in located["plan"]["sources"]))
                    fabricated = self.question()
                    fabricated["citations"][0]["quote"] = "A invented claim never stated by the instructor."
                    rejected = await call("save_exam_questions", {"plan_id": pid, "questions": [fabricated]})
                    self.assertFalse(rejected["ok"])
                    self.assertIn("evidence", rejected["message"])
                    response = await session.call_tool("search_course_materials", {"query": "investment valuation", "course": self.folder,
                                                                                  "alternate_queries": ["net present value"]})
                    self.assertIn("Page 1", response.content[0].text)
                    removed = await call("remove_exam_questions", {"plan_id": pid, "question_ids": saved["question_ids"]})
                    self.assertEqual((removed["removed"], removed["question_count"]), (1, 0))
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
