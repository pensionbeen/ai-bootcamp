import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from citizen_answer_style import readable_answer
from concise_public_answer import concise_answer


LAW = "서울특별시 공공시설 관리 조례"
OTHER = "서울특별시 공원 관리 조례"
A = LAW + " 제4조 제1항"
B = LAW + " 제5조의2 제2항"
ANNEX = LAW + " 별표 2"


class ConcisePublicAnswerTests(unittest.TestCase):
    def check_answer(self, original, sources, expected):
        unchanged = list(sources)
        self.assertEqual(concise_answer(original, sources), expected)
        self.assertEqual(concise_answer(expected, sources), expected)
        self.assertEqual(sources, unchanged)

    def test_only_moved_source_of_a_self_contained_single_claim_can_disappear(self):
        body = "서울시 공공시설은 야간 이용료의 100분의 25를 할인할 수 있습니다."
        original = A + "에 따르면, " + body
        self.check_answer(original, [A], body.replace("100분의 25를", "25%를"))
        self.check_answer(body[:-1] + f" ({A}).", [A], body[:-1].replace("100분의 25를", "25%를") + f" ({A}).")

    def test_complete_annex_and_related_article_remain_in_public_citations(self):
        source = ANNEX + " 공공시설 이용요금표(제4조제1항 관련)"
        original = source + "에 따르면, 실내 공공시설의 야간 이용료는 1시간당 500원입니다."
        self.check_answer(original, [A, ANNEX], "실내 공공시설의 야간 이용료는 1시간당 500원입니다 (공공시설 이용요금표).")
        self.check_answer(original, [ANNEX], original)
        self.check_answer(original, [ANNEX, OTHER + " 제4조 제1항"], original)

    def test_body_must_identify_context_and_source_assignment(self):
        for body in ["서울시의 할인율은 10%입니다.", "공공시설은 무료입니다.",
                     "해당 서울시 공공시설은 무료입니다.", "서울시 공공시설은 제3조에 해당합니다.",
                     "서울시 공공시설은 기존 조례의 적용을 받습니다.", "서울시 공공시설은 2025년 1월부터 무료입니다.",
                     "서울시 공공시설의 수입은 ‘기본 요금’입니다.", "첫째입니다. 둘째입니다.",
                     "서울시 공공시설인가요? 서울시 공공시설은 야간에 무료입니다."]:
            original = A + "에 따르면, " + body
            with self.subTest(body=body):
                self.check_answer(original, [A], readable_answer(original, [A]))
        subj = "서울시 공공시설의 야간 이용료는 500원입니다."
        text = A + "에 따르면, " + subj
        youth_law = "서울특별시 청년 기본 조례 제7조"
        unrelated = youth_law + "에 따르면, 서울시의 기본요금은 500원입니다."
        self.check_answer(unrelated, [youth_law], readable_answer(unrelated, [youth_law]))
        for original, sources in [(text, [A, B]), (text, [A, None]), (text, [A, OTHER + " 제4조"]),
                                  (text + "\n\n기준은 달라질 수 있습니다.", [A])]:
            self.check_answer(original, sources, readable_answer(original, sources))

    def test_empty_unresolved_and_unrecognized_labels(self):
        for answer, citations in [(None, []), ("확인 불가", []), ("", []), ("공공시설의 이용 기준입니다.", [None])]:
            self.check_answer(answer, citations, answer)
