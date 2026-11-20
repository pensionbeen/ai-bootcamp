import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scoped_benefit_answer import scoped_benefit_answer
from benefit_procedure import unmatched_benefit_procedure


def benefit_rows():
    common = dict(ordinance="서울특별시 문화회관 조례", owner_id="para", effective_date="2026-01-01",
                  public_label="서울특별시 문화회관 조례 제12조 제4항", source_url="https://example.test/culture")
    intro = dict(common, id="grant", unit_id="para::intro", locator="제12조 제4항", required_ids=[],
                 quote="④ 시장은 다음 각 호에 해당할 경우에는 시설이용료를 감면할 수 있다. "
                       "이 경우 회원조회기 또는 전자회원대장으로 각 호의 자격이 확인되는 시설 이용자는 "
                       "증빙서류를 제시하지 않고 시설이용료를 감면받을 수 있다.")
    item = dict(common, id="item", unit_id="para::item::3", locator="제12조 제4항 제3호", required_ids=["grant"],
                quote="3. 다음의 청소년 및 예술인 등이 이용하는 시설에 대하여는 시설이용료의 100분의 45를 할인한다. "
                      "다만, 야외 공연장의 경우에는 1일 2회당 최초 30분까지의 시설이용료는 면제하고 이후 시설이용료의 25%를 할인한다.\n"
                      "가. 「청소년 기본법」 제3조에 따른 청소년으로서 청소년증을 제시한 경우\n"
                      "나. 「예술인 복지법」에 따른 예술인으로서 예술인증을 제시한 사람")
    return [intro, item]


class BenefitProcedureTests(unittest.TestCase):
    def setUp(self):
        self.waiver = dict(kind="body", ordin_seq="A", source_url="scheme-a", unit_id="A1",
                           quote=benefit_rows()[0]["quote"])
        self.other = dict(kind="body", ordin_seq="B", source_url="scheme-b", unit_id="B1",
                          quote="① 구청장은 공익 활동에 사용하는 경우 대여료를 감면할 수 있다.")

    def test_unspecified_filing_question_requires_conflicting_schemes(self):
        for q in ("할인 신청 서류는 어디에 제출하나요?", "면제를 받으려면 어떤 서류가 필요한가요?"):
            self.assertEqual(unmatched_benefit_procedure(q, [self.waiver, self.other], []), ["A1", "B1"])
            self.assertFalse(unmatched_benefit_procedure(q, [self.waiver], []))
        for q in ("청소년이 시설이용료를 감면받으려면 어디로 신청하나요?", "회원조회기로 확인되면 증빙서류도 필요한가요?",
                  "주차 할인 신청 서류와 요금을 함께 알려 주세요.", "입장료의 공통 감면 요건은 무엇인가요?"):
            self.assertFalse(unmatched_benefit_procedure(q, [self.waiver, self.other], []))

    def test_shared_or_unexplained_document_rules_keep_the_normal_answer_path(self):
        q = "감면 신청 서류는 어디에 제출하나요?"
        common = "감면 신청인은 요금 종류에 관계없이 신청서를 담당자에게 제출한다."
        for rows, conditions in (([self.waiver, self.other, {**self.other, "quote": common}], []),
                                 ([self.waiver, self.other], [{"source": self.other, "condition": {"quote": common}}]),
                                 ([self.waiver, {**self.other, "quote": self.other["quote"]+" "+common}], []),
                                 ([{**self.waiver, "quote": self.waiver["quote"]+"\n1. 다만, 추가서류를 제출한다."},
                                   self.other], [])):
            self.assertFalse(unmatched_benefit_procedure(q, rows, conditions))
        for note, ambiguous in (("시설 사용요율은 점용료 공고 기준을 준용한다.", True),
                                ("감면 신청에 관하여 다른 조례 제5조를 준용한다.", False),
                                ("어느 요금이든 모든 감면 신청은 통합민원센터에 해야 한다.", False)):
            condition = {"source":self.other, "condition":{"quote":note}}
            self.assertEqual(bool(unmatched_benefit_procedure(q, [self.waiver,self.other], [condition])), ambiguous)


