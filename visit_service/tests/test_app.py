import base64
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from visit_service.app import create_server, parse_upstream
from visit_service.audit import DOCUMENT_PATHS, VisitAudit


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.command, self.path, dict(self.headers)))
        mode = self.server.mode
        if mode == "timeout":
            time.sleep(0.2)
        body = b"original document bytes\x00\xff"
        if mode == "large":
            body = b"x" * 1025
        status = 302 if mode == "redirect" else 404 if mode == "404" else 200
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Original", "retained")
        self.send_header("Connection", "close, X-Hop-Response")
        self.send_header("X-Hop-Response", "removed")
        if mode == "redirect":
            self.send_header("Location", "https://elsewhere.invalid/never-follow")
        if mode != "unbounded":
            self.send_header("Content-Length", str(len(body)))
        else:
            body = b"x" * 1025
        self.end_headers()
        try:
            if mode == "trickle":
                for character in body:
                    time.sleep(0.03)
                    self.wfile.write(bytes((character,)))
            else:
                self.wfile.write(body)
        except OSError:
            pass


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        cls.upstream.daemon_threads = True
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.upstream_thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.audit = VisitAudit(Path(self.temporary.name) / "data")
        self.web_root = Path(self.temporary.name) / "web"
        self.web_root.mkdir()
        (self.web_root / "visit-log.html").write_text("<html>dashboard</html>", encoding="utf-8")
        (self.web_root / "visit-log.mjs").write_text("// public dashboard script", encoding="utf-8")
        self.upstream.mode = "normal"
        self.upstream.requests = []
        self.server = create_server("127.0.0.1", 0, audit=self.audit,
                                    upstream=f"http://127.0.0.1:{self.upstream.server_port}",
                                    web_root=self.web_root, password="test-password", max_body=1024)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertTrue(self.server.wait_requests())
        self.thread.join()
        self.audit.close()
        self.temporary.cleanup()

    def request(self, method="GET", path="/", *, headers=None, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        merged = {"Host": "upstage.makertion.com", **(headers or {})}
        try:
            connection.request(method, path, body=body, headers=merged)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def auth(self, password="test-password"):
        return {"Authorization": "Basic " + base64.b64encode(("admin:" + password).encode()).decode()}

    def records(self):
        self.assertTrue(self.audit.flush())
        return self.audit.page()["items"]

    def test_proxy_preserves_document_status_body_headers_host_and_query(self):
        status, headers, body = self.request(path="/?lang=en&private-query=private-token", headers={
            "Origin": "https://upstage.makertion.com", "User-Agent": "Example Browser",
            "Connection": "close, X-Hop-Request", "X-Hop-Request": "removed",
            "CF-Connecting-IP": "198.51.100.10", "X-Forwarded-Proto": "https",
            "Authorization": "Basic private-auth", "Cookie": "private-cookie",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body, b"original document bytes\x00\xff")
        self.assertEqual(headers["Content-Security-Policy"], "default-src 'none'; script-src 'self'")
        self.assertEqual(headers["X-Original"], "retained")
        self.assertNotIn("X-Hop-Response", headers)
        self.assertEqual(int(headers["Content-Length"]), len(body))
        method, target, received = self.upstream.requests[0]
        self.assertEqual(target, "/?lang=en&private-query=private-token")
        self.assertEqual(received["Host"], "upstage.makertion.com")
        self.assertEqual(received["Origin"], "https://upstage.makertion.com")
        self.assertEqual(received["X-Forwarded-Proto"], "https")
        self.assertNotIn("X-Hop-Request", received)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["path"], "/")
        self.assertEqual(records[0]["reported_client_ip"], "198.51.100.10")
        self.assertNotIn("private-", json.dumps(records))

    def test_all_and_only_document_paths_are_proxied(self):
        for path in DOCUMENT_PATHS:
            self.assertEqual(self.request(path=path)[0], 200, path)
        for path in ("/ask", "/ui/ask", "/ui/jobs", "/jobs/1", "/kakao/skill", "/anything", "/%2e%2e/ask"):
            self.assertEqual(self.request(path=path)[0], 404, path)
        self.assertEqual(len(self.upstream.requests), len(DOCUMENT_PATHS))
        self.assertEqual(len(self.records()), len(DOCUMENT_PATHS))
        for path in ("/", "/ask", "/ui/ask", "/kakao/skill", "/admin/visits/data"):
            status, headers, _ = self.request("POST", path, body=b'{"question":"never run"}')
            self.assertEqual(status, 405)
            self.assertEqual(headers["Allow"], "GET")
        self.assertEqual(len(self.upstream.requests), len(DOCUMENT_PATHS))

    def test_auth_protects_both_html_aliases_and_data_only(self):
        for path in ("/admin/visits", "/admin/visits/", "/admin/visits/data"):
            for headers in ({}, self.auth("wrong"), {"Authorization": "Basic malformed=="}):
                status, response_headers, _ = self.request(path=path, headers=headers)
                self.assertEqual(status, 401)
                self.assertEqual(response_headers["WWW-Authenticate"], 'Basic realm="Request log", charset="UTF-8"')
            self.assertEqual(self.request(path=path, headers=self.auth())[0], 200)
        status, headers, _ = self.request(path="/visit-log.mjs")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.records(), [])
        self.server.password = None
        self.assertEqual(self.request(path="/admin/visits", headers=self.auth())[0], 503)

    def test_data_contract_and_query_validation(self):
        self.request()
        self.records()
        status, _, body = self.request(path="/admin/visits/data?period=today", headers=self.auth())
        self.assertEqual(status, 200)
        page = json.loads(body)
        self.assertEqual(set(page), {"items", "total", "next_before", "summary", "retention", "capturing_since"})
        self.assertEqual(page["retention"], {"days": 30, "max_records": 10000})
        self.assertEqual(page["total"], 1)
        for query in ("before=0", "before=-1", "before=9223372036854775808", "before=", "before=1&before=2",
                      "period=week", "period=", "period=all&period=all", "unknown=x", "before=1.2"):
            self.assertEqual(self.request(path="/admin/visits/data?" + query, headers=self.auth())[0], 400, query)

    def test_redirect_not_followed_and_upstream_404_preserved(self):
        self.upstream.mode = "redirect"
        status, headers, _ = self.request()
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://elsewhere.invalid/never-follow")
        self.assertEqual(len(self.upstream.requests), 1)
        self.upstream.mode = "404"
        self.assertEqual(self.request()[0], 404)
        self.assertEqual({item["http_status"] for item in self.records()}, {302, 404})

    def test_timeout_and_size_limits_are_logged_without_model_requests(self):
        for mode, expected_status, expected_error in (("large", 502, "upstream_response_too_large"),
                                                      ("unbounded", 502, "upstream_response_too_large"),
                                                      ("timeout", 504, "upstream_timeout")):
            self.upstream.mode = mode
            self.server.proxy_timeout = 0.05
            status, _, body = self.request()
            self.assertEqual(status, expected_status, mode)
            self.assertEqual(json.loads(body)["error"], expected_error)
        self.assertEqual(len(self.records()), 3)
        self.assertTrue(all(request[0:2] == ("GET", "/") for request in self.upstream.requests))

    def test_logging_failure_does_not_change_successful_document(self):
        with patch.object(self.audit, "_write", side_effect=OSError("private internal details")):
            self.assertEqual(self.request()[0], 200)
            self.assertTrue(self.audit.flush())
            status, _, body = self.request(path="/health")
            health = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(health["status"], "degraded")
            self.assertNotIn("private", body.decode())
        with patch.object(self.audit, "record", side_effect=RuntimeError("logging broken")):
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(self.audit.health()["last_error"], "RuntimeError")
        with patch.object(self.audit, "page", side_effect=OSError("private internal details")):
            status, _, body = self.request(path="/admin/visits/data", headers=self.auth())
            self.assertEqual(status, 503)
            self.assertNotIn("private", body.decode())

    def test_fixed_upstream_and_origin_checks(self):
        for target in ("https://elsewhere.invalid/", "//elsewhere.invalid/", "/#secret"):
            self.assertEqual(self.request(path=target)[0], 400)
        self.assertEqual(self.request(headers={"Origin": "https://elsewhere.invalid"})[0], 403)
        self.assertEqual(self.upstream.requests, [])
        for url in ("file:///etc/passwd", "http://user:password@localhost", "http://localhost/path", "http://localhost?secret=1"):
            with self.assertRaises(ValueError):
                parse_upstream(url)

    def test_connection_close_trickle_obeys_total_body_deadline(self):
        self.upstream.mode = "trickle"
        self.server.proxy_timeout = 0.08
        started = time.monotonic()
        status, _, body = self.request()
        self.assertEqual(status, 504)
        self.assertEqual(json.loads(body)["error"], "upstream_timeout")
        self.assertLess(time.monotonic() - started, 0.3)

    def test_thread_saturation_rejects_then_recovers(self):
        # Hold one accepted socket before its request line, occupying the sole slot.
        self.server._capacity = threading.BoundedSemaphore(1)
        stalled = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        try:
            with self.server._requests:
                self.assertTrue(self.server._requests.wait_for(lambda: self.server._active == 1, timeout=1))
            status, headers, body = self.request()
            self.assertEqual(status, 503)
            self.assertEqual(headers["Retry-After"], "1")
            self.assertEqual(body, b"")
            self.assertEqual(self.upstream.requests, [])
        finally:
            stalled.close()
        self.assertTrue(self.server.wait_requests(timeout=2))
        self.assertEqual(self.request()[0], 200)


if __name__ == "__main__":
    unittest.main()
