"""Local-only import regression tests using six synthetic XML fixtures.

The real project and the supplied starter kit are never read or changed here.
"""

import contextlib
import copy
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import import_starter_sources as importer


class SourceImportTests(unittest.TestCase):
    def setUp(self):
        self.temp_parent = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="ordinance-import-test-", dir=self.temp_parent)
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.cleanup_temp)
        self.source = self.root / "provided" / "raw"
        self.project = self.root / "project"
        self.raw_dir = self.project / "data" / "raw"
        self.source.mkdir(parents=True)
        self.project.mkdir()
        self.original_scope = tuple(importer.SCOPE)
        self.originals = {}
        fixture_scope = []
        for record in self.original_scope:
            raw = self.make_xml(record)
            self.source_path(record[2]).write_bytes(raw)
            self.originals[record[2]] = raw
            fixture_scope.append((*record[:4], importer.sha256(raw)))
        scope_patch = patch.object(importer, "SCOPE", tuple(fixture_scope))
        scope_patch.start()
        self.addCleanup(scope_patch.stop)

    def cleanup_temp(self):
        if not self.root.resolve().is_relative_to(self.temp_parent):
            raise ValueError("시험 임시 폴더가 지정한 범위를 벗어났습니다.")
        if not self.root.name.startswith("ordinance-import-test-"):
            raise ValueError("시험 임시 폴더 이름이 잘못되었습니다.")
        self.temporary.cleanup()

    @staticmethod
    def make_xml(record):
        name, ordin_id, seq, date, _ = record
        root = ET.Element("자치법규")
        info = ET.SubElement(root, "자치법규기본정보")
        for key, value in {
            "자치법규명": name,
            "지자체기관명": "서울특별시",
            "자치법규ID": ordin_id,
            "자치법규일련번호": seq,
            "자치법규종류": "C0001",
            "시행일자": date.replace("-", ""),
            "공포일자": date.replace("-", ""),
        }.items():
            ET.SubElement(info, key).text = value
        articles = ET.SubElement(root, "조문")
        for number, text in (("000100", "제1조(목적) 시험 원문"), ("000200", "제2조 삭제")):
            article = ET.SubElement(articles, "조")
            ET.SubElement(article, "조문여부").text = "Y"
            ET.SubElement(article, "조문번호").text = number
            ET.SubElement(article, "조내용").text = text
        supplement = ET.SubElement(root, "부칙")
        ET.SubElement(supplement, "부칙내용").text = "이 조례는 공포한 날부터 시행한다."
        if seq == "2171833":
            # Number 0000 and numbering gaps are real source possibilities.
            for number, kind in (("0000", "별표"), ("0001", "별표"), ("0003", "별표"), ("0005", "별표"), ("0006", "별도")):
                annex = ET.SubElement(root, "별표단위", {"별표키": f"fixture-{number}"})
                ET.SubElement(annex, "별표구분").text = kind
                ET.SubElement(annex, "별표번호").text = number
                ET.SubElement(annex, "별표제목").text = f"원문 제목 {number}"
        ET.indent(root, space="  ")
        # Preserve declaration, non-ASCII bytes, CRLF, and final whitespace.
        return ET.tostring(root, encoding="utf-8", xml_declaration=True).replace(b"\n", b"\r\n") + b"\r\n  "

    def source_path(self, seq):
        return self.source / f"{seq}.xml"

    def quiet_run(self, check_only=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return importer.run(self.source, self.project, check_only=check_only)

    def snapshot(self):
        return {
            str(path.relative_to(self.project)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.project.rglob("*") if path.is_file()
        }

    def set_source_xml(self, seq, root, repin=True):
        raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        self.source_path(seq).write_bytes(raw)
        if repin:
            importer.SCOPE = tuple(
                (*record[:4], importer.sha256(raw)) if record[2] == seq else record
                for record in importer.SCOPE
            )

    def restore_source(self, seq):
        raw = self.originals[seq]
        self.source_path(seq).write_bytes(raw)
        importer.SCOPE = tuple(
            (*record[:4], importer.sha256(raw)) if record[2] == seq else record
            for record in importer.SCOPE
        )

    def seed_existing(self, seq):
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        record = next(item for item in importer.SCOPE if item[2] == seq)
        raw = self.source_path(seq).read_bytes()
        core, _ = importer.inspect_xml(raw, record)
        metadata = {**core, "fetched_at": "2026-10-30T11:30:32+00:00", "original_note": "보존"}
        metadata_raw = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\r\n"
        target = self.raw_dir / f"{seq}.xml"
        metadata_path = target.with_suffix(".metadata.json")
        target.write_bytes(raw)
        metadata_path.write_bytes(metadata_raw)
        return target, metadata_path

    def assert_rejected_before_writes(self, message):
        before = self.snapshot()
        with patch.object(importer, "write_new", wraps=importer.write_new) as writer:
            with self.assertRaisesRegex(ValueError, message):
                self.quiet_run()
            writer.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_check_only_leaves_destination_untouched(self):
        with patch.object(importer, "write_new") as writer:
            manifest = self.quiet_run(check_only=True)
        writer.assert_not_called()
        self.assertEqual(manifest["ordinance_count"], 6)
        self.assertEqual(self.snapshot(), {})
        self.assertFalse(self.raw_dir.exists())

    def test_wrong_identity_is_rejected_even_with_matching_fixture_hash(self):
        seq = importer.SCOPE[-1][2]
        for field, wrong in (
            ("자치법규명", "서울특별시 강남구 다른 조례"),
            ("지자체기관명", "서울특별시 강남구"),
            ("자치법규ID", "wrong-id"),
            ("자치법규일련번호", "9999999"),
            ("자치법규종류", "not-an-ordinance"),
        ):
            with self.subTest(field=field):
                root = ET.fromstring(self.originals[seq])
                root.find(f"자치법규기본정보/{field}").text = wrong
                self.set_source_xml(seq, root)
                self.assert_rejected_before_writes(field)
                self.restore_source(seq)

    def test_after_cutoff_malformed_and_unexpected_dates_are_rejected(self):
        seq = importer.SCOPE[-1][2]
        for field in ("시행일자", "공포일자"):
            for value, message in (
                ("20261002", "기준일 이후"),
                ("2026-10-01", "잘못된 XML 날짜"),
                ("20260930", "검토한 시행일/공포일"),
            ):
                with self.subTest(field=field, value=value):
                    root = ET.fromstring(self.originals[seq])
                    root.find(f"자치법규기본정보/{field}").text = value
                    self.set_source_xml(seq, root)
                    self.assert_rejected_before_writes(message)
                    self.restore_source(seq)

    def test_duplicate_basic_information_or_required_field_is_rejected(self):
        seq = importer.SCOPE[-1][2]
        for field in (None, "자치법규명", "지자체기관명", "자치법규ID", "자치법규일련번호", "자치법규종류", "시행일자", "공포일자"):
            with self.subTest(field=field):
                root = ET.fromstring(self.originals[seq])
                info = root.find("자치법규기본정보")
                if field is None:
                    root.append(copy.deepcopy(info))
                else:
                    ET.SubElement(info, field).text = info.findtext(field)
                self.set_source_xml(seq, root)
                self.assert_rejected_before_writes("정확히 하나")
                self.restore_source(seq)

    def test_missing_or_unexpected_xml_is_rejected(self):
        seq = importer.SCOPE[-1][2]
        self.source_path(seq).unlink()
        self.assert_rejected_before_writes("목록 불일치.*누락")
        self.restore_source(seq)
        extra = self.source / "unexpected.XML"
        extra.write_bytes(self.originals[seq])
        self.assert_rejected_before_writes("목록 불일치.*예상 밖")
        extra.unlink()

    def test_source_bytes_drift_is_rejected_without_repinning(self):
        seq = importer.SCOPE[-1][2]
        self.source_path(seq).write_bytes(self.originals[seq] + b"\n")
        self.assert_rejected_before_writes("SHA-256")

    def test_import_preserves_bytes_dates_numbering_gaps_and_pending_status(self):
        manifest = self.quiet_run()
        self.assertEqual(manifest["ordinance_count"], 6)
        self.assertEqual(manifest["article_count_including_deleted"], 12)
        self.assertEqual(manifest["supplement_block_count"], 6)
        self.assertEqual(manifest["attachment_count"], 5)
        self.assertIs(manifest["production_ready"], False)
        self.assertEqual(manifest["as_of_version_status"], "pending")
        self.assertEqual(manifest["source_review"], "pending")
        for record in manifest["ordinances"]:
            seq = record["ordin_seq"]
            target = self.project / record["raw_file"]
            self.assertEqual(target.read_bytes(), self.originals[seq])
            metadata_raw = (self.project / record["metadata_file"]).read_bytes()
            metadata = json.loads(metadata_raw)
            expected = next(item for item in importer.SCOPE if item[2] == seq)
            self.assertEqual(metadata["effective_date"], expected[3])
            self.assertEqual(metadata["promulgation_date"], expected[3])
            self.assertEqual(metadata["as_of_date"], "2026-10-01")
            self.assertIsNone(metadata["fetched_at"])
            self.assertEqual(metadata["acquisition_method"], "local_starter_kit")
            self.assertEqual(metadata["as_of_version_status"], "pending")
            self.assertEqual(metadata["source_review"], "pending")
            self.assertEqual(record["metadata_content_hash"], importer.sha256(metadata_raw))
            self.assertEqual(record["content_hash"], importer.sha256(target.read_bytes()))
        parking = next(record for record in manifest["ordinances"] if record["ordin_seq"] == "2171833")
        numbers = [annex["attachment_no"] for annex in parking["attachments"]]
        self.assertEqual(numbers, ["0000", "0001", "0003", "0005", "0006"])
        self.assertNotIn("0004", numbers)
        self.assertEqual(parking["attachments"][-1]["attachment_type"], "별도")
        self.assertTrue(all(annex["attachment_branch_no"] == "00" for annex in parking["attachments"]))

    def test_existing_parking_xml_and_metadata_bytes_are_preserved(self):
        xml_path, metadata_path = self.seed_existing("2171833")
        old = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (xml_path, metadata_path)}
        with patch.object(importer, "write_new", wraps=importer.write_new) as writer:
            self.quiet_run()
        written_paths = [call.args[0] for call in writer.call_args_list]
        self.assertEqual(len(written_paths), 11)  # Five new pairs plus manifest.
        self.assertEqual(written_paths[-1], self.raw_dir / importer.MANIFEST_NAME)
        for path, before in old.items():
            self.assertNotIn(path, written_paths)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_late_destination_conflict_stops_before_any_other_write(self):
        seq = importer.SCOPE[-1][2]
        target, metadata_path = self.seed_existing(seq)
        good_xml, good_metadata = target.read_bytes(), metadata_path.read_bytes()
        target.write_bytes(good_xml + b"\n")
        self.assert_rejected_before_writes("기존 XML")
        target.write_bytes(good_xml)
        metadata = json.loads(good_metadata)
        metadata["content_hash"] = "0" * 64
        metadata_path.write_bytes(importer.json_bytes(metadata))
        self.assert_rejected_before_writes("기존 metadata")

    def test_incomplete_xml_metadata_pair_is_rejected(self):
        seq = importer.SCOPE[-1][2]
        target, metadata_path = self.seed_existing(seq)
        saved_metadata = metadata_path.read_bytes()
        metadata_path.unlink()
        self.assert_rejected_before_writes("XML/metadata 중 하나만")
        target.unlink()
        metadata_path.write_bytes(saved_metadata)
        self.assert_rejected_before_writes("XML/metadata 중 하나만")

    def test_rerun_writes_zero_files_and_preserves_manifest(self):
        first = self.quiet_run()
        before = self.snapshot()
        with patch.object(importer, "write_new", wraps=importer.write_new) as writer:
            second = self.quiet_run()
        writer.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(before, self.snapshot())
        manifest, pending, reused = importer.prepare(self.source, self.project)
        self.assertEqual(pending, [])
        self.assertEqual(reused, 6)
        self.assertEqual(manifest, first)

    def seed_version_review(self):
        manifest = self.quiet_run()
        check_dir = self.raw_dir / "version_checks" / importer.AS_OF_DATE
        check_dir.mkdir(parents=True)
        evidence = b'{"fixture": true}\n'
        (check_dir / "version_evidence.json").write_bytes(evidence)
        status = "verified_official_catalog"
        report = {
            "status": status, "as_of_date": importer.AS_OF_DATE,
            "source_review": "pending", "production_ready": False,
            "evidence_file": "version_evidence.json",
            "evidence_content_hash": importer.sha256(evidence),
            "ordinances": copy.deepcopy(manifest["ordinances"]),
        }
        for record in report["ordinances"]:
            record["as_of_version_status"] = status
        report_path = check_dir / "version_report.json"
        report_raw = importer.json_bytes(report)
        report_path.write_bytes(report_raw)
        manifest["as_of_version_status"] = status
        manifest["review_note"] = importer.VERIFIED_NOTE
        manifest["as_of_verification"] = {
            "report_file": report_path.relative_to(self.project).as_posix(),
            "report_content_hash": importer.sha256(report_raw),
        }
        for record in manifest["ordinances"]:
            record["as_of_version_status"] = status
        (self.raw_dir / importer.MANIFEST_NAME).write_bytes(importer.json_bytes(manifest))
        return manifest, report, report_path

    def test_verified_review_survives_rerun_without_writes(self):
        first, _, _ = self.seed_version_review()
        before = self.snapshot()
        with patch.object(importer, "write_new", wraps=importer.write_new) as writer:
            second = self.quiet_run()
        writer.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(before, self.snapshot())

    def test_changed_report_is_rejected(self):
        _, _, path = self.seed_version_review()
        path.write_bytes(path.read_bytes() + b" ")
        self.assert_rejected_before_writes("보고서의 해시")

    def test_changed_official_evidence_is_rejected(self):
        _, _, path = self.seed_version_review()
        evidence_path = path.parent / "version_evidence.json"
        evidence_path.write_bytes(b"changed")
        self.assert_rejected_before_writes("공식 조회 근거 파일의 해시")

    def test_report_for_different_source_is_rejected_even_if_report_hash_matches(self):
        manifest, report, path = self.seed_version_review()
        report["ordinances"][0]["content_hash"] = "0" * 64
        raw = importer.json_bytes(report)
        path.write_bytes(raw)
        manifest["as_of_verification"]["report_content_hash"] = importer.sha256(raw)
        (self.raw_dir / importer.MANIFEST_NAME).write_bytes(importer.json_bytes(manifest))
        self.assert_rejected_before_writes("현재 원본/metadata")

    def test_version_review_does_not_approve_source_or_production(self):
        manifest, _, _ = self.seed_version_review()
        for key, value in (("source_review", "approved"), ("production_ready", True)):
            with self.subTest(key=key):
                changed = copy.deepcopy(manifest)
                changed[key] = value
                (self.raw_dir / importer.MANIFEST_NAME).write_bytes(importer.json_bytes(changed))
                self.assert_rejected_before_writes("기존 원본 목록")

    def test_same_directory_and_exclusive_write_prevent_overwrite(self):
        self.raw_dir.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "폴더가 같을 수 없습니다"):
            importer.prepare(self.raw_dir, self.project)
        target = self.raw_dir / "preserve.xml"
        target.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            importer.write_new(target, b"replacement")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(sorted(path.name for path in self.raw_dir.iterdir()), ["preserve.xml"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
