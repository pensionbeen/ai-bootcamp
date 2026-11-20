"""Offline candidate checks and trusted, non-destructive deployment verification.

backup/live run from an operator-installed copy over kubectl stdin, including on
older images. Never import a candidate's audit class against the live database.
"""
import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path("/app")
DATA = ROOT / "data/processed/local_rag"
DB = DATA / "request_log.sqlite3"
BACKUPS = DATA / "deploy-backups"
MAX_RECORDS = 10000
SUITE = ("test_request_audit", "test_submission_http", "test_local_rag_service", "test_check_submission")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fetch(path, port=8000, password=None):
    headers = {}
    if password is not None:
        headers["Authorization"] = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=10)) as conn:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read(2 * 1024 * 1024 + 1)
        require(len(body) <= 2 * 1024 * 1024, "Oversized verification response")
        return response.status, response.getheader("Content-Type", ""), body


def endpoints(port, password):
    require(bool(password), "Admin password is missing")
    status, kind, body = fetch("/health", port)
    require(status == 200 and json.loads(body).get("status") == "ok", "Health check failed")
    for path, marker in (("/test", b"ask-test.mjs"), ("/ask-test.mjs", b"/test/submission"),
                         ("/ask-validation.mjs", b"answer"), ("/request-log.mjs", b"/admin/requests/data")):
        status, _, body = fetch(path, port)
        require(status == 200 and marker in body, "Missing expected asset: " + path)
    for path in ("/admin/requests", "/admin/requests/data", "/request-log.html"):
        status, _, _ = fetch(path, port)
        require(status == 401, "Unauthenticated admin route was not protected: " + path)
        status, _, _ = fetch(path, port, "wrong-preflight-password")
        require(status == 401, "Wrong password accepted: " + path)
    status, _, body = fetch("/admin/requests", port, password)
    require(status == 200 and b"request-log.mjs" in body, "Admin page failed")
    status, kind, body = fetch("/admin/requests/data", port, password)
    require(status == 200 and "application/json" in kind, "Admin data failed")
    page = json.loads(body)
    require(isinstance(page.get("items"), list) and isinstance(page.get("total"), int), "Invalid log page")
    return page


def rows(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        require(db.execute("PRAGMA quick_check").fetchone() == ("ok",), "Audit SQLite integrity failed")
        result = {}
        for seq, request_id, payload in db.execute("SELECT seq,request_id,payload FROM requests ORDER BY seq"):
            item = json.loads(payload)
            require(item["request_id"] == request_id, "Audit identity mismatch")
            result[str(seq)] = {"request_id": request_id,
                                "sha256": hashlib.sha256(payload.encode()).hexdigest()
                                if item.get("write_outcome") in ("written", "legacy", "disconnected", "interrupted") else None}
        return result


def snapshot_paths(snapshot_id):
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", snapshot_id or ""), "Invalid snapshot ID")
    require(not BACKUPS.is_symlink(), "Backup directory must not be a symlink")
    return BACKUPS / (snapshot_id + ".sqlite3"), BACKUPS / (snapshot_id + ".json")


def backup(snapshot_id):
    target, manifest = snapshot_paths(snapshot_id)
    require(DB.is_file() and not DB.is_symlink(), "Existing audit database is missing")
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not target.exists() and not manifest.exists(), "Snapshot already exists")
    deadline = time.monotonic() + 30

    def progress(*_):
        require(time.monotonic() < deadline, "Online backup exceeded 30 seconds")

    with closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=10)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination, pages=256, progress=progress, sleep=.1)
    target.chmod(0o600)
    baseline = {"snapshot_id": snapshot_id, "created_at": datetime.now(timezone.utc).isoformat(),
                "rows": rows(target), "verified": False}
    with manifest.open("x", encoding="utf-8") as stream:
        json.dump(baseline, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    manifest.chmod(0o600)
    print(json.dumps({"backup": snapshot_id, "records": len(baseline["rows"])}))


def preserved(baseline, current):
    require(len(current) <= MAX_RECORDS, "Unexpected log retention size")
    oldest = min(map(int, current), default=0)
    for seq, item in baseline.items():
        if seq not in current:
            require(len(current) == MAX_RECORDS and int(seq) < oldest, "Previously recorded request is missing")
            continue
        require(current[seq]["request_id"] == item["request_id"], "Previously recorded request identity changed")
        if item["sha256"] is not None:
            require(current[seq]["sha256"] == item["sha256"], "Previously completed request payload changed")


def retain_backups():
    verified = []
    for path in BACKUPS.glob("*.json"):
        if path.is_symlink() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", path.stem):
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("verified") is True and item.get("snapshot_id") == path.stem:
                verified.append((path.stat().st_mtime, path))
        except (OSError, ValueError):
            continue
    for _, path in sorted(verified, reverse=True)[10:]:
        database = path.with_suffix(".sqlite3")
        if database.is_file() and not database.is_symlink():
            database.unlink()
            path.unlink()


def live(snapshot_id):
    _, manifest = snapshot_paths(snapshot_id)
    require(manifest.is_file() and not manifest.is_symlink(), "Backup manifest is missing")
    baseline = json.loads(manifest.read_text(encoding="utf-8"))
    require(baseline["snapshot_id"] == snapshot_id, "Backup manifest identity mismatch")
    page = endpoints(8000, os.environ.get("REQUEST_LOG_PASSWORD"))
    current = rows(DB)
    preserved(baseline["rows"], current)
    baseline["verified"] = True
    temporary = manifest.with_suffix(".json.tmp")
    require(not temporary.exists() and not temporary.is_symlink(), "Unexpected temporary backup manifest")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(baseline, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(manifest)
    retain_backups()
    print(json.dumps({"live": "ok", "baseline_records": len(baseline["rows"]), "current_records": len(current),
                      "admin_page_records": page["total"]}))


def candidate():
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src") + os.pathsep + str(ROOT / "tests"))
    env.pop("UPSTAGE_API_KEY", None)
    env.pop("REQUEST_LOG_PASSWORD", None)
    subprocess.run([sys.executable, "-m", "unittest", *SUITE], cwd=ROOT, env=env, check=True, timeout=180)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    password = "isolated-deployment-check"
    env.update(UPSTAGE_API_KEY="offline-presence-only", REQUEST_LOG_PASSWORD=password,
               SUBMISSION_CHECK_BASE_URL=f"http://127.0.0.1:{port}")
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen([sys.executable, "-B", "run.py", "serve", "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            for attempt in range(100):
                require(process.poll() is None, "Candidate exited during startup")
                try:
                    if fetch("/health", port)[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError("Candidate readiness timed out")
            endpoints(port, password)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    print(json.dumps({"candidate": "ok", "tests": list(SUITE), "production_data_used": False}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("candidate", "backup", "live"))
    parser.add_argument("--snapshot-id")
    args = parser.parse_args()
    os.umask(0o077)
    if args.mode == "candidate":
        candidate()
    elif args.mode == "backup":
        backup(args.snapshot_id)
    else:
        live(args.snapshot_id)


if __name__ == "__main__":
    main()
