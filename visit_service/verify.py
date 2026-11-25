"""Operator-installed verifier. Executed through stdin inside the visit container."""
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unittest
import urllib.error
import urllib.request
import uuid


def request(path, password=None, marker=None):
    headers = {"Host": "upstage.makertion.com"}
    if marker:
        headers["User-Agent"] = marker
    if password is not None:
        token = base64.b64encode(("admin:" + password).encode()).decode()
        headers["Authorization"] = "Basic " + token
    req = urllib.request.Request("http://127.0.0.1:8080" + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def database():
    path = Path(os.environ.get("VISIT_DATA_DIR", "/data")) / "visit_log.sqlite3"
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok", "Visit database is not healthy"
    return path, connection


def candidate():
    tests = Path("/app/visit_service/tests")
    files = sorted(p for p in tests.glob("test_*.py") if p.name != "test_deploy_runner.py")
    assert files, "No visit application tests present"
    suite = unittest.TestSuite()
    for path in files:
        suite.addTests(unittest.defaultTestLoader.discover(str(tests), pattern=path.name))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("Visit candidate application tests passed.")


def live():
    status, body = request("/health")
    health = json.loads(body)
    assert status == 200 and health["status"] == "ok" and health["logging"]["available"], "Visit logging is unhealthy"
    assert request("/admin/visits")[0] == 401, "Visit dashboard must require authentication"
    assert request("/admin/visits/data", "invalid-deployment-check")[0] == 401
    password = os.environ["REQUEST_LOG_PASSWORD"]
    status, body = request("/admin/visits", password)
    assert status == 200 and b"visit-log.mjs" in body
    status, body = request("/admin/visits/data", password)
    data = json.loads(body)
    assert status == 200 and isinstance(data["items"], list)
    assert data["retention"] == {"days": 30, "max_records": 10000}
    # Test the fixed upstream and the preserved website without invoking a model.
    marker = "UpstageVisitVerifierBot/" + uuid.uuid4().hex
    status, body = request("/", marker=marker)
    assert status == 200 and b"<html" in body.lower(), "Document proxy failed"
    deadline = time.monotonic() + 5
    captured = False
    while time.monotonic() < deadline:
        status, body = request("/admin/visits/data", password)
        assert status == 200
        captured = any(row["user_agent"] == marker and row["path"] == "/" and row["http_status"] == 200
                       for row in json.loads(body)["items"])
        if captured:
            break
        time.sleep(0.1)
    assert captured, "New document visit was not persisted"
    status, body = request("/health")
    assert status == 200 and json.loads(body)["logging"]["available"]
    path, connection = database()
    try:
        count = connection.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
        assert 0 <= count <= 10000
    finally:
        connection.close()
    print(json.dumps({"visit_service": "healthy", "retained_visits": count, "admin_auth": "verified"}))


def backup():
    path, source = database()
    folder = path.parent / "deploy-backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = folder / (stamp + "-" + uuid.uuid4().hex[:8] + ".sqlite3")
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination, pages=128, sleep=0.05)
            assert destination.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            count = destination.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
        finally:
            destination.close()
    finally:
        source.close()
    target.chmod(0o600)
    for old in sorted(folder.glob("*.sqlite3"), reverse=True)[10:]:
        old.unlink()
    print(json.dumps({"visit_backup": target.name, "retained_visits": count}))


if __name__ == "__main__":
    modes = {"candidate": candidate, "live": live, "backup": backup}
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        raise SystemExit("Usage: verify.py candidate|live|backup")
    modes[sys.argv[1]]()
