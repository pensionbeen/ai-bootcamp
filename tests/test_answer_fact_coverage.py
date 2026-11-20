import unittest

from test_complete_selected_evidence import COMPARE, PRICE, benefits, fees
from answer_fact_coverage import missing_material_evidence


class AnswerFactCoverageTests(unittest.TestCase):
    def compare(self, answer, used=("card", "artist"), *, question=COMPARE, rows=None):
        return missing_material_evidence(question, benefits() if rows is None else rows, ["compare"], answer, used)

    def fee(self, answer, used=("table",), *, question=PRICE, rows=None, selected=("article", "table")):
        return missing_material_evidence(question, fees() if rows is None else rows, selected, answer, used)

    def test_both_named_rates_in_any_order_and_fraction_or_percent(self):
        for answer in ("예술인은 100분의 37, 지역 문화카드는 42%입니다.",
                       "지역 문화카드는 42퍼센트이고 예술인 할인은 37%입니다.",
                       "예술인과 지역 문화카드는 각각 37%, 42%입니다.",
                       "42%인 지역 문화카드 할인과 37%인 예술인 할인 중 하나를 받습니다."):
            with self.subTest(answer=answer):
                self.assertEqual(self.compare(answer), [])

    def test_swapped_percentages_opposite_subject_or_negated_rate_are_detected(self):
        swapped = self.compare("예술인은 42%, 지역 문화카드는 37%입니다.")
        self.assertEqual(len(swapped), 2)
        self.assertEqual(len(self.compare("예술인과 지역 문화카드는 각각 42%, 37%입니다.")), 2)
        for answer in ("비예술인은 37%, 지역 문화카드는 42%입니다.",
                       "예술인은 37%가 아닙니다. 지역 문화카드는 42%입니다.",
                       "지역 문화카드는 42%입니다. 별도 제도의 지원율은 37%입니다."):
            with self.subTest(answer=answer):
                result = self.compare(answer)
                self.assertEqual(len(result), 1)
                self.assertIn("artist", result[0])

    def test_supporting_id_is_required_even_with_correct_numbers(self):
        result = self.compare("지역 문화카드 할인은 42%, 예술인은 37%입니다.", used=["compare", "artist"])
        self.assertEqual(len(result), 1)
        self.assertIn("card", result[0])

    def test_discount_questions_with_unspecified_or_conflicting_targets_are_out_of_scope(self):
        for q in ("예술인 할인과 지역 문화카드 할인을 카드 없이 함께 받을 수 있나요?",
                  "예술인 할인은 신청 서류가 무엇인가요?", "예술인 할인이 있나요?"):
            self.assertEqual(self.compare("확인 불가", [], question=q), [])
        rows = benefits()
        rows.append({**rows[0], "id": "duplicate"})
        self.assertEqual(self.compare("확인 불가", [], rows=rows), [])

    def test_fee_cells_bind_each_time_period_and_scale_to_its_amount(self):
        for answer in ("시간당 7,000원, 월 정기권은 31,000원입니다.",
                       "월 정기권 3만 1천원, 시간당 7천원입니다."):
            with self.subTest(answer=answer):
                self.assertEqual(self.fee(answer), [])
        for answer in ("시간당 31,000원, 월 정기권 7,000원입니다.",
                       "시간당 7원, 월 정기권 31원입니다.",
                       "시간당 7, 월 정기권은 31입니다."):
            with self.subTest(answer=answer):
                self.assertEqual(len(self.fee(answer)), 2)
        self.assertEqual(len(self.fee("시간당 7천원, 월 정기권은 별도입니다.")), 1)

    def test_table_data_row_requires_citation_while_its_unit_can_be_a_dependency(self):
        result = self.fee("시간당 7천원, 월 정기권은 31,000원입니다.", used=["currency", "article"])
        self.assertEqual(len(result), 1)
        self.assertIn("데이터 행", result[0])
        self.assertTrue(self.fee("원칙적으로 무료입니다.", used=["article"], selected=["title", "article"]))

    def test_ambiguous_tariffs_or_external_issues_do_not_signal_missing_facts(self):
        rows = fees()
        rows.append({**rows[0], "id": "other-zone", "owner_id": "other", "unit_id": "other"})
        self.assertEqual(self.fee("확인 불가", [], rows=rows), [])
        self.assertEqual(self.fee("확인 불가", [], question="공유연습실 반려동물 이용료는 얼마인가요?"), [])
        self.assertEqual(self.fee("확인 불가", [], selected=[]), [])


if __name__ == "__main__":
    unittest.main()
