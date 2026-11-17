"""Public submission-check transport using loopback HTTP or simulated network errors."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch
import urllib.error

import submission_http
from submission_http import SubmissionCheck


class Upstream(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        self.server.capture.append((self.command, self.path, self.headers, self.rfile.read(length)))
        status, headers, body = self.server.reply
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class SubmissionTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.server.capture = []
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.check = SubmissionCheck(self.origin)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_python_request_and_raw_upstream_results(self):
        question = "서울시 청년?"
        for code, headers, text in (
            (200, {"Content-Type": "application/json; charset=utf-8", "X-Rag-Status": "completed"}, '{"answer":"답"}'),
            (403, {"Content-Type": "text/plain"}, "error code: 1010"),
            (302, {"Location": self.origin + "/other"}, "redirect"),
        ):
            self.server.capture.clear()
            self.server.reply = code, headers, text.encode()
            status, report = self.check.run(question)
            self.assertEqual(status, 200)
            self.assertGreaterEqual(report.pop("elapsed_ms"), 0)
            self.assertEqual(report, {"http_status": code, "body": text, "content_type": headers.get("Content-Type", ""),
                                      "rag_status": headers.get("X-Rag-Status")})
            self.assertEqual(len(self.server.capture), 1)
            method, path, received, raw = self.server.capture[0]
            self.assertEqual((method, path, raw), ("POST", "/ask", json.dumps({"question": question}, ensure_ascii=False).encode()))
            self.assertEqual(received["Content-Type"], "application/json; charset=utf-8")
            self.assertTrue(received["User-Agent"].startswith("Python-urllib/"))
            self.assertNotIn("Cookie", received)

    def test_invalid_config_busy_body_bounds_and_timeout(self):
        for url in (None, "http://example.invalid/other", "file:///tmp/test", "https://[invalid"):
            status, result = SubmissionCheck(url).run("a")
            self.assertEqual((status, result["error"]), (503, "submission_check_not_configured"))
        with self.check.slots, self.check.slots:
            status, result = self.check.run("a")
            self.assertEqual((status, result["error"]), (429, "submission_check_busy"))
        for data, reason in ((b"A" * (submission_http.MAX_RESPONSE_BYTES + 1), "upstream_body_too_large"),
                             (b"\xff", "upstream_invalid_utf8")):
            self.server.reply = 200, {}, data
            status, result = self.check.run("a")
            self.assertEqual((status, result["error"]), (502, reason))
        with patch("urllib.request.OpenerDirector.open", side_effect=urllib.error.URLError(TimeoutError())):
            status, result = self.check.run("a")
            self.assertEqual((status, result["error"]), (504, "upstream_timeout"))
