from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from visit_service.audit import VisitAudit, reported_ip, safe_referrer


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 11, 25, 2, tzinfo=timezone.utc)
        self.audit = VisitAudit(self.temporary.name, clock=lambda: self.now)

    def tearDown(self):
        self.audit.close()
        self.temporary.cleanup()

    def record(self, **overrides):
        args = dict(path="/", http_status=200, elapsed_ms=12.345,
                    headers={"User-Agent": "Example browser"}, socket_peer="127.0.0.1")
        args.update(overrides)
        self.assertTrue(self.audit.record(**args))

    def page(self, **kwargs):
        self.assertTrue(self.audit.flush())
        return self.audit.page(**kwargs)

    def test_metadata_privacy_and_untrusted_ip(self):
        self.record(headers={
            "User-Agent": "python-requests/2.0\r\n" + "x" * 1000,
            "Referer": "https://private-user:private-password@example.com/reset/private-token?q=private-query#private-fragment",
            "CF-Connecting-IP": "not-an-ip",
            "X-Forwarded-For": "198.51.100.55",
            "CF-Ray": "x" * 200,
            "Authorization": "Basic private-auth",
            "Cookie": "private-cookie",
        })
        page = self.page()
        item = page["items"][0]
        self.assertEqual(item["reported_client_ip"], "127.0.0.1")
        self.assertEqual(item["referrer"], "https://example.com")
        self.assertTrue(item["is_bot"])
        self.assertLessEqual(len(item["user_agent"]), 512)
        self.assertNotIn("\r", item["user_agent"])
        self.assertLessEqual(len(item["reported_cf_ray"]), 100)
        self.assertEqual(item["elapsed_ms"], 12.35)
        serialized = json.dumps(page)
        self.assertNotIn("private-", serialized)
        self.assertNotIn("198.51.100.55", serialized)
        self.assertFalse(self.audit.record(path="/?private-query", http_status=200,
                                          elapsed_ms=0, headers={}, socket_peer="127.0.0.1"))
        self.assertFalse(self.audit.record(path="/ask", http_status=200,
                                          elapsed_ms=0, headers={}, socket_peer="127.0.0.1"))
        with closing(sqlite3.connect(self.audit.db_path)) as connection:
            stored = str(connection.execute("SELECT * FROM visits").fetchall())
        self.assertNotIn("private-", stored)

    def test_ip_and_referrer_validation(self):
        self.assertEqual(reported_ip("2001:db8::1", "127.0.0.1"), "2001:db8::1")
        self.assertEqual(reported_ip("198.51.100.20", "127.0.0.1"), "198.51.100.20")
        self.assertEqual(reported_ip("192.0.2.1, 192.0.2.2", "127.0.0.1"), "127.0.0.1")
        self.assertEqual(reported_ip("bad", "bad"), "")
        self.assertEqual(reported_ip("fe80::1%private-scope", "127.0.0.1"), "127.0.0.1")
        self.assertEqual(safe_referrer("javascript:alert(1)"), "")
        self.assertEqual(safe_referrer("https://example.com:bad/path"), "")
        self.assertEqual(safe_referrer("https://example.com/\r\nsecret"), "")
        self.assertEqual(safe_referrer("https://user:pass@[2001:db8::1]:443/p?q=x#f"),
                         "https://[2001:db8::1]:443")

    def test_kst_boundaries_summary_filters_and_empty_page(self):
        # KST midnight is 15:00 UTC on the previous date; 7d includes today + six days.
        for moment in (
            "2026-11-24T15:00:00+00:00", "2026-11-24T14:59:59+00:00",
            "2026-11-18T15:00:00+00:00", "2026-11-18T14:59:59+00:00",
        ):
            self.record(received_at=datetime.fromisoformat(moment))
        page = self.page()
        self.assertEqual(page["total"], 4)
        self.assertEqual(page["summary"], {"today": 1, "last_7_days": 3})
        self.assertEqual(self.audit.page(period="today")["total"], 1)
        self.assertEqual(self.audit.page(period="7d")["total"], 3)
        self.assertEqual(self.audit.page(before=1)["items"], [])
        self.assertIsNone(self.audit.page(before=1)["next_before"])

    def test_retention_age_and_count_with_persistent_capture_time(self):
        self.audit.max_records = 3
        started = self.audit.page()["capturing_since"]
        self.record(received_at=self.now - timedelta(days=31))
        for _ in range(5):
            self.record()
        page = self.page()
        self.assertEqual(page["total"], 3)
        self.assertEqual([item["seq"] for item in page["items"]], [6, 5, 4])
        self.now += timedelta(days=31)
        # Read filtering applies immediately, even before the idle cleanup timer.
        self.assertEqual(self.audit.page()["total"], 0)
        self.audit.close()
        self.audit = VisitAudit(self.temporary.name, clock=lambda: self.now, max_records=3)
        self.assertEqual(self.audit.page()["capturing_since"], started)
        with closing(sqlite3.connect(self.audit.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 0)

    def test_pagination_and_thread_concurrency(self):
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda _: self.record(), range(120)))
        first = self.page()
        self.assertEqual(first["total"], 120)
        self.assertEqual(len(first["items"]), 50)
        second = self.audit.page(before=first["next_before"])
        third = self.audit.page(before=second["next_before"])
        items = first["items"] + second["items"] + third["items"]
        self.assertEqual(len(items), 120)
        self.assertEqual(len({item["seq"] for item in items}), 120)
        self.assertEqual(len({item["request_id"] for item in items}), 120)
        self.assertIsNone(third["next_before"])
        self.assertEqual(self.audit.health()["dropped_records"], 0)

    def test_two_instances_can_write_same_wal_database(self):
        second = VisitAudit(self.temporary.name, clock=lambda: self.now)
        try:
            def record(index):
                audit = self.audit if index % 2 else second
                return audit.record(path="/", http_status=200, elapsed_ms=0,
                                    headers={}, socket_peer="127.0.0.1")
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertTrue(all(pool.map(record, range(100))))
            self.assertTrue(self.audit.flush())
            self.assertTrue(second.flush())
            self.assertEqual(self.audit.page()["total"], 100)
            self.assertEqual(second.page()["total"], 100)
            self.assertEqual(self.audit.health()["dropped_records"], 0)
            self.assertEqual(second.health()["dropped_records"], 0)
        finally:
            second.close()

    def test_database_failure_is_visible_and_record_does_not_block(self):
        lock = sqlite3.connect(self.audit.db_path)
        try:
            lock.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            self.record()
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertTrue(self.audit.flush(timeout=2))
            health = self.audit.health()
            self.assertFalse(health["available"])
            self.assertEqual(health["last_error"], "OperationalError")
            self.assertEqual(health["dropped_records"], 1)
        finally:
            lock.rollback()
            lock.close()
        self.record()
        self.assertTrue(self.audit.flush())
        self.assertTrue(self.audit.health()["available"])

    def test_queue_bound_and_graceful_drain(self):
        self.audit.close()
        self.audit = VisitAudit(self.temporary.name, clock=lambda: self.now, queue_size=1)
        entered = threading.Event()
        release = threading.Event()
        original = self.audit._write

        def paused_write(record):
            entered.set()
            release.wait(2)
            return original(record)

        with patch.object(self.audit, "_write", side_effect=paused_write):
            self.record()
            self.assertTrue(entered.wait(1))
            self.record()
            self.assertFalse(self.audit.record(path="/", http_status=200, elapsed_ms=0,
                                              headers={}, socket_peer="127.0.0.1"))
            self.assertEqual(self.audit.health()["dropped_records"], 1)
            release.set()
            self.assertTrue(self.audit.close(timeout=2))
        self.assertEqual(self.audit.page()["total"], 2)


if __name__ == "__main__":
    unittest.main()
