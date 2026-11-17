"""Local HTTP service for unapproved local RAG drafts (loopback by default).

POST /ask without `wait_seconds` is the grader mode (starter-kit app.py shape):
wait up to 80 seconds, then always answer HTTP 200 with exactly the four keys
`answer`, `citation`, `source_url`, `effective_date`. Every outcome other than a
completed answer (timeout, failure, interruption, processing error, full queue)
is {"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""}.
The outcome stays visible to operators in the `X-Rag-Status` header (and
`X-Rag-Job-Id` when a job exists), in GET /jobs/<id> and in the saved job record.
Invalid /ask requests keep their 4xx code with the same four-key body.

POST /ui/ask without `wait_seconds` is the demo-page mode: the same wait, HTTP 200
and the detailed body (`status`, `job_id`, `poll_url`, `sources`, `result`, ...).
A body with `wait_seconds` (0..75) on either path is the asynchronous mode: HTTP
202 reports a pending job, not a completed legal answer. Solar work may continue
longer in its worker. `--host` other than loopback is for the grading
environment only. GET / serves the citizen demo page (src/web/index.html), which
uses POST /ui/ask. GET /test serves the submission checker, which calls POST /ask.
Other GET paths serve allow-listed static files under src/web/.

Timing: the grader allows 90 seconds per question. A synchronous request waits
at most SYNC_WAIT_SECONDS (80) measured from the start of the request handler,
read-only pre-checks included, whatever its queue position; the 10 seconds left
cover connection, body and response transfer. Queue depth (by default 4 running +
12 queued; --workers/--queue-size or LOCAL_RAG_WORKERS/LOCAL_RAG_QUEUE_SIZE)
only decides whether a queued question is answered or reported as
`deadline_exceeded` within that bound; a full queue answers at once (429 for
asynchronous callers, HTTP 200 "확인 불가" with `X-Rag-Status: queue_full` on /ask).

Concurrency: different questions run in different workers at the same time; the
same question is one job. The default runtime gives each question's client a
per-request-body lock (different bodies are sent at the same time), a
CLIENT_DEADLINE_SECONDS deadline for the HTTP 429 resend and one SendGate shared
by all workers, so a 429 pauses new sends of every worker until its resend time.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib
import json
import math
import os
from pathlib import Path
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import uuid

from submission_http import SubmissionCheck

SUBMISSION_CHECK_PATH = "/test/submission"
JOB_AREA = "data/processed/local_rag/2026-10-01/service_jobs"
JOB_ID = re.compile(r"[0-9a-f]{64}\Z")
MAX_BODY = 32768
MAX_QUESTION = 16000
ASYNC_WAIT_LIMIT = 75
SYNC_WAIT_SECONDS = 80
UNKNOWN = "확인 불가"
ASK_PATHS = ("/ask", "/ui/ask")
# X-Rag-Status values for a grader-mode /ask answer that has a job.
KIT_STATUSES = ("completed", "deadline_exceeded", "failed", "interrupted", "processing_error")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# A failed or interrupted job is queued again when the same question is
# submitted again, at most this many times per job (persisted in the record).
MAX_REQUEUES = 1
REQUEUE_STATUSES = ("failed", "interrupted")
# Workers run different questions at the same time; queued questions wait for a
# free worker within the synchronous bound. Jobs and create_server check the ranges.
DEFAULT_WORKERS = 4
DEFAULT_QUEUE_SIZE = 12
MAX_WORKERS = 16
MAX_QUEUE_SIZE = 64
# Default runtime only: the client's HTTP 429 resend deadline, measured from the
# client's creation in run_question (the synchronous wait is 80 s).
CLIENT_DEADLINE_SECONDS = 75
# What a new (uncached) answer needs from this process. Mirrors where
# local_rag_client.Client reads the key; the key value is never kept or reported.
API_KEY_NAME = "UPSTAGE_API_KEY"
HTTP_DEPENDENCIES = ("requests", "dotenv")
# Citizen demo page and its static files: the web root next to this module. "/" and
# "/index.html" serve WEB_INDEX; other GET paths serve a static file only when the
# path matches WEB_STATIC_PATH, its extension is in WEB_TYPES and the resolved file is
# a regular file inside WEB_ROOT whose relative path equals the request exactly.
# API paths are matched first. The page only calls POST /ui/ask and GET /jobs/<id>.
WEB_ROOT = Path(__file__).resolve().parent / "web"
WEB_INDEX = WEB_ROOT / "index.html"
WEB_PATHS = ("/", "/index.html")
# Each segment starts with a letter or digit: no "..", hidden files, empty segments,
# backslashes, percent-encoding or query strings.
WEB_STATIC_PATH = re.compile(r"/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
WEB_STATIC_PATH_MAX = 200
WEB_STATIC_MAX_BYTES = 10 * 1024 * 1024
WEB_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2",
}
# Same-origin files only; every external origin stays blocked.
WEB_SECURITY_POLICY = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                       "img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; "
                       "form-action 'none'; frame-ancestors 'none'")


def static_file(path):
    """(bytes, content type) of an allow-listed file under WEB_ROOT for a request path, else None."""
    if len(path) > WEB_STATIC_PATH_MAX or not WEB_STATIC_PATH.fullmatch(path):
        return None
    relative = path[1:]
    content_type = WEB_TYPES.get(Path(relative).suffix)
    if content_type is None:
        return None
    try:
        root = WEB_ROOT.resolve(strict=True)
        target = (root / relative).resolve(strict=True)
        # Exact relative path (same case, no symlink leaving or renaming inside the root).
        if not target.is_relative_to(root) or target.relative_to(root).as_posix() != relative:
            return None
        if not target.is_file() or target.stat().st_size > WEB_STATIC_MAX_BYTES:
            return None
        with target.open("rb") as stream:
            body = stream.read(WEB_STATIC_MAX_BYTES + 1)
    except (OSError, ValueError, RuntimeError):
        return None
    if len(body) > WEB_STATIC_MAX_BYTES:
        return None
    return body, content_type


def service_readiness(project):
    """Return reason codes (empty when ready). Never returns, logs or stores the key."""
    reasons = []
    for name in HTTP_DEPENDENCIES:
        try:
            importlib.import_module(name)
        except Exception:
            reasons.append("missing_dependency_" + name)
    present = False
    key = None
    try:
        # Same order as Client._api_key: environment first, then <project>/.env.
        key = os.getenv(API_KEY_NAME)
        if not key and "missing_dependency_dotenv" not in reasons:
            from dotenv import dotenv_values
            key = dotenv_values(Path(project) / ".env").get(API_KEY_NAME)
        present = isinstance(key, str) and bool(key.strip())
    except Exception:
        present = False
    finally:
        key = None
    if not present:
        reasons.append("missing_upstage_api_key")
    return reasons


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write((json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


class Busy(RuntimeError):
    pass


class PreflightError(RuntimeError):
    pass


class Jobs:
    def __init__(self, project, *, runtime=None, publicizer=None, health_provider=None,
                 queue_size=DEFAULT_QUEUE_SIZE, readiness_provider=None, workers=DEFAULT_WORKERS):
        if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
            raise ValueError("invalid_workers")
        if type(queue_size) is not int or not 0 <= queue_size <= MAX_QUEUE_SIZE:
            raise ValueError("invalid_queue_size")
        self.project = Path(project).resolve(strict=True)
        self.folder = (self.project / JOB_AREA).resolve()
        if not self.folder.is_relative_to(self.project):
            raise ValueError("jobs_outside_project")
        # Shared by every worker's client (default runtime only).
        self.gate = None
        if runtime is None or health_provider is None:
            from ask_local_rag import run_question, database_identity, DEFAULT_DATABASE
            if runtime is None:
                from local_rag_client import SendGate
                self.gate = SendGate()
                runtime = self._default_runtime(run_question, self.gate)
            health_provider = health_provider or (lambda: database_identity(self.project / DEFAULT_DATABASE))
        if publicizer is None:
            from public_rag_response import public_result
            publicizer = public_result
        self.runtime, self.publicizer = runtime, publicizer
        self.health_provider = health_provider
        self.readiness_provider = readiness_provider or (lambda: service_readiness(self.project))
        self.lock = threading.RLock()
        self.records, self.events = {}, {}
        self.workers, self.queue_size = workers, queue_size
        self.slots = threading.BoundedSemaphore(workers + queue_size)
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="local-rag")
        self.closed = False
        for path in sorted(self.folder.glob("*.json")):
            if not JOB_ID.fullmatch(path.stem):
                continue
            record = json.loads(path.read_bytes())
            if record.get("job_id") != path.stem or record.get("run_identity") != path.stem:
                raise ValueError("invalid_saved_job_identity")
            if record.get("status") in ("queued", "running"):
                record["status"] = "interrupted"
                self._save(record)
            self.records[path.stem] = record

    @staticmethod
    def _default_runtime(run_question, gate):
        """run_question with the service's client options; the call shape is unchanged."""
        def runtime(project, question, check_only=False):
            options = {"parallel": True, "deadline_seconds": CLIENT_DEADLINE_SECONDS, "gate": gate}
            return run_question(project, question, check_only=check_only, client_options=options)
        return runtime

    def _save(self, record):
        atomic_json(self.folder / (record["job_id"] + ".json"), record)

    def _public_result(self, audit):
        result = self.publicizer(audit, self.project)
        if not isinstance(result, dict):
            raise ValueError("invalid_public_result")
        result = copy.deepcopy(result)
        result.update(answer_allowed=False, final_answer_generated=False)
        return result

    def _cached(self, question):
        audit = self.runtime(self.project, question, check_only=True)
        if not isinstance(audit, dict) or audit.get("status") == "processing_error":
            raise PreflightError("read_only_preflight_failed")
        identity = audit.get("run_identity")
        if not isinstance(identity, str) or not JOB_ID.fullmatch(identity):
            raise PreflightError("invalid_run_identity")
        complete = audit.get("cached") is True and audit.get("status") == "completed"
        return identity, self._public_result(audit) if complete else None

    def submit(self, question):
        identity, cached = self._cached(question)
        with self.lock:
            if cached is not None:
                record = {"job_id": identity, "run_identity": identity, "question": question,
                          "status": "completed", "result": cached, "cached": True}
                self.records[identity] = record
                self._save(record)
                return self.public(record)
            previous = self.records.get(identity)
            if previous is not None and not self._requeue_allowed(previous):
                return self.public(previous)
            if self.closed or not self.slots.acquire(blocking=False):
                raise Busy("queue_full")
            record = {"job_id": identity, "run_identity": identity, "question": question,
                      "status": "queued", "cached": False}
            if previous is not None:
                record["requeues"] = previous.get("requeues", 0) + 1
            event = threading.Event()
            try:
                self._save(record)
                self.records[identity], self.events[identity] = record, event
                self.executor.submit(self._work, identity, event)
            except Exception:
                if previous is not None:
                    self.records[identity] = previous
                self.slots.release()
                raise
            return self.public(record)

    @staticmethod
    def _requeue_allowed(record):
        requeues = record.get("requeues", 0)
        return (record.get("status") in REQUEUE_STATUSES and type(requeues) is int
                and 0 <= requeues < MAX_REQUEUES)

    def _work(self, identity, event):
        try:
            with self.lock:
                record = self.records[identity]
                record["status"] = "running"
                self._save(record)
                question = record["question"]
            current_identity, cached = self._cached(question)
            if current_identity != identity:
                raise PreflightError("queued_identity_changed")
            if cached is not None:
                result = cached
            else:
                audit = self.runtime(self.project, question, check_only=False)
                if not isinstance(audit, dict) or audit.get("run_identity") != identity:
                    raise PreflightError("runtime_identity_changed")
                result = self._public_result(audit)
            with self.lock:
                record.update(status="completed", result=result)
                self._save(record)
        except Exception:
            # Never persist exception strings, provider rationale or private paths.
            with self.lock:
                record = self.records[identity]
                record.update(status="failed", result={"status": "processing_error",
                    "answer_allowed": False, "final_answer_generated": False,
                    "message": "처리 중 오류가 발생했습니다. 근거가 없다는 뜻은 아닙니다."})
                try:
                    self._save(record)
                except OSError:
                    pass
        finally:
            # This run's own event: a requeued run of the same job has a new one.
            event.set()
            self.slots.release()

    @staticmethod
    def public(record):
        result = {"job_id": record["job_id"], "status": record["status"],
                  "answer_allowed": False, "final_answer_generated": False}
        if "result" in record:
            result["result"] = copy.deepcopy(record["result"])
        if record["status"] in ("queued", "running"):
            result["poll_url"] = "/jobs/" + record["job_id"]
        if record["status"] == "interrupted":
            result["message"] = "이전 실행이 중단되었습니다. 유료 요청을 자동 재개하지 않습니다."
        return result

    def get(self, identity):
        with self.lock:
            record = self.records.get(identity)
            if record is None:
                return None
            question = record["question"] if record["status"] == "interrupted" else None
        if question is not None:
            # Recover only an already completed, identity-matching pipeline cache.
            recovered_identity, cached = self._cached(question)
            if recovered_identity == identity and cached is not None:
                with self.lock:
                    record.update(status="completed", result=cached, cached=True)
                    self._save(record)
        with self.lock:
            return self.public(record)

    def wait(self, identity, seconds):
        with self.lock:
            event = self.events.get(identity)
        if event is not None and seconds > 0:
            event.wait(seconds)
        return self.get(identity)

    def readiness(self):
        try:
            reasons = [str(reason) for reason in self.readiness_provider()]
        except Exception:
            reasons = ["readiness_check_failed"]
        return reasons

    def health(self):
        """`status` is "ok" only when a new answer could run; otherwise "not_ready" with reason codes."""
        reasons = self.readiness()
        try:
            db = self.health_provider()
            if not isinstance(db, dict):
                raise ValueError("invalid_database_identity")
        except Exception:
            db = None
            reasons.append("database_unavailable")
        if reasons:
            return {"status": "not_ready", "reasons": reasons,
                    "answer_allowed": False, "final_answer_generated": False}
        return {"status": "ok", "as_of_date": db.get("as_of_date"),
                "database": {key: db.get(key) for key in
                    ("identity", "database_sha256", "manifest_sha256", "dimension", "model", "query_model", "summary")},
                "answer_allowed": False, "final_answer_generated": False,
                "execution": "asynchronous_local", "http_wait_limit_seconds": ASYNC_WAIT_LIMIT,
                "sync_wait_seconds": SYNC_WAIT_SECONDS, "workers": self.workers, "queue_size": self.queue_size}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        # Let submitted work finish. Never terminate an uncertain provider request.
        self.executor.shutdown(wait=True, cancel_futures=False)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_field")
        result[key] = value
    return result


