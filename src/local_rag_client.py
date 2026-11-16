"""Upstage requests with immutable, exact-payload audit receipts.

No SDK retries. An attempt without a conclusive HTTP receipt is never repeated.
By default every request holds one lock across kinds and processes (serial).
The service passes `parallel=True` (one lock per request body, so different
bodies are sent at the same time), `deadline_seconds` (a short HTTP 429 resend
wait that must fit the deadline) and a shared `SendGate`; none of these is part
of a request identity, descriptor, payload or receipt hash.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

QUERY_MODEL = "solar-embedding-2-query"
CHAT_MODEL = "solar-pro4"
ALLOWED_RETURNED_MODELS = {
    QUERY_MODEL: frozenset({QUERY_MODEL}),
    CHAT_MODEL: frozenset({CHAT_MODEL, "solar-pro4-260806"}),
}
ENDPOINTS = {"embedding": "https://api.upstage.ai/v1/embeddings",
             "gate": "https://api.upstage.ai/v1/chat/completions",
             "draft": "https://api.upstage.ai/v1/chat/completions",
             "verify": "https://api.upstage.ai/v1/chat/completions",
             "provision_check": "https://api.upstage.ai/v1/chat/completions"}

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 180
# A first attempt answered with one of these conclusive HTTP receipts may be
# sent once more (attempt 2) by a later call; the client never does it itself.
RESENDABLE_SERVER_STATUSES = frozenset({500, 502, 503, 504})

# Deadline mode (Client deadline_seconds) for a call without `timeout`: the HTTP
# 429 resend waits Retry-After (when it parses) or 10 s, bounded to 2..60 s, and
# is skipped (RateLimitedNoResend, nothing written) unless at least the minimum
# work time remains before the deadline after the wait (30 s for every kind:
# retrieval and the draft still follow an embedding, and
# local_rag_answer.DRAFT_RETRY_MIN_SECONDS is also 30 s). The same bounded wait
# is the hold a 429 receipt puts on a shared SendGate.
DEADLINE_RETRY_DEFAULT_SECONDS = 10.0
DEADLINE_RETRY_MIN_SECONDS = 2.0
DEADLINE_RETRY_MAX_SECONDS = 60.0
DEADLINE_MIN_WORK_SECONDS = {"embedding": 30.0}
DEADLINE_MIN_WORK_DEFAULT_SECONDS = 30.0

# (request kind, json_schema name) pairs whose schema field order is authored.
ORDERED_SCHEMAS = frozenset({("draft", "local_rag_single_generation"),
                             ("provision_check", "local_rag_provision_check")})


def require(ok, message):
    if not ok:
        raise ValueError(message)


def raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def request_wire(kind, payload):
    """Preserve the single-generation schema order without changing old caches.

    Canonical receipt serialization remains sorted. Only the operating draft
    schema and the pre-publication provision check use an order-sensitive body
    and a separate cache descriptor.
    """
    response_format = payload.get("response_format", {})
    ordered = (isinstance(response_format, dict)
               and response_format.get("type") == "json_schema"
               and (kind, response_format.get("json_schema", {}).get("name")) in ORDERED_SCHEMAS)
    if not ordered:
        return raw(payload), None
    # Receipt JSON is canonicalized, so callers replaying its payload have
    # already lost dictionary insertion order. The schema's required arrays
    # retain the explicitly authored field order across that round trip.
    ordered_payload = json.loads(raw(payload))
    def order_properties(value):
        if isinstance(value, dict):
            for child in value.values():
                order_properties(child)
            if value.get("type") == "object" and "properties" in value:
                required = value.get("required", [])
                properties = value["properties"]
                require(isinstance(required, list) and len(required) == len(set(required))
                        and set(required) == set(properties), "invalid_ordered_schema_fields")
                value["properties"] = {name: properties[name] for name in required}
        elif isinstance(value, list):
            for child in value:
                order_properties(child)
    order_properties(ordered_payload["response_format"]["json_schema"]["schema"])
    content = (json.dumps(ordered_payload, ensure_ascii=False, sort_keys=False, indent=2,
                          allow_nan=False) + "\n").encode("utf-8")
    return content, "ordered_json_v1"


def digest(content):
    return hashlib.sha256(content).hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def save_bytes(path, content):
    path = Path(path)
    if path.exists():
        require(path.read_bytes() == content, "immutable_receipt_conflict:" + path.name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def save(path, value):
    save_bytes(path, raw(value))


class CacheMiss(RuntimeError):
    pass


class RateLimitedNoResend(RuntimeError):
    """A deadline-bound request cannot wait for an HTTP 429 resend or hold.

    Raised when a request with `timeout` holds an HTTP 429 receipt whose resend
    wait has not elapsed, when a deadline-mode request's bounded resend wait
    does not fit its deadline, or when a shared SendGate hold cannot be waited
    out. A 429 is a conclusive, already saved receipt; nothing new is written,
    so the existing state rules still allow exactly one later resend.
    """
    def __init__(self, retry_wait_seconds):
        super().__init__("rate_limited_no_resend")
        self.http_status = 429
        self.retry_wait_seconds = retry_wait_seconds

    def __reduce__(self):
        return type(self), (self.retry_wait_seconds,)


class ProviderServerError(RuntimeError):
    """The first attempt of this body got a conclusive HTTP 500/502/503/504 receipt.

    Raised by the call that received it; the client does not resend by itself
    because only the caller knows its time budget. The receipt is saved, so a
    later call of the same body is attempt 2 (sent without waiting) and stays
    within the two-attempt limit. Unknown outcomes never reach this state.
    """
    def __init__(self, http_status):
        super().__init__("provider_server_error_resendable")
        self.http_status = http_status

    def __reduce__(self):
        return type(self), (self.http_status,)


class SendGate:
    """Thread-safe "no new send before this time" mark shared by service clients.

    A client that receives an HTTP 429 records its bounded resend time here;
    every client holding the same gate waits until then before any new send
    (or raises RateLimitedNoResend without writing when the wait does not fit).
    Times use the clients' clock (time.time by default).
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0

    def hold(self, until):
        with self._lock:
            if until > self._until:
                self._until = until

    def until(self):
        with self._lock:
            return self._until


