"""Local service tests use fake runtime calls and no external API."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import local_rag_service
from local_rag_service import (create_server, Jobs, atomic_json, JOB_AREA, sync_view, kit_view, kit_status,
                               service_readiness)

# Starter-kit (rag.py UNKNOWN) body: the only keys a grader-mode POST /ask may return.
KIT_KEYS = {"answer", "citation", "source_url", "effective_date"}
KIT_UNKNOWN = {"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""}
# Keys of an asynchronous job body (Jobs.public), unchanged by the kit body split.
ASYNC_KEYS = {"job_id", "status", "answer_allowed", "final_answer_generated"}
# Content-Security-Policy of the page and every static file: same origin only.
WEB_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; "
           "form-action 'none'; frame-ancestors 'none'")


def ready():
    return []


class FakeRuntime:
    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.paid = []
        self.cached = set()

    def __call__(self, project, question, check_only=False, *, attempt_id=None):
        identity = hashlib.sha256((question + (attempt_id or "")).encode()).hexdigest()
        if check_only:
            cached = attempt_id is None and question in self.cached
            return {"status": "completed" if cached else "check_only", "cached": cached, "run_identity": identity}
        self.paid.append(question)
        self.started.set()
        self.release.wait(5)
        return {"status": "completed", "run_identity": identity,
                "private_reason": "do-not-expose", "output_paths": {"audit": "private-path"}}


def publicizer(audit, project):
    return {"status": "verified_draft", "response": {"answer": "초안"}, "answer_allowed": False}


def full_publicizer(audit, project):
    return {"status": "verified_draft", "as_of_date": "2026-10-01", "answer_allowed": False,
            "response": {"answer": "초안", "citation": ["서울특별시 예시 조례 제1조"],
                         "source_url": ["https://example.invalid/1"], "effective_date": ["2026-01-01"]},
            "sources": [{"citation": "서울특별시 예시 조례 제1조"}]}


def health():
    return {"as_of_date": "2026-10-01", "database": "private-path", "identity": "fake-db",
            "dimension": 1024, "summary": {"chunk_count": 810}}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="service-test-")
        self.project = Path(self.temp.name)
        self.runtime = FakeRuntime()
        self.server = create_server(self.project, 0, runtime=self.runtime,
                                    publicizer=publicizer, health_provider=health, queue_size=1,
                                    readiness_provider=ready, workers=1)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.runtime.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, value=None, body=None, headers=None):
        status, _, result = self.request_full(method, path, value, body, headers)
        return status, result

    def request_full(self, method, path, value=None, body=None, headers=None):
        """(HTTP status, lower-cased response headers, JSON body)."""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        data = json.dumps(value).encode() if value is not None else body
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=data, headers=request_headers)
        response = connection.getresponse()
        result = (response.status, {k.lower(): v for k, v in response.getheaders()}, json.loads(response.read()))
        connection.close()
        return result

    def assertKitUnknown(self, headers, body, outcome):
        """Grader-mode /ask rejection or non-answer: the four kit keys only, the outcome in the header."""
        self.assertEqual(body, KIT_UNKNOWN)
        self.assertEqual(headers.get("x-rag-status"), outcome)

    def test_health_is_read_only_and_does_not_call_runtime(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["database"]["identity"], "fake-db")
        self.assertNotIn("private-path", json.dumps(body))
        self.assertEqual(self.runtime.paid, [])
        self.assertEqual(list(self.project.rglob("*")), [])

    def test_repeated_question_starts_new_job_and_old_job_remains_pollable(self):
        self.runtime.release.set()
        self.runtime.cached.add("cached")  # A receipt from the previous, unscoped runtime.
        _, headers, first = self.request_full("POST", "/ask", {"question": "cached"})
        first_id = headers["x-rag-job-id"]
        status, view = self.request("POST", "/ui/ask", {"question": "cached"})
        self.assertEqual((status, view["status"]), (200, "completed"))
        self.assertNotEqual(view["job_id"], first_id)
        self.assertEqual(kit_view(self.server.jobs.get(first_id)), first)
        self.assertEqual(self.runtime.paid, ["cached", "cached"])
        reopened = Jobs(self.project, runtime=self.runtime, publicizer=publicizer, health_provider=health)
        try:
            self.assertEqual(kit_view(reopened.get(first_id)), first)
            third_id = reopened.submit("cached")["job_id"]
            self.assertNotIn(third_id, {first_id, view["job_id"]})
            self.assertEqual(reopened.wait(third_id, 2)["status"], "completed")
            self.assertEqual(self.runtime.paid, ["cached"] * 3)
        finally:
            reopened.close()

    def test_slow_preflight_allows_polling_another_completed_job(self):
        self.runtime.release.set()
        jobs = self.server.jobs
        ready_id = jobs.submit("ready")["job_id"]
        self.assertEqual(jobs.wait(ready_id, 2)["status"], "completed")
        entered, resume = threading.Event(), threading.Event()
        preflight = jobs._cached

        def slow_preflight(question, attempt_id):
            entered.set()
            resume.wait(5)
            return preflight(question, attempt_id)

        with patch.object(jobs, "_cached", slow_preflight), ThreadPoolExecutor(2) as requests:
            submission = requests.submit(jobs.submit, "another question")
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(requests.submit(jobs.get, ready_id).result(2)["status"], "completed")
            finally:
                resume.set()
            self.assertEqual(jobs.wait(submission.result(2)["job_id"], 2)["status"], "completed")

    def test_deadline_202_and_singleflight_and_backpressure(self):
        started = time.monotonic()
        status, first = self.request("POST", "/ask", {"question": "one", "wait_seconds": 0.02})
        self.assertEqual(status, 202)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(self.runtime.started.wait(1))
        status, duplicate = self.request("POST", "/ask", {"question": "one", "wait_seconds": 0})
        self.assertEqual(status, 202)
        self.assertEqual(first["job_id"], duplicate["job_id"])
        status, second = self.request("POST", "/ask", {"question": "two", "wait_seconds": 0})
        self.assertEqual(status, 202)
        status, _ = self.request("POST", "/ask", {"question": "three", "wait_seconds": 0})
        self.assertEqual(status, 429)
        self.assertEqual(self.runtime.paid, ["one"])
        self.runtime.release.set()
        self.server.jobs.events[second["job_id"]].wait(2)
        status, final = self.request("GET", "/jobs/" + first["job_id"])
        self.assertEqual(status, 200)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(self.runtime.paid, ["one", "two"])
        text = json.dumps(final)
        for private in ("question", "do-not-expose", "private-path", "private_reason", "output_paths"):
            self.assertNotIn(private, text)

    def test_request_boundaries_and_loopback_origin(self):
        # Grader-mode /ask (no wait_seconds seen): 4xx code kept, four kit keys, reason in the header.
        for value, code in ((["x"], "invalid_request_fields"), ({"db": "evil"}, "invalid_question"),
                            ({"question": " "}, "invalid_question"), ({"question": "x" * 16001}, "invalid_question")):
            status, headers, body = self.request_full("POST", "/ask", value)
            self.assertEqual(status, 400)
            self.assertKitUnknown(headers, body, code)
            self.assertNotIn("x-rag-job-id", headers)
            # The demo path keeps the detailed rejection body.
            status, headers, body = self.request_full("POST", "/ui/ask", value)
            self.assertEqual((status, body["status"], body["answer"], body["citation"]), (400, code, "확인 불가", []))
            self.assertNotIn("x-rag-status", headers)
        # An asynchronous request (wait_seconds present) keeps the existing rejection body on both paths.
        for value in ({"question": "x", "wait_seconds": 76}, {"question": "x", "wait_seconds": 10 ** 1000},
                      {"question": "x", "wait_seconds": True}, {"question": " ", "wait_seconds": 1}):
            for path in ("/ask", "/ui/ask"):
                status, headers, body = self.request_full("POST", path, value)
                self.assertEqual(status, 400)
                self.assertIn(body["status"], ("invalid_wait_seconds", "invalid_question"))
                self.assertEqual((body["answer"], body["citation"]), ("확인 불가", []))
                self.assertNotIn("source_url", body)
                self.assertNotIn("x-rag-status", headers)
        status, headers, body = self.request_full("POST", "/ask", body=b"x" * 32769)
        self.assertEqual(status, 413)
        self.assertKitUnknown(headers, body, "request_too_large")
        status, headers, body = self.request_full("POST", "/ask", {"question": "x"},
                                                  headers={"Origin": "https://other.invalid"})
        self.assertEqual(status, 403)
        self.assertKitUnknown(headers, body, "local_origin_required")
        status, headers, body = self.request_full("POST", "/ask", body=b'{"question":"x","question":"y"}')
        self.assertEqual(status, 400)
        self.assertKitUnknown(headers, body, "invalid_json_request")
        status, headers, body = self.request_full("POST", "/ask", body=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertKitUnknown(headers, body, "application_json_required")
        status, headers, body = self.request_full("POST", "/ui/ask", body=b"x" * 32769)
        self.assertEqual((status, body["status"]), (413, "request_too_large"))
        self.assertNotIn("x-rag-status", headers)
        self.assertEqual(self.runtime.paid, [])

    def test_missing_content_length_on_ask_is_411_with_kit_body(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("POST", "/ask")
        connection.putheader("Content-Type", "application/json")
        connection.endheaders()
        response = connection.getresponse()
        status, headers = response.status, {k.lower(): v for k, v in response.getheaders()}
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(status, 411)
        self.assertKitUnknown(headers, body, "content_length_required")
        self.assertEqual(self.runtime.paid, [])

    def test_restart_interrupts_without_paid_resume_and_recovers_only_cache(self):
        identity = hashlib.sha256(b"restart").hexdigest()
        atomic_json(self.project / JOB_AREA / (identity + ".json"), {
            "job_id": identity, "run_identity": identity, "question": "restart", "status": "running"})
        restarted = Jobs(self.project, runtime=self.runtime, publicizer=publicizer,
                         health_provider=health, queue_size=1)
        try:
            self.assertEqual(restarted.get(identity)["status"], "interrupted")
            self.server.jobs.records[identity] = dict(restarted.records[identity])
            status, body = self.request("GET", "/jobs/" + identity)
            self.assertEqual(status, 409)
            self.assertEqual(body["status"], "interrupted")
            self.assertEqual(self.runtime.paid, [])
            self.runtime.cached.add("restart")
            self.assertEqual(restarted.get(identity)["status"], "completed")
            self.assertEqual(self.runtime.paid, [])
        finally:
            restarted.close()

    def test_worker_identity_drift_stops_before_paid_call_and_reports_500(self):
        checks, paid = [], []
        identity = hashlib.sha256(b"drift").hexdigest()
        def drifting(project, question, check_only=False, *, attempt_id=None):
            if not check_only:
                paid.append(question)
                raise AssertionError("paid call must not run")
            checks.append(1)
            return {"status": "check_only", "run_identity": identity if len(checks) == 1 else "f" * 64}
        jobs = Jobs(self.project, runtime=drifting, publicizer=publicizer, health_provider=health)
        try:
            jobs.submit("drift")
            self.assertTrue(jobs.events[identity].wait(2))
            result = jobs.get(identity)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(paid, [])
            self.server.jobs.records[identity] = dict(jobs.records[identity])
            status, body = self.request("GET", "/jobs/" + identity)
            self.assertEqual(status, 500)
            self.assertEqual(body["result"]["status"], "processing_error")
        finally:
            jobs.close()

    def test_worker_uses_matching_cache_completed_while_queued(self):
        checks, paid = [], []
        identity = hashlib.sha256(b"cache-race").hexdigest()
        def completed(project, question, check_only=False, *, attempt_id=None):
            if not check_only:
                paid.append(question)
                raise AssertionError("matching cache must be reused")
            checks.append(1)
            return {"status": "check_only" if len(checks) == 1 else "completed",
                    "cached": len(checks) > 1, "run_identity": identity}
        jobs = Jobs(self.project, runtime=completed, publicizer=publicizer, health_provider=health)
        try:
            jobs.submit("cache-race")
            self.assertTrue(jobs.events[identity].wait(2))
            self.assertEqual(jobs.get(identity)["status"], "completed")
            self.assertEqual(paid, [])
        finally:
            jobs.close()

    def test_sync_without_wait_seconds_returns_kit_body_on_ask_and_detail_on_ui(self):
        self.server.jobs.publicizer = full_publicizer
        self.runtime.release.set()
        status, headers, body = self.request_full("POST", "/ask", {"question": "sync"})
        self.assertEqual(status, 200)
        # Exactly the starter-kit app.py body: no status/job_id/poll_url/sources/result/flags.
        self.assertEqual(set(body), KIT_KEYS)
        self.assertEqual(body, {"answer": "초안", "citation": ["서울특별시 예시 조례 제1조"],
                                "source_url": "https://example.invalid/1", "effective_date": "2026-01-01"})
        self.assertEqual(headers["x-rag-status"], "completed")
        first_id = headers["x-rag-job-id"]
        # /ui/ask returns the detailed body for a new submission.
        status, headers, body = self.request_full("POST", "/ui/ask", {"question": "sync"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        response = body["result"]["response"]
        for field in ("answer", "citation"):
            self.assertEqual(body[field], response[field])
        self.assertEqual(body["source_url"], response["source_url"][0])
        self.assertEqual(body["effective_date"], response["effective_date"][0])
        self.assertEqual(body["sources"], body["result"]["sources"])
        self.assertEqual(body["as_of_date"], "2026-10-01")
        self.assertNotEqual(body["job_id"], first_id)
        self.assertFalse(body["answer_allowed"])
        self.assertFalse(body["final_answer_generated"])
        self.assertNotIn("x-rag-status", headers)
        self.assertEqual(self.runtime.paid, ["sync", "sync"])

    def test_sync_deadline_keeps_job_running_and_retrievable(self):
        original = local_rag_service.SYNC_WAIT_SECONDS
        local_rag_service.SYNC_WAIT_SECONDS = 0.2
        try:
            status, headers, body = self.request_full("POST", "/ask", {"question": "slow"})
            self.assertEqual(status, 200)
            self.assertKitUnknown(headers, body, "deadline_exceeded")
            job_id = headers["x-rag-job-id"]
            self.assertRegex(job_id, r"^[0-9a-f]{64}$")
            # The demo path shows the same pending job with its poll URL.
            status, ui = self.request("POST", "/ui/ask", {"question": "slow"})
        finally:
            local_rag_service.SYNC_WAIT_SECONDS = original
        self.assertEqual((status, ui["status"], ui["answer"], ui["citation"]), (200, "deadline_exceeded", "확인 불가", []))
        self.assertEqual((ui["job_id"], ui["poll_url"]), (job_id, "/jobs/" + job_id))
        status, pending = self.request("GET", "/jobs/" + job_id)
        self.assertEqual((status, pending["status"]), (202, "running"))
        self.runtime.release.set()
        self.assertTrue(self.server.jobs.events[job_id].wait(2))
        status, final = self.request("GET", "/jobs/" + job_id)
        self.assertEqual((status, final["status"]), (200, "completed"))
        self.assertEqual(self.runtime.paid, ["slow"])

    def test_explicit_wait_zero_stays_async(self):
        for path, question in (("/ask", "async"), ("/ui/ask", "async")):
            status, headers, body = self.request_full("POST", path, {"question": question, "wait_seconds": 0})
            self.assertEqual(status, 202, path)
            self.assertNotIn("answer", body)
            self.assertEqual(set(body), ASYNC_KEYS | {"poll_url"})
            self.assertEqual(body["poll_url"], "/jobs/" + body["job_id"])
            self.assertEqual(headers["retry-after"], "2")
            self.assertNotIn("x-rag-status", headers)
            self.assertNotIn("x-rag-job-id", headers)

    def test_async_completed_body_is_unchanged(self):
        self.runtime.release.set()
        for path in ("/ask", "/ui/ask"):
            status, headers, body = self.request_full("POST", path, {"question": "cached", "wait_seconds": 1})
            self.assertEqual(status, 200, path)
            self.assertEqual(set(body), ASYNC_KEYS | {"result"})
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["result"]["response"], {"answer": "초안"})
            self.assertNotIn("x-rag-status", headers)
        self.assertEqual(self.runtime.paid, ["cached", "cached"])

    def test_readability_through_grader_ui_and_poll_preserves_saved_answer(self):
        record = full_publicizer({}, None)
        original = "주차요금의 100분의 50을 감면할 수 있습니다."
        expected = "주차요금의 50%를 감면할 수 있습니다."
        record["response"]["answer"] = original
        self.server.jobs.publicizer = lambda audit, project: record
        self.runtime.release.set()
        status, headers, response = self.request_full("POST", "/ask", {"question": "할인"})
        self.assertEqual((status, response["answer"]), (200, expected))
        self.assertEqual(response["citation"], record["response"]["citation"])
        identity = headers["x-rag-job-id"]
        status, view = self.request("POST", "/ui/ask", {"question": "할인"})
        self.assertEqual((status, view["answer"], view["result"]["response"]["answer"]), (200, expected, expected))
        status, view = self.request("GET", "/jobs/" + identity)
        self.assertEqual((status, view["result"]["response"]["answer"]), (200, expected))
        self.assertEqual(self.server.jobs.records[identity]["result"]["response"]["answer"], original)
        self.assertEqual(self.runtime.paid, ["할인", "할인"])

    def test_confirmed_unknown_has_an_inquiry_url_separate_from_legal_citations(self):
        self.runtime.release.set()
        url = "https://www.epeople.go.kr/"
        for decision in ("insufficient_evidence", "out_of_scope"):
            with self.subTest(decision=decision):
                self.server.jobs.publicizer = lambda audit, project: {
                    "status": decision, "response": {"answer": "확인 불가", "citation": []}}
                status, headers, body = self.request_full("POST", "/ask", {"question": decision})
                self.assertEqual((status, headers["x-rag-status"], body), (200, "completed", KIT_UNKNOWN))
                self.assertEqual(headers["link"], f'<{url}>; rel="help"')
                _, job = self.request("GET", "/jobs/" + headers["x-rag-job-id"])
                _, ui = self.request("POST", "/ui/ask", {"question": decision})
                for view in (job, ui):
                    self.assertEqual(view["result"]["inquiry_url"], url)
                    self.assertEqual(view["result"]["response"], {"answer": "확인 불가", "citation": []})
        for result, expected_status in ((full_publicizer({}, None), "completed"),
                                        ({"status": "code_check_failed"}, "processing_error"),
                                        ({"status": "processing_error"}, "processing_error")):
            self.server.jobs.publicizer = lambda audit, project: result
            _, headers, _ = self.request_full("POST", "/ask", {"question": "다른 결과"})
            self.assertEqual(headers["x-rag-status"], expected_status)
            self.assertNotIn("link", headers)
            _, job = self.request("GET", "/jobs/" + headers["x-rag-job-id"])
            self.assertNotIn("inquiry_url", job["result"])

    def test_kit_view_shape_for_every_job_state(self):
        result = full_publicizer({}, None)
        result["response"]["source_url"] = ["https://example.invalid/a", "https://example.invalid/b"]
        result["response"]["effective_date"] = ["2025-01-01", "2026-01-01"]
        job = {"job_id": "a" * 64, "status": "completed", "result": result}
        view = kit_view(job)
        self.assertEqual(list(view), ["answer", "citation", "source_url", "effective_date"])
        self.assertEqual(view, {"answer": "초안", "citation": ["서울특별시 예시 조례 제1조"],
                                "source_url": "https://example.invalid/a", "effective_date": "2025-01-01"})
        view["citation"].append("changed")
        self.assertEqual(result["response"]["citation"], ["서울특별시 예시 조례 제1조"])
        # Scalar metadata is kept; empty lists, missing or non-string metadata become "".
        for source_url, effective_date, expected in (
                ("https://example.invalid/s", "2026-02-02", ("https://example.invalid/s", "2026-02-02")),
                ([], [], ("", "")), (None, 20260101, ("", "")), ([None], [5], ("", ""))):
            response = {"answer": "답", "citation": ["제1조"], "source_url": source_url, "effective_date": effective_date}
            view = kit_view({"job_id": "a" * 64, "status": "completed", "result": {"response": response}})
            self.assertEqual((view["source_url"], view["effective_date"]), expected)
            self.assertEqual(set(view), KIT_KEYS)
        view = kit_view({"job_id": "a" * 64, "status": "completed", "result": {"response": {"answer": "답"}}})
        self.assertEqual(view, {"answer": "답", "citation": [], "source_url": "", "effective_date": ""})
        # A legal "확인 불가" answer: published as is, metadata empty.
        unknown = {"job_id": "a" * 64, "status": "completed",
                   "result": {"status": "insufficient_evidence", "response": {"answer": "확인 불가", "citation": []}}}
        self.assertEqual(kit_view(unknown), KIT_UNKNOWN)
        self.assertEqual(kit_status(unknown), "completed")
        # Every non-answer state is the kit's "확인 불가".
        for job, outcome in (
                ({"job_id": "b" * 64, "status": "queued"}, "deadline_exceeded"),
                ({"job_id": "b" * 64, "status": "running"}, "deadline_exceeded"),
                ({"job_id": "b" * 64, "status": "failed", "result": {"status": "processing_error"}}, "failed"),
                ({"job_id": "b" * 64, "status": "interrupted"}, "interrupted"),
                ({"job_id": "b" * 64, "status": "completed",
                  "result": {"status": "processing_error", "response": {"answer": "초안"}}}, "processing_error"),
                ({"job_id": "b" * 64, "status": "completed", "result": {"response": {"answer": None}}},
                 "processing_error"),
                ({"job_id": "b" * 64, "status": "completed", "result": "broken"}, "processing_error"),
                # A completed job without a kit-shaped response is not published.
                ({"job_id": "b" * 64, "status": "completed", "result": {"status": "verified_draft"}},
                 "processing_error"),
                ({"job_id": "b" * 64, "status": "completed"}, "processing_error"),
                ({"job_id": "b" * 64, "status": "unknown_state"}, "processing_error")):
            with self.subTest(job=job):
                self.assertEqual(kit_view(job), KIT_UNKNOWN)
                self.assertEqual(kit_status(job), outcome)
        # A citation that is not a list of strings is not published.
        for citation in ("서울특별시 예시 조례 제1조", None, ["제1조", 2], [None], {"a": 1}):
            job = {"job_id": "c" * 64, "status": "completed",
                   "result": {"status": "verified_draft", "response": {"answer": "답", "citation": citation,
                              "source_url": "https://example.invalid/1", "effective_date": "2026-01-01"}}}
            with self.subTest(citation=citation):
                self.assertEqual(kit_view(job), KIT_UNKNOWN)
                self.assertEqual(kit_status(job), "processing_error")
        self.assertEqual(kit_view(None), KIT_UNKNOWN)
        self.assertIsNot(kit_view(None)["citation"], kit_view(None)["citation"])

    def test_sync_view_uses_first_citation_metadata_and_keeps_all_sources(self):
        result = full_publicizer({}, None)
        sources = [
            {"citation": "조례 가 제1조", "source_url": "https://example.invalid/a", "effective_date": "2025-01-01"},
            {"citation": "조례 나 제2조", "source_url": "https://example.invalid/b", "effective_date": "2026-01-01"},
        ]
        result["sources"] = sources
        for field in ("citation", "source_url", "effective_date"):
            result["response"][field] = [source[field] for source in sources]
        for shared_date in (False, True):
            if shared_date:
                result["response"]["effective_date"] = sources[1]["effective_date"] = sources[0]["effective_date"]
            view = sync_view({"status": "completed", "job_id": "a" * 64, "result": result})
            self.assertEqual(view["source_url"], sources[0]["source_url"])
            self.assertEqual(view["effective_date"], sources[0]["effective_date"])
            self.assertEqual(view["citation"], [source["citation"] for source in sources])
            self.assertEqual(view["sources"], sources)
            self.assertEqual(view["result"], result)

    def test_sync_view_unknown_result_adds_no_source_fields(self):
        job = {"job_id": "a" * 64, "status": "completed", "answer_allowed": False, "final_answer_generated": False,
               "result": {"status": "insufficient_evidence", "as_of_date": "2026-10-01",
                          "response": {"answer": "확인 불가", "citation": []}}}
        view = sync_view(job)
        self.assertEqual((view["answer"], view["citation"], view["status"]), ("확인 불가", [], "completed"))
        self.assertNotIn("source_url", view)
        self.assertNotIn("effective_date", view)
        self.assertEqual(view["result"], job["result"])
        failed = sync_view({"job_id": "b" * 64, "status": "failed", "result": {"status": "processing_error"}})
        self.assertEqual((failed["answer"], failed["citation"], failed["status"]), ("확인 불가", [], "failed"))

    def test_non_loopback_host_accepts_tunnel_hosts_and_loopback_keeps_protection(self):
        self.server.bound_host = "0.0.0.0"
        port = str(self.server.server_port)
        # Tunnels and proxies rewrite Host: any name, any or no port is accepted.
        for host in ("grader.internal:" + port, "grader.internal:1", "abc.tunnel.example", "203.0.113.5:443"):
            status, _ = self.request("GET", "/health", headers={"Host": host})
            self.assertEqual(status, 200, host)
        status, _ = self.request("GET", "/health", headers={"Host": "grader.internal:" + port,
                                                             "Origin": "http://grader.internal:" + port})
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/health", headers={"Host": "abc.tunnel.example",
                                                             "Origin": "https://abc.tunnel.example"})
        self.assertEqual(status, 200)
        status, headers, body = self.request_full("POST", "/ask", {"question": "x"},
                                                  headers={"Host": "abc.tunnel.example", "Origin": "https://other.invalid"})
        self.assertEqual(status, 403)
        self.assertKitUnknown(headers, body, "local_origin_required")
        status, body = self.request("POST", "/ui/ask", {"question": "x"},
                                    headers={"Host": "abc.tunnel.example", "Origin": "https://other.invalid"})
        self.assertEqual((status, body["status"], body["answer"], body["citation"]),
                         (403, "local_origin_required", "확인 불가", []))
        status, body = self.request("GET", "/health", headers={"Host": "grader.internal:" + port,
                                                                "Origin": "https://other.invalid"})
        self.assertEqual(status, 403)
        self.assertNotIn("answer", body)
        self.server.bound_host = "127.0.0.1"
        for host in ("grader.internal:" + port, "abc.tunnel.example", "127.0.0.1:1"):
            status, _ = self.request("GET", "/health", headers={"Host": host})
            self.assertEqual(status, 403, host)
        self.assertEqual(self.runtime.paid, [])

    def raw_get(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        result = (response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
        connection.close()
        return result

    def web_root(self, files=None):
        """Patch WEB_ROOT/WEB_INDEX to a temporary web root outside the project folder."""
        temp = tempfile.TemporaryDirectory(prefix="service-web-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "web"
        root.mkdir()
        for name, data in (files or {}).items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for name, value in (("WEB_ROOT", root), ("WEB_INDEX", root / "index.html")):
            patcher = patch.object(local_rag_service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return root

    def assertWebHeaders(self, headers, body, content_type):
        self.assertEqual(headers["content-type"], content_type)
        self.assertEqual(int(headers["content-length"]), len(body))
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["content-security-policy"], WEB_CSP)
        self.assertEqual(headers["connection"], "close")

    def assertNotFound(self, path):
        status, headers, body = self.raw_get(path)
        self.assertEqual(status, 404, path)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8", path)
        self.assertEqual(json.loads(body)["status"], "not_found", path)

    def test_demo_page_is_served_without_runtime_or_writes(self):
        for path in ("/", "/index.html"):
            status, headers, body = self.raw_get(path)
            self.assertEqual(status, 200, path)
            self.assertWebHeaders(headers, body, "text/html; charset=utf-8")
            self.assertEqual(body, local_rag_service.WEB_INDEX.read_bytes())
        self.assertEqual(local_rag_service.WEB_INDEX, local_rag_service.WEB_ROOT / "index.html")
        self.assertEqual(self.runtime.paid, [])
        self.assertEqual(list(self.project.rglob("*")), [])

    def test_demo_page_keeps_host_origin_checks_and_unknown_paths(self):
        self.web_root({"index.html": b"<!DOCTYPE html><title>t</title>"})
        port = str(self.server.server_port)
        for headers in ({"Host": "evil.example:" + port}, {"Host": "127.0.0.1:1"},
                        {"Host": "127.0.0.1:" + port, "Origin": "https://other.invalid"}):
            status, _, body = self.raw_get("/", headers=headers)
            self.assertEqual(status, 403, headers)
            self.assertEqual(json.loads(body)["status"], "local_origin_required")
        status, _, _ = self.raw_get("/", headers={"Host": "localhost:" + port})
        self.assertEqual(status, 200)
        for path in ("/index.htm", "/web/index.html", "/src/web/index.html", "/../src/web/index.html",
                     "/?q=1", "/favicon.ico", "/INDEX.HTML", "/Index.html"):
            self.assertNotFound(path)
        for path in ("/", "/ASK", "/ask/", "/ui/ask/", "/ui", "/ask?x=1", "/index.html"):
            status, headers, body = self.request_full("POST", path, {"question": "x"})
            self.assertEqual((status, body["status"]), (404, "not_found"), path)
            self.assertNotIn("x-rag-status", headers)
        self.assertEqual(self.runtime.paid, [])

    def test_submission_checker_serves_static_page_without_submitting(self):
        content = b"<!DOCTYPE html><title>submission checker</title>"
        root = self.web_root({"test.html": content})
        for path in ("/test", "/test/"):
            status, headers, body = self.raw_get(path)
            self.assertEqual((status, body), (200, content), path)
            self.assertWebHeaders(headers, body, "text/html; charset=utf-8")
        (root / "test.html").unlink()
        self.assertNotFound("/test")
        self.assertEqual(self.runtime.paid, [])

    def test_submission_checker_forwards_only_question(self):
        route = "/test/submission"
        result = {"http_status": 403, "body": "error code: 1010", "content_type": "text/plain",
                  "rag_status": None, "elapsed_ms": 25}
        with patch.object(self.server.submission_check, "run", return_value=(200, result)) as outbound:
            for kwargs, code in (({"value": {"question": "x", "url": "http://other.invalid"}}, 400),
                                 ({"value": {"question": "x", "wait_seconds": 1}}, 400),
                                 ({"value": {"question": " "}}, 400),
                                 ({"value": {"question": "x"}, "headers": {"Origin": "https://other.invalid"}}, 403),
                                 ({"body": b"x" * 32769}, 413),
                                 ({"body": b"{}", "headers": {"Content-Type": "text/plain"}}, 415)):
                status, body = self.request("POST", route, **kwargs)
                self.assertEqual((status, set(body)), (code, {"error", "elapsed_ms"}))
            outbound.assert_not_called()
            status, body = self.request("POST", route, {"question": "서울시 조례?"})
            self.assertEqual((status, body), (200, result))
            outbound.assert_called_once_with("서울시 조례?")
        self.assertEqual(self.runtime.paid, [])

    def test_demo_page_on_non_loopback_follows_tunnel_host_rules(self):
        self.server.bound_host = "0.0.0.0"
        try:
            for host in ("abc.tunnel.example", "grader.internal:1", "203.0.113.5:443"):
                status, headers, _ = self.raw_get("/", headers={"Host": host})
                self.assertEqual((status, headers["content-type"]), (200, "text/html; charset=utf-8"), host)
            status, _, _ = self.raw_get("/", headers={"Host": "abc.tunnel.example",
                                                      "Origin": "https://abc.tunnel.example"})
            self.assertEqual(status, 200)
            status, _, body = self.raw_get("/", headers={"Host": "abc.tunnel.example",
                                                         "Origin": "https://other.invalid"})
            self.assertEqual((status, json.loads(body)["status"]), (403, "local_origin_required"))
        finally:
            self.server.bound_host = "127.0.0.1"
        self.assertEqual(self.runtime.paid, [])

    def test_demo_page_missing_file_is_404(self):
        with patch.object(local_rag_service, "WEB_INDEX", self.project / "missing" / "index.html"):
            status, _, body = self.raw_get("/")
        self.assertEqual((status, json.loads(body)["status"]), (404, "not_found"))
        self.web_root({})
        for path in ("/", "/index.html"):
            self.assertNotFound(path)

    def test_static_files_are_served_from_web_root_without_runtime_or_writes(self):
        files = {"index.html": b"<!DOCTYPE html><title>t</title>", "app.css": b"body{color:#123}",
                 "assets/app.js": b"console.log(1);", "img/logo.png": b"\x89PNG\r\n\x1a\n\x00\x01",
                 "fonts/a.woff2": b"wOF2\x00\x01", "about.html": "<p>안내</p>".encode("utf-8"),
                 "data/x.json": b"{}", "m.mjs": b"export {};", "i.svg": b"<svg/>", "f.ico": b"\x00\x00\x01\x00",
                 "p.jpg": b"\xff\xd8", "p.jpeg": b"\xff\xd8", "p.gif": b"GIF89a", "p.webp": b"RIFF",
                 "f.woff": b"wOFF", "my_app-v1.2.css": b"b{}"}
        root = self.web_root(files)
        expected = {"/app.css": "text/css; charset=utf-8", "/assets/app.js": "text/javascript; charset=utf-8",
                    "/img/logo.png": "image/png", "/fonts/a.woff2": "font/woff2",
                    "/about.html": "text/html; charset=utf-8", "/data/x.json": "application/json; charset=utf-8",
                    "/m.mjs": "text/javascript; charset=utf-8", "/i.svg": "image/svg+xml",
                    "/f.ico": "image/x-icon", "/p.jpg": "image/jpeg", "/p.jpeg": "image/jpeg",
                    "/p.gif": "image/gif", "/p.webp": "image/webp", "/f.woff": "font/woff",
                    "/my_app-v1.2.css": "text/css; charset=utf-8"}
        for path, content_type in expected.items():
            status, headers, body = self.raw_get(path)
            self.assertEqual(status, 200, path)
            self.assertWebHeaders(headers, body, content_type)
            self.assertEqual(body, (root / path[1:]).read_bytes(), path)
        for path in ("/", "/index.html"):
            status, headers, body = self.raw_get(path)
            self.assertEqual((status, body), (200, files["index.html"]), path)
            self.assertWebHeaders(headers, body, "text/html; charset=utf-8")
        self.assertEqual(self.runtime.paid, [])
        self.assertEqual(list(self.project.rglob("*")), [])

    def test_static_files_reject_unsafe_paths_types_and_sizes(self):
        outside = tempfile.TemporaryDirectory(prefix="service-outside-")
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / "secret.js").write_bytes(b"secret")
        root = self.web_root({"index.html": b"<!DOCTYPE html>", "app.css": b"a{}", "assets/app.js": b"1;",
                              ".env": b"KEY=1", ".git/config": b"[core]", "app.py": b"x=1",
                              "noext": b"x", "app.js.map": b"{}", "UPPER.CSS": b"a{}"})
        (root.parent / "x.css").write_bytes(b"outside")
        big = root / "big.png"
        with big.open("wb") as stream:
            stream.truncate(local_rag_service.WEB_STATIC_MAX_BYTES + 1)
        too_long = "/" + "a" * 200 + ".css"
        for path in ("/../x.css", "/assets/../../secret.js", "/.env", "/.git/config", "/assets//app.js",
                     "/app.css?v=1", "/app.py", "/assets", "/assets/", "/noext", "/%2e%2e/x.css", "/a\\b.css",
                     "/missing.css", "/big.png", "/app.js.map", "/UPPER.CSS", "/APP.css", "/app.css.",
                     too_long, "/./app.css", "/assets/./app.js", "/app.css#x", "/app%2ecss"):
            self.assertNotFound(path)
        # Exactly WEB_STATIC_MAX_BYTES is still served.
        with big.open("r+b") as stream:
            stream.truncate(local_rag_service.WEB_STATIC_MAX_BYTES)
        status, _, body = self.raw_get("/big.png")
        self.assertEqual((status, len(body)), (200, local_rag_service.WEB_STATIC_MAX_BYTES))
        self.assertEqual(self.runtime.paid, [])
        self.assertEqual(list(self.project.rglob("*")), [])

    def test_static_symlink_leaving_web_root_is_404(self):
        outside = tempfile.TemporaryDirectory(prefix="service-outside-")
        self.addCleanup(outside.cleanup)
        secret = Path(outside.name) / "secret.js"
        secret.write_bytes(b"secret")
        root = self.web_root({"index.html": b"<!DOCTYPE html>"})
        try:
            (root / "link.js").symlink_to(secret)
            (root / "linkdir").symlink_to(Path(outside.name), target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("symlinks unavailable: " + type(error).__name__)
        for path in ("/link.js", "/linkdir/secret.js"):
            self.assertNotFound(path)
        self.assertEqual(self.runtime.paid, [])

    @unittest.skipUnless(os.name == "nt", "Windows directory junction")
    def test_static_junction_leaving_web_root_is_404(self):
        import _winapi
        outside = tempfile.TemporaryDirectory(prefix="service-outside-")
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / "secret.js").write_bytes(b"secret")
        root = self.web_root({"index.html": b"<!DOCTYPE html>"})
        _winapi.CreateJunction(outside.name, str(root / "junction"))
        self.assertTrue((root / "junction" / "secret.js").is_file())
        self.assertNotFound("/junction/secret.js")
        self.assertEqual(self.runtime.paid, [])

    def test_static_files_keep_host_origin_rules(self):
        self.web_root({"index.html": b"<!DOCTYPE html>", "app.css": b"a{}"})
        port = str(self.server.server_port)
        for headers in ({"Host": "evil.example:" + port}, {"Host": "127.0.0.1:1"},
                        {"Host": "127.0.0.1:" + port, "Origin": "https://other.invalid"},
                        {"Host": "127.0.0.1:" + port, "Origin": "http://127.0.0.1:5173"}):
            status, _, body = self.raw_get("/app.css", headers=headers)
            self.assertEqual((status, json.loads(body)["status"]), (403, "local_origin_required"), headers)
        status, _, body = self.raw_get("/app.css", headers={"Host": "localhost:" + port,
                                                            "Origin": "http://localhost:" + port})
        self.assertEqual((status, body), (200, b"a{}"))
        self.server.bound_host = "0.0.0.0"
        try:
            for host in ("abc.tunnel.example", "grader.internal:1", "203.0.113.5:443"):
                status, _, body = self.raw_get("/app.css", headers={"Host": host})
                self.assertEqual((status, body), (200, b"a{}"), host)
            status, _, body = self.raw_get("/app.css", headers={"Host": "abc.tunnel.example",
                                                                "Origin": "https://other.invalid"})
            self.assertEqual((status, json.loads(body)["status"]), (403, "local_origin_required"))
        finally:
            self.server.bound_host = "127.0.0.1"
        self.assertEqual(self.runtime.paid, [])

    def test_api_paths_take_precedence_over_static_files(self):
        identity = hashlib.sha256(b"cached").hexdigest()
        # Same-named file and folder entries in the web root never shadow the API.
        self.web_root({"index.html": b"<!DOCTYPE html>", "health/x.json": b"{}",
                       "jobs/" + identity + "/a.json": b"{}"})
        status, headers, body = self.raw_get("/health")
        self.assertEqual((status, headers["content-type"]), (200, "application/json; charset=utf-8"))
        self.assertEqual(json.loads(body)["status"], "ok")
        status, _, body = self.raw_get("/jobs/" + identity)
        self.assertEqual((status, json.loads(body)["status"]), (404, "job_not_found"))
        self.runtime.release.set()
        status, submitted = self.request("POST", "/ui/ask", {"question": "cached"})
        identity = submitted["job_id"]
        self.assertEqual(status, 200)
        status, _, body = self.raw_get("/jobs/" + identity)
        self.assertEqual((status, json.loads(body)["job_id"]), (200, identity))
        status, _, body = self.raw_get("/health/x.json")
        self.assertEqual((status, body), (200, b"{}"))
        self.assertEqual(self.runtime.paid, ["cached"])

    def test_unknown_request_fields_are_ignored(self):
        self.runtime.release.set()
        status, headers, body = self.request_full("POST", "/ask", {"question": "cached", "db": "evil", "top_k": 99})
        self.assertEqual((status, headers["x-rag-status"], set(body)), (200, "completed", KIT_KEYS))
        status, body = self.request("POST", "/ui/ask", {"question": "cached", "db": "evil", "top_k": 99})
        self.assertEqual((status, body["status"]), (200, "completed"))
        status, body = self.request("POST", "/ask", {"question": "cached", "extra": {"x": 1}, "wait_seconds": 1})
        self.assertEqual((status, body["status"]), (200, "completed"))
        self.assertEqual(self.runtime.paid, ["cached"] * 3)

    # ---- 1. failed / interrupted jobs are requeued once on resubmission

    def test_resubmitted_failed_job_is_requeued_once(self):
        paid = []
        def flaky(project, question, check_only=False, *, attempt_id=None):
            identity = hashlib.sha256((question + attempt_id).encode()).hexdigest()
            if check_only:
                return {"status": "check_only", "cached": False, "run_identity": identity}
            paid.append(question)
            if len(paid) != 2:
                raise RuntimeError("provider down")
            return {"status": "completed", "run_identity": identity}
        jobs = Jobs(self.project, runtime=flaky, publicizer=publicizer, health_provider=health,
                    readiness_provider=ready)
        try:
            identity = jobs.submit("flaky")["job_id"]
            self.assertTrue(jobs.events[identity].wait(2))
            self.assertEqual(jobs.get(identity)["status"], "failed")
            first_event = jobs.events[identity]
            self.assertEqual(jobs.submit("flaky")["status"], "queued")
            self.assertIsNot(jobs.events[identity], first_event)
            self.assertTrue(jobs.events[identity].wait(2))
            done = jobs.get(identity)
            self.assertEqual(done["status"], "completed")
            self.assertNotIn("requeues", done)
            saved = json.loads((self.project / JOB_AREA / (identity + ".json")).read_bytes())
            self.assertEqual((saved["status"], saved["requeues"]), ("completed", 1))
            self.assertEqual(paid, ["flaky", "flaky"])
            next_job = jobs.submit("flaky")
            self.assertNotEqual(next_job["job_id"], identity)
            self.assertEqual(jobs.get(identity)["status"], "completed")
        finally:
            jobs.close()

    def test_requeue_limit_is_one_and_survives_restart(self):
        identity = hashlib.sha256(b"broken").hexdigest()
        paid = []
        def broken(project, question, check_only=False, *, attempt_id=None):
            if check_only:
                return {"status": "check_only", "cached": False, "run_identity": identity}
            paid.append(question)
            raise RuntimeError("provider down")
        jobs = Jobs(self.project, runtime=broken, publicizer=publicizer, health_provider=health,
                    readiness_provider=ready)
        try:
            jobs.submit("broken")
            self.assertTrue(jobs.events[identity].wait(2))
            jobs.submit("broken")
            self.assertTrue(jobs.events[identity].wait(2))
            self.assertEqual(jobs.get(identity)["status"], "failed")
            for _ in range(3):
                result = jobs.submit("broken")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["result"]["status"], "processing_error")
            self.assertEqual(paid, ["broken", "broken"])
        finally:
            jobs.close()
        restarted = Jobs(self.project, runtime=broken, publicizer=publicizer, health_provider=health,
                         readiness_provider=ready)
        try:
            self.assertEqual(restarted.submit("broken")["status"], "failed")
            self.assertEqual(len(paid), 2)
        finally:
            restarted.close()

    def test_interrupted_job_is_requeued_on_resubmission_not_on_restart(self):
        attempt_id = "interrupted-attempt"
        identity = hashlib.sha256(("restart" + attempt_id).encode()).hexdigest()
        atomic_json(self.project / JOB_AREA / (identity + ".json"), {
            "job_id": identity, "run_identity": identity, "question": "restart", "status": "running",
            "attempt_id": attempt_id, "created_ns": 10})
        self.runtime.release.set()
        restarted = Jobs(self.project, runtime=self.runtime, publicizer=publicizer,
                         health_provider=health, queue_size=1, readiness_provider=ready)
        try:
            self.assertEqual(restarted.get(identity)["status"], "interrupted")
            self.assertEqual(self.runtime.paid, [])
            self.assertEqual(restarted.submit("restart")["status"], "queued")
            self.assertTrue(restarted.events[identity].wait(2))
            self.assertEqual(restarted.get(identity)["status"], "completed")
            self.assertEqual(self.runtime.paid, ["restart"])
        finally:
            restarted.close()
        # A requeued job interrupted again stays interrupted: the limit is reached.
        atomic_json(self.project / JOB_AREA / (identity + ".json"), {
            "job_id": identity, "run_identity": identity, "question": "restart", "status": "running",
            "attempt_id": attempt_id, "created_ns": 10,
            "requeues": 1})
        again = Jobs(self.project, runtime=self.runtime, publicizer=publicizer,
                     health_provider=health, queue_size=1, readiness_provider=ready)
        try:
            self.assertEqual(again.submit("restart")["status"], "interrupted")
            self.assertEqual(self.runtime.paid, ["restart"])
        finally:
            again.close()

    def test_requeue_with_full_queue_returns_429_and_keeps_failed_record(self):
        attempt_id = "queued-attempt"
        identity = hashlib.sha256(("one" + attempt_id).encode()).hexdigest()
        self.server.jobs.latest["one"] = identity
        self.server.jobs.records[identity] = {"job_id": identity, "run_identity": identity, "question": "one",
                                              "status": "failed", "cached": False, "attempt_id": attempt_id, "created_ns": 1,
                                              "result": {"status": "processing_error"}}
        status, _ = self.request("POST", "/ask", {"question": "busy-1", "wait_seconds": 0})
        self.assertEqual(status, 202)
        self.assertTrue(self.runtime.started.wait(1))
        status, _ = self.request("POST", "/ask", {"question": "busy-2", "wait_seconds": 0})
        self.assertEqual(status, 202)
        status, body = self.request("POST", "/ask", {"question": "one", "wait_seconds": 0})
        self.assertEqual((status, body["status"]), (429, "queue_full"))
        self.assertEqual(self.server.jobs.records[identity]["status"], "failed")
        self.assertNotIn("requeues", self.server.jobs.records[identity])
        # The same rejection of a grader-mode request is HTTP 200 with the four kit keys only.
        status, headers, body = self.request_full("POST", "/ask", {"question": "one"})
        self.assertEqual(status, 200)
        self.assertKitUnknown(headers, body, "queue_full")
        self.assertNotIn("x-rag-job-id", headers)
        # The demo path keeps the detailed rejection body.
        status, body = self.request("POST", "/ui/ask", {"question": "one"})
        self.assertEqual((status, body["status"], body["answer"], body["citation"]),
                         (200, "queue_full", "확인 불가", []))
        self.assertEqual(self.server.jobs.records[identity]["status"], "failed")

    def test_server_side_rejections_are_200_for_sync_and_keep_codes_for_async(self):
        def preflight_fails(project, question, check_only=False, *, attempt_id=None):
            return {"status": "processing_error"}
        def submit_crashes(question):
            raise OSError("private-path")
        cases = (("runtime", preflight_fails, 503, "processing_error"),
                 ("submit", submit_crashes, 500, "processing_error"))
        for target, replacement, async_code, code in cases:
            with self.subTest(target=target):
                owner = self.server.jobs
                with patch.object(owner, target, replacement):
                    for path in ("/ask", "/ui/ask"):
                        status, headers, body = self.request_full("POST", path, {"question": "x", "wait_seconds": 0})
                        self.assertEqual((status, body["status"]), (async_code, code))
                        self.assertEqual((body["answer"], body["citation"]), ("확인 불가", []))
                        self.assertNotIn("x-rag-status", headers)
                    status, headers, body = self.request_full("POST", "/ask", {"question": "x"})
                    self.assertEqual(status, 200)
                    self.assertKitUnknown(headers, body, code)
                    self.assertNotIn("x-rag-job-id", headers)
                    status, body = self.request("POST", "/ui/ask", {"question": "x"})
                    self.assertEqual((status, body["status"]), (200, code))
                    self.assertEqual((body["answer"], body["citation"]), ("확인 불가", []))
                    self.assertNotIn("private-path", json.dumps(body))
        # Invalid requests keep their 4xx codes in synchronous mode too.
        status, headers, body = self.request_full("POST", "/ask", {"question": " "})
        self.assertEqual(status, 400)
        self.assertKitUnknown(headers, body, "invalid_question")
        status, body = self.request("POST", "/ui/ask", {"question": " "})
        self.assertEqual((status, body["status"]), (400, "invalid_question"))
        status, _ = self.request("POST", "/ask", {"question": "x"}, headers={"Origin": "https://other.invalid"})
        self.assertEqual(status, 403)
        self.assertEqual(self.runtime.paid, [])

    def test_invalid_public_result_is_operational_failure_on_each_endpoint(self):
        self.runtime.release.set()
        self.server.jobs.publicizer = lambda audit, project: "not-a-dict"
        status, headers, body = self.request_full("POST", "/ask", {"question": "q"})
        self.assertEqual(status, 200)
        self.assertKitUnknown(headers, body, "failed")
        status, body = self.request("POST", "/ui/ask", {"question": "q"})
        self.assertEqual((status, body["status"]), (200, "failed"))
        self.assertNotIn("epeople.go.kr", json.dumps(body))

    def test_grader_ask_failed_and_interrupted_jobs_are_kit_unknown(self):
        identity = hashlib.sha256(b"broken").hexdigest()
        paid = []
        def broken(project, question, check_only=False, *, attempt_id=None):
            if check_only:
                return {"status": "check_only", "cached": False, "run_identity": identity}
            paid.append(question)
            raise RuntimeError("private-path")
        self.server.jobs.runtime = broken
        # First run fails, the resubmission is requeued once and fails again, then no more paid runs.
        for attempt in range(3):
            status, headers, body = self.request_full("POST", "/ask", {"question": "broken"})
            self.assertEqual(status, 200, attempt)
            self.assertKitUnknown(headers, body, "failed")
            self.assertEqual(headers["x-rag-job-id"], identity)
        self.assertEqual(paid, ["broken", "broken"])
        status, body = self.request("POST", "/ui/ask", {"question": "broken"})
        self.assertEqual((status, body["status"], body["result"]["status"]), (200, "failed", "processing_error"))
        self.assertEqual(len(paid), 2)
        # An interrupted job whose requeue is used up stays interrupted.
        attempt_id = "interrupted-attempt"
        interrupted = hashlib.sha256(("stopped" + attempt_id).encode()).hexdigest()
        self.server.jobs.latest["stopped"] = interrupted
        self.server.jobs.records[interrupted] = {"job_id": interrupted, "run_identity": interrupted,
                                                 "question": "stopped", "status": "interrupted", "requeues": 1,
                                                 "attempt_id": attempt_id, "created_ns": 1}
        self.server.jobs.runtime = self.runtime
        status, headers, body = self.request_full("POST", "/ask", {"question": "stopped"})
        self.assertEqual(status, 200)
        self.assertKitUnknown(headers, body, "interrupted")
        self.assertEqual(headers["x-rag-job-id"], interrupted)
        self.assertEqual(self.runtime.paid, [])

    # ---- 2. /health reports readiness for new answers

    def test_health_503_with_reason_codes_when_not_ready(self):
        self.server.jobs.readiness_provider = lambda: ["missing_upstage_api_key", "missing_dependency_requests"]
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["reasons"], ["missing_upstage_api_key", "missing_dependency_requests"])
        self.assertNotIn("database", body)
        def broken_db():
            raise OSError("private-path")
        self.server.jobs.readiness_provider = ready
        self.server.jobs.health_provider = broken_db
        status, body = self.request("GET", "/health")
        self.assertEqual((status, body["reasons"]), (503, ["database_unavailable"]))
        self.assertNotIn("private-path", json.dumps(body))
        def raising():
            raise RuntimeError("secret")
        self.server.jobs.readiness_provider = raising
        self.server.jobs.health_provider = health
        status, body = self.request("GET", "/health")
        self.assertEqual((status, body["reasons"]), (503, ["readiness_check_failed"]))
        self.assertNotIn("secret", json.dumps(body))
        self.assertEqual(self.runtime.paid, [])

    def test_health_diagnostics_do_not_disable_the_runtime(self):
        self.server.jobs.readiness_provider = lambda: ["missing_upstage_api_key"]
        self.runtime.release.set()
        status, headers, body = self.request_full("POST", "/ask", {"question": "cached"})
        self.assertEqual((status, headers["x-rag-status"], body["answer"]), (200, "completed", "초안"))

    def test_service_readiness_checks_key_and_dependencies_without_exposing_key(self):
        secret = "fake-test-value-do-not-print-0123456789"
        env_file = self.project / ".env"
        with patch.dict(os.environ, {"UPSTAGE_API_KEY": secret}):
            self.assertEqual(service_readiness(self.project), [])
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UPSTAGE_API_KEY", None)
            self.assertEqual(service_readiness(self.project), ["missing_upstage_api_key"])
            env_file.write_text("UPSTAGE_API_KEY=" + secret + "\n", encoding="utf-8")
            self.assertEqual(service_readiness(self.project), [])
            env_file.write_text("UPSTAGE_API_KEY=   \n", encoding="utf-8")
            self.assertEqual(service_readiness(self.project), ["missing_upstage_api_key"])
            env_file.write_text("UPSTAGE_API_KEY=" + secret + "\n", encoding="utf-8")
            real_import = local_rag_service.importlib.import_module
            def no_requests(name, *args, **kwargs):
                if name == "requests":
                    raise ImportError(name)
                return real_import(name, *args, **kwargs)
            with patch.object(local_rag_service.importlib, "import_module", side_effect=no_requests):
                self.assertEqual(service_readiness(self.project), ["missing_dependency_requests"])
            def no_dotenv(name, *args, **kwargs):
                if name == "dotenv":
                    raise ImportError(name)
                return real_import(name, *args, **kwargs)
            with patch.object(local_rag_service.importlib, "import_module", side_effect=no_dotenv):
                # The .env file cannot be read without python-dotenv, as in the client.
                self.assertEqual(service_readiness(self.project),
                                 ["missing_dependency_dotenv", "missing_upstage_api_key"])
        # Through the server: the key never appears in the /health body.
        self.server.jobs.readiness_provider = lambda: service_readiness(self.project)
        status, raw_body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotIn(secret, json.dumps(raw_body))

    # ---- 4. concurrency with workers=1 (the previous single-worker service): bounded queue, every request answered in time

    def _server(self, runtime, queue_size=8, workers=1):
        server = create_server(self.project, 0, runtime=runtime, publicizer=publicizer,
                               health_provider=health, queue_size=queue_size, readiness_provider=ready,
                               workers=workers)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(2)
        return server, stop

    @staticmethod
    def _concurrent(port, bodies, timeout=10):
        results = [None] * len(bodies)
        def send(index):
            started = time.monotonic()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            connection.request("POST", "/ask", body=json.dumps(bodies[index]).encode(),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            results[index] = (response.status, json.loads(response.read()), time.monotonic() - started, headers)
            connection.close()
        threads = [threading.Thread(target=send, args=(i,)) for i in range(len(bodies))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout + 5)
        return results

    def test_ten_concurrent_sync_requests_fill_queue_and_all_answer_before_deadline(self):
        runtime = FakeRuntime()
        active, peak, lock = [0], [0], threading.Lock()
        def tracked(project, question, check_only=False, *, attempt_id=None):
            if check_only:
                return runtime(project, question, check_only=True, attempt_id=attempt_id)
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                return runtime(project, question, attempt_id=attempt_id)
            finally:
                with lock:
                    active[0] -= 1
        server, stop = self._server(tracked, queue_size=8)
        original = local_rag_service.SYNC_WAIT_SECONDS
        local_rag_service.SYNC_WAIT_SECONDS = 0.5
        try:
            results = self._concurrent(server.server_port, [{"question": "q%d" % i} for i in range(10)])
        finally:
            local_rag_service.SYNC_WAIT_SECONDS = original
        try:
            self.assertTrue(all(results), results)
            # 1 running + 8 queued keep their jobs; the 10th is refused at once. Grader-mode
            # callers get HTTP 200 and the kit's "확인 불가" for both, told apart by X-Rag-Status.
            self.assertEqual({status for status, _, _, _ in results}, {200})
            self.assertEqual(sorted(headers["x-rag-status"] for _, _, _, headers in results),
                             ["deadline_exceeded"] * 9 + ["queue_full"])
            for status, body, elapsed, _ in results:
                self.assertEqual(body, KIT_UNKNOWN)
                self.assertLess(elapsed, 0.5 + 8.0)
            self.assertNotIn("x-rag-job-id",
                             [headers for _, _, _, headers in results if headers["x-rag-status"] == "queue_full"][0])
            self.assertEqual(len(runtime.paid), 1)
            runtime.release.set()
            jobs = [headers["x-rag-job-id"] for _, _, _, headers in results
                    if headers["x-rag-status"] == "deadline_exceeded"]
            for job_id in jobs:
                self.assertTrue(server.jobs.events[job_id].wait(5))
                self.assertEqual(server.jobs.get(job_id)["status"], "completed")
            self.assertEqual(len(runtime.paid), 9)
            self.assertEqual(peak[0], 1)
        finally:
            runtime.release.set()
            stop()

    def test_ten_concurrent_requests_complete_in_order_with_one_worker(self):
        delay = 0.05
        active, peak, paid, lock = [0], [0], [], threading.Lock()
        def quick(project, question, check_only=False, *, attempt_id=None):
            identity = hashlib.sha256((question + (attempt_id or "")).encode()).hexdigest()
            if check_only:
                return {"status": "check_only", "cached": False, "run_identity": identity}
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                paid.append(question)
            time.sleep(delay)
            with lock:
                active[0] -= 1
            return {"status": "completed", "run_identity": identity}
        server, stop = self._server(quick, queue_size=8)
        try:
            # Overlapping submissions share a job; a request arriving after completion starts another.
            results = self._concurrent(server.server_port, [{"question": "same"}] * 10)
            self.assertTrue(all(results), results)
            self.assertEqual({(status, headers["x-rag-status"]) for status, _, _, headers in results},
                             {(200, "completed")})
            same_runs = len({headers["x-rag-job-id"] for _, _, _, headers in results})
            self.assertEqual(paid, ["same"] * same_runs)
            # Ten distinct questions in two waves of five: all answered, never two runs at once.
            for wave in (range(0, 5), range(5, 10)):
                results = self._concurrent(server.server_port, [{"question": "d%d" % i} for i in wave])
                self.assertTrue(all(results), results)
                self.assertEqual({(status, headers["x-rag-status"]) for status, _, _, headers in results},
                                 {(200, "completed")})
                for _, body, elapsed, _ in results:
                    self.assertEqual(set(body), KIT_KEYS)
                    self.assertEqual(body["answer"], "초안")
                    self.assertLess(elapsed, 5 * delay + 8.0)
            self.assertEqual(sorted(paid), sorted(["same"] * same_runs + ["d%d" % i for i in range(10)]))
            self.assertEqual(peak[0], 1)
        finally:
            stop()

    # ---- 5. several workers (default 4 workers + 12 queued)

    @staticmethod
    def _tracked_runtime(delay=0.0, release=None):
        """Fake paid runtime recording concurrency; `release` blocks each paid run until set."""
        state = {"active": 0, "peak": 0, "paid": [], "lock": threading.Lock()}
        def runtime(project, question, check_only=False, *, attempt_id=None):
            identity = hashlib.sha256((question + (attempt_id or "")).encode()).hexdigest()
            if check_only:
                return {"status": "check_only", "cached": False, "run_identity": identity}
            with state["lock"]:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                state["paid"].append(question)
            try:
                if release is not None:
                    release.wait(10)
                time.sleep(delay)
            finally:
                with state["lock"]:
                    state["active"] -= 1
            return {"status": "completed", "run_identity": identity}
        return runtime, state

    def test_defaults_are_four_workers_and_twelve_queued(self):
        jobs = Jobs(self.project, runtime=self.runtime, publicizer=publicizer, health_provider=health,
                    readiness_provider=ready)
        try:
            self.assertEqual((jobs.workers, jobs.queue_size, jobs.executor._max_workers), (4, 12, 4))
            self.assertIsNone(jobs.gate)  # injected runtime: no client options, call shape unchanged
            health_body = jobs.health()
            self.assertEqual((health_body["workers"], health_body["queue_size"]), (4, 12))
        finally:
            jobs.close()
        status, body = self.request("GET", "/health")
        self.assertEqual((status, body["workers"], body["queue_size"]), (200, 1, 1))
        for bad in ({"workers": 0}, {"workers": 17}, {"workers": True}, {"workers": 2.0},
                    {"queue_size": -1}, {"queue_size": 65}, {"queue_size": None}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                create_server(self.project, 0, runtime=self.runtime, publicizer=publicizer,
                              health_provider=health, readiness_provider=ready, **bad)
        server = create_server(self.project, 0, runtime=self.runtime, publicizer=publicizer,
                               health_provider=health, readiness_provider=ready, workers=16, queue_size=64)
        try:
            self.assertEqual((server.jobs.workers, server.jobs.queue_size), (16, 64))
            # Pending connection backlog for many simultaneous grader connections.
            self.assertEqual(server.request_queue_size, 64)
        finally:
            server.server_close()

    def test_default_runtime_passes_parallel_deadline_and_shared_gate(self):
        import ask_local_rag
        from local_rag_client import SendGate
        calls = []
        def fake_run_question(project, question, check_only=False, client_options=None, *, attempt_id=None):
            calls.append((question, check_only, client_options))
            return {"status": "check_only", "run_identity": "0" * 64}
        with patch.object(ask_local_rag, "run_question", fake_run_question):
            jobs = Jobs(self.project, publicizer=publicizer, health_provider=health, readiness_provider=ready)
        try:
            self.assertIsInstance(jobs.gate, SendGate)
            jobs.runtime(self.project, "q1", check_only=True)
            jobs.runtime(self.project, "q2", check_only=False)
            self.assertEqual([(q, c) for q, c, _ in calls], [("q1", True), ("q2", False)])
            for _, _, options in calls:
                self.assertEqual(set(options), {"parallel", "deadline_seconds", "gate"})
                self.assertIs(options["parallel"], True)
                self.assertEqual(options["deadline_seconds"], local_rag_service.CLIENT_DEADLINE_SECONDS)
                self.assertEqual(options["deadline_seconds"], 75)
                self.assertIs(options["gate"], jobs.gate)
            self.assertIsNot(calls[0][2], calls[1][2])
        finally:
            jobs.close()

    def test_four_workers_answer_ten_distinct_questions_concurrently(self):
        delay = 0.5
        runtime, state = self._tracked_runtime(delay=delay)
        server, stop = self._server(runtime, queue_size=12, workers=4)
        try:
            started = time.monotonic()
            results = self._concurrent(server.server_port, [{"question": "p%d" % i} for i in range(10)])
            total = time.monotonic() - started
            self.assertTrue(all(results), results)
            self.assertEqual({(status, headers["x-rag-status"]) for status, _, _, headers in results},
                             {(200, "completed")})
            for _, body, _, _ in results:
                self.assertEqual(set(body), KIT_KEYS)
                self.assertEqual(body["answer"], "초안")
            self.assertEqual(sorted(state["paid"]), sorted("p%d" % i for i in range(10)))
            self.assertGreaterEqual(state["peak"], 2)
            self.assertLessEqual(state["peak"], 4)
            # Ten runs of 0.5 s one after another take 5.0 s; four workers take about 1.5 s.
            self.assertLess(total, 10 * delay)
            # Every job record written by the concurrent workers is valid JSON and completed.
            records = sorted((self.project / JOB_AREA).glob("*.json"))
            self.assertEqual(len(records), 10)
            for path in records:
                record = json.loads(path.read_bytes())
                self.assertEqual((record["job_id"], record["status"]), (path.stem, "completed"))
            self.assertEqual(list((self.project / JOB_AREA).glob("*.tmp")), [])
        finally:
            stop()

    def test_four_workers_twelve_queued_accept_sixteen_of_twenty_slow_questions(self):
        release = threading.Event()
        runtime, state = self._tracked_runtime(release=release)
        server, stop = self._server(runtime, queue_size=12, workers=4)
        original = local_rag_service.SYNC_WAIT_SECONDS
        local_rag_service.SYNC_WAIT_SECONDS = 0.5
        try:
            results = self._concurrent(server.server_port, [{"question": "s%d" % i} for i in range(20)])
        finally:
            local_rag_service.SYNC_WAIT_SECONDS = original
        try:
            self.assertTrue(all(results), results)
            self.assertEqual({status for status, _, _, _ in results}, {200})
            outcomes = sorted(headers["x-rag-status"] for _, _, _, headers in results)
            self.assertEqual(outcomes, ["deadline_exceeded"] * 16 + ["queue_full"] * 4)
            for status, body, elapsed, headers in results:
                self.assertEqual(body, KIT_UNKNOWN)
                self.assertLess(elapsed, 0.5 + 8.0)
                self.assertEqual("x-rag-job-id" in headers, headers["x-rag-status"] != "queue_full")
            # Only the four running jobs have started a paid run.
            self.assertEqual(len(state["paid"]), 4)
            release.set()
            accepted = [headers["x-rag-job-id"] for _, _, _, headers in results
                        if headers["x-rag-status"] == "deadline_exceeded"]
            for job_id in accepted:
                self.assertTrue(server.jobs.events[job_id].wait(5))
                self.assertEqual(server.jobs.get(job_id)["status"], "completed")
            self.assertEqual(len(state["paid"]), 16)
            self.assertEqual(len(set(state["paid"])), 16)
            self.assertLessEqual(state["peak"], 4)
        finally:
            release.set()
            stop()

    def test_four_workers_same_question_ten_times_is_one_paid_run(self):
        runtime, state = self._tracked_runtime(delay=0.1)
        server, stop = self._server(runtime, queue_size=12, workers=4)
        try:
            results = self._concurrent(server.server_port, [{"question": "same"}] * 10)
            self.assertTrue(all(results), results)
            self.assertEqual({(status, headers["x-rag-status"]) for status, _, _, headers in results},
                             {(200, "completed")})
            self.assertEqual([body["answer"] for _, body, _, _ in results], ["초안"] * 10)
            self.assertEqual(len({headers["x-rag-job-id"] for _, _, _, headers in results}), 1)
            self.assertEqual(state["paid"], ["same"])
        finally:
            stop()

    def test_rate_limited_runtime_is_kit_unknown_in_time_and_requeued_once(self):
        """run_question returns processing_error (e.g. RateLimitedNoResend) without a run identity."""
        identity = hashlib.sha256(b"limited").hexdigest()
        paid = []
        def limited(project, question, check_only=False, *, attempt_id=None):
            if check_only:
                return {"status": "check_only", "cached": False, "run_identity": identity}
            paid.append(question)
            return {"status": "processing_error", "error_type": "RateLimitedNoResend",
                    "error": "rate_limited_no_resend", "called": 1, "answer_allowed": False}
        server, stop = self._server(limited, queue_size=12, workers=4)
        try:
            for attempt in range(3):
                started = time.monotonic()
                status, headers, body = self.request_full_at(server, "POST", "/ask", {"question": "limited"})
                self.assertLess(time.monotonic() - started, 8.0)
                self.assertEqual(status, 200, attempt)
                self.assertKitUnknown(headers, body, "failed")
                self.assertEqual(headers["x-rag-job-id"], identity)
                self.assertEqual(server.jobs.get(identity)["status"], "failed")
            # First run, one requeue on resubmission, then no more paid runs.
            self.assertEqual(paid, ["limited", "limited"])
            saved = json.loads((self.project / JOB_AREA / (identity + ".json")).read_bytes())
            self.assertEqual((saved["status"], saved["requeues"], saved["result"]["status"]),
                             ("failed", 1, "processing_error"))
            self.assertNotIn("rate_limited", json.dumps(saved))
        finally:
            stop()

    def request_full_at(self, server, method, path, value):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(method, path, body=json.dumps(value).encode(),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = (response.status, {k.lower(): v for k, v in response.getheaders()}, json.loads(response.read()))
        connection.close()
        return result

    def test_main_reads_workers_and_queue_size_from_arguments_then_environment(self):
        class Stop(Exception):
            pass
        seen = []
        def capture(project, port, **options):
            seen.append(options)
            raise Stop()
        cases = (([], {}, (4, 12)),
                 ([], {"LOCAL_RAG_WORKERS": "6", "LOCAL_RAG_QUEUE_SIZE": "20"}, (6, 20)),
                 (["--workers", "2", "--queue-size", "0"], {"LOCAL_RAG_WORKERS": "6"}, (2, 0)),
                 ([], {"LOCAL_RAG_WORKERS": " "}, (4, 12)))
        for argv, environment, expected in cases:
            with self.subTest(argv=argv, environment=environment):
                seen.clear()
                with patch.dict(os.environ, environment, clear=False), \
                        patch.object(local_rag_service, "create_server", capture):
                    for name in ("LOCAL_RAG_WORKERS", "LOCAL_RAG_QUEUE_SIZE"):
                        if name not in environment:
                            os.environ.pop(name, None)
                    with self.assertRaises(Stop):
                        local_rag_service.main(["--project", str(self.project)] + argv)
                self.assertEqual((seen[0]["workers"], seen[0]["queue_size"]), expected)
        for argv, environment in ((["--workers", "0"], {}), (["--workers", "17"], {}),
                                  (["--queue-size", "65"], {}), ([], {"LOCAL_RAG_WORKERS": "four"}),
                                  ([], {"LOCAL_RAG_QUEUE_SIZE": "-1"})):
            with self.subTest(argv=argv, environment=environment):
                with patch.dict(os.environ, environment, clear=False), \
                        patch.object(local_rag_service, "create_server", capture), \
                        patch("sys.stderr"):
                    for name in ("LOCAL_RAG_WORKERS", "LOCAL_RAG_QUEUE_SIZE"):
                        if name not in environment:
                            os.environ.pop(name, None)
                    with self.assertRaises(SystemExit) as caught:
                        local_rag_service.main(["--project", str(self.project)] + argv)
                self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