class ScopedBenefitAnswerTests(unittest.TestCase):
    def test_pick_just_the_asked_branch_and_source_rates(self):
        rows = benefit_rows()
        for query in ("청소년 시설이용료 감면되나요?", "청소년도 시설이용료를 할인받을 수 있나요?",
                      "청소년 시설이용료는 할인돼요?"):
            with self.subTest(query=query):
                plan = scoped_benefit_answer(query, rows)
                self.assertEqual(plan["used_ids"], ["grant", "item"])
                for required in ("청소년증", "시설이용료를 45%", "회원조회기 또는 전자회원대장으로", "1일 2회당 최초 30분", "25%"):
                    self.assertIn(required, plan["answer"])
                self.assertNotIn("예술인", plan["answer"])
        plan = scoped_benefit_answer("예술인 시설이용료 할인됩니까?", rows)
        self.assertIn("예술인증", plan["answer"])
        self.assertNotIn("청소년", plan["answer"])

    def test_boarding_condition_follows_the_source(self):
        rows = benefit_rows()
        rows[1]["quote"] = rows[1]["quote"].replace("청소년으로서 청소년증을 제시한 경우",
                                                   "청소년차량표지를 부착하고 청소년이 탑승하여 청소년증을 제시한 경우")
        plan = scoped_benefit_answer("청소년 시설이용료 할인돼요?", rows)
        self.assertIn("청소년차량표지를 부착하고 청소년이 탑승하여 청소년증을 제시하면", plan["answer"])

    def test_document_question_only_waives_proof_when_the_named_system_confirms_eligibility(self):
        rows = benefit_rows()
        question = "청소년 시설이용료 감면을 신청할 때 회원조회기로 자격이 확인되면 증빙서류를 따로 제시해야 하나요?"
        result = scoped_benefit_answer(question, rows)
        self.assertEqual(result["used_ids"], ["grant","item"])
        self.assertIn("회원조회기 또는 전자회원대장으로", result["answer"])
        self.assertIn("감면 대상이라면", result["answer"])
        self.assertIn("따로 제시하지 않아도", result["answer"])
        for different in (question.replace("회원조회기", "은행전산"), question.replace("확인되면", "확인되지 않으면"),
                          question.replace("증빙서류", "은행등록증"), question+" 금액도 알려주세요."):
            self.assertIsNone(scoped_benefit_answer(different, rows))

    def test_separate_documented_items_do_not_share_their_rates_or_exceptions(self):
        rows = benefit_rows()
        for index, (subject, document, rate) in enumerate((("예술인", "예술인증", 35), ("조합원", "조합원증", 15)), 4):
            rows.append(dict(rows[1], id=str(index), unit_id=f"para::item::{index}", locator=f"제12조 제4항 제{index}호",
                             quote=f"{index}. {subject}으로서 {document}을 제시한 경우 시설이용료의 {rate}%를 할인한다."))
        self.assertIsNone(scoped_benefit_answer("예술인 시설이용료 감면되나요?", rows))
        answer = scoped_benefit_answer("조합원 시설이용료 감면되나요?", rows)["answer"]
        self.assertIn("조합원증", answer)
        self.assertIn("15%", answer)
        for unused in ("예술인", "35%", "30분"):
            self.assertNotIn(unused, answer)

    def test_conditional_terms_do_not_leak_from_the_fixture(self):
        rows = benefit_rows()
        rows[0]["quote"] = rows[0]["quote"].split(". ")[0] + "."
        text = rows[1]["quote"]
        rows[1]["quote"] = text.split(". 다만,")[0].replace("100분의 45", "12.5%") + ".\n" + text.split("\n", 1)[1]
        answer = scoped_benefit_answer("예술인 시설이용료 감면되나요?", rows)["answer"]
        self.assertIn("12.5%", answer)
        for unused in ("증빙서류", "25%", "45%", "공연장"):
            self.assertNotIn(unused, answer)

    def test_unrelated_questions_and_incomplete_evidence_fall_back(self):
        for query in ("주차요금은 얼마인가요?", "청소년의 보호자도 시설이용료 감면되나요?",
                      "청소년 시설이용료 할인이랑 예술인 할인 중복인가요?", "청소년 체육관 이용료 감면되나요?",
                      "공영수영장에서 청소년 시설이용료 감면되나요?", "청소년증이 없어도 시설이용료 감면되나요?"):
            self.assertIsNone(scoped_benefit_answer(query, benefit_rows()), query)
        for change in (lambda rows: rows.pop(0), lambda rows: rows[0].update(effective_date="2025-01-01"),
                       lambda rows: rows[0].update(owner_id="wrong"), lambda rows: rows[1].update(required_ids=[]),
                       lambda rows: rows[1].update(required_ids=["grant", "missing"]),
                       lambda rows: rows[1].update(quote=rows[1]["quote"] + "\n다만, 방학 동안에는 제외한다."),
                       lambda rows: rows[1].update(quote=rows[1]["quote"].replace("나.", "다.")),
                       lambda rows: rows[1].update(quote=rows[1]["quote"].replace("청소년으로서", "청소년의 보호자로서")),
                       lambda rows: rows.append(dict(rows[1], id="another", unit_id="para::item::4",
                                                     quote="4. 청소년에게 시설이용료의 70%를 할인한다."))):
            rows = benefit_rows()
            change(rows)
            self.assertIsNone(scoped_benefit_answer("청소년 시설이용료 감면되나요?", rows), rows)

    def test_facility_requires_explicit_operating_scope_in_the_grant(self):
        query = "서울시 공영미술관에서 청소년 시설이용료 감면되나요?"
        rows = benefit_rows()
        for title in ("서울특별시 사립미술관 지원 조례", "서울특별시 공영미술관 보조금 조례", "서울특별시 공영미술관 조례"):
            for row in rows:
                row.update(ordinance=title, public_label=title + " 제12조 제4항")
            self.assertIsNone(scoped_benefit_answer(query, rows), title)
        rows[0]["quote"] = rows[0]["quote"].replace("다음 각 호", "서울특별시장이 직접 운영하는 공영미술관에서 다음 각 호", 1)
        plan = scoped_benefit_answer(query, rows)
        self.assertIn("서울특별시장이 직접 운영하는 공영미술관에서는", plan["answer"])
        self.assertIsNone(scoped_benefit_answer(query.replace("공영미술관", "공영주차장"), rows))
        for rival in ("구청장", "다른시장", "시장"):
            original = rows[0]["quote"]
            rows[0]["quote"] = original.replace("서울특별시장", rival)
            self.assertIsNone(scoped_benefit_answer(query, rows), rival)
            rows[0]["quote"] = original
        for row in rows:
            row.update(ordinance="서울특별시 강남구 공영미술관 조례", public_label="서울특별시 강남구 공영미술관 조례 제12조 제4항")
        self.assertIsNone(scoped_benefit_answer(query, rows))

    def test_an_explicit_restriction_can_refer_to_a_benefit_by_its_item_number(self):
        for restriction in ("4. 제3호에도 불구하고 토요일에는 시설이용료를 감면하지 아니한다.",
                            "4. 청소년에게는 토요일에 제3호의 할인을 적용하지 아니한다.",
                            "4. 제12조제4항제3호에 따른 할인은 평일에 한한다."):
            rows = benefit_rows()
            rows.append(dict(rows[1], id="special", unit_id="para::item::4", locator="제12조 제4항 제4호", quote=restriction))
            self.assertIsNone(scoped_benefit_answer("청소년 시설이용료 감면되나요?", rows))


if __name__ == "__main__":
    unittest.main()