def sync_view(job):
    """Flatten a public job for synchronous callers that read top-level answer/citation."""
    view = {"answer": UNKNOWN, "citation": []}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    response = result.get("response")
    status = job["status"]
    if status == "completed" and result.get("status") == "processing_error":
        status = "processing_error"
    if status == "completed" and isinstance(response, dict) and isinstance(response.get("answer"), str):
        view["answer"] = response["answer"]
        view["citation"] = copy.deepcopy(response.get("citation", []))
        for field in ("source_url", "effective_date"):
            # The kit reads scalar metadata for the first citation. The full
            # per-citation values remain available in sources and result.
            value = response.get(field)
            if isinstance(value, list):
                value = value[0] if value else None
            if isinstance(value, str):
                view[field] = value
    elif status in ("queued", "running"):
        status = "deadline_exceeded"
    view.update(status=status, job_id=job["job_id"])
    for field in ("sources", "as_of_date"):
        if field in result:
            view[field] = copy.deepcopy(result[field])
    for field in ("poll_url", "message"):
        if field in job:
            view[field] = job[field]
    if "result" in job:
        view["result"] = copy.deepcopy(job["result"])
    view.update(answer_allowed=False, final_answer_generated=False)
    return view


def _kit_response(job):
    """Public response of a completed job that fits the kit body, else None."""
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    response = result.get("response")
    citation = response.get("citation", []) if isinstance(response, dict) else None
    if (job.get("status") == "completed" and result.get("status") != "processing_error"
            and isinstance(response, dict) and isinstance(response.get("answer"), str)
            and isinstance(citation, list) and all(isinstance(c, str) for c in citation)):
        return response
    return None


