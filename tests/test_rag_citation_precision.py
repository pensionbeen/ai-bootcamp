"""Exact legal subunits remain source slices; no API or local corpus mutation."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from local_rag_answer import (assess_and_answer, precise_citations, expand_model_input, compact_model_input,
                              restore_verification_citations, prepare_evidence, _citations, gate_model_input, packed)
from local_rag_answer import narrow_citations, expand_gate_input
from local_rag_answer import verification_review_context
from public_rag_response import citation_label, public_result
from test_local_rag_answer import retrieval, gate, verification, generation, FakeClient, CID, ANSWER, CONDITION


def unstructured_items():
    data = retrieval()
    record = data["evidence"][0]["source"]["source_record"]
    record.pop("item")
    text = ' 이 조례에서 사용하는 용어의 뜻은 다음과 같다.\n  1. "청년"이란 19세 이상 39세 이하의 사람을 말한다. 다만, 별도 규정에 따른다.\n  2. "청년정책"이란 청년발전을 목적으로 하는 정책을 말한다.\n'
    record.update(quote_text=text, source_span={"start": 7, "end": 7 + len(text)})
    data['evidence'][0]['required_contexts'][0]['text']='제3조(정의)'+text
    data['evidence'][0]['condition_candidates'][0]['review']['condition']['quote']=text[text.index('  1.'):text.index('  2.')]
    return data


def structured_exception(other_item='다른 대상자'):
    data = unstructured_items()
    record = data['evidence'][0]['source']['source_record']
    heading = '제7조(감면)\n'
    first = ' ① 다음 각 호의 사람은 사용료를 50% 감면한다.\n  1. 65세 이상인 사람\n  2. '+other_item+'\n'
    exception = ' ② 감면 사유가 둘 이상이면 높은 감면율 하나만 적용한다.\n'
    note = '[시행에 관한 필수 주석]\n'
    full = heading + first + exception + note
    # The retrieved primary is the exact common text + first item. The second
    # item exists only in its full owned context and cannot supply new claims.
    primary = first[:first.index('  2.')]
    record.update(article='제7조', paragraph=1, quote_text=primary,
                  source_span={'start': len(heading), 'end': len(heading) + len(primary)})
    context = data['evidence'][0]['required_contexts'][0]
    context['text'] = full
    context_record = context['source']['source_record']
    segments = []
    cursor = 0
    for kind, paragraph, text in (('heading', None, heading), ('provision', 1, first),
                                   ('provision', 2, exception), ('notes', None, note)):
        segments.append({'kind': kind, 'paragraph': paragraph, 'start': cursor,
                         'end': cursor + len(text), 'text': text})
        cursor += len(text)
    context_record.update(article='제7조', source_text=full, segments=segments)
    condition = data['evidence'][0]['condition_candidates'][0]['review']['condition']
    condition.update(quote=exception, current_target_ids=[], interpretation='복수 감면의 전역 제한')
    return data


def add_exact_article_condition(data):
    candidate=copy.deepcopy(data['evidence'][0]['condition_candidates'][0])
    identity=CONDITION+':aggregate'
    candidate['condition_id']=identity
    candidate['review']['condition'].update(condition_id=identity,
        quote=data['evidence'][0]['required_contexts'][0]['text'],interpretation='조문 전체의 넓은 감사 해석')
    data['evidence'][0]['condition_candidates'].append(candidate)
    return identity


def reviewed_conditions(*identities):
    return [{'condition_id':identity,'status':'general_statement_only','reason':'제공된 일반 문언 검토'}
            for identity in identities]


def unaligned_attachment():
    source={'ordin_name':'시험 조례','ordin_seq':'sample','as_of_date':'2026-10-01','effective_date':'2025-01-01',
            'source_url':'https://www.law.go.kr/ordinInfoP.do?ordinSeq=sample'}
    aid='sample:attachment';cid='sample:row';origin='sample:attachment-source'
    ref={'attachment_id':aid,'parse_file':'data/processed/sample.parse.json','parse_content_hash':'a'*64,
         'page':1,'element_id':'element','row_index':1}
    raw='XML 표 전체: 일반 요건과 필수 예외를 함께 확인해야 한다.'
    condition={'condition_id':CONDITION,'kind':'application','quote':raw,'interpretation':'표 전체 감사',
        'required_facts':[],'current_target_ids':[],'applies_as_of':'conditional','caution':'원문 대조 필요',
        'evidence_binding':{'source_url':source['source_url'],'xml_path':'./별표단위[1]/별표내용',
                            'start':0,'end':len(raw)}}
    record={'source':source,'attachment_id':aid,'primary_text':'Parse 요금행의 실제 문언','source_refs':[ref]}
    item={'chunk_id':cid,'kind':'attachment','effective_date':source['effective_date'],'as_of_date':'2026-10-01',
        'is_deleted':False,'source':{'source_unit_id':origin,'source_record':record,'required_context_ids':['sample:note'],
                                   'dependency_primary_ids':[],'external_reference_ids':[]},
        'required_contexts':[{'context_id':'sample:note','kind':'required_contexts','text':'Parse 비고의 일반 정의',
            'source':{'source_record':source,'source_refs':[ref]}}],
        'condition_candidates':[{'condition_id':CONDITION,'source_unit_id':origin,
            'review':{'condition':condition,'source':{**source,'kind':'attachment','attachment_id':aid,
                      'source_location':{'xml_path':'./별표단위[1]/별표내용'}},'blockers':[],'requirements':{}},
            'relation':{'same_source_unit':True,'matched_explicit_target_ids':[]}}]}
    return {'as_of_date':'2026-10-01','evidence':[item],'external_tasks':{}},cid


class PreciseCitationTests(unittest.TestCase):
    def test_unresolved_local_reference_in_required_intro_is_not_external_material(self):
        data=unstructured_items();record=data['evidence'][0]['source']['source_record']
        text=record['quote_text'].replace('이 조례에서 사용하는 용어의 뜻은 다음과 같다.','별표 1에 따른 공통 문언은 다음과 같다.')
        record.update(quote_text=text,source_span={'start':7,'end':7+len(text)})
        data['evidence'][0]['required_contexts'][0]['text']=text
        request={'reference_id':'body:ref','reference':'별표 1','kind':'attachment','origin':'explicit_body_reference',
                 'scope':None,'source_chunk_id':CID}
        data['local_reference']={'requested':[request],'resolved':[],'unresolved':[
            {**request,'reason':'local_reference_missing','scope_status':'local_corpus_unresolved'}]}
        result=assess_and_answer('일반 정의는?',data,FakeClient(generation(ids=['Ca3.i1'])))
        self.assertEqual(result['decision'],'insufficient_evidence',result)
        self.assertEqual(result['draft_status'],'held')
        self.assertIn('needed_local_reference_unresolved',result['code_checks']['issues'])
        self.assertEqual(result['missing_materials'],[])

    def test_single_generation_uses_exact_item_without_public_siblings_or_fake_paragraph(self):
        data=unstructured_items();before=copy.deepcopy(data)
        text='제3조 제1호에 따르면 청년은 19세 이상 39세 이하이며 별도 규정이 있으면 그 규정에 따릅니다.'
        client=FakeClient(generation(text,ids=['Ca3','Ca3.i1']))
        result=assess_and_answer('청년의 정의와 단서는?',data,client)
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        rows={row['unit_id']:row for row in result['draft_response']['citation']}
        child=rows[CID+'::item::1'];span=child['source_quote_span']
        self.assertEqual(child['quote'],data['evidence'][0]['source']['source_record']['quote_text'][span['start']:span['end']])
        self.assertIsNone(child['locator']['paragraph']);self.assertEqual(child['locator']['item'],'1')
        self.assertEqual(child['parent_chunk_id'],CID)
        public=public_result({'status':'completed','result':result},catalog={})
        self.assertNotIn('제2호',str(public));self.assertNotIn('제1항',str(public))
        self.assertEqual(public['response']['citation'], ['서울특별시 청년 기본 조례 제3조'])
        self.assertEqual(data,before);self.assertEqual([kind for kind, _ in client.calls], ['draft', 'provision_check'])

    def test_full_article_is_inspection_only_and_exact_exception_closure_is_preserved(self):
        data=structured_exception('다른 대상은 90% 감면한다.');aggregate=add_exact_article_condition(data)
        answer='65세 이상인 사람은 50% 감면하며 중복 사유는 높은 감면율 하나만 적용합니다.'
        client=FakeClient(generation(answer,ids=['Ca7.p1','Ca7.p1.i1','Ca7.p2']))
        result=assess_and_answer('감면과 중복 원칙은?',data,client)
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        wire=json.loads(client.calls[0][1]['messages'][1]['content'])
        self.assertNotIn('90%',packed(wire['cited_evidence']))
        self.assertIn('90%',packed(wire['completeness_context']))
        self.assertIn('90%',packed(result['verification_only']))
        self.assertEqual(len(result['verification_only']['condition_checks']),2)
        self.assertTrue(all(c['gate_status']=='not_assessed' for c in result['verification_only']['condition_checks']))
        rows=result['draft_response']['citation']
        self.assertTrue(any(row['locator'].get('paragraph')==2 and '높은 감면율' in row['quote'] for row in rows))
        self.assertTrue(any('[시행에 관한 필수 주석]' in row['quote'] for row in rows))
        self.assertFalse(any(row['locator'].get('item')=='2' for row in rows))
        self.assertFalse(any(row['quote']==data['evidence'][0]['required_contexts'][0]['text'] for row in rows))

    def test_inspection_only_number_or_id_cannot_support_generation(self):
        data=structured_exception('다른 대상은 90% 감면한다.');add_exact_article_condition(data)
        result=assess_and_answer('대상 감면은?',data,FakeClient(generation('65세 이상이면 90% 감면합니다.',ids=['Ca7.p1','Ca7.p1.i1'])))
        # The inspection-only number still fails the check; that issue alone now releases the draft.
        self.assertEqual(result['draft_status'],'released_with_check_issues',result)
        self.assertEqual(result['code_checks']['status'],'failed')
        self.assertIn('answer_contains_unsupported_number',result['code_checks']['issues'])
        result=assess_and_answer('대상 감면은?',data,FakeClient(generation(ids=['R1'])))
        self.assertEqual(result['error_code'],'response_unknown_enum_or_id')

    def test_whole_condition_with_final_lf_difference_does_not_require_span_coverage(self):
        data=unstructured_items();full=data['evidence'][0]['required_contexts'][0]['text']
        condition=data['evidence'][0]['condition_candidates'][0]['review']['condition']
        quote=full[:-1];condition.update(quote=quote,evidence_binding={
            'source_url':data['evidence'][0]['source']['source_record']['source_url'],
            'start':0,'end':len(quote),'quote_sha256':hashlib.sha256(quote.encode()).hexdigest()})
        result=assess_and_answer('정의?',data,FakeClient(generation(ids=['Ca3.i1'])))
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertIn(quote,result['verification_only']['texts'].values())
        self.assertIn(full,result['verification_only']['texts'].values())
        self.assertEqual(result['condition_sources'][0]['condition']['quote'],quote)

    def test_partial_context_map_keeps_unmapped_text_only_in_inspection_pool(self):
        data=structured_exception();full=data['evidence'][0]['required_contexts'][0]['text']
        data['evidence'][0]['required_contexts'][0]['source']['source_record']['segments'].pop()
        result=assess_and_answer('일반 감면?',data,FakeClient(generation('65세 이상은 50% 감면합니다.',ids=['Ca7.p1','Ca7.p1.i1'])))
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertFalse(any(row['quote']==full for row in result['draft_response']['citation']))
        self.assertIn(full,result['verification_only']['texts'].values())

    def test_common_rate_must_be_cited_on_that_claim(self):
        data=structured_exception()
        for ids,status in ((['Ca7.p1.i1'],'released_with_check_issues'),(['Ca7.p1','Ca7.p1.i1'],'code_checked_draft')):
            result=assess_and_answer('고령자 감면?',data,FakeClient(generation('65세 이상이면 50% 감면합니다.',ids=ids)))
            self.assertEqual(result['draft_status'],status,result)
            if status=='released_with_check_issues':
                self.assertIn('answer_contains_unsupported_number',result['code_checks']['issues'])

    def test_dependency_keeps_original_owner_and_claim_used_parent_is_code_derived(self):
        data=retrieval();other=copy.deepcopy(data['evidence'][0]);other['chunk_id']='dependency:owner'
        other['condition_candidates']=[];data['evidence'].append(other)
        data['evidence'][0]['source']['dependency_primary_ids']=[other['chunk_id']]
        result=assess_and_answer('정의?',data,FakeClient(generation()))
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertEqual(result['selected_ids'],[CID])
        self.assertTrue(any(row['parent_chunk_id']=='dependency:owner' for row in result['source_citations']))
        self.assertTrue(any('dependency:owner' in row['owner_primary_ids']
                            for row in result['verification_only']['mandatory_contexts']))

    def test_context_projection_does_not_materialize_every_paragraph(self):
        data=structured_exception();context=data['evidence'][0]['required_contexts'][0]
        record=context['source']['source_record']
        text=context['text'];segments=record['segments']
        for number in range(3,50):
            extra=' '+str(number)+'번째 무관한 항의 문언\n';start=len(text);text+=extra
            segments.append({'kind':'provision','paragraph':number,'start':start,'end':len(text),'text':extra})
        context['text']=text;record['source_text']=text
        model,units,_=prepare_evidence(data);originals=_citations([CID],units,model['primary_units'])
        selected=precise_citations(originals,context_conditions=model['condition_candidates'])
        self.assertFalse(any(row['locator'].get('paragraph') in range(3,50) for row in selected))
        self.assertLess(len(selected),10)


    def test_condition_binding_integrity_is_checked_before_any_model_request(self):
        for field,value,error in (
            ('source_url','https://www.law.go.kr/ordinInfoP.do?ordinSeq=foreign',
             'condition_binding_source_url_mismatch'),
            ('quote_sha256','0'*64,'condition_binding_quote_hash_mismatch')):
            data=retrieval()
            data['evidence'][0]['condition_candidates'][0]['review']['condition']['evidence_binding'][field]=value
            client=FakeClient()
            result=assess_and_answer('일반 정의는?',data,client)
            self.assertEqual(result['error_code'],error)
            self.assertFalse(client.calls)

    def test_explicit_target_condition_keeps_its_different_source_and_version(self):
        data=retrieval();candidate=data['evidence'][0]['condition_candidates'][0]
        foreign_source={'ordin_name':'명시적으로 연결된 다른 조례','ordin_seq':'foreign',
            'as_of_date':'2026-10-01','effective_date':'2024-01-01',
            'source_url':'https://www.law.go.kr/ordinInfoP.do?ordinSeq=foreign'}
        candidate.update(source_unit_id='foreign:source',relation={
            'same_source_unit':False,'matched_explicit_target_ids':[CID]})
        candidate['review']['source']=foreign_source
        candidate['review']['condition']['evidence_binding']['source_url']=foreign_source['source_url']
        model,_,_=prepare_evidence(data)
        condition=model['condition_candidates'][0]
        self.assertEqual(condition['source'],foreign_source)
        self.assertEqual(condition['owners'][CID],candidate['relation'])
        self.assertEqual(condition['source_unit_id'],'foreign:source')

    def test_parent_gate_keeps_complete_neighboring_item_text_without_second_selection(self):
        data=unstructured_items();record=data['evidence'][0]['source']['source_record']
        text='공통 머리문언\n  7. 유형 A의 카드를 소지한 사람에게 적용하는 문언과 단서가 길게 계속된다.\n  7의2. 유형 B의 증명서를 제시하는 사람에게 적용한다.\n'
        record.update(quote_text=text,source_span={'start':0,'end':len(text)})
        data['evidence'][0]['required_contexts'][0]['text']=text
        model,units,_=prepare_evidence(data)
        citations=precise_citations(_citations([CID],units,model['primary_units']))
        wire=gate_model_input(model)
        self.assertNotIn('fine_citations',wire)
        self.assertEqual(wire['source_texts'][wire['primary_units'][0]['text_ref']],text)
        items={row['locator'].get('item'):row for row in citations if not row.get('context_only')}
        self.assertIn('유형 A',items['7']['quote'])
        self.assertIn('유형 B',items['7의2']['quote'])
        self.assertEqual(expand_gate_input(wire),model)





    def test_verification_context_deduplicates_text_without_losing_source_or_owners(self):
        data=retrieval()
        second=copy.deepcopy(data['evidence'][0]);second['chunk_id']='second:owner';second['condition_candidates']=[]
        data['evidence'].append(second)
        model,units,_=prepare_evidence(data)
        originals=_citations([CID,'second:owner'],units,model['primary_units'])
        before=copy.deepcopy(originals)
        reviews=[{'condition_id':CONDITION,'status':'general_statement_only'}]
        context=verification_review_context(originals,model['condition_candidates'],reviews)
        self.assertEqual(len(context['mandatory_contexts']),1)
        grouped=context['mandatory_contexts'][0]
        self.assertEqual(set(grouped['owner_primary_ids']),{CID,'second:owner'})
        self.assertEqual(len(grouped['audit_context_ids']),2)
        self.assertEqual(context['texts'][grouped['text_ref']],data['evidence'][0]['required_contexts'][0]['text'])
        self.assertFalse(context['may_supply_claim_or_calculation_citations'])
        self.assertNotIn('unit_id',grouped)
        self.assertEqual(originals,before)



    def test_unknown_resolved_context_cannot_masquerade_as_restored_local_evidence(self):
        data=retrieval();request={'reference_id':'question:1','reference':'별표 1','kind':'attachment','origin':'question','scope':None}
        data['local_reference']={'requested':[request],'resolved':[{**request,'primary_ids':[CID],
            'context_ids':['missing:context'],'candidate_only':True,'applicability_established':False}], 'unresolved':[]}
        client=FakeClient()
        result=assess_and_answer('비고 정의는?',data,client)
        self.assertEqual(result['error_code'],'unrestored_local_reference_identity')
        self.assertFalse(client.calls)

    def test_local_reference_resolution_must_cover_requested_refs_and_have_a_carrier(self):
        request={'reference_id':'question:1','reference':'제3조','kind':'body','origin':'question','scope':None}
        for resolved,expected in (([],'incomplete_local_reference_resolution'),
                ([{**request,'primary_ids':[],'context_ids':[],'candidate_only':True,'applicability_established':False}],
                 'unrestored_local_reference_identity')):
            data=retrieval();data['local_reference']={'requested':[request],'resolved':resolved,'unresolved':[]}
            client=FakeClient();result=assess_and_answer('명시 조문은?',data,client)
            self.assertEqual(result['error_code'],expected)
            self.assertFalse(client.calls)



    def test_condition_audit_identity_rejects_other_attachment_version_or_body_kind(self):
        for field,value in (('attachment_id','foreign:attachment'),('ordin_seq','foreign'),
                             ('as_of_date','2026-09-01'),('effective_date','2024-01-01'),
                             ('ordin_name','다른 이름'),('kind','article')):
            data,cid=unaligned_attachment()
            data['evidence'][0]['condition_candidates'][0]['review']['source'][field]=value
            client=FakeClient(gate(selected_ids=[cid]))
            result=assess_and_answer('일반 정의는?',data,client)
            expected=('condition_source_identity_mismatch' if field in ('attachment_id','kind') else
                      'condition_asof_mismatch' if field=='as_of_date' else 'condition_source_version_mismatch')
            self.assertEqual(result['error_code'],expected,field)
            self.assertFalse(result['draft_generated'])

    def test_attachment_identity_can_be_verified_without_filling_a_missing_display_name(self):
        data,cid=unaligned_attachment()
        data['evidence'][0]['source']['source_record']['source']['ordin_name']=None
        model,units,_=prepare_evidence(data)
        originals=_citations([cid],units,model['primary_units'])
        expanded=precise_citations(originals)
        note=next(row for row in expanded if row['kind']=='required_context')
        closed=narrow_citations([note['unit_id']],expanded,originals,units,model['condition_candidates'])
        inspection=verification_review_context(originals,model['condition_candidates'],reviewed_conditions(CONDITION))
        self.assertIn(data['evidence'][0]['condition_candidates'][0]['review']['condition']['quote'],inspection['texts'].values())
        self.assertTrue(all(row['ordin_name'] is None for row in originals))

    def test_unaligned_note_from_a_different_parse_capture_is_not_the_same_attachment_proof(self):
        data,cid=unaligned_attachment()
        context=data['evidence'][0]['required_contexts'][0]
        context['source']['source_refs']=[{**context['source']['source_refs'][0],'parse_content_hash':'b'*64}]
        result=assess_and_answer('비고의 일반 정의는?',data,
            FakeClient(gate(selected_ids=[cid])))
        self.assertEqual(result['error_code'],'context_parse_capture_mismatch')
        self.assertFalse(result['draft_generated'])






    def test_context_paragraph_and_common_intro_keep_exact_source_and_ownership(self):
        text = '제7조(감면)\n ① 이용료를 50% 감면한다. 다음 각 호에 해당하는 사람에 적용한다.\n  1. 대상 가\n  2. 대상 나\n ② 다른 요건을 충족해야 한다.\n'
        heading_end = text.index(' ①')
        second_start = text.index(' ②')
        row = {"unit_id": "attachment::context::body", "parent_chunk_id": "attachment", "kind": "required_context",
               "quote": text, "locator": {"article": '제7조'}, "ordin_name": '조례',
               "source_url": 'https://www.law.go.kr/a', "effective_date": '2025-01-01',
               "source_segments": [{"kind": 'heading', "start": 0, "end": heading_end, "text": text[:heading_end]},
                   {"kind": 'provision', "paragraph": 1, "start": heading_end, "end": second_start,
                    "text": text[heading_end:second_start]},
                   {"kind": 'provision', "paragraph": 2, "start": second_start, "end": len(text), "text": text[second_start:]}]}
        rows = precise_citations([row])
        self.assertEqual(rows[0]["quote"], text)
        intro = next(r for r in rows if r["unit_id"].endswith('paragraph::1::intro'))
        item = next(r for r in rows if r["unit_id"].endswith('paragraph::1::item::2'))
        second = next(r for r in rows if r["unit_id"].endswith('paragraph::2'))
        self.assertIn('50%', intro["quote"])
        self.assertEqual(item["locator"]["paragraph"], 1)
        self.assertEqual(citation_label(item, {}), '조례 제7조 제1항 제2호')
        self.assertEqual(citation_label(second, {}), '조례 제7조 제2항')
        for child in rows[1:]:
            self.assertEqual(child["parent_chunk_id"], 'attachment')
            span = child["source_quote_span"]
            self.assertEqual(child["quote"], text[span["start"]:span["end"]])
        tampered = copy.deepcopy(row)
        tampered["source_segments"][1]["text"] = '다른 원문'
        with self.assertRaisesRegex(ValueError, 'context_source_segment_mismatch'):
            precise_citations([tampered])

    def test_explicit_locator_formats_never_invent_an_absent_paragraph(self):
        for item in (1, '1', '제1호'):
            row = {"ordin_name": '조례', "locator": {"article": '제3조', "paragraph": None, "item": item}}
            self.assertEqual(citation_label(row, {}), '조례 제3조')
        row = {"ordin_name": '조례', "locator": {"article": '제7조', "paragraph": '제1항', "item": '1의2'}}
        self.assertEqual(citation_label(row, {}), '조례 제7조 제1항 제1호의2')
        row["locator"]["item"] = '임의 호'
        with self.assertRaises(ValueError):
            citation_label(row, {})





    def test_gate_retains_every_original_context_and_condition_under_reverse_expansion(self):
        data, units, _ = prepare_evidence(structured_exception())
        data['question'] = '대상과 예외는?'
        ids = [row['unit_id'] for row in data['primary_units']]
        wire = gate_model_input(data)
        expanded = expand_gate_input(wire)
        self.assertEqual(packed(expanded), packed(data))
        self.assertNotIn('text', wire['primary_units'][0])
        self.assertIn('text_ref', wire['primary_units'][0])
        self.assertNotIn('cited_evidence', wire)
        self.assertNotIn('fine_citations',wire)
        self.assertNotIn('citation_owner_alias',wire['primary_units'][0])


    def test_shared_catalog_is_exact_and_never_rewrites_quote_or_raw_target_ids(self):
        raw = '원문에는 '+CID+' 및 C001이 그대로 적혀 있다.'
        row = {'unit_id': CID + '::item::1', 'parent_chunk_id': CID, 'kind': 'body',
               'quote': raw, 'locator': {'article': '제3조', 'item': '1'}, 'context_only': False}
        source = {'cited_evidence': [row, {**row, 'unit_id': CID + '::item::2'}],
                  'condition_review': [{'applies_to_ids': [CID]}],
                  'condition_candidates': [{'owners': {CID: {'same_source_unit': True}},
                                            'condition': {'current_target_ids': [CID], 'quote': raw}}]}
        wire, crosswalk = compact_model_input(source)
        self.assertEqual(len(wire['cited_evidence']['quotes']), 1)
        self.assertEqual(next(iter(wire['cited_evidence']['quotes'].values())), raw)
        self.assertEqual(wire['condition_candidates'][0]['condition']['current_target_ids'], [CID])
        self.assertEqual(packed(expand_model_input(wire, crosswalk)), packed(source))
        tampered = copy.deepcopy(wire)
        tampered['cited_evidence']['units'][0]['quote_ref'] = 'Q999'
        with self.assertRaisesRegex(ValueError, 'unknown_shared_evidence_reference'):
            expand_model_input(tampered, crosswalk)



    def test_condition_closure_never_infers_a_source_from_repeated_words(self):
        data, units, conditions = prepare_evidence(structured_exception())
        originals = _citations([CID], units, data['primary_units'])
        citations = precise_citations(originals)
        condition = next(iter(conditions.values()))
        exception = next(row for row in citations if row['locator'].get('paragraph') == 2)
        for change in ('other_source_unit', 'other_date', 'ambiguous_paragraph'):
            candidates = copy.deepcopy(citations)
            reviewed = copy.deepcopy(condition)
            if change == 'other_source_unit':
                reviewed['source_unit_id'] = 'another:article'
            elif change == 'other_date':
                reviewed['source']['effective_date'] = '2024-01-01'
            else:
                duplicate = copy.deepcopy(exception)
                duplicate['unit_id'] += '::repeated'
                duplicate['locator']['paragraph'] = 3
                candidates.append(duplicate)
            closed = narrow_citations([CID + '::item::1'], candidates, originals, units, [reviewed])
            self.assertFalse(any(row['locator'].get('paragraph') in (2, 3) for row in closed), change)
            self.assertEqual(reviewed['condition']['quote'], condition['condition']['quote'])



