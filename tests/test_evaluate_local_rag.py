"""Synthetic contract failures and read-only evaluator boundaries; no API calls."""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import evaluate_local_rag as evaluation
import build_answer_evaluation_cases as cases


def public():
    label='서울특별시 청년 기본 조례 제3조'
    url='https://www.law.go.kr/ordinInfoP.do?ordinSeq=2054497'
    return {'as_of_date':'2026-10-01','answer_allowed':False,'final_answer_generated':False,
            'status':'verified_draft','called':0,
            'response':{'answer':'19세 이상 39세 이하. 별도 규정의 단서를 따릅니다.',
                        'citation':[label],'source_url':url,'effective_date':'2025-07-14'},
            'sources':[{'citation':label,'source_url':url,'effective_date':'2025-07-14'}]}


def fixture():
    cid='2054497:000300:all'; quote='  1. 테스트 청년 정의와 단서.\n  2. 다른 정의.\n'
    return {'id':'synthetic','question':'합성 청년 정의 질문','expected_decision':'supported','expected_draft_status':'verified_draft',
            'expected_primary_id':cid,'source_anchors':[{'chunk_id':cid,'quote':quote,
                'source_url':'https://www.law.go.kr/ordinInfoP.do?ordinSeq=2054497','effective_date':'2025-07-14',
                'ordin_seq':'2054497','_validated_locator':{'article':'제3조','paragraph':None}}],
            'required_citations':[cases.cite('y','제3조',item='1')],
            'answer_checks':[r'19\s*세',r'39\s*세'], 'correctness_constraints':['synthetic only']}


def audit():
    anchor=fixture()['source_anchors'][0];child_id=anchor['chunk_id']+'::item::1'
    quote='  1. 테스트 청년 정의와 단서.\n'
    source={'unit_id':anchor['chunk_id'],'kind':'body','quote':anchor['quote'],
            'parent_chunk_id':anchor['chunk_id'],'ordin_seq':'2054497',
            'source_url':anchor['source_url'],'effective_date':anchor['effective_date'],
            'locator':{'article':'제3조','paragraph':None}}
    child={**source,'unit_id':child_id,'quote':quote,'source_origin_id':anchor['chunk_id'],
           'source_quote_span':{'start':0,'end':len(quote)},'locator':{'article':'제3조','paragraph':None,'item':'1'}}
    return {'status':'completed','result':{'decision':'supported','draft_status':'verified_draft',
            'answer_allowed':False,'final_answer_generated':False,
            'selected_ids':[anchor['chunk_id']], 'source_citations':[source],
            'verification':{'independent_model_review':{'claims':[{'supported':True,'citation_ids':[child_id]}]}},
            'draft_response':{'citation':[child]}}}


def note_case_and_audit(owner_suffix='r2'):
    first='2147709:table:row:r1';second='2147709:table:row:r2'
    owner=first if owner_suffix=='r1' else second
    context_id='ctx-synthetic-age-note';quote='청소년: 13세 이상 18세 이하. 다른 표에 자동 적용하지 않는다.'
    metadata={'ordin_seq':'2147709','source_url':'https://www.law.go.kr/ordinInfoP.do?ordinSeq=2147709',
              'effective_date':'2026-07-13','as_of_date':'2026-10-01'}
    refs=[{'attachment_id':'2147709:table','page':1,'element_id':2,'parse_content_hash':'synthetic-parse'}]
    anchor={**metadata,'chunk_id':first,'kind':'attachment','quote':'합성 동물원 요금행',
            '_validated_kind':'attachment','_validated_locator':{'article':None}}
    context={'context_id':context_id,'quote':quote,'quote_sha256':evaluation.digest(quote.encode()),
             '_validated_owners':{first:metadata,second:metadata},'_validated_source_refs':refs}
    case={'expected_primary_id':first,'source_anchors':[anchor],'context_anchors':[context],
          'required_citations':[cases.cite('c',attachment='별표 1')]}
    original={**metadata,'unit_id':owner+'::context::'+context_id,'kind':'required_context',
              'parent_chunk_id':owner,'source_unit_id':context_id,'quote':quote,'locator':{'source_refs':copy.deepcopy(refs)}}
    row=copy.deepcopy(original)
    value={'result':{'draft_status':'scope_limited_draft','selected_ids':[owner],
           'source_citations':[original],'draft_response':{'citation':[row]},
           'verification':{'independent_model_review':{'claims':[{'supported':True,'citation_ids':[row['unit_id']]}]}}}}
    return case,value


def materialized_context_audit(paragraph='1',item='2'):
    heading='제20조(합성 규정)\n';intro=' ① 다음 사람은 면제할 수 있다.\n'
    first='  1. 첫 번째 사람.\n';second='  2. 두 번째 사람. 다만 예외는 보존한다.\n';plain=' ② 두 감면은 중복하지 않는다.\n'
    p1=intro+first+second;p3=' ③ 합성 원 소유 항.\n';text=heading+p1+plain+p3
    owner='synthetic:002000:3';origin=owner+'::context::synthetic:002000'
    metadata={'ordin_seq':'synthetic','source_url':'https://www.law.go.kr/ordinInfoP.do?ordinSeq=synthetic',
              'effective_date':'2026-07-13','as_of_date':'2026-10-01','ordin_name':'서울특별시 합성 조례'}
    source={**metadata,'unit_id':origin,'parent_chunk_id':owner,'source_unit_id':'synthetic:002000',
            'kind':'required_context','quote':text,'locator':{'article':'제20조'},'source_segments':[
                {'kind':'heading','start':0,'end':len(heading),'text':heading},
                {'kind':'provision','paragraph':1,'start':len(heading),'end':len(heading+p1),'text':p1},
                {'kind':'provision','paragraph':2,'start':len(heading+p1),'end':len(heading+p1+plain),'text':plain},
                {'kind':'provision','paragraph':3,'start':len(heading+p1+plain),'end':len(text),'text':p3}]}
    primary={**metadata,'unit_id':owner,'parent_chunk_id':owner,'source_unit_id':'synthetic:002000',
             'kind':'body','quote':p3,'locator':{'article':'제20조','paragraph':3}}
    spans=[(len(heading),len(heading+intro),None)]
    if paragraph=='2':spans=[(len(heading+p1),len(heading+p1+plain),None)]
    elif item:spans.append((len(heading+intro+first),len(heading+p1),'2'))
    children=[]
    for start,end,label in spans:
        child={**copy.deepcopy(source),'unit_id':origin+'::paragraph::'+paragraph+('::item::'+label if label else '::intro'),
               'quote':text[start:end],'source_origin_id':origin,'source_quote_span':{'start':start,'end':end},
               'locator':{'article':'제20조','paragraph':int(paragraph)},'context_only':False}
        if label:child['locator']['item']=label
        child.pop('source_segments');children.append(child)
    ids=[row['unit_id'] for row in children];selector={'context_id':'H001','paragraph':paragraph,'item':item}
    raw={'claims':[{'text':'합성 규정의 조건과 예외를 따른다.','citation_ids':[],
                    'context_selections':[selector]}],'calculations':[]}
    result={'source_selection_contract':'structured_context_selection_v1','context_source_catalog':{'H001':copy.deepcopy(source)},
            'source_selection_trace':{'claims':[{'citation_aliases':[],'context_selections':[selector],
                'resolved_context_citations':[ids],'citation_ids':ids}],'calculations':[]},
            'audit':[{'stage':'draft','parsed_response':copy.deepcopy(raw)}],
            'source_citations':[source,primary],'citation_aliases':{},'citation_contract':'exact_source_subunits_v1',
            'generated_claims':[{'text':raw['claims'][0]['text'],'citation_ids':ids}],
            'generated_calculations':[],'draft_response':{'citation':children},'code_checks':{'status':'passed'},
            'selected_ids':[owner],'draft_status':'code_checked_draft'}
    anchor={**metadata,'chunk_id':'synthetic:002000:1','quote':p1,
            '_validated_locator':{'article':'제20조','paragraph':1},
            '_validated_source_span':{'start':len(heading),'end':len(heading+p1)}}
    return {'result':result},{'expected_primary_id':anchor['chunk_id'],'source_anchors':[anchor]}


def body_leaf_context_audit(with_items=False):
    owner='synthetic:table:row';context='ctx-body-paragraph';origin=owner+'::context::'+context
    text=' ② 설치제한지역의 기준은 별표 3과 같다.\n'
    if with_items:text=' ② 다음 요건을 따른다.\n  1. 첫 요건.\n  2. 둘째 요건. 다만 예외는 보존한다.\n'
    metadata={'ordin_seq':'synthetic','source_url':'https://www.law.go.kr/ordinInfoP.do?ordinSeq=synthetic',
              'effective_date':'2026-07-13','as_of_date':'2026-10-01','ordin_name':'서울특별시 합성 조례'}
    raw={**metadata,'chunk_id':'synthetic:002100:2','parent_id':'synthetic:002100','article':'제21조',
         'paragraph':2,'quote_text':text,'source_span':{'start':83,'end':83+len(text)},
         'is_deleted':False,'source_review':'pending'}
    proof={'contract':'single_body_paragraph_v1','context_id':context,'origin_id':raw['chunk_id'],
           'source_record':copy.deepcopy(raw)}
    source={**metadata,'unit_id':origin,'parent_chunk_id':owner,'source_unit_id':context,
            'kind':'required_context','quote':text,'locator':{'article':'제21조','paragraph':2,
              'source_span':copy.deepcopy(raw['source_span'])},'body_context_provenance':proof,
            'source_segments':[{'kind':'provision','paragraph':2,'start':0,'end':len(text),'text':text}]}
    primary={**metadata,'unit_id':owner,'parent_chunk_id':owner,'kind':'attachment',
             'quote':'합성 시설 기준 표행','locator':{'attachment_id':'synthetic:table'}}
    spans=[(0,len(text),None)]
    if with_items:spans=[(0,text.index('  1.'),None),(text.index('  2.'),len(text),'2')]
    children=[]
    for left,right,item in spans:
        child={**copy.deepcopy(source),'unit_id':origin+'::paragraph::2'+('::item::'+item if item else '::intro'),
               'quote':text[left:right],'source_origin_id':origin,
               'source_quote_span':{'start':left,'end':right},'context_only':False,
               'locator':{'article':'제21조','paragraph':2,'source_span':{'start':83+left,'end':83+right}}}
        if item:child['locator']['item']=item
        child.pop('source_segments');children.append(child)
    ids=[row['unit_id'] for row in children]
    selector={'context_id':'H001','paragraph':'2','item':'2' if with_items else ''}
    raw_answer={'claims':[{'text':'합성 규정의 조건과 예외를 따른다.','citation_ids':[],
                          'context_selections':[selector]}],'calculations':[]}
    result={'source_selection_contract':'structured_context_selection_v1',
            'context_source_catalog':{'H001':copy.deepcopy(source)},'citation_aliases':{},
            'source_selection_trace':{'claims':[{'citation_aliases':[],'context_selections':[selector],
                'resolved_context_citations':[ids],'citation_ids':ids}],'calculations':[]},
            'source_citations':[source,primary],'citation_contract':'exact_source_subunits_v1',
            'prepared_evidence':{'contexts':[{'context_id':context,'text':text,'source':metadata,
                'locator':copy.deepcopy(source['locator']),'body_context_provenance':copy.deepcopy(proof)}]},
            'audit':[{'stage':'draft','parsed_response':raw_answer}],
            'generated_claims':[{'text':raw_answer['claims'][0]['text'],'citation_ids':ids}],
            'generated_calculations':[],'draft_response':{'citation':children},
            'selected_ids':[owner],'draft_status':'code_checked_draft','code_checks':{'status':'passed'}}
    anchor={**metadata,'chunk_id':raw['chunk_id'],'quote':text,'_validated_source_span':raw['source_span'],
            '_validated_locator':{'article':'제21조','paragraph':2}}
    return {'result':result},{'expected_primary_id':raw['chunk_id'],'source_anchors':[anchor]}


