import copy
import unittest

from test_local_rag_answer import FakeClient, generation, retrieval
from local_rag_answer import assess_and_answer
from local_rag_service import kit_status, kit_view
from public_rag_response import public_result


class PublicWithheldProvisionTests(unittest.TestCase):
    def setUp(self):
        verdict = {"asked_matter": "지원 대상", "provision_id": "NONE", "basis": "no_provision"}
        client = FakeClient(generation(), provision=[verdict])
        self.result = assess_and_answer("지원 대상인가요?", retrieval(), client)
        self.assertEqual(self.result["draft_status"], "withheld_no_provision")

    def public(self, result, status="completed"):
        return public_result({"status": status, "result": result}, catalog={})

    def assert_held(self, result):
        view = self.public(result)
        self.assertEqual(view["status"], "code_check_failed")
        self.assertNotIn("response", view)
        self.assertEqual(kit_status({"status": "completed", "result": view}), "processing_error")

    def test_explicit_verdict_returns_only_the_kit_abstention(self):
        self.result.update(missing_materials=["private draft materials"], missing_information=["private draft facts"])
        view = self.public(self.result)
        self.assertEqual(view["status"], "insufficient_evidence")
        self.assertEqual(set(view), {"as_of_date", "answer_allowed", "final_answer_generated", "status",
                                     "message", "response"})
        self.assertEqual(view["response"], {"answer": "확인 불가", "citation": []})
        job = {"status": "completed", "result": view}
        self.assertEqual(kit_status(job), "completed")
        self.assertEqual(kit_view(job), {"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""})

    def test_missing_changed_or_failed_receipt_cannot_establish_absence(self):
        for mutation in ({"parsed_response": None}, {"parsed_response": {"provision_id": "NONE"}},
                         {"response": None}, {"error_type": "ReadTimeout"}, {"deadline": {"outcome": "timeout"}},
                         {"validation_error": "response_wrong_fields"}, {"rate_limit": {"outcome": "no_resend"}},
                         {"purpose": "uncited_draft_reselection"}):
            with self.subTest(mutation=mutation):
                result = copy.deepcopy(self.result)
                result["audit"][-1].update(mutation)
                self.assert_held(result)
        for stages in ([], self.result["audit"][:1], self.result["audit"] + [self.result["audit"][-1]]):
            with self.subTest(stages=len(stages)):
                self.assert_held({**self.result, "audit": stages})

    def test_direct_delegated_and_inconsistent_verdicts_are_never_absence(self):
        for alias, basis in (("C001", "states_content"), ("C001", "delegates_elsewhere"),
                             ("NONE", "delegates_elsewhere"), ("C001", "no_provision")):
            with self.subTest(basis=basis, alias=alias):
                result = copy.deepcopy(self.result)
                result["provision_check"].update(provision_id=alias, basis=basis)
                result["audit"][-1]["parsed_response"] = result["provision_check"]
                self.assert_held(result)

    def test_other_failures_never_adopt_the_ordinary_abstention(self):
        for mutation in (
                {"draft_status": "held"}, {"draft_generated": False}, {"decision_adjustment": {}},
                {"code_checks": {"status": "failed", "issues": ["claim_without_owned_citations"]}},
                {"code_checks": {"status": "failed", "issues": ["answer_contains_unsupported_number"]}},
                {"code_checks": {"status": "passed", "issues": ["claim_without_owned_citations"]}},
                {"answer_allowed": True}, {"final_answer_generated": True}):
            with self.subTest(mutation=mutation):
                self.assert_held({**self.result, **mutation})
        self.assertEqual(self.public(self.result, "processing_error")["status"], "processing_error")
