"""Offline attachment collection checks using isolated, explicitly copied fixtures.

Only six reviewed XML/metadata pairs, their verification records, and the two
existing HWP/metadata pairs are copied. No credentials or live HTTP are used.
"""

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import download_attachment as downloader


PROJECT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
TEMP_PREFIX = "attachment-test-"
EXISTING_STEMS = ("2171833_22740723", "2171833_22740725")


@unittest.skipUnless((PROJECT / "data/raw/ordinances.manifest.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/raw/ordinances.manifest.json")
class AttachmentCollectionTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=TEST_ROOT)).resolve()
        self.addCleanup(self.cleanup_project)
        no_network = patch.object(
            requests.sessions.Session,
            "request",
            side_effect=AssertionError("Live HTTP is forbidden in attachment tests"),
        )
        no_network.start()
        self.addCleanup(no_network.stop)

        manifest_path = Path("data/raw/ordinances.manifest.json")
        manifest = json.loads((PROJECT / manifest_path).read_text(encoding="utf-8"))
        relatives = [manifest_path]
        for entry in manifest["ordinances"]:
            relatives.extend((Path(entry["raw_file"]), Path(entry["metadata_file"])))
        report_path = Path(manifest["as_of_verification"]["report_file"])
        report = json.loads((PROJECT / report_path).read_text(encoding="utf-8"))
        relatives.extend((report_path, report_path.parent / report["evidence_file"]))
        for stem in EXISTING_STEMS:
            relatives.extend(
                Path("data/raw/attachments") / f"{stem}{suffix}"
                for suffix in (".hwp", ".metadata.json")
            )
        for relative in relatives:
            source = (PROJECT / relative).resolve(strict=True)
            target = (self.project / relative).resolve()
            self.assertTrue(source.is_relative_to(PROJECT))
            self.assertTrue(target.is_relative_to(self.project))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        self.raw_dir = self.project / "data/raw/attachments"
        self.hwp_bytes = (self.raw_dir / f"{EXISTING_STEMS[0]}.hwp").read_bytes()
        self.inputs, self.items = downloader.catalog(self.project)
        self.before_existing = self.existing_snapshot()

    def cleanup_project(self):
        target = self.project.resolve()
        if (
            target == TEST_ROOT.resolve()
            or target.parent != TEST_ROOT.resolve()
            or not target.is_relative_to(TEST_ROOT.resolve())
            or not target.name.startswith(TEMP_PREFIX)
        ):
            raise AssertionError(f"Unsafe test cleanup target: {target}")
        if target.exists():
            shutil.rmtree(target)

    def path(self, item, field):
        value = Path(item[field])
        result = value if value.is_absolute() else self.project / value
        self.assertTrue(result.resolve().is_relative_to(self.project))
        return result

    def existing_snapshot(self):
        result = {}
        for stem in EXISTING_STEMS:
            for suffix in (".hwp", ".metadata.json"):
                path = self.raw_dir / f"{stem}{suffix}"
                result[path.name] = (path.read_bytes(), path.stat().st_mtime_ns)
        return result

    def attachment_snapshot(self):
        return {
            path.relative_to(self.project).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.raw_dir.rglob("*")
            if path.is_file() and path.suffix in (".hwp", ".json")
        }

    def fake_fetch(self, url):
        requested = downloader.normalize_url(url)
        return self.hwp_bytes, {
            "requested_url": requested,
            "final_url": requested,
            "content_type": "application/hwp;;charset=utf-8",
        }

    def collect_all(self):
        with patch.object(downloader, "fetch_hwp", side_effect=self.fake_fetch) as fetch:
            result = downloader.collect(self.project)
        return result, fetch

    def assert_complete(self):
        inputs, items, records = downloader.inspect(self.project)
        self.assertEqual(len(items), 14)
        self.assertEqual(len(records), 14)
        self.assertTrue(all(record is not None for record in records))
        downloader.check_collection(self.project, require_complete=True)
        return inputs, items, records

    def test_catalog_has_exact_scope_and_preserves_unusual_numbering(self):
        self.assertEqual(len(self.items), 14)
        ids = [item["attachment"]["attachment_id"] for item in self.items]
        self.assertEqual(len(set(ids)), 14)
        counts = {
            seq: sum(item["ordin_seq"] == seq for item in self.items)
            for seq in ("2171833", "2171767", "2147709")
        }
        self.assertEqual(counts, {"2171833": 8, "2171767": 1, "2147709": 5})
        bike = next(item for item in self.items if item["ordin_seq"] == "2171767")
        self.assertEqual(bike["attachment"]["attachment_no"], "0000")
        park_numbers = {
            item["attachment"]["attachment_no"]
            for item in self.items if item["ordin_seq"] == "2147709"
        }
        self.assertEqual(park_numbers, {"0001", "0002", "0003", "0005", "0006"})
        self.assertTrue(all(item["attachment"]["file_type"] == "hwp" for item in self.items))

    def test_offline_check_reports_two_existing_twelve_missing_without_writes(self):
        before = self.attachment_snapshot()
        with patch.object(downloader, "fetch_hwp") as fetch:
            summary = downloader.check_collection(self.project)
            _, items, records = downloader.inspect(self.project)
        fetch.assert_not_called()
        self.assertEqual(summary, {"total": 14, "present": 2, "missing": 12, "manifest_verified": False})
        self.assertEqual(len(items), 14)
        self.assertEqual(sum(record is None for record in records), 12)
        self.assertEqual(sum(record is not None for record in records), 2)
        self.assertEqual(before, self.attachment_snapshot())

    def test_require_complete_rejects_missing_attachments(self):
        with self.assertRaises(ValueError):
            downloader.check_collection(self.project, require_complete=True)

    def test_collect_downloads_only_missing_and_preserves_existing_bytes_and_timestamps(self):
        summary, fetch = self.collect_all()
        self.assertEqual(fetch.call_count, 12)
        self.assertEqual(summary, {"total": 14, "present": 14, "missing": 0, "manifest_verified": True,
                                   "downloaded": 12, "reused": 2})
        self.assertEqual(self.before_existing, self.existing_snapshot())
        _, items, records = self.assert_complete()
        for item, record in zip(items, records):
            content = self.path(item, "file_path").read_bytes()
            metadata_raw = self.path(item, "metadata_path").read_bytes()
            metadata = json.loads(metadata_raw)
            self.assertEqual(metadata["attachment"]["attachment_id"], item["attachment"]["attachment_id"])
            self.assertEqual(record["size_bytes"], len(content))
            self.assertEqual(record["content_hash"], hashlib.sha256(content).hexdigest())
            self.assertEqual(record["metadata_content_hash"], hashlib.sha256(metadata_raw).hexdigest())
            self.assertEqual(metadata["download"]["content_hash"], record["content_hash"])

    def test_completed_rerun_makes_no_http_and_rewrites_no_timestamp_or_manifest(self):
        self.collect_all()
        before = self.attachment_snapshot()
        with patch.object(downloader, "fetch_hwp") as fetch:
            summary = downloader.collect(self.project)
        fetch.assert_not_called()
        self.assertEqual(summary["downloaded"], 0)
        self.assertEqual(summary["reused"], 14)
        self.assertEqual(before, self.attachment_snapshot())
        self.assert_complete()

    def test_changed_parent_xml_aborts_before_any_http_or_attachment_write(self):
        xml = self.project / "data/raw/2171803.xml"
        xml.write_bytes(xml.read_bytes() + b"\n")
        before = self.attachment_snapshot()
        with patch.object(downloader, "fetch_hwp") as fetch:
            with self.assertRaises(ValueError):
                downloader.collect(self.project)
        fetch.assert_not_called()
        self.assertEqual(before, self.attachment_snapshot())

    def test_conflicting_last_attachment_aborts_before_fetching_earlier_missing_items(self):
        last = self.items[-1]
        self.path(last, "file_path").write_bytes(self.hwp_bytes)
        self.path(last, "metadata_path").write_text("{}", encoding="utf-8")
        before = self.attachment_snapshot()
        with patch.object(downloader, "fetch_hwp") as fetch:
            with self.assertRaises((ValueError, KeyError)):
                downloader.collect(self.project)
        fetch.assert_not_called()
        self.assertEqual(before, self.attachment_snapshot())

    def test_both_kinds_of_incomplete_pair_are_rejected_before_http(self):
        missing = next(item for item in self.items if not self.path(item, "file_path").exists())
        for field, raw in (("file_path", self.hwp_bytes), ("metadata_path", b"{}")):
            with self.subTest(orphan=field):
                orphan = self.path(missing, field)
                orphan.write_bytes(raw)
                try:
                    before = self.attachment_snapshot()
                    with patch.object(downloader, "fetch_hwp") as fetch:
                        with self.assertRaises(ValueError):
                            downloader.collect(self.project)
                    fetch.assert_not_called()
                    self.assertEqual(before, self.attachment_snapshot())
                finally:
                    orphan.unlink()

    def test_existing_hwp_hash_mismatch_is_not_repaired_by_redownload(self):
        original = self.raw_dir / f"{EXISTING_STEMS[0]}.hwp"
        content = bytearray(original.read_bytes())
        content[-1] ^= 1
        original.write_bytes(content)
        before = self.attachment_snapshot()
        with patch.object(downloader, "fetch_hwp") as fetch:
            with self.assertRaises(ValueError):
                downloader.collect(self.project)
        fetch.assert_not_called()
        self.assertEqual(before, self.attachment_snapshot())

    def test_legacy_metadata_checks_each_parent_source_field_without_rewriting(self):
        item = next(item for item in self.items if self.path(item, "file_path").exists())
        path = self.path(item, "metadata_path")
        original = path.read_bytes()
        record = json.loads(original)
        for field in ("ordin_id", "ordin_seq", "as_of_date", "effective_date", "promulgation_date", "source_url", "content_hash"):
            with self.subTest(field=field):
                changed = copy.deepcopy(record)
                changed["metadata"][field] = "conflicting-value"
                path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(ValueError):
                    downloader.validate_saved(self.project, item)
        path.write_bytes(original)
        self.assertIsNotNone(downloader.validate_saved(self.project, item))
        self.assertEqual(path.read_bytes(), original)

    def test_network_failure_keeps_completed_pair_and_resume_fetches_only_remaining(self):
        calls = 0

        def fail_second(url):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("simulated download connection failure")
            return self.fake_fetch(url)

        with patch.object(downloader, "fetch_hwp", side_effect=fail_second):
            with self.assertRaises(ValueError):
                downloader.collect(self.project)
        _, _, records = downloader.inspect(self.project)
        self.assertEqual(sum(record is not None for record in records), 3)
        self.assertFalse((self.project / downloader.MANIFEST).exists())
        self.assertEqual(self.before_existing, self.existing_snapshot())
        _, fetch = self.collect_all()
        self.assertEqual(fetch.call_count, 11)
        self.assert_complete()

    def test_hwp_validator_accepts_real_hwp_and_rejects_error_payloads(self):
        downloader.validate_hwp(self.hwp_bytes, "application/hwp;;charset=utf-8")
        bad = (
            (b"", "application/hwp"),
            (b"<html>error</html>", "application/octet-stream"),
            (b'{"error":"denied"}', "application/hwp"),
            (b"Service temporarily unavailable", "application/hwp"),
            (self.hwp_bytes, "text/html"),
            (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 512, "application/hwp"),
        )
        for content, content_type in bad:
            with self.subTest(content_type=content_type, prefix=content[:12]):
                with self.assertRaises(ValueError):
                    downloader.validate_hwp(content, content_type)

    def test_http_200_error_document_is_not_accepted_as_a_download(self):
        url = downloader.normalize_url(self.items[0]["attachment"]["download_url"])
        for content_type, raw in (
            ("text/html", b"<html>download unavailable</html>"),
            ("application/hwp", b"The service is not available"),
            ("application/json", b'{"error":"not found"}'),
        ):
            with self.subTest(content_type=content_type):
                response = requests.Response()
                response.status_code = 200
                response.url = url
                response.headers["Content-Type"] = content_type
                response._content = raw
                response._content_consumed = True
                with patch.object(downloader.requests, "get", return_value=response):
                    with self.assertRaises(ValueError):
                        downloader.fetch_hwp(url)

    def test_redirect_and_truncated_download_are_rejected(self):
        url = downloader.normalize_url(self.items[0]["attachment"]["download_url"])
        for status, declared in ((302, None), (200, str(len(self.hwp_bytes) + 1))):
            with self.subTest(status=status, declared=declared):
                response = requests.Response()
                response.status_code = status
                response.url = url
                response.headers["Content-Type"] = "application/hwp"
                if declared is not None:
                    response.headers["Content-Length"] = declared
                response._content = self.hwp_bytes
                response._content_consumed = True
                with patch.object(downloader.requests, "get", return_value=response) as get:
                    with self.assertRaises(ValueError):
                        downloader.fetch_hwp(url)
                self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_manifest_is_last_and_binds_each_attachment_to_source_and_pending_review(self):
        written = []
        original_write = downloader.corpus.source_import.write_new

        def record_write(path, raw):
            written.append(Path(path).relative_to(self.project).as_posix())
            return original_write(path, raw)

        with patch.object(downloader.corpus.source_import, "write_new", side_effect=record_write):
            self.collect_all()
        self.assertEqual(len(written), 25)
        self.assertEqual(written[-1], downloader.MANIFEST)
        inputs, items, records = self.assert_complete()
        manifest_raw = (self.project / downloader.MANIFEST).read_bytes()
        self.assertEqual(manifest_raw, downloader.collection_bytes(inputs, items, records))
        manifest = json.loads(manifest_raw)
        self.assertEqual(manifest["input_files"], inputs)
        self.assertEqual(manifest["attachment_count"], 14)
        self.assertTrue(manifest["raw_collection_complete"])
        self.assertEqual(manifest["source_review"], "pending")
        self.assertEqual(manifest["document_content_review"], "pending")
        self.assertEqual(manifest["article_linking"], "pending")
        self.assertFalse(manifest["production_ready"])
        for item, saved in zip(items, manifest["attachments"]):
            self.assertEqual(saved["source"], item["source"])
            self.assertEqual(saved["source_location"], item["source_location"])
        bike = next(item for item in manifest["attachments"] if item["ordin_seq"] == "2171767")
        self.assertEqual(bike["display_label"], "별표 (번호 없음)")

    def test_parent_changed_during_fetch_prevents_completion_manifest(self):
        changed = False

        def change_parent(url):
            nonlocal changed
            if not changed:
                xml = self.project / "data/raw/2171803.xml"
                xml.write_bytes(xml.read_bytes() + b"\n")
                changed = True
            return self.fake_fetch(url)

        with patch.object(downloader, "fetch_hwp", side_effect=change_parent):
            with self.assertRaises(ValueError):
                downloader.collect(self.project)
        self.assertFalse((self.project / downloader.MANIFEST).exists())
        self.assertEqual(self.before_existing, self.existing_snapshot())

    def test_tampered_completion_manifest_is_not_overwritten_or_redownloaded(self):
        self.collect_all()
        path = self.project / downloader.MANIFEST
        changed = json.loads(path.read_bytes())
        changed["attachments"][-1]["content_hash"] = "0" * 64
        path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        before = self.attachment_snapshot()
        with patch.object(downloader, "fetch_hwp") as fetch:
            with self.assertRaises(ValueError):
                downloader.collect(self.project)
        fetch.assert_not_called()
        self.assertEqual(before, self.attachment_snapshot())

    def test_url_normalization_preserves_file_identity_and_rejects_other_hosts(self):
        url = self.items[0]["attachment"]["download_url"]
        normalized = downloader.normalize_url(url)
        self.assertEqual(normalized, url.replace("http://", "https://", 1))
        self.assertEqual(downloader.normalize_url(normalized), normalized)
        for bad in (
            "https://example.com/flDownload.do?flSeq=1",
            "https://www.law.go.kr.evil.example/flDownload.do?flSeq=1",
            "https://www.law.go.kr@evil.example/flDownload.do?flSeq=1",
            "file:///C:/secret.hwp",
            "https://www.law.go.kr/ordinInfoP.do?ordinSeq=2171833",
        ):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    downloader.normalize_url(bad)


if __name__ == "__main__":
    unittest.main()
