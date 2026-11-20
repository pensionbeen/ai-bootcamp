"""Request-level audit storage and authenticated operator routes."""
import base64
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import local_rag_service
from request_audit import AUDIT_PATH, RequestAudit
from test_local_rag_service import FakeRuntime, full_publicizer, health, ready


class RequestAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_lazy_persisted_records_redact_headers_and_restart_pending(self):
        audit = RequestAudit(self.project)
        self.assertFalse((self.project / AUDIT_PATH).exists())
        seq = audit.begin({"Authorization": "secret", "Cookie": "secret", "User-Agent": "grader",
                           "CF-Connecting-IP": "198.51.100.2", "CF-Ray": "sample"}, "10.1.1.2")
        audit.update(seq, question="  질문  ", request={"question": "  질문  "})
        next_seq = audit.begin({}, "10.1.1.2")
        audit.update(next_seq, http_status=400, response={"answer": "확인 불가"}, write_outcome="written")
        restarted = RequestAudit(self.project)
        rows = restarted.page()["items"]
        self.assertEqual([r["seq"] for r in rows], [next_seq, seq])
        self.assertEqual(rows[1]["write_outcome"], "interrupted")
        self.assertEqual(rows[1]["question"], "  질문  ")
        self.assertEqual(rows[1]["reported_client_ip"], "198.51.100.2")
        self.assertNotIn("secret", json.dumps(rows))

    def test_legacy_order_once_and_bounded_pagination(self):
        folder = self.project / "jobs"
        folder.mkdir()
        for job, modified in (("first", 100), ("last", 200)):
            path = folder / (job + ".json")
            path.write_text("{}")
            os.utime(path, (modified, modified))
        legacy = [{"job_id": job, "question": job, "status": "completed", "response": {"answer": "ok"}}
                  for job in ("last", "first")]
        audit = RequestAudit(self.project, legacy, folder)
        rows = audit.page()["items"]
        self.assertEqual([r["question"] for r in rows], ["last", "first"])
        self.assertTrue(all(r["received_at"] is None and r["http_status"] is None for r in rows))
        self.assertEqual(RequestAudit(self.project, legacy, folder).page()["total"], 2)
        with patch("request_audit.MAX_RECORDS", 3), patch("request_audit.PAGE_SIZE", 2):
            for _ in range(3):
                audit.begin({}, "peer")
            page = audit.page()
            self.assertEqual(page["total"], 3)
            self.assertEqual(len(page["items"]), 2)
            older = audit.page(page["next_before"])
            self.assertEqual(len(older["items"]), 1)
            self.assertIsNone(older["next_before"])

    def test_disconnected_response_headers_or_body_are_recorded(self):
        for failed_operation in ("end_headers", "write"):
            with self.subTest(operation=failed_operation):
                handler = local_rag_service.Handler.__new__(local_rag_service.Handler)
                handler._audit_seq = 1
                handler._request_started = time.monotonic()
                handler._audit = Mock()
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.wfile = Mock()
                if failed_operation == "write":
                    handler.wfile.write.side_effect = BrokenPipeError
                else:
                    handler.end_headers.side_effect = BrokenPipeError
                handler._send(400, {"answer": "확인 불가"}, {"X-Rag-Status": "invalid_question"})
                details = handler._audit.call_args.kwargs
                self.assertEqual(details["write_outcome"], "disconnected")
                self.assertEqual(details["http_status"], 400)
                self.assertEqual(details["rag_status"], "invalid_question")


class RequestAuditHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.runtime = FakeRuntime()
        self.runtime.release.set()
        with patch.dict(os.environ, {"REQUEST_LOG_PASSWORD": "테스트-password"}):
            self.server = local_rag_service.create_server(self.project, 0, runtime=self.runtime,
                publicizer=full_publicizer, health_provider=health, readiness_provider=ready)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.auth = "Basic " + base64.b64encode("admin:테스트-password".encode()).decode()

    def tearDown(self):
        self.runtime.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, value=None, auth=None, raw=None, extra=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        body = json.dumps(value).encode() if value is not None else raw
        headers = {"Content-Type": "application/json", **(extra or {})}
        if auth is not None:
            headers["Authorization"] = auth
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def rows(self):
        # The body may reach the caller just before its socket-write audit update commits.
        deadline = time.monotonic() + 2
        while True:
            code, _, raw = self.request("GET", "/admin/requests/data", auth=self.auth)
            self.assertEqual(code, 200)
            rows = json.loads(raw)["items"]
            if all(r["write_outcome"] != "pending" for r in rows) or time.monotonic() > deadline:
                return rows
            time.sleep(.01)

    def test_auth_protects_html_data_and_direct_static_path(self):
        for path in ("/admin/requests", "/admin/requests/", "/admin/requests/data", "/request-log.html"):
            code, headers, _ = self.request("GET", path)
            self.assertEqual(code, 401)
            self.assertIn("Basic", headers["WWW-Authenticate"])
            self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("GET", "/admin/requests/data", auth="Basic !!!")[0], 401)
        self.assertEqual(self.request("GET", "/admin/requests/data", auth=self.auth)[0], 200)
        for suffix in ("?before=0", "?before=1%20OR%201=1", "?before=1&before=2", "?unknown=1"):
            self.assertEqual(self.request("GET", "/admin/requests/data" + suffix, auth=self.auth)[0], 400)
        self.server.request_log_password = ""
        self.assertEqual(self.request("GET", "/admin/requests/data", auth=self.auth)[0], 503)

    def test_repeated_requests_exact_question_and_response_and_rejection(self):
        question = "  question  "
        for _ in range(2):
            code, _, raw = self.request("POST", "/ask", {"question": question, "api_key": "private"},
                                        extra={"User-Agent": "grader", "Cookie": "private"})
            self.assertEqual(code, 200)
            response = json.loads(raw)
            self.assertEqual(set(response), {"answer", "citation", "source_url", "effective_date"})
        code, _, raw = self.request("POST", "/ask", {"question": " "})
        self.assertEqual(code, 400)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["http_status"], 400)
        self.assertEqual(rows[0]["rag_status"], "invalid_question")
        self.assertEqual(rows[1]["response"], response)
        self.assertEqual(rows[1]["question"], question)
        self.assertEqual(rows[1]["request"], {"question": question})
        self.assertNotEqual(rows[1]["request_id"], rows[2]["request_id"])
        self.assertNotIn("private", json.dumps(rows))
        self.assertEqual(rows[1]["write_outcome"], "written")

    def test_malformed_body_origin_denial_and_audit_failure_do_not_change_api(self):
        self.assertEqual(self.request("POST", "/ask", raw=b'{"broken"')[0], 400)
        self.assertEqual(self.request("POST", "/ask", {"question": "question"}, extra={"Origin": "https://evil.invalid"})[0], 403)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["question"] is None for row in rows))
        with patch.object(self.server.request_audit, "begin", side_effect=OSError):
            code, _, raw = self.request("POST", "/ask", {"question": "question"})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)["answer"], "초안")
