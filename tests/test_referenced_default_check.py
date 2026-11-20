import json
import time
import unittest

import local_rag_answer as rag
from test_local_rag_answer import FakeClient, generation, replace_source_quote, retrieval


class ReferencedDefaultTests(unittest.TestCase):
    quote = "1. 자료의 열람은 「자료 이용 규칙」 제2조의3제1항에 따라 허용을 원칙으로 한다."
    answer = "자료 열람은 「자료 이용 규칙」에 따라 허용하는 것이 원칙입니다."

    def run_pipeline(self, question, alias, basis):
        data = retrieval()
        replace_source_quote(data, self.quote)
        client = FakeClient(generation(self.answer), provision=[
            {"asked_matter": question, "provision_id": alias, "basis": basis}])
        result = rag.assess_and_answer(question, data, client)
        self.assertEqual([stage for stage, _ in client.calls], ["draft", "provision_check"])
        payload = client.calls[-1][1]
        self.assertIn(rag.REFERENCED_DEFAULT_CHECK, payload["messages"][0]["content"])
        self.assertEqual(result["code_checks"]["status"], "passed")
        return result

    def test_exception_question_can_publish_the_cited_default_and_reference(self):
        result = self.run_pipeline("자료를 열람할 수 없는 경우는 언제인가요?", "C001", "delegates_elsewhere")
        self.assertEqual(result["response"]["answer"], self.answer)
        self.assertEqual(result["provision_check"]["basis"], "delegates_elsewhere")
        self.assertEqual(result["draft_status"], "code_checked_draft")

    def test_unrelated_penalty_can_be_withheld_despite_reference_in_citation(self):
        result = self.run_pipeline("허락 없이 자료를 열람하면 과태료가 얼마인가요?", "NONE", "no_provision")
        self.assertEqual(result["draft_status"], "withheld_no_provision")
        self.assertEqual(result["response"]["citation"], [])

    def test_unused_rule_never_influences_the_check_and_a_mention_is_insufficient(self):
        selected = {"unit_id": "one", "locator": {}, "quote": "자료는 열람할 수 있다."}
        other = {"unit_id": "two", "locator": {}, "quote": self.quote}
        client = FakeClient()
        rag._provision_check(client, "자료를 볼 수 있나요?", [{"text": "자료는 열람할 수 있다.", "citation_ids": ["one"]}],
                             [selected, other], {"one": "C001", "two": "C002"}, [], time.monotonic() + 30)
        payload = client.calls[0][1]
        self.assertEqual(payload["messages"][0]["content"], rag.PROVISION_CHECK_PROMPT)
        cited = json.loads(payload["messages"][1]["content"])["citations"]
        self.assertEqual([unit["id"] for unit in cited], ["C001"])
        for quote in (
            "원칙은 신청순 처리다. 「다른 조례」 제2조에 따라 비용을 지급한다.",
            "사업 운영의 원칙을 논의한다. 관련 자료 목록에 「도서관 조례」가 있다.",
            "「도서관 조례」 제3조에 따라 요금을 정한다.",
        ):
            with self.subTest(quote=quote):
                self.assertFalse(rag._states_referenced_default(quote))


if __name__ == "__main__":
    unittest.main()
