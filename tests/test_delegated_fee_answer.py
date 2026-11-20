from copy import deepcopy
import unittest

from delegated_fee_answer import delegated_fee_answer


LAW = "서울특별시 도시공원 이용 조례"
NOTICE = "「서울특별시 자원 조례」 제8조 관련 사용 및 임대요율 공고"
ORDER = "시설관리법 시행령 제42조제2항 [별표 7]"


def fixture(name="집열장치", reference=NOTICE, *, period="월액", note="", ditto=True):
    common = {"ordinance": LAW, "effective_date": "2025-05-01", "source_url": "https://www.law.go.kr/example",
              "public_label": LAW + " 별표 5", "owner_id": "unit-1"}
    title = "[별표 5] 공원 및 녹지 점용료(제21조 관련) <개정 2025. 3. 19.>"
    marker = f"〃 [동일 열 앞 셀: {period}]" if ditto else period
    rows = [{**common, "id": "A", "unit_id": "unit-1", "locator": "별표 5 — 1쪽 행 인덱스 2",
             "quote": f"점 용 대 상: 에너지 설비\n점 용 대 상: {name}\n단위기준: {marker}\n점 용 료: {reference} 준용{note}",
             "required_ids": ["B", "B2", "C"] if ditto else ["B", "B2"]},
            {**common, "id": "B", "unit_id": "unit-1::header", "locator": "별표 5 — 1쪽", "quote": title},
            {**common, "id": "B2", "unit_id": "unit-1::header2", "locator": "별표 5 — 1쪽", "quote": title}]
    if ditto:
        rows.append({**common, "id": "C", "unit_id": "unit-1::period", "locator": "별표 5 — 1쪽",
                     "quote": f"단위기준 〃은 같은 열의 앞 셀 '{period}'을 가리킨다."})
    return rows


class DelegatedFeeTests(unittest.TestCase):
    def test_amount_needs_the_exact_notice_and_scope(self):
        plan = delegated_fee_answer("집열장치 점용료는 얼마인가요?", fixture())
        self.assertEqual(plan["ids"], ["A", "B", "C"])
        self.assertEqual(plan["missing_materials"], [NOTICE])
        self.assertIn("월간", plan["answer"])
        self.assertIn(NOTICE, plan["answer"])

    def test_method_needs_no_document_to_explain_the_delegation(self):
        rows = fixture("저장시설(저장고)", ORDER, period="일액", ditto=False)
        plan = delegated_fee_answer("서울시 도시공원에 저장고를 두면 일간 점용료를 어떤 기준으로 정하나요?", rows)
        self.assertEqual(plan["missing_materials"], [])
        self.assertEqual(plan["used_ids"], ["A", "B"])
        self.assertIn(ORDER, plan["answer"])

    def test_permit_date_and_alias_are_bound_to_the_selected_facility(self):
        note = "※ 법 시행령 개정 전(2021.11.23.) 저장고 점용허가 사례는 최초 허가시점의 점용료 산정기준에 따라 부과한다"
        question = "서울시 도시공원에 저장고를 설치할 때 점용료는 어떻게 정하나요?"
        rows = fixture("저장시설(저장고)", ORDER, note=note)
        answer = delegated_fee_answer(question, rows)["answer"]
        self.assertIn("2021년 11월 23일", answer)
        self.assertIn("최초 허가시점", answer)
        for other in [note.replace("저장고", "풍력설비"), note.replace("11.23", "2.30"), note + " 다만 취약계층은 면제한다"]:
            with self.subTest(other=other):
                self.assertIsNone(delegated_fee_answer(question, fixture("저장시설(저장고)", ORDER, note=other)))

    def test_wrong_location_period_subject_or_additional_issue_defers(self):
        for question in ["부산시 도시공원에 집열장치를 두면 점용료는 얼마인가요?", "집열장치 연간 점용료는 얼마인가요?",
                         "집열장치와 저장고 점용료는 얼마인가요?", "집열장치 점용료는 3개월에 얼마인가요?",
                         "집열장치 점용료는 얼마인가요? 감면도 가능한가요?", "에너지 설비 점용료는 얼마인가요?"]:
            with self.subTest(question=question):
                self.assertIsNone(delegated_fee_answer(question, fixture()))

    def test_missing_conflicting_or_unknown_source_context_defers(self):
        question = "집열장치 점용료는 얼마인가요?"
        for field, update in [
                ("owner_id", "other"), ("source_url", "another ordinance"), ("effective_date", "2025-06-01"),
                ("quote", "[별표 9] 공원 점용료(제21조 관련)"), ("quote", "[별표 5] 공원 점용료(제21조 관련) 주말 할증")]:
            rows = fixture()
            rows[1][field] = update
            with self.subTest(field=field, update=update):
                self.assertIsNone(delegated_fee_answer(question, rows))
        for change in ["월액을 가리킨다.", "단위기준 〃은 같은 열의 앞 셀 '연액'을 가리킨다."]:
            rows = fixture()
            rows[-1]["quote"] = change
            self.assertIsNone(delegated_fee_answer(question, rows))
        rows = fixture()
        rows.append({**deepcopy(rows[0]), "id": "D", "unit_id": "other", "owner_id": "other", "locator": "제13조",
                     "quote": "집열장치는 일정 기간 점용료를 면제한다."})
        self.assertIsNone(delegated_fee_answer(question, rows))
        rows[-1].update(locator="별표 5", quote=rows[0]["quote"].replace(NOTICE, ORDER))
        self.assertIsNone(delegated_fee_answer(question, rows))
        for other in [LAW, "「" + LAW + "」 제21조", NOTICE + " 단, 50%", "해당재산가액의 10/1,000"]:
            self.assertIsNone(delegated_fee_answer(question, fixture(reference=other)))


if __name__ == "__main__":
    unittest.main()
