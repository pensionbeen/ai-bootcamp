"""Mixed requirement contracts with synthetic sources and offline clients only."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import development_rag_review as development
import evaluate_local_rag as evaluation
from local_rag_answer import assess_and_answer
from public_rag_response import ORDINANCE_SCOPE_NOTICE, public_result
from test_local_rag_answer import ANSWER, CID, FakeClient, retrieval
from test_development_rag_review import ReviewClient, gate_answer


QUESTION = '청년의 정의와 이 사업에 신청할 수 있는지는?'
FACTS = ['실제 신청일']
MATERIALS = ['별도 사업 규칙']


def mixed_response(**changes):
    response = {'decision': 'needs_clarification',
                'missing_information': list(FACTS), 'missing_materials': list(MATERIALS),
                'claims': [{'text': ANSWER, 'citation_ids': ['C001']}], 'calculations': []}
    response.update(changes)
    return response


def operating_response(response=None, question=QUESTION):
    client = FakeClient(response if response is not None else mixed_response())
    result = assess_and_answer(question, retrieval(), client)
    audit = {'status': 'completed', 'question': question, 'result': result}
    return audit, public_result(audit, catalog={}), client


def mixed_review_gate(facts=FACTS):
    result = gate_answer('external_material_required')
    result.update(missing_information=list(facts), missing_materials=list(MATERIALS))
    return result


class MixedRequirementTests(unittest.TestCase):
    def test_partial_answer_and_both_lists_reach_public_evaluation_and_review(self):
        raw = mixed_response(); before = copy.deepcopy(raw)
        audit, public, client = operating_response(raw)
        result = audit['result']; saved_audit = copy.deepcopy(audit)
        self.assertEqual(raw, before)
        self.assertEqual(result['audit'][0]['parsed_response'], before)
        self.assertEqual(result['decision'], 'external_material_required')
        self.assertEqual(result['draft_status'], 'code_checked_draft', result)
        self.assertEqual(result['generated_claims'], [{'text': ANSWER, 'citation_ids': [CID]}])
        self.assertEqual(public['response']['answer'], ORDINANCE_SCOPE_NOTICE+'\n\n'+ANSWER)
        self.assertEqual(public['response']['citation'], ['서울특별시 청년 기본 조례 제3조'])
        self.assertEqual(public['response']['source_url'],
                         'https://www.law.go.kr/ordinInfoP.do?ordinSeq=2054497')
        self.assertEqual(public['response']['effective_date'], '2025-07-14')
        for value in (result, public):
            self.assertEqual(value['missing_information'], FACTS)
            self.assertEqual(value['missing_materials'], MATERIALS)
            self.assertFalse(value['fully_resolved'])
            self.assertFalse(value['answer_allowed'])
            self.assertFalse(value['final_answer_generated'])
        self.assertEqual(public['external_material_status'], '외부 자료 확인 필요')
        self.assertEqual([kind for kind, _ in client.calls], ['draft', 'provision_check'])
        self.assertFalse(result['format_repair_used'])
        self.assertFalse(result['capacity_recovery_used'])
        self.assertEqual(evaluation.public_contract_errors(public, evaluation.SINGLE_RESPONSE_CONTRACT), [])
        self.assertEqual(evaluation.single_response_consistency_errors(public, result), [])
        snapshot = development.response_snapshot(audit, public, catalog={})
        self.assertEqual(snapshot['answer'], ANSWER)
        self.assertEqual(snapshot['claims'], result['generated_claims'])
        self.assertEqual(snapshot['missing_information'], FACTS)
        self.assertEqual(snapshot['missing_materials'], MATERIALS)
        reviewer = ReviewClient(gate=mixed_review_gate())
        review = development.review_response(audit, public, reviewer, catalog={})
        self.assertTrue(review['passed'], review)
        self.assertEqual([kind for kind, _ in reviewer.calls], ['gate', 'verify'])
        self.assertEqual(audit, saved_audit)

    def test_mixed_response_without_known_claims_preserves_lists_without_inventing_answer(self):
        raw = mixed_response(claims=[])
        audit, public, client = operating_response(raw)
        result = audit['result']
        self.assertEqual(result['decision'], 'external_material_required')
        self.assertEqual(result['draft_status'], 'not_generated')
        self.assertFalse(result['draft_generated'])
        self.assertFalse(result['request_more_evidence'])
        self.assertEqual(result['audit'][0]['parsed_response'], raw)
        self.assertEqual(public['status'], 'external_material_required')
        self.assertNotIn('response', public)
        self.assertEqual(public['missing_information'], FACTS)
        self.assertEqual(public['missing_materials'], MATERIALS)
        snapshot = development.response_snapshot(audit, public, catalog={})
        self.assertEqual(snapshot['claims'], [])
        self.assertEqual(snapshot['answer'], '')
        self.assertEqual([kind for kind, _ in client.calls], ['draft'])

    def test_invalid_mixed_lists_fail_before_normalization_can_publish_claims(self):
        cases = [('missing_information', [], 'clarification_without_missing_fact'),
                 ('missing_information', [' '], 'invalid_generation_missing_information'),
                 ('missing_information', FACTS*2, 'invalid_generation_missing_information'),
                 ('missing_materials', [' '], 'invalid_generation_missing_materials'),
                 ('missing_materials', MATERIALS*2, 'invalid_generation_missing_materials')]
        for field, value, error in cases:
            with self.subTest(field=field, value=value):
                raw = mixed_response(**{field: value})
                audit, public, client = operating_response(raw)
                self.assertEqual(audit['result'].get('error_code'), error, audit['result'])
                self.assertEqual(audit['result']['audit'][0]['parsed_response'], raw)
                self.assertNotIn('response', public)
                self.assertEqual(len(client.calls), 1)

    def test_mixed_state_does_not_bypass_citation_or_numeric_checks(self):
        cases = [([], ANSWER, 'held', 'claim_without_owned_citations'),
                 (['C999'], ANSWER, 'not_generated', 'response_unknown_enum_or_id'),
                 (['C001'], '청년은 70세 이하입니다.', 'released_with_check_issues', 'answer_contains_unsupported_number')]
        for ids, text, status, issue in cases:
            with self.subTest(issue=issue):
                audit, public, client = operating_response(mixed_response(
                    claims=[{'text': text, 'citation_ids': ids}]))
                result = audit['result']
                self.assertEqual(result['draft_status'], status, result)
                self.assertIn(issue, result['code_checks']['issues'])
                self.assertFalse(result['answer_allowed'])
                self.assertFalse(result['final_answer_generated'])
                if status == 'released_with_check_issues':
                    # The unsupported number alone no longer withholds the draft: it passes the
                    # provision check and is published with the mixed-state boundary intact.
                    self.assertEqual(result['code_checks']['status'], 'failed')
                    self.assertEqual(public['status'], 'code_checked_draft')
                    self.assertTrue(public['response']['answer'].endswith(text))
                    self.assertEqual(public['fully_resolved'], False)
                    self.assertEqual(len(client.calls), 2)
                else:
                    self.assertNotIn('response', public)
                    self.assertEqual(len(client.calls), 1)

    def test_review_can_reject_reasking_a_fact_despite_normalized_state(self):
        facts = ['나이', '실제 신청일']
        audit, public, _ = operating_response(mixed_response(missing_information=facts),
                                             question='나는 25세이다. '+QUESTION)
        original = copy.deepcopy(audit)
        gate = mixed_review_gate(facts)
        gate.update(response_handling_correct=False, missing_information_correct=False,
                    response_handling_issues=['질문에 제시한 나이를 다시 요구했습니다.'])
        reviewer = ReviewClient(gate=gate)
        review = development.review_response(audit, public, reviewer, catalog={})
        self.assertEqual(review['status'], 'completed', review)
        self.assertTrue(review['gate_decision_consistent'])
        self.assertFalse(review['gate_response_handling_passed'])
        self.assertFalse(review['passed'])
        self.assertEqual(public['missing_information'], facts)
        self.assertEqual(audit, original)
        self.assertEqual([kind for kind, _ in reviewer.calls], ['gate', 'verify'])

    def test_review_can_reject_unrelated_rule_without_rewriting_partial_answer(self):
        text = ANSWER+' 모든 시설에서도 같은 혜택을 받을 수 있습니다.'
        audit, public, _ = operating_response(mixed_response(
            claims=[{'text': text, 'citation_ids': ['C001']}]))
        original = copy.deepcopy(audit)

        def reject_unrelated_rule(answer, payload):
            answer['scope_correct'] = False
            answer['issues'] = ['질문과 무관한 시설의 혜택까지 확대했습니다.']
            answer['public_contract_review'].update(scope_correct=False,
                issues=['질문과 무관한 시설의 혜택까지 확대했습니다.'])

        reviewer = ReviewClient(gate=mixed_review_gate(), change=reject_unrelated_rule)
        review = development.review_response(audit, public, reviewer, catalog={})
        self.assertEqual(review['status'], 'completed', review)
        self.assertTrue(review['gate_decision_consistent'])
        self.assertTrue(review['gate_response_handling_passed'])
        self.assertFalse(review['public_contract_passed'])
        self.assertFalse(review['passed'])
        self.assertEqual(public['response']['answer'], ORDINANCE_SCOPE_NOTICE+'\n\n'+text)
        self.assertEqual(audit, original)
        self.assertEqual([kind for kind, _ in reviewer.calls], ['gate', 'verify'])


if __name__ == '__main__':
    unittest.main()
