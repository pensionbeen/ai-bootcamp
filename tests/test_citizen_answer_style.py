import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from citizen_answer_style import readable_answer


LAW = "서울특별시 주차장 설치 및 관리 조례"
PARK = "서울특별시 도시공원 조례"
ARTICLE = LAW + " 제7조 제1항"
ANNEX = LAW + " 별표 1"


class CitizenAnswerStyleTests(unittest.TestCase):
    def test_exact_cited_source_moves_after_single_complete_sentence(self):
        original = LAW + " 제7조제1항에 따르면, 경차 요금의 100분의 50을 할인할 수 있습니다."
        expected = "경차 요금의 50%를 할인할 수 있습니다 (" + LAW + " 제7조제1항)."
        citations = [ARTICLE]
        self.assertEqual(readable_answer(original, citations), expected)
        self.assertEqual(readable_answer(expected, citations), expected)
        self.assertEqual(citations, [ARTICLE])

    def test_each_law_stays_with_its_own_single_statement(self):
        a = "「" + LAW + "」 별표 1"
        b = PARK + " 제20조 제1항"
        text = f"{a}에 따르면, 요금은 100원입니다.\r\n\r\n{b}에 따르면 면제 대상이 될 수 있습니다."
        self.assertEqual(readable_answer(text, [ANNEX, b]),
                         f"요금은 100원입니다 ({a}).\r\n\r\n면제 대상이 될 수 있습니다 ({b}).")

    def test_annex_with_connected_article_requires_both_published_locators(self):
        source = ANNEX + " 공영주차장 주차요금표(제6조제1항 관련)"
        text = source + "에 따르면, 5분당 요금은 500원입니다."
        self.assertEqual(readable_answer(text, [ANNEX, LAW + " 제6조 제1항"]),
                         "5분당 요금은 500원입니다 (" + source + ").")
        self.assertEqual(readable_answer(text, [ANNEX]), text)
        self.assertEqual(readable_answer(text, [ANNEX, PARK + " 제6조 제1항"]), text)

    def test_nested_source_introductions_preserve_their_order(self):
        original = ARTICLE + "에 따르면, " + ANNEX + "에 따르면, 요금을 정합니다."
        self.assertEqual(readable_answer(original, [ARTICLE, ANNEX]), original)

    def test_source_validation_preserves_detail_and_relative_references(self):
        intro = ARTICLE + "에 따르면, "
        for s in (
            LAW + " 제7조 제1항 제2호에 따르면, 할인받을 수 있습니다.",
            PARK + " 제7조 제1항에 따르면, 면제받을 수 있습니다.",
            "상위법 제7조 제1항에 따르면, 별도 기준을 확인해야 합니다.",
            LAW + " 부칙 제1조에 따르면, 시행일은 2025년 1월 1일입니다.",
            intro + "첫째 조건이 있습니다. 예외도 있습니다.",
            intro + "할인 대상입니다.\n\n같은 항 제3호의 단서도 적용됩니다.",
            "해당되는 경우에 한하여 " + intro + "할인을 받을 수 있습니다.",
            f'“인용 시작\n\n{intro}전액 면제됩니다.\n\n인용 끝”',
        ):
            with self.subTest(s=s):
                self.assertEqual(readable_answer(s, [ARTICLE]), s)

    def test_percents_preserve_roles_limits_and_modality(self):
        text = ("비용의 100분의 12.5를 지원할 수 있습니다. "
                "100분의 60 이상인 경우 지원하여야 합니다. "
                "부담률은 100분의 80으로 정합니다.")
        expected = ("비용의 12.5%를 지원할 수 있습니다. "
                    "60% 이상인 경우 지원하여야 합니다. "
                    "부담률은 80%로 정합니다.")
        self.assertEqual(readable_answer(text, []), expected)
        self.assertEqual(readable_answer(expected, []), expected)

    def test_quoted_numbers_other_denominators_formulas_and_ranges(self):
        for s in (
            "조문은 “전체의 100분의 50을 지원할 수 있다”입니다.",
            "별표의 ‘100분의 50을 감면’이라는 문언입니다.",
            "‘원문은 “100분의 80을”로 적었다’는 설명입니다.",
            "“인용이 끝나지 않은 경우 100분의 40을 적습니다.",
            "> 전체의 100분의 50을 감면한다.",
            "조문 원문: 전체의 100분의 50을 감면한다.",
            "1000분의 50을 적용합니다. 100분의 500원을 지급합니다.",
            "100분의 50~100분의 80을 적용합니다.",
            "산식은 100분의 50을 적용한 값 × 2입니다.",
            "시행일은 2026-10-01입니다. 3분의 2를 적용합니다.",
        ):
            with self.subTest(s=s):
                self.assertEqual(readable_answer(s, []), s)
        mix = "「자동차관리법」에 따른 경차는 100분의 50을 할인할 수 있습니다."
        self.assertIn("경차는 50%를 할인할 수 있습니다.", readable_answer(mix, []))

    def test_unknown_and_multiple_subjects_are_preserved(self):
        self.assertEqual(readable_answer("확인 불가", []), "확인 불가")
        text = "등록은 의무입니다.\n\n추가 대상자에게도 등록은 의무입니다."
        self.assertEqual(readable_answer(text, []), text)
