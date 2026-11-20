import json
import time
import unittest
from unittest.mock import patch

from test_local_rag_answer import FakeClient, generation, replace_source_quote, retrieval
from local_rag_answer import assess_and_answer, PROVISION_CHECK_DEADLINE_SECONDS, _confirmed_retrieved_absence
from public_rag_response import public_result


QUESTION = "이용자가 보호장구를 착용하지 않았을 때 과태료가 있나요?"
ABSENCE = "제공된 원문에서 과태료에 관한 규정이 확인되지 않습니다."


class ReviewClient(FakeClient):
    def __init__(self, review, draft=None):
        super().__init__(generation(ABSENCE, ids=[]) if draft is None else draft)
        self.review = review

    def request(self, kind, payload, timeout=None):
        if kind == "provision_check":
            units = json.loads(payload["messages"][1]["content"])["candidates"]
            self.provision = [self.review(units) if callable(self.review) else self.review]
        return super().request(kind, payload, timeout=timeout)


def absence(units):
    return {"matter_term": "과태료", "provision_id": "NONE",
            "units": {unit["id"]: "unrelated" for unit in units}}


def evaluate(quote, review=absence, *, draft=None, started=None):
    data = retrieval()
    replace_source_quote(data, quote)
    client = ReviewClient(review, draft)
    result = assess_and_answer(QUESTION, data, client, started=started)
    public = public_result({"status": "completed", "result": result}, catalog={})
    return result, public, client