class Client:
    def __init__(self, project, cache_dir=None, *, read_only=False, check_only=False,
                 env_file=None, transport=None, sleeper=None, clock=None,
                 parallel=False, deadline_seconds=None, gate=None):
        self.project = Path(project).resolve(strict=True)
        self.cache_dir = Path(cache_dir or self.project / "data/processed/local_rag/2026-10-01/request_cache").resolve()
        require(self.cache_dir.is_relative_to(self.project), "cache_outside_project")
        self.read_only = read_only or check_only
        self.env_file = Path(env_file) if env_file else self.project / ".env"
        self._transport = transport
        self._sleep = sleeper or time.sleep
        self._clock = clock or time.time
        require(type(parallel) is bool, "invalid_parallel_option")
        require(deadline_seconds is None or (type(deadline_seconds) in (int, float) and
                                             math.isfinite(deadline_seconds) and deadline_seconds > 0),
                "invalid_deadline_seconds")
        require(gate is None or isinstance(gate, SendGate), "invalid_send_gate")
        self._parallel = parallel
        # Absolute deadline on self._clock, measured from client creation.
        self._deadline = None if deadline_seconds is None else self._clock() + deadline_seconds
        self._gate = gate
        self.called = 0
        self.calls = []
        # Kinds whose HTTP 429 wait this client skipped (RateLimitedNoResend);
        # bookkeeping for the caller only, never part of an identity or receipt.
        self.rate_limit_skipped = set()
        self._key = None

    def _api_key(self):
        if self._key is None:
            from dotenv import dotenv_values
            self._key = os.getenv("UPSTAGE_API_KEY") or dotenv_values(self.env_file).get("UPSTAGE_API_KEY")
            require(isinstance(self._key, str) and bool(self._key.strip()), "missing_upstage_api_key")
        return self._key

    def _wait(self, seconds):
        while seconds > 0:
            step = min(seconds, 30.0)
            self._sleep(step)
            seconds -= step

    def _state(self, folder, identity, payload, wire, wire_format=None):
        request_path = folder / "request.json"
        if request_path.exists():
            require(digest(request_path.read_bytes()) == identity, "saved_request_descriptor_mismatch")
            if wire_format:
                body_path = folder / "request.body.json"
                require(body_path.exists() and body_path.read_bytes() == wire, "saved_request_wire_mismatch")
        for number in (1, 2):
            base = folder / f"attempt-{number}"
            attempt_path = base.with_suffix(".json")
            response_path = folder / f"attempt-{number}.response.json"
            raw_path = folder / f"attempt-{number}.response.raw"
            failure_path = folder / f"attempt-{number}.failure.json"
            if not attempt_path.exists():
                require(not any(p.exists() for p in (response_path, raw_path, failure_path)), "orphan_receipt")
                if number == 1:
                    require(not any(folder.glob("attempt-2*")), "second_attempt_without_first")
                    return {"number": number}
                return {"number": number, "first_status": first_status}
            attempt = read(attempt_path)
            require(request_path.exists(), "attempt_without_request_descriptor")
            require(attempt["request_identity"] == identity and attempt["payload_sha256"] == digest(wire), "saved_request_mismatch")
            require(response_path.exists() and raw_path.exists() and not failure_path.exists(), "uncertain_attempt_no_automatic_retry")
            receipt = read(response_path)
            require(receipt["attempt_sha256"] == digest(attempt_path.read_bytes()) and
                    receipt["raw_sha256"] == digest(raw_path.read_bytes()), "response_receipt_hash_mismatch")
            if receipt["http_status"] == 200:
                require(number != 1 or not any(folder.glob("attempt-2*")), "duplicate_success_attempt")
                body = json.loads(raw_path.read_bytes())
                require(isinstance(body, dict) and isinstance(body.get("model"), str) and
                        body["model"] in ALLOWED_RETURNED_MODELS[payload["model"]], "unexpected_response_model")
                return {"complete": True, "body": body, "number": number,
                        "response_path": str(response_path), "raw_path": str(raw_path)}
            if number == 1:
                first_status = receipt["http_status"]
                if first_status in RESENDABLE_SERVER_STATUSES:
                    continue  # attempt 2 may be sent once; a second non-200 stops below
            require(receipt["http_status"] == 429, "provider_http_error:" + str(receipt["http_status"]))
            require(number == 1, "second_rate_limit_response_stop")
        raise ValueError("unreachable_request_state")

    def _retry_wait(self, folder):
        receipt = read(folder / "attempt-1.response.json")
        value = receipt.get("retry_after", "")
        try:
            seconds = float(value or 60)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - receipt["received_epoch"]
            except (ValueError, TypeError, OverflowError):
                seconds = 60
        seconds = min(300.0, max(60.0, seconds)) if math.isfinite(seconds) else 60.0
        return max(0.0, receipt["received_epoch"] + seconds - self._clock())

    @staticmethod
    def _deadline_retry_seconds(retry_after, received_epoch):
        """Bounded 429 wait of deadline mode and of a SendGate hold (2..60 s)."""
        try:
            seconds = float(retry_after or DEADLINE_RETRY_DEFAULT_SECONDS)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(retry_after).timestamp() - received_epoch
            except (ValueError, TypeError, OverflowError):
                seconds = DEADLINE_RETRY_DEFAULT_SECONDS
        if not math.isfinite(seconds):
            seconds = DEADLINE_RETRY_DEFAULT_SECONDS
        return min(DEADLINE_RETRY_MAX_SECONDS, max(DEADLINE_RETRY_MIN_SECONDS, seconds))

    def _deadline_retry_wait(self, folder):
        receipt = read(folder / "attempt-1.response.json")
        seconds = self._deadline_retry_seconds(receipt.get("retry_after", ""), receipt["received_epoch"])
        return max(0.0, receipt["received_epoch"] + seconds - self._clock())

    def _deadline_allows(self, kind, wait):
        if self._deadline is None:
            return True
        minimum = DEADLINE_MIN_WORK_SECONDS.get(kind, DEADLINE_MIN_WORK_DEFAULT_SECONDS)
        return self._clock() + wait + minimum <= self._deadline

    def _skip(self, kind, wait):
        """Record a skipped HTTP 429 wait for `kind` and return the error to raise."""
        self.rate_limit_skipped.add(kind)
        return RateLimitedNoResend(round(wait, 3))

    def _await_gate(self, kind, timeout):
        """Wait out a shared SendGate hold before a new send. Never writes.

        A call with `timeout` never sleeps for a 429 (as for its own resend);
        a deadline-mode call waits only when the minimum work time still fits.
        """
        if self._gate is None:
            return
        until = self._gate.until()
        while True:
            wait = until - self._clock()
            if wait <= 0:
                return
            if timeout is not None or not self._deadline_allows(kind, wait):
                raise self._skip(kind, wait)
            self._wait(wait)
            later = self._gate.until()
            if later <= until:
                return
            until = later  # another 429 extended the hold while this call waited

    def request(self, kind, payload, timeout=None):
        """`timeout` optionally shortens this call's read wait (seconds).

        It is transport-only: never part of the request identity, descriptor or
        receipt hash, and it never enables a retry of an unknown outcome.
        A request with `timeout` never sleeps for the HTTP 429 resend: when the
        resend wait has not yet elapsed it raises RateLimitedNoResend instead.
        A first attempt answered with HTTP 500/502/503/504 raises
        ProviderServerError and is not resent by this call; a later call of the
        same body sends attempt 2 at once (the 60-300 s wait is for 429 only).
        In deadline mode (client `deadline_seconds`) a call without `timeout`
        replaces the 60-300 s wait by Retry-After or 10 s bounded to 2..60 s,
        sent only when the deadline still leaves the kind's minimum work time
        (else RateLimitedNoResend, nothing written); that resend reads for at
        most the time left. A second 429 still stops for good.
        """
        require(kind in ENDPOINTS and isinstance(payload, dict), "invalid_request_kind")
        require(timeout is None or (type(timeout) in (int, float) and math.isfinite(timeout) and
                                    0 < timeout <= READ_TIMEOUT_SECONDS), "invalid_request_timeout")
        require(payload.get("model") == (QUERY_MODEL if kind == "embedding" else CHAT_MODEL), "disallowed_request_model")
        if kind == "embedding":
            require(isinstance(payload.get("input"), list) and len(payload["input"]) == 1 and
                    isinstance(payload["input"][0], str) and payload["input"][0].strip(), "only_one_question_embedding_allowed")
        descriptor = {"endpoint": ENDPOINTS[kind], "kind": kind, "payload": payload}
        wire, wire_format = request_wire(kind, payload)
        if wire_format:
            descriptor.update(wire_format=wire_format, wire_sha256=digest(wire))
        identity = digest(raw(descriptor))
        folder = self.cache_dir / identity
        state = self._state(folder, identity, payload, wire, wire_format)
        if state.get("complete"):
            meta = {"kind": kind, "request_identity": identity, "cached": True, "called": 0,
                    "requested_model": payload["model"], "returned_model": state["body"]["model"],
                    "response_path": state["response_path"], "raw_path": state["raw_path"]}
            self.calls.append(meta)
            return state["body"], meta
        if self.read_only:
            raise CacheMiss("read_only_cache_miss:" + identity)
        # A shared SendGate hold is waited out (or refused) before anything is written.
        self._await_gate(kind, timeout)
        from request_lock import request_lock
        # Default: one lock across kinds/processes, including the bounded HTTP 429
        # retry. Parallel (service): one lock per request body, so only the same
        # body is serialized and different bodies are sent at the same time.
        # Successful requests do not impose an artificial global start interval.
        lock_path = folder / ".request.lock" if self._parallel else self.cache_dir / ".api.lock"
        with request_lock(lock_path):
            state = self._state(folder, identity, payload, wire, wire_format)
            if state.get("complete"):
                meta = {"kind": kind, "request_identity": identity, "cached": True, "called": 0,
                        "requested_model": payload["model"], "returned_model": state["body"]["model"],
                        "response_path": state["response_path"], "raw_path": state["raw_path"]}
                self.calls.append(meta)
                return state["body"], meta
            key = self._api_key()
            save(folder / "request.json", descriptor)
            if wire_format:
                save_bytes(folder / "request.body.json", wire)
            before = self.called
            transport_timeout = ((CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS) if timeout is None
                                 else (min(CONNECT_TIMEOUT_SECONDS, timeout), timeout))
            while not state.get("complete"):
                number = state["number"]
                if number == 2 and state["first_status"] in RESENDABLE_SERVER_STATUSES:
                    if self.called > before:
                        # This call received the 5xx; the caller decides on a resend.
                        raise ProviderServerError(state["first_status"])
                elif number == 2 and timeout is None and self._deadline is not None:
                    # Deadline mode: a short wait that must leave the minimum work time.
                    wait = self._deadline_retry_wait(folder)
                    if not self._deadline_allows(kind, wait):
                        raise self._skip(kind, wait)
                    self._wait(wait)
                    self._await_gate(kind, timeout)
                    remaining = min(READ_TIMEOUT_SECONDS, self._deadline - self._clock())
                    if not remaining > 0:
                        raise self._skip(kind, 0.0)
                    transport_timeout = (min(CONNECT_TIMEOUT_SECONDS, remaining), remaining)
                elif number == 2:
                    wait = self._retry_wait(folder)
                    if timeout is not None and wait > 0:
                        # The bounded 60-300 s wait ignores the caller's deadline.
                        raise self._skip(kind, wait)
                    self._wait(wait)
                    self._await_gate(kind, timeout)
                attempt_path = folder / f"attempt-{number}.json"
                save(attempt_path, {"request_identity": identity, "payload_sha256": digest(wire),
                                    "requested_at": datetime.now(timezone.utc).isoformat(), "requested_epoch": self._clock()})
                self.called += 1
                response = None
                try:
                    if self._transport is None:
                        import requests
                        transport = requests.post
                    else:
                        transport = self._transport
                    response = transport(ENDPOINTS[kind], headers={"Authorization": "Bearer " + key,
                                         "Content-Type": "application/json"}, data=wire,
                                         timeout=transport_timeout, allow_redirects=False)
                    content = response.content.replace(key.encode("utf-8"), b"[REDACTED]")
                    raw_path = folder / f"attempt-{number}.response.raw"
                    save_bytes(raw_path, content)
                    received = self._clock()
                    retry_after = str(response.headers.get("Retry-After") or "").replace(key, "[REDACTED]")
                    save(folder / f"attempt-{number}.response.json", {
                        "attempt_sha256": digest(attempt_path.read_bytes()), "raw_sha256": digest(content),
                        "http_status": response.status_code, "received_epoch": received,
                        "retry_after": retry_after})
                except Exception as exc:
                    # Never serialize exception messages, which may contain auth data.
                    save(folder / f"attempt-{number}.failure.json", {"error_type": type(exc).__name__,
                         "outcome": "unknown_no_automatic_retry"})
                    error = RuntimeError("request_outcome_unknown_no_automatic_retry")
                    # Class name only (no message), so callers can tell a timeout apart.
                    error.transport_error_type = type(exc).__name__
                    raise error from None
                finally:
                    if response is not None:
                        response.close()
                if self._gate is not None and response.status_code == 429:
                    # After the receipt block: the 429 receipt is already complete.
                    self._gate.hold(received + self._deadline_retry_seconds(retry_after, received))
                state = self._state(folder, identity, payload, wire, wire_format)
            meta = {"kind": kind, "request_identity": identity, "cached": False, "called": self.called - before,
                    "requested_model": payload["model"], "returned_model": state["body"]["model"],
                    "response_path": state["response_path"], "raw_path": state["raw_path"]}
            self.calls.append(meta)
            return state["body"], meta