def title_locator_proof_result():
    quote='[별표 2]\n시설 기준(제6조 제1항 관련)\n본문 요금 100원.'
    text='시설 기준(제6조제1항 관련)을 설명한다.';reference='제6조제1항';source_reference='제6조 제1항'
    metadata={'parent_chunk_id':'fee-row','ordin_seq':'s','source_url':'https://www.law.go.kr/s',
              'effective_date':'2026-01-01','as_of_date':'2026-10-01'}
    refs=[{'attachment_id':'annex','parse_file':'synthetic.json','parse_content_hash':'capture'}]
    source_span={'start':quote.index(source_reference),'end':quote.index(source_reference)+len(source_reference)}
    source={**metadata,'unit_id':'fee-row::context::title','kind':'required_context','quote':quote,
            'locator':{'source_refs':refs},'title_context_kind':'ordered_attachment_text_across_pages',
            'title_locator_spans':[{'article':'제6조','paragraph':1,'reference_text':source_reference,'source_quote_span':source_span}]}
    row={**metadata,'unit_id':'fee-row','kind':'attachment','quote':'시설 기준: 요금 100원',
         'locator':{'attachment_id':'annex','source_refs':copy.deepcopy(refs)}}
    proof={'claim_index':0,'claim_span':{'start':text.index(reference),'end':text.index(reference)+len(reference)},
           'reference_text':reference,'attachment_citation_id':'fee-row','source_context_id':source['unit_id'],
           'source_quote_span':source_span,'source_quote_sha256':evaluation.digest(quote.encode()),'article':'제6조','paragraph':1}
    return {'source_citations':[source,copy.deepcopy(row)],'draft_response':{'citation':[row]},
            'generated_claims':[{'text':text,'citation_ids':['fee-row']}],'code_checks':{'locator_proofs':[proof]}}


def typo_title_locator_proof_result():
    quote='[별표 3]\n시설 기준(제21제2항 관련)\n본문 요금 100원.'
    text='시설 기준(제21조제2항 관련)을 설명한다.';reference='제21조제2항';source_reference='제21제2항'
    result=title_locator_proof_result();source=result['source_citations'][0];proof=result['code_checks']['locator_proofs'][0]
    source_span={'start':quote.index(source_reference),'end':quote.index(source_reference)+len(source_reference)}
    source.update(quote=quote,title_locator_spans=[{'article':'제21조','paragraph':2,'reference_text':source_reference,
        'source_quote_span':source_span,'normalized':'missing_article_suffix'}])
    result['generated_claims'][0]['text']=text
    proof.update(claim_span={'start':text.index(reference),'end':text.index(reference)+len(reference)},
                 reference_text=reference,source_quote_span=source_span,source_quote_sha256=evaluation.digest(quote.encode()),
                 article='제21조',paragraph=2,normalized='missing_article_suffix')
    return result


def metadata_proof_result(kind='attachment_label'):
    value=title_locator_proof_result();source,owner=value['source_citations']
    note={**copy.deepcopy(owner),'unit_id':'fee-row::context::note','kind':'required_context',
          'quote':'품목별 등급은 이 표에서 정한 분류에 따른다.'}
    value['source_citations'].append(copy.deepcopy(note))
    value['draft_response']['citation']=[note]
    fields=('ordin_seq','source_url','effective_date','as_of_date')
    metadata={key:owner[key] for key in fields}
    if kind=='attachment_label':
        text='별표 2의 품목별 등급 정의를 따른다.';reference='별표 2'
        proof={'kind':kind,'claim_index':0,'claim_span':{'start':0,'end':len(reference)},
            'reference_text':reference,'citation_id':note['unit_id'],'owner_primary_id':owner['unit_id'],
            'attachment_id':'annex','source_context_id':source['unit_id'],
            'source_quote_span':{'start':1,'end':1+len(reference)},
            'source_quote_sha256':evaluation.digest(source['quote'].encode()),'source_metadata':metadata}
    else:
        text='현재 품목별 등급(2026-10-01 기준)을 설명한다.';reference='2026-10-01 기준'
        proof={'kind':kind,'claim_index':0,
            'claim_span':{'start':text.index(reference),'end':text.index(reference)+len(reference)},
            'reference_text':reference,'metadata_field':'as_of_date','metadata_value':'2026-10-01',
            'source_bindings':[{'citation_id':note['unit_id'],'owner_primary_id':owner['unit_id'],
                                'source_metadata':metadata}]}
    value['generated_claims']=[{'text':text,'citation_ids':[note['unit_id']]}]
    value['code_checks']={'metadata_proof_contract':'typed_source_metadata_spans_v1','metadata_proofs':[proof]}
    return value


