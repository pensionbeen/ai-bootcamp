from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tariff_selection import tariff_evidence


Q = "공영주차장의 야간 주차요금은 얼마인가요?"
LAW = "도시 교통시설 운영 조례"
ANNEX = LAW + " 별표 12"


def sample():
    """Two non-contiguous, explicitly named tiers and shuffled table rows."""
    heading = ("■ 도시 교통시설 운영 조례 [별표 12]\n공영주차장 요금표(제17조제2항 관련)\n"
               "(단위 : 천원/대)\n가. 2급지: 동쪽\n나. 4급지: 서쪽")
    rows = []
    for tier, daily, monthly in [(4, 8, 39), (2, 11, 87)]:
        owner = f"table-row:{tier}"
        data, note = f"data-{tier}", f"title-{tier}"
        common = {"owner_id": owner, "ordinance": LAW, "source_url": "https://example.invalid/city",
                  "effective_date": "2024-11-15", "public_label": ANNEX}
        rows.extend([
            {**common, "id": data, "unit_id": owner, "locator": f"별표 12 — 2쪽 행 {tier}",
             "quote": f"구분: {tier}급지\n노 상 주 차 장 > 1일주차권(야간에한함): {daily}\n"
                      f"노외주차장 > 월 정기권 > 야 간: {monthly}\n노외주차장 > 월 정기권 > 전 일: 101",
             "required_ids": [note]},
            {**common, "id": note, "unit_id": owner + "::caption", "locator": "별표 12 — 2쪽",
             "quote": heading, "required_ids": []},
        ])
    rows.append({**common, "id": "ordinance", "unit_id": "law:17.2", "owner_id": "law:17.2",
                 "public_label": LAW + " 제17조 제2항", "locator": "제17조 제2항",
                 "quote": "시장이 설치한 공영주차장의 주차요금은 별표 12와 같다.",
                 "required_ids": []})
    return rows


class TariffSelectionTests(unittest.TestCase):
    def test_all_declared_grades_and_both_products_have_source_bound_amounts(self):
        rows = sample()
        untouched = deepcopy(rows)
        result = tariff_evidence(Q, rows)
        self.assertEqual(result["data_ids"], ["data-2", "data-4"])
        self.assertEqual(result["used_ids"], ["data-2", "data-4", "title-2", "ordinance"])
        self.assertEqual([(c["tier"], c["ticket"], c["won"]) for c in result["cells"]], [
            (2, "1일주차권", 11000), (4, "1일주차권", 8000),
            (2, "월정기권", 87000), (4, "월정기권", 39000)])
        self.assertIn("1대당", result["answer"])
        self.assertEqual(rows, untouched)

    def test_explicit_grade_facility_or_ticket_only_select_matching_cells(self):
        rows = sample()
        for question, wanted in [
            ("2급지 공영노외주차장 월정기권 야간 요금은 얼마예요?", [87000]),
            ("서울시 공영주차장 4급지 야간 1일권 가격을 알려주세요", [8000]),
            ("공영 노외주차장 전일 월 정기권은 얼마입니까?", [101000, 101000]),
        ]:
            with self.subTest(question=question):
                self.assertEqual([c["won"] for c in tariff_evidence(question, rows)["cells"]], wanted)

    def test_complete_inventory_required_for_an_unqualified_request(self):
        rows = sample()
        self.assertIsNone(tariff_evidence(Q, rows[2:]))
        self.assertEqual(tariff_evidence("2급지 공영주차장 야간 요금은 얼마?", rows[2:])["data_ids"], ["data-2"])
        self.assertIsNone(tariff_evidence("공영주차장 3급지 야간 요금이 얼마?", rows))
        for bad in ("3~4", "2,5", "무료", "₩3", "30%"):
            with self.subTest(value=bad):
                changed = deepcopy(rows)
                changed[0]["quote"] = changed[0]["quote"].replace("야간에한함): 8", "야간에한함): " + bad)
                self.assertIsNone(tariff_evidence(Q, changed))

    def test_source_identity_units_exceptions_and_distinct_rows(self):
        conflict = sample()
        conflict[0]["effective_date"] = "2019-07-08"
        self.assertIsNone(tariff_evidence(Q, conflict))
        for old, new in [("천원/대", "달러/대"), ("12]", "3]"), ("제17조", "제14조"),
                         ("나. 4급지: 서쪽", "나. 4급지: 서쪽\n야간에는 요금을 40% 할인한다.")]:
            with self.subTest(value=new):
                rows = sample()
                for r in rows[1:4:2]:
                    r["quote"] = r["quote"].replace(old, new)
                self.assertIsNone(tariff_evidence(Q, rows))
        for change in (
            lambda r: r[-1].update(quote=r[-1]["quote"] + " 다만, 휴일에는 야간 요금을 받지 않는다."),
            lambda r: r[1].update(owner_id="another-row"),
            lambda r: r[2].update(required_ids=["title-4"]),
            lambda r: r.append({**r[0], "id": "other", "quote": r[0]["quote"].replace(": 8", ": 3")}),
            lambda r: r.append({**r[0], "id": "special", "quote": r[0]["quote"].replace("4급지", "4급지(휴일)")}),
        ):
            rows = sample()
            change(rows)
            self.assertIsNone(tariff_evidence(Q, rows))
        rows = sample()
        rows.append({**rows[0], "id": "alias"})
        self.assertEqual(tariff_evidence(Q, rows)["data_ids"], ["data-2", "data-4"])

    def test_reject_specific_location_eligibility_multiple_facts_and_unknown_period(self):
        for question in ["공영주차장 장애인 야간 할인은 얼마인가요?", "강남역 공영주차장 야간 요금은 얼마인가요?",
                         "사설주차장 야간 요금은 얼마인가요?", "공영주차장 요금은 얼마인가요?",
                         "공영주차장 야간에 월정기권이 항상 발급되나요?", "공영주차장 2급지 주간 요금은 얼마인가요?",
                         "공영주차장 평일 야간 주차요금은 얼마인가요?", "공영주차장 전일 야간 요금이 얼마인가요?"]:
            with self.subTest(question=question):
                self.assertIsNone(tariff_evidence(question, sample()))