class UncitedAbsenceReviewTests(unittest.TestCase):
    def test_only_explicit_full_source_review_yields_citizen_insufficiency(self):
        quote = "시장은 공영시설 운영 일정을 홈페이지에 공고한다."
        result, public, client = evaluate(quote)
        self.assertEqual(public["status"], "insufficient_evidence")
        self.assertEqual(public["response"], {"answer": "확인 불가", "citation": []})
        self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
        units = json.loads(client.calls[1][1]["messages"][1]["content"])
        self.assertEqual(set(units), {"question", "candidates"})
        self.assertTrue(all(set(unit) == {"id", "locator", "quote"} for unit in units["candidates"]))
        self.assertTrue(any(unit["quote"] == quote for unit in units["candidates"]))
        self.assertNotIn(ABSENCE, json.dumps(units, ensure_ascii=False))
        self.assertEqual(result["audit"][0]["parsed_response"]["claims"][0]["text"], ABSENCE)

    def test_direct_or_delegated_or_uncertain_evidence_never_becomes_absence(self):
        for verdict, quote in (
                ("direct", "보호장구를 착용하지 않은 이용자에게 과태료 5만원을 부과한다."),
                ("partial", "보호장구 미착용에 대한 제재는 별도 시행규칙에 따른다."),
                ("uncertain", "시설의 이용에 관하여 별표를 적용한다.")):
            with self.subTest(verdict=verdict):
                def review(units):
                    value = absence(units)
                    value["provision_id"] = units[0]["id"]
                    value["units"][units[0]["id"]] = verdict
                    return value

                result, public, client = evaluate(quote, review)
                self.assertEqual(public["status"], "code_check_failed")
                self.assertNotIn("response", public)
                self.assertEqual(result["uncited_draft_review"]["result"]["provision_id"], "R001")
                self.assertEqual(len(client.calls), 2)

    def test_literal_provision_vetoes_even_an_all_unrelated_judgment(self):
        _, public, _ = evaluate("보호장구 미착용 과태료는 오만원으로 한다.")
        self.assertEqual(public["status"], "code_check_failed")

    def test_verbatim_compound_matter_checks_head_and_distinctive_modifiers(self):
        name = "서울특별시 자전거이용 활성화에 관한 조례"
        question = name + "에 자전거 헬멧 미착용 과태료 액수가 정해져 있나요?"
        units = [{"id": "R001", "locator": name + " 부칙", "quote": "자전거 안전교육 이수자에게도 적용한다."}]
        review = {**absence(units), "matter_term": "자전거 헬멧 미착용 과태료"}
        self.assertTrue(_confirmed_retrieved_absence(review, units, question, {name}))
        for quote in ("과태료는 시장이 정한다.", "헬멧을 착용하지 않은 경우에 적용한다.", "미착용 시 경고한다."):
            with self.subTest(quote=quote):
                self.assertFalse(_confirmed_retrieved_absence(review, [{**units[0], "quote": quote}], question, {name}))
        for term in ("금액 여부", "자전거 과태료", "자전거 헬멧 과태료", "가" * 49):
            with self.subTest(term=term):
                self.assertFalse(_confirmed_retrieved_absence({**review, "matter_term": term}, units,
                                                               question + " 금액 여부", {name}))

    def test_spurious_selector_output_is_held(self):
        bad = (
            lambda units: {**absence(units), "provision_id": "R998"},
            lambda units: {**absence(units), "units": {}},
            lambda units: {**absence(units), "matter_term": "있나요"},
            lambda units: {**absence(units), "matter_term": "위조용어"},
            lambda units: {**absence(units), "units": {unit["id"]: "partial" for unit in units}},
            RuntimeError("transport unavailable"),
        )
        for review in bad:
            with self.subTest(review=review):
                result, public, client = evaluate("조례는 공포한 날부터 시행한다.", review)
                self.assertFalse(result["uncited_draft_review"]["confirmed_retrieved_absence"])
                self.assertEqual(public["status"], "code_check_failed")
                self.assertEqual(len(client.calls), 2)

    def test_deadline_does_not_send_or_publish(self):
        _, public, client = evaluate("조례는 공포한 날부터 시행한다.",
                                     started=time.monotonic() - PROVISION_CHECK_DEADLINE_SECONDS)
        self.assertEqual([kind for kind, _ in client.calls], ["draft"])
        self.assertEqual(public["status"], "code_check_failed")

    def test_successful_response_after_the_check_deadline_cannot_confirm_absence(self):
        clock = [1000.0]
        def delayed(units):
            clock[0] += PROVISION_CHECK_DEADLINE_SECONDS + 1
            return absence(units)
        with patch("local_rag_answer.time.monotonic", side_effect=lambda: clock[0]):
            result, public, _ = evaluate("조례는 공포한 날부터 시행한다.", review=delayed, started=clock[0])
        self.assertEqual(public["status"], "code_check_failed")
        self.assertFalse(result["uncited_draft_review"]["confirmed_retrieved_absence"])
        self.assertIn("response", result["audit"][-1])
        self.assertEqual(result["audit"][-1]["error_type"], "late_provision_check")

    def test_missing_document_alone_needs_a_successful_review_of_the_sources(self):
        draft = generation(claims=[], decision="external_material_required", missing_materials=["이 조례의 제재 조문"])
        result, public, _ = evaluate("시장은 시설을 설치할 수 있다.", draft=draft)
        self.assertEqual(public["status"], "insufficient_evidence")
        self.assertEqual(result["missing_materials"], [])
        self.assertEqual(result["audit"][0]["parsed_response"]["missing_materials"], ["이 조례의 제재 조문"])
        for review in (RuntimeError("transport unavailable"),
                       lambda units: {**absence(units), "provision_id": units[0]["id"],
                                      "units": {unit["id"]: "partial" for unit in units}}):
            result, public, _ = evaluate("제재의 세부 기준은 시장이 따로 정한다.", review, draft=draft)
            self.assertEqual(result["decision"], "external_material_required")
            self.assertNotIn("response", public)
        result, public, client = evaluate("시설에 관한 조례이다.", draft=draft,
                                          started=time.monotonic() - PROVISION_CHECK_DEADLINE_SECONDS)
        self.assertEqual([kind for kind, _ in client.calls], ["draft"])
        self.assertFalse(result["uncited_draft_review"]["confirmed_retrieved_absence"])
        self.assertNotIn("response", public)

    def test_bad_affirmative_draft_and_partial_do_not_trigger_reselection(self):
        for draft in (generation("이용자는 반드시 보호장구를 구입해야 합니다.", ids=[]),
                      generation(ABSENCE, ids=[], decision="external_material_required",
                                 missing_materials=["이용 시행규칙"])):
            with self.subTest(draft=draft):
                _, public, client = evaluate("조례는 공포한 날부터 시행한다.", draft=draft)
                self.assertEqual([kind for kind, _ in client.calls], ["draft"])
                self.assertEqual(public["status"], "code_check_failed")
