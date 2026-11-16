"""Offline answer contract tests against saved corpus and six linked gate cases.

All model calls are stubs. Passing these tests does not establish semantic
accuracy: a generator and verifier using the same model can share errors.
"""

from project_paths import PROJECT_ROOT
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import answer_attachment as answer

ARTIFACT_ROOT = Path(__file__).resolve().parent
PROJECT = PROJECT_ROOT
PASS = {"supported": True, "complete": True, "citations_sufficient": True, "issues": []}


@unittest.skipUnless((PROJECT / "data/processed/attachments/2171833_22740725.search_chunks.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/processed/attachments/2171833_22740725.search_chunks.json")
class AnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = answer.load_corpus(PROJECT)
        cls.links = answer.build_links(PROJECT, cls.corpus)
        folder = PROJECT / "data" / "processed" / "attachments" / "linked_gate_results"
        cls.cases = {path.stem: answer.read_json(path) for path in folder.glob("*.result.json")}
        if len(cls.cases) != 6:
            raise AssertionError("Expected six saved linked evaluation cases")
        cls.supported = cls.cases["public_office_specific_row.result"]

    def setUp(self):
        self.addCleanup(patch.stopall)
        self.network = patch.object(answer.requests, "post", side_effect=AssertionError("offline only")).start()
        patch.object(answer, "load_dotenv", side_effect=AssertionError("no key reads")).start()
        self.linked, self.model_input = answer.prepare_input(self.supported, self.corpus, self.links)
        self.text = self.model_input["cited_evidence"][0]["text"]

    def call_stub(self, generated=None, verification=None):
        return Mock(side_effect=[
            (generated if generated is not None else {"answer": self.text}, {"stub": "generator"}),
            (verification if verification is not None else copy.deepcopy(PASS), {"stub": "verifier"}),
        ])

    def draft(self, gate_result=None, call=None):
        return answer.generate_draft(PROJECT, ARTIFACT_ROOT, gate_result or self.supported,
                                     self.corpus, self.links, call_fn=call or self.call_stub())

    def rejected(self, code, fn, *args):
        with self.assertRaises(answer.GateError) as caught:
            fn(*args)
        self.assertEqual(str(caught.exception), code)

    def test_supported_has_exact_four_fields_and_pending_publication(self):
        call = self.call_stub()
        audit = self.draft(call=call)
        response = audit["response"]
        self.assertEqual(set(response), {"answer", "citation", "source_url", "effective_date"})
        self.assertEqual(response["answer"], self.text.strip())
        self.assertTrue(response["citation"])
        self.assertTrue(all(isinstance(label, str) for label in response["citation"]))
        self.assertEqual(response["source_url"], self.links["source"]["source_url"])
        self.assertEqual(response["effective_date"], self.links["source"]["effective_date"])
        self.assertFalse(audit["answer_allowed"])
        self.assertIn("source_review_pending", audit["publication_blocks"])
        self.assertEqual(call.call_count, 2)
        self.network.assert_not_called()

    def test_fallback_is_exactly_two_fields_without_model_call(self):
        negatives = [case for case in self.cases.values()
                     if case["decision"] in ("insufficient_evidence", "out_of_scope")]
        self.assertEqual({case["decision"] for case in negatives}, {"insufficient_evidence", "out_of_scope"})
        for case in negatives:
            with self.subTest(decision=case["decision"]):
                call = Mock(side_effect=AssertionError("fallback must not call model"))
                audit = self.draft(case, call)
                self.assertEqual(audit["response"], {"answer": "확인 불가", "citation": []})
                self.assertFalse(audit["answer_allowed"])
                self.assertEqual(audit["calls"], [])
                call.assert_not_called()
                linked, _ = answer.prepare_input(case, self.corpus, self.links)
                self.rejected("invalid_fallback_contract", answer.validate_response,
                              {**audit["response"], "effective_date": "2026-10-01"}, linked, self.links)

    def test_clarification_produces_no_response_and_no_model_calls(self):
        case = next(case for case in self.cases.values() if case["decision"] == "needs_clarification")
        call = Mock(side_effect=AssertionError("clarification must not call model"))
        audit = self.draft(case, call)
        self.assertEqual(audit["status"], "needs_clarification")
        self.assertIsNone(audit["response"])
        self.assertTrue(audit["missing_information"])
        self.assertFalse(audit["answer_allowed"])
        call.assert_not_called()

    def test_malformed_generation_and_unknown_numbers_stop_before_verifier(self):
        invalid = [
            ({"answers": self.text}, "invalid_generated_schema"),
            ({"answer": self.text, "citation": []}, "invalid_generated_schema"),
            ({"answer": ""}, "invalid_answer_text"),
            ({"answer": "확인 불가"}, "generator_disagrees_with_supported_gate"),
            ({"answer": "필요 주차대수는 987654321대입니다."}, "answer_has_unsourced_number"),
        ]
        for generated, code in invalid:
            with self.subTest(code=code):
                call = self.call_stub(generated=generated)
                self.rejected(code, self.draft, None, call)
                self.assertEqual(call.call_count, 1)

    def test_each_failed_verifier_flag_prevents_response(self):
        for field in ("supported", "complete", "citations_sufficient"):
            with self.subTest(field=field):
                verification = {**PASS, field: False, "issues": ["원문과 불일치"]}
                audit = self.draft(call=self.call_stub(verification=verification))
                self.assertEqual(audit["status"], "blocked")
                self.assertEqual(audit["block_reason"], "answer_failed_semantic_check")
                self.assertIsNone(audit["response"])
                self.assertFalse(audit["answer_allowed"])

    def test_wrong_url_date_and_field_typo_are_rejected(self):
        valid = answer.assemble_response(self.text, self.linked, self.links)
        for key, value in [("source_url", "https://example.invalid/wrong"), ("effective_date", "1900-01-01")]:
            with self.subTest(key=key):
                self.rejected("public_response_metadata_mismatch", answer.validate_response,
                              {**valid, key: value}, self.linked, self.links)
        typo = dict(valid)
        typo["citations"] = typo.pop("citation")
        self.rejected("invalid_public_response_fields", answer.validate_response, typo, self.linked, self.links)

    def test_failed_rerun_removes_stale_draft_in_verified_artifact_tempdir(self):
        with tempfile.TemporaryDirectory(dir=ARTIFACT_ROOT, prefix="answer-offline-") as temporary:
            root = Path(temporary).resolve()
            # Confirm TemporaryDirectory's recursive cleanup stays in our artifact root.
            self.assertTrue(root.is_relative_to(ARTIFACT_ROOT.resolve()))
            audit = self.draft()
            _, response_path = answer.save_draft(root, "same-question", audit)
            self.assertTrue(response_path.is_file())
            failure = answer.failure_audit("question", answer.GateError("invalid_generated_schema"))
            audit_path, returned_path = answer.save_draft(root, "same-question", failure)
            self.assertFalse(response_path.exists())
            self.assertIsNone(returned_path)
            self.assertEqual(answer.read_json(audit_path)["status"], "error")

    def test_same_model_verifier_acceptance_is_not_semantic_guarantee(self):
        # No deterministic check catches this invented non-numeric fact if
        # both model calls agree. The draft must still remain unpublished.
        invented = "이 조례는 달 여행을 허가합니다."
        audit = self.draft(call=self.call_stub(generated={"answer": invented}))
        self.assertEqual(audit["response"]["answer"], invented)
        self.assertEqual(audit["status"], "draft")
        self.assertFalse(audit["answer_allowed"])
        self.assertIn("demo_only", audit["publication_blocks"])


    def test_note_11_short_answer_requires_full_parent_and_child_conditions(self):
        case = self.cases["note_11_parent_condition.result"]
        _, model_input = answer.prepare_input(case, self.corpus, self.links)
        short = "해당 시설의 설치기준을 적용합니다."
        self.rejected("answer_missing_required_condition_text", answer.validate_required_conditions,
                      short, model_input)
        required = answer.required_condition_units(model_input)
        parent_ids = {parent for unit in model_input["cited_evidence"]
                      for parent in unit["required_context_ids"]}
        self.assertTrue(parent_ids)
        self.assertTrue(any(unit["required_context_ids"] for unit in required))
        self.assertTrue(parent_ids.issubset({unit["unit_id"] for unit in required}))
        preserved = answer.preserve_required_conditions(short, model_input)
        self.assertTrue(preserved.startswith(short))
        for unit in required:
            self.assertIn(unit["label"], preserved)
            self.assertIn(unit["text"], preserved)
        answer.validate_required_conditions(preserved, model_input)

    def test_cli_load_failures_remove_prior_draft_for_same_gate_result_path(self):
        gate_path = (PROJECT / "data" / "processed" / "attachments" / "linked_gate_results"
                     / "public_office_specific_row.result.json")
        input_identity = {"gate_result_path": str(gate_path.resolve()).casefold()}
        expected_key = answer.digest_json(input_identity)[:16]
        success = self.draft()
        with tempfile.TemporaryDirectory(dir=ARTIFACT_ROOT, prefix="answer-cli-offline-") as temporary:
            root = Path(temporary).resolve()
            self.assertTrue(root.is_relative_to(ARTIFACT_ROOT.resolve()))
            argv = ["answer_attachment.py", "--project-dir", str(PROJECT),
                    "--output-dir", str(root), "--gate-result", str(gate_path), "--demo"]
            response_path = root / f"{expected_key}.draft.response.json"
            audit_path = root / f"{expected_key}.audit.json"
            inline_worker = lambda function, kwargs, work_dir, seconds: function(**kwargs)
            with patch("sys.argv", argv), patch("builtins.print"), patch.object(answer, "run_with_deadline", side_effect=inline_worker):
                for failure in [answer.GateError("input_hwp_hash_mismatch"), answer.ParseError("broken XML")]:
                    with self.subTest(error=type(failure).__name__):
                        with patch.object(answer, "generate_draft", return_value=copy.deepcopy(success)):
                            answer.main()
                        self.assertTrue(response_path.is_file())
                        with patch.object(answer, "load_corpus", side_effect=failure):
                            with self.assertRaises(SystemExit) as caught:
                                answer.main()
                        self.assertEqual(caught.exception.code, 1)
                        self.assertFalse(response_path.exists())
                        audit = answer.read_json(audit_path)
                        self.assertEqual(audit["status"], "error")
                        self.assertIsNone(audit["response"])
                        expected_error = str(failure) if isinstance(failure, answer.GateError) else "ParseError"
                        self.assertEqual(audit["error_code"], expected_error)
        self.network.assert_not_called()

if __name__ == "__main__":
    unittest.main()

