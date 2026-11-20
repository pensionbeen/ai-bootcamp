import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from benefit_selection import benefit_evidence


def candidates():
    law = "서울특별시 문화회관 운영 조례"
    label = law + " 제17조"
    base = {"ordinance": law, "source_url": "https://example.invalid/culture",
            "effective_date": "2025-01-10", "owner_id": "unit"}

    def item(alias, number, text):
        return dict(base, id=alias, unit_id=f"unit::item::{number}", public_label=label + " 제1항",
                    locator=f"제17조 제1항 제{number}호", quote=f"{number}. " + text, required_ids=["stack"])

    rule = dict(base, id="stack", unit_id="unit::context::third", public_label=label + " 제3항",
                locator="제17조 제3항", required_ids=[],
                quote="③ 둘 이상의 시설이용료 감면사유에 해당하는 때에는 그 중 감면율이 높은 하나만 적용한다.")
    return [item("student", 3, "청소년은 시설이용료의 100분의 12.5를 감면한다."),
            item("other", 7, "예술인은 시설이용료의 19%를 할인할 수 있다."),
            item("card", 4, "「지역 문화카드」 소지자는 시설이용료의 48%를 감면한다."), rule]


QUERY = "청소년 할인과 지역 문화카드 할인을 공영문화회관에서 둘 다 받을 수 있나요?"


class BenefitSelectionTests(unittest.TestCase):
    def test_resolves_unselected_subjects_and_rule_in_source_order_independent_ways(self):
        rows = candidates()
        questions = [QUERY, "공영문화회관에서 청소년 할인과 지역 문화카드 할인은 중복 적용이 가능한가요?",
                     "청소년과 지역 문화카드 할인을 함께 받을 수 있을까요?",
                     "지역 문화카드 할인과 청소년 할인을 같이 적용되나요?"]
        for q in questions:
            for order in (rows, rows[::-1]):
                with self.subTest(q=q, order=order[0]["id"]):
                    result = benefit_evidence(q, order)
                    self.assertEqual(set(result["ids"]), {"student", "card", "stack"})
                    self.assertEqual(result["ids"], result["used_ids"])
                    self.assertIn("청소년 할인(12.5%)", result["answer"])
                    self.assertIn("지역 문화카드 할인(48%)", result["answer"])
                    self.assertIn("높은 하나만", result["answer"])
                    self.assertIn("두 감면 요건을 모두 충족", result["answer"])

    def test_missing_conflicting_or_indirect_rows_abstain(self):
        changes = [
            lambda rows: rows.pop(0), lambda rows: rows.pop(-1), lambda rows: rows[0].update(required_ids=[]),
            lambda rows: rows[0].update(effective_date="2026-01-01"),
            lambda rows: rows[-1].update(locator="제19조 제3항"),
            lambda rows: rows[0].update(quote="3. 청소년의 보호자에게 시설이용료의 12.5%를 할인한다."),
            lambda rows: rows[0].update(quote="3. 청소년이 아닌 사람은 시설이용료의 12.5%를 감면한다."),
            lambda rows: rows[0].update(quote=rows[0]["quote"].replace("시설이용료", "등록수수료")),
            lambda rows: rows.append(dict(rows[0], id="other-tier", unit_id="unit::item::6")),
            lambda rows: rows[-1].update(quote=rows[-1]["quote"] + " 다만 별도 조항에 따르는 경우는 제외한다."),
            lambda rows: rows[2].update(quote=rows[2]["quote"] + " 다만 청소년은 제외한다."),
            lambda rows: rows[0].update(quote=rows[0]["quote"] + " 다만 주말에는 시설이용료의 37%를 감면한다."),
            lambda rows: rows.append(dict(rows[-1], id="other-rule", quote="③ 두 감면은 중복 적용한다.")),
        ]
        for change in changes:
            rows = candidates()
            change(rows)
            self.assertIsNone(benefit_evidence(QUERY, rows), rows)
        for question in [QUERY.replace("공영문화회관", "공영체육관"), QUERY.replace("둘 다", "야간에 둘 다"),
                         QUERY + " 신청 서류도 알려주세요.", "청소년이 아니어도 카드 할인을 받을 수 있나요?"]:
            self.assertIsNone(benefit_evidence(question, candidates()), question)

    def test_single_direct_row_keeps_all_its_exceptions_and_excludes_unrelated_rows(self):
        rows = candidates()
        rows[1]["quote"] += " 다만 주말에는 이용료를 면제한다."
        original = copy.deepcopy(rows)
        result = benefit_evidence("공영문화회관에서 예술인 시설이용료 할인되나요?", rows)
        self.assertEqual(result, {"kind": "single_benefit", "ids": ["other"], "required_ids": ["other"],
                                  "subject": "예술인", "source_subject": "예술인", "answer": None, "used_ids": []})
        self.assertEqual(rows, original)
        for change in [lambda r: r[1].update(quote=r[1]["quote"].replace("예술인은", "예술인의 보호자는")),
                       lambda r: r[1].update(quote=r[1]["quote"] + "\n 가. 등록 서류를 제시한 경우"),
                       lambda r: r.append(dict(r[1], id="more", unit_id="unit::item::8"))]:
            changed = copy.deepcopy(rows)
            change(changed)
            self.assertIsNone(benefit_evidence("예술인 시설이용료 할인되나요?", changed))


if __name__ == "__main__":
    unittest.main()
