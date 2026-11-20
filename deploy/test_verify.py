"""Deployment data-preservation checks using isolated SQLite files only."""
from contextlib import closing, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("deployment_verify", Path(__file__).with_name("verify.py"))
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def record(identity, fingerprint="completed-payload"):
    return {"request_id": identity, "sha256": fingerprint}


class PreservationTests(unittest.TestCase):
    def test_new_requests_are_allowed(self):
        verify.preserved({"1": record("first")}, {"1": record("first"), "2": record("new")})

    def test_missing_record_below_retention_limit_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "request is missing"):
            verify.preserved({"1": record("first")}, {"2": record("new")})

    def test_changed_identity_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            verify.preserved({"1": record("first")}, {"1": record("replacement")})

    def test_completed_payload_change_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "payload changed"):
            verify.preserved({"1": record("first")}, {"1": record("first", "changed")})

    def test_completed_record_cannot_become_pending(self):
        with self.assertRaisesRegex(RuntimeError, "payload changed"):
            verify.preserved({"1": record("first")}, {"1": record("first", None)})

    def test_pending_request_may_complete_without_identity_change(self):
        verify.preserved({"1": record("first", None)}, {"1": record("first", "new-response")})

    def test_natural_pruning_is_allowed_only_with_full_retained_window(self):
        current = {str(seq): record("request-" + str(seq)) for seq in range(2, verify.MAX_RECORDS + 2)}
        verify.preserved({"1": record("old"), "2": record("request-2")}, current)
        current.pop(str(verify.MAX_RECORDS + 1))
        with self.assertRaisesRegex(RuntimeError, "request is missing"):
            verify.preserved({"1": record("old")}, current)

    def test_hole_inside_full_window_is_not_retention(self):
        current = {str(seq): record("request-" + str(seq)) for seq in range(1, verify.MAX_RECORDS + 2) if seq != 2}
        with self.assertRaisesRegex(RuntimeError, "request is missing"):
            verify.preserved({"2": record("removed-in-middle")}, current)

    def test_retention_limit_overflow_is_rejected(self):
        current = {str(seq): record("id") for seq in range(verify.MAX_RECORDS + 1)}
        with self.assertRaisesRegex(RuntimeError, "retention size"):
            verify.preserved({}, current)


class SQLiteDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="upstage-verifier-test-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        data = root / "data/processed/local_rag"
        data.mkdir(parents=True)
        self.paths = patch.multiple(verify, ROOT=root, DATA=data, DB=data / "request_log.sqlite3", BACKUPS=data / "deploy-backups")
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.environment = patch.dict(os.environ, {"REQUEST_LOG_PASSWORD": "test-only-admin-password"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.connection = sqlite3.connect(verify.DB)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE requests (seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.connection.execute("INSERT INTO metadata VALUES ('legacy_imported', '1')")
        self.add_record("completed", "written", "요청 질문")
        self.add_record("in-flight", "pending", "진행 중 질문")

    def add_record(self, identity, outcome, question):
        payload = {"request_id": identity, "write_outcome": outcome, "question": question}
        self.connection.execute("INSERT INTO requests (request_id,payload) VALUES (?,?)", (identity, json.dumps(payload, ensure_ascii=False)))
        self.connection.commit()

    def update_record(self, identity, **updates):
        payload = json.loads(self.connection.execute("SELECT payload FROM requests WHERE request_id=?", (identity,)).fetchone()[0])
        payload.update(updates)
        self.connection.execute("UPDATE requests SET payload=? WHERE request_id=?", (json.dumps(payload, ensure_ascii=False), identity))
        self.connection.commit()

    def snapshot(self, identity="first-deployment"):
        with redirect_stdout(io.StringIO()):
            verify.backup(identity)
        target, manifest = verify.snapshot_paths(identity)
        return target, manifest

    def page(self):
        items = [{"seq": seq, "request_id": identity} for seq, identity in self.connection.execute(
            "SELECT seq,request_id FROM requests ORDER BY seq DESC LIMIT 50")]
        return {"total": self.connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0], "items": items}

    def test_online_backup_contains_committed_wal_records_without_mutating_source(self):
        self.assertTrue(Path(str(verify.DB) + "-wal").exists())
        before = list(self.connection.execute("SELECT request_id,payload FROM requests ORDER BY seq"))
        target, manifest = self.snapshot()
        with closing(sqlite3.connect(target)) as snapshot:
            self.assertEqual(before, list(snapshot.execute("SELECT request_id,payload FROM requests ORDER BY seq")))
            self.assertEqual("1", snapshot.execute("SELECT value FROM metadata WHERE name='legacy_imported'").fetchone()[0])
        self.assertEqual(before, list(self.connection.execute("SELECT request_id,payload FROM requests ORDER BY seq")))
        value = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(2, len(value["rows"]))
        self.assertFalse(value["verified"])
        self.assertIsNotNone(value["rows"]["1"]["sha256"])
        self.assertIsNone(value["rows"]["2"]["sha256"])

    def test_live_allows_new_request_and_pending_completion_and_verifies_snapshot(self):
        _, manifest = self.snapshot()
        self.update_record("in-flight", write_outcome="written", response={"answer": "완료"})
        self.add_record("new-arrival", "written", "추가 질문")
        with patch.object(verify, "endpoints", return_value=self.page()) as endpoints, redirect_stdout(io.StringIO()):
            verify.live("first-deployment")
        endpoints.assert_called_once_with(8000, "test-only-admin-password")
        self.assertTrue(json.loads(manifest.read_text(encoding="utf-8"))["verified"])
        self.assertEqual(3, self.connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0])

    def test_live_payload_change_fails_without_marking_verified(self):
        _, manifest = self.snapshot()
        self.update_record("completed", question="unexpectedly rewritten question")
        with patch.object(verify, "endpoints", return_value=self.page()), self.assertRaisesRegex(RuntimeError, "payload changed"):
            verify.live("first-deployment")
        self.assertFalse(json.loads(manifest.read_text(encoding="utf-8"))["verified"])

    def test_live_missing_record_fails_without_database_restoration(self):
        _, manifest = self.snapshot()
        self.connection.execute("DELETE FROM requests WHERE request_id='completed'")
        self.connection.commit()
        with patch.object(verify, "endpoints", return_value=self.page()), self.assertRaisesRegex(RuntimeError, "request is missing"):
            verify.live("first-deployment")
        self.assertFalse(json.loads(manifest.read_text(encoding="utf-8"))["verified"])
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0])

    def test_failed_endpoint_does_not_mark_snapshot_verified(self):
        _, manifest = self.snapshot()
        with patch.object(verify, "endpoints", side_effect=RuntimeError("admin unavailable")), self.assertRaisesRegex(RuntimeError, "admin unavailable"):
            verify.live("first-deployment")
        self.assertFalse(json.loads(manifest.read_text(encoding="utf-8"))["verified"])

    def test_empty_viewer_fails_even_when_database_is_preserved(self):
        _, manifest = self.snapshot()
        with patch.object(verify, "endpoints", return_value={"total": 0, "items": []}), self.assertRaisesRegex(RuntimeError, "missing historical records"):
            verify.live("first-deployment")
        self.assertFalse(json.loads(manifest.read_text(encoding="utf-8"))["verified"])
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0])

    def test_incomplete_viewer_page_is_rejected(self):
        _, manifest = self.snapshot()
        with patch.object(verify, "endpoints", return_value={"total": 2, "items": []}), self.assertRaisesRegex(RuntimeError, "incomplete page"):
            verify.live("first-deployment")
        self.assertFalse(json.loads(manifest.read_text(encoding="utf-8"))["verified"])

    def test_viewer_with_wrong_record_identity_is_rejected(self):
        _, manifest = self.snapshot()
        page = self.page()
        page["items"][0]["request_id"] = "from-another-database"
        with patch.object(verify, "endpoints", return_value=page), self.assertRaisesRegex(RuntimeError, "does not match"):
            verify.live("first-deployment")
        self.assertFalse(json.loads(manifest.read_text(encoding="utf-8"))["verified"])

    def test_viewer_with_duplicate_records_is_rejected(self):
        _, manifest = self.snapshot()
        page = self.page()
        page["items"][1] = dict(page["items"][0])
        with patch.object(verify, "endpoints", return_value=page), self.assertRaisesRegex(RuntimeError, "duplicate records"):
            verify.live("first-deployment")
        self.assertFalse(json.loads(manifest.read_text(encoding="utf-8"))["verified"])

    def test_interrupted_request_is_terminal_and_cannot_be_rewritten(self):
        self.update_record("in-flight", write_outcome="interrupted")
        _, manifest = self.snapshot()
        self.update_record("in-flight", question="unexpectedly rewritten interrupted question")
        with patch.object(verify, "endpoints", return_value=self.page()), self.assertRaisesRegex(RuntimeError, "payload changed"):
            verify.live("first-deployment")
        self.assertFalse(json.loads(manifest.read_text(encoding="utf-8"))["verified"])

    def test_duplicate_snapshot_is_not_overwritten(self):
        target, manifest = self.snapshot()
        original = target.read_bytes(), manifest.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.snapshot()
        self.assertEqual(original, (target.read_bytes(), manifest.read_bytes()))

    def test_invalid_snapshot_path_is_rejected(self):
        for identity in ("../escape", "nested/name", "", "name.json", "a" * 121):
            with self.subTest(identity=identity), self.assertRaisesRegex(RuntimeError, "Invalid snapshot ID"):
                verify.backup(identity)

    def test_retention_removes_only_older_verified_pairs(self):
        verify.BACKUPS.mkdir()
        for index in range(12):
            identity = "known-" + str(index)
            target, manifest = verify.snapshot_paths(identity)
            target.write_bytes(b"test snapshot")
            manifest.write_text(json.dumps({"snapshot_id": identity, "verified": True}), encoding="utf-8")
            os.utime(manifest, (1000 + index, 1000 + index))
        pending_target, pending_manifest = verify.snapshot_paths("not-yet-verified")
        pending_target.write_bytes(b"pending backup")
        pending_manifest.write_text(json.dumps({"snapshot_id": "not-yet-verified", "verified": False}), encoding="utf-8")
        unrelated = verify.BACKUPS / "operator-notes.json"
        unrelated.write_text('{"keep": true}', encoding="utf-8")
        verify.retain_backups()
        self.assertFalse((verify.BACKUPS / "known-0.json").exists())
        self.assertFalse((verify.BACKUPS / "known-1.sqlite3").exists())
        self.assertTrue((verify.BACKUPS / "known-2.json").exists())
        self.assertTrue((verify.BACKUPS / "known-11.sqlite3").exists())
        self.assertTrue(pending_target.exists())
        self.assertTrue(pending_manifest.exists())
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
