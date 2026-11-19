"""Persistent operator-only records of HTTP requests, independent of answer caching."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid


MAX_RECORDS = 10_000
PAGE_SIZE = 50
AUDIT_PATH = "data/processed/local_rag/request_log.sqlite3"


def timestamp(seconds=None):
    value = datetime.now(timezone.utc) if seconds is None else datetime.fromtimestamp(seconds, timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RequestAudit:
    def __init__(self, project, legacy_records=(), legacy_folder=None):
        self.project = Path(project).resolve(strict=True)
        self.path = (self.project / AUDIT_PATH).resolve()
        if not self.path.is_relative_to(self.project):
            raise ValueError("audit_outside_project")
        self.lock = threading.Lock()
        self.closed = False
        self.initialized = False
        self.legacy_records = list(legacy_records)
        self.legacy_folder = legacy_folder
        if self.path.exists():
            with self.lock:
                self._initialize()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        if self.closed:
            raise RuntimeError("audit_closed")
        if self.initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS requests (seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for seq, raw in connection.execute("SELECT seq, payload FROM requests"):
                record = json.loads(raw)
                if record["write_outcome"] == "pending":
                    record["write_outcome"] = "interrupted"
                    connection.execute("UPDATE requests SET payload=? WHERE seq=?", (json.dumps(record, ensure_ascii=False), seq))
            if connection.execute("SELECT value FROM metadata WHERE name='legacy_imported'").fetchone() is None:
                legacy_items = []
                for record in self.legacy_records:
                    job_id = record["job_id"]
                    saved_at = None
                    if self.legacy_folder is not None:
                        try:
                            saved_at = timestamp((self.legacy_folder / (job_id + ".json")).stat().st_mtime)
                        except OSError:
                            pass
                    item = self._record("legacy", "legacy-" + job_id)
                    item.update(last_saved_at=saved_at, question=record.get("question"),
                                response=record.get("response"), rag_status=record.get("status"),
                                write_outcome="legacy", job_id=job_id)
                    legacy_items.append(item)
                for item in sorted(legacy_items, key=lambda item: item["last_saved_at"] or ""):
                    connection.execute("INSERT OR IGNORE INTO requests (request_id,payload) VALUES (?,?)", (item["request_id"], json.dumps(item, ensure_ascii=False)))
                connection.execute("INSERT INTO metadata VALUES ('legacy_imported','1')")
            self._prune(connection)
        self.legacy_records = []
        self.initialized = True

    def close(self):
        # Synchronize with the last writer before the server's owner releases storage.
        with self.lock:
            self.closed = True

    @staticmethod
    def _record(kind, request_id):
        return {"request_id": request_id, "record_kind": kind, "received_at": None, "last_saved_at": None,
                "question": None, "request": None, "response": None, "http_status": None,
                "rag_status": None, "elapsed_ms": None, "reported_client_ip": None,
                "reported_cf_ray": None, "user_agent": None, "socket_peer": None,
                "write_outcome": "pending", "job_id": None}

    @staticmethod
    def _prune(connection):
        connection.execute("DELETE FROM requests WHERE seq NOT IN (SELECT seq FROM requests ORDER BY seq DESC LIMIT ?)", (MAX_RECORDS,))

    def begin(self, headers, socket_peer):
        item = self._record("request", uuid.uuid4().hex)
        item.update(received_at=timestamp(), socket_peer=socket_peer,
                    reported_client_ip=(headers.get("CF-Connecting-IP") or "")[:100] or None,
                    reported_cf_ray=(headers.get("CF-Ray") or "")[:100] or None,
                    user_agent=(headers.get("User-Agent") or "")[:512] or None)
        with self.lock:
            self._initialize()
            with self._connection() as connection:
                cursor = connection.execute("INSERT INTO requests (request_id,payload) VALUES (?,?)", (item["request_id"], json.dumps(item, ensure_ascii=False)))
                self._prune(connection)
                return cursor.lastrowid

    def update(self, seq, **fields):
        if seq is None:
            return
        with self.lock:
            self._initialize()
            with self._connection() as connection:
                row = connection.execute("SELECT payload FROM requests WHERE seq=?", (seq,)).fetchone()
                if row is None:
                    return
                record = json.loads(row[0])
                record.update(fields)
                connection.execute("UPDATE requests SET payload=? WHERE seq=?", (json.dumps(record, ensure_ascii=False, allow_nan=False), seq))

    def page(self, before=None):
        with self.lock:
            self._initialize()
            with self._connection() as connection:
                total = connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
                if before is None:
                    rows = connection.execute("SELECT seq,payload FROM requests ORDER BY seq DESC LIMIT ?", (PAGE_SIZE + 1,)).fetchall()
                else:
                    rows = connection.execute("SELECT seq,payload FROM requests WHERE seq < ? ORDER BY seq DESC LIMIT ?", (before, PAGE_SIZE + 1)).fetchall()
                items = [{**json.loads(raw), "seq": seq} for seq, raw in rows[:PAGE_SIZE]]
                next_before = items[-1]["seq"] if len(rows) > PAGE_SIZE else None
                return {"items": items, "next_before": next_before, "total": total}