class EvaluationContractTest(unittest.TestCase):
    def test_typed_metadata_proof_accepts_only_exact_original_label_bindings(self):
        for kind in ('attachment_label','as_of_date'):
            value=metadata_proof_result(kind)
            self.assertEqual(evaluation.metadata_proof_errors(value),[])
            self.assertEqual(evaluation.runtime_source_errors({'result':value},[]),[])

    def test_metadata_proof_contract_unknown_kind_and_duplicate_span_are_rejected(self):
        for mutation in ('missing_contract','missing_proofs','unknown_kind','extra_field','duplicate','claim_text'):
            with self.subTest(mutation=mutation):
                value=metadata_proof_result();checks=value['code_checks'];proof=checks['metadata_proofs'][0]
                if mutation=='missing_contract':checks.pop('metadata_proof_contract')
                elif mutation=='missing_proofs':checks.pop('metadata_proofs')
                elif mutation=='unknown_kind':proof['kind']='amount'
                elif mutation=='extra_field':proof['amount']='500'
                elif mutation=='duplicate':checks['metadata_proofs'].append(copy.deepcopy(proof))
                else:value['generated_claims'][0]['text']='별표 3의 품목별 등급 정의를 따른다.'
                self.assertTrue(evaluation.metadata_proof_errors(value))

    def test_annex_metadata_cannot_prove_amount_or_other_capture_version(self):
        for mutation in ('amount','uncited','owner','date','capture','attachment','body_heading','span','hash','note_text','ambiguous_heading'):
            with self.subTest(mutation=mutation):
                value=metadata_proof_result();proof=value['code_checks']['metadata_proofs'][0]
                source=value['source_citations'][0]
                if mutation=='amount':
                    value['generated_claims'][0]['text']='500원';proof.update(reference_text='500원',claim_span={'start':0,'end':4})
                elif mutation=='uncited':value['generated_claims'][0]['citation_ids']=['fee-row']
                elif mutation=='owner':proof['owner_primary_id']='other-owner'
                elif mutation=='date':source['effective_date']='2025-01-01'
                elif mutation=='capture':source['locator']['source_refs'][0]['parse_content_hash']='other'
                elif mutation=='attachment':proof['attachment_id']='other-annex'
                elif mutation=='body_heading':source['title_context_kind']='table_note'
                elif mutation=='span':proof['source_quote_span']['end']+=1
                elif mutation=='hash':proof['source_quote_sha256']='changed'
                elif mutation=='note_text':value['draft_response']['citation'][0]['quote']='바뀐 비고 원문'
                else:
                    source['quote']=source['quote'].replace('[별표 2]','[별표 2] [별표 3]')
                    proof['source_quote_sha256']=evaluation.digest(source['quote'].encode())
                self.assertTrue(evaluation.metadata_proof_errors(value))

    def test_asof_metadata_is_parenthesized_platform_scope_and_all_used_sources(self):
        for mutation in ('effective','unparenthesized','date','binding_missing','extra_citation','owner_date','row_date','binding_order'):
            with self.subTest(mutation=mutation):
                value=metadata_proof_result('as_of_date');proof=value['code_checks']['metadata_proofs'][0]
                if mutation=='effective':value['generated_claims'][0]['text']+=' 시행일을 뜻한다.'
                elif mutation=='unparenthesized':value['generated_claims'][0]['text']=value['generated_claims'][0]['text'].replace('(',' ').replace(')',' ')
                elif mutation=='date':proof['metadata_value']='2025-10-01'
                elif mutation=='binding_missing':proof['source_bindings']=[]
                elif mutation=='extra_citation':value['generated_claims'][0]['citation_ids'].append('fee-row')
                elif mutation=='owner_date':value['source_citations'][1]['as_of_date']='2025-10-01'
                elif mutation=='row_date':value['draft_response']['citation'][0]['as_of_date']='2025-10-01'
                else:proof['source_bindings'][0]['owner_primary_id']='other-owner'
                self.assertTrue(evaluation.metadata_proof_errors(value))

    def test_selection_check_reads_stored_sequence_and_new_address_aliases_alike(self):
        # Stored audits use C001; new runs name a unit by its own 조/항/호 address.
        def direct(alias,mapped=True):
            value,_=materialized_context_audit();result=value['result']
            first=result['draft_response']['citation'][0]['unit_id']
            result['citation_aliases']={alias:first} if mapped else {}
            result['audit'][0]['parsed_response']['claims'][0]['citation_ids']=[alias]
            result['source_selection_trace']['claims'][0]['citation_aliases']=[alias]
            return result
        for alias in ('C001','Ca20.p1','Ca20.p1.i2','Ca12-3.p2.i7-3','CA.a20.i2','CB.a20','Ca5'):
            self.assertEqual(evaluation.source_selection_errors(direct(alias)),[],alias)
        for alias in ('O001','H001','Ca0.p1','Ca20.p01','C20.1.2','Cab','CA.001','ca20'):
            self.assertEqual(evaluation.source_selection_errors(direct(alias)),
                             ['source_selection_direct_alias_changed'],alias)
        self.assertEqual(evaluation.source_selection_errors(direct('Ca20.p1',mapped=False)),
                         ['source_selection_direct_alias_changed'])

    def test_selector_omission_is_not_allowed_when_real_h_catalog_exists(self):
        value,_=materialized_context_audit()
        value['result']['audit'][0]['parsed_response']['claims'][0].pop('context_selections')
        self.assertTrue(evaluation.source_selection_errors(value['result']))

    def test_context_selector_uses_exact_sibling_source_and_preserves_anchor_contract(self):
        value,case=materialized_context_audit()
        self.assertEqual(evaluation.runtime_source_errors(value,[]),[])
        self.assertTrue(evaluation.anchor_in_selected(case,value,evaluation.SINGLE_RESPONSE_CONTRACT))
        for paragraph,item in [('1',''),('2','')]:
            value,_=materialized_context_audit(paragraph,item)
            self.assertEqual(evaluation.runtime_source_errors(value,[]),[])

    def test_body_leaf_h_preserves_absolute_db_anchor_and_local_item_spans(self):
        for with_items in (False,True):
            with self.subTest(with_items=with_items):
                value,case=body_leaf_context_audit(with_items)
                self.assertEqual(evaluation.runtime_source_errors(value,case['source_anchors']),[])
                self.assertTrue(evaluation.anchor_in_selected(case,value,evaluation.SINGLE_RESPONSE_CONTRACT))
                record=value['result']['source_citations'][0]['body_context_provenance']['source_record']
                self.assertEqual(record['source_review'],'pending')

    def test_explicit_unnumbered_body_record_is_valid_without_inventing_a_paragraph(self):
        value,_=body_leaf_context_audit();source=value['result']['source_citations'][0]
        record=source['body_context_provenance']['source_record']
        record.update(paragraph=None,chunk_id=record['parent_id']+':all')
        source['body_context_provenance']['origin_id']=record['chunk_id']
        source['locator']['paragraph']=None;source['source_segments'][0]['paragraph']=None
        self.assertEqual(evaluation._body_context_record(source),record)
        for paragraph in (True,0,-1,'1'):
            modified=copy.deepcopy(source)
            modified['body_context_provenance']['source_record']['paragraph']=paragraph
            self.assertIsNone(evaluation._body_context_record(modified))
        record.pop('paragraph')
        self.assertIsNone(evaluation._body_context_record(source))

    def test_body_record_suffix_cannot_disagree_with_explicit_paragraph(self):
        value,_=body_leaf_context_audit();source=value['result']['source_citations'][0]
        proof=source['body_context_provenance']
        proof['origin_id']=proof['source_record']['chunk_id']='synthetic:002100:all'
        self.assertIsNone(evaluation._body_context_record(source))

    def test_body_leaf_h_rejects_unknown_provenance_or_changed_child_lineage(self):
        mutations=('contract','body_id','raw_quote','deleted','paragraph','source_span','segment','item',
                   'missing_original_proof','missing_prepared_proof','prepared_review_status',
                   'missing_child_proof','child_review_status','child_absolute_span','child_origin','child_owner')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                value,case=body_leaf_context_audit(True);result=value['result']
                source=result['source_citations'][0];proof=source['body_context_provenance']
                child=result['draft_response']['citation'][-1]
                if mutation=='contract':proof['contract']='unknown_v1'
                elif mutation=='body_id':proof['origin_id']='synthetic:002100:3'
                elif mutation=='raw_quote':proof['source_record']['quote_text']=source['quote'].rstrip()
                elif mutation=='deleted':proof['source_record']['is_deleted']=True
                elif mutation=='paragraph':proof['source_record']['paragraph']=1
                elif mutation=='source_span':proof['source_record']['source_span']['start']+=1
                elif mutation=='segment':source['source_segments'][0]['start']=1
                elif mutation=='item':proof['source_record']['item']='2'
                elif mutation=='missing_original_proof':source.pop('body_context_provenance')
                elif mutation=='missing_prepared_proof':result['prepared_evidence']['contexts'][0].pop('body_context_provenance')
                elif mutation=='prepared_review_status':result['prepared_evidence']['contexts'][0]['body_context_provenance']['source_record']['source_review']='verified'
                elif mutation=='missing_child_proof':child.pop('body_context_provenance')
                elif mutation=='child_review_status':child['body_context_provenance']['source_record']['source_review']='verified'
                elif mutation=='child_absolute_span':child['locator']['source_span']['start']+=1
                elif mutation=='child_origin':child['source_origin_id']='other-context'
                else:child['parent_chunk_id']='other-owner'
                result['context_source_catalog']['H001']=copy.deepcopy(source)
                self.assertTrue(evaluation.runtime_source_errors(value,case['source_anchors']))

    def test_body_leaf_h_equal_prose_cannot_move_to_another_db_position(self):
        value,case=body_leaf_context_audit();result=value['result'];source=result['source_citations'][0]
        for span in (source['locator']['source_span'],source['body_context_provenance']['source_record']['source_span']):
            span['start']+=1;span['end']+=1
        for child in result['draft_response']['citation']:
            child['body_context_provenance']=copy.deepcopy(source['body_context_provenance'])
            child['locator']['source_span']['start']+=1;child['locator']['source_span']['end']+=1
        result['context_source_catalog']['H001']=copy.deepcopy(source)
        common=result['prepared_evidence']['contexts'][0]
        common['body_context_provenance']=copy.deepcopy(source['body_context_provenance'])
        common['locator']=copy.deepcopy(source['locator'])
        self.assertIn('runtime_body_context_changed_from_database_anchor',
                      evaluation.runtime_source_errors(value,case['source_anchors']))
        self.assertFalse(evaluation.anchor_in_selected(case,value,evaluation.SINGLE_RESPONSE_CONTRACT))

    def test_selector_contract_cannot_be_downgraded_to_legacy(self):
        for key in ('source_selection_contract','context_source_catalog','source_selection_trace'):
            value,_=materialized_context_audit();value['result'].pop(key)
            self.assertIn('source_selection_contract_incomplete',evaluation.runtime_source_errors(value,[]))
        value,_=materialized_context_audit()
        for key in ('source_selection_contract','context_source_catalog','source_selection_trace'):value['result'].pop(key)
        self.assertIn('source_selection_contract_incomplete',evaluation.runtime_source_errors(value,[]))
        self.assertEqual(evaluation.runtime_source_errors(audit(),[]),[])

    def test_new_nonanswer_preserves_empty_selection_trace_without_phantom_claims(self):
        result={'source_selection_contract':'structured_context_selection_v1','context_source_catalog':{},
                'source_selection_trace':{'claims':[],'calculations':[]},'generated_claims':[],
                'generated_calculations':[],'draft_status':'not_generated'}
        self.assertEqual(evaluation.source_selection_errors(result),[])
        result['audit']=[{'stage':'draft','parsed_response':{'claims':[],'calculations':[]}}]
        self.assertEqual(evaluation.source_selection_errors(result),[])
        result['source_selection_trace']['claims']=[{'citation_aliases':[],'context_selections':[],
            'resolved_context_citations':[],'citation_ids':[]}]
        self.assertIn('source_selection_row_count_changed',evaluation.source_selection_errors(result))

    def test_selector_cannot_change_item_owner_version_or_drop_item_exception(self):
        for mutation in ('item','owner','date','partial','raw_text','whole_paragraph'):
            with self.subTest(mutation=mutation):
                value,_=materialized_context_audit();result=value['result'];child=result['draft_response']['citation'][-1]
                if mutation=='item':
                    result['audit'][0]['parsed_response']['claims'][0]['context_selections'][0]['item']='1'
                    result['source_selection_trace']['claims'][0]['context_selections'][0]['item']='1'
                elif mutation=='owner':child['parent_chunk_id']='other-owner'
                elif mutation=='date':child['effective_date']='2025-01-01'
                elif mutation=='partial':
                    child['source_quote_span']['end']-=8;child['quote']=child['quote'][:-8]
                elif mutation=='raw_text':result['audit'][0]['parsed_response']['claims'][0]['text']='바꾼 주장'
                else:
                    original=result['source_citations'][0];segment=original['source_segments'][1]
                    child['quote']=segment['text'];child['source_quote_span']={'start':segment['start'],'end':segment['end']}
                self.assertTrue(evaluation.runtime_source_errors(value,[]))

    def test_selector_cannot_swap_registry_source_or_add_unrequested_citation(self):
        value,_=materialized_context_audit();value['result']['context_source_catalog']['H001']['effective_date']='2024-01-01'
        self.assertIn('source_selection_catalog_changed',evaluation.runtime_source_errors(value,[]))
        value,_=materialized_context_audit();result=value['result'];result['generated_claims'][0]['citation_ids'].append('extra')
        self.assertTrue(evaluation.runtime_source_errors(value,[]))

    @staticmethod
    def repaired_audit(**changes):
        """A materialized audit whose claim 0 was extended by one citation_repairs entry."""
        value,_=materialized_context_audit();result=value['result']
        extra={**{k:v for k,v in result['draft_response']['citation'][0].items()
                  if k not in ('source_origin_id','source_quote_span')},
               'unit_id':'synthetic:002000:3','locator':{'article':'제20조','paragraph':3},'context_only':False}
        hidden={**extra,'unit_id':'synthetic:002000:4','context_only':True}
        result['draft_response']['citation']+= [extra,hidden]
        original=list(result['generated_claims'][0]['citation_ids'])
        repair={'claim_index':0,'reference_text':'제20조 제3항','claim_span':{'start':0,'end':9},
                'added_citation_ids':['synthetic:002000:3'],'original_citation_ids':original,
                'reason':'attributed_provision_in_catalog'}
        repair.update(changes)
        result['citation_repairs']=[repair]
        result['generated_claims'][0]['citation_ids']=original+['synthetic:002000:3']
        return result,original

    def test_citation_repairs_explain_exactly_the_added_claim_citations(self):
        result,_=self.repaired_audit()
        self.assertEqual(evaluation.source_selection_errors(result),[])
        result,_=self.repaired_audit(reason='attributed_provision_cited_by_other_claim')
        self.assertEqual(evaluation.source_selection_errors(result),[])
        result,_=self.repaired_audit(reason='stated_item_cited_by_other_claim')
        self.assertEqual(evaluation.source_selection_errors(result),[])
        # Without the repair record the same extension is still a changed selection.
        result,_=self.repaired_audit();result.pop('citation_repairs')
        self.assertEqual(evaluation.source_selection_errors(result),['source_selection_trace_changed'])
        result,_=self.repaired_audit();result['citation_repairs']=[]
        self.assertEqual(evaluation.source_selection_errors(result),['source_selection_trace_changed'])
        # The trace stays the model's own selection.
        result,original=self.repaired_audit()
        self.assertEqual(result['source_selection_trace']['claims'][0]['citation_ids'],original)

    def test_citation_repairs_that_do_not_match_are_rejected(self):
        invalid={'original':{'original_citation_ids':['other']},'index':{'claim_index':1},
                 'negative_index':{'claim_index':-1},'bool_index':{'claim_index':True},
                 'not_in_catalog':{'added_citation_ids':['missing']},'context_only':{'added_citation_ids':['synthetic:002000:4']},
                 'empty':{'added_citation_ids':[]},'duplicate':{'added_citation_ids':['synthetic:002000:3']*2},
                 'reason':{'reason':'model_choice'}}
        for name,change in invalid.items():
            with self.subTest(name=name):
                result,_=self.repaired_audit(**change)
                self.assertEqual(evaluation.source_selection_errors(result),['citation_repair_invalid'])
        result,_=self.repaired_audit();result['citation_repairs'][0]['extra']=1
        self.assertEqual(evaluation.source_selection_errors(result),['citation_repair_invalid'])
        result,_=self.repaired_audit();result['citation_repairs']={}
        self.assertEqual(evaluation.source_selection_errors(result),['citation_repair_invalid'])
        # Recorded but not applied, applied out of order, or an unrecorded extra citation.
        result,original=self.repaired_audit();result['generated_claims'][0]['citation_ids']=original
        self.assertEqual(evaluation.source_selection_errors(result),['source_selection_trace_changed'])
        result,original=self.repaired_audit();result['generated_claims'][0]['citation_ids']=['synthetic:002000:3']+original
        self.assertEqual(evaluation.source_selection_errors(result),['source_selection_trace_changed'])
        result,original=self.repaired_audit();result['generated_claims'][0]['citation_ids'].append('extra')
        self.assertEqual(evaluation.source_selection_errors(result),['source_selection_trace_changed'])

    def test_catalog_owner_and_all_source_segments_are_independently_pinned(self):
        for mutation in ('joint_date','missing_owner','other_owner_source','unselected_segment','duplicate_paragraph'):
            with self.subTest(mutation=mutation):
                value,_=materialized_context_audit();result=value['result'];source=result['source_citations'][0]
                if mutation=='joint_date':
                    source['effective_date']='2024-01-01'
                    for child in result['draft_response']['citation']:child['effective_date']='2024-01-01'
                elif mutation=='missing_owner':result['source_citations'].pop()
                elif mutation=='other_owner_source':result['source_citations'][1]['ordin_seq']='other-law'
                elif mutation=='unselected_segment':source['source_segments'][-1]['end']-=1
                else:source['source_segments'][-1]['paragraph']=2
                result['context_source_catalog']['H001']=copy.deepcopy(source)
                self.assertTrue(evaluation.runtime_source_errors(value,[]))

    def test_title_reference_proof_is_limited_to_same_attachment_heading_span(self):
        result=title_locator_proof_result();self.assertEqual(evaluation.locator_proof_errors(result),[])
        for mutation in ('amount','note','foreign_attachment','date','uncited','capture','missing_owner'):
            with self.subTest(mutation=mutation):
                result=title_locator_proof_result();source=result['source_citations'][0];proof=result['code_checks']['locator_proofs'][0]
                if mutation=='amount':
                    result['generated_claims'][0]['text']='100원';proof['claim_span']={'start':0,'end':3};proof['reference_text']='100'
                elif mutation=='note':source['title_context_kind']='table_note'
                elif mutation=='foreign_attachment':source['locator']['source_refs'][0]['attachment_id']='another-annex'
                elif mutation=='date':source['effective_date']='2025-01-01'
                elif mutation=='uncited':result['generated_claims'][0]['citation_ids']=[]
                elif mutation=='capture':source['locator']['source_refs'][0]['parse_content_hash']='other-capture'
                else:result['source_citations'].pop()
                self.assertTrue(evaluation.locator_proof_errors(result))

    def test_title_reference_proof_reads_source_title_missing_article_suffix(self):
        result=typo_title_locator_proof_result()
        self.assertEqual(evaluation.locator_proof_errors(result),[])
        result['code_checks']['locator_proofs'][0].pop('normalized')
        self.assertEqual(evaluation.locator_proof_errors(result),['locator_proof_reference_changed'])
        result=typo_title_locator_proof_result();result['code_checks']['locator_proofs'][0]['normalized']='other'
        self.assertEqual(evaluation.locator_proof_errors(result),['locator_proof_reference_changed'])
        result=title_locator_proof_result();result['code_checks']['locator_proofs'][0]['normalized']='missing_article_suffix'
        self.assertEqual(evaluation.locator_proof_errors(result),['locator_proof_reference_changed'])
        self.assertEqual(evaluation._reference_tuple('제21제2항'),('제21조',2))
        self.assertIsNone(evaluation._reference_tuple('제21'))

    def single_response(self):
        value=audit();shown=public();result=value['result'];body=shown['response']['answer']
        shown['status']='code_checked_draft'
        cid=result['draft_response']['citation'][0]['unit_id']
        result.update(draft_status='code_checked_draft',code_checks={'status':'passed'},
                      generated_claims=[{'text':body,'citation_ids':[cid]}],generated_calculations=[],
                      claim_joiner='\n\n',claim_segments={'claim_1':body})
        result['draft_response']['answer']=body;result.pop('verification')
        value.update(code={'sha256':'synthetic'},called=0)
        return value,shown

    def test_single_response_status_adapter_is_explicit_and_does_not_change_gold(self):
        case=fixture();before=copy.deepcopy(case);value,shown=self.single_response()
        checked=evaluation.check_case_output(case,shown,value,evaluation.SINGLE_RESPONSE_CONTRACT)
        self.assertEqual(checked['errors'],[])
        self.assertEqual(checked['expected_execution_outcomes'],[{'decision':'supported','draft_status':'code_checked_draft'}])
        self.assertEqual(case,before)
        self.assertIn('unexpected_decision_or_draft_status',evaluation.check_case_output(case,shown,value)['errors'])
        shown['sources'][0]['citation']='서울특별시 청년 기본 조례 제3조 제1호'
        shown['response']['citation']=[shown['sources'][0]['citation']]
        self.assertIn('missing_precise_source_citation',evaluation.check_case_output(case,shown,value,
                      evaluation.SINGLE_RESPONSE_CONTRACT)['errors'])

    def test_released_with_check_issues_is_evaluated_like_a_code_checked_draft(self):
        case=fixture();value,shown=self.single_response();result=value['result']
        result.update(draft_status='released_with_check_issues',
                      code_checks={'status':'failed','issues':['answer_contains_unsupported_number']})
        checked=evaluation.check_case_output(case,shown,value,evaluation.SINGLE_RESPONSE_CONTRACT)
        self.assertEqual(checked['errors'],[])
        self.assertEqual(checked['anchor_evidence_meaning'],'actual_claim_used_source')
        self.assertTrue(evaluation.used_citation_rows(result,evaluation.SINGLE_RESPONSE_CONTRACT))
        # Any non-releasable issue, or a released status without failed checks, is not accepted.
        for checks in ({'status':'failed','issues':['claim_without_owned_citations']},
                       {'status':'failed','issues':[]},{'status':'passed','issues':[]}):
            with self.subTest(checks=checks):
                result['code_checks']=checks
                errors=evaluation.check_case_output(case,shown,value,evaluation.SINGLE_RESPONSE_CONTRACT)['errors']
                self.assertIn('unexpected_decision_or_draft_status',errors)
                if checks['status']=='failed':
                    self.assertIn('operating_code_checks_not_passed',errors)

    def test_numeric_presence_scope_uses_only_actual_public_selection(self):
        value,shown=self.single_response();result=value['result'];checks=result['code_checks']
        checks.update(numeric_citation_scope='public_selected_citations_v1',
                      numeric_citation_ids=list(result['generated_claims'][0]['citation_ids']))
        self.assertEqual(evaluation.single_response_consistency_errors(shown,result),[])
        checks['numeric_citation_ids'].append('retrieved-but-never-selected')
        self.assertIn('numeric_citations_differ_from_public_selection',
                      evaluation.single_response_consistency_errors(shown,result))

    def test_partial_clarification_keeps_public_boundary_separate_from_legal_body(self):
        from public_rag_response import CLARIFICATION_NOTICE
        value,shown=self.single_response();result=value['result']
        result.update(decision='needs_clarification',missing_information=['취소 사유'])
        shown.update(clarification_required=True,missing_information=['취소 사유'],fully_resolved=False,
                     answer_scope='ordinance_only')
        shown['response']['answer']=CLARIFICATION_NOTICE+'\n\n'+result['draft_response']['answer']
        self.assertEqual(evaluation.single_response_consistency_errors(shown,result),[])
        self.assertEqual(evaluation.public_contract_errors(shown,evaluation.SINGLE_RESPONSE_CONTRACT),[])
        shown.pop('clarification_required')
        self.assertIn('clarification_boundary_lost',evaluation.single_response_consistency_errors(shown,result))

    def clarification_case(self):
        case=fixture();case.update(expected_decision='needs_clarification',expected_draft_status='not_generated',
                                   requires_missing_information=True)
        return case

    def test_single_response_clarification_expectation_also_allows_partial_draft(self):
        case=self.clarification_case();before=copy.deepcopy(case)
        self.assertEqual(evaluation.execution_outcomes(case,evaluation.SINGLE_RESPONSE_CONTRACT),
                         [{'decision':'needs_clarification','draft_status':'not_generated'},
                          {'decision':'needs_clarification','draft_status':'code_checked_draft'}])
        self.assertEqual(evaluation.execution_outcomes(case,evaluation.LEGACY_CONTRACT),
                         [{'decision':'needs_clarification','draft_status':'not_generated'}])
        self.assertEqual(case,before)
        other=fixture();other.update(expected_decision='insufficient_evidence',expected_draft_status='not_generated')
        self.assertEqual(evaluation.execution_outcomes(other,evaluation.SINGLE_RESPONSE_CONTRACT),
                         [{'decision':'insufficient_evidence','draft_status':'not_generated'}])

    def test_clarification_expansion_is_deduplicated_across_expected_outcomes(self):
        case=fixture();case['expected_outcomes']=[
            {'decision':'needs_clarification','draft_status':'not_generated'},
            {'decision':'needs_clarification','draft_status':'scope_limited_draft'},
            {'decision':'insufficient_evidence','draft_status':'not_generated'}]
        self.assertEqual(evaluation.execution_outcomes(case,evaluation.SINGLE_RESPONSE_CONTRACT),
                         [{'decision':'needs_clarification','draft_status':'not_generated'},
                          {'decision':'needs_clarification','draft_status':'code_checked_draft'},
                          {'decision':'insufficient_evidence','draft_status':'not_generated'}])
        self.assertEqual(evaluation.execution_outcomes(case,evaluation.LEGACY_CONTRACT),case['expected_outcomes'])

    def test_clarification_case_status_match_under_each_contract(self):
        case=self.clarification_case();value,shown=self.single_response();result=value['result']
        result.update(decision='needs_clarification',missing_information=['취소 사유'])
        shown.update(clarification_required=True,missing_information=['취소 사유'],fully_resolved=False,
                     answer_scope='ordinance_only')
        single=evaluation.SINGLE_RESPONSE_CONTRACT
        self.assertNotIn('unexpected_decision_or_draft_status',evaluation.check_case_output(case,shown,value,single)['errors'])
        self.assertIn('unexpected_decision_or_draft_status',evaluation.check_case_output(case,shown,value)['errors'])
        result['draft_status']='not_generated'
        self.assertNotIn('unexpected_decision_or_draft_status',evaluation.check_case_output(case,shown,value,single)['errors'])
        for decision,status in [('needs_clarification','held'),('insufficient_evidence','code_checked_draft'),
                                ('supported','code_checked_draft')]:
            with self.subTest(decision=decision,status=status):
                result.update(decision=decision,draft_status=status)
                self.assertIn('unexpected_decision_or_draft_status',
                              evaluation.check_case_output(case,shown,value,single)['errors'])
        other=fixture();other.update(expected_decision='insufficient_evidence',expected_draft_status='not_generated')
        result.update(decision='insufficient_evidence',draft_status='code_checked_draft')
        self.assertIn('unexpected_decision_or_draft_status',evaluation.check_case_output(other,shown,value,single)['errors'])

    def withheld_response(self):
        value,shown=self.single_response();result=value['result']
        result.update(decision='insufficient_evidence',draft_status='withheld_no_provision',
                      decision_adjustment={'from':'supported','to':'insufficient_evidence',
                                           'reason':'provision_check_no_provision'},
                      provision_check={'asked_matter':'x','provision_id':'NONE','basis':'no_provision'},
                      response={'answer':'확인 불가','citation':[],'effective_date':[]},
                      audit=[{'stage':'draft'},{'stage':'provision_check'}])
        shown={'as_of_date':'2026-10-01','answer_allowed':False,'final_answer_generated':False,
               'status':'code_check_failed','message':'보류'}
        return value,shown

    def test_withheld_no_provision_matches_only_insufficient_not_generated_in_exact_shape(self):
        value,shown=self.withheld_response();result=value['result'];single=evaluation.SINGLE_RESPONSE_CONTRACT
        insufficient=fixture();insufficient.update(expected_decision='insufficient_evidence',expected_draft_status='not_generated')
        before=copy.deepcopy(insufficient)
        errors=evaluation.check_case_output(insufficient,shown,value,single)['errors']
        self.assertNotIn('unexpected_decision_or_draft_status',errors)
        self.assertNotIn('withheld_no_provision_invariant_failed',errors)
        self.assertEqual(evaluation.execution_outcomes(insufficient,single),
                         [{'decision':'insufficient_evidence','draft_status':'not_generated'}])
        self.assertEqual(insufficient,before)
        self.assertIn('unexpected_decision_or_draft_status',evaluation.check_case_output(insufficient,shown,value)['errors'])
        self.assertIn('unexpected_decision_or_draft_status',evaluation.check_case_output(fixture(),shown,value,single)['errors'])
        broken={'provision_check':{'asked_matter':'x','provision_id':'C001','basis':'no_provision'},
                'decision_adjustment':{'from':'supported','to':'insufficient_evidence','reason':'other'},
                'response':{'answer':'초안','citation':[],'effective_date':[]},
                'audit':[{'stage':'draft'},{'stage':'provision_check'},{'stage':'provision_check'}],
                'decision':'supported'}
        for key,bad in broken.items():
            with self.subTest(key=key):
                value,shown=self.withheld_response();value['result'][key]=bad
                errors=evaluation.check_case_output(insufficient,shown,value,single)['errors']
                self.assertIn('withheld_no_provision_invariant_failed',errors)
                self.assertIn('unexpected_decision_or_draft_status',errors)

    def test_published_clarification_draft_still_requires_missing_information(self):
        case=self.clarification_case();value,shown=self.single_response()
        value['result'].update(decision='needs_clarification')
        shown.update(clarification_required=True,missing_information=[],fully_resolved=False,answer_scope='ordinance_only')
        errors=evaluation.check_case_output(case,shown,value,evaluation.SINGLE_RESPONSE_CONTRACT)['errors']
        self.assertNotIn('unexpected_decision_or_draft_status',errors)
        self.assertIn('user_fact_gap_lost',errors)

    def test_single_response_anchor_uses_actual_claim_ids_without_self_supported(self):
        case=fixture();value,shown=self.single_response()
        self.assertTrue(evaluation.anchor_in_selected(case,value,evaluation.SINGLE_RESPONSE_CONTRACT))
        value['result']['generated_claims'][0]['citation_ids']=[]
        value['result']['verification']={'independent_model_review':{'claims':[{'supported':True,
            'citation_ids':[value['result']['draft_response']['citation'][0]['unit_id']]}]}}
        self.assertFalse(evaluation.anchor_in_selected(case,value,evaluation.SINGLE_RESPONSE_CONTRACT))
        self.assertIn('operating_claim_shape_changed',evaluation.check_case_output(case,shown,value,
                      evaluation.SINGLE_RESPONSE_CONTRACT)['errors'])

    def test_single_response_shared_note_requires_actual_use_with_unchanged_identity_checks(self):
        case,value=note_case_and_audit();result=value['result']
        cid=result['verification']['independent_model_review']['claims'][0]['citation_ids'][0]
        result.update(draft_status='code_checked_draft',code_checks={'status':'passed'},
                      generated_claims=[{'text':'합성 비고 설명','citation_ids':[cid]}])
        result.pop('verification')
        self.assertTrue(evaluation.anchor_in_selected(case,value,evaluation.SINGLE_RESPONSE_CONTRACT))
        result['draft_response']['citation'][0]['effective_date']='2025-01-01'
        self.assertFalse(evaluation.anchor_in_selected(case,value,evaluation.SINGLE_RESPONSE_CONTRACT))

    def test_single_response_clarification_checks_exact_retrieval_arrival_not_fictional_claims(self):
        case=fixture();anchor=case['source_anchors'][0]
        candidate={'unit_id':anchor['chunk_id'],'kind':'body','text':anchor['quote'],
                   'source':{key:anchor[key] for key in ('ordin_seq','source_url','effective_date')},
                   'locator':anchor['_validated_locator']}
        value={'result':{'draft_status':'not_generated','decision':'needs_clarification','selected_ids':[],
                        'generated_claims':[],'prepared_evidence':{'primary_units':[candidate]}}}
        self.assertTrue(evaluation.anchor_in_selected(case,value,evaluation.SINGLE_RESPONSE_CONTRACT))
        candidate['source']['effective_date']='2024-01-01'
        self.assertFalse(evaluation.anchor_in_selected(case,value,evaluation.SINGLE_RESPONSE_CONTRACT))
        candidate['source']['effective_date']=anchor['effective_date'];candidate['text']+=' 다른 원문'
        self.assertFalse(evaluation.anchor_in_selected(case,value,evaluation.SINGLE_RESPONSE_CONTRACT))

    def test_cli_defaults_to_new_contract_without_implicitly_enabling_development_review(self):
        report={'case_count':1,'passed_count':1,'development_passed_count':0,'cases':[]}
        with patch.object(evaluation,'evaluate',return_value=report) as run,patch('builtins.print'):
            self.assertEqual(evaluation.main(['--project',str(ROOT),'--cases','synthetic.json']),0)
        self.assertEqual(run.call_args.args[-2:],(evaluation.SINGLE_RESPONSE_CONTRACT,False))

    def test_mechanical_and_development_results_and_api_counts_are_separate(self):
        value,shown=self.single_response();declared={'database_sha256':'synthetic','cases':[fixture()]}
        rejected={'status':'completed','passed':False,'model_verification_passed':False,'called':2}
        with tempfile.TemporaryDirectory(dir=ROOT) as folder, \
             patch.object(evaluation,'validate_cases',return_value=(declared,'synthetic-fixture')), \
             patch.object(evaluation,'run_question',return_value=value), \
             patch.object(evaluation,'public_result',return_value=shown),patch('builtins.print'), \
             patch('development_rag_review.review_saved_audit',return_value=rejected) as review:
            report=evaluation.evaluate(folder,'unused',check_only=True,development_review=True)
            self.assertEqual(report['passed_count'],1)
            self.assertEqual(report['development_passed_count'],0)
            self.assertEqual(report['operating_called'],0);self.assertEqual(report['development_called'],2)
            self.assertEqual(report['total_called'],2);self.assertEqual(review.call_count,1)
            review.reset_mock()
            report=evaluation.evaluate(folder,'unused',check_only=True)
            self.assertEqual(report['passed_count'],1);self.assertFalse(review.called)
            self.assertIsNone(report['cases'][0]['development_review_passed'])

    def test_actual_cli_output_must_be_from_the_same_evaluated_audit(self):
        value,shown=self.single_response();other=copy.deepcopy(shown);other['response']['answer']+=' 다른 응답'
        declared={'database_sha256':'synthetic','cases':[fixture()]}
        with tempfile.TemporaryDirectory(dir=ROOT) as folder, \
             patch.object(evaluation,'validate_cases',return_value=(declared,'synthetic-fixture')), \
             patch.object(evaluation,'run_question',return_value=value), \
             patch.object(evaluation,'public_result',return_value=shown), \
             patch.object(evaluation,'read_cli_json',return_value=other),patch('builtins.print'):
            report=evaluation.evaluate(folder,'unused',check_only=True,verify_cli=True)
        self.assertEqual(report['passed_count'],0)
        self.assertEqual(report['cases'][0]['error_code'],'cli_response_differs_from_evaluated_audit')

    def test_exact_item_without_invented_paragraph(self):
        self.assertEqual(evaluation.check_case_output(fixture(),public(),audit())['errors'],[])
        value=public()
        value['response']['citation'][0]='서울특별시 청년 기본 조례 제3조 제1호'
        value['sources'][0]['citation']=value['response']['citation'][0]
        self.assertIn('missing_precise_source_citation',evaluation.check_case_output(fixture(),value,audit())['errors'])
        value=public();value['response']['citation'][0]='서울특별시 청년 기본 조례 제3조 제1항 제1호'
        value['sources'][0]['citation']=value['response']['citation'][0]
        self.assertIn('invented_paragraph',evaluation.check_case_output(fixture(),value,audit())['errors'])

    def test_article_label_still_requires_the_claim_to_use_the_expected_item(self):
        case=fixture()
        case['required_citations'][0]['item']='2'
        self.assertIn('missing_used_item_citation',evaluation.check_case_output(case,public(),audit())['errors'])

    def test_public_contract_rejects_added_item_label_beside_article(self):
        value=public()
        duplicate=dict(value['sources'][0],citation='서울특별시 청년 기본 조례 제3조 제2호')
        value['sources'].append(duplicate)
        value['response']['citation'].append(duplicate['citation'])
        self.assertIn('unexpected_public_item_without_paragraph',evaluation.public_contract_errors(value))

    def test_wrong_source_date_and_seq_are_not_saved_by_good_label(self):
        value=public();value['response']['effective_date']='2026-10-01';value['sources'][0]['effective_date']='2026-10-01'
        self.assertIn('missing_precise_source_citation',evaluation.check_case_output(fixture(),value,audit())['errors'])
        value=public();value['response']['source_url']='https://www.law.go.kr/ordinInfoP.do?ordinSeq=999'
        value['sources'][0]['source_url']=value['response']['source_url']
        self.assertIn('unexpected_source_identity_or_date',evaluation.check_case_output(fixture(),value,audit())['errors'])

    def test_exact_unknown_two_fields_not_starter_empty_four(self):
        value={'as_of_date':'2026-10-01','answer_allowed':False,'final_answer_generated':False,
               'status':'out_of_scope','response':{'answer':'확인 불가','citation':[]}}
        self.assertEqual(evaluation.public_contract_errors(value),[])
        value['response'].update(source_url='',effective_date='')
        self.assertIn('unknown_must_have_exactly_two_fields',evaluation.public_contract_errors(value))

    def test_normal_contract_rejects_extra_fields_and_string_citation(self):
        value=public();value['response']['notes']='not part of contract'
        self.assertIn('normal_must_have_exactly_four_fields',evaluation.public_contract_errors(value))
        value=public();value['response']['citation']='서울특별시 청년 기본 조례 제3조 제1호'
        self.assertIn('invalid_citation_array',evaluation.public_contract_errors(value))

    def test_multiple_sources_must_remain_aligned(self):
        value=public();second=copy.deepcopy(value['sources'][0]);second.update(
            citation='서울특별시 도시공원 조례 제20조 제1항 제2호',
            source_url='https://www.law.go.kr/ordinInfoP.do?ordinSeq=2147709',effective_date='2026-07-13')
        value['sources'].append(second)
        value['response']['citation'].append(second['citation'])
        value['response']['source_url']=[x['source_url'] for x in value['sources']]
        value['response']['effective_date']=[x['effective_date'] for x in value['sources']]
        self.assertEqual(evaluation.public_contract_errors(value),[])
        value['response']['source_url'].reverse()
        self.assertIn('source_metadata_not_aligned',evaluation.public_contract_errors(value))

    def test_runtime_child_must_quote_actual_parent(self):
        self.assertTrue(evaluation.anchor_in_selected(fixture(),audit()))
        value=audit();value['result']['draft_response']['citation'][0]['quote']='다른 원문'
        self.assertFalse(evaluation.anchor_in_selected(fixture(),value))

    def test_runtime_child_span_item_and_owner_are_independently_verified(self):
        value=audit();anchor=fixture()['source_anchors'][0];child=value['result']['draft_response']['citation'][0]
        original={'unit_id':anchor['chunk_id'],'quote':anchor['quote'],'kind':'body',
                  'parent_chunk_id':anchor['chunk_id'],'locator':{'paragraph':None},
                  'source_url':anchor['source_url'],'effective_date':anchor['effective_date'],'ordin_seq':'2054497'}
        value['result'].update(citation_contract='exact_source_subunits_v1',source_citations=[original])
        child.update(source_origin_id=anchor['chunk_id'],source_quote_span={'start':0,'end':len(child['quote'])},
                     parent_chunk_id=anchor['chunk_id'],source_url=anchor['source_url'],
                     effective_date=anchor['effective_date'],locator={'paragraph':None,'item':'1'})
        self.assertEqual(evaluation.runtime_source_errors(value,[anchor]),[])
        child['locator']['item']='2'
        self.assertIn('runtime_child_item_number_mismatch',evaluation.runtime_source_errors(value,[anchor]))
        child['source_quote_span']['start']=1
        self.assertIn('runtime_child_source_span_mismatch',evaluation.runtime_source_errors(value,[anchor]))

    def test_same_article_context_from_other_owner_requires_used_exact_source_span(self):
        anchor={'chunk_id':'2147709:002000:1','ordin_seq':'2147709',
                'source_url':'https://www.law.go.kr/ordinInfoP.do?ordinSeq=2147709',
                'effective_date':'2026-07-13','quote':' ① 공통 문언.\n  1. 다른 대상.\n  2. 65세 이상.\n',
                '_validated_locator':{'article':'제20조','paragraph':1}}
        heading='제20조(감면)\n';text=heading+anchor['quote']+' ③ 다른 항.\n'
        left=len(heading);right=left+len(anchor['quote'])
        anchor['_validated_source_span']={'start':left,'end':right}
        owner='2147709:002000:3';origin_id=owner+'::context::2147709:002000'
        original={**{k:anchor[k] for k in ('ordin_seq','source_url','effective_date')},
                  'unit_id':origin_id,'kind':'required_context','parent_chunk_id':owner,'quote':text,
                  'locator':{'article':'제20조'},'source_segments':[{'kind':'provision','paragraph':1,
                  'start':left,'end':right,'text':anchor['quote']}]}
        quote='  2. 65세 이상.\n';start=text.index(quote)
        child={**original,'unit_id':origin_id+'::paragraph::1::item::2','quote':quote,
               'source_origin_id':origin_id,'source_quote_span':{'start':start,'end':start+len(quote)},
               'locator':{'article':'제20조','paragraph':1,'item':'2'}}
        child.pop('source_segments')
        case={'expected_primary_id':anchor['chunk_id'],'source_anchors':[anchor]}
        value={'result':{'draft_status':'verified_draft','selected_ids':[owner],
               'source_citations':[original],'draft_response':{'citation':[child]},
               'verification':{'independent_model_review':{'claims':[{'supported':True,'citation_ids':[child['unit_id']]}]}}}}
        self.assertTrue(evaluation.anchor_in_selected(case,value))
        for field,replacement in (('effective_date','2026-10-01'),('ordin_seq','999'),
                                  ('source_url','https://www.law.go.kr/ordinInfoP.do?ordinSeq=999')):
            altered=copy.deepcopy(value);altered['result']['draft_response']['citation'][0][field]=replacement
            self.assertFalse(evaluation.anchor_in_selected(case,altered),field)
        for field,replacement in (('article','제21조'),('paragraph',2),('item','1')):
            altered=copy.deepcopy(value);altered['result']['draft_response']['citation'][0]['locator'][field]=replacement
            self.assertFalse(evaluation.anchor_in_selected(case,altered),field)
        altered=copy.deepcopy(value);altered['result']['selected_ids'].append(anchor['chunk_id'])
        altered['result']['verification']['independent_model_review']['claims']=[]
        self.assertFalse(evaluation.anchor_in_selected(case,altered),'unused candidate and gate selection cannot pass')
        altered=copy.deepcopy(value);altered['result']['draft_response']['citation'][0]['source_quote_span']['start']+=1
        self.assertFalse(evaluation.anchor_in_selected(case,altered),'wrong actual source position')

    def test_table_number_and_row_are_not_interchangeable(self):
        expected=cases.cite('p',attachment='별표 1',row=3)
        self.assertTrue(evaluation.matches_citation('서울특별시 주차장 설치 및 관리 조례 별표 1 — 1쪽 행 인덱스 3',expected))
        self.assertFalse(evaluation.matches_citation('서울특별시 주차장 설치 및 관리 조례 별표 2 — 1쪽 행 인덱스 3',expected))
        self.assertFalse(evaluation.matches_citation('서울특별시 주차장 설치 및 관리 조례 별표 1 — 1쪽 행 인덱스 4',expected))
        self.assertTrue(evaluation.matches_citation('서울특별시 주차장 설치 및 관리 조례 제7조 제1항 제1호의2',cases.cite('p','제7조',1,'1의2')))

    def test_shared_note_is_required_by_actual_use_not_its_retrieval_row(self):
        for owner in ('r1','r2'):
            case,value=note_case_and_audit(owner)
            self.assertTrue(evaluation.anchor_in_selected(case,value),owner)
        case,value=note_case_and_audit()
        child=value['result']['draft_response']['citation'][0]
        child.update(unit_id=child['unit_id']+'::note',source_origin_id=child['unit_id'],
                     source_quote_span={'start':0,'end':len(child['quote'])})
        value['result']['verification']['independent_model_review']['claims'][0]['citation_ids']=[child['unit_id']]
        self.assertTrue(evaluation.anchor_in_selected(case,value),'exact full-note span')
        child['source_quote_span']['start']=1
        self.assertFalse(evaluation.anchor_in_selected(case,value),'incorrect span')

    def test_note_candidates_and_fee_row_alone_cannot_pass(self):
        for change in ('unused','unsupported','missing_citation','fee_row_only'):
            with self.subTest(change=change):
                case,value=note_case_and_audit();result=value['result']
                claim=result['verification']['independent_model_review']['claims'][0]
                if change=='unused':claim['citation_ids']=[]
                elif change=='unsupported':claim['supported']=False
                elif change=='missing_citation':result['draft_response']['citation']=[]
                else:
                    anchor=case['source_anchors'][0]
                    row={**anchor,'unit_id':anchor['chunk_id'],'parent_chunk_id':anchor['chunk_id'],
                         'kind':'attachment','locator':{'attachment_id':'2147709:table'}}
                    result['selected_ids'].append(anchor['chunk_id'])
                    result['source_citations'].append(row);result['draft_response']['citation'].append(row)
                    claim['citation_ids']=[row['unit_id']]
                self.assertFalse(evaluation.anchor_in_selected(case,value))

    def test_note_identity_version_quote_and_original_context_are_pinned(self):
        changes=(('ordin_seq','999'),('source_url','https://www.law.go.kr/ordinInfoP.do?ordinSeq=999'),
                 ('effective_date','2025-07-13'),('as_of_date','2025-10-01'),
                 ('source_unit_id','another-note'),('quote','청소년: 12세 이상 17세 이하.'),
                 ('parent_chunk_id','unlinked-owner'))
        for field,replacement in changes:
            with self.subTest(field=field):
                case,value=note_case_and_audit()
                for row in value['result']['source_citations']+value['result']['draft_response']['citation']:
                    row[field]=replacement
                self.assertFalse(evaluation.anchor_in_selected(case,value))
        for change in ('other_attachment','other_context_id','missing_original','context_only'):
            with self.subTest(change=change):
                case,value=note_case_and_audit();result=value['result']
                if change=='other_attachment':
                    for row in result['source_citations']+result['draft_response']['citation']:
                        row['locator']['source_refs'][0]['attachment_id']='2147709:another-table'
                elif change=='other_context_id':result['source_citations'][0]['unit_id']+='-different'
                elif change=='missing_original':result['source_citations']=[]
                else:result['draft_response']['citation'][0]['context_only']=True
                self.assertFalse(evaluation.anchor_in_selected(case,value))

    def test_explicit_fee_row_still_requires_the_row_and_the_note(self):
        case,value=note_case_and_audit();case['required_citations'][0]['row_index']=1
        self.assertFalse(evaluation.anchor_in_selected(case,value))
        anchor=case['source_anchors'][0]
        row={**anchor,'unit_id':anchor['chunk_id'],'parent_chunk_id':anchor['chunk_id'],
             'kind':'attachment','locator':{'attachment_id':'2147709:table'}}
        result=value['result'];result['source_citations'].append(row);result['draft_response']['citation'].append(row)
        result['selected_ids'].append(anchor['chunk_id'])
        claim=result['verification']['independent_model_review']['claims'][0]
        claim['citation_ids'].append(row['unit_id'])
        self.assertTrue(evaluation.anchor_in_selected(case,value))
        claim['citation_ids']=[row['unit_id']]
        self.assertFalse(evaluation.anchor_in_selected(case,value),'note remains mandatory')

    def test_context_validation_reads_shared_db_owners_without_changing_fixture(self):
        case,value=note_case_and_audit();anchor=case['source_anchors'][0];context=case['context_anchors'][0]
        refs=copy.deepcopy(context['_validated_source_refs']);owners=context['_validated_owners']
        for record in (anchor,context):
            for key in list(record):
                if key.startswith('_validated_'):record.pop(key)
        anchor['quote_sha256']=evaluation.digest(anchor['quote'].encode())
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project=Path(temp);db=project/evaluation.DEFAULT_DATABASE;db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as connection:
                connection.executescript('CREATE TABLE chunks(chunk_id TEXT,kind TEXT,source_json TEXT,is_deleted INTEGER);'
                    'CREATE TABLE contexts(context_id TEXT,text TEXT,source_json TEXT);'
                    'CREATE TABLE chunk_context_links(chunk_id TEXT,context_id TEXT);')
                for cid,metadata in owners.items():
                    source={'source_record':{'source':metadata,'primary_text':anchor['quote']}}
                    connection.execute('INSERT INTO chunks VALUES(?,?,?,0)',(cid,'attachment',json.dumps(source)))
                    connection.execute('INSERT INTO chunk_context_links VALUES(?,?)',(cid,context['context_id']))
                connection.execute('INSERT INTO contexts VALUES(?,?,?)',(context['context_id'],context['quote'],
                                   json.dumps({'provenance':{'source_refs':refs}})))
            connection.close()
            case['id']='synthetic-note'
            fixture={'as_of_date':'2026-10-01','answer_allowed':False,
                     'database_sha256':evaluation.digest(db.read_bytes()),'cases':[case]}
            path=project/'cases.json';path.write_bytes(evaluation.encoded(fixture));before=path.read_bytes()
            validated,_=evaluation.validate_cases(project,path)
            loaded=validated['cases'][0]['context_anchors'][0]
            self.assertEqual(loaded['_validated_owners'],owners)
            self.assertEqual(loaded['_validated_source_refs'],refs)
            self.assertEqual(path.read_bytes(),before)
            self.assertTrue(evaluation.anchor_in_selected(validated['cases'][0],value))

    def test_mandatory_context_segment_is_exact_but_never_answer_anchor(self):
        heading='제20조(감면)\n';body=' ① 공통 문언.\n'
        original={'unit_id':'p::context::article','kind':'required_context','parent_chunk_id':'p',
                  'quote':heading+body,'locator':{'article':'제20조'},
                  'source_segments':[{'kind':'heading','start':0,'end':len(heading),'text':heading},
                                     {'kind':'provision','paragraph':1,'start':len(heading),
                                      'end':len(heading+body),'text':body}]}
        child={**original,'unit_id':original['unit_id']+'::mandatory::0','quote':heading,
               'source_origin_id':original['unit_id'],'source_quote_span':{'start':0,'end':len(heading)},
               'context_only':True}
        child.pop('source_segments')
        value={'result':{'citation_contract':'exact_source_subunits_v1','source_citations':[original],
                        'draft_response':{'citation':[child]}}}
        self.assertEqual(evaluation.runtime_source_errors(value,[]),[])
        self.assertFalse(evaluation._used_source_matches_anchor(child,{original['unit_id']:original},{'chunk_id':'p'}))
        child['context_only']=False
        self.assertIn('runtime_context_paragraph_span_mismatch',evaluation.runtime_source_errors(value,[]))

    def test_content_pattern_success_never_claims_semantic_approval(self):
        result=evaluation.check_case_output(fixture(),public(),audit())
        self.assertEqual(result['semantic_review'],'pending')
        value=public();value['response']['answer']='청년은 18세 이상입니다.'
        self.assertTrue(any(e.startswith('missing_answer_pattern') for e in evaluation.check_case_output(fixture(),value,audit())['errors']))

    def test_missing_user_facts_not_replaced_with_external_only(self):
        case=fixture();case['requires_missing_information']=True
        self.assertIn('user_fact_gap_lost',evaluation.check_case_output(case,public(),audit())['errors'])

    def test_external_draft_requires_unresolved_boundary(self):
        value=public();value['status']='scope_limited_draft'
        self.assertIn('external_boundary_lost',evaluation.public_contract_errors(value))
        value.update(fully_resolved=False,external_material_status='외부 자료 확인 필요',
                     missing_materials=['별도 요율 규칙'],answer_scope='ordinance_only')
        self.assertEqual(evaluation.public_contract_errors(value),[])

    @patch('evaluate_local_rag.subprocess.run')
    def test_real_cli_request_is_check_only_without_gold(self,run):
        run.return_value=subprocess.CompletedProcess([],0,json.dumps(public(),ensure_ascii=False),'')
        result=evaluation.read_cli_json(Path('/test/project'),'sample question')
        self.assertEqual(result['called'],0)
        args=run.call_args.args[0]
        self.assertIn('--check-only',args);self.assertIn('--json',args)
        self.assertEqual(args[args.index('--question')+1],'sample question')
        self.assertNotIn('expected',str(args));self.assertNotIn('correctness_constraints',str(args))
        run.return_value.stdout='log before JSON\n'+run.return_value.stdout
        with self.assertRaisesRegex(ValueError,'cli_stdout_not_single_json'):
            evaluation.read_cli_json(Path('/test/project'),'sample question')

    @patch('evaluate_local_rag.subprocess.run')
    def test_code_check_failed_cli_requires_nonzero_exit(self,run):
        held={'as_of_date':'2026-10-01','answer_allowed':False,'final_answer_generated':False,
              'status':'code_check_failed','called':0,'message':'합성 코드 검사 보류'}
        run.return_value=subprocess.CompletedProcess([],1,json.dumps(held,ensure_ascii=False),'')
        self.assertEqual(evaluation.read_cli_json(Path('/test/project'),'sample question'),held)
        for status,exit_code in [('code_check_failed',0),('code_checked_draft',1),('insufficient_evidence',1)]:
            with self.subTest(status=status,exit_code=exit_code):
                run.return_value=subprocess.CompletedProcess([],exit_code,json.dumps({**held,'status':status}),'')
                with self.assertRaisesRegex(ValueError,'cli_status_exit_code_mismatch'):
                    evaluation.read_cli_json(Path('/test/project'),'sample question')

    @patch('evaluate_local_rag.subprocess.run')
    def test_code_check_failed_preserves_held_result_without_contract_error(self,run):
        value,_=self.single_response()
        value['result'].update(draft_status='held',code_checks={'status':'failed','issues':['answer_contains_unsupported_number']})
        held={'as_of_date':'2026-10-01','answer_allowed':False,'final_answer_generated':False,
              'status':'code_check_failed','called':0,'message':'합성 코드 검사 보류'}
        run.return_value=subprocess.CompletedProcess([],1,json.dumps(held,ensure_ascii=False),'')
        declared={'database_sha256':'synthetic','cases':[fixture()]}
        with tempfile.TemporaryDirectory(dir=ROOT) as folder, \
             patch.object(evaluation,'validate_cases',return_value=(declared,'synthetic-fixture')), \
             patch.object(evaluation,'run_question',return_value=value), \
             patch.object(evaluation,'public_result',return_value=held),patch('builtins.print'):
            report=evaluation.evaluate(folder,'unused',check_only=True,verify_cli=True)
        actual=report['cases'][0]
        self.assertEqual(report['passed_count'],0)
        self.assertFalse(actual['passed_automated_checks'])
        self.assertEqual(actual['public_result'],held)
        self.assertEqual(actual['draft_status'],'held')
        self.assertIsNone(actual['error_code'])
        self.assertIn('unexpected_decision_or_draft_status',actual['contract_and_content_checks']['errors'])
        self.assertIn('expected_answer_not_published',actual['contract_and_content_checks']['errors'])

    def test_inventory_separates_eleven_samples_and_fourteen_confusion_questions(self):
        rows=cases.definitions()
        self.assertEqual(len(rows),25);self.assertEqual(len({r['id'] for r in rows}),25)
        self.assertEqual(sum(r['cohort']=='starter' for r in rows),11)
        self.assertEqual(sum(r['cohort']=='confusion' for r in rows),14)
        self.assertTrue(all(r['correctness_constraints'] for r in rows))


