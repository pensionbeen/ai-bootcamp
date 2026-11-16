"""check_submission against the in-process fake service from test_local_rag_service (no external API)."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_submission  # noqa: E402
import local_rag_service  # noqa: E402
from local_rag_service import create_server, Handler  # noqa: E402
from test_local_rag_service import FakeRuntime, full_publicizer, health  # noqa: E402


class LegacyHandler(Handler):
    """An older service that answered every POST /ask with 202 + job_id (asynchronous only)."""

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._send(202, self.server.jobs.submit(request["question"]))


class CheckSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="service-test-")
        self.runtime = FakeRuntime()
        self.server = create_server(Path(self.temp.name), 0, runtime=self.runtime, publicizer=full_publicizer,
                                    health_provider=health, queue_size=1)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.runtime.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def test_completed_job_passes(self):
        self.runtime.release.set()
        verdict = check_submission.check_question(self.url, "완료 질문", timeout=10)
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual((verdict["status_code"], verdict["status"]), (200, "completed"))
        self.assertEqual(verdict["citation"], ["서울특별시 예시 조례 제1조"])
        self.assertEqual(self.runtime.paid, ["완료 질문"])

    def test_sync_deadline_exceeded_fails(self):
        original = local_rag_service.SYNC_WAIT_SECONDS
        local_rag_service.SYNC_WAIT_SECONDS = 0.2
        try:
            verdict = check_submission.check_question(self.url, "느린 질문", timeout=10)
        finally:
            local_rag_service.SYNC_WAIT_SECONDS = original
        self.assertFalse(verdict["ok"])
        self.assertEqual((verdict["status_code"], verdict["reason"]), (200, "deadline_exceeded"))

    def test_legacy_202_fails(self):
        self.server.RequestHandlerClass = LegacyHandler
        verdict = check_submission.check_question(self.url, "옛 서버", timeout=10)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["status_code"], 202)
        self.assertTrue(verdict["reason"].startswith("http_202"), verdict)

    def _fake_reply(self, body, headers=None, code=200):
        """check_question against a canned HTTP reply (no server)."""
        with patch("check_submission.urllib.request.urlopen") as send:
            response = send.return_value.__enter__.return_value
            response.status = code
            response.headers = headers or {}
            response.read.return_value = json.dumps(body).encode()
            return check_submission.check_question(self.url, "질문")

    def test_kit_format_rejects_list_metadata_and_accepts_unknown_without_sources(self):
        url, date = "https://example.invalid/1", "2026-01-01"
        for source_url, effective_date, reason in (
                ([url], date, "source_url_or_effective_date_is_not_str"),
                (url, [date], "source_url_or_effective_date_is_not_str"),
                (url, "20260101", "invalid_source_url_or_effective_date"),
                ("", "", "invalid_source_url_or_effective_date")):
            with self.subTest(url=source_url, date=effective_date):
                verdict = self._fake_reply({"answer": "답", "citation": ["제1조"],
                                            "source_url": source_url, "effective_date": effective_date})
                self.assertEqual(verdict["reason"], reason)
        verdict = self._fake_reply({"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""},
                                   {"X-Rag-Status": "completed"})
        self.assertEqual((verdict["ok"], verdict["status"]), (True, "completed"))
        self.assertTrue(check_submission.kit_source_format({"answer": "확인 불가", "citation": []}))

    def test_body_must_have_exactly_the_four_kit_keys(self):
        good = {"answer": "답", "citation": ["제1조"], "source_url": "https://example.invalid/1",
                "effective_date": "2026-01-01"}
        self.assertTrue(self._fake_reply(good, {"X-Rag-Status": "completed"})["ok"])
        verdict = self._fake_reply({**good, "status": "completed", "job_id": "a" * 64})
        self.assertEqual(verdict["reason"], "unexpected_fields: job_id,status")
        verdict = self._fake_reply({"answer": "확인 불가", "citation": []})
        self.assertEqual(verdict["reason"], "missing_fields: source_url,effective_date")
        # A status in the body is not read: the outcome comes from the header only.
        self.assertIsNone(self._fake_reply({**good, "status": "failed"})["status"])

    def test_bad_outcome_header_fails_a_valid_unknown_body(self):
        unknown = {"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""}
        # A full queue is also something the grader would only see as "확인 불가".
        self.assertIn("queue_full", check_submission.BAD_STATUSES)
        for outcome in check_submission.BAD_STATUSES:
            with self.subTest(outcome=outcome):
                verdict = self._fake_reply(unknown, {"X-Rag-Status": outcome})
                self.assertEqual((verdict["ok"], verdict["status"], verdict["reason"]), (False, outcome, outcome))
        # Without the header there is nothing to tell a legal "확인 불가" from an error.
        verdict = self._fake_reply(unknown)
        self.assertEqual((verdict["ok"], verdict["status"]), (True, None))

    def test_foreign_host_header_on_loopback_bind_fails_403(self):
        verdict = check_submission.check_question(self.url, "호스트", timeout=10, host_header="grader.internal:1")
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "http_403 (local_origin_required)")

    def test_main_exit_code(self):
        self.runtime.release.set()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(check_submission.main(["--url", self.url, "--question", "질문"]), 0)
            self.server.RequestHandlerClass = LegacyHandler
            self.assertEqual(check_submission.main(["--url", self.url, "--question", "다른 질문", "--timeout", "10"]), 1)
        self.assertIn("PASS: ok", out.getvalue())
        self.assertIn("FAIL: http_202", out.getvalue())


if __name__ == "__main__":
    unittest.main()
