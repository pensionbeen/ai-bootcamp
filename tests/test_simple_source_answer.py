import copy
import unittest

from simple_source_answer import simple_source_answer
from test_complete_selected_evidence import ANNEX, LAW, row


FACILITY = "저는 72세인데 역사공원 전시관 입장료를 면제받을 수 있나요?"
FINE = "서울시가 지정한 금주구역에서 술을 마시면 과태료가 얼마인가요?"
APPEAL = FINE.replace("얼마인가요?", "얼마이고, 이의제기 절차는 어느 법을 따르나요?")


def ages():
    label = LAW + " 제14조 제3항"
    return [
        row("waiver", "③ 구청장은 다음 각 호의 사람에 대하여 입장료를 면제한다.", label=label, unit="body::intro", owner="body"),
        row("age", "5. 8세 미만 또는 70세 이상인 사람", unit="body::item::5", owner="body", label=label,
            locator="제14조 제3항 제5호", required_ids=["waiver"]),
        row("venue", "시설별: 역사공원\n종별: 전시관 (작품관 포함)\n요금: 800원", owner="table", label=ANNEX, required_ids=["header"]),
        row("header", "전시시설 입장료(제3조 관련)", owner="table", unit="table::heading", label=ANNEX),
    ]


def fines():
    return [
        row("fine", "③ 구청장은 제9조제2항을 위반하여 금주구역에서 음주를 한 사람에게는 35만원의 과태료를 부과할 수 있다.",
            label=LAW + " 제28조 제3항", owner="fine"),
        row("appeal", "④ 제3항에 따른 과태료의 부과·징수 및 이의제기 절차는 「행정심판법」에 따른다.",
            label=LAW + " 제28조 제4항", owner="appeal"),
    ]


class AgeAdmissionTests(unittest.TestCase):
    def test_conditions_and_modality_are_read_from_the_matched_item_and_intro(self):
        rows = ages()
        result = simple_source_answer(FACILITY, rows)
        self.assertEqual(result["used_ids"], ["waiver", "age"])
        self.assertEqual(result["answer"], "네. 70세 이상인 사람은 입장료를 면제받습니다.")
        for q in ["역사공원 전시관에서 저는 만 72세인데 입장료 면제가 되나요?",
                  "역사공원 전시관 입장료는 만 72세도 면제받을 수 있을까요?"]:
            self.assertEqual(simple_source_answer(q, rows), result)
        rows[0]["quote"] = rows[0]["quote"].replace("면제한다", "면제할 수 있다")
        for age in (3, 7, 70):
            answer = simple_source_answer(FACILITY.replace("72세", f"{age}세"), rows)["answer"]
            self.assertIn("8세 미만" if age < 8 else "70세 이상", answer)
            self.assertIn("면제받을 수 있습니다", answer)
        self.assertIsNone(simple_source_answer(FACILITY.replace("72세", "8세"), rows))
        self.assertIsNone(simple_source_answer(FACILITY.replace("72세", "69세"), rows))

    def test_defer_when_a_qualifier_or_facility_provenance_is_missing(self):
        for index, changes in [
            (1, {"quote": "5. 70세 이상인 사람으로서 지역에 거주하는 사람"}),
            (1, {"quote": "5. 70세 이상인 사람. 다만 주말은 제외한다."}),
            (1, {"required_ids": []}), (1, {"owner_id": "other"}), (3, {"quote": "시설 이용료(제3조 관련)"}),
            (3, {"effective_date": "2024-02-03"}), (3, {"owner_id": "other"}),
            (2, {"quote": "시설별: 역사공원\n종별: 전시관 (70세 이상 제외)\n요금: 800원"}),
        ]:
            with self.subTest(changes=changes):
                rows = ages()
                rows[index].update(changes)
                self.assertIsNone(simple_source_answer(FACILITY, rows))
        for q in [FACILITY.replace("역사공원", "소나무공원"), FACILITY.replace("전시관", "전시관과 수영장"),
                  FACILITY.replace("가능한가", "신청 가능한가") + " 필요한 서류도 알려주세요."]:
            self.assertIsNone(simple_source_answer(q, ages()))
        rows = ages() + [row("condition", "제3항에 따른 면제를 받으려면 주민증을 제시해야 한다.", label=LAW + " 제14조 제5항")]
        self.assertIsNone(simple_source_answer(FACILITY, rows))


class AreaFineTests(unittest.TestCase):
    def test_fine_only_and_appeal_use_different_provisions(self):
        result = simple_source_answer(FINE, fines())
        self.assertEqual(result["used_ids"], ["fine"])
        self.assertEqual(result["answer"], "금주구역에서 음주하면 35만원의 과태료가 부과될 수 있습니다.")
        result = simple_source_answer(APPEAL, fines())
        self.assertEqual(result["used_ids"], ["fine", "appeal"])
        self.assertIn("「행정심판법」", result["answer"])

    def test_reject_inapplicable_or_incomplete_penalty(self):
        for q in [FINE.replace("술을 마시", "담배를 피우"), FINE.replace("금주", "금연"), FINE.replace("서울시", "부산시"),
                  FINE.replace("과태료", "벌금"), FINE + " 감면 신청 기간도 궁금합니다."]:
            self.assertIsNone(simple_source_answer(q, fines()))
        rows = fines()
        for extra in ["다만 주말에는 2만원으로 한다.", "단, 계도 기간 중에는 적용하지 않는다."]:
            changed = copy.deepcopy(rows)
            changed[0]["quote"] += " " + extra
            self.assertIsNone(simple_source_answer(FINE, changed))
        duplicate = {**rows[0], "id": "conflict", "quote": rows[0]["quote"].replace("35만원", "5만원")}
        self.assertIsNone(simple_source_answer(FINE, rows + [duplicate]))

    def test_appeal_must_refer_to_this_penalty_at_its_verified_legal_address(self):
        for change in [{"locator": "제24조 제4항"}, {"effective_date": "2023-06-05"},
                       {"quote": fines()[1]["quote"].replace("제3항", "제1항")}]:
            rows = fines()
            rows[1].update(change)
            self.assertIsNone(simple_source_answer(APPEAL, rows))
            self.assertIsNotNone(simple_source_answer(FINE, rows))


if __name__ == "__main__":
    unittest.main()