class AnchorIdentityDateTest(unittest.TestCase):
    """Anchor effective_date uses the same fallback as the DB builder and answer code."""
    URL='https://www.law.go.kr/ordinInfoP.do?ordinSeq=2171833'

    def validate(self,kind,record,anchor_date):
        key={'body':'quote_text','supplement':'source_text'}[kind]
        quote=record[key]
        anchor={'chunk_id':'c1','quote':quote,'quote_sha256':evaluation.digest(quote.encode('utf-8')),
                'ordin_seq':'2171833','source_url':self.URL}
        if anchor_date is not None:anchor['effective_date']=anchor_date
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project=Path(temp);db=project/evaluation.DEFAULT_DATABASE;db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE chunks(chunk_id TEXT,kind TEXT,source_json TEXT)')
                connection.execute('INSERT INTO chunks VALUES(?,?,?)',('c1',kind,json.dumps({'source_record':record})))
            connection.close()
            fixture={'as_of_date':'2026-10-01','answer_allowed':False,'database_sha256':evaluation.digest(db.read_bytes()),
                     'cases':[{'id':'synthetic-date','source_anchors':[anchor]}]}
            path=project/'cases.json';path.write_bytes(evaluation.encoded(fixture));before=path.read_bytes()
            try:validated,_=evaluation.validate_cases(project,path)
            finally:self.assertEqual(path.read_bytes(),before)
            return validated['cases'][0]['source_anchors'][0]

    def supplement(self):
        # Real supplement records have no effective_date key at all.
        return {'source_text':'부칙\n이 조례는 공포한 날부터 시행한다.\n','ordin_seq':'2171833','source_url':self.URL,
                'selected_version_effective_date':'2026-10-01'}

    def body(self):
        return {'quote_text':'제1조(목적) 합성 문언.\n','source':{'ordin_seq':'2171833','source_url':self.URL,
                'effective_date':'2025-07-14','selected_version_effective_date':'2026-10-01'}}

    def test_supplement_selected_version_date_matches_anchor(self):
        self.assertEqual(self.validate('supplement',self.supplement(),'2026-10-01')['effective_date'],'2026-10-01')

    def test_supplement_fallback_date_still_rejects_a_different_anchor_date(self):
        with self.assertRaisesRegex(ValueError,'case_anchor_identity_changed'):
            self.validate('supplement',self.supplement(),'2025-07-14')

    def test_body_effective_date_is_preferred_over_selected_version_date(self):
        self.assertEqual(self.validate('body',self.body(),'2025-07-14')['effective_date'],'2025-07-14')
        for wrong in ('2026-10-01','2024-01-01'):
            with self.assertRaisesRegex(ValueError,'case_anchor_identity_changed'):
                self.validate('body',self.body(),wrong)

    def test_missing_anchor_date_is_filled_with_the_same_fallback_value(self):
        self.assertEqual(self.validate('supplement',self.supplement(),None)['effective_date'],'2026-10-01')
        self.assertEqual(self.validate('body',self.body(),None)['effective_date'],'2025-07-14')



