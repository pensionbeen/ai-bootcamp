"""Synthetic development-review contracts only; no production/API execution."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import development_rag_review as development
import evaluate_local_rag as evaluation

CID='2054497:000300:all'
URL='https://www.law.go.kr/ordinInfoP.do?ordinSeq=2054497'
CONDITION='2054497:000300:condition:1'


def operating_response(two_claims=False):
    quote='  1. 청년은 19세 이상 39세 이하이다. 다만 다른 규정은 그에 따른다.\n'
    second='  2. 신청은 별도로 한다.\n'
    metadata={'ordin_seq':'2054497','ordin_name':'서울특별시 청년 기본 조례',
              'source_url':URL,'effective_date':'2025-07-14','as_of_date':'2026-10-01'}
    original={**metadata,'unit_id':CID,'parent_chunk_id':CID,'kind':'body','quote':quote+second,
              'source_unit_id':'2054497:000300','locator':{'article':'제3조','paragraph':None}}
    child={**original,'unit_id':CID+'::item::1','quote':quote,'source_origin_id':CID,
           'source_quote_span':{'start':0,'end':len(quote)},'locator':{'article':'제3조','paragraph':None,'item':'1'}}
    other={**original,'unit_id':CID+'::item::2','quote':second,'source_origin_id':CID,
           'source_quote_span':{'start':len(quote),'end':len(quote+second)},
           'locator':{'article':'제3조','paragraph':None,'item':'2'}}
    claims=[{'text':'청년은 19세 이상 39세 이하이다. 다만 다른 규정은 그에 따른다.',
             'citation_ids':[child['unit_id']]}]
    if two_claims:claims.append({'text':'신청은 별도로 한다.','citation_ids':[other['unit_id']]})
    condition={'condition':{'condition_id':CONDITION,'kind':'application','quote':quote+second,
                            'current_target_ids':[],'required_facts':[],'interpretation':'합성 감사용 해석'},
               'source':metadata,'source_unit_id':'2054497:000300','owners':{CID:{'same_source_unit':True}}}
    inspection={'purpose':'completeness_checks_only','texts':{'R1':quote+second},
                'condition_checks':[{**copy.deepcopy(condition),'gate_status':'not_assessed'}],
                'mandatory_contexts':[],'may_supply_claim_or_calculation_citations':False}
    prepared={'as_of_date':'2026-10-01','candidate_scope':'provided_retrieval_only','expansion_attempted':False,
              'primary_units':[{'unit_id':CID,'kind':'body','text':quote+second,'source':metadata,
                                'locator':original['locator'],'dependency_primary_ids':[]}],
              'contexts':[],'condition_candidates':[condition],'external_tasks':{},
              'local_reference':{'requested':[],'resolved':[],'unresolved':[]}}
    body='\n\n'.join(c['text'] for c in claims)
    result={'decision':'supported','draft_status':'code_checked_draft','code_checks':{'status':'passed'},
            'semantic_verification':'not_run','answer_allowed':False,'final_answer_generated':False,
            'draft_response':{'answer':body,'citation':[child,other]},'generated_claims':claims,
            'generated_calculations':[],'claim_joiner':'\n\n',
            'claim_segments':{'claim_'+str(i+1):c['text'] for i,c in enumerate(claims)},
            'source_citations':[original],'condition_sources':[condition],'prepared_evidence':prepared,
            'verification_only':inspection,'selected_ids':[CID],'citation_contract':'exact_source_subunits_v1',
            'missing_information':[],'missing_materials':[],'answer_scope':'ordinance_only','fully_resolved':True}
    audit={'status':'completed','question':'청년 정의와 단서는?','as_of_date':'2026-10-01','result':result,
           'code':{'sha256':'synthetic-runtime'},'database':{'database_sha256':'synthetic-db'},'called':0}
    label='서울특별시 청년 기본 조례 제3조'
    public={'status':'code_checked_draft','as_of_date':'2026-10-01','answer_allowed':False,
            'final_answer_generated':False,'called':0,'answer_scope':'ordinance_only',
            'response':{'answer':body,'citation':[label],'source_url':URL,'effective_date':'2025-07-14'},
            'sources':[{'citation':label,'source_url':URL,'effective_date':'2025-07-14'}]}
    return audit,public


def gate_answer(decision='supported'):
    return {'decision':decision,'reason':'동일 질문의 제공 근거를 검토함','selected_ids':[CID],
            'condition_review':{CONDITION:{'status':'general_statement_only','reason':'정의와 단서를 검토함'}},
            'missing_information':[],'missing_materials':[],
            'response_handling_correct':True,'missing_information_correct':True,
            'missing_materials_correct':True,'response_handling_issues':[]}


def declare_selection_contract(audit):
    result=audit['result'];aliases={}
    for index,row in enumerate(result['draft_response']['citation'],1):aliases['C'+str(index).zfill(3)]=row['unit_id']
    inverse={uid:alias for alias,uid in aliases.items()};raw={};trace={}
    for key in ('claims','calculations'):
        raw[key]=[];trace[key]=[]
        for generated in result['generated_'+key]:
            direct=[inverse[uid] for uid in generated['citation_ids']]
            raw[key].append({**copy.deepcopy(generated),'citation_ids':direct,'context_selections':[]})
            trace[key].append({'citation_aliases':direct,'context_selections':[],
                               'resolved_context_citations':[],'citation_ids':list(generated['citation_ids'])})
    result.update(source_selection_contract='structured_context_selection_v1',context_source_catalog={},
                  source_selection_trace=trace,citation_aliases=aliases,audit=[{'stage':'draft','parsed_response':raw}])
    result['code_checks']['locator_proofs']=[]
    return audit


def verify_answer(payload):
    data=json.loads(payload['messages'][1]['content'])
    result={key:True for key in ('supported','complete','citations_sufficient','conditions_satisfied',
                                'scope_correct','units_correct','calculation_correct')}
    result.update(issues=[],claims={key:{'supported':True,'reason':'원래 인용과 문장을 대조함',
                   'citation_ids':list(data['operating_claim_citations'][key])} for key in data['claim_segments']},
                  calculations=copy.deepcopy(data['operating_calculations']))
    if 'external_boundary_preserved' in payload['response_format']['json_schema']['schema']['properties']:
        result['external_boundary_preserved']=True
    result['public_contract_review']={**{key:True for key in development.PUBLIC_REVIEW_FLAGS},'issues':[]}
    return result


class ReviewClient:
    def __init__(self,change=None,gate=None):self.calls=[];self.change=change;self.gate=gate
    def request(self,kind,payload):
        self.calls.append((kind,copy.deepcopy(payload)))
        if kind=='gate':return copy.deepcopy(self.gate or gate_answer()),{'called':1,'cached':False}
        if kind!='verify':raise AssertionError('development must never generate a draft')
        answer=verify_answer(payload)
        if self.change:self.change(answer,payload)
        return answer,{'called':1,'cached':False}


class DevelopmentReviewTest(unittest.TestCase):
    def test_snapshot_accepts_known_partial_answer_without_treating_notice_as_a_claim(self):
        from public_rag_response import CLARIFICATION_NOTICE
        audit,public=operating_response();result=audit['result']
        result.update(decision='needs_clarification',missing_information=['취소 사유'],fully_resolved=False)
        public.update(clarification_required=True,missing_information=['취소 사유'],fully_resolved=False)
        public['response']['answer']=CLARIFICATION_NOTICE+'\n\n'+result['draft_response']['answer']
        snapshot=development.response_snapshot(audit,public)
        self.assertEqual(snapshot['answer'],result['draft_response']['answer'])
        self.assertEqual(snapshot['claims'],result['generated_claims'])
        public['fully_resolved']=True
        with self.assertRaisesRegex(ValueError,'clarification_boundary_lost'):
            development.response_snapshot(audit,public)

    @classmethod
    def setUpClass(cls):
        cls.original_runtime=development.runtime
        supplied=os.environ.get('RAG_REVIEW_TEST_RUNTIME')
        if supplied:
            # Explicit read-only test dependency; never a runtime fallback and
            # never copied into this feature's worktree.
            spec=importlib.util.spec_from_file_location('review_test_runtime',supplied)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            development.runtime=module

    @classmethod
    def tearDownClass(cls):development.runtime=cls.original_runtime

    def metadata_response(self):
        audit,public=operating_response();result=audit['result']
        result['generated_claims'][0]['text']='현재 정의(2026-10-01 기준)는 다음과 같다. '+result['generated_claims'][0]['text']
        text=result['generated_claims'][0]['text'];reference='2026-10-01 기준'
        result['draft_response']['answer']=text;result['claim_segments']={'claim_1':text}
        public['response']['answer']=text
        child=result['draft_response']['citation'][0]
        proof={'kind':'as_of_date','claim_index':0,
            'claim_span':{'start':text.index(reference),'end':text.index(reference)+len(reference)},
            'reference_text':reference,'metadata_field':'as_of_date','metadata_value':'2026-10-01',
            'source_bindings':[{'citation_id':child['unit_id'],'owner_primary_id':CID,
                'source_metadata':{key:child[key] for key in ('ordin_seq','source_url','effective_date','as_of_date')}}]}
        result['code_checks'].update(metadata_proof_contract='typed_source_metadata_spans_v1',metadata_proofs=[proof])
        declare_selection_contract(audit)
        return audit,public

    def test_missing_selector_field_is_allowed_only_for_empty_h_catalog(self):
        audit,public=operating_response();declare_selection_contract(audit)
        for row in audit['result']['audit'][0]['parsed_response']['claims']:row.pop('context_selections')
        review=development.review_response(audit,public,ReviewClient())
        self.assertTrue(review['passed'],review)
        self.assertEqual(audit['result']['source_selection_trace']['claims'][0]['context_selections'],[])

    def test_metadata_snapshot_and_verifier_replay_keep_original_proof(self):
        audit,public=self.metadata_response();before=copy.deepcopy(audit)
        snapshot=development.response_snapshot(audit,public)
        self.assertEqual(snapshot['metadata_proof_contract'],'typed_source_metadata_spans_v1')
        self.assertEqual(snapshot['metadata_proofs'],audit['result']['code_checks']['metadata_proofs'])
        self.assertIsNot(snapshot['metadata_proofs'],audit['result']['code_checks']['metadata_proofs'])
        review=development.review_response(audit,public,ReviewClient())
        self.assertTrue(review['passed'],review)
        self.assertEqual(review['verification']['metadata_proofs'],snapshot['metadata_proofs'])
        self.assertEqual(audit,before)
        snapshot['metadata_proofs'][0]['metadata_value']='changed'
        self.assertNotEqual(development.digest(snapshot),development.digest(development.response_snapshot(audit,public)))

    def test_invalid_metadata_proof_fails_before_development_calls(self):
        for mutation in ('unknown','missing_contract','missing_proofs','span','effective_date'):
            with self.subTest(mutation=mutation):
                audit,public=self.metadata_response();checks=audit['result']['code_checks']
                if mutation=='unknown':checks['metadata_proofs'][0]['kind']='effective_date'
                elif mutation=='missing_contract':checks.pop('metadata_proof_contract')
                elif mutation=='missing_proofs':checks.pop('metadata_proofs')
                elif mutation=='span':checks['metadata_proofs'][0]['claim_span']['end']+=1
                else:checks['metadata_proofs'][0]['source_bindings'][0]['source_metadata']['effective_date']='2026-10-01'
                client=ReviewClient();review=development.review_response(audit,public,client)
                self.assertFalse(review['passed']);self.assertEqual(client.calls,[])
                self.assertEqual(review['error_code'],'operating_source_integrity_failed')

    def test_metadata_replay_cannot_silently_drop_or_replace_proof(self):
        audit,public=self.metadata_response();original=development.runtime._verify_result
        def altered(*args,**kwargs):
            result=original(*args,**kwargs);result['metadata_proofs']=[];return result
        with patch.object(development.runtime,'_verify_result',side_effect=altered):
            review=development.review_response(audit,public,ReviewClient())
        self.assertFalse(review['passed'])
        self.assertEqual(review['error_code'],'development_metadata_proof_replay_changed')

    def test_typed_metadata_does_not_supply_amounts_effective_dates_or_operands(self):
        from test_evaluate_local_rag import metadata_proof_result
        for kind in ('attachment_label','as_of_date'):
            result=metadata_proof_result(kind);claim=result['generated_claims'][0]
            reviewed={key:True for key in ('supported','complete','citations_sufficient','conditions_satisfied',
                                          'scope_correct','units_correct','calculation_correct')}
            reviewed.update(issues=[],claims=[{**copy.deepcopy(claim),'supported':True,'reason':'합성 metadata 검사'}],calculations=[])
            def verify(value):
                return development.runtime._verify_result(value,value['claims'][0]['text'],
                    result['draft_response']['citation'],'품목 분류는?',source_citations=result['source_citations'])
            self.assertEqual(verify(reviewed)['status'],'passed')
            amount=copy.deepcopy(reviewed);amount['claims'][0]['text']+=' 요금은 2026원이다.'
            self.assertEqual(verify(amount)['status'],'failed')
            calculation=copy.deepcopy(reviewed);calculation['claims'][0]['text']+=' 계산값은 200이다.'
            calculation['calculations']=[{'expression':'2*100','result':'200','citation_ids':claim['citation_ids']}]
            self.assertEqual(verify(calculation)['status'],'failed')
            if kind=='as_of_date':
                effective=copy.deepcopy(reviewed);effective['claims'][0]['text']+=' 시행일을 뜻한다.'
                self.assertEqual(verify(effective)['status'],'failed')

    def test_new_selection_snapshot_pins_raw_trace_and_forwards_original_sources(self):
        audit,public=operating_response();declare_selection_contract(audit)
        snapshot=development.response_snapshot(audit,public)
        self.assertEqual(snapshot['source_selection_trace'],audit['result']['source_selection_trace'])
        self.assertEqual(snapshot['generation_selection_response'],audit['result']['audit'][0]['parsed_response'])
        self.assertEqual(snapshot['context_source_catalog'],{})
        self.assertEqual(snapshot['locator_proofs'],[])
        with patch.object(development.runtime,'_verify_result',wraps=development.runtime._verify_result) as verify:
            reviewed=development.review_response(audit,public,ReviewClient())
        self.assertTrue(reviewed['passed'],reviewed)
        self.assertEqual(verify.call_args.kwargs['source_citations'],audit['result']['source_citations'])
        self.assertEqual(reviewed['claims_sha256'],development.digest(audit['result']['generated_claims']))

    def test_raw_selection_mismatch_and_removed_new_contract_fail_before_paid_review(self):
        for mutation in ('trace','missing_contract','all_contract_fields'):
            with self.subTest(mutation=mutation):
                audit,public=operating_response();declare_selection_contract(audit);result=audit['result']
                if mutation=='trace':result['source_selection_trace']['claims'][0]['citation_aliases']=['C002']
                elif mutation=='missing_contract':result.pop('context_source_catalog')
                else:
                    for key in ('source_selection_contract','context_source_catalog','source_selection_trace'):result.pop(key)
                client=ReviewClient();reviewed=development.review_response(audit,public,client)
                self.assertFalse(reviewed['passed']);self.assertEqual(client.calls,[])
                self.assertEqual(reviewed['error_code'],'operating_source_integrity_failed')

    def test_new_selection_contract_does_not_allow_judge_to_rebind_citations(self):
        audit,public=operating_response(two_claims=True);declare_selection_contract(audit)
        def move(answer,payload):answer['claims']['claim_1']['citation_ids']=answer['claims']['claim_2']['citation_ids']
        reviewed=development.review_response(audit,public,ReviewClient(move))
        self.assertFalse(reviewed['passed']);self.assertEqual(reviewed['error_code'],'response_unknown_enum_or_id')

    def test_title_locator_proof_does_not_supply_prices_or_calculation_operands(self):
        from test_evaluate_local_rag import title_locator_proof_result
        result=title_locator_proof_result();claim=result['generated_claims'][0]
        reviewed={key:True for key in ('supported','complete','citations_sufficient','conditions_satisfied',
                                      'scope_correct','units_correct','calculation_correct')}
        reviewed.update(issues=[],claims=[{**copy.deepcopy(claim),'supported':True,'reason':'합성 표제 검사'}],calculations=[])
        def verify(value):
            return development.runtime._verify_result(value,value['claims'][0]['text'],
                result['draft_response']['citation'],'시설 기준은?',source_citations=result['source_citations'])
        self.assertEqual(verify(reviewed)['status'],'passed')
        amount=copy.deepcopy(reviewed);amount['claims'][0]['text']+=' 요금은 500원이다.'
        self.assertEqual(verify(amount)['status'],'failed')
        calculation=copy.deepcopy(reviewed)
        calculation['claims'][0]['text']+=' 6 곱하기 100은 600이다.'
        calculation['calculations']=[{'expression':'6*100','result':'600','citation_ids':claim['citation_ids']}]
        self.assertEqual(verify(calculation)['status'],'failed')

    def test_same_answer_claims_sources_and_gold_free_requests(self):
        audit,public=operating_response();before=copy.deepcopy(audit);client=ReviewClient()
        result=development.review_response(audit,public,client)
        self.assertTrue(result['passed'],result)
        self.assertEqual([kind for kind,_ in client.calls],['gate','verify'])
        self.assertEqual(audit,before)
        self.assertEqual(result['claims_sha256'],development.digest(audit['result']['generated_claims']))
        for _,payload in client.calls:
            self.assertEqual(payload['model'],'solar-pro4');self.assertEqual(payload['reasoning_effort'],'high')
            data=json.loads(payload['messages'][1]['content'])
            self.assertNotIn('expected_outcomes',data);self.assertNotIn('correctness_constraints',data)
        verify=json.loads(client.calls[1][1]['messages'][1]['content'])
        self.assertEqual(verify['draft_answer'],audit['result']['draft_response']['answer'])
        self.assertEqual(verify['verification_only'],audit['result']['verification_only'])
        self.assertTrue(result['model_verification_passed'])
        self.assertEqual(result['called'],2)

    def test_review_identity_includes_the_reused_source_integrity_checker(self):
        audit,public=operating_response()
        first=development.review_response(audit,public,ReviewClient())
        evaluator_path=Path(evaluation.__file__).resolve();read=Path.read_bytes
        def changed_dependency(path):
            return b'different source checker code' if path.resolve()==evaluator_path else read(path)
        with patch.object(Path,'read_bytes',changed_dependency):
            second=development.review_response(audit,public,ReviewClient())
        self.assertTrue(first['passed']);self.assertTrue(second['passed'])
        self.assertNotEqual(first['identity'],second['identity'])
        self.assertNotEqual(first['identity_components']['source_integrity_code_sha256'],
                            second['identity_components']['source_integrity_code_sha256'])
        self.assertEqual(first['answer_sha256'],second['answer_sha256'])
        self.assertEqual(first['sources_sha256'],second['sources_sha256'])

    def test_judge_cannot_move_citation_from_another_operating_claim(self):
        audit,public=operating_response(two_claims=True)
        def move(answer,payload):answer['claims']['claim_1']['citation_ids']=answer['claims']['claim_2']['citation_ids']
        result=development.review_response(audit,public,ReviewClient(move))
        self.assertFalse(result['passed']);self.assertEqual(result['error_code'],'response_unknown_enum_or_id')

    def test_judge_cannot_delete_an_original_citation_to_repair_the_answer(self):
        audit,public=operating_response();claims=audit['result']['generated_claims']
        claims[0]['citation_ids'].append(CID+'::item::2')
        def delete(answer,payload):answer['claims']['claim_1']['citation_ids']=answer['claims']['claim_1']['citation_ids'][:1]
        result=development.review_response(audit,public,ReviewClient(delete))
        self.assertFalse(result['passed']);self.assertEqual(result['error_code'],'development_judge_changed_operating_citations')

    def test_judge_cannot_introduce_a_new_calculation(self):
        audit,public=operating_response()
        def add(answer,payload):answer['calculations']=[{'expression':'19+39','result':'58','citation_ids':['Ca3.i1']}]
        result=development.review_response(audit,public,ReviewClient(add))
        self.assertFalse(result['passed']);self.assertEqual(result['error_code'],'development_judge_changed_operating_calculations')

    def test_judge_cannot_change_the_operating_calculation_result(self):
        audit,public=operating_response();result=audit['result']
        text='39에서 19를 빼면 20이다.'
        result['generated_claims'][0]['text']=text;result['claim_segments']={'claim_1':text}
        result['draft_response']['answer']=text;public['response']['answer']=text
        result['generated_calculations']=[{'expression':'39-19','result':'20','citation_ids':[CID+'::item::1']}]
        def change(answer,payload):answer['calculations'][0]['result']='21'
        reviewed=development.review_response(audit,public,ReviewClient(change))
        self.assertFalse(reviewed['passed'])
        self.assertEqual(reviewed['error_code'],'development_judge_changed_operating_calculations')

    def test_gate_disagreement_is_not_overwritten_by_positive_verify_flags(self):
        audit,public=operating_response();gate=gate_answer('needs_clarification')
        gate['missing_information']=['대상 사업']
        reviewed=development.review_response(audit,public,ReviewClient(gate=gate))
        self.assertFalse(reviewed['passed']);self.assertFalse(reviewed['gate_decision_consistent'])
        self.assertTrue(reviewed['model_verification_passed'])

    def test_unanswered_response_has_decision_review_without_a_phantom_verified_answer(self):
        audit,public=operating_response();result=audit['result']
        result.update(decision='out_of_scope',draft_status='not_generated',generated_claims=[],
                      generated_calculations=[],draft_response=None,selected_ids=[])
        public.update(status='out_of_scope',response={'answer':'확인 불가','citation':[]})
        gate=gate_answer('out_of_scope');gate['selected_ids']=[]
        gate['condition_review'][CONDITION]['status']='not_relevant'
        client=ReviewClient(gate=gate);reviewed=development.review_response(audit,public,client)
        self.assertTrue(reviewed['passed'],reviewed)
        self.assertIsNone(reviewed['model_verification_passed'])
        self.assertEqual(reviewed['review_kind'],'decision_review_without_generated_answer')
        self.assertEqual([kind for kind,_ in client.calls],['gate'])

    def test_same_clarification_decision_cannot_excuse_the_wrong_requested_fact(self):
        audit,public=operating_response();result=audit['result']
        result.update(decision='needs_clarification',draft_status='not_generated',generated_claims=[],
                      generated_calculations=[],draft_response=None,selected_ids=[],missing_information=['나이'])
        public.update(status='needs_clarification',missing_information=['나이']);public.pop('response')
        audit['question']='저는 65세입니다. 신청 여부는 어떻게 되나요?'
        gate=gate_answer('needs_clarification');gate.update(missing_information=['신청일'],
            response_handling_correct=False,missing_information_correct=False,
            response_handling_issues=['나이는 이미 질문에 제공됐고 필요한 신청일은 요청하지 않았다.'])
        reviewed=development.review_response(audit,public,ReviewClient(gate=gate))
        self.assertTrue(reviewed['gate_decision_consistent'])
        self.assertFalse(reviewed['gate_response_handling_passed']);self.assertFalse(reviewed['passed'])

    def test_failed_http_attempt_is_counted_even_without_a_receipt(self):
        class FailedClient:
            def __init__(self):self.called=7
            def request(self,kind,payload):
                self.called+=1;raise RuntimeError('network failed after request attempt')
        audit,public=operating_response();reviewed=development.review_response(audit,public,FailedClient())
        self.assertEqual(reviewed['error_code'],'gate_request_failed')
        self.assertEqual(reviewed['called'],1)
        self.assertEqual(reviewed['called_source'],'client_attempt_counter_delta')

    def test_semantic_rejections_do_not_trigger_generation_or_repair(self):
        for issue in ('필수 예외 누락','별표1 정의를 별표2에 전용','존재하지 않는 항을 표시'):
            audit,public=operating_response()
            audit['result']['verification']={'status':'passed','independent_model_review':{'supported':True}}
            def reject(answer,payload):
                answer.update(supported=False,issues=[issue])
                answer['public_contract_review'].update(supported=False,issues=[issue])
            client=ReviewClient(reject);result=development.review_response(audit,public,client)
            self.assertFalse(result['passed']);self.assertFalse(result['model_verification_passed'])
            self.assertEqual(len(client.calls),2)
            self.assertEqual(result['verification']['independent_model_review']['issues'],[issue])

    def test_nonempty_issues_reject_even_when_all_independent_flags_are_true(self):
        audit,public=operating_response()
        def reject(answer,payload):
            answer.update(issues=['잘못된 근거'])
            answer['public_contract_review']['issues']=['잘못된 근거']
        result=development.review_response(audit,public,ReviewClient(reject))
        self.assertFalse(result['passed']);self.assertFalse(result['model_verification_passed'])

    def test_public_contract_is_separate_from_immutable_claim_binding_diagnostics(self):
        audit,public=operating_response(two_claims=True);before=copy.deepcopy(audit)
        def misplaced(answer,payload):
            answer['claims']['claim_1']['supported']=False
            answer['issues']=['문장별 ID 배치는 부정확하지만 공개 선택 집합은 충분함']
        client=ReviewClient(misplaced);result=development.review_response(audit,public,client)
        self.assertTrue(result['passed'],result)
        self.assertTrue(result['public_contract_passed'])
        self.assertFalse(result['claim_binding_passed'])
        self.assertEqual(result['public_review_contract'],'whole_answer_selected_citations_v1')
        self.assertEqual(audit,before)
        self.assertEqual([kind for kind,_ in client.calls],['gate','verify'])

    def test_public_completeness_conditions_and_boundary_remain_required(self):
        for flag in development.PUBLIC_REVIEW_FLAGS:
            with self.subTest(flag=flag):
                audit,public=operating_response()
                def reject(answer,payload):answer['public_contract_review'][flag]=False
                result=development.review_response(audit,public,ReviewClient(reject))
                self.assertFalse(result['passed'])
                self.assertFalse(result['public_contract_passed'])
                self.assertTrue(result['claim_binding_passed'])

    def test_missing_public_review_cannot_default_to_passed(self):
        audit,public=operating_response()
        def omit(answer,payload):answer.pop('public_contract_review')
        result=development.review_response(audit,public,ReviewClient(omit))
        self.assertFalse(result['passed'])

    def test_original_public_answer_and_source_span_cannot_be_changed(self):
        for change in ('public','claim','source'):
            audit,public=operating_response();client=ReviewClient()
            if change=='public':public['response']['answer']+=' 새 사실'
            elif change=='claim':audit['result']['generated_claims'][0]['text']+=' 새 사실'
            else:audit['result']['draft_response']['citation'][0]['source_quote_span']['start']=1
            result=development.review_response(audit,public,client)
            self.assertFalse(result['passed'],change);self.assertFalse(client.calls,change)

    def test_public_citation_deletion_or_replacement_is_rejected_before_judge(self):
        for mutation in ('delete','article','date','url','response_only'):
            with self.subTest(mutation=mutation):
                audit,public=operating_response(two_claims=True)
                if mutation=='delete':
                    public['sources'].pop();public['response']['citation'].pop()
                elif mutation=='article':
                    public['sources'][0]['citation']='서울특별시 청년 기본 조례 제4조'
                    public['response']['citation'][0]=public['sources'][0]['citation']
                elif mutation=='date':public['sources'][0]['effective_date']='2026-01-01'
                elif mutation=='url':public['sources'][0]['source_url']='https://www.law.go.kr/wrong'
                else:public['response']['citation'].pop()
                client=ReviewClient();result=development.review_response(audit,public,client)
                self.assertFalse(result['passed']);self.assertEqual(client.calls,[])
                self.assertEqual(result['error_code'],'public_sources_differ_from_selected_citations')

    def test_output_limit_is_one_failed_request_without_budget_increase(self):
        class Limited:
            def __init__(self):self.calls=[]
            def request(self,kind,payload):
                self.calls.append((kind,payload))
                return {'choices':[{'finish_reason':'length','message':{'content':'','reasoning':'DO_NOT_EXPORT'}}],
                        'usage':{'completion_tokens':9000}}, {'called':1,'cached':False}
        audit,public=operating_response();client=Limited();result=development.review_response(audit,public,client)
        self.assertEqual(result['error_code'],'model_output_limit');self.assertEqual(len(client.calls),1)
        self.assertEqual(client.calls[0][1]['max_tokens'],9000)
        self.assertNotIn('DO_NOT_EXPORT',json.dumps(result));self.assertEqual(result['called'],1)

    def test_malformed_review_does_not_retry(self):
        audit,public=operating_response();client=ReviewClient(gate={'decision':'supported'})
        result=development.review_response(audit,public,client)
        self.assertFalse(result['passed']);self.assertEqual(len(client.calls),1)
        self.assertEqual(result['error_code'],'response_wrong_fields')

    def test_public_scope_notice_is_kept_separate_from_immutable_legal_claims(self):
        audit,public=operating_response();result=audit['result'];result.update(decision='external_material_required',
            fully_resolved=False,missing_materials=['별도 적용 규칙'],scope_notice={'text':'외부 자료 확인 필요'})
        public['response']['answer']='외부 자료 확인 필요\n\n'+result['draft_response']['answer']
        public.update(missing_information=[],missing_materials=['별도 적용 규칙'])
        gate=gate_answer('external_material_required');gate['missing_materials']=['별도 적용 규칙']
        reviewed=development.review_response(audit,public,ReviewClient(gate=gate))
        self.assertTrue(reviewed['passed'],reviewed)
        self.assertNotEqual(reviewed['answer_sha256'],development.digest(public['response']['answer']))

    def test_saved_audit_check_only_keeps_original_bytes_and_never_uses_network(self):
        audit,public=operating_response()
        class ReadOnlyClient(ReviewClient):
            def __init__(self,project,check_only=False):
                self.check_only=check_only;super().__init__();self.calls=[]
            def request(self,kind,payload):
                if not self.check_only:raise AssertionError('read-only required')
                body,meta=super().request(kind,payload);return body,{**meta,'called':0,'cached':True}
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path=Path(folder)/'audit.json';audit['output_paths']={'audit':str(path)}
            audit['run_identity']='synthetic-run'
            path.write_text(json.dumps(audit,ensure_ascii=False),encoding='utf-8');before=path.read_bytes()
            manifest=Path(folder)/'audit.manifest.json'
            manifest.write_text(json.dumps({'run_identity':audit['run_identity'],
                'audit_sha256':hashlib.sha256(before).hexdigest()}),encoding='utf-8')
            with patch('local_rag_client.Client',ReadOnlyClient):
                review=development.review_saved_audit(folder,audit,public,check_only=True)
            self.assertTrue(review['passed'],review);self.assertEqual(review['called'],0)
            self.assertEqual(path.read_bytes(),before);self.assertEqual(set(Path(folder).iterdir()),{path,manifest})
            for key in ('run_identity','code','database','as_of_date'):
                changed=copy.deepcopy(audit);changed[key]='different'
                with patch('local_rag_client.Client',ReadOnlyClient),self.assertRaisesRegex(ValueError,'saved_operating_response_changed'):
                    development.review_saved_audit(folder,changed,public,check_only=True)
            changed=copy.deepcopy(audit);changed['result']['source_citations'][0]['quote']+=' modified'
            path.write_text(json.dumps(changed),encoding='utf-8')
            with patch('local_rag_client.Client',ReadOnlyClient),self.assertRaisesRegex(ValueError,'saved_operating_audit_hash_mismatch'):
                development.review_saved_audit(folder,changed,public,check_only=True)

    def test_production_entrypoints_do_not_import_or_call_development_review(self):
        # This is a static boundary check of the worktree snapshot, not a claim
        # that the new runtime has been installed or executed in this worktree.
        for name in ('ask_local_rag.py','local_rag_answer.py','public_rag_response.py'):
            tree=ast.parse((ROOT/'src'/name).read_bytes())
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom):self.assertNotEqual(node.module,'development_rag_review',name)
                if isinstance(node,ast.Import):self.assertNotIn('development_rag_review',[n.name for n in node.names],name)


if __name__=='__main__':unittest.main()
