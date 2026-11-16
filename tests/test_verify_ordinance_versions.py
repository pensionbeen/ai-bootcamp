"""저장한 공식 응답으로 오프라인 분기 검사. API와 원본 파일은 변경하지 않음."""

from project_paths import PROJECT_ROOT
import copy
import json
import unittest
from pathlib import Path
import verify_ordinance_versions as verifier


@unittest.skipUnless((PROJECT_ROOT / "data/raw/version_checks/2026-10-01/manifest.before.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/raw/version_checks/2026-10-01/manifest.before.json")
class VersionChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = PROJECT_ROOT
        saved = base / "data/raw/version_checks/2026-10-01"
        cls.manifest = json.loads((saved / "manifest.before.json").read_text(encoding="utf-8"))
        cls.evidence = json.loads((saved / "version_evidence.json").read_text(encoding="utf-8"))

    def setUp(self):
        self.record = copy.deepcopy(self.manifest["ordinances"][0])
        self.entry = copy.deepcopy(self.evidence["ordinances"][0])

    def check(self):
        return verifier.check_record(self.record, self.entry, "2026-10-01")

    def change_selected(self, field, value):
        for catalog in self.entry["catalogs"]:
            lists = [catalog["matched_id_records"]] + [p["records"] for p in catalog["pages"]]
            for rows in lists:
                for row in rows:
                    if row["자치법규일련번호"] == self.record["ordin_seq"]:
                        row[field] = value

    def add_history(self, seq, effective, promulgation):
        catalog = next(c for c in self.entry["catalogs"] if c["nw"] == 2)
        row = copy.deepcopy(next(r for r in catalog["matched_id_records"] if r["자치법규일련번호"] == self.record["ordin_seq"]))
        row.update(자치법규일련번호=seq, 시행일자=effective, 공포일자=promulgation)
        catalog["pages"][-1]["records"].append(copy.deepcopy(row))
        catalog["matched_id_records"].append(copy.deepcopy(row))
        catalog["total_count"] += 1
        for page in catalog["pages"]:
            page["total_count"] = catalog["total_count"]

    def test_actual_six_records_match(self):
        results = [verifier.check_record(r, e, "2026-10-01") for r, e in zip(self.manifest["ordinances"], self.evidence["ordinances"])]
        self.assertEqual(sum(r["unique_catalog_versions"] for r in results), 264)
        self.assertTrue(all(r["as_of_version_status"] == verifier.STATUS for r in results))

    def test_missing_history_is_rejected(self):
        self.entry["catalogs"] = self.entry["catalogs"][:1]
        with self.assertRaisesRegex(ValueError, "현행·연혁"):
            self.check()

    def test_missing_last_page_is_rejected(self):
        self.entry["catalogs"][1]["pages"].pop()
        with self.assertRaisesRegex(ValueError, "마지막 페이지"):
            self.check()

    def test_changed_query_filter_is_rejected(self):
        self.entry["catalogs"][1]["pages"][0]["request_parameters"]["rrClsCd"] = "300202"
        with self.assertRaisesRegex(ValueError, "요청 조건"):
            self.check()

    def test_filtered_matches_cannot_hide_records(self):
        self.entry["catalogs"][1]["matched_id_records"].pop()
        with self.assertRaisesRegex(ValueError, "다시 고른 목록"):
            self.check()

    def test_wrong_organization_is_rejected(self):
        self.change_selected("지자체기관명", "서울특별시 강남구")
        with self.assertRaisesRegex(ValueError, "기관/종류"):
            self.check()

    def test_invalid_date_is_rejected(self):
        self.change_selected("시행일자", "20261301")
        with self.assertRaises(ValueError):
            self.check()

    def test_current_body_changed_is_rejected(self):
        self.entry["current_id_body"]["response_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "바이트"):
            self.check()

    def test_changed_selected_id_is_rejected(self):
        self.entry["ordin_id"] = "999999"
        with self.assertRaisesRegex(ValueError, "ID/판본"):
            self.check()

    def test_repeal_is_not_filtered_away(self):
        self.change_selected("제개정구분명", "타법폐지")
        with self.assertRaisesRegex(ValueError, "폐지"):
            self.check()

    def test_same_effective_day_is_not_resolved_by_largest_seq(self):
        self.add_history("9999999", "20261001", "20261001")
        with self.assertRaisesRegex(ValueError, "복수 판본"):
            self.check()

    def test_newer_eligible_version_is_rejected(self):
        self.record = copy.deepcopy(self.manifest["ordinances"][4])
        self.entry = copy.deepcopy(self.evidence["ordinances"][4])
        self.add_history("9999999", "20260901", "20260901")
        with self.assertRaisesRegex(ValueError, "기준일 후보"):
            self.check()

    def test_future_effective_version_is_not_selected(self):
        self.add_history("9999999", "20270101", "20261001")
        self.assertEqual(self.check()["ordin_seq"], self.record["ordin_seq"])

    def test_historical_name_changes_are_preserved_by_id(self):
        result = self.check()
        self.assertIn("서울특별시주차장설치및관리조례", result["observed_historical_titles"])
        self.assertEqual(result["as_of_version_status"], verifier.STATUS)


if __name__ == "__main__":
    unittest.main()
