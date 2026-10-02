"""A fake Canvas that behaves like the real one where it matters for syncing.

Edge cases covered: pagination via Link headers, a rate-limit 403 that must be retried,
a hidden Files/Pages tab that errors, a discussion view that returns 503 first, a
discussion that requires posting first, a "PDF" whose download is really a sign-in page,
a locked file, captions on an embedded video, and file downloads the way Canvas does them
since it retired `verifier` links: the token on the Canvas host, then a redirect to a separate
file-storage host (a different host name) that must never receive the token.

`extras = True` adds the cases from the final review (block-editor page, past quiz questions,
rubric details, feedback files and recorded comments, linked-only pages, deleted and multi-line
replies, a PDF whose name looks like a sign-in address). Other switches simulate changes
between syncs (deleted and replaced files, a page unlocking, a revoked token, a flaky download).
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlparse

TOKEN = "test-token-123"
TEACHER_ID = 77


class FakeCanvas:
    def __init__(self, files: Dict[str, Path]):
        self.files = files
        self.hits: List[str] = []
        self.token_on_downloads = 0      # Canvas-host download requests that carried the token (expected now)
        self.token_on_storage = 0        # requests to the file-storage host that carried it (must stay 0)
        self.storage_hits: List[str] = []
        self.rate_limited_once = False
        self.view_503_once = False
        self.server: Optional[ThreadingHTTPServer] = None
        self.storage_server: Optional[ThreadingHTTPServer] = None
        self.base = ""
        self.storage = ""
        self.extra_announcement = False
        self.video_reuploaded = False
        # switches for the review-fix tests
        self.extras = False
        self.replace_duration_notes = False   # 1002 deleted; 1012 "Duration Notes.pdf" replaces it
        self.page_locked = True               # extras: "Week 3 preview" locked until it isn't
        self.broken_downloads: set = set()    # file ids whose download fails everywhere
        self.flaky_once: set = set()          # file ids whose first storage download is cut off mid-file
        self.revoke_after: Optional[int] = None   # API calls before the token stops working
        self.api_calls = 0
        self.legacy_verifier = False          # older Canvas: verifier links work without the token
        self.rename_week2 = False             # the instructor renames a module

    # ------------------------------------------------------------------ data
    def file_json(self, fid: int, name: str, folder_id: int = 1, ctype: str = "application/octet-stream",
                  locked: bool = False, updated: str = "2026-09-01T12:00:00Z", **extra) -> Dict[str, Any]:
        source = {"Old exam 2025.pdf": "Duration Notes.pdf", "SSO and SAML basics.pdf": "Duration Notes.pdf",
                  LONG_CJK_NAME: "Duration Notes.pdf"}.get(name, name)
        size = self.files[source].stat().st_size if source in self.files else 1234
        data = {"id": fid, "display_name": name, "filename": name.replace(" ", "+"), "folder_id": folder_id,
                "content-type": ctype, "size": size, "updated_at": updated, "modified_at": updated,
                "url": "" if locked else f"{self.base}/files/{fid}/download?download_frd=1&verifier=v{fid}",
                "locked_for_user": locked}
        if locked:
            data["lock_explanation"] = "This file is locked until Oct 1 at 12:00am."
        data.update(extra)
        return data

    def all_files_101(self) -> List[Dict[str, Any]]:
        files = [
            self.file_json(1001, "Lecture 1 - Bond Basics.pptx", 2,
                           "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            self.file_json(1002, "Duration Notes.pdf", 2, "application/pdf"),
            self.file_json(1003, "Bond Model.xlsx", 2, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            self.file_json(1004, "Week 2 Lecture Recording.mp4", 3, "video/mp4",
                           updated="2026-09-02T12:00:00Z" if self.video_reuploaded else "2026-09-01T12:00:00Z"),
            self.file_json(1006, "Old exam 2025.pdf", 4, "application/pdf"),
            self.file_json(1007, "Login trap.pdf", 4, "application/pdf"),
            self.file_json(1008, "Problem Set Handout.docx", 1,
                           "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            self.file_json(1009, "payoff.png", 1, "image/png"),
            self.file_json(1010, "Locked answers.pdf", 4, "application/pdf", locked=True),
        ]
        if self.replace_duration_notes:
            files = [f for f in files if f["id"] != 1002]
            files.append(self.file_json(1012, "Duration Notes.pdf", 2, "application/pdf", updated="2026-09-20T12:00:00Z"))
        if self.extras:
            files.append(self.file_json(1013, "SSO and SAML basics.pdf", 4, "application/pdf"))
            files.append(self.file_json(1014, LONG_CJK_NAME, 4, "application/pdf"))
        return files

    def file_source(self, fid: int) -> Optional[str]:
        """Which fixture holds a file id's bytes."""
        names = {f["id"]: f["display_name"] for f in self.all_files_101()}
        names.update({9001: "Problem Set Handout.docx", 9002: "Duration Notes.pdf", 2001: "Duration Notes.pdf",
                      1005: "Week 2 Lecture Recording.mp4", 1012: "Duration Notes.pdf", 1002: "Duration Notes.pdf",
                      7777: "Week 2 Lecture Recording.mp4"})
        name = names.get(fid)
        name = {"Old exam 2025.pdf": "Duration Notes.pdf", "SSO and SAML basics.pdf": "Duration Notes.pdf",
                LONG_CJK_NAME: "Duration Notes.pdf"}.get(name, name)
        return name if name in self.files else None

    def storage_url(self, fid: int, name: str = "file") -> str:
        return f"{self.storage}/blob/{fid}/{quote(name)}?sig=s{fid}"

    # ------------------------------------------------------------------ routes
    def route(self, method: str, path: str, query: Dict[str, List[str]], headers) -> Tuple[int, Any, Dict[str, str]]:
        auth_ok = headers.get("Authorization") == f"Bearer {TOKEN}"
        if self.revoke_after is not None and path.startswith("/api/v1/") and auth_ok:
            self.api_calls += 1
            if self.api_calls > self.revoke_after:
                auth_ok = False
        if path.startswith("/files/") and "/download" in path:
            if headers.get("Authorization"):
                self.token_on_downloads += 1
            fid = int(path.split("/")[2])
            if fid in self.broken_downloads:
                return 404, {"errors": [{"message": "not found"}]}, {}
            if fid == 1007:
                return 200, ("text/html", b"<!DOCTYPE html><html><form id='login_form'><input type='password'></form></html>"), {}
            if auth_ok:
                if not self.file_source(fid):
                    return 404, {"errors": [{"message": "not found"}]}, {}
                if fid == 9001:    # a submitted file: Canvas wants the submission named (see public_url)
                    return 401, {"status": "unauthorized", "errors": [{"message": "user not authorized to perform that action"}]}, {}
                return 302, None, {"Location": self.storage_url(fid, "download")}
            if self.legacy_verifier and query.get("verifier", [""])[0] == f"v{fid}" and self.file_source(fid):
                return 200, ("application/octet-stream", self.files[self.file_source(fid)].read_bytes()), {}
            return 302, None, {"Location": f"{self.base}/login/canvas"}
        if path.startswith("/users/") and "/media_download" in path:
            if not auth_ok:
                return 302, None, {"Location": f"{self.base}/login/canvas"}
            return 302, None, {"Location": self.storage_url(7777, "feedback.m4a")}
        if path.startswith("/login"):
            return 200, ("text/html", b"<html><body>Log In to Canvas<form id='login_form'></form></body></html>"), {}
        if not path.startswith("/api/v1/"):
            return 404, {"errors": [{"message": "not found"}]}, {}
        if path == "/api/v1/accounts/search":   # Instructure's public school finder (no sign-in)
            name = query.get("name", [""])[0].lower()
            # (the real finder returns bare domains; this one includes http:// because the fake has no TLS)
            rows = [{"name": "Fake State University", "domain": self.base},
                    {"name": "Fake State Community College", "domain": "fscc.instructure.com"}]
            return 200, [r for r in rows if name[:4] in r["name"].lower()], {}
        if not auth_ok:
            return 401, {"errors": [{"message": "Invalid access token."}]}, {"WWW-Authenticate": 'Bearer realm="canvas-lms"'}
        self.hits.append(path)
        p = path[len("/api/v1"):]

        if p == "/users/self":
            return 200, {"id": 42, "name": "Test Student"}, {}
        if p == "/courses":
            return 200, [self.course(101), self.course(202), {"id": 303, "access_restricted_by_date": True}], {}
        m = re.fullmatch(r"/courses/(\d+)", p)
        if m:
            return 200, self.course(int(m.group(1))), {}
        m = re.fullmatch(r"/files/(\d+)/public_url", p)
        if m:
            fid = int(m.group(1))
            if fid in self.broken_downloads or fid == 1007 or not self.file_source(fid):
                return 404, {"errors": [{"message": "not found"}]}, {}
            if fid == 9001 and query.get("submission_id", [""])[0] != "6001":
                return 401, {"status": "unauthorized", "errors": [{"message": "user not authorized to perform that action"}]}, {}
            return 200, {"public_url": self.storage_url(fid, "public")}, {}
        m = re.fullmatch(r"/quiz_submissions/(\d+)/questions", p)
        if m and self.extras:
            return 200, {"quiz_submission_questions": [{"id": 501, "answer": 2}, {"id": 502, "answer": "4.2"}]}, {}
        m = re.fullmatch(r"/courses/(\d+)/(.*)", p)
        if not m:
            m2 = re.fullmatch(r"/files/(\d+)", p)
            if m2:
                return self.single_file(101, int(m2.group(1)))
            m2 = re.fullmatch(r"/media_attachments/(\d+)/media_tracks", p)
            if m2 and m2.group(1) == "1005":
                return 200, [{"id": 1, "locale": "en", "kind": "captions", "webvtt_content": CAPTIONS}], {}
            if m2:
                return 200, [], {}
            m2 = re.fullmatch(r"/media_objects/([^/]+)/media_tracks", p)
            if m2:
                return 200, [], {}
            if p == "/calendar_events":
                code = query.get("context_codes[]", [""])[0]
                if code == "course_101":
                    return 200, [{"title": "Midterm Exam", "start_at": "2026-10-15T13:00:00Z", "end_at": "2026-10-15T15:00:00Z",
                                  "location_name": "Room 101", "description": "<p>Closed book. Bring a calculator.</p>",
                                  "html_url": f"{self.base}/calendar?event_id=1", "workflow_state": "active"},
                                 {"title": "Review session", "start_at": "2026-10-13T22:00:00Z", "location_name": "Zoom",
                                  "description": "", "workflow_state": "active"}], {}
                return 200, [], {}
            if p == "/announcements":
                return 200, [], {}
            return 404, {"errors": [{"message": "The specified resource does not exist."}]}, {}
        cid, rest = int(m.group(1)), m.group(2)
        return self.course_route(cid, rest, query)

    def course(self, cid: int) -> Dict[str, Any]:
        if cid == 101:
            return {"id": 101, "name": "Fixed Income", "course_code": "FIN 6100", "default_view": "modules",
                    "term": {"name": "Fall 2026"}, "teachers": [{"id": TEACHER_ID, "display_name": "Prof. Rivera"}],
                    "apply_assignment_group_weights": True, "start_at": "2026-08-28T00:00:00Z", "end_at": "2026-12-15T00:00:00Z",
                    "enrollments": [{"type": "student", "computed_current_score": 90.0, "computed_current_grade": "A-"}],
                    "syllabus_body": SYLLABUS.replace("BASE", self.base)}
        return {"id": 202, "name": "Econometrics", "course_code": "ECON 5200", "default_view": "wiki",
                "term": {"name": "Fall 2026"}, "teachers": [{"id": 88, "display_name": "Dr. Chen"}],
                "syllabus_body": ""}

    def single_file(self, cid: int, fid: int):
        for f in self.all_files_101():
            if f["id"] == fid:
                return 200, f, {}
        if fid == 2001:
            return 200, self.file_json(2001, "Duration Notes.pdf", 9, "application/pdf"), {}
        if fid == 1005:
            return 200, self.file_json(1005, "week2.mp4", 3, "video/mp4", media_entry_id="m-555"), {}
        return 404, {"errors": [{"message": "The specified resource does not exist."}]}, {}

    def pages_101(self) -> Dict[str, Dict[str, Any]]:
        pages = dict(PAGES_101)
        if self.extras:
            pages["block-page"] = {"title": "Week 3 Overview", "updated_at": "2026-09-15T12:00:00Z", "body": None,
                                   "editor": "block_editor", "block_editor_attributes": {"id": 3, "version": "0.2",
                                                                                         "blocks": json.dumps(BLOCKS)}}
            pages["week-3-preview"] = {"title": "Week 3 Preview", "updated_at": "2026-09-10T12:00:00Z",
                                       "body": "<p>Week 3 covers immunization and the key-rate durations.</p>"}
            if self.page_locked:
                pages["week-3-preview"] = dict(pages["week-3-preview"], body=None, locked_for_user=True,
                                               lock_explanation="This page is locked until Oct 5.")
        return pages

    def course_route(self, cid: int, rest: str, query: Dict[str, List[str]]):
        if rest == "tabs":
            tabs = ["home", "announcements", "modules", "assignments", "discussions", "quizzes", "grades", "syllabus"]
            if cid == 101:
                tabs += ["files", "pages"]
            data = [{"id": t, "label": t.title(), "type": "internal"} for t in tabs]
            if cid == 101:
                data.append({"id": "context_external_tool_9", "label": "Panopto Recordings", "type": "external",
                             "full_url": f"{self.base}/courses/101/external_tools/9"})
            return 200, data, {}
        if cid == 202:
            return self.course_202(rest, query)
        if rest == "modules":
            return 200, MODULES_101(self.base, self), {}
        pages = self.pages_101()
        if rest == "pages":
            return 200, [{"url": u, "title": p["title"], "updated_at": p["updated_at"]} for u, p in pages.items()], {}
        m = re.fullmatch(r"pages/([^/]+)", rest)
        if m:
            page = pages.get(m.group(1))
            if not page:
                return 404, {"errors": [{"message": "page not found"}]}, {}
            return 200, {"url": m.group(1), **page, "body": (page.get("body") or "").replace("BASE", self.base) or None,
                         "html_url": f"{self.base}/courses/101/pages/{m.group(1)}"}, {}
        if rest == "folders":
            return 200, [{"id": 1, "full_name": "course files"}, {"id": 2, "full_name": "course files/Lectures"},
                         {"id": 3, "full_name": "course files/Recordings"}, {"id": 4, "full_name": "course files/Exams"}], {}
        if rest == "files":
            return 200, self.all_files_101(), {}
        m = re.fullmatch(r"files/(\d+)", rest)
        if m:
            return self.single_file(cid, int(m.group(1)))
        if rest == "assignments":
            if not self.rate_limited_once:
                self.rate_limited_once = True
                return 403, ("text/plain", b"403 Forbidden (Rate Limit Exceeded)"), {}
            return 200, ASSIGNMENTS_101(self.base, self.extras), {}
        if rest == "students/submissions":
            return 200, SUBMISSIONS_101(self.base, self.extras), {}
        if rest == "assignment_groups":
            return 200, [{"id": 1, "name": "Problem Sets", "group_weight": 30}, {"id": 2, "name": "Exams", "group_weight": 60},
                         {"id": 3, "name": "Quizzes", "group_weight": 10}], {}
        if rest == "quizzes":
            return 200, [{"id": 7001, "title": "Quiz 1: Bond Pricing", "quiz_type": "assignment", "due_at": "2026-09-12T03:59:00Z",
                          "question_count": 10, "points_possible": 10, "time_limit": 20, "allowed_attempts": 1,
                          "assignment_id": 5003, "description": "<p>Covers Week 1. Calculator allowed.</p>",
                          "html_url": f"{self.base}/courses/101/quizzes/7001"}], {}
        if rest == "quizzes/7001/submissions" and self.extras:
            return 200, {"quiz_submissions": [{"id": 1, "attempt": 1, "workflow_state": "complete", "score": 7,
                                               "kept_score": 7, "finished_at": "2026-09-11T20:00:00Z"}]}, {}
        if rest == "quizzes/7001/questions" and self.extras:
            if query.get("quiz_submission_id", [""])[0] != "1" or query.get("quiz_submission_attempt", [""])[0] != "1":
                return 401, {"errors": [{"message": "user not authorized to perform that action"}]}, {}
            return 200, QUIZ_QUESTIONS, {}
        if rest == "discussion_topics":
            if query.get("only_announcements", [""])[0] == "true":
                anns = [{"id": 9101, "title": "Midterm review session", "posted_at": "2026-09-20T15:00:00Z",
                         "user_name": "Prof. Rivera", "html_url": f"{self.base}/courses/101/discussion_topics/9101",
                         "message": "<p>The midterm on Oct 15 is cumulative through Week 6. Make sure you know how to "
                                    "compute convexity. Bring a financial calculator.</p>"}]
                if self.extra_announcement:
                    anns.append({"id": 9102, "title": "Formula sheet posted", "posted_at": "2026-09-28T15:00:00Z",
                                 "user_name": "Prof. Rivera", "message": "<p>The formula sheet for the exam is in Files.</p>"})
                return 200, anns, {}
            topics = [{"id": 8001, "title": "Duration intuition", "message": "<p>Why does duration fall as coupons rise?</p>",
                       "posted_at": "2026-09-05T12:00:00Z", "last_reply_at": "2026-09-07T12:00:00Z",
                       "discussion_subentry_count": 2, "user_name": "Prof. Rivera",
                       "html_url": f"{self.base}/courses/101/discussion_topics/8001"},
                      {"id": 8002, "title": "Introduce yourself", "message": "<p>Say hi.</p>", "require_initial_post": True,
                       "posted_at": "2026-08-29T12:00:00Z", "discussion_subentry_count": 5}]
            if self.extras:     # a classmate's topic: not the instructor's word about the exam
                topics.append({"id": 8003, "title": "Midterm rumor", "user_name": "Sam", "posted_at": "2026-09-21T12:00:00Z",
                               "message": "<p>I heard the midterm will only cover chapters 1 and 2, so skip convexity.</p>",
                               "discussion_subentry_count": 0})
            return 200, topics, {}
        m = re.fullmatch(r"discussion_topics/(\d+)/view", rest)
        if m:
            if m.group(1) == "8002":
                return 403, ("text/plain", b"require_initial_post"), {}
            if not self.view_503_once:
                self.view_503_once = True
                return 503, {"errors": [{"message": "cache not ready"}]}, {"Retry-After": "0"}
            view = [{"id": 1, "user_id": 5, "message": "<p>Higher coupons pay back sooner.</p>",
                     "created_at": "2026-09-06T12:00:00Z",
                     "replies": [{"id": 2, "user_id": TEACHER_ID, "created_at": "2026-09-07T12:00:00Z",
                                  "message": "<p>Exactly. This will be on the exam, so practice it.</p>"}]}]
            if self.extras:
                view.append({"id": 3, "deleted": True, "created_at": "2026-09-07T13:00:00Z",
                             "replies": [{"id": 4, "user_id": TEACHER_ID, "created_at": "2026-09-07T14:00:00Z",
                                          "message": "<p>Two things to remember:</p><ul><li>Duration is a weighted "
                                                     "average time.</li><li>Expect a duration question on the final.</li></ul>"}]})
            return 200, {"participants": [{"id": TEACHER_ID, "display_name": "Prof. Rivera"}, {"id": 5, "display_name": "Sam"}],
                         "view": view, "new_entries": []}, {}
        if rest in ("media_attachments", "media_objects"):
            return 401, {"errors": [{"message": "user not authorized to perform that action"}]}, {}
        return 404, {"errors": [{"message": "The specified resource does not exist."}]}, {}

    def course_202(self, rest: str, query):
        if rest == "modules":
            return 200, [{"id": 21, "name": "Unit 1: Regression", "position": 1, "items_count": 2, "items": None}], {}
        if rest == "modules/21/items":
            return 200, [{"id": 1, "type": "File", "title": "Duration Notes.pdf", "content_id": 2001, "position": 1},
                         {"id": 2, "type": "Page", "title": "OLS assumptions", "page_url": "ols-assumptions", "position": 2}], {}
        if rest == "pages":
            return 404, {"errors": [{"message": "That page has been disabled for this course"}]}, {}
        if rest == "pages/ols-assumptions":
            body = "<h2>Gauss-Markov</h2><p>Know all five assumptions for the midterm.</p>"
            if self.extras:
                body += f"<p>The proof is on <a href='{self.base}/courses/202/pages/gauss-markov-proof'>its own page</a>.</p>"
            return 200, {"url": "ols-assumptions", "title": "OLS assumptions", "updated_at": "2026-09-02T12:00:00Z",
                         "body": body}, {}
        if rest == "pages/gauss-markov-proof" and self.extras:
            return 200, {"url": "gauss-markov-proof", "title": "Gauss-Markov proof", "updated_at": "2026-09-03T12:00:00Z",
                         "body": "<p>OLS is BLUE: the best linear unbiased estimator under the five assumptions.</p>"}, {}
        if rest == "front_page":
            return 404, {"errors": [{"message": "no front page"}]}, {}
        if rest in ("files", "folders"):
            return 401, {"errors": [{"message": "user not authorized to perform that action"}]}, {}
        m = re.fullmatch(r"files/(\d+)", rest)
        if m:
            return self.single_file(202, int(m.group(1)))
        if rest in ("assignments", "quizzes", "discussion_topics", "assignment_groups", "students/submissions"):
            return 200, [], {}
        if rest in ("media_attachments", "media_objects"):
            return 401, {"errors": [{"message": "unauthorized"}]}, {}
        return 404, {"errors": [{"message": "not found"}]}, {}

    def storage_route(self, path: str, query: Dict[str, List[str]], headers):
        self.storage_hits.append(path)
        if headers.get("Authorization"):
            self.token_on_storage += 1
        m = re.fullmatch(r"/blob/(\d+)/[^/]*", path)
        if not m or query.get("sig", [""])[0] != f"s{m.group(1)}":
            return 403, ("text/plain", b"AccessDenied"), {}
        fid = int(m.group(1))
        source = self.file_source(fid)
        if not source:
            return 404, ("text/plain", b"NoSuchKey"), {}
        data = self.files[source].read_bytes()
        if fid in self.flaky_once:
            self.flaky_once.discard(fid)
            return "cut", ("application/octet-stream", data), {}
        return 200, ("application/octet-stream", data), {}

    # ------------------------------------------------------------------ server
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                status, body, headers = fake.route("GET", parsed.path, query, self.headers)
                _respond(self, status, body, headers, parsed, query, paginate=True)

        class StorageHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                status, body, headers = fake.storage_route(parsed.path, query, self.headers)
                _respond(self, status, body, headers, parsed, query, paginate=False)

        def _respond(handler, status, body, headers, parsed, query, paginate):
            if status == 302:
                handler.send_response(302)
                handler.send_header("Location", headers["Location"])
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
            if isinstance(body, tuple):
                ctype, raw = body
            else:
                raw = json.dumps(body).encode()
                ctype = "application/json"
                if paginate and isinstance(body, list) and parsed.path.startswith("/api/v1/") and status == 200:
                    page = int(query.get("page", ["1"])[0])
                    per = min(int(query.get("per_page", ["10"])[0]), 3)   # force pagination
                    chunk = body[(page - 1) * per: page * per]
                    raw = json.dumps(chunk).encode()
                    if page * per < len(body):
                        q = dict(query)
                        q["page"] = [str(page + 1)]
                        q["per_page"] = [str(per)]
                        nxt = f"{fake.base}{parsed.path}?{urlencode(q, doseq=True)}"
                        headers = dict(headers, Link=f'<{nxt}>; rel="next"')
            if status == "cut":          # promise the whole file, send half, hang up
                handler.send_response(200)
                handler.send_header("Content-Type", ctype)
                handler.send_header("Content-Length", str(len(raw)))
                handler.end_headers()
                handler.wfile.write(raw[: len(raw) // 2])
                handler.wfile.flush()
                handler.close_connection = True
                return
            handler.send_response(status)
            handler.send_header("Content-Type", ctype)
            handler.send_header("Content-Length", str(len(raw)))
            handler.send_header("X-Rate-Limit-Remaining", "650.0")
            for k, v in headers.items():
                handler.send_header(k, v)
            handler.end_headers()
            handler.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # File storage on a different host name (localhost vs 127.0.0.1): the token must never go there.
        self.storage_server = ThreadingHTTPServer(("127.0.0.1", 0), StorageHandler)
        self.storage = f"http://localhost:{self.storage_server.server_address[1]}"
        threading.Thread(target=self.storage_server.serve_forever, daemon=True).start()
        return self.base

    def stop(self) -> None:
        for server in (self.server, self.storage_server):
            if server:
                server.shutdown()


LONG_CJK_NAME = "第三章 债券定价与收益率曲线的基本原理以及久期和凸性在利率风险管理中的应用 " * 3 + ".pdf"

CAPTIONS = """WEBVTT

1
00:00:01.000 --> 00:00:05.000
Today we look at how duration changes as yields move.

2
00:00:05.000 --> 00:00:09.000
This will be on the midterm, so make sure you know how to compute modified duration.

3
00:01:10.000 --> 00:01:15.000
Convexity corrects the linear duration approximation.
"""

SYLLABUS = """<h2>Grading</h2><p>Problem sets 30%, midterm and final 60%, quizzes 10%.</p>
<p>The final exam is cumulative. You may bring one formula sheet (index card) to the midterm.</p>
<p>Formula: <img class="equation_image" title="P=\\sum_{t=1}^{T} \\frac{C}{(1+y)^t}" src="/equation_images/x" data-equation-content="P=\\sum_{t=1}^{T} \\frac{C}{(1+y)^t}" alt="LaTeX: P"></p>
<p>See the <a href="BASE/courses/101/files/1008/download?wrap=1" data-api-endpoint="BASE/api/v1/courses/101/files/1008">handout</a>.</p>"""

PAGES_101 = {
    "reading-guide-week-1": {"title": "Reading Guide Week 1", "updated_at": "2026-08-30T12:00:00Z",
                             "body": "<h2>Read</h2><p>Chapter 3. Use the <a href='BASE/courses/101/files/1008/preview'>handout</a> "
                                     "and the <a href='BASE/courses/101/pages/week-2-recording'>week 2 recording</a>.</p>"
                                     "<p><img src='BASE/courses/101/files/1009/preview' alt='payoff diagram'></p>"
                                     "<p>Also see <a href='https://www.investopedia.com/terms/d/duration.asp'>Investopedia</a>.</p>"},
    "week-2-recording": {"title": "Week 2 Recording", "updated_at": "2026-09-08T12:00:00Z",
                         "body": "<p>Watch before class:</p><iframe title='Week 2 lecture' "
                                 "src='BASE/media_attachments_iframe/1005?type=video&embedded=true'></iframe>"
                                 "<iframe src='https://school.hosted.panopto.com/Panopto/Pages/Embed.aspx?id=abc' title='Panopto clip'></iframe>"},
    "course-policies": {"title": "Course Policies", "updated_at": "2026-08-28T12:00:00Z",
                        "body": "<p>No make-up quizzes. AI tools may be used for studying but not on exams.</p>"},
}

BLOCKS = {
    "ROOT": {"type": {"resolvedName": "PageBlock"}, "isCanvas": True, "props": {}, "nodes": ["h", "t", "b"]},
    "h": {"type": {"resolvedName": "HeadingBlock"}, "props": {"text": "Immunization", "level": "h2"}, "nodes": []},
    "t": {"type": {"resolvedName": "RichTextBlock"},
          "props": {"text": "<p>Match the portfolio's duration to the horizon. This is the main idea for the final.</p>"}},
    "b": {"type": {"resolvedName": "ButtonBlock"}, "props": {"text": "Practice problems", "href": "https://example.com/practice"}},
}

QUIZ_QUESTIONS = [
    {"id": 501, "position": 1, "question_name": "Question", "question_type": "multiple_choice_question",
     "points_possible": 5, "question_text": "<p>When yields rise, bond prices…</p>",
     "answers": [{"id": 1, "text": "rise", "weight": 0}, {"id": 2, "text": "fall", "weight": 100},
                 {"id": 3, "text": "stay the same", "weight": 0}],
     "correct_comments": "Price and yield move in opposite directions."},
    {"id": 502, "position": 2, "question_name": "Duration", "question_type": "numerical_question",
     "points_possible": 5, "question_text": "<p>Modified duration of the bond in the handout?</p>",
     "answers": [{"id": 4, "exact": 4.31, "margin": 0.05, "weight": 100}]},
]


def MODULES_101(base: str, fake: Optional[FakeCanvas] = None):
    notes_id = 1012 if fake is not None and fake.replace_duration_notes else 1002
    week2 = [
        {"id": 7, "type": "File", "title": "Duration Notes", "content_id": notes_id, "position": 1},
        {"id": 8, "type": "File", "title": "Bond Model", "content_id": 1003, "position": 2},
        {"id": 9, "type": "File", "title": "Lecture recording", "content_id": 1004, "position": 3},
        {"id": 10, "type": "Page", "title": "Week 2 Recording", "page_url": "week-2-recording", "position": 4},
        {"id": 11, "type": "Quiz", "title": "Quiz 1: Bond Pricing", "content_id": 7001, "position": 5},
        {"id": 12, "type": "Discussion", "title": "Duration intuition", "content_id": 8001, "position": 6},
    ]
    modules = [
        {"id": 11, "name": "Week 1 - Bond Basics", "position": 1, "items": [
            {"id": 1, "type": "SubHeader", "title": "Lecture", "position": 1},
            {"id": 2, "type": "File", "title": "Lecture 1 slides", "content_id": 1001, "position": 2},
            {"id": 3, "type": "Page", "title": "Reading Guide Week 1", "page_url": "reading-guide-week-1", "position": 3},
            {"id": 4, "type": "Assignment", "title": "Problem Set 1", "content_id": 5001, "position": 4,
             "content_details": {"due_at": "2026-09-10T03:59:00Z", "points_possible": 20}},
            {"id": 5, "type": "ExternalUrl", "title": "Khan Academy: bond prices", "external_url": "https://www.youtube.com/watch?v=abcdefghijk", "position": 5},
            {"id": 6, "type": "ExternalTool", "title": "Pearson MyLab homework", "external_url": "https://mylab.pearson.com/launch", "position": 6,
             "html_url": f"{base}/courses/101/modules/items/6"},
        ]},
        {"id": 12, "name": "Week 2 - Duration and Convexity" if fake is not None and fake.rename_week2 else "Week 2 - Duration",
         "position": 2, "unlock_at": "2026-09-03T12:00:00Z", "items": week2},
    ]
    if fake is not None and fake.extras:
        modules.append({"id": 13, "name": "Week 3 - Immunization", "position": 3, "items": [
            {"id": 20, "type": "Page", "title": "Week 3 Overview", "page_url": "block-page", "position": 1},
            {"id": 21, "type": "Page", "title": "Week 3 Preview", "page_url": "week-3-preview", "position": 2},
        ]})
    return modules


def ASSIGNMENTS_101(base: str, extras: bool = False):
    rubric = [{"id": "c1", "description": "Correct pricing", "points": 10,
               "ratings": [{"id": "r1", "description": "Full", "points": 10}, {"id": "r2", "description": "Partial", "points": 5}]},
              {"id": "c2", "description": "Shows work", "points": 10,
               "ratings": [{"id": "r3", "description": "Clear", "points": 10}, {"id": "r4", "description": "Some", "points": 8}]}]
    if extras:
        rubric[0]["long_description"] = "Price all three bonds\nusing semiannual compounding | annual is wrong"
        rubric[0]["ratings"][1]["long_description"] = "One bond priced\nincorrectly"
    return [
        {"id": 5001, "name": "Problem Set 1", "due_at": "2026-09-10T03:59:00Z", "points_possible": 20, "assignment_group_id": 1,
         "submission_types": ["online_upload"], "html_url": f"{base}/courses/101/assignments/5001",
         "description": "<p>Price the bonds in the <a href='BASE/courses/101/files/1008/download'>handout</a>. Show your work.</p>".replace("BASE", base),
         "rubric": rubric},
        {"id": 5002, "name": "Midterm Exam", "due_at": "2026-10-15T15:00:00Z", "points_possible": 100, "assignment_group_id": 2,
         "submission_types": ["on_paper"], "html_url": f"{base}/courses/101/assignments/5002",
         "description": "<p>Covers Weeks 1-6. Closed book; one formula sheet allowed.</p>"},
        {"id": 5003, "name": "Quiz 1: Bond Pricing", "due_at": "2026-09-12T03:59:00Z", "points_possible": 10, "assignment_group_id": 3,
         "submission_types": ["online_quiz"], "quiz_id": 7001, "html_url": f"{base}/courses/101/assignments/5003"},
        {"id": 5004, "name": "Case Study (MyLab)", "due_at": "2026-11-01T03:59:00Z", "points_possible": 10, "assignment_group_id": 1,
         "submission_types": ["external_tool"], "external_tool_tag_attributes": {"url": "https://mylab.pearson.com/case"},
         "html_url": f"{base}/courses/101/assignments/5004"},
    ] + ([{"id": 5005, "name": "Discussion: yield curves", "due_at": None, "points_possible": 10, "assignment_group_id": 1,
           "submission_types": ["discussion_topic"], "html_url": f"{base}/courses/101/assignments/5005",
           "checkpoints": [{"tag": "reply_to_topic", "name": "Reply to topic", "due_at": "2026-10-20T03:59:00Z", "points_possible": 6},
                           {"tag": "reply_to_entry", "name": "Required replies", "due_at": "2026-10-23T03:59:00Z", "points_possible": 4}]}]
         if extras else [])


def SUBMISSIONS_101(base: str, extras: bool = False):
    comments = [{"author_name": "Prof. Rivera", "comment": "Watch the compounding frequency.",
                 "created_at": "2026-09-12T12:00:00Z"}]
    if extras:
        comments += [{"author_name": "Prof. Rivera", "comment": "", "created_at": "2026-09-12T12:05:00Z",
                      "attachments": [{"id": 9002, "display_name": "PS1 marked up.pdf", "content-type": "application/pdf",
                                       "size": 3000, "updated_at": "2026-09-12T12:05:00Z",
                                       "url": f"{base}/files/9002/download?verifier=v9002"}]},
                     {"author_name": "Prof. Rivera", "comment": "", "created_at": "2026-09-12T12:06:00Z",
                      "media_comment": {"media_id": "m-777", "media_type": "audio", "content-type": "audio/mp4",
                                        "display_name": "feedback", "url": f"{base}/users/42/media_download?entryId=m-777&media_type=audio&redirect=1"}}]
    return [
        {"id": 6001, "assignment_id": 5001, "score": 18, "grade": "18", "workflow_state": "graded", "submitted_at": "2026-09-09T20:00:00Z",
         "late": False, "submission_comments": comments,
         "rubric_assessment": {"c1": {"points": 10, "rating_id": "r1"}, "c2": {"points": 8, "rating_id": "r4", "comments": "Show the discounting steps."}},
         "attachments": [{"id": 9001, "display_name": "PS1 Marc.docx", "size": 5000, "updated_at": "2026-09-09T20:00:00Z",
                          "url": f"{base}/files/9001/download?verifier=v9001"}]},
        {"id": 6003, "assignment_id": 5003, "score": 7, "workflow_state": "graded", "submitted_at": "2026-09-11T20:00:00Z"},
    ]
