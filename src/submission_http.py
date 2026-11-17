"""Python urllib transport for submission-format requests and the public-site check."""
import concurrent.futures
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_SECONDS = 90
MAX_RESPONSE_BYTES = 128 * 1024
STATUS_HEADER = "X-Rag-Status"


def submission_request(base_url, question, *, host_header=None):
    body = json.dumps({"question": question}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if host_header:
        headers["Host"] = host_header
    return urllib.request.Request(base_url.rstrip("/") + "/ask", data=body, headers=headers, method="POST")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SubmissionCheck:
    def __init__(self, base_url):
        self.base_url = None
        if base_url:
            try:
                url = urllib.parse.urlsplit(base_url)
                url.port  # Validate the optional port before accepting the origin.
            except ValueError:
                url = None
            if (url and url.scheme in ("http", "https") and url.hostname and url.username is None
                    and url.password is None and url.path in ("", "/") and not url.query and not url.fragment):
                self.base_url = base_url.rstrip("/")
        self.slots = threading.BoundedSemaphore(2)

    def run(self, question):
        if not self.base_url:
            return 503, {"error": "submission_check_not_configured", "elapsed_ms": 0}
        if not self.slots.acquire(blocking=False):
            return 429, {"error": "submission_check_busy", "elapsed_ms": 0}
        started = time.monotonic()
        result = concurrent.futures.Future()

        def worker():
            try:
                result.set_result(self._fetch(question, started))
            except Exception:
                result.set_result((502, {"error": "upstream_unreachable"}))
            finally:
                self.slots.release()

        # urllib timeouts apply per socket operation. Bound the caller's entire wait,
        # including slow DNS or a peer that keeps trickling bytes; retain its slot
        # until the actual outbound operation has stopped.
        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError:
            self.slots.release()
            return 503, {"error": "submission_check_busy", "elapsed_ms": 0}
        try:
            status, details = result.result(timeout=max(0, TIMEOUT_SECONDS - (time.monotonic() - started)))
        except TimeoutError:
            status, details = 504, {"error": "upstream_timeout"}
        return status, {**details, "elapsed_ms": round((time.monotonic() - started) * 1000)}

    def _fetch(self, question, started):
        request = submission_request(self.base_url, question)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            try:
                response = opener.open(request, timeout=TIMEOUT_SECONDS)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                body = bytearray()
                while len(body) <= MAX_RESPONSE_BYTES:
                    if time.monotonic() - started >= TIMEOUT_SECONDS:
                        return 504, {"error": "upstream_timeout"}
                    chunk = response.read1(min(16 * 1024, MAX_RESPONSE_BYTES + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    return 502, {"error": "upstream_body_too_large"}
                return 200, {"http_status": response.status, "content_type": response.headers.get("Content-Type", ""),
                             "rag_status": response.headers.get(STATUS_HEADER), "body": body.decode("utf-8")}
        except TimeoutError:
            return 504, {"error": "upstream_timeout"}
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                return 504, {"error": "upstream_timeout"}
            return 502, {"error": "upstream_unreachable"}
        except UnicodeDecodeError:
            return 502, {"error": "upstream_invalid_utf8"}
        except (http.client.HTTPException, OSError):
            return 502, {"error": "upstream_unreachable"}