class DuplicateCitationMergeEvaluationTest(unittest.TestCase):
    """The evaluator recomputes the runtime merge of exact repeats on a copy of the raw draft."""

    @staticmethod
    def runtime():
        if str(ROOT/'tests') not in sys.path:sys.path.insert(0,str(ROOT/'tests'))
        import test_local_rag_answer as fixtures
        from local_rag_answer import assess_and_answer
        return fixtures,assess_and_answer

    def merged_results(self):
        fixtures,assess=self.runtime()
        question='청년의 나이 기준은?'
        claim=assess(question,fixtures.retrieval(),fixtures.FakeClient(fixtures.generation(ids=['C001','C001'])))
        answer=fixtures.ANSWER+' 상한과 하한의 차이는 20입니다.'
        calculation=assess(question,fixtures.retrieval(),fixtures.FakeClient(fixtures.generation(
            answer,calculations=[{'expression':'39-19','result':'20','citation_ids':['C001','C001']}])))
        selector={'context_id':'H001','paragraph':'1','item':'2'};text='65세 이상인 사람의 입장료를 면제할 수 있습니다.'
        context=assess('감면 기준은?',fixtures.selector_retrieval(),fixtures.FakeClient(fixtures.generation(
            text,ids=[],claims=[{'text':text,'citation_ids':[],'context_selections':[selector,dict(selector)]}])))
        return {'claim':claim,'calculation':calculation,'context_selection':context}

    def test_recorded_merges_of_real_runtime_results_pass(self):
        for name,result in self.merged_results().items():
            with self.subTest(name=name):
                self.assertNotEqual(result['decision'],'processing_error',result)
                self.assertEqual(result['duplicate_citation_ids_merged'][0]['kind'],name)
                before=copy.deepcopy(result['audit'][0]['parsed_response'])
                self.assertEqual(evaluation.source_selection_errors(result),[])
                # The check works on a copy: the receipt keeps the model's repeats.
                self.assertEqual(result['audit'][0]['parsed_response'],before)
        results=self.merged_results()
        self.assertEqual(results['claim']['audit'][0]['parsed_response']['claims'][0]['citation_ids'],['C001','C001'])
        self.assertEqual(results['calculation']['audit'][0]['parsed_response']['calculations'][0]['citation_ids'],
                         ['C001','C001'])
        self.assertEqual(len(results['context_selection']['audit'][0]['parsed_response']['claims'][0]
                             ['context_selections']),2)

    def test_missing_or_altered_merge_record_is_a_changed_trace(self):
        for name,result in self.merged_results().items():
            with self.subTest(name=name,change='missing'):
                value=copy.deepcopy(result);value.pop('duplicate_citation_ids_merged')
                self.assertEqual(evaluation.source_selection_errors(value),['source_selection_trace_changed'])
            changes={'removed':lambda row:row.update(removed=row['removed']*2),
                     'index':lambda row:row.update(index=row['index']+1),
                     'kind':lambda row:row.update(kind='claim' if row['kind']!='claim' else 'calculation'),
                     'extra':lambda row:row.update(extra=1)}
            if name=='context_selection':changes['owner']=lambda row:row.update(owner='calculation')
            for change,apply in changes.items():
                with self.subTest(name=name,change=change):
                    value=copy.deepcopy(result);apply(value['duplicate_citation_ids_merged'][0])
                    self.assertEqual(evaluation.source_selection_errors(value),['source_selection_trace_changed'])
            with self.subTest(name=name,change='empty'):
                value=copy.deepcopy(result);value['duplicate_citation_ids_merged']=[]
                self.assertEqual(evaluation.source_selection_errors(value),['source_selection_trace_changed'])

    def test_merge_record_without_repeats_is_rejected(self):
        fixtures,assess=self.runtime()
        plain=assess('청년의 나이 기준은?',fixtures.retrieval(),fixtures.FakeClient(fixtures.generation()))
        self.assertNotIn('duplicate_citation_ids_merged',plain)
        self.assertEqual(evaluation.source_selection_errors(plain),[])
        forged=[{'kind':'claim','index':0,'removed':['C001']}]
        for record in (forged,[],None):
            with self.subTest(record=record):
                value=copy.deepcopy(plain);value['duplicate_citation_ids_merged']=record
                self.assertEqual(evaluation.source_selection_errors(value),['source_selection_trace_changed'])

    def test_merge_record_on_a_draft_without_rows_is_rejected(self):
        fixtures,assess=self.runtime()
        empty=None
        for result in self.merged_results().values():
            empty=copy.deepcopy(result);break
        for key in ('claims','calculations'):
            empty['audit'][0]['parsed_response'][key]=[]
            empty['generated_'+key]=[]
            empty['source_selection_trace'][key]=[]
        empty.pop('duplicate_citation_ids_merged',None)
        base=evaluation.source_selection_errors(empty)
        forged=copy.deepcopy(empty);forged['duplicate_citation_ids_merged']=[{'kind':'claim','index':0,'removed':['C001']}]
        self.assertEqual(base,[])
        self.assertEqual(evaluation.source_selection_errors(forged),['source_selection_trace_changed'])


if __name__=='__main__':unittest.main()
