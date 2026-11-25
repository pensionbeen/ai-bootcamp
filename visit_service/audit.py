"""Bounded, asynchronous visit metadata storage on its own SQLite volume."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import uuid


DOCUMENT_PATHS = frozenset(("/", "/index.html", "/legacy.html", "/test", "/test/", "/test.html"))
RETENTION_DAYS = 30
MAX_RECORDS = 10_000
PAGE_SIZE = 50
KST = timezone(timedelta(hours=9))
BOT_PATTERN = re.compile(
    r"bot|crawler|spider|slurp|headless|bingpreview|facebookexternalhit|"
    r"curl|wget|python-requests|python-urllib|httpx|go-http-client", re.I
)


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bounded_text(value, limit):
    # Control characters are not useful metadata and can spoof a displayed log row.
    return "".join(c for c in str(value or "")[:limit] if ord(c) >= 32 and ord(c) != 127)


def reported_ip(cf_connecting_ip, socket_peer):
    # CF's value is reported metadata, not proof of identity. Never trust arbitrary XFF.
    for value in (cf_connecting_ip, socket_peer):
        value = str(value or "").strip()
        if len(value) > 45 or "%" in value:
            continue
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass
    return ""


def safe_referrer(value):
    value = str(value or "")
    if len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port is not None:
            host += ":" + str(parsed.port)
        # Referrers retain only the origin. A path can itself contain a reset token.
        return urlunsplit((parsed.scheme, host, "", "", ""))[:300]
    except ValueError:
        return ""


class VisitAudit:
    """One writer per process; WAL also supports overlapping rollout processes.

    Request threads only enqueue bounded metadata. Database errors and a full queue
    are visible in health, but cannot prevent a document response from being sent.
    """

    def __init__(self, data_dir, *, clock=utc_now, max_records=MAX_RECORDS,
                 retention_days=RETENTION_DAYS, queue_size=1024):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "visit_log.sqlite3"
        self.clock = clock
        self.max_records = max_records
        self.retention_days = retention_days
        self._queue = queue.Queue(maxsize=queue_size)
        self._state = threading.Condition()
        self._db_lock = threading.Lock()
        self._closing = threading.Event()
        self._pending = 0
        self._last_error = None
        self._dropped = 0
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._worker = threading.Thread(target=self._run, name="visit-log-writer", daemon=True)
        self._worker.start()

    @contextmanager
    def _connection(self, timeout=0.25):
        connection = sqlite3.connect(self.db_path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=NORMAL")
            yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection(timeout=5) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            with connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS visits (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT NOT NULL UNIQUE,
                        received_at TEXT NOT NULL,
                        path TEXT NOT NULL,
                        http_status INTEGER NOT NULL,
                        elapsed_ms REAL NOT NULL,
                        reported_client_ip TEXT NOT NULL,
                        user_agent TEXT NOT NULL,
                        referrer TEXT NOT NULL,
                        reported_cf_ray TEXT NOT NULL,
                        is_bot INTEGER NOT NULL
                    )
                """)
                connection.execute("CREATE INDEX IF NOT EXISTS visits_received_at ON visits(received_at)")
                connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("INSERT OR IGNORE INTO metadata VALUES ('capturing_since', ?)",
                                   (timestamp(self.clock()),))
                self._prune(connection)

    def _prune(self, connection):
        cutoff = timestamp(self.clock() - timedelta(days=self.retention_days))
        connection.execute("DELETE FROM visits WHERE received_at < ?", (cutoff,))
        connection.execute("""
            DELETE FROM visits WHERE seq <= COALESCE(
                (SELECT seq FROM visits ORDER BY seq DESC LIMIT 1 OFFSET ?), 0
            )
        """, (self.max_records,))

    def _failure(self, error, *, dropped=False):
        with self._state:
            # Do not expose exception messages, file contents, or incoming headers.
            self._last_error = type(error).__name__
            if dropped:
                self._dropped += 1

    def note_failure(self, error):
        """Surface an enqueue failure without propagating it to a page request."""
        self._failure(error, dropped=True)

    def health(self):
        with self._state:
            return {"available": self._last_error is None,
                    "last_error": self._last_error,
                    "dropped_records": self._dropped,
                    "pending_records": self._pending}

    def record(self, *, path, http_status, elapsed_ms, headers, socket_peer, received_at=None):
        if path not in DOCUMENT_PATHS:
            return False
        user_agent = bounded_text(headers.get("User-Agent"), 512)
        record = (uuid.uuid4().hex, timestamp(received_at or self.clock()), path,
                  int(http_status), round(max(0, float(elapsed_ms)), 2),
                  reported_ip(headers.get("CF-Connecting-IP"), socket_peer),
                  user_agent, safe_referrer(headers.get("Referer")),
                  bounded_text(headers.get("CF-Ray"), 100), bool(BOT_PATTERN.search(user_agent)))
        with self._state:
            if self._closing.is_set():
                return False
            try:
                self._queue.put_nowait(record)
            except queue.Full as error:
                self._last_error = type(error).__name__
                self._dropped += 1
                return False
            self._pending += 1
        return True

    def _write(self, record):
        if not self._db_lock.acquire(timeout=0.25):
            raise TimeoutError("visit log is busy")
        try:
            with self._connection() as connection, connection:
                if record is not None:
                    connection.execute("""
                        INSERT INTO visits
                        (request_id, received_at, path, http_status, elapsed_ms,
                         reported_client_ip, user_agent, referrer, reported_cf_ray, is_bot)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, record)
                self._prune(connection)
        finally:
            self._db_lock.release()

    def _run(self):
        last_cleanup = time.monotonic()
        while not self._closing.is_set() or not self._queue.empty():
            try:
                record = self._queue.get(timeout=0.25)
            except queue.Empty:
                if time.monotonic() - last_cleanup >= 60:
                    try:
                        self._write(None)
                    except Exception as error:
                        self._failure(error)
                    last_cleanup = time.monotonic()
                continue
            try:
                self._write(record)
            except Exception as error:
                self._failure(error, dropped=True)
            else:
                with self._state:
                    self._last_error = None
                last_cleanup = time.monotonic()
            finally:
                with self._state:
                    self._pending -= 1
                    self._state.notify_all()
                self._queue.task_done()

    def flush(self, timeout=5):
        with self._state:
            return self._state.wait_for(lambda: self._pending == 0, timeout=timeout)

    def close(self, timeout=10):
        # Do not let a last request enqueue after the writer observes an empty queue.
        with self._state:
            self._closing.set()
        self._worker.join(timeout=timeout)
        return not self._worker.is_alive()

    def page(self, *, before=None, period="all"):
        if period not in ("all", "today", "7d"):
            raise ValueError("invalid period")
        if before is not None and (not isinstance(before, int) or isinstance(before, bool)
                                   or not 1 <= before <= 9223372036854775807):
            raise ValueError("invalid cursor")
        now = self.clock()
        today = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        last_7_days = today - timedelta(days=6)
        retention_cutoff = timestamp(now - timedelta(days=self.retention_days))
        cutoff = {"all": retention_cutoff, "today": timestamp(today),
                  "7d": timestamp(last_7_days)}[period]
        cutoff = max(cutoff, retention_cutoff)
        if not self._db_lock.acquire(timeout=0.25):
            raise TimeoutError("visit log is busy")
        try:
            with self._connection() as connection:
                # One read snapshot keeps the count and cursor page consistent.
                connection.execute("BEGIN")
                total = connection.execute("SELECT COUNT(*) FROM visits WHERE received_at >= ?", (cutoff,)).fetchone()[0]
                rows = connection.execute("""
                    SELECT * FROM visits WHERE received_at >= ? AND (? IS NULL OR seq < ?)
                    ORDER BY seq DESC LIMIT ?
                """, (cutoff, before, before, PAGE_SIZE + 1)).fetchall()
                counts = connection.execute("""
                    SELECT COALESCE(SUM(received_at >= ?), 0),
                           COALESCE(SUM(received_at >= ?), 0)
                    FROM visits WHERE received_at >= ?
                """, (timestamp(today), timestamp(last_7_days), retention_cutoff)).fetchone()
                started = connection.execute("SELECT value FROM metadata WHERE key='capturing_since'").fetchone()[0]
        except Exception as error:
            self._failure(error)
            raise
        finally:
            self._db_lock.release()
        items = [dict(row) for row in rows[:PAGE_SIZE]]
        for item in items:
            item["is_bot"] = bool(item["is_bot"])
        return {"items": items, "total": total,
                "next_before": items[-1]["seq"] if len(rows) > PAGE_SIZE else None,
                "summary": {"today": counts[0], "last_7_days": counts[1]},
                "retention": {"days": self.retention_days, "max_records": self.max_records},
                "capturing_since": started}