def kit_status(job):
    """Outcome code for the X-Rag-Status header of a grader-mode /ask answer."""
    status = job.get("status")
    if status == "completed" and _kit_response(job) is None:
        return "processing_error"
    if status in ("queued", "running"):
        return "deadline_exceeded"
    return status if status in KIT_STATUSES else "processing_error"


def kit_view(job):
    """Starter-kit body for a grader-mode /ask: exactly answer/citation/source_url/effective_date.

    Only a completed job whose public response carries a string answer and a
    list-of-strings citation is published; every other state is the kit's
    "확인 불가" with empty metadata.
    """
    view = {"answer": UNKNOWN, "citation": [], "source_url": "", "effective_date": ""}
    if not isinstance(job, dict):
        return view
    response = _kit_response(job)
    if response is not None:
        view["answer"] = response["answer"]
        view["citation"] = copy.deepcopy(response.get("citation", []))
        for field in ("source_url", "effective_date"):
            # The kit reads scalar metadata for the first citation (rag.py).
            value = response.get(field)
            if isinstance(value, list):
                value = value[0] if value else None
            view[field] = value if isinstance(value, str) else ""
    return view


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # True while a POST /ask is (or may still be) a grader-mode request: its
    # rejections then use the four-key kit body. Cleared once `wait_seconds` is seen.
    _kit = False

    def log_message(self, *_args):
        pass

    def _send(self, status, value, headers=None):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if status == 202:
            self.send_header("Retry-After", "2")
        for name, text in (headers or {}).items():
            self.send_header(name, text)
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _send_page(self):
        try:
            body = WEB_INDEX.read_bytes()
        except FileNotFoundError:
            return self._error(404, "not_found")
        self._send_file(body, "text/html; charset=utf-8")

    def _send_file(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", WEB_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _error(self, status, code):
        if self.path == SUBMISSION_CHECK_PATH and self.command == "POST":
            return self._send(status, {"error": code, "elapsed_ms": 0})
        if self._kit:
            # Grader-mode /ask: the kit body only; the reason stays in the header.
            return self._send(status, kit_view(None), {"X-Rag-Status": code})
        value = {"status": code, "answer_allowed": False, "final_answer_generated": False}
        if self.command == "POST":
            # Kit callers read answer/citation from every /ask body; `status` keeps the
            # rejection distinguishable from a legal "확인 불가" (same as sync_view).
            value.update(answer=UNKNOWN, citation=[])
        self._send(status, value)

    def _local(self):
        port = str(self.server.server_port)
        hosts = self.headers.get_all("Host", [])
        origin = self.headers.get("Origin")
        bound = getattr(self.server, "bound_host", "127.0.0.1")
        if bound in LOOPBACK:
            allowed = {"127.0.0.1:" + port, "localhost:" + port}
            return len(hosts) == 1 and hosts[0] in allowed and (origin is None or origin in {"http://" + h for h in allowed})
        # Grading environment only: the socket is already reachable from outside, so the
        # Host name and port are not checked (tunnels and proxies rewrite them). A browser
        # Origin must still match the Host it sent.
        if len(hosts) != 1 or not hosts[0].strip():
            return False
        return origin is None or origin in ("http://" + hosts[0], "https://" + hosts[0])

    @staticmethod
    def _job_status(job):
        if job["status"] == "interrupted":
            return 409
        if job["status"] == "failed" or job.get("result", {}).get("status") == "processing_error":
            return 500
        return 202 if job["status"] in ("queued", "running") else 200

    def do_GET(self):
        if not self._local():
            return self._error(403, "local_origin_required")
        try:
            if self.path in WEB_PATHS:
                return self._send_page()
            if self.path == "/health":
                health = self.server.jobs.health()
                return self._send(200 if health.get("status") == "ok" else 503, health)
            match = re.fullmatch(r"/jobs/([0-9a-f]{64})", self.path)
            if match:
                job = self.server.jobs.get(match[1])
                return self._send(self._job_status(job), job) if job else self._error(404, "job_not_found")
            static_path = "/test.html" if self.path in ("/test", "/test/") else self.path
            static = static_file(static_path)
            if static is not None:
                return self._send_file(*static)
            self._error(404, "not_found")
        except Exception:
            self._error(503, "processing_error")

    def do_POST(self):
        started = time.monotonic()
        self._kit = self.path == "/ask"
        if not self._local():
            return self._error(403, "local_origin_required")
        if self.path not in ASK_PATHS and self.path != SUBMISSION_CHECK_PATH:
            return self._error(404, "not_found")
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None:
            return self._error(400, "chunked_body_unsupported")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            return self._error(411, "content_length_required")
        if len(lengths[0]) > 10:
            return self._error(413, "request_too_large")
        length = int(lengths[0])
        if length > MAX_BODY:
            return self._error(413, "request_too_large")
        if self.headers.get_content_type() != "application/json":
            return self._error(415, "application_json_required")
        # Set once the body is parsed: server-side rejections of a synchronous request
        # answer HTTP 200 (four-key kit body on /ask, detailed body on /ui/ask);
        # asynchronous callers keep 429/503/500. Invalid requests keep their 4xx codes.
        sync = submitted = False
        try:
            self.connection.settimeout(5)
            content = self.rfile.read(length)
            if len(content) != length:
                return self._error(400, "incomplete_body")
            request = json.loads(content, object_pairs_hook=unique_object)
            # Unknown fields are ignored, as in the starter kit's request model.
            if not isinstance(request, dict):
                return self._error(400, "invalid_request_fields")
            sync = "wait_seconds" not in request
            self._kit = self._kit and sync
            question, wait = request.get("question"), request.get("wait_seconds", 0)
            if not isinstance(question, str) or not question.strip() or len(question.encode("utf-8")) > MAX_QUESTION:
                return self._error(400, "invalid_question")
            if self.path == SUBMISSION_CHECK_PATH:
                if set(request) != {"question"}:
                    return self._error(400, "invalid_request_fields")
                return self._send(*self.server.submission_check.run(question))
            if type(wait) not in (int, float) or not 0 <= wait <= ASYNC_WAIT_LIMIT or not math.isfinite(wait):
                return self._error(400, "invalid_wait_seconds")
            if sync:
                wait = SYNC_WAIT_SECONDS
            submitted = True
            job = self.server.jobs.submit(question.strip())
            if job["status"] in ("queued", "running"):
                limit = max(ASYNC_WAIT_LIMIT, SYNC_WAIT_SECONDS)
                remaining = max(0, min(wait, limit - (time.monotonic() - started)))
                job = self.server.jobs.wait(job["job_id"], remaining)
            if self._kit:
                # Grader cannot read 202/5xx bodies: always 200 with the kit's four keys.
                # The header keeps errors distinguishable from a legal "확인 불가".
                return self._send(200, kit_view(job), {"X-Rag-Status": kit_status(job),
                                                       "X-Rag-Job-Id": job["job_id"]})
            if sync:
                return self._send(200, sync_view(job))
            self._send(self._job_status(job), job)
        except Busy:
            self._error(200 if sync else 429, "queue_full")
        except PreflightError:
            self._error(200 if sync else 503, "processing_error")
        except (ValueError, UnicodeError, TimeoutError):
            if self._kit and submitted:
                # A server-side ValueError after validation is not a client error.
                return self._error(200, "processing_error")
            self._error(400, "invalid_json_request")
        except Exception:
            self._error(200 if sync else 500, "processing_error")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Pending connections the OS keeps while handler threads start (default 5).
    request_queue_size = 64

    def server_close(self):
        super().server_close()
        if hasattr(self, "jobs"):
            self.jobs.close()


def create_server(project, port=8000, *, runtime=None, publicizer=None, health_provider=None,
                  queue_size=DEFAULT_QUEUE_SIZE, host="127.0.0.1", readiness_provider=None, workers=DEFAULT_WORKERS):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid_local_port")
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError("invalid_workers")
    if type(queue_size) is not int or not 0 <= queue_size <= MAX_QUEUE_SIZE:
        raise ValueError("invalid_queue_size")
    jobs = Jobs(project, runtime=runtime, publicizer=publicizer, health_provider=health_provider, queue_size=queue_size,
                readiness_provider=readiness_provider, workers=workers)
    try:
        submission_check = SubmissionCheck(os.environ.get("SUBMISSION_CHECK_BASE_URL"))
        server = Server((host, port), Handler)
        server.submission_check = submission_check
        server.jobs, server.bound_host = jobs, host
        return server
    except Exception:
        jobs.close()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1", help="non-loopback binding is for the grading environment only")
    parser.add_argument("--workers", type=int,
                        help="questions run at the same time, 1-%d (default: LOCAL_RAG_WORKERS or %d)"
                        % (MAX_WORKERS, DEFAULT_WORKERS))
    parser.add_argument("--queue-size", type=int,
                        help="questions waiting for a worker, 0-%d (default: LOCAL_RAG_QUEUE_SIZE or %d)"
                        % (MAX_QUEUE_SIZE, DEFAULT_QUEUE_SIZE))
    args = parser.parse_args(argv)
    settings = {}
    for name, value, variable, default, low, high in (
            ("workers", args.workers, "LOCAL_RAG_WORKERS", DEFAULT_WORKERS, 1, MAX_WORKERS),
            ("queue_size", args.queue_size, "LOCAL_RAG_QUEUE_SIZE", DEFAULT_QUEUE_SIZE, 0, MAX_QUEUE_SIZE)):
        if value is None:
            text = os.environ.get(variable, "").strip()
            try:
                value = int(text) if text else default
            except ValueError:
                parser.error("invalid " + variable)
        if not low <= value <= high:
            parser.error("invalid %s: %d (allowed %d-%d)" % (name, value, low, high))
        settings[name] = value
    server = create_server(args.project, args.port, host=args.host, **settings)
    print("Local RAG: http://" + args.host + ":" + str(server.server_port)
          + " (workers %d, queue %d;" % (settings["workers"], settings["queue_size"])
          + " POST /ask: grader four-key body; POST /ui/ask: demo detail; async with wait_seconds)", flush=True)
    health = server.jobs.health()
    if health.get("status") != "ok":
        # Reason codes only (never the key). Cached answers are still served.
        print("Local RAG not ready for new answers: " + ",".join(health.get("reasons", [])), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
