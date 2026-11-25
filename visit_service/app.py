"""Independent visit dashboard and fixed-upstream document proxy.

DOCUMENT_PATHS: /, /index.html, /legacy.html, /test, /test/, /test.html
Only these GET documents go through this service. API, job, Kakao, and other
asset routes must continue going directly to the existing RAG service.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .audit import DOCUMENT_PATHS, VisitAudit, utc_now


DEFAULT_UPSTREAM = "http://upstage-bootcamp.default.svc.cluster.local"
WEB_ROOT = Path(__file__).resolve().parent / "web"
MAX_BODY = 2 * 1024 * 1024
PROXY_TIMEOUT = 10
HOP_BY_HOP = frozenset(("connection", "keep-alive", "proxy-authenticate",
                        "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"))
SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def parse_upstream(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("UPSTREAM_URL must contain only an http(s) origin")
    return parsed.scheme, parsed.hostname, parsed.port


def end_to_end_headers(headers):
    blocked = set(HOP_BY_HOP)
    for name, value in headers:
        if name.lower() == "connection":
            blocked.update(token.strip().lower() for token in value.split(","))
    return [(name, value) for name, value in headers
            if name.lower() not in blocked and name.lower() != "content-length"]


class UpstreamFailure(Exception):
    def __init__(self, code, status=502):
        self.code = code
        self.status = status


def fetch_document(upstream, target, headers, *, timeout=PROXY_TIMEOUT, max_body=MAX_BODY):
    """The request target never supplies a hostname; redirects are returned as-is."""
    scheme, host, port = upstream
    connection_class = HTTPSConnection if scheme == "https" else HTTPConnection
    connection = connection_class(host, port, timeout=timeout)
    deadline = time.monotonic() + timeout
    try:
        connection.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
        for name, value in end_to_end_headers(headers):
            connection.putheader(name, value)
        connection.putheader("Connection", "close")
        connection.endheaders()
        # getresponse() can detach connection.sock for a Connection: close response.
        # Retain the socket so each body read still observes the remaining deadline.
        upstream_socket = connection.sock
        response = connection.getresponse()
        lengths = response.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or int(lengths[0]) > max_body)):
            raise UpstreamFailure("upstream_response_too_large")
        body = bytearray()
        while True:
            if response.isclosed():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("upstream deadline exceeded")
            if upstream_socket is not None:
                upstream_socket.settimeout(remaining)
            chunk = response.read1(min(64 * 1024, max_body + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > max_body:
                raise UpstreamFailure("upstream_response_too_large")
        if lengths and response.status not in (204, 304) and len(body) != int(lengths[0]):
            raise UpstreamFailure("upstream_incomplete_response")
        return response.status, end_to_end_headers(response.getheaders()), bytes(body)
    except UpstreamFailure:
        raise
    except TimeoutError as error:
        raise UpstreamFailure("upstream_timeout", 504) from error
    except Exception as error:
        raise UpstreamFailure("upstream_unavailable") from error
    finally:
        connection.close()


class VisitServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, handler, *, audit, upstream, web_root, password,
                 proxy_timeout=PROXY_TIMEOUT, max_body=MAX_BODY, max_active=64):
        self.audit = audit
        self.upstream = parse_upstream(upstream)
        self.web_root = Path(web_root)
        self.password = password
        self.proxy_timeout = proxy_timeout
        self.max_body = max_body
        self._capacity = threading.BoundedSemaphore(max_active)
        self._active = 0
        self._requests = threading.Condition()
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._capacity.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nCache-Control: no-store\r\n"
                                b"Retry-After: 1\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        with self._requests:
            self._active += 1
            self._requests.notify_all()
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._requests:
                self._capacity.release()
                self._active -= 1
                self._requests.notify_all()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._requests:
                self._capacity.release()
                self._active -= 1
                self._requests.notify_all()

    def wait_requests(self, timeout=12):
        with self._requests:
            return self._requests.wait_for(lambda: self._active == 0, timeout=timeout)

    def handle_error(self, request, client_address):
        # Never dump a request, headers, or authentication material to stderr.
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VisitService"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def log_message(self, format, *args):
        pass

    def _send(self, status, body, headers=()):
        self.send_response_only(status)
        for name, value in headers:
            self.send_header(name, value)
        if status not in (204, 304):
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD" and status not in (204, 304):
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    def _json(self, status, value, headers=()):
        return self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), (
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), *headers))

    def _static(self, filename, content_type):
        try:
            body = (self.server.web_root / filename).read_bytes()
        except OSError:
            return self._json(503, {"error": "dashboard_unavailable"})
        return self._send(200, body, (("Content-Type", content_type), ("Cache-Control", "no-store"),
                                     ("X-Content-Type-Options", "nosniff"),
                                     ("Content-Security-Policy", SECURITY_POLICY),
                                     ("Referrer-Policy", "no-referrer")))

    def _authenticate(self):
        password = self.server.password
        if not password:
            self._json(503, {"error": "visit_log_not_configured"})
            return False
        supplied = b""
        values = self.headers.get_all("Authorization", [])
        if len(values) == 1:
            kind, _, token = values[0].partition(" ")
            if kind.lower() == "basic":
                try:
                    supplied = base64.b64decode(token, validate=True)
                except (ValueError, binascii.Error):
                    pass
        if not hmac.compare_digest(supplied, b"admin:" + password.encode("utf-8")):
            self._json(401, {"error": "authentication_required"}, (
                ("WWW-Authenticate", 'Basic realm="Request log", charset="UTF-8"'),))
            return False
        return True

    def _admin(self, target):
        if not self._authenticate():
            return
        if target.path in ("/admin/visits", "/admin/visits/"):
            return self._static("visit-log.html", "text/html; charset=utf-8")
        try:
            query = parse_qs(target.query, keep_blank_values=True, max_num_fields=4)
            before = query.get("before", [None])[0]
            period = query.get("period", ["all"])[0]
            if (set(query) - {"before", "period"} or any(len(values) != 1 for values in query.values())
                    or period not in ("all", "today", "7d")
                    or (before is not None and (not re.fullmatch(r"[1-9][0-9]{0,18}", before)
                                                or int(before) > 9223372036854775807))):
                raise ValueError("invalid query")
        except ValueError:
            return self._json(400, {"error": "invalid_query"})
        try:
            page = self.server.audit.page(before=int(before) if before else None, period=period)
        except Exception:
            return self._json(503, {"error": "visit_log_unavailable"})
        return self._json(200, page)

    def do_GET(self):
        received_at = utc_now()
        started = time.monotonic()
        raw_target = self.requestline.split()[1]
        if (len(self.path) > 8192 or not self.path.startswith("/")
                or raw_target.startswith("//")):
            return self._json(400, {"error": "invalid_target"})
        try:
            target = urlsplit(self.path)
        except ValueError:
            return self._json(400, {"error": "invalid_target"})
        if target.scheme or target.netloc or target.fragment:
            return self._json(400, {"error": "invalid_target"})
        if target.path == "/health":
            health = self.server.audit.health()
            return self._json(200, {"status": "ok" if health["available"] else "degraded", "logging": health})
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if (len(hosts) != 1 or not hosts[0].strip() or len(origins) > 1
                or (origins and origins[0] not in ("http://" + hosts[0], "https://" + hosts[0]))):
            return self._json(403, {"error": "same_origin_required"})
        if target.path in ("/admin/visits", "/admin/visits/", "/admin/visits/data"):
            return self._admin(target)
        if target.path == "/visit-log.mjs":
            return self._static("visit-log.mjs", "text/javascript; charset=utf-8")
        if target.path not in DOCUMENT_PATHS:
            return self._json(404, {"error": "not_found"})
        if self.headers.get("Transfer-Encoding") is not None:
            return self._json(400, {"error": "unexpected_request_body"})
        try:
            status, headers, body = fetch_document(self.server.upstream, self.path,
                                                  list(self.headers.items()),
                                                  timeout=self.server.proxy_timeout,
                                                  max_body=self.server.max_body)
        except UpstreamFailure as error:
            status = error.status
            headers = (("Content-Type", "application/json; charset=utf-8"),
                       ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"))
            body = json.dumps({"error": error.code}).encode("utf-8")
        try:
            self.server.audit.record(path=target.path, http_status=status,
                                     elapsed_ms=(time.monotonic() - started) * 1000,
                                     headers=self.headers, socket_peer=self.client_address[0],
                                     received_at=received_at)
        except Exception as error:
            # Logging must never turn a successful document into a service outage.
            self.server.audit.note_failure(error)
        return self._send(status, body, headers)

    def _method_not_allowed(self):
        return self._json(405, {"error": "method_not_allowed"}, (("Allow", "GET"),))

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = _method_not_allowed


def create_server(host="0.0.0.0", port=8080, *, audit, upstream=DEFAULT_UPSTREAM,
                  web_root=WEB_ROOT, password=None, proxy_timeout=PROXY_TIMEOUT, max_body=MAX_BODY,
                  max_active=64):
    return VisitServer((host, port), Handler, audit=audit, upstream=upstream,
                       web_root=web_root, password=password, proxy_timeout=proxy_timeout,
                       max_body=max_body, max_active=max_active)


def main():
    audit = VisitAudit(os.environ.get("VISIT_DATA_DIR", "/data"))
    server = create_server(port=int(os.environ.get("PORT", "8080")), audit=audit,
                           upstream=os.environ.get("UPSTREAM_URL", DEFAULT_UPSTREAM),
                           password=os.environ.get("REQUEST_LOG_PASSWORD"))
    stopping = threading.Event()

    def stop(signum, frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        server.wait_requests(timeout=12)
        audit.close(timeout=10)


if __name__ == "__main__":
    main()
