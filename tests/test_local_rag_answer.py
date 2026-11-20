"""Focused fail-closed answer tests; no network, keys, or corpus writes."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from local_rag_answer import assess_and_answer, prepare_evidence, _normalize_calendar_dates, _numbers, _percentage_operands, _immutable_claim_segments, expand_generation_input
from public_rag_response import public_result, ORDINANCE_SCOPE_NOTICE


CID = "2054497:000300:all"
PARENT = "2054497:000300"
CONDITION = PARENT + ":condition:1"
QUOTE = ' 1. "청년"이란 19세 이상 39세 이하의 사람을 말한다. 다만, 다른 관계 법령 및 조례의 별도 규정에 따른다.\n'
ANSWER = "청년은 19세 이상 만39세 이하이며, 다른 관계 법령 및 조례에 별도 규정이 있으면 그 규정에 따릅니다."
SCOPE_ANSWER = ANSWER + " 해당 사업의 별도 연령 규정에 따른 구체적인 자격은 외부 자료 확인 필요 상태입니다."


def retrieval():
    source = {"ordin_name": "서울특별시 청년 기본 조례", "ordin_seq": "2054497",
              "as_of_date": "2026-10-01", "effective_date": "2025-07-14",
              "source_url": "https://www.law.go.kr/ordinInfoP.do?ordinSeq=2054497"}
    record = {**source, "article": "제3조", "paragraph": None, "item": "제1호", "quote_text": QUOTE,
              "parent_id": PARENT, "source_span": {"start": 10, "end": 10 + len(QUOTE)}}
    condition = {"condition_id": CONDITION, "kind": "application", "quote": QUOTE,
                 "interpretation": "19~39세 및 별도 규정", "required_facts": ["나이", "적용 정책"],
                 "current_target_ids": [PARENT], "applies_as_of": "conditional", "caution": "개별 적용 미확정",
                 "evidence_binding": {"source_url": source["source_url"], "xml_path": "./조문/조[3]/조내용"}}
    review = {"condition": condition, "source": source, "blockers": [], "requirements": {"required_facts": ["나이"]}}
    item = {"chunk_id": CID, "kind": "body", "as_of_date": "2026-10-01", "effective_date": "2025-07-14",
            "text": "embedding representation is not the citation", "is_deleted": False,
            "source": {"source_unit_id": PARENT, "source_record": record, "required_context_ids": [PARENT],
                       "dependency_primary_ids": [], "external_reference_ids": []},
            "required_contexts": [{"context_id": PARENT, "kind": "article", "text": "제3조(정의)\n" + QUOTE,
                                   "source": {"source_record": {**source, "article": "제3조"}}}],
            "condition_candidates": [{"condition_id": CONDITION, "source_unit_id": PARENT, "review": review,
                                     "relation": {"same_source_unit": True, "matched_explicit_target_ids": [PARENT]}}]}
    return {"as_of_date": "2026-10-01", "evidence": [item], "external_tasks": {}, "top_k": 8}


def gate(decision="supported", **changes):
    result = {"decision": decision, "reason": "제공된 원문 및 적용 단서를 함께 확인했습니다.",
              "selected_ids": [CID], "condition_review": [{"condition_id": CONDITION,
                  "status": "general_statement_only", "applies_to_ids": [CID], "reason": "일반 정의를 인용하는 질문입니다."}],
              "missing_information": [], "missing_materials": [], "required_external_reference_ids": [],
              "request_more_evidence": False}
    result.update(changes)
    # Wire responses use required condition-ID properties. Public pipeline
    # results are normalized back to the source-bound review list.
    if isinstance(result["condition_review"], list):
        result["condition_review"] = {entry["condition_id"]: {
            key: value for key, value in entry.items() if key not in ("condition_id", "applies_to_ids")}
            for entry in result["condition_review"]}
    result.pop("required_external_reference_ids", None)
    result.pop("request_more_evidence", None)
    return result


def verification(answer=ANSWER, **changes):
    value = {key: True for key in ("supported", "complete", "citations_sufficient", "conditions_satisfied",
                                   "scope_correct", "units_correct", "calculation_correct")}
    value.update(issues=[], claims={key: {"citation_ids": ["C001"], "supported": True,
                  "reason": "정의와 단서를 보존했습니다."} for key in _immutable_claim_segments(answer)}, calculations=[])
    value.update(changes)
    # Declarative legacy fixtures name the text they judge. Bind those judgments
    # to the requested immutable keys; the provider no longer emits claim text.
    if isinstance(value['claims'],list):
        declared=value['claims'];keyed={}
        for key,text in _immutable_claim_segments(answer).items():
            matches=[entry for entry in declared if entry['text'].strip() in text or text.strip() in entry['text']]
            if not matches:continue
            ids=list(matches[0]['citation_ids'])
            for entry in matches[1:]:
                ids.extend(uid for uid in entry['citation_ids'] if uid not in ids)
            keyed[key]={'citation_ids':ids,'supported':all(entry['supported'] for entry in matches),
                        'reason':'명시한 주장의 지원 여부를 검토했습니다.'}
        value['claims']=keyed
    return value


def replace_source_quote(data, text):
    record=data['evidence'][0]['source']['source_record']
    record['quote_text']=text
    record['source_span']={'start':10,'end':10+len(text)}
    data['evidence'][0]['required_contexts'][0]['text']='제3조(정의)\n'+text
    data['evidence'][0]['condition_candidates'][0]['review']['condition']['quote']=text


class FakeClient:
    def __init__(self, *responses, provision=None):
        self.responses = list(responses)
        # Pre-publication provision checks draw from their own queue. When it is
        # empty the check answers with the first cited alias and states_content,
        # so a code-checked draft publishes exactly as before the check existed.
        self.provision = list(provision or [])
        self.calls = []
        self.timeouts = []

    def request(self, kind, payload, timeout=None):
        self.calls.append((kind, copy.deepcopy(payload)))
        self.timeouts.append((kind, timeout))
        if kind == "provision_check":
            if self.provision:
                response = copy.deepcopy(self.provision.pop(0))
            else:
                ids = payload["response_format"]["json_schema"]["schema"]["properties"]["provision_id"]["enum"]
                response = {"asked_matter": "질문 사항", "provision_id": ids[1] if len(ids) > 1 else "NONE",
                            "basis": "states_content"}
        else:
            response = copy.deepcopy(self.responses.pop(0))
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict) and "choices" in response:
            return response, {"cached": False, "receipt_id": str(len(self.calls))}
        if kind == "gate" and isinstance(response, dict):
            schema = payload["response_format"]["json_schema"]["schema"]
            if "required_external_reference_ids" in schema["properties"]:
                response.setdefault("required_external_reference_ids", [])
        if kind == "draft" and isinstance(response, dict):
            # Legacy fixtures declare empty selectors uniformly. The current
            # no-H response schema omits that field entirely; nonempty invalid
            # selectors stay intact so the real strict validator rejects them.
            schema = payload["response_format"]["json_schema"]["schema"]
            for key in ("claims", "calculations"):
                if "context_selections" not in schema["properties"][key]["items"]["properties"]:
                    for entry in response.get(key, []):
                        if entry.get("context_selections") == []:
                            entry.pop("context_selections")
        if not isinstance(response, str):
            response = json.dumps(response, ensure_ascii=False)
        body = {"model": "solar-pro4", "usage": {"total_tokens": 10},
                "choices": [{"finish_reason": "stop", "message": {"content": response}}]}
        return body, {"cached": False, "receipt_id": str(len(self.calls))}


def generation(answer=ANSWER, ids=None, **changes):
    result={"decision":"supported","missing_information":[],"missing_materials":[],
            "claims":[{"text":answer,"citation_ids":ids if ids is not None else ["C001"]}],"calculations":[]}
    result.update(changes)
    result=copy.deepcopy(result)
    for entry in result['claims']+result['calculations']:
        entry.setdefault('context_selections',[])
    return result


def selector_retrieval():
    data=retrieval();item=data['evidence'][0];record=item['source']['source_record']
    heading='제3조(감면)\n'
    paragraphs=[' ① 다음 각 호의 입장료를 면제할 수 있다.\n  1. 6세 미만인 사람\n  2. 65세 이상인 사람\n',
                ' ② 다른 대상의 입장료는 30% 감면할 수 있다.\n',
                ' ③ 다음 각 호의 시설 이용료는 50% 범위에서 감면할 수 있다.\n  1. 별도로 정한 대상\n']
    full=heading+''.join(paragraphs);start=len(heading)+len(paragraphs[0])+len(paragraphs[1])
    record.pop('item');record.update(paragraph=3,quote_text=paragraphs[2],source_span={'start':start,'end':len(full)})
    context=item['required_contexts'][0];context['text']=full
    segments=[{'kind':'heading','paragraph':None,'start':0,'end':len(heading),'text':heading}]
    cursor=len(heading)
    for number,text in enumerate(paragraphs,1):
        segments.append({'kind':'provision','paragraph':number,'start':cursor,'end':cursor+len(text),'text':text})
        cursor+=len(text)
    context['source']['source_record'].update(source_text=full,segments=segments)
    item['condition_candidates'][0]['review']['condition']['quote']=full
    return data


def title_retrieval(article=6):
    source={'ordin_name':'시험 조례','ordin_seq':'sample','as_of_date':'2026-10-01','effective_date':'2025-01-01',
            'source_url':'https://www.law.go.kr/ordinInfoP.do?ordinSeq=sample'}
    ref={'attachment_id':'sample:table','parse_file':'sample.parse.json','parse_content_hash':'a'*64,'page':1}
    context={'context_id':'title','kind':'required_contexts',
             'text':'[별표 1]\n요금표(제'+str(article)+'조제1항 관련)\n다른 문맥의 값은 900이다.',
             'source':{'source_record':{'kind':'ordered_attachment_text_across_pages','source':source},'source_refs':[ref]}}
    return {'as_of_date':'2026-10-01','external_tasks':{},'evidence':[{
        'chunk_id':'sample:row','kind':'attachment','effective_date':source['effective_date'],
        'source':{'source_unit_id':'sample:table','source_record':{'source':source,'attachment_id':'sample:table',
            'primary_text':'같은 행: 노상 500원, 노외 400원','source_refs':[ref]},
            'required_context_ids':['title'],'dependency_primary_ids':[]},
        'required_contexts':[context],'condition_candidates':[]}]}


def body_context_retrieval():
    data=title_retrieval(article=8);item=data['evidence'][0];record=item['source']['source_record']
    text=' ② 다음 각 호의 자료를 제출한다.\n  1. 신청서\n  2. 확인서\n'
    raw={**copy.deepcopy(record['source']),'chunk_id':'sample:000800:2','parent_id':'sample:000800',
         'article':'제8조','paragraph':2,'quote_text':text,'source_span':{'start':100,'end':100+len(text)},
         'is_deleted':False,'source_review':'pending'}
    record['grounding_bundle']={'required_body_contexts':[copy.deepcopy(raw)]}
    item['required_contexts'].append({'context_id':'body-context','kind':'required_body_contexts',
        'relation':'required_body_contexts','text':text,'source':{'kind':'required_body_contexts',
            'unit_id':'body-context','origin_id':raw['chunk_id'],'text':text,'provenance':raw}})
    item['source']['required_context_ids'].append('body-context')
    return data


class GenerationProjectionTests(unittest.TestCase):
    def test_projection_removes_only_named_capture_fields_without_mutating_audit(self):
        from local_rag_answer import _generation_context
        binding={'file_sha256':'hash','quote_sha256':'quote-hash','project_file':'local.xml',
                 'xml_path':'./article','start':2,'end':8,'span_semantics':'capture',
                 'source_url':'https://source','unknown_future_key':'retain'}
        location={'raw_file':'local.xml','xml_path':'./article','xml_node_ordinal':2,
                  'container_ordinal':3,'content_ordinal':4,'attachment_id':'a','page':2,'element_id':'e'}
        reference={'coordinates':[1,2],'parse_content_hash':'hash','parse_file':'parse.json',
                   'attachment_id':'a','page':2,'element_id':'e','unknown_future_key':'retain'}
        condition={'condition':{'quote':'정확한 조건과 예외','current_target_ids':[],
                     'evidence_binding':binding},'source_locator':{'article':'제8조','paragraph':2,
                     'source_location':copy.deepcopy(location),'source_span':{'start':2,'end':8}},
                   'owners':{'raw-owner':{'matched_explicit_target_ids':[]}},'source_unit_id':'raw-source'}
        mandatory={'locator':{'article':'제8조','paragraph':2,'source_location':copy.deepcopy(location),
                     'source_refs':[reference],'source_span':{'start':2,'end':8}},
                   'owner_primary_ids':['raw-owner'],'source_unit_id':'raw-source',
                   'source':{'source_url':'https://source','as_of_date':'2026-10-01'}}
        audit={'condition_checks':[condition],'mandatory_contexts':[mandatory],'texts':{'R1':'정확한 원문'}}
        before=copy.deepcopy(audit);expected=copy.deepcopy(audit)
        expected['condition_checks'][0]['condition']['evidence_binding']={
            'source_url':'https://source','unknown_future_key':'retain'}
        for row in expected['condition_checks']+expected['mandatory_contexts']:
            locator=row['source_locator'] if 'condition' in row else row['locator']
            locator['source_location']={'attachment_id':'a','page':2,'element_id':'e'}
        expected['mandatory_contexts'][0]['locator'].pop('source_span')
        expected['mandatory_contexts'][0]['locator']['source_refs']=[{
            'attachment_id':'a','page':2,'element_id':'e','unknown_future_key':'retain'}]
        self.assertEqual(_generation_context(audit),expected)
        self.assertEqual(audit,before)

    def context_input(self):
        client=FakeClient(generation());result=assess_and_answer('정의?',retrieval(),client)
        return expand_generation_input(json.loads(client.calls[0][1]['messages'][1]['content']),result['citation_aliases'])

    def test_H_blocks_preserve_interleaved_ids_versions_and_no_paragraph(self):
        from local_rag_answer import compact_generation_input
        data=self.context_input();data['completeness_context']['texts']['R-test']='같은 정확한 원문'
        handle={'context_id':'H001','source':{'effective_date':'2025-01-01'},
                'article':'제8조','paragraphs':['2'],'text_ref':'R-test'}
        data['context_sources']=[handle,{**copy.deepcopy(handle),'context_id':'H002',
            'source':{'effective_date':'2024-01-01'}},{**copy.deepcopy(handle),'context_id':'H003'},
            {**copy.deepcopy(handle),'context_id':'H004','paragraphs':['']}]
        before=copy.deepcopy(data);view,crosswalk=compact_generation_input(data)
        self.assertEqual(view['context_sources']['context_order'],['H001','H002','H003','H004'])
        self.assertEqual([r['context_ids'] for r in view['context_sources']['records']],
                         [['H001','H003'],['H002'],['H004']])
        self.assertEqual(expand_generation_input(view,crosswalk),before)
        self.assertEqual(data,before)

    def test_H_display_rejects_invalid_shape_unknown_refs_and_duplicate_or_missing_ids(self):
        from local_rag_answer import compact_generation_input, AnswerError
        data=self.context_input();data['completeness_context']['texts']['R-test']='정확한 원문'
        data['context_sources']=[{'context_id':'H001','source':{},'article':'제8조',
                                 'paragraphs':[''],'text_ref':'R-test'}]
        view,crosswalk=compact_generation_input(data)
        for defect in ('override','unknown_ref','duplicate','unknown_id','missing_id','duplicate_order','empty'):
            bad=copy.deepcopy(view);handles=bad['context_sources'];record=handles['records'][0]
            if defect=='override':record['metadata']['context_id']='H999'
            elif defect=='unknown_ref':record['metadata']['text_ref']='R-unknown'
            elif defect=='duplicate':handles['records'].append(copy.deepcopy(record))
            elif defect=='unknown_id':record['context_ids']=['H999']
            elif defect=='missing_id':handles['records']=[]
            elif defect=='duplicate_order':handles['context_order']*=2
            else:record['context_ids']=[]
            with self.subTest(defect=defect),self.assertRaises(AnswerError):
                expand_generation_input(bad,crosswalk)


class BodyContextAdapterTests(unittest.TestCase):
    def response(self,paragraph='2',item='2'):
        return generation('확인서를 제출합니다.',ids=[],claims=[{'text':'확인서를 제출합니다.',
            'citation_ids':[],'context_selections':[{'context_id':'H001','paragraph':paragraph,'item':item}]}])

    def test_exact_body_leaf_is_H_only_and_preserves_pending_provenance_and_absolute_span(self):
        from local_rag_answer import _citations, precise_citations
        data=body_context_retrieval();before=copy.deepcopy(data)
        prepared,units,_=prepare_evidence(data)
        originals=_citations(['sample:row'],units,prepared['primary_units'])
        context=next(row for row in originals if row.get('body_context_provenance'))
        raw=data['evidence'][0]['required_contexts'][-1]['source']['provenance']
        self.assertEqual(context['source_segments'],[{'kind':'provision','paragraph':2,'start':0,'end':len(raw['quote_text']),'text':raw['quote_text']}])
        fine=precise_citations(originals)
        self.assertFalse(any(row['unit_id'].startswith(context['unit_id']+'::') for row in fine))
        self.assertTrue(next(row for row in fine if row['unit_id']==context['unit_id'])['context_only'])
        client=FakeClient(self.response());result=assess_and_answer('필요한 자료는?',data,client)
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertEqual([kind for kind, _ in client.calls], ['draft', 'provision_check'])
        wire=json.loads(client.calls[0][1]['messages'][1]['content'])
        self.assertNotIn('body_context_provenance',json.dumps(wire))
        self.assertEqual(wire['context_sources']['records'][0]['metadata']['paragraphs'],['2'])
        proof=context['body_context_provenance']
        self.assertEqual(proof,{'contract':'single_body_paragraph_v1','context_id':'body-context',
            'origin_id':raw['chunk_id'],'source_record':raw})
        prepared_context=next(row for row in result['prepared_evidence']['contexts'] if row['context_id']=='body-context')
        self.assertEqual(prepared_context['body_context_provenance'],proof)
        self.assertEqual(result['context_source_catalog']['H001']['body_context_provenance'],proof)
        children=(result['draft_response'] or {})['citation']
        used=set(result['generated_claims'][0]['citation_ids'])
        self.assertEqual(len(used),2)
        for child in children:
            if child['unit_id'] not in used:continue
            local=child['source_quote_span'];absolute=child['locator']['source_span']
            self.assertEqual(child['quote'],raw['quote_text'][local['start']:local['end']])
            self.assertEqual(absolute,{'start':100+local['start'],'end':100+local['end']})
            self.assertEqual(child['source_origin_id'],context['unit_id'])
            self.assertEqual(child['source_unit_id'],'body-context')
            self.assertEqual(child['body_context_provenance'],proof)
            self.assertEqual(child['body_context_provenance']['source_record']['source_review'],'pending')
        self.assertEqual(data,before)

    def test_mismatched_body_identity_quote_span_version_deletion_and_bundle_fail_before_generation(self):
        for defect,error in (
            ('origin','body_context_origin_mismatch'),('context_id','body_context_origin_mismatch'),
            ('quote','body_context_quote_mismatch'),('span_length','body_context_source_span_mismatch'),
            ('span_shift','body_context_bundle_mismatch'),('version','body_context_source_version_mismatch'),
            ('asof','body_context_source_version_mismatch'),('source','body_context_source_version_mismatch'),
            ('deleted','deleted_body_context_not_selectable'),('paragraph','unsupported_body_context_structure'),
            ('item','unsupported_body_context_structure'),('bundle_missing','body_context_bundle_mismatch'),
            ('bundle_duplicate','body_context_bundle_mismatch')):
            data=body_context_retrieval();item=data['evidence'][0];context=item['required_contexts'][-1]
            raw=context['source']['provenance'];bundle=item['source']['source_record']['grounding_bundle']['required_body_contexts']
            if defect=='origin':context['source']['origin_id']='other-body'
            elif defect=='context_id':context['source']['unit_id']='other-context'
            elif defect=='quote':raw['quote_text']+='변조'
            elif defect=='span_length':raw['source_span']['end']+=1
            elif defect=='span_shift':raw['source_span']['start']+=1;raw['source_span']['end']+=1
            elif defect=='version':raw['effective_date']='2024-01-01'
            elif defect=='asof':raw['as_of_date']='2026-09-01'
            elif defect=='source':raw['source_url']='https://www.law.go.kr/other'
            elif defect=='deleted':raw['is_deleted']=True
            elif defect=='paragraph':raw['paragraph']=0
            elif defect=='item':raw['item']='1'
            elif defect=='bundle_missing':bundle.clear()
            elif defect=='bundle_duplicate':bundle.append(copy.deepcopy(bundle[0]))
            client=FakeClient();result=assess_and_answer('필요한 자료는?',data,client)
            with self.subTest(defect=defect):
                self.assertEqual(result.get('error_code'),error,result)
                self.assertEqual(client.calls,[])

    def test_unknown_body_wrapper_stays_read_only_without_inferred_segments(self):
        for defect in ('wrapper_kind','missing_structural_field'):
            data=body_context_retrieval();context=data['evidence'][0]['required_contexts'][-1]
            if defect=='wrapper_kind':context['source']['kind']='unknown_context'
            else:context['source']['provenance'].pop('source_span')
            client=FakeClient(generation('표행을 확인했습니다.'));result=assess_and_answer('표행은?',data,client)
            with self.subTest(defect=defect):
                self.assertEqual(result['context_source_catalog'],{})
                original=next(row for row in result['source_citations'] if row.get('source_unit_id')=='body-context')
                self.assertNotIn('source_segments',original)
                self.assertNotIn('body_context_provenance',original)
                self.assertEqual([kind for kind, _ in client.calls], ['draft', 'provision_check'])

    def test_unnumbered_body_keeps_all_identity_empty_H_address_and_exact_span(self):
        data=body_context_retrieval();owner=data['evidence'][0];context=owner['required_contexts'][-1]
        raw=context['source']['provenance'];text='신청서를 제출한다.'
        raw.update(chunk_id=raw['parent_id']+':all',paragraph=None,quote_text=text,
                   source_span={'start':100,'end':100+len(text)})
        context['text']=text;context['source'].update(origin_id=raw['chunk_id'],text=text)
        owner['source']['source_record']['grounding_bundle']['required_body_contexts']=[copy.deepcopy(raw)]
        before=copy.deepcopy(data)
        reply=generation(text,ids=[],claims=[{'text':text,'citation_ids':[],
            'context_selections':[{'context_id':'H001','paragraph':'','item':''}]}])
        client=FakeClient(reply);result=assess_and_answer('제출 자료는?',data,client)
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        context_source=result['context_source_catalog']['H001']
        self.assertIsNone(context_source['locator']['paragraph'])
        self.assertEqual(context_source['body_context_provenance']['source_record'],raw)
        used=set(result['generated_claims'][0]['citation_ids'])
        children=[r for r in result['draft_response']['citation'] if r['unit_id'] in used]
        self.assertEqual(len(children),1)
        self.assertEqual(children[0]['quote'],text)
        self.assertEqual(children[0]['locator']['source_span'],raw['source_span'])
        self.assertIsNone(children[0]['locator']['paragraph'])
        self.assertEqual(children[0]['source_quote_span'],{'start':0,'end':len(text)})
        self.assertEqual(data,before)
        reply['claims'][0]['context_selections'][0]['paragraph']='1'
        wrong=assess_and_answer('제출 자료는?',data,FakeClient(reply))
        self.assertEqual(wrong['error_code'],'response_unknown_enum_or_id')

    def test_body_paragraph_types_and_identity_are_consistent_across_paths(self):
        for paragraph in (True,0,-1,'1'):
            direct=retrieval();direct['evidence'][0]['source']['source_record']['paragraph']=paragraph
            context=body_context_retrieval();context['evidence'][0]['required_contexts'][-1]['source']['provenance']['paragraph']=paragraph
            segmented=selector_retrieval()
            segmented['evidence'][0]['required_contexts'][0]['source']['source_record']['segments'][1]['paragraph']=paragraph
            for data,error in ((direct,'invalid_body_paragraph_identity'),
                               (context,'unsupported_body_context_structure'),
                               (segmented,'invalid_context_paragraph_identity')):
                with self.subTest(paragraph=paragraph,error=error):
                    client=FakeClient();result=assess_and_answer('자료는?',data,client)
                    self.assertEqual(result['error_code'],error)
                    self.assertEqual(client.calls,[])
        data=body_context_retrieval()
        data['evidence'][0]['required_contexts'][-1]['source']['provenance']['paragraph']=None
        self.assertEqual(assess_and_answer('자료는?',data,FakeClient())['error_code'],
                         'body_context_paragraph_identity_mismatch')
        direct=retrieval();direct['evidence'][0]['source']['source_record'].pop('paragraph')
        segmented=selector_retrieval()
        segmented['evidence'][0]['required_contexts'][0]['source']['source_record']['segments'][1].pop('paragraph')
        for data,error in ((direct,'invalid_body_paragraph_identity'),(segmented,'invalid_context_paragraph_identity')):
            with self.subTest(missing_paragraph=error):
                self.assertEqual(assess_and_answer('자료는?',data,FakeClient())['error_code'],error)

    def test_wrong_H_paragraph_is_not_corrected_to_existing_body(self):
        client=FakeClient(self.response(paragraph='1'))
        result=assess_and_answer('필요한 자료는?',body_context_retrieval(),client)
        self.assertEqual(result['decision'],'processing_error')
        self.assertEqual(result['error_code'],'response_unknown_enum_or_id')
        self.assertEqual(result['context_source_catalog']['H001']['locator']['paragraph'],2)
        self.assertEqual(len(client.calls),1)


class StructuredSelectionTests(unittest.TestCase):
    def response(self,text='65세 이상인 사람의 입장료를 면제할 수 있습니다.',paragraph='1',item='2',**changes):
        return generation(text,ids=[],claims=[{'text':text,'citation_ids':[],
            'context_selections':[{'context_id':'H001','paragraph':paragraph,'item':item}]}],**changes)

    def test_one_response_materializes_only_declared_item_and_intro_with_original_owner(self):
        data=selector_retrieval();before=copy.deepcopy(data);response=self.response();client=FakeClient(response)
        result=assess_and_answer('감면 기준은?',data,client)
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertEqual([kind for kind, _ in client.calls], ['draft', 'provision_check'])
        wire=json.loads(client.calls[0][1]['messages'][1]['content'])
        self.assertEqual(wire['context_sources']['context_order'],['H001'])
        handle=wire['context_sources']['records'][0]['metadata']
        self.assertEqual(wire['completeness_context']['texts'][handle['text_ref']],data['evidence'][0]['required_contexts'][0]['text'])
        origin=CID+'::context::'+PARENT;prefix=origin+'::paragraph::1::'
        expected=[prefix+'intro',prefix+'item::2']
        self.assertEqual(result['generated_claims'],[{'text':response['claims'][0]['text'],'citation_ids':expected}])
        self.assertEqual(result['selected_ids'],[CID])
        self.assertEqual(result['source_selection_trace']['claims'][0],{
            'citation_aliases':[],'context_selections':response['claims'][0]['context_selections'],
            'resolved_context_citations':[expected],'citation_ids':expected})
        self.assertNotIn(prefix+'item::1',{row['unit_id'] for row in result['draft_response']['citation']})
        original=result['context_source_catalog']['H001']
        for row in result['draft_response']['citation']:
            if row['unit_id'] in expected:
                span=row['source_quote_span']
                self.assertEqual(row['quote'],original['quote'][span['start']:span['end']])
                self.assertEqual(row['source_origin_id'],origin)
                self.assertEqual(row['parent_chunk_id'],CID)
                self.assertEqual(row['locator']['paragraph'],1)
        labels=public_result({'status':'completed','result':result},catalog={})['response']['citation']
        # Public labels fold the selected item into its paragraph (the audit row keeps the item).
        self.assertTrue(any(label.endswith('제1항') for label in labels))
        self.assertFalse(any('제2호' in label for label in labels))
        self.assertFalse(any('제3항' in label for label in labels))
        self.assertEqual(data,before)

    def test_empty_item_selects_intro_and_cannot_borrow_other_item_numbers(self):
        result=assess_and_answer('감면 기준은?',selector_retrieval(),FakeClient(self.response(item='')))
        # An unsupported number alone no longer withholds the draft; the check still fires.
        self.assertEqual(result['draft_status'],'released_with_check_issues',result)
        self.assertEqual(result['code_checks']['status'],'failed')
        self.assertEqual(result['code_checks']['unsupported_numbers'],['65'])
        self.assertEqual(len(result['generated_claims'][0]['citation_ids']),1)
        self.assertTrue(result['generated_claims'][0]['citation_ids'][0].endswith('::intro'))
        result=assess_and_answer('감면 기준은?',selector_retrieval(),FakeClient(self.response('다른 대상의 입장료는 30% 감면할 수 있습니다.',paragraph='2',item='')))
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertTrue(result['generated_claims'][0]['citation_ids'][0].endswith('::paragraph::2'))

    def test_wrong_addresses_and_duplicate_selectors_fail_without_a_second_call(self):
        for field,value in [('context_id','R1'),('context_id','H999'),('paragraph','9'),('item','99'),('item','제2호')]:
            response=self.response();response['claims'][0]['context_selections'][0][field]=value
            client=FakeClient(response);result=assess_and_answer('감면 기준은?',selector_retrieval(),client)
            self.assertEqual(result['decision'],'processing_error',(field,value,result))
            self.assertEqual(len(client.calls),1)
        # An exact repeated selector is merged once before resolution (recorded), not a processing error.
        response=self.response();response['claims'][0]['context_selections']*=2
        result=assess_and_answer('감면 기준은?',selector_retrieval(),FakeClient(response))
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertEqual(result['duplicate_citation_ids_merged'][0]['kind'],'context_selection')

    def test_context_map_tampering_and_ambiguous_paragraphs_stop_before_generation(self):
        for change in ('span','duplicate'):
            data=selector_retrieval();raw=data['evidence'][0]['required_contexts'][0]['source']['source_record']
            if change=='span':raw['segments'][1]['start']+=1
            else:raw['segments'][2]['paragraph']=1
            client=FakeClient();result=assess_and_answer('기준?',data,client)
            self.assertEqual(result['decision'],'processing_error',result)
            self.assertFalse(client.calls)

    def test_different_version_source_or_unstructured_context_has_no_handle(self):
        for change in ('version','source','unstructured'):
            data=selector_retrieval();item=data['evidence'][0];item['condition_candidates']=[]
            raw=item['required_contexts'][0]['source']['source_record']
            if change=='version':raw['effective_date']='2024-01-01'
            elif change=='source':raw['ordin_seq']='other';raw['source_url']='https://www.law.go.kr/other'
            else:raw.pop('segments')
            client=FakeClient(self.response());result=assess_and_answer('기준?',data,client)
            self.assertEqual(result['context_source_catalog'],{},(change,result))
            self.assertEqual(result['error_code'],'response_unknown_field_or_id')
            self.assertEqual(len(client.calls),1)

    def test_selector_calculations_use_only_materialized_units(self):
        for expression,answer,status in [('65-65','0','code_checked_draft'),('65-6','59','released_with_check_issues')]:
            selector={'context_id':'H001','paragraph':'1','item':'2'}
            response=self.response('차이는 '+answer+'입니다.',calculations=[{
                'expression':expression,'result':answer,'citation_ids':[],'context_selections':[selector]}])
            result=assess_and_answer('차이는?',selector_retrieval(),FakeClient(response))
            self.assertEqual(result['draft_status'],status,result)
            self.assertEqual(result['generated_calculations'][0]['expression'],expression)
            self.assertEqual(result['generated_calculations'][0]['citation_ids'],result['generated_claims'][0]['citation_ids'])


class TitleLocatorTests(unittest.TestCase):
    def test_title_locator_is_proven_without_adding_a_citation_or_number_pool(self):
        text='요금표(제6조 제1항 관련)의 노상 요금은 500원입니다.'
        result=assess_and_answer('표의 요금은?',title_retrieval(),FakeClient(generation(text)))
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertEqual(result['generated_claims'][0]['citation_ids'],['sample:row'])
        proof=result['code_checks']['locator_proofs'][0]
        self.assertEqual(text[proof['claim_span']['start']:proof['claim_span']['end']],'제6조 제1항')
        self.assertEqual(proof['attachment_citation_id'],'sample:row')
        self.assertEqual(proof['source_context_id'],'sample:row::context::title')

    def test_title_proof_never_supplies_money_time_uncited_body_facts_or_arithmetic(self):
        cases=[('요금표(제6조제1항 관련)의 요금은 6원입니다.',6,[],{'6'}),
               ('요금표(제6조제1항 관련)의 사용 시간은 6시간입니다.',6,[],{'6'}),
               ('요금표(제6조제1항 관련)는 500원과 400원으로 차이는 100원입니다.',6,[],{'100'}),
               ('요금표(제15조제1항 관련)에 있고 제20조 제1항 제2호로 면제됩니다.',15,[],{'20','2','1'}),
               ('제6조 제1항에서 정한 요금은 500원입니다.',6,[],{'6','1'}),
               ('요금표(제6조제1항 관련)의 결과는 506원입니다.',6,[{
                   'expression':'500+6','result':'506','citation_ids':['C001']}],{'506'})]
        for text,article,calculations,unsupported in cases:
            with self.subTest(text=text):
                result=assess_and_answer('표의 요금은?',title_retrieval(article),FakeClient(generation(text,calculations=calculations)))
                self.assertEqual(result['draft_status'],'released_with_check_issues',result)
                self.assertEqual(result['code_checks']['status'],'failed')
                self.assertEqual(set(result['code_checks']['unsupported_numbers']),unsupported)
                if calculations:self.assertIn('unverified_derived_calculation',result['code_checks']['issues'])

    def test_other_capture_attachment_version_or_note_cannot_supply_title_proof(self):
        for change in ('capture','attachment','version','note','later_note'):
            data=title_retrieval();context=data['evidence'][0]['required_contexts'][0]
            if change=='capture':context['source']['source_refs']=copy.deepcopy(context['source']['source_refs']);context['source']['source_refs'][0]['parse_content_hash']='b'*64
            elif change=='attachment':context['source']['source_refs']=copy.deepcopy(context['source']['source_refs']);context['source']['source_refs'][0]['attachment_id']='other:table'
            elif change=='version':context['source']['source_record']['source']=copy.deepcopy(context['source']['source_record']['source']);context['source']['source_record']['source']['effective_date']='2024-01-01'
            elif change=='note':context['source']['source_record']['kind']='table_note'
            else:context['text']='[별표 1]\n요금표\n다른 비고(제6조제1항 관련)'
            client=FakeClient(generation('요금표(제6조제1항 관련)는 500원입니다.'))
            result=assess_and_answer('요금은?',data,client)
            self.assertNotEqual(result['draft_status'],'code_checked_draft',(change,result))
            self.assertFalse(result['code_checks'].get('locator_proofs'))

    def typo_title_retrieval(self,title_locator='제21제2항'):
        data=title_retrieval(21);item=data['evidence'][0]
        item['required_contexts'][0]['text']=('[별표 3]\n부설주차장의 설치제한 지역에서의 시설물의 종류별 설치기준('
                                             +title_locator+' 관련)\n다른 문맥의 값은 900이다.')
        item['source']['source_record']['primary_text']='같은 행: 위락시설 시설면적 134㎡당 1대'
        return data

    def test_source_title_missing_article_suffix_is_read_as_article_and_paragraph(self):
        for claim_locator in ('제21조제2항','제21제2항'):
            with self.subTest(claim_locator=claim_locator):
                text='별표 3('+claim_locator+' 관련)의 위락시설 최고한도는 시설면적 134㎡당 1대입니다.'
                result=assess_and_answer('위락시설 최고한도는?',self.typo_title_retrieval(),FakeClient(generation(text)))
                self.assertNotIn('answer_contains_unsupported_number',result['code_checks']['issues'],result)
                proofs=result['code_checks']['locator_proofs']
                self.assertEqual(len(proofs),1,result)
                self.assertEqual((proofs[0]['article'],proofs[0]['paragraph'],proofs[0]['normalized']),
                                 ('제21조',2,'missing_article_suffix'))
                self.assertEqual(text[proofs[0]['claim_span']['start']:proofs[0]['claim_span']['end']],claim_locator)
                self.assertEqual(proofs[0]['reference_text'],claim_locator)

    def test_source_title_missing_article_suffix_without_paragraph_is_not_a_proof(self):
        text='별표 3(제21조 관련)의 위락시설 최고한도는 시설면적 134㎡당 1대입니다.'
        result=assess_and_answer('위락시설 최고한도는?',self.typo_title_retrieval('제21'),FakeClient(generation(text)))
        self.assertFalse(result['code_checks'].get('locator_proofs'),result)
        self.assertIn('answer_contains_unsupported_number',result['code_checks']['issues'])
        self.assertIn('21',result['code_checks']['unsupported_numbers'])

    def test_regular_title_proof_has_no_normalized_marker(self):
        result=assess_and_answer('표의 요금은?',title_retrieval(),FakeClient(generation('요금표(제6조제1항 관련)는 500원입니다.')))
        self.assertNotIn('normalized',result['code_checks']['locator_proofs'][0])


class TypedMetadataProofTests(unittest.TestCase):
    def rows(self):
        from local_rag_answer import _citations
        data=title_retrieval();item=data['evidence'][0]
        note=copy.deepcopy(item['required_contexts'][0])
        note.update(context_id='note',text='어른은 19세 이상 65세 미만인 사람이다.')
        note['source']['source_record']['kind']='table_note'
        item['required_contexts'].append(note);item['source']['required_context_ids'].append('note')
        prepared,units,_=prepare_evidence(data)
        return _citations([row['unit_id'] for row in prepared['primary_units']],units,prepared['primary_units'])

    def checked(self,text,rows=None,ids=None,calculations=None):
        from local_rag_answer import _check_claims
        rows=self.rows() if rows is None else rows
        claims=[{'text':text,'citation_ids':ids or ['sample:row::context::note']}]
        return _check_claims(claims,calculations or [],text,rows,'원문 기준은?',rows)

    def test_note_annex_number_is_bound_to_exact_owner_heading_span(self):
        text='별표 1의 비고에서 어른은 19세 이상 65세 미만입니다.'
        rows=self.rows();checked=self.checked(text,rows)
        self.assertEqual(checked['status'],'passed',checked)
        proof=checked['metadata_proofs'][0]
        self.assertEqual(proof['kind'],'attachment_label')
        self.assertEqual(proof['citation_id'],'sample:row::context::note')
        self.assertEqual(proof['owner_primary_id'],'sample:row')
        self.assertEqual(text[proof['claim_span']['start']:proof['claim_span']['end']],'별표 1')
        title=next(row for row in rows if row['unit_id']==proof['source_context_id'])
        self.assertEqual(title['quote'][proof['source_quote_span']['start']:proof['source_quote_span']['end']],'별표 1')
        self.assertEqual(checked['checked_claims'][0]['citation_ids'],['sample:row::context::note'])

    def test_wrong_annex_capture_owner_version_or_nonheading_has_no_proof(self):
        for change in ('label','capture','owner','version','heading_kind','later_line','ambiguous_heading'):
            rows=self.rows();title=next(row for row in rows if row['unit_id'].endswith('::title'))
            if change=='label':title['quote']=title['quote'].replace('[별표 1]','[별표 2]')
            elif change=='capture':title['locator']['source_refs'][0]['parse_content_hash']='b'*64
            elif change=='owner':title['parent_chunk_id']='another:owner'
            elif change=='version':title['effective_date']='2024-01-01'
            elif change=='heading_kind':title['title_context_kind']='table_note'
            elif change=='later_line':title['quote']='일반 표제\n[별표 1]\n다른 비고'
            else:title['quote']='[별표 1] [별표 2]\n모호한 제목'
            checked=self.checked('별표 1의 비고입니다.',rows)
            self.assertEqual(checked['status'],'failed',(change,checked))
            self.assertFalse(checked['metadata_proofs'],(change,checked))

    def test_annex_label_never_supplies_money_or_calculation_operands(self):
        for text,calculations in (
                ('별표 1의 요금은 1원입니다.',[]),
                ('별표 1원입니다.',[]),
                ('별표 1의 계산 결과는 20입니다.',[{'expression':'19+1','result':'20',
                                                   'citation_ids':['sample:row::context::note']}])):
            checked=self.checked(text,calculations=calculations)
            self.assertEqual(checked['status'],'failed',checked)
            self.assertIn('answer_contains_unsupported_number',checked['issues'])
            if calculations:self.assertIn('unverified_derived_calculation',checked['issues'])

    def test_platform_asof_proves_only_parenthesized_marker_and_all_owners(self):
        text='표(2026-10-01 기준)의 요금은 500원입니다.'
        checked=self.checked(text,ids=['sample:row'])
        self.assertEqual(checked['status'],'passed',checked)
        proof=checked['metadata_proofs'][0]
        self.assertEqual(proof['kind'],'as_of_date')
        self.assertEqual(proof['metadata_field'],'as_of_date')
        self.assertEqual(text[proof['claim_span']['start']:proof['claim_span']['end']],'2026-10-01 기준')
        self.assertEqual(proof['source_bindings'][0]['citation_id'],'sample:row')
        for field,value in (('as_of_date','2025-10-01'),('effective_date','2024-01-01'),
                            ('parent_chunk_id','unowned')):
            rows=self.rows();note=next(row for row in rows if row['unit_id'].endswith('::note'));note[field]=value
            checked=self.checked(text,rows,ids=['sample:row','sample:row::context::note'])
            self.assertFalse(checked['metadata_proofs'],(field,checked))
            self.assertEqual(checked['status'],'failed',checked)

    def test_other_dates_legal_effective_dates_money_and_arithmetic_are_not_asof(self):
        for text,calculations in (
                ('표(2024-10-01 기준)의 요금은 500원입니다.',[]),
                ('표의 시행일은 2026-10-01입니다.',[]),
                ('2026-10-01 기준의 요금은 500원입니다.',[]),
                ('표(2026-10-01 기준)가 시행되었습니다.',[]),
                ('표(2026-10-01 기준)의 요금은 2026원입니다.',[]),
                ('표(2026-10-01 기준)의 계산 결과는 2526입니다.',[
                    {'expression':'500+2026','result':'2526','citation_ids':['sample:row']}])):
            checked=self.checked(text,ids=['sample:row'],calculations=calculations)
            self.assertEqual(checked['status'],'failed',checked)
            if calculations:self.assertIn('unverified_derived_calculation',checked['issues'])


class ContextDateProofTests(unittest.TestCase):
    S1='별표 1(2023. 12. 29. 개정)에 따른 노상 요금은 500원입니다.'

    def rows(self,title_first_line='[별표 1] <개정 2023. 12. 29.>'):
        from local_rag_answer import _citations
        data=title_retrieval();context=data['evidence'][0]['required_contexts'][0]
        context['text']=context['text'].replace('[별표 1]',title_first_line,1)
        prepared,units,_=prepare_evidence(data)
        return _citations([row['unit_id'] for row in prepared['primary_units']],units,prepared['primary_units'])

    def checked(self,text,rows,scope='public_selected_citations_v1'):
        from local_rag_answer import _check_claims
        claims=[{'text':text,'citation_ids':['sample:row']}]
        return _check_claims(claims,[],text,rows,'요금은?',rows,numeric_citation_scope=scope)

    def test_amendment_date_label_is_bound_to_cited_owner_title_context(self):
        rows=self.rows()
        for scope in ('public_selected_citations_v1','per_claim_selected_citations_v1'):
            checked=self.checked(self.S1,rows,scope)
            self.assertNotIn('answer_contains_unsupported_number',checked['issues'],(scope,checked))
            self.assertEqual(checked['unsupported_numbers'],[])
            self.assertEqual(len(checked['context_date_proofs']),1)
            proof=checked['context_date_proofs'][0]
            self.assertEqual(self.S1[proof['claim_span']['start']:proof['claim_span']['end']],'2023. 12. 29.')
            self.assertEqual((proof['date'],proof['label'],proof['citation_id'],proof['owner_primary_id']),
                             ('2023-12-29','개정','sample:row','sample:row'))
            title=next(row for row in rows if row['unit_id']==proof['source_context_id'])
            self.assertEqual(title['quote'][proof['source_quote_span']['start']:proof['source_quote_span']['end']],
                             '2023. 12. 29.')
        for text in ('별표 1 <개정 2023. 12. 29.>의 노상 요금은 500원입니다.',
                     '별표 1(2023-12-29 개정)의 노상 요금은 500원입니다.'):
            self.assertEqual(self.checked(text,rows)['unsupported_numbers'],[],text)

    def test_same_label_bound_to_another_owner_does_not_prove(self):
        rows=self.rows('[별표 1]')
        other=copy.deepcopy(next(row for row in rows if row['unit_id'].endswith('::title')))
        other.update(unit_id='other::title',parent_chunk_id='another:owner',
                     quote='[별표 1] <개정 2023. 12. 29.>\n다른 표')
        checked=self.checked(self.S1,rows+[other])
        self.assertIn('answer_contains_unsupported_number',checked['issues'])
        self.assertEqual(checked['unsupported_numbers'],['12','29','2023'])
        self.assertFalse(checked['context_date_proofs'])

    def test_bare_date_without_amendment_keyword_does_not_prove(self):
        checked=self.checked(self.S1,self.rows('[별표 1] 2023. 12. 29. 현재 요금'))
        self.assertIn('answer_contains_unsupported_number',checked['issues'])
        self.assertEqual(checked['unsupported_numbers'],['12','29','2023'])
        self.assertFalse(checked['context_date_proofs'])

    def test_date_proof_never_supplies_other_context_numbers(self):
        checked=self.checked('별표 1(2023. 12. 29. 개정)에 따른 노상 요금은 6원입니다.',self.rows())
        self.assertEqual(len(checked['context_date_proofs']),1)
        self.assertIn('answer_contains_unsupported_number',checked['issues'])
        self.assertEqual(checked['unsupported_numbers'],['6'])

    def test_unsupported_numbers_are_plain_decimal_strings(self):
        checked=self.checked('별표 1의 노상 요금은 50원입니다.',self.rows('[별표 1]'))
        self.assertEqual(checked['unsupported_numbers'],['50'])


class MultiplicationNotationTests(unittest.TestCase):
    def test_multiplication_glyph_and_ascii_share_existing_arithmetic_rules(self):
        from local_rag_answer import _calculation
        operands = _numbers('19 39 2 3 0.5')
        for expression in ('19*2', '19×2', '(39-19)×2', 'ceil(19×0.5)',
                           'floor(19×0.5)', '19×-2', '19×2*3', '39//2×3',
                           'ﬂoor(19×0.5)', 'ｃｅｉｌ(19×0.5)', '19×2 # 두 배'):
            with self.subTest(expression=expression):
                self.assertEqual(_calculation(expression, operands),
                                 _calculation(expression.replace('×', '*'), operands))

    def test_unsafe_syntax_and_unsourced_operands_still_fail(self):
        from local_rag_answer import _calculation, AnswerError
        operands = _numbers('19 39 2 3 0.5')
        for expression in ('19×18', '19××2', '19×*2', '19*×2', '19**2',
                           '19× ×2', '19× *2', '19×(2**3)', 'abs(19×2)',
                           'ﬂoorx(19×0.5)', 'ａｂｓ(19×0.5)', '１９×2',
                           '__import__("os")×2', '(19,2)×2', 'True×2',
                           '[19]×2', '19×2<<3', '19×2@3', '19×2/0.0'):
            with self.subTest(expression=expression), self.assertRaises((AnswerError, SyntaxError, ArithmeticError)):
                _calculation(expression, operands)

    def test_public_pipeline_preserves_raw_calculation_and_owned_operand_rules(self):
        # An unverified calculation alone is released (published) with its check issue kept.
        for expression, result, published in (('19×2', '38', True), ('19*2', '38', True),
                                              ('19×18', '342', True), ('19××2', '361', True)):
            with self.subTest(expression=expression):
                calculation = {'expression': expression, 'result': result, 'citation_ids': ['C001']}
                data = retrieval()
                response = generation('계산 결과는 '+result+'입니다.', calculations=[calculation])
                original = copy.deepcopy(response)
                checked = assess_and_answer('19세의 두 배(2)는?', data, FakeClient(response))
                shown = public_result({'status': 'completed', 'question': '19세의 두 배(2)는?',
                    'as_of_date': '2026-10-01', 'result': checked}, catalog={})
                self.assertEqual(bool((shown.get('response') or {}).get('answer')), published)
                self.assertEqual(checked['generated_calculations'][0]['expression'], expression)
                self.assertEqual(response, original)
                if expression in ('19×18', '19××2'):
                    self.assertEqual(checked['draft_status'], 'released_with_check_issues')
                    self.assertIn('unverified_derived_calculation', checked['code_checks']['issues'])


class SingleGenerationTests(unittest.TestCase):
    def reference_state(self, reason, scope=None, origin='question'):
        request={'reference_id':'reference:1','reference':'제3조' if origin=='question' else '별표 1',
                 'kind':'body' if origin=='question' else 'attachment','origin':origin,'scope':scope}
        if origin!='question':request['source_chunk_id']=CID
        return {'requested':[request],'resolved':[],'unresolved':[
            {**request,'reason':reason,'scope_status':'local_corpus_unresolved'}]}

    def test_unrecognized_question_owner_is_preserved_and_never_auto_resolved(self):
        data=retrieval();data['local_reference']=self.reference_state('source_scope_unspecified')
        original=copy.deepcopy(data['local_reference'])
        responses=[generation(),generation(decision='needs_clarification',claims=[],missing_information=['어느 조례인지 확인 필요']),
                   generation(decision='insufficient_evidence',claims=[])]
        for response in responses:
            client=FakeClient(response);result=assess_and_answer('제3조의 정의는?',data,client)
            self.assertEqual([kind for kind, _ in client.calls],
                             ['draft', 'provision_check'] if response['claims'] else ['draft'],result)
            self.assertEqual(result['decision'],response['decision'])
            wire=json.loads(client.calls[0][1]['messages'][1]['content'])
            self.assertEqual(wire['local_reference'],original)
            self.assertEqual(result['local_reference'],original)
            self.assertEqual(result['prepared_evidence']['local_reference'],original)
            self.assertEqual(result['local_reference']['resolved'],[])
            self.assertIsNone(result['local_reference']['unresolved'][0]['scope'])
            self.assertFalse(result['answer_allowed']);self.assertFalse(result['final_answer_generated'])
        self.assertEqual(data['local_reference'],original)

    def test_known_missing_budget_and_version_ambiguity_still_block_before_generation(self):
        scope={'ordin_seq':'2054497','as_of_date':'2026-10-01','effective_date':'2025-07-14','content_hash':'a'*64}
        for reason,owner in (('local_reference_missing',scope),('row_selection_requires_review',scope),
                             ('ambiguous_ordinance_version',None),('source_scope_unspecified',scope)):
            with self.subTest(reason=reason,scope=owner):
                data=retrieval();data['local_reference']=self.reference_state(reason,owner)
                if reason=='row_selection_requires_review':
                    data['local_reference']['unresolved'][0].update(budget_kind='reference_row_candidates',candidate_count=25,candidate_limit=24)
                client=FakeClient();result=assess_and_answer('명시한 조문의 원문은?',data,client)
                self.assertEqual(result['decision'],'insufficient_evidence',result)
                self.assertFalse(client.calls)
                self.assertEqual(result['missing_local_references'],data['local_reference']['unresolved'])
                self.assertEqual(result['missing_materials'],[])

    def test_selected_body_reference_is_still_required_after_unknown_question_owner_is_separated(self):
        data=retrieval();replace_source_quote(data,QUOTE+'별표 1의 필수 조건에 따른다.\n')
        scope={'ordin_seq':'2054497','as_of_date':'2026-10-01','effective_date':'2025-07-14','content_hash':'a'*64}
        data['local_reference']=self.reference_state('local_reference_missing',scope,'explicit_body_reference')
        client=FakeClient(generation());result=assess_and_answer('일반 정의는?',data,client)
        self.assertEqual(len(client.calls),1,result)
        self.assertEqual(result['decision'],'insufficient_evidence')
        self.assertEqual(result['draft_status'],'held')
        self.assertIn('needed_local_reference_unresolved',result['code_checks']['issues'])
        self.assertEqual(result['missing_materials'],[])

    def test_one_generation_has_no_model_approval_or_automatic_second_stage(self):
        client=FakeClient(generation())
        result=assess_and_answer('청년의 일반 정의는?',retrieval(),client)
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertEqual([kind for kind, _ in client.calls], ['draft', 'provision_check'])
        payload=client.calls[0][1]
        self.assertEqual((payload['model'],payload['reasoning_effort'],payload['max_tokens']),('solar-pro4','none',6000))
        schema=payload['response_format']['json_schema']['schema']
        self.assertEqual(set(schema['properties']),{'decision','missing_information','missing_materials','claims','calculations'})
        self.assertEqual(result['verification'],{'status':'not_run'})
        self.assertEqual(result['code_checks']['semantic_verification'],'not_run')
        self.assertEqual(result['selected_ids'],[CID])
        self.assertEqual(result['generated_claims'],[{'text':ANSWER,'citation_ids':[CID]}])
        self.assertFalse(result['condition_gate_evaluated'])
        self.assertFalse(result['answer_allowed']);self.assertFalse(result['final_answer_generated'])
        public=public_result({'status':'completed','result':result},catalog={})
        self.assertEqual(public['status'],'code_checked_draft')
        self.assertEqual(public['response']['answer'],ANSWER)
        self.assertIn('의미 검증 미실행',public['message'])

    def test_malformed_length_refusal_unknown_and_duplicate_cids_stop_after_one_call(self):
        cases=[('{"bad":','response_not_json'),
               ({'choices':[{'finish_reason':'length','message':{'content':''}}]},'model_output_limit'),
               ({'choices':[{'finish_reason':'stop','message':{'refusal':'blocked','content':''}}]},'model_refusal'),
               (generation(ids=['C999']),'response_unknown_enum_or_id'),
               # A repeated known CID is now merged (DuplicateCitationIdMergeTests); a repeated unknown one still stops.
               (generation(ids=['C999','C999']),'response_unknown_enum_or_id'),
               (generation(supported=True),'response_unknown_field_or_id')]
        for response,error in cases:
            with self.subTest(error=error):
                client=FakeClient(response)
                result=assess_and_answer('정의는?',retrieval(),client)
                self.assertEqual(result.get('error_code'),error,result)
                self.assertEqual(len(client.calls),1)
                self.assertFalse(result['format_repair_used']);self.assertFalse(result['capacity_recovery_used'])
                self.assertNotEqual(result['draft_status'],'code_checked_draft')

    def test_provider_failure_has_no_second_logical_request(self):
        client=FakeClient(RuntimeError('sensitive provider detail'))
        result=assess_and_answer('정의는?',retrieval(),client)
        self.assertEqual(result['error_code'],'draft_request_failed')
        self.assertEqual(len(client.calls),1)
        self.assertNotIn('sensitive',json.dumps(result))

    def test_empty_and_unresolved_question_evidence_do_not_call_solar(self):
        data=retrieval();data['evidence']=[]
        client=FakeClient();result=assess_and_answer('정의?',data,client)
        self.assertEqual(result['decision'],'insufficient_evidence');self.assertFalse(client.calls)
        self.assertFalse(result['request_more_evidence'])
        data=retrieval();request={'reference_id':'question:1','reference':'별표 1','kind':'attachment','origin':'question','scope':None}
        data['local_reference']={'requested':[request],'resolved':[],'unresolved':[
            {**request,'reason':'local_reference_missing','scope_status':'local_corpus_unresolved'}]}
        result=assess_and_answer('별표 1의 정의?',data,client)
        self.assertEqual(result['decision'],'insufficient_evidence');self.assertFalse(client.calls)
        self.assertEqual(result['missing_materials'],[])

    def test_code_does_not_turn_semantically_wrong_text_into_a_verified_answer(self):
        # This intentionally has no novel numeric token. Code checks cannot
        # independently determine whether the proposition is legally supported.
        text='다른 별표의 정의도 모든 시설에 적용됩니다.'
        result=assess_and_answer('정의?',retrieval(),FakeClient(generation(text)))
        self.assertEqual(result['draft_status'],'code_checked_draft')
        self.assertEqual(result['semantic_verification'],'not_run')
        self.assertNotIn('independent_model_review',result['code_checks'])

    def test_claims_are_joined_exactly_and_exported_without_reselection(self):
        claims=[{'text':'청년은 19세 이상입니다.','citation_ids':['C001']},
                {'text':'다른 관계 법령의 별도 규정은 그 규정에 따릅니다.','citation_ids':['C001']}]
        result=assess_and_answer('정의?',retrieval(),FakeClient(generation(claims=claims)))
        self.assertEqual(result['draft_response']['answer'],'\n\n'.join(c['text'] for c in claims))
        self.assertEqual(list(result['claim_segments'].values()),[c['text'] for c in claims])
        self.assertEqual([c['citation_ids'] for c in result['generated_claims']],[[CID],[CID]])
        self.assertEqual(result['generated_claims'],result['code_checks']['checked_claims'])

    def test_missing_citation_and_unowned_number_hold_without_repair(self):
        response,issue=generation(ids=[]),'claim_without_owned_citations'
        client=FakeClient(response);result=assess_and_answer('정의?',retrieval(),client)
        self.assertEqual(result['draft_status'],'held',result)
        self.assertIn(issue,result['code_checks']['issues'])
        public=public_result({'status':'completed','result':result},catalog={})
        self.assertEqual(public['status'],'code_check_failed');self.assertNotIn('response',public)
        self.assertEqual(len(client.calls),1)
        # An unsupported number alone is released after the provision check, without repair.
        client=FakeClient(generation('청년은 70세 이하입니다.'));result=assess_and_answer('정의?',retrieval(),client)
        self.assertEqual(result['draft_status'],'released_with_check_issues',result)
        self.assertEqual(result['code_checks']['issues'],['answer_contains_unsupported_number'])
        self.assertEqual(result['citation_repairs'],[])
        public=public_result({'status':'completed','result':result},catalog={})
        self.assertEqual(public['status'],'code_checked_draft')
        self.assertEqual(public['response']['answer'],'청년은 70세 이하입니다.')
        self.assertEqual([kind for kind, _ in client.calls],['draft','provision_check'])

    def test_calculation_uses_only_its_own_citations_and_question(self):
        good=generation('두 연령의 차이는 20세입니다.',calculations=[{
            'expression':'39-19','result':'20','citation_ids':['C001']}])
        result=assess_and_answer('연령 차이는?',retrieval(),FakeClient(good))
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        for calculation in ({'expression':'39-18','result':'21','citation_ids':['C001']},
                            {'expression':'39-19','result':'21','citation_ids':['C001']}):
            result=assess_and_answer('차이는?',retrieval(),FakeClient(generation(
                '차이는 21세입니다.',calculations=[calculation])))
            self.assertEqual(result['draft_status'],'released_with_check_issues')
            self.assertEqual(result['code_checks']['status'],'failed')
            self.assertIn('unverified_derived_calculation',result['code_checks']['issues'])

    def test_failed_calculation_whose_result_is_not_stated_does_not_hold_the_draft(self):
        calculation={'expression':'39-18','result':'21','citation_ids':['C001']}
        result=assess_and_answer('청년 연령은?',retrieval(),FakeClient(generation(calculations=[calculation])))
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertEqual(result['code_checks']['issues'],[])
        self.assertEqual(result['code_checks']['unverified_calculations_not_stated'],
                         [{'expression':'39-18','result':'21','reason':'calculation_has_unsourced_operand'}])
        self.assertEqual(result['generated_calculations'],[{**calculation,'citation_ids':[CID]}])
        public=public_result({'status':'completed','question':'청년 연령은?','as_of_date':'2026-10-01',
                              'result':result},catalog={})
        self.assertEqual(public['response']['answer'],ANSWER)

    def test_numbers_from_an_uncited_candidate_do_not_support_a_claim(self):
        data=retrieval();other=copy.deepcopy(data['evidence'][0]);other['chunk_id']='other:owner'
        other['source']['source_unit_id']='other:source';other['required_contexts']=[]
        other['source']['required_context_ids']=[];other['condition_candidates']=[]
        other['source']['source_record']['quote_text']='90세인 다른 대상'
        data['evidence'].append(other)
        result=assess_and_answer('정의?',data,FakeClient(generation('청년은 90세 이하입니다.')))
        self.assertEqual(result['draft_status'],'released_with_check_issues',result)
        self.assertEqual(result['code_checks']['status'],'failed')
        self.assertIn('answer_contains_unsupported_number',result['code_checks']['issues'])

    def test_public_selected_union_supports_split_text_but_not_unselected_numbers(self):
        from local_rag_answer import _check_claims, _verify_result, AnswerError
        rows=[{'unit_id':'intro','quote':'다음 요건에 해당하면 인수할 수 있다.','locator':{},'context_only':False},
              {'unit_id':'item','quote':'6개월 이상 입원한 경우','locator':{},'context_only':False},
              {'unit_id':'unused','quote':'다른 900원의 기준','locator':{},'context_only':False}]
        claims=[{'text':'6개월 이상 입원한 경우 인수할 수 있습니다.','citation_ids':['intro']},
                {'text':'장기 입원 요건이 있습니다.','citation_ids':['item']}]
        answer='\n\n'.join(c['text'] for c in claims)
        checked=_check_claims(claims,[],answer,rows,'요건은?')
        self.assertEqual(checked['status'],'passed')
        self.assertEqual(checked['numeric_citation_ids'],['intro','item'])
        self.assertEqual(checked['numeric_citation_scope'],'public_selected_citations_v1')
        self.assertTrue(checked['numeric_presence_is_not_semantic_proof'])
        diagnostic=_check_claims(claims,[],answer,rows,'요건은?',
                                numeric_citation_scope='per_claim_selected_citations_v1')
        self.assertEqual(diagnostic['unsupported_numbers'],['6'])
        reviewed={key:True for key in ('supported','complete','citations_sufficient','conditions_satisfied',
                                      'scope_correct','units_correct','calculation_correct')}
        reviewed.update(claims=[{**claim,'supported':True} for claim in claims],calculations=[],issues=[])
        verified=_verify_result(reviewed,answer,rows,'요건은?')
        self.assertEqual(verified['numeric_citation_scope'],'per_claim_selected_citations_v1')
        self.assertEqual(verified['unsupported_numbers'],['6'])
        self.assertEqual(verified['status'],'failed')
        missing=copy.deepcopy(claims);missing[1]['citation_ids']=['intro']
        for scope in ('public_selected_citations_v1','per_claim_selected_citations_v1'):
            self.assertEqual(_check_claims(missing,[],answer,rows,'요건은?',
                                          numeric_citation_scope=scope)['unsupported_numbers'],['6'])
        with self.assertRaisesRegex(AnswerError,'unknown_numeric_citation_scope'):
            _check_claims(claims,[],answer,rows,'요건은?',numeric_citation_scope='all_retrieved')
        unrelated=copy.deepcopy(claims);unrelated[0]['text']='900원입니다.'
        self.assertIn('900',_check_claims(unrelated,[],'\n\n'.join(c['text'] for c in unrelated),rows,'요건은?')['unsupported_numbers'])
        for uid,error in (('absent','unknown_claim_citation'),('item','claim_uses_unrefined_context_citation')):
            bad=copy.deepcopy(claims);bad[1]['citation_ids']=[uid]
            altered=copy.deepcopy(rows);altered[1]['context_only']=True
            with self.subTest(uid=uid),self.assertRaisesRegex(AnswerError,error):
                _check_claims(bad,[],answer,altered,'요건은?')

    def test_clarification_keeps_known_ordinance_answer_and_only_missing_user_facts(self):
        response=generation(decision='needs_clarification',missing_information=['신청일'])
        client=FakeClient(response);result=assess_and_answer('정의와 신청 가능 여부는?',retrieval(),client)
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertEqual(result['decision'],'needs_clarification')
        self.assertEqual(result['response']['answer'],ANSWER)
        self.assertEqual(result['missing_information'],['신청일'])
        self.assertEqual(result['missing_materials'],[])
        self.assertFalse(result['fully_resolved'])
        self.assertFalse(result['answer_allowed'])
        self.assertNotIn('scope_notice',result)
        self.assertEqual([kind for kind, _ in client.calls], ['draft', 'provision_check'])

    def test_external_boundary_and_user_facts_remain_code_owned(self):
        response=generation(decision='external_material_required',missing_materials=['별도 사업 규칙'],
                            missing_information=['실제 신청일'])
        result=assess_and_answer('정의와 자격은?',retrieval(),FakeClient(response))
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertFalse(result['fully_resolved'])
        public=public_result({'status':'completed','result':result},catalog={})
        self.assertEqual(public['response']['answer'],ORDINANCE_SCOPE_NOTICE+'\n\n'+ANSWER)
        self.assertEqual(public['missing_materials'],['별도 사업 규칙'])
        self.assertEqual(public['missing_information'],['실제 신청일'])
        for tamper in ('scope_notice','fully_resolved'):
            bad=copy.deepcopy(result)
            if tamper=='scope_notice':bad[tamper]['text']='확인 완료'
            else:bad[tamper]=True
            with self.assertRaises(ValueError):public_result({'status':'completed','result':bad},catalog={})

    def test_nonanswer_states_have_no_claims_and_never_expand(self):
        for decision in ('insufficient_evidence','out_of_scope','needs_clarification','external_material_required'):
            response=generation(decision=decision,claims=[],missing_information=['신청일'] if decision=='needs_clarification' else [],
                missing_materials=['별도 규칙'] if decision=='external_material_required' else [])
            client=FakeClient(response);result=assess_and_answer('정의?',retrieval(),client)
            self.assertEqual(result['decision'],decision,result)
            self.assertFalse(result['draft_generated']);self.assertFalse(result['request_more_evidence'])
            self.assertEqual(len(client.calls),1)

    def test_nonanswer_claim_and_false_supported_requirements_are_rejected(self):
        for response,error in ((generation(decision='out_of_scope'),'nonanswer_decision_has_claims'),
                               (generation(missing_materials=['필요 규칙']),'supported_with_missing_requirements'),
                               (generation(claims=[]),'supported_without_claims')):
            result=assess_and_answer('정의?',retrieval(),FakeClient(response))
            self.assertEqual(result.get('error_code'),error,result)

    def test_no_context_handle_omits_selector_field_and_rejects_sentinel_without_repair(self):
        client=FakeClient(generation());result=assess_and_answer('정의?',retrieval(),client)
        schema=client.calls[0][1]['response_format']['json_schema']['schema']
        self.assertEqual(result['context_source_catalog'],{})
        for key in ('claims','calculations'):
            self.assertNotIn('context_selections',schema['properties'][key]['items']['properties'])
        self.assertNotIn('__no_available_id__',json.dumps(schema))
        parsed=result['audit'][0]['parsed_response']
        self.assertNotIn('context_selections',parsed['claims'][0])
        self.assertEqual(result['source_selection_trace']['claims'][0]['context_selections'],[])
        bad=generation();bad['claims'][0]['context_selections']=[{
            'context_id':'__no_available_id__','paragraph':'__no_available_id__','item':''}]
        client=FakeClient(bad);result=assess_and_answer('정의?',retrieval(),client)
        self.assertEqual(result['error_code'],'response_unknown_field_or_id')
        self.assertEqual(len(client.calls),1)

    def test_generated_wire_declares_sources_before_claim_text_and_calculation(self):
        from local_rag_answer import _generation_schema
        from local_rag_client import request_wire
        for handles in ([],[{'context_id':'H001','paragraphs':['','1']}]):
            schema=_generation_schema(['C001','C002'],handles)
            optional=['context_selections'] if handles else []
            expected={'claims':['citation_ids']+optional+['text'],
                      'calculations':['citation_ids']+optional+['expression','result']}
            payload={'model':'solar-pro4','response_format':{'type':'json_schema','json_schema':{
                'name':'local_rag_single_generation','strict':True,'schema':schema}}}
            wire,version=request_wire('draft',payload)
            self.assertEqual(version,'ordered_json_v1')
            restored=json.loads(wire)
            self.assertEqual(restored,payload)
            for key,fields in expected.items():
                item=restored['response_format']['json_schema']['schema']['properties'][key]['items']
                self.assertEqual(item['required'],fields)
                self.assertEqual(list(item['properties']),fields)
                self.assertEqual(set(item['properties']),set(fields))
                self.assertEqual(item['properties']['citation_ids']['items']['enum'],['C001','C002'])

    def test_generation_context_excludes_review_interpretations_but_audit_preserves_them(self):
        data=retrieval();candidate=data['evidence'][0]['condition_candidates'][0]
        candidate['review']['condition'].update(interpretation='비법적 감사 해석',caution='검토자 주의 설명',
                                               required_facts=['검토자 추정 사실'],current_target_ids=[])
        candidate['review']['requirements']={'required_facts':['추정 요구']}
        client=FakeClient(generation());result=assess_and_answer('정의?',data,client)
        wire=json.loads(client.calls[0][1]['messages'][1]['content'])
        for text in ('비법적 감사 해석','검토자 주의 설명','검토자 추정 사실','추정 요구'):
            self.assertNotIn(text,json.dumps(wire,ensure_ascii=False))
        self.assertEqual(result['condition_sources'][0]['condition']['current_target_ids'],[])
        self.assertEqual(result['verification_only']['condition_checks'][0]['gate_status'],'not_assessed')
        self.assertIn('비법적 감사 해석',json.dumps(result['verification_only'],ensure_ascii=False))

    def test_external_source_sharing_is_lossless_without_new_citation_candidates(self):
        data=retrieval();original=copy.deepcopy(data)
        shared='외부 참조가 나타나는 로컬 원문 777원.\n'*12
        data['external_tasks']={
            'external-a':{'status':'외부 자료 확인 필요','target_content_verified':False,
                'target':'별도 기준','source_units':['source-a'],'affected_chunk_ids':[CID],
                'source_quotes':[{'quote_text':shared,'source_unit':'source-a'},
                                 {'quote_text':QUOTE,'source_unit':'source-b'}]},
            'external-b':{'status':'참조 대상 확인 필요','target_content_verified':False,
                'target':None,'source_units':['source-c'],'affected_chunk_ids':[],
                'source_quotes':[{'quote_text':shared,'source_unit':'source-c'}]}}
        before=copy.deepcopy(data)
        client=FakeClient(generation());result=assess_and_answer('정의?',data,client)
        baseline_client=FakeClient(generation());baseline=assess_and_answer('정의?',original,baseline_client)
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        wire=json.loads(client.calls[0][1]['messages'][1]['content'])
        expanded=expand_generation_input(wire,result['citation_aliases'])
        self.assertEqual(expanded['external_tasks'],before['external_tasks'])
        self.assertEqual(result['prepared_evidence']['external_tasks'],before['external_tasks'])
        self.assertEqual(data,before)
        self.assertEqual(result['citation_aliases'],baseline['citation_aliases'])
        self.assertEqual(result['context_source_catalog'],baseline['context_source_catalog'])
        quotes=wire['cited_evidence']['external_quotes']
        self.assertEqual(list(quotes.values()).count(shared),1)
        a=wire['external_tasks']['external-a']['source_quotes'][0]
        b=wire['external_tasks']['external-b']['source_quotes'][0]
        self.assertEqual(a['quote_ref'],b['quote_ref'])
        self.assertNotEqual(a['source_unit'],b['source_unit'])
        self.assertFalse(wire['external_tasks']['external-a']['target_content_verified'])
        self.assertLess(len(json.dumps(wire)),len(json.dumps(expanded)))

    def test_external_shared_quote_cannot_supply_claim_or_calculation_numbers(self):
        data=retrieval();data['external_tasks']={'external-a':{
            'status':'외부 자료 확인 필요','target_content_verified':False,
            'source_quotes':[{'source_unit':'external-source','quote_text':'별도 대상 777원'}]}}
        for response in (generation('청년에게 777원을 지급합니다.'),
                         generation('차액은 758원입니다.',calculations=[{
                             'expression':'777-19','result':'758','citation_ids':['C001']}])):
            client=FakeClient(response);result=assess_and_answer('정의?',data,client)
            # The external number still fails the check; that issue alone now releases the draft.
            self.assertEqual(result['draft_status'],'released_with_check_issues',result)
            self.assertIn('answer_contains_unsupported_number',result['code_checks']['issues'])
            self.assertEqual([kind for kind, _ in client.calls],['draft','provision_check'])

    def test_invalid_external_quote_references_cannot_expand(self):
        from local_rag_answer import AnswerError
        data=retrieval();data['external_tasks']={'external-a':{
            'source_quotes':[{'source_unit':'external-source','quote_text':'별도 원문'}]}}
        client=FakeClient(generation());result=assess_and_answer('정의?',data,client)
        wire=json.loads(client.calls[0][1]['messages'][1]['content'])
        for unknown,collision in ((True,False),(False,True)):
            bad=copy.deepcopy(wire);entry=bad['external_tasks']['external-a']['source_quotes'][0]
            if unknown:entry['quote_ref']='Q-does-not-exist'
            if collision:entry['quote_text']='대체 원문'
            with self.assertRaises(AnswerError):expand_generation_input(bad,result['citation_aliases'])

    def test_generation_display_retains_citation_order_and_separate_owner_bindings(self):
        from local_rag_answer import compact_generation_input, compact_model_input, expand_model_input, packed
        client=FakeClient(generation());result=assess_and_answer('정의?',retrieval(),client)
        wire=json.loads(client.calls[0][1]['messages'][1]['content'])
        original=expand_generation_input(wire,result['citation_aliases'])
        row=copy.deepcopy(original['cited_evidence'][0])
        duplicate=copy.deepcopy(row);duplicate.update(unit_id='other-citation',parent_chunk_id='other-owner')
        different=copy.deepcopy(row);different.update(unit_id='different-version',effective_date='2000-01-01')
        original['cited_evidence']=[row,different,duplicate]
        conditions=original['completeness_context']['condition_checks']
        relation={'same_source_unit':False,'matched_explicit_target_ids':[]}
        conditions[0]['owners']={'raw-owner-b':copy.deepcopy(relation),'raw-owner-a':copy.deepcopy(relation),'empty-owner':{}}
        conditions[0]['condition']['current_target_ids']=[]
        before=copy.deepcopy(original)
        compact,crosswalk=compact_generation_input(original)
        legacy,legacy_crosswalk=compact_model_input(original)
        self.assertEqual(crosswalk,legacy_crosswalk)
        self.assertEqual(packed(expand_generation_input(compact,crosswalk)),packed(expand_model_input(legacy,crosswalk)))
        self.assertEqual(original,before)
        records=compact['cited_evidence']['records']
        self.assertEqual(len(records),2)
        self.assertEqual([entry['citation_id'] for entry in records[0]['citations']],['C001','C003'])
        self.assertNotEqual(records[0]['citations'][0]['owner_id'],records[0]['citations'][1]['owner_id'])
        self.assertEqual(compact['cited_evidence']['citation_order'],['C001','C002','C003'])
        owners=compact['completeness_context']['condition_checks'][0]['owners']
        self.assertEqual(list(owners),['raw-owner-b','raw-owner-a','empty-owner'])
        self.assertEqual(owners['raw-owner-b'],owners['raw-owner-a'])
        self.assertNotEqual(owners['raw-owner-b'],owners['empty-owner'])
        self.assertEqual(len(compact['completeness_context']['owner_relations']),2)
        self.assertEqual(expand_generation_input(compact,crosswalk),before)

    def test_generation_display_rejects_identity_override_missing_bindings_and_unknown_relations(self):
        from local_rag_answer import AnswerError
        client=FakeClient(generation());result=assess_and_answer('정의?',retrieval(),client)
        wire=json.loads(client.calls[0][1]['messages'][1]['content'])
        for defect in ('identity','duplicate','missing','unknown_owner','unknown_relation'):
            bad=copy.deepcopy(wire);record=bad['cited_evidence']['records'][0]
            if defect=='identity':record['source']['unit_id']='injected'
            elif defect=='duplicate':record['citations'].append(copy.deepcopy(record['citations'][0]))
            elif defect=='missing':bad['cited_evidence']['citation_order'].pop()
            elif defect=='unknown_owner':record['citations'][0]['owner_id']='O-unknown'
            else:
                owners=bad['completeness_context']['condition_checks'][0]['owners']
                owners[next(iter(owners))]='P-unknown'
            with self.subTest(defect=defect),self.assertRaises(AnswerError):
                expand_generation_input(bad,result['citation_aliases'])

    def test_generation_owner_aliases_connect_records_without_mapping_legal_targets(self):
        from local_rag_answer import _map_generation_owner_references
        relation={'same_source_unit':False,'matched_explicit_target_ids':['raw-source','C001']}
        source={'completeness_context':{'condition_checks':[{
            'owners':{'raw-source':copy.deepcopy(relation),'unknown-owner':{}},
            'condition':{'current_target_ids':['raw-source','C001']},'source_unit_id':'raw-source'},
            {'owners':{},'condition':{'current_target_ids':[]}}],
            'mandatory_contexts':[{'owner_primary_ids':['unknown-owner','raw-source','raw-source'],
                'audit_context_ids':['raw-source'],'source_unit_id':'raw-source'}],
            'owner_relations':{'P1':copy.deepcopy(relation)}},
            'external_tasks':{'external':{'affected_chunk_ids':['raw-source']}}}
        before=copy.deepcopy(source)
        _map_generation_owner_references(source,{'raw-source':'O001'})
        checks=source['completeness_context']['condition_checks']
        self.assertEqual(list(checks[0]['owners']),['O001','unknown-owner'])
        self.assertEqual(checks[0]['owners']['O001'],relation)
        self.assertEqual(checks[0]['condition']['current_target_ids'],['raw-source','C001'])
        self.assertEqual(checks[0]['source_unit_id'],'raw-source')
        self.assertEqual(checks[1],before['completeness_context']['condition_checks'][1])
        mandatory=source['completeness_context']['mandatory_contexts'][0]
        self.assertEqual(mandatory['owner_primary_ids'],['unknown-owner','O001','O001'])
        self.assertEqual(mandatory['audit_context_ids'],['raw-source'])
        self.assertEqual(source['external_tasks'],before['external_tasks'])
        self.assertEqual(source['completeness_context']['owner_relations'],before['completeness_context']['owner_relations'])
        _map_generation_owner_references(source,{'O001':'raw-source'})
        self.assertEqual(source,before)

    def test_generation_owner_alias_collision_is_rejected_for_both_reference_fields(self):
        from local_rag_answer import _map_generation_owner_references, AnswerError
        for field in ('condition','mandatory'):
            for unknown_only in (False,True):
                owners=['C001'] if unknown_only else ['raw-source','C001']
                data={'completeness_context':{'condition_checks':[], 'mandatory_contexts':[]}}
                if field=='condition':
                    data['completeness_context']['condition_checks']=[{'owners':{uid:{} for uid in owners}}]
                else:data['completeness_context']['mandatory_contexts']=[{'owner_primary_ids':owners}]
                with self.subTest(field=field,unknown_only=unknown_only),self.assertRaisesRegex(AnswerError,'generation_owner_alias_collision'):
                    _map_generation_owner_references(data,{'raw-source':'C001'})

    def test_public_rejects_changed_claim_text_cids_or_fabricated_semantic_status(self):
        good=assess_and_answer('정의?',retrieval(),FakeClient(generation()))
        for change in ('text','ids','semantic'):
            bad=copy.deepcopy(good)
            if change=='text':bad['draft_response']['answer']='바뀐 본문'
            elif change=='ids':bad['generated_claims'][0]['citation_ids']=[]
            else:bad['verification']['status']='passed'
            with self.assertRaises(ValueError):public_result({'status':'completed','result':bad},catalog={})


class ClaimGuardTests(unittest.TestCase):
    def checked(self,texts_ids,rows,question='요건은?'):
        from local_rag_answer import _check_claims
        claims=[{'text':text,'citation_ids':ids} for text,ids in texts_ids]
        rows=[{'locator':{},'context_only':False,'effective_date':'2025-07-14','as_of_date':'2026-10-01',**row} for row in rows]
        return _check_claims(claims,[],'\n\n'.join(c['text'] for c in claims),rows,question)

    def test_requested_dated_time_point_must_match_cited_version_or_as_of_year(self):
        rows=[{'unit_id':'a','quote':'시장은 예산의 범위에서 지원할 수 있다.'}]
        claim=[('시장은 예산의 범위에서 지원할 수 있다.',['a'])]
        for question in ('2024년 1월 1일 시행 중이던 조문은?','2028-01-01 기준 지급액은?','2023년 당시 문언은?'):
            with self.subTest(question=question):
                checked=self.checked(claim,rows,question)
                self.assertEqual(checked['status'],'failed')
                self.assertIn('requested_time_point_unverified',checked['issues'])
        for question in ('2025년 7월 시행 판본의 조문은?','2026-10-01 판본 기준 조문은?',
                         '서울특별시고시 제2020-254호의 공원은?','「2024 행사 조례」의 지원은?','2024년에 바뀌었나요?'):
            with self.subTest(question=question):
                self.assertEqual(self.checked(claim,rows,question)['issues'],[])

    def test_intro_only_citation_cannot_carry_sibling_item_content(self):
        rows=[{'unit_id':'p::intro','quote':'시장은 다음 각 호의 어느 하나에 해당하는 불편이 발생한 경우 이용권을 제공할 수 있다.'},
              {'unit_id':'p::item::1','quote':'1. 차체 결함, 시스템 장애로 정상적인 이용이 불가능한 경우'}]
        item_text='차체 결함이나 시스템 장애로 이용이 불가능하면 시장은 이용권을 제공할 수 있다.'
        checked=self.checked([(item_text,['p::intro'])],rows)
        self.assertEqual(checked['status'],'failed')
        self.assertIn('claim_cites_only_intro_of_itemized_paragraph',checked['issues'])
        self.assertEqual(self.checked([(item_text,['p::intro','p::item::1'])],rows)['issues'],[])
        self.assertEqual(self.checked([('불편이 발생한 경우 시장은 각 호에 따라 이용권을 제공할 수 있다.',['p::intro'])],rows)['issues'],[])

    A3='requested_time_point_unverified'

    def test_a3_ignores_user_fact_and_referenced_statute_dates(self):
        # G01 shape: the date is a fact about the user's situation, not the version the question asks for.
        for effective in ('2026-01-01','2025-07-14'):
            rows=[{'unit_id':'a','effective_date':effective,'quote':'소유자는 인식표를 부착하여야 한다.'}]
            for question in ('2023년 3월에 동물등록을 마친 반려견을 키우고 있습니다. 지금 인식표에는 무엇을 표시해야 하나요?',
                             '2014년 12월 1일 처음 점용허가를 받은 어스앵커의 올해 점용료는?',
                             '국토교통부령 제498호(2018.3.21.) 부칙 제3조의 경과조치는?',
                             '2016년에 설치한 제 주차장도 감면 대상인가요?',
                             '2023년 3월 15일에 등록한 반려견도 해당하나요?'):
                with self.subTest(effective=effective,question=question):
                    self.assertNotIn(self.A3,self.guard('소유자는 인식표를 부착하여야 한다.',['a'],rows,question))

    def test_a3_still_fires_on_requested_unknown_time_point(self):
        for effective in ('2026-01-01','2025-07-14'):
            rows=[{'unit_id':'a','effective_date':effective,'quote':'시장은 예산의 범위에서 지원할 수 있다.'}]
            for question in ('2020년 1월 1일의 서울시 금연환경 조례에도 같은 규정이 있었나요? 당시 문언은?',
                             '2024년 1월 1일 시행 중이던 서울시 동물보호 조례의 규정은?',
                             '2028년 1월에 시행될 서울시 청년수당 지급액은?',
                             '2023년 6월 1일 당시 시행 중이던 조문은?',
                             '2024.1.1. 당시 조례의 지원 대상은?',
                             '2024-01-01의 조례는 무엇을 정했나요?'):
                with self.subTest(effective=effective,question=question):
                    self.assertIn(self.A3,self.guard('시장은 예산의 범위에서 지원할 수 있다.',['a'],rows,question))

    def test_a3_known_year_time_point_does_not_fire(self):
        rows=[{'unit_id':'a','effective_date':'2025-07-14','quote':'시장은 청년을 지원할 수 있다.'}]
        for question in ('2026-10-01 기준 청년 지원 요건은?','2025년 7월 14일 시행된 서울시 청년 기본 조례 기준 지원 대상은?'):
            with self.subTest(question=question):
                self.assertNotIn(self.A3,self.guard('시장은 청년을 지원할 수 있다.',['a'],rows,question))

    B2='claim_cites_only_intro_of_itemized_paragraph'

    def guard(self,text,ids,rows,question='요건은?'):
        from local_rag_answer import _claim_guard_issues
        known={row['unit_id']:{'effective_date':'2025-07-14','as_of_date':'2026-10-01',**row} for row in rows}
        return _claim_guard_issues([{'text':text,'citation_ids':ids}],known,question)

    @staticmethod
    def old_b2_overlap(text,intro,item):
        # The pre-fix B2 rule: every guard word of the item not inside the intro, against every claim word.
        from local_rag_answer import _guard_words
        return {w for w in _guard_words(item) if w not in intro} & _guard_words(text)

    def test_b2_ignores_cited_ordinance_name_locators_and_endings(self):
        # M1 shape: the claim restates the intro itself; the old rule fired on 설치 (ordinance name) and 있다고 (ending).
        name='서울특별시 주차장 설치 및 관리 조례'
        intro='시장은 다음 각 호의 어느 하나에 해당할 경우 주차요금을 감면할 수 있다. 다만, 증빙서류를 제시하지 않고 감면받을 수 없다.'
        item='15. 그 밖에 공영주차장 설치 목적에 맞는다고 인정할 수 있다고 시장이 정한 자'
        rows=[{'unit_id':'x::intro','ordin_name':name,'quote':intro},{'unit_id':'x::item::15','ordin_name':name,'quote':item}]
        text=('서울특별시 주차장 설치 및 관리 조례 제7조제1항 본문은 시장이 각 호에 해당할 경우 주차요금을 감면할 수 있다고 정하고, '
              '증빙서류를 제시하지 않고 감면받을 수 있다고 규정한다.')
        self.assertEqual(self.old_b2_overlap(text,intro,item),{'설치','있다고'})
        self.assertNotIn(self.B2,self.guard(text,['x::intro'],rows))

    def test_b2_still_fires_on_item_content_words(self):
        # H31 shape: the claim states item 1's content words with only the intro cited.
        name='서울특별시 공공자전거 이용 조례'
        rows=[{'unit_id':'p::intro','ordin_name':name,'quote':'시장은 다음 각 호의 어느 하나에 해당하는 불편이 발생한 경우 이용권을 제공할 수 있다.'},
              {'unit_id':'p::item::1','ordin_name':name,'quote':'1. 차체 결함, 시스템 장애로 정상적인 이용이 불가능한 경우'}]
        text='차체 결함, 시스템 장애로 서비스가 중단된 경우 시장은 이용권을 제공할 수 있다고 정한다.'
        self.assertIn(self.B2,self.guard(text,['p::intro'],rows))
        self.assertNotIn(self.B2,self.guard(text,['p::intro','p::item::1'],rows))
        # C3 shape: an adoption/donation registration claim placed under item 2 while citing only the intro.
        name='서울특별시 동물보호 조례'
        rows=[{'unit_id':'c::intro','ordin_name':name,'quote':'다음 각 호의 어느 하나에 해당하는 경우에는 등록수수료를 감면한다.'},
              {'unit_id':'c::item::2','ordin_name':name,'quote':'2. 유기견을 입양 또는 기증받아 등록하는 경우'}]
        self.assertIn(self.B2,self.guard('유기견 입양·기증 등록은 제28조 제2호에 해당합니다.',['c::intro'],rows))

    def test_b2_does_not_fire_on_locator_and_ending_overlap_only(self):
        intro='다음 각 호의 어느 하나에 해당하는 자는 지원을 받을 수 있다.'
        item='3. 제5조 본문에 따라 신청한다고 인정되는 자'
        rows=[{'unit_id':'q::intro','ordin_name':'서울특별시 청년 기본 조례','quote':intro},
              {'unit_id':'q::item::3','ordin_name':'서울특별시 청년 기본 조례','quote':item}]
        text='제5조 본문은 신청한다고 정한다.'
        self.assertGreaterEqual(len(self.old_b2_overlap(text,intro,item)),2)
        self.assertNotIn(self.B2,self.guard(text,['q::intro'],rows))

    def test_discretion_claim_cannot_restate_purely_mandatory_source(self):
        rows=[{'unit_id':'m','quote':'시장은 직원이 교육을 받도록 하여야 하며, 민간단체에 교육 시행을 권장하여야 한다.'},
              {'unit_id':'d','quote':'시장은 점검을 보고할 수 있다.'}]
        for text in ('민간단체 교육은 권장 사항으로 같은 강도의 의무가 아니다.','민간단체 교육 권장은 의무(하여야 한다)가 아니라 권장 규정이다.',
                     '시장은 직원 교육을 재량으로 정한다.'):
            with self.subTest(text=text):
                checked=self.checked([(text,['m'])],rows)
                self.assertEqual(checked['status'],'failed')
                self.assertIn('obligation_strength_mismatch',checked['issues'])
        self.assertEqual(self.checked([('직원 교육은 하여야 하므로 선택 사항이 아니다.',['m'])],rows)['issues'],[])
        self.assertEqual(self.checked([('교육은 의무이고 점검 보고는 재량이다.',['m','d'])],rows)['issues'],[])


class SelfReferentialMissingMaterialTests(unittest.TestCase):
    SELF=["서울특별시 동물보호 조례상 '반려동물 양육수당'이라는 명칭의 매달 지급 수당 규정",
          "서울특별시 청년 기본 조례에서 사용하는 '청년'의 연령 정의(제공된 원문에는 연령 정의가 포함되어 있지 않음)",
          "별표 1의 연령·단체 정의가 별표 2에 준용된다는 조례 본문 또는 부칙상 근거",
          "서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제5조(금연구역 지정 등) — 금연구역의 범위·지정 대상(원문 미제공)"]
    APPLICATION_DOC=("별표 1의 어린이·어른·청소년·단체의 나이 및 인원 정의가 별표 2의 청소년 체험의 숲 "
                     "이용료에 그대로 적용되는지에 관한 적용 관계 문서")
    EXTERNAL=[APPLICATION_DOC,
              "별표 2 내 '단체' 정의 또는 인원 기준(조례 본문 제15조·제20조 등 포함)",
              "서울특별시 도시공원 조례의 단체 정의가 적용되는지 확인할 수 있는 운영 자료",
              "서울특별시 주차장 설치 및 관리 조례상 감면 대상 확인 방법",
              "서울특별시 주차장 설치 및 관리 조례 시행규칙의 감면 신청 서식",
              "시장이 따로 정하는 보조금 산정기준·지원절차",
              "해당 양육수당의 신청 장소·방법·신청처를 정한 조례 또는 관련 고시·공고·사업 지침",
              "서울특별시 청년 기본 조례 제21조에 따른 청년시설 운영 규칙",
              "2028년 1월 시점의 서울특별시 청년 기본 조례 판본",
              "「국민건강증진법」의 과태료 규정",
              "「경기도 주차장 조례」의 요금 규정",
              "실제 허가서와 관리청의 환불 결정"]

    def test_self_referential_items_fire(self):
        from local_rag_answer import _self_referential_missing_materials
        for item in self.SELF:
            with self.subTest(item=item):
                self.assertEqual(_self_referential_missing_materials([item]),[item])

    def test_external_mixed_delegated_version_and_other_ordinance_items_do_not_fire(self):
        from local_rag_answer import _self_referential_missing_materials
        for item in self.EXTERNAL:
            with self.subTest(item=item):
                self.assertEqual(_self_referential_missing_materials([item]),[])
        self.assertEqual(_self_referential_missing_materials(self.EXTERNAL+self.SELF[:1]),self.SELF[:1])

    def test_pipeline_withholds_external_decision_naming_the_ordinance_itself(self):
        external='서울특별시 주차장 설치 및 관리 조례 시행규칙의 감면 신청 서식'
        materials=[self.SELF[0],external]
        result=assess_and_answer('반려동물 양육수당은 얼마인가요?',retrieval(),
            FakeClient(generation(decision='external_material_required',missing_materials=materials)))
        self.assertEqual(result['draft_status'],'held',result)
        self.assertIn('self_referential_missing_material',result['code_checks']['issues'])
        self.assertEqual(result['code_checks']['self_referential_missing_materials'],[self.SELF[0]])
        self.assertEqual(result['decision'],'external_material_required')
        self.assertEqual(result['missing_materials'],materials)
        self.assertEqual(result['response']['answer'],'확인 불가')
        clean=assess_and_answer('반려동물 양육수당은 얼마인가요?',retrieval(),
            FakeClient(generation(decision='external_material_required',missing_materials=[external])))
        self.assertEqual(clean['draft_status'],'code_checked_draft',clean)
        self.assertNotIn('self_referential_missing_material',clean['code_checks']['issues'])
        self.assertNotIn('self_referential_missing_materials',clean['code_checks'])

    def test_pipeline_keeps_application_relation_document_as_external(self):
        result=assess_and_answer('청소년 체험의 숲 이용료는 얼마인가요?',retrieval(),
            FakeClient(generation(decision='external_material_required',missing_materials=[self.APPLICATION_DOC])))
        self.assertEqual(result['draft_status'],'code_checked_draft',result)
        self.assertNotIn('self_referential_missing_material',result['code_checks']['issues'])
        self.assertNotIn('self_referential_missing_materials',result['code_checks'])
        self.assertEqual(result['missing_materials'],[self.APPLICATION_DOC])

    def test_clarification_turned_external_is_also_checked(self):
        result=assess_and_answer('양육수당은?',retrieval(),
            FakeClient(generation(decision='needs_clarification',missing_information=['신청일'],
                                  missing_materials=[self.SELF[1]])))
        self.assertEqual(result['decision'],'external_material_required')
        self.assertEqual(result['draft_status'],'held',result)
        self.assertEqual(result['code_checks']['self_referential_missing_materials'],[self.SELF[1]])


YOUTH = {"ordin_name": "서울특별시 청년 기본 조례", "ordin_seq": "2054497"}
SMOKE = {"ordin_name": "서울특별시 금연환경 조성 및 간접흡연 피해방지조례", "ordin_seq": "2171803"}


def attribution_retrieval(units):
    """Body units of the youth ordinance, one evidence item each; no contexts or conditions."""
    source = {**YOUTH, "as_of_date": "2026-10-01", "effective_date": "2025-07-14",
              "source_url": "https://www.law.go.kr/ordinInfoP.do?ordinSeq=2054497"}
    evidence = []
    for cid, article, paragraph, quote in units:
        parent = cid.rsplit(":", 1)[0]
        record = {**source, "article": article, "paragraph": paragraph, "quote_text": quote, "parent_id": parent,
                  "source_span": {"start": 10, "end": 10 + len(quote)}}
        evidence.append({"chunk_id": cid, "kind": "body", "as_of_date": "2026-10-01", "effective_date": "2025-07-14",
                         "text": "embedding representation", "is_deleted": False,
                         "source": {"source_unit_id": parent, "source_record": record, "required_context_ids": [],
                                    "dependency_primary_ids": [], "external_reference_ids": []},
                         "required_contexts": [], "condition_candidates": []})
    return {"as_of_date": "2026-10-01", "evidence": evidence, "external_tasks": {}, "top_k": 8}


class ClaimAttributionLocatorTests(unittest.TestCase):
    ISSUE = "claim_attributes_uncited_provision"
    ROWS = [{"unit_id": "y22-2:1", **YOUTH, "locator": {"article": "제22조의2", "paragraph": 1}},
            {"unit_id": "y22-2:2", **YOUTH, "locator": {"article": "제22조의2", "paragraph": 2}},
            {"unit_id": "y24:all", **YOUTH, "locator": {"article": "제24조"}},
            {"unit_id": "y5:3", **YOUTH, "locator": {"article": "제5조", "paragraph": 3}},
            {"unit_id": "s5:1::intro", **SMOKE, "locator": {"article": "제5조", "paragraph": 1}},
            {"unit_id": "s5:1::item::2", **SMOKE, "locator": {"article": "제5조", "paragraph": 1, "item": "2"}},
            {"unit_id": "s5:3", **SMOKE, "locator": {"article": "제5조", "paragraph": 3}},
            {"unit_id": "s10:1", **SMOKE, "locator": {"article": "제10조", "paragraph": 1}},
            {"unit_id": "s-supplement", **SMOKE, "locator": {"supplement_id": "0019"}}]
    G02 = ("2025년 7월 14일 시행된 서울특별시 청년 기본 조례 제22조의2 제1항에 따르면, 시장은 청년의 날 취지에 적합한 "
           "행사와 교육·홍보사업을 실시할 수 있으므로, 시장이 청년의 날 행사를 반드시 열어야 하는 것은 아니다.")
    G15 = ("같은 조례 제5조제4항은 누구든지 제1항에 따라 지정된 금연구역에서 흡연을 해서는 안 된다고 정하고, "
           "제10조제1항은 그 흡연 위반에 대해 10만원의 과태료를 부과·징수한다고 정하므로, 지정된 금연구역에서의 "
           "흡연 행위가 과태료 부과 대상이다.")

    def unmatched(self, text, ids):
        from local_rag_answer import _unmatched_attributions
        known = {row["unit_id"]: row for row in self.ROWS}
        return [m["reference_text"] for m in _unmatched_attributions({"text": text, "citation_ids": ids}, known)]

    def test_attributed_provision_without_matching_citation_fires(self):
        self.assertEqual(self.unmatched(self.G02, ["y22-2:2"]), ["제22조의2 제1항"])
        self.assertEqual(self.unmatched(self.G15, ["s10:1", "s5:3"]), ["제5조제4항"])
        self.assertEqual(self.unmatched("서울특별시 청년 기본 조례 제24조는 청년의 날 기념을 정한다.", ["y22-2:1"]), ["제24조"])
        # A named ordinance is compared by ordinance, not only by article and paragraph.
        self.assertEqual(self.unmatched("서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제5조제3항은 금연구역을 정한다.",
                                        ["y5:3"]), ["제5조제3항"])
        from local_rag_answer import _claim_guard_issues
        known = {row["unit_id"]: {"effective_date": "2025-07-14", "as_of_date": "2026-10-01", "quote": "시장은 지원할 수 있다.",
                                  **row} for row in self.ROWS}
        self.assertIn(self.ISSUE, _claim_guard_issues([{"text": self.G02, "citation_ids": ["y22-2:2"]}], known, "요건은?"))
        self.assertNotIn(self.ISSUE, _claim_guard_issues([{"text": self.G02, "citation_ids": ["y22-2:1"]}], known, "요건은?"))

    def test_cross_references_external_laws_supplements_and_matching_units_do_not_fire(self):
        cases = [
            ("서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조제1항은 시장이 제5조제4항을 위반하여 흡연을 한 사람에게 "
             "과태료를 부과한다고 정한다.", ["s10:1"]),
            ("같은 조례 제22조의2 제2항에 따르면, 시장은 단체가 제1항에 따른 청년의 날 행사를 개최하면 지원할 수 있다.", ["y22-2:2"]),
            ("제17조에 따른 점용료는 매년 부과한다.", ["y22-2:1"]),
            ("제11조에 따라 공고하여야 한다.", ["y22-2:1"]),
            ("제1항은 시장의 재량을 정한다.", ["y22-2:2"]),
            ("「청년기본법」 제7조는 청년의 날을 정한다.", ["y22-2:1"]),
            ("시장은 법 제9조는 물론 조례에 따라 지원한다.", ["y22-2:1"]),
            ("도로법 시행령 제69조제1항은 점용료 산정 기준을 정한다.", ["y22-2:1"]),
            ("부칙 <제8060호, 2021.7.20> 제2조는 개정규정을 이 조례 시행 전 인증을 받은 사람에게도 적용한다고 규정한다.",
             ["s-supplement"]),
            ("서울특별시 청년 기본 조례 제24조제2항은 청년단체 지원을 정한다.", ["y24:all"]),
            ("같은 조례 제5조제1항제2호는 금연구역 지정 대상을 정한다.", ["s5:1::intro", "s5:1::item::2"]),
            ("서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제5조제3항은 금연구역을 정한다.", ["y22-2:1", "s5:3"]),
            ("별표 2(제22조의2 제1항은 관련) 요금은 정액이다.", ["y22-2:2"]),
            ("2026년 10월 1일 개정 부칙(서울특별시 주차장 설치 및 관리 조례 부칙 <제10187호, 2026.10.1>) 제2조는 "
             "종전 보훈신분증에 관한 경과조치를 규정합니다.", ["y22-2:2"]),
            ("개정 부칙(2026.10.1.) 제2조는 경과조치를 정한다.", ["y22-2:2"]),
        ]
        for text, ids in cases:
            with self.subTest(text=text):
                self.assertEqual(self.unmatched(text, ids), [])

    def run_pipeline(self, units, claims, question="2025년 7월 14일 시행된 청년 기본 조례 기준 청년의 날 행사 의무는?"):
        aliases = assess_and_answer(question, attribution_retrieval(units),
                                    FakeClient(generation(decision="insufficient_evidence", claims=[])))["citation_aliases"]
        alias_of = {uid: alias for alias, uid in aliases.items()}
        raw = generation(claims=[{"text": text, "citation_ids": [alias_of[uid] for uid in ids]} for text, ids in claims])
        return assess_and_answer(question, attribution_retrieval(units), FakeClient(raw))

    P1 = ("2054497:002202:1", "제22조의2", 1, " ① 시장은 청년의 날 취지에 적합한 행사와 교육·홍보사업을 실시할 수 있다.\n")
    P2 = ("2054497:002202:2", "제22조의2", 2, " ② 시장은 청년 관련 단체가 제1항에 따른 청년의 날 행사를 개최하는 경우 지원할 수 있다.\n")
    ITEMIZED = ("2054497:000500:1", "제5조", 1, " ① 시장은 다음 각 호의 사업을 한다.\n  1. 교육\n  2. 홍보\n")
    SECOND = "같은 조례 제22조의2 제2항에 따르면, 시장은 청년 관련 단체가 제1항에 따른 청년의 날 행사를 개최하는 경우 지원할 수 있다."

    def test_single_catalog_row_is_added_to_the_claim_and_every_derived_selection(self):
        result = self.run_pipeline([self.P2, self.P1, self.ITEMIZED],
                                   [(self.G02, ["2054497:002202:2"]), (self.SECOND, ["2054497:002202:2"])])
        self.assertEqual(result["draft_status"], "code_checked_draft", result["code_checks"])
        self.assertEqual(result["citation_repairs"], [{
            "claim_index": 0, "reference_text": "제22조의2 제1항", "claim_span": {"start": 32, "end": 42},
            "added_citation_ids": ["2054497:002202:1"], "original_citation_ids": ["2054497:002202:2"],
            "reason": "attributed_provision_in_catalog"}])
        self.assertEqual(self.G02[32:42], "제22조의2 제1항")
        self.assertEqual(result["generated_claims"][0]["citation_ids"], ["2054497:002202:2", "2054497:002202:1"])
        self.assertEqual(result["generated_claims"][1]["citation_ids"], ["2054497:002202:2"])
        self.assertEqual(result["generated_claims"], result["code_checks"]["checked_claims"])
        self.assertEqual(result["selected_citation_ids"], ["2054497:002202:1", "2054497:002202:2"])
        self.assertEqual(result["code_checks"]["numeric_citation_ids"], ["2054497:002202:1", "2054497:002202:2"])
        # The model's own selection remains in the trace and the raw receipt.
        self.assertEqual(result["source_selection_trace"]["claims"][0]["citation_ids"], ["2054497:002202:2"])
        self.assertEqual(result["audit"][0]["parsed_response"]["claims"][0]["citation_ids"], ["Ca22-2.p2"])
        public = public_result({"status": "completed", "result": result}, catalog={})
        self.assertEqual(public["status"], "code_checked_draft")
        self.assertEqual(len(public["response"]["citation"]), 2)
        from evaluate_local_rag import source_selection_errors
        self.assertEqual(source_selection_errors(result), [])

    def test_absent_row_holds_without_repair(self):
        result = self.run_pipeline([self.P2, self.ITEMIZED], [(self.G02, ["2054497:002202:2"])])
        self.assertEqual(result["draft_status"], "released_with_check_issues")
        self.assertIn(self.ISSUE, result["code_checks"]["issues"])
        self.assertEqual(result["citation_repairs"], [])
        self.assertEqual(result["generated_claims"][0]["citation_ids"], ["2054497:002202:2"])
        self.assertEqual(result["response"], result["draft_response"])

    def test_itemized_paragraph_with_several_rows_holds_without_repair(self):
        text = "서울특별시 청년 기본 조례 제5조제1항은 시장이 교육과 홍보 사업을 한다고 정한다."
        result = self.run_pipeline([self.P2, self.P1, self.ITEMIZED], [(text, ["2054497:002202:2"])],
                                   question="청년 기본 조례 제5조의 사업은?")
        self.assertEqual(result["draft_status"], "released_with_check_issues")
        self.assertIn(self.ISSUE, result["code_checks"]["issues"])
        self.assertEqual(result["citation_repairs"], [])

    def test_matching_attribution_records_no_repair(self):
        result = self.run_pipeline([self.P2, self.P1], [(self.SECOND, ["2054497:002202:2"])])
        self.assertEqual(result["draft_status"], "code_checked_draft", result["code_checks"])
        self.assertEqual(result["citation_repairs"], [])
        default = assess_and_answer("q?", attribution_retrieval([self.P1]),
                                    FakeClient(generation(decision="insufficient_evidence", claims=[])))
        self.assertEqual(default["citation_repairs"], [])

    ITEMIZED_LONG = ("2054497:000500:1", "제5조", 1, " ① 시장은 다음 각 호의 사업을 한다.\n"
                     "  1. 진로 교육 프로그램 운영\n  2. 정책 홍보 캠페인 추진\n")

    def test_other_original_issue_blocks_repair(self):
        # A named provision that is one catalog row is now cited before any check
        # (W1 attachment), whatever the other issues; the added row cannot hide
        # the unsupported number, which alone releases the draft with the issue kept.
        text = self.G02 + " 행사는 연 3회 이상 연다."
        result = self.run_pipeline([self.P2, self.P1], [(text, ["2054497:002202:2"])])
        self.assertEqual(result["draft_status"], "released_with_check_issues")
        self.assertEqual([repair["added_citation_ids"] for repair in result["citation_repairs"]], [["2054497:002202:1"]])
        self.assertEqual(result["code_checks"]["issues"], ["answer_contains_unsupported_number"])
        self.assertEqual(result["code_checks"]["unsupported_numbers"], ["3"])
        self.assertEqual(result["generated_claims"][0]["citation_ids"], ["2054497:002202:2", "2054497:002202:1"])
        # Intro-only citation stating item content (B2): adding 제22조의2 제1항 must not hide it.
        text = "서울특별시 청년 기본 조례 제22조의2 제1항에 따르면 시장은 진로 교육 프로그램 운영을 한다."
        result = self.run_pipeline([self.P2, self.P1, self.ITEMIZED_LONG], [(text, ["2054497:000500:1::intro"])],
                                   question="청년 기본 조례 제22조의2 제1항의 사업은?")
        self.assertEqual(result["draft_status"], "released_with_check_issues")
        self.assertEqual(result["citation_repairs"], [])
        self.assertIn("claim_cites_only_intro_of_itemized_paragraph", result["code_checks"]["issues"])
        self.assertIn(self.ISSUE, result["code_checks"]["issues"])

    def test_repair_is_all_or_nothing(self):
        text = "서울특별시 청년 기본 조례 제22조의2 제1항은 행사 실시를 정하고, 제5조제1항은 사업을 정한다."
        result = self.run_pipeline([self.P2, self.P1, self.ITEMIZED], [(text, ["2054497:002202:2"])],
                                   question="청년 기본 조례 제22조의2 제1항과 제5조제1항은?")
        self.assertEqual(result["draft_status"], "released_with_check_issues")
        self.assertEqual(result["code_checks"]["issues"], [self.ISSUE])
        self.assertEqual(result["citation_repairs"], [])
        self.assertEqual(result["generated_claims"][0]["citation_ids"], ["2054497:002202:2"])

    def test_rows_cited_by_another_claim_are_preferred_and_keep_the_public_union(self):
        first = "서울특별시 청년 기본 조례 제5조제1항에 따르면 시장은 교육 사업을 한다."
        second = "같은 조례 제5조제1항은 시장이 하는 사업을 정하고, 제22조의2 제2항에 따르면 시장은 단체 행사를 지원할 수 있다."
        result = self.run_pipeline([self.P2, self.P1, self.ITEMIZED],
                                   [(first, ["2054497:000500:1::intro", "2054497:000500:1::item::1"]),
                                    (second, ["2054497:002202:2"])],
                                   question="청년 기본 조례 제5조제1항과 제22조의2 제2항은?")
        self.assertEqual(result["draft_status"], "code_checked_draft", result["code_checks"])
        self.assertEqual(result["citation_repairs"], [{
            "claim_index": 1, "reference_text": "제5조제1항", "claim_span": {"start": 6, "end": 12},
            "added_citation_ids": ["2054497:000500:1::intro", "2054497:000500:1::item::1"],
            "original_citation_ids": ["2054497:002202:2"], "reason": "attributed_provision_cited_by_other_claim"}])
        self.assertEqual(result["generated_claims"][1]["citation_ids"],
                         ["2054497:002202:2", "2054497:000500:1::intro", "2054497:000500:1::item::1"])
        self.assertEqual(result["selected_citation_ids"],
                         sorted(["2054497:000500:1::intro", "2054497:000500:1::item::1", "2054497:002202:2"]))
        self.assertEqual(result["generated_claims"], result["code_checks"]["checked_claims"])
        from evaluate_local_rag import source_selection_errors
        self.assertEqual(source_selection_errors(result), [])


PARK = {"ordin_name": "서울특별시 주차장 설치 및 관리 조례", "ordin_seq": "2171833"}
ANIMAL = {"ordin_name": "서울특별시 동물보호 조례", "ordin_seq": "2171829"}


class NamedProvisionTests(unittest.TestCase):
    """W1: position labels are not values; a named provision the catalog resolves once is cited."""

    def row(self, uid, meta, article=None, paragraph=None, item=None, quote="원문.", **extra):
        locator = {key: value for key, value in (("article", article), ("paragraph", paragraph), ("item", item))
                   if value is not None}
        return {"unit_id": uid, **meta, "locator": locator, "quote": quote, "effective_date": "2025-07-14",
                "as_of_date": "2026-10-01", **extra}

    def q03_rows(self):
        return [self.row("p17:4", PARK, "제17조", 4, quote="④ 규모는 사업부지면적의 0.6퍼센트 이상으로 한다."),
                self.row("p17:2::intro", PARK, "제17조", 2, quote="② 단지조성사업 등의 종류는 다음 각 호와 같다."),
                self.row("p17:2::item::1", PARK, "제17조", 2, "1", quote="1. 택지개발사업"),
                self.row("p17:2::item::3", PARK, "제17조", 2, "3", quote="3. 산업단지개발사업"),
                self.row("p17::title", PARK, "제17조", quote="제17조(주차장설치의 고시)", context_only=True)]

    def attach_and_check(self, claims, rows, question="기준은?"):
        from local_rag_answer import _attach_named_provisions, _check_claims
        claims = [{"text": text, "citation_ids": list(ids)} for text, ids in claims]
        repairs = _attach_named_provisions(claims, rows)
        answer = "\n\n".join(claim["text"] for claim in claims)
        return repairs, claims, _check_claims(claims, [], answer, rows, question)

    def test_named_item_is_cited_and_its_position_numbers_are_not_values(self):
        text = "「서울특별시 주차장 설치 및 관리 조례」 제17조제2항제3호의 산업단지개발사업에 해당합니다."
        repairs, claims, checked = self.attach_and_check([(text, ["p17:4"])], self.q03_rows())
        self.assertEqual(claims[0]["citation_ids"], ["p17:4", "p17:2::item::3"])
        self.assertEqual(repairs, [{"claim_index": 0, "reference_text": "제17조제2항제3호",
                                    "claim_span": {"start": text.index("제17조"), "end": text.index("의 산업")},
                                    "added_citation_ids": ["p17:2::item::3"], "original_citation_ids": ["p17:4"],
                                    "reason": "attributed_provision_in_catalog"}])
        self.assertEqual(checked["status"], "passed", checked)
        # A paragraph naming an itemized paragraph resolves to several rows: nothing is added.
        repairs, claims, _ = self.attach_and_check([("같은 조례 제17조제2항의 사업입니다.", ["p17:4"])], self.q03_rows())
        self.assertEqual((repairs, claims[0]["citation_ids"]), ([], ["p17:4"]))

    def test_positions_absent_from_the_catalog_or_external_keep_their_numbers(self):
        rows = self.q03_rows()
        # The whole label must name a catalog position; otherwise all of its numbers stay.
        for text, numbers in (("같은 조례 제17조제9항에 따라 정한다.", ["9"]),
                              ("같은 조례 제17조제2항제8호의 사업이다.", ["2", "8"]),
                              ("「도시교통정비 촉진법 시행령」 제33조에 따른 계획으로 정한다.", ["33"])):
            with self.subTest(text=text):
                repairs, claims, checked = self.attach_and_check([(text, ["p17:4"])], rows)
                self.assertEqual(repairs, [])
                self.assertEqual(claims[0]["citation_ids"], ["p17:4"])
                self.assertEqual(checked["unsupported_numbers"], numbers, checked)

    def test_restated_cross_reference_and_uncited_claim_are_not_completed(self):
        rows = self.q03_rows() + [self.row("p6:2::intro", PARK, "제6조", 2,
                                           quote="② 가산금은 다음 각 호의 경우에 부과한다. 다만, 제17조제2항제3호에 해당하면 유예한다.")]
        text = "다만, 제17조제2항제3호에 해당하면 유예합니다."
        repairs, claims, _ = self.attach_and_check([(text, ["p6:2::intro"])], rows)
        self.assertEqual((repairs, claims[0]["citation_ids"]), ([], ["p6:2::intro"]))
        repairs, claims, checked = self.attach_and_check(
            [("「서울특별시 주차장 설치 및 관리 조례」 제17조제2항제3호의 사업입니다.", [])], rows)
        self.assertEqual((repairs, claims[0]["citation_ids"]), ([], []))
        self.assertIn("claim_without_owned_citations", checked["issues"])

    def test_parenthesized_list_completes_the_missing_paragraph(self):
        rows = [self.row("y20:1", YOUTH, "제20조", 1, quote="① 시장은 서울청년센터를 설치·운영할 수 있다."),
                self.row("y20:2::intro", YOUTH, "제20조", 2, quote="② 광역센터는 다음 각 호의 기능을 수행한다."),
                self.row("y20:3::intro", YOUTH, "제20조", 3, quote="③ 자치구센터는 다음 각 호의 기능을 수행한다.")]
        text = "두 센터는 각각 제20조 제2항과 제3항에 열거된 기능을 수행한다. (서울특별시 청년 기본 조례 제20조 제1항, 제2항, 제3항)"
        repairs, claims, checked = self.attach_and_check([(text, ["y20:2::intro", "y20:3::intro"])], rows)
        self.assertEqual(claims[0]["citation_ids"], ["y20:2::intro", "y20:3::intro", "y20:1"])
        self.assertEqual([repair["reference_text"] for repair in repairs], ["제20조 제1항"])
        self.assertEqual(checked["status"], "passed", checked)

    def test_article_less_label_takes_the_one_cited_article(self):
        rows = [self.row("a2::intro", YOUTH, "제2조", quote="이 조례에서 사용하는 용어의 뜻은 다음과 같다."),
                self.row("a2::item::6", YOUTH, "제2조", item="6", quote="6. 청년시설이란 시설을 말한다."),
                self.row("a2::item::7", YOUTH, "제2조", item="7", quote="7. 청년단체란 단체를 말한다."),
                self.row("a5:1", YOUTH, "제5조", 1, quote="① 시장은 지원할 수 있다.")]
        text = "청년시설은 시설을 말하고(제6호), 청년단체는 단체를 말한다(제7호)."
        repairs, claims, checked = self.attach_and_check([(text, ["a2::intro", "a2::item::6"])], rows)
        self.assertEqual(claims[0]["citation_ids"], ["a2::intro", "a2::item::6", "a2::item::7"])
        self.assertEqual(checked["status"], "passed", checked)
        # With citations in two articles the label is not resolved: no repair, number kept.
        repairs, claims, checked = self.attach_and_check([(text, ["a2::item::6", "a5:1"])], rows)
        self.assertEqual(repairs, [])
        self.assertEqual(checked["unsupported_numbers"], ["7"], checked)

    def test_annex_section_number_needs_its_heading_in_the_catalog(self):
        from local_rag_answer import _check_claims
        rows = [self.row("t", PARK, quote="[별표 3] 공원시설 이용료\n종 별: 글램핑"),
                self.row("t::context::section", PARK, quote="2. 휴양시설")]
        text = "별표 3 2. 휴양시설 중 글램핑 이용료입니다."
        claims = [{"text": text, "citation_ids": ["t"]}]
        self.assertEqual(_check_claims(claims, [], text, rows, "이용료는?")["status"], "passed")
        self.assertEqual(_check_claims(claims, [], text, rows[:1], "이용료는?")["unsupported_numbers"], ["2"])

    def test_pipeline_record_passes_the_saved_selection_validator(self):
        p1 = ClaimAttributionLocatorTests.P1
        itemized = ("2054497:000500:1", "제5조", 1, " ① 시장은 다음 각 호의 사업을 한다.\n  1. 진로 교육\n  2. 정책 홍보\n")
        text = "청년 기본 조례 제5조제1항제2호의 정책 홍보 사업도 시장이 한다."
        result = ClaimAttributionLocatorTests.run_pipeline(self, [p1, itemized], [(text, ["2054497:002202:1"])],
                                                           question="청년 기본 조례의 사업은?")
        self.assertEqual(result["draft_status"], "code_checked_draft", result["code_checks"])
        self.assertEqual(result["generated_claims"][0]["citation_ids"], ["2054497:002202:1", "2054497:000500:1::item::2"])
        self.assertEqual(result["source_selection_trace"]["claims"][0]["citation_ids"], ["2054497:002202:1"])
        self.assertEqual([repair["original_citation_ids"] for repair in result["citation_repairs"]], [["2054497:002202:1"]])
        from evaluate_local_rag import source_selection_errors
        self.assertEqual(source_selection_errors(result), [])


class ClauseObligationTests(unittest.TestCase):
    """W3: a discretion statement is compared with the clause of the cited unit it talks about."""
    ISSUE = "obligation_strength_mismatch"
    INTRO = ("② 공무원이 아닌 위원은 다음 각 호의 어느 하나에 해당하는 사람 중에서 시장이 위촉하되, "
             "「양성평등기본법」 제21조제2항 본문에 따라 특정 성별이 위촉직 위원 수의 10분의 6을 초과하지 아니하도록 하여야 한다.")

    def issues(self, text, rows, ids):
        from local_rag_answer import _claim_guard_issues
        known = {row["unit_id"]: {"effective_date": "2025-07-14", "as_of_date": "2026-10-01", **ANIMAL, **row}
                 for row in rows}
        return _claim_guard_issues([{"text": text, "citation_ids": ids}], known, "요건은?")

    def test_discretion_about_a_non_mandatory_clause_is_not_a_mismatch(self):
        rows = [{"unit_id": "a5:2::intro", "quote": self.INTRO, "locator": {"article": "제5조", "paragraph": 2}},
                {"unit_id": "a5:2::item::4", "quote": "4. 명예동물보호관으로서 구청장의 추천을 받은 사람",
                 "locator": {"article": "제5조", "paragraph": 2, "item": "4"}}]
        ids = ["a5:2::item::4", "a5:2::intro"]
        self.assertNotIn(self.ISSUE, self.issues("다만, 위촉 여부는 시장이 각 호 요건 해당자를 위촉하는 재량 사항입니다.", rows, ids))
        self.assertIn(self.ISSUE, self.issues("특정 성별이 위촉직 위원 수의 10분의 6을 초과하지 않도록 하는 것은 재량 사항입니다.",
                                              rows, ids))

    def test_named_unit_is_the_comparison_target(self):
        rows = [{"unit_id": "s4:1", "quote": "① 시장은 금연환경을 조성하는 조치를 해야한다.",
                 "locator": {"article": "제4조", "paragraph": 1}},
                {"unit_id": "s4:2", "quote": "② 시장은 금연환경 조성을 위한 시책을 수립한다.",
                 "locator": {"article": "제4조", "paragraph": 2}}]
        ids = ["s4:1", "s4:2"]
        self.assertIn(self.ISSUE, self.issues("제4조제1항의 금연환경 조치는 시장의 재량입니다.", rows, ids))
        self.assertNotIn(self.ISSUE, self.issues("제4조제2항의 시책 수립 방식은 시장의 재량입니다.", rows, ids))
        # With no clause sharing words, the unit-level rule is kept.
        self.assertIn(self.ISSUE, self.issues("이는 재량입니다.", rows[:1], ids[:1]))


class IntroOnlyItemCitationRepairTests(unittest.TestCase):
    """B2 repair: add the items the guard names only when another claim of the same answer cites them all."""
    B2 = "claim_cites_only_intro_of_itemized_paragraph"
    ITEMIZED = ("2054497:000500:1", "제5조", 1, " ① 시장은 다음 각 호의 사업을 한다.\n"
                "  1. 진로 교육 프로그램 운영\n  2. 정책 홍보 캠페인 추진\n")
    INTRO, ITEM1, ITEM2 = "2054497:000500:1::intro", "2054497:000500:1::item::1", "2054497:000500:1::item::2"
    FIRST = "시장은 진로 교육 프로그램 운영 사업을 한다."
    SECOND = "그 사업에는 진로 교육 프로그램 운영이 포함된다."
    BOTH = "그 사업에는 진로 교육 프로그램 운영과 정책 홍보 캠페인 추진이 포함된다."
    QUESTION = "청년 기본 조례 제5조의 사업은?"

    def run_pipeline(self, claims):
        return ClaimAttributionLocatorTests.run_pipeline(self, [self.ITEMIZED], claims, question=self.QUESTION)

    def test_item_cited_by_other_claim_is_added_and_published(self):
        result = self.run_pipeline([(self.FIRST, [self.INTRO, self.ITEM1]), (self.SECOND, [self.INTRO])])
        self.assertEqual(result["draft_status"], "code_checked_draft", result["code_checks"])
        self.assertEqual(result["citation_repairs"], [{
            "claim_index": 1, "reference_text": self.SECOND, "claim_span": {"start": 0, "end": len(self.SECOND)},
            "added_citation_ids": [self.ITEM1], "original_citation_ids": [self.INTRO],
            "reason": "stated_item_cited_by_other_claim"}])
        self.assertEqual(result["generated_claims"][1]["citation_ids"], [self.INTRO, self.ITEM1])
        self.assertEqual(result["generated_claims"], result["code_checks"]["checked_claims"])
        self.assertEqual(result["selected_citation_ids"], sorted([self.INTRO, self.ITEM1]))
        # The model's own selection stays in the trace.
        self.assertEqual(result["source_selection_trace"]["claims"][1]["citation_ids"], [self.INTRO])
        public = public_result({"status": "completed", "result": result}, catalog={})
        self.assertEqual(public["status"], "code_checked_draft")
        from evaluate_local_rag import source_selection_errors
        self.assertEqual(source_selection_errors(result), [])

    def test_item_not_cited_by_other_claim_holds_unchanged(self):
        for claims in ([(self.SECOND, [self.INTRO])],
                       [(self.FIRST.replace("진로 교육 프로그램 운영", "정책 홍보 캠페인 추진"), [self.INTRO, self.ITEM2]),
                        (self.SECOND, [self.INTRO])]):
            with self.subTest(claims=claims):
                result = self.run_pipeline(claims)
                self.assertEqual(result["draft_status"], "released_with_check_issues")
                self.assertEqual(result["code_checks"]["issues"], [self.B2])
                self.assertEqual(result["citation_repairs"], [])
                self.assertEqual(result["generated_claims"][-1]["citation_ids"], [self.INTRO])

    def test_other_original_issue_blocks_repair(self):
        second = self.SECOND + " 사업은 연 3회 이상 연다."
        result = self.run_pipeline([(self.FIRST, [self.INTRO, self.ITEM1]), (second, [self.INTRO])])
        self.assertEqual(result["draft_status"], "released_with_check_issues")
        self.assertEqual(result["citation_repairs"], [])
        self.assertIn(self.B2, result["code_checks"]["issues"])
        self.assertIn("answer_contains_unsupported_number", result["code_checks"]["issues"])
        self.assertEqual(result["generated_claims"][1]["citation_ids"], [self.INTRO])

    def test_partly_cited_items_change_nothing(self):
        result = self.run_pipeline([(self.FIRST, [self.INTRO, self.ITEM1]), (self.BOTH, [self.INTRO])])
        self.assertEqual(result["draft_status"], "released_with_check_issues")
        self.assertEqual(result["code_checks"]["issues"], [self.B2])
        self.assertEqual(result["citation_repairs"], [])
        self.assertEqual(result["generated_claims"][1]["citation_ids"], [self.INTRO])

    def test_silent_guard_records_no_repair(self):
        result = self.run_pipeline([(self.FIRST, [self.INTRO, self.ITEM1]), (self.SECOND, [self.INTRO, self.ITEM1])])
        self.assertEqual(result["draft_status"], "code_checked_draft", result["code_checks"])
        self.assertEqual(result["citation_repairs"], [])
        self.assertEqual(result["generated_claims"][1]["citation_ids"], [self.INTRO, self.ITEM1])

    def test_triggers_name_the_firing_items_once_per_intro(self):
        from local_rag_answer import _claim_guard_issues, _intro_only_item_triggers
        rows = [{"unit_id": "p::intro", "quote": "시장은 다음 각 호의 사업을 한다."},
                {"unit_id": "p::item::1", "quote": "1. 진로 교육 프로그램 운영"},
                {"unit_id": "p::item::2", "quote": "2. 정책 홍보 캠페인 추진"}]
        known = {row["unit_id"]: {"effective_date": "2025-07-14", "as_of_date": "2026-10-01", **row} for row in rows}
        claim = {"text": self.BOTH, "citation_ids": ["p::intro"]}
        self.assertEqual(_intro_only_item_triggers(claim, known), [("p::intro", ["p::item::1", "p::item::2"])])
        self.assertEqual(_claim_guard_issues([claim], known, "요건은?"), [self.B2])
        self.assertEqual(_intro_only_item_triggers({"text": self.BOTH, "citation_ids": ["p::intro", "p::item::1"]}, known), [])


class AnswerPipelineTest(unittest.TestCase):


























    def test_paragraph_segments_preserve_every_separator_and_do_not_split_dates(self):
        first = "확인한 범위입니다. 기준일은 2015. 2. 10.입니다.\r\n \r\n"
        second = "조례의 정의와 단서를 함께 설명합니다.\n\n\n"
        third = "별도 기준은 외부 자료 확인 필요 상태입니다."
        answer = first + second + third
        segments = _immutable_claim_segments(answer)
        self.assertEqual(list(segments.values()), [first, second, third])
        self.assertEqual("".join(segments.values()), answer)
        self.assertIn("2015. 2. 10.", segments["claim_1"])
        self.assertEqual(_immutable_claim_segments("앞 문장. 뒤 문장."), {
            "claim_1": "앞 문장. ", "claim_2": "뒤 문장."})










    def test_complete_calendar_dates_normalize_without_decimal_or_invalid_date_reinterpretation(self):
        self.assertEqual(_normalize_calendar_dates("2015.2.10."), "2015-02-10")
        self.assertEqual(_normalize_calendar_dates("2015. 2. 10."), "2015-02-10")
        self.assertEqual(_numbers("2015.2.10."), _numbers("2015. 2. 10."))
        self.assertEqual(_normalize_calendar_dates("2024.2.29."), "2024-02-29")
        for text in ("2015.2.30.", "2023.2.29.", "2015.2", "0.7", "2015.2.10.5"):
            self.assertEqual(_normalize_calendar_dates(text), text)
        self.assertNotIn("2015", _numbers("2015.2.30."))
        self.assertEqual(_numbers("0.7"), {"0.7"})






















    def test_supplement_date_meaning_preserves_historical_promulgation(self):
        data = retrieval()
        item = data["evidence"][0]
        item["kind"] = "supplement"
        record = item["source"]["source_record"]
        record.update(source_text="부칙 <제5812호,2015.1.2.> 이 조례는 공포한 날부터 시행한다.",
                      supplement_promulgation_date="2015-01-02", selected_version_effective_date="2025-07-14")
        model, units, _ = prepare_evidence(data)
        self.assertEqual(units[CID]["effective_date_meaning"], "selected_ordinance_version")
        self.assertEqual(units[CID]["locator"]["supplement_promulgation_date"], "2015-01-02")
        self.assertEqual(model["primary_units"][0]["source"]["effective_date"], "2025-07-14")


class GenerationProfileHookTests(unittest.TestCase):
    """The experiment hook is off by default and changes only prompt suffix and reasoning."""
    PRODUCT_PROMPT_SHA256 = "f30a978063e1be98a2c3068bba6dbb3432531baaec4bf428c6cf63cfb14a07db"
    # Installed generation A/B prompt delta (experiments/generation-ab/prompt_delta.md);
    # the product system prompt ends with one blank line followed by these 8 lines.
    PROMPT_DELTA_LINES = (
        "호의 내용을 서술하는 claim은 그 호의 C별칭 또는 H의 해당 item 선택을 직접 인용한다.",
        "항의 도입부·머리문언 C만 인용해 호의 내용을 증명하지 않는다. 결론이 '시장이 정한다'·'따로",
        "정한다' 같은 위임 문언에 기대면 그 위임을 둔 항·호의 근거도 같은 claim에 인용한다.",
        "질문에 구체적 사례 값(시간·금액·기간·나이·면적·수량 등)이 있으면 값마다 결론 문장을 하나씩 쓴다.",
        "그 문장은 해당 값이 요건 충족·미충족·확인 불가 중 무엇인지 명시하고 비교한 원문 기준을 함께 쓴다.",
        "missing_information은 사용자가 알고 답할 개별 사실(제품·장소·시간·신분 등)만 적는다. 요청한 결론에",
        "필요하지만 제공되지 않은 시장 등의 기준·고시·공고·사업 지침·지정 목록·예산·다른 법령 원문은",
        "문서로 확인할 자료이므로 missing_materials에 적고 decision은 external_material_required로 한다.",
    )

    def _payload(self):
        client = FakeClient(generation())
        result = assess_and_answer('청년의 일반 정의는?', retrieval(), client)
        self.assertEqual([kind for kind, _ in client.calls], ['draft', 'provision_check'], result)
        return client.calls[0][1], result

    def test_default_profile_keeps_literal_prompt_and_reasoning(self):
        import hashlib
        import local_rag_answer as module
        self.assertEqual(module.GENERATION_PROFILE, {"prompt_extra": "", "reasoning_effort": "none"})
        payload, result = self._payload()
        system = payload['messages'][0]['content']
        self.assertEqual(system, module.GENERATION_PROMPT + module.GENERATION_EVIDENCE_PROMPT)
        self.assertEqual(hashlib.sha256(system.encode('utf-8')).hexdigest(), self.PRODUCT_PROMPT_SHA256)
        self.assertEqual(payload['reasoning_effort'], 'none')
        self.assertEqual(result['draft_status'], 'code_checked_draft')

    def test_product_prompt_ends_with_installed_delta(self):
        import local_rag_answer as module
        system = module.GENERATION_PROMPT + module.GENERATION_EVIDENCE_PROMPT
        suffix = '\n\n' + '\n'.join(self.PROMPT_DELTA_LINES) + '\n'
        self.assertTrue(system.endswith(suffix))
        self.assertTrue(module.GENERATION_EVIDENCE_PROMPT.endswith(suffix))
        self.assertEqual(module.GENERATION_PROFILE['prompt_extra'], '')

    def test_profile_changes_prompt_suffix_and_reasoning_only(self):
        import local_rag_answer as module
        baseline, _ = self._payload()
        saved = dict(module.GENERATION_PROFILE)
        try:
            module.GENERATION_PROFILE.update(prompt_extra="\n추가 규칙.\n", reasoning_effort="low")
            changed, _ = self._payload()
        finally:
            module.GENERATION_PROFILE.clear()
            module.GENERATION_PROFILE.update(saved)
        self.assertEqual(changed['messages'][0]['content'], baseline['messages'][0]['content'] + "\n추가 규칙.\n")
        self.assertEqual(changed['reasoning_effort'], 'low')
        self.assertEqual(changed['messages'][1], baseline['messages'][1])
        strip = lambda p: {k: v for k, v in p.items() if k not in ('messages', 'reasoning_effort')}
        self.assertEqual(strip(changed), strip(baseline))
        after, _ = self._payload()
        self.assertEqual(after, baseline)


class ProvisionCheckTests(unittest.TestCase):
    """Pre-publication provision check: generation untouched, enum-only withholding."""
    BASE_PROMPT_SHA256 = "f30a978063e1be98a2c3068bba6dbb3432531baaec4bf428c6cf63cfb14a07db"
    BASE_SCHEMA_SHA256 = "7e7db217e71e19ba1804e5154faf89ef9137f828a5dbe5f9d72e4a95c90a08f7"
    BASE_SCHEMA_H_SHA256 = "f47d2498cd2d518fb5974306cd6a9597b0a00fb47890c0041301a3cea2617d3e"
    BASE_PAYLOAD_SHA256 = "eeeef0b6c902a15b2d2eb2d70ab40d65da2df709d9b3298b3c68912f1be30c91"
    BASE_DRAFT_WIRE_SHA256 = "ab6fcd572f658e376c3b50626e19ea28f3c362616905c16869604f7f8b740253"
    QUESTION = "청년의 나이 기준은?"

    def run_case(self, *provision, response=None, data=None, question=None):
        client = FakeClient(response if response is not None else generation(), provision=list(provision))
        result = assess_and_answer(question or self.QUESTION, data if data is not None else retrieval(), client)
        return client, result

    def check(self, provision_id, basis, asked="청년 나이 기준"):
        return {"asked_matter": asked, "provision_id": provision_id, "basis": basis}

    def test_generation_prompt_schema_and_payload_match_base_commit_bytes(self):
        import hashlib
        import local_rag_answer as module
        from local_rag_client import request_wire
        digest = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual(digest(module.GENERATION_PROMPT + module.GENERATION_EVIDENCE_PROMPT), self.BASE_PROMPT_SHA256)
        self.assertEqual(digest(module.packed(module._generation_schema(["C001", "C002"], ()))), self.BASE_SCHEMA_SHA256)
        self.assertEqual(digest(module.packed(module._generation_schema(
            ["C001"], [{"context_id": "H001", "paragraphs": ["", "1", "2"]}]))), self.BASE_SCHEMA_H_SHA256)
        client, result = self.run_case()
        payload = client.calls[0][1]
        self.assertEqual(client.calls[0][0], "draft")
        self.assertEqual(digest(module.packed(payload)), self.BASE_PAYLOAD_SHA256)
        wire, wire_format = request_wire("draft", payload)
        self.assertEqual((hashlib.sha256(wire).hexdigest(), wire_format), (self.BASE_DRAFT_WIRE_SHA256, "ordered_json_v1"))
        self.assertEqual(result["audit"][0]["stage"], "draft")
        self.assertEqual(result["max_chat_calls_per_retrieval"], 2)

    def test_check_runs_once_after_passed_draft_with_minimal_input(self):
        import hashlib
        import local_rag_answer as module
        from local_rag_client import request_wire, ENDPOINTS
        client, result = self.run_case(self.check("C001", "states_content"))
        self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
        self.assertEqual(ENDPOINTS["provision_check"], ENDPOINTS["draft"])
        payload = client.calls[1][1]
        self.assertEqual((payload["model"], payload["temperature"], payload["reasoning_effort"], payload["max_tokens"],
                          payload["stream"]), ("solar-pro4", 0, "low", 8000, False))
        self.assertEqual(payload["messages"][0], {"role": "system", "content": module.PROVISION_CHECK_PROMPT})
        fmt = payload["response_format"]
        self.assertEqual((fmt["type"], fmt["json_schema"]["name"], fmt["json_schema"]["strict"]),
                         ("json_schema", "local_rag_provision_check", True))
        schema = fmt["json_schema"]["schema"]
        self.assertEqual(list(schema["properties"]), ["asked_matter", "provision_id", "basis"])
        self.assertEqual(schema["required"], ["asked_matter", "provision_id", "basis"])
        self.assertEqual(schema["properties"]["provision_id"]["enum"], ["NONE", "C001"])
        self.assertEqual(schema["properties"]["basis"]["enum"], list(module.PROVISION_CHECK_BASES))
        content = payload["messages"][1]["content"]
        data = json.loads(content)
        self.assertEqual(data, {"question": self.QUESTION, "answer_claims": [ANSWER], "citations": [
            {"id": "C001", "locator": "서울특별시 청년 기본 조례 제3조 제1호", "quote": QUOTE}]})
        for forbidden in ("completeness_context", "external_tasks", "decision", "missing_", "context_sources"):
            self.assertNotIn(forbidden, content)
        receipt = result["audit"][1]
        self.assertEqual(receipt["stage"], "provision_check")
        self.assertEqual(receipt["request_sha256"], hashlib.sha256(module.packed(payload).encode()).hexdigest())
        self.assertEqual(receipt["parsed_response"], self.check("C001", "states_content"))
        self.assertEqual(request_wire("provision_check", payload)[1], "ordered_json_v1")
        self.assertEqual(result["provision_check"], self.check("C001", "states_content"))
        self.assertEqual(result["draft_status"], "code_checked_draft")

    def test_prompt_adds_delegation_guidance_and_keeps_existing_sentences(self):
        import local_rag_answer as module
        prompt = module.PROVISION_CHECK_PROMPT
        added = (
            "인용이 그 사항을 「법령」·다른 조례·규칙·고시·공고에 따르게 하거나(…에 따른, …에서 정하는, …을 준용) "
            "시장 등이 따로 정하게 하면, 그 넘겨받는 문서의 내용이 제공되지 않았더라도 delegates_elsewhere이며 "
            "provision_id는 그 인용이다. 이것은 no_provision이 아니다.",
            "- no_provision: 질문이 묻는 사항을 다루거나 다른 문서에 넘기는 인용이 하나도 없다(provision_id는 NONE).",
            "먼저 넘기는 인용이 있는지 확인하고, 직접 정하거나 넘기는 인용이 모두 없을 때만 NONE과 no_provision을 고른다.",
        )
        for sentence in added:
            self.assertEqual(prompt.count(sentence), 1, sentence)
        self.assertNotIn("- no_provision: 그 사항을 다루는 인용이 없다(provision_id는 NONE).", prompt)
        kept = (
            "서울특별시 본청 조례 6종의 제공 인용과 질문, 공개 예정 답변 문장을 받는다. 답변을 고치지 않고 아래만 판정한다.",
            "질문이 실제로 묻는 사항(제도·금액·기한·절차·제재·자격·해당 여부)을 asked_matter에 한 구절로 적는다.",
            "그 사항 자체를 다루는 인용 단위가 있으면 그 id를 provision_id로 고른다. 인접 조문, 비슷한 다른 제도,\n"
            "일반 원칙·목적 조문만 있으면 NONE이다.",
            "basis는 고른 인용 기준이다.",
            "- states_content: 인용이 그 사항의 내용·요건·효과를 직접 정한다. 목록·요건에 비추어 해당하지 않는다고\n"
            "  원문으로 답할 수 있는 경우도 여기에 속한다.",
            "- delegates_elsewhere: 인용이 그 사항을 다루면서 구체 내용을 다른 법령·조례·규칙·고시·공고·따로 정하는\n"
            "  기준에 넘긴다.",
            "답변 문장이 \"규정이 없다\"고 써도 그 사항을 다루는 인용이 있으면 no_provision이 아니다.",
            "JSON 스키마만 출력한다.",
        )
        for sentence in kept:
            self.assertEqual(prompt.count(sentence), 1, sentence)
        # Order: delegation guidance follows the delegates_elsewhere definition;
        # the decision-order line sits just before the output instruction.
        self.assertLess(prompt.index(kept[5]), prompt.index(added[0]))
        self.assertLess(prompt.index(added[0]), prompt.index(added[1]))
        self.assertTrue(prompt.endswith(added[2] + "\nJSON 스키마만 출력한다.\n"))

    def test_contradictory_enum_pairs_publish_without_withholding(self):
        _, baseline = self.run_case()
        for provision_id, basis in (("NONE", "delegates_elsewhere"), ("C001", "no_provision")):
            with self.subTest(provision_id=provision_id, basis=basis):
                client, result = self.run_case(self.check(provision_id, basis))
                self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
                self.assertEqual((result["decision"], result["draft_status"]), ("supported", "code_checked_draft"))
                self.assertEqual(result["response"], baseline["response"])
                self.assertEqual(result["provision_check"], self.check(provision_id, basis))
                self.assertNotIn("decision_adjustment", result)
                self.assertEqual(client.calls[1][1]["max_tokens"], 8000)

    def test_H_selected_units_without_alias_get_distinct_ids(self):
        text = "65세 이상인 사람의 입장료를 면제할 수 있습니다."
        response = generation(text, ids=[], claims=[{"text": text, "citation_ids": [],
            "context_selections": [{"context_id": "H001", "paragraph": "1", "item": "2"}]}])
        client, result = self.run_case(response=response, data=selector_retrieval(), question="감면 기준은?")
        self.assertEqual(result["draft_status"], "code_checked_draft", result)
        data = json.loads(client.calls[1][1]["messages"][1]["content"])
        self.assertEqual([(row["id"], row["locator"]) for row in data["citations"]],
                         [("S001", "서울특별시 청년 기본 조례 제3조 제1항"),
                          ("S002", "서울특별시 청년 기본 조례 제3조 제1항 제2호")])
        self.assertTrue(set(row["id"] for row in data["citations"]).isdisjoint(result["citation_aliases"]))

    def test_none_with_no_provision_withholds_and_keeps_draft_for_audit(self):
        client, result = self.run_case(self.check("NONE", "no_provision"))
        self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
        self.assertEqual(result["decision"], "insufficient_evidence")
        self.assertEqual(result["draft_status"], "withheld_no_provision")
        self.assertEqual(result["response"], {"answer": "확인 불가", "citation": [], "effective_date": []})
        self.assertEqual(result["decision_adjustment"], {"from": "supported", "to": "insufficient_evidence",
                                                         "reason": "provision_check_no_provision"})
        self.assertEqual(result["reason"], "질문이 묻는 사항을 다루는 규정을 제공 원문에서 확인하지 못했습니다.")
        self.assertEqual(result["provision_check"], self.check("NONE", "no_provision"))
        self.assertEqual(result["draft_response"]["answer"], ANSWER)
        self.assertEqual(result["generated_claims"], [{"text": ANSWER, "citation_ids": [CID]}])
        self.assertEqual(result["code_checks"]["status"], "passed")
        self.assertTrue(result["draft_generated"])
        self.assertFalse(result["answer_allowed"])

    def test_other_combinations_publish_unchanged(self):
        _, baseline = self.run_case()
        for provision_id, basis in (("NONE", "states_content"), ("NONE", "delegates_elsewhere"),
                                    ("C001", "no_provision"), ("C001", "delegates_elsewhere"),
                                    ("C001", "states_content")):
            with self.subTest(provision_id=provision_id, basis=basis):
                client, result = self.run_case(self.check(provision_id, basis))
                self.assertEqual(len(client.calls), 2)
                self.assertEqual((result["decision"], result["draft_status"]), ("supported", "code_checked_draft"))
                self.assertEqual(result["response"], baseline["response"])
                self.assertEqual(result["provision_check"], self.check(provision_id, basis))
                self.assertNotIn("decision_adjustment", result)

    def test_request_or_format_failure_is_fail_open(self):
        _, baseline = self.run_case()
        failures = {
            "exception": RuntimeError("request_outcome_unknown_no_automatic_retry"),
            "not_json": "not json",
            "unknown_id": {"asked_matter": "x", "provision_id": "C999", "basis": "no_provision"},
            "unknown_basis": {"asked_matter": "x", "provision_id": "NONE", "basis": "absent"},
            "missing_field": {"provision_id": "NONE", "basis": "no_provision"},
            "output_limit": {"model": "solar-pro4", "choices": [{"finish_reason": "length",
                             "message": {"content": "{}"}}]},
        }
        for name, failure in failures.items():
            with self.subTest(name):
                client, result = self.run_case(failure)
                self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
                self.assertEqual((result["decision"], result["draft_status"]), ("supported", "code_checked_draft"))
                self.assertEqual(result["response"], baseline["response"])
                self.assertEqual(result["provision_check"], {"status": "failed"})
                receipt = result["audit"][-1]
                self.assertEqual(receipt["stage"], "provision_check")
                self.assertTrue(receipt.get("error_type") or receipt.get("validation_error"), receipt)
                self.assertNotIn("parsed_response", receipt)

    def test_no_check_on_held_or_nonanswer_paths(self):
        cases = {
            "insufficient": generation(decision="insufficient_evidence", claims=[]),
            "out_of_scope": generation(decision="out_of_scope", claims=[]),
            "clarification_without_claims": generation(decision="needs_clarification", claims=[],
                                                       missing_information=["나이"]),
            # A non-releasable issue (here mixed with a releasable one) still holds the draft unchecked.
            "code_check_failed": generation("질문에 따라 청년은 20세 이상 45세 이하입니다."),
        }
        for name, response in cases.items():
            with self.subTest(name):
                client, result = self.run_case(self.check("NONE", "no_provision"), response=response)
                self.assertEqual([kind for kind, _ in client.calls], ["draft"])
                self.assertNotIn("provision_check", result)
                self.assertNotEqual(result["draft_status"], "withheld_no_provision")
                self.assertEqual(len(client.provision), 1)

    RELEASABLE_TEXT = "청년은 20세 이상 45세 이하입니다."

    def passed_twin(self, result):
        """The same audit as if its code checks had passed (the existing published shape)."""
        twin = copy.deepcopy(result)
        twin["draft_status"] = "code_checked_draft"
        twin["code_checks"].update(status="passed", issues=[])
        return twin

    def test_releasable_issue_alone_is_provision_checked_and_released(self):
        from local_rag_answer import RELEASABLE_CHECK_ISSUES
        client, result = self.run_case(response=generation(self.RELEASABLE_TEXT))
        self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
        self.assertEqual((result["decision"], result["draft_status"]), ("supported", "released_with_check_issues"))
        self.assertEqual(result["code_checks"]["status"], "failed")
        self.assertEqual(result["code_checks"]["issues"], ["answer_contains_unsupported_number"])
        self.assertEqual(result["code_checks"]["unsupported_numbers"], ["20", "45"])
        self.assertLessEqual(set(result["code_checks"]["issues"]), RELEASABLE_CHECK_ISSUES)
        self.assertEqual(result["provision_check"]["provision_id"], "C001")
        self.assertEqual(result["response"], result["draft_response"])
        self.assertNotIn("decision_adjustment", result)
        self.assertFalse(result["answer_allowed"])
        self.assertFalse(result["final_answer_generated"])
        public = public_result({"status": "completed", "result": result}, catalog={})
        twin = public_result({"status": "completed", "result": self.passed_twin(result)}, catalog={})
        # Everything but the message matches the passed twin (response fields, sources, status, the rest).
        without_message = lambda output: {key: value for key, value in output.items() if key != "message"}
        self.assertEqual(json.dumps(without_message(public), ensure_ascii=False),
                         json.dumps(without_message(twin), ensure_ascii=False))
        self.assertEqual(public["response"], twin["response"])
        self.assertEqual(public["sources"], twin["sources"])
        self.assertEqual(public["status"], "code_checked_draft")
        self.assertEqual(public["response"]["answer"], self.RELEASABLE_TEXT)
        # The released message states the failed checks; it never claims the numeric check passed.
        self.assertNotIn("수치 검사 완료", public["message"])
        self.assertEqual(public["message"],
                         "출처·형식 검사 초안 · 수치·인용 범위 검사 일부 미통과 · 의미 검증 미실행 · 최종 답변 승인 전")
        self.assertIn("수치 검사 완료", twin["message"])

    def test_released_external_material_draft_keeps_scoped_public_invariants(self):
        response = generation(self.RELEASABLE_TEXT, decision="external_material_required",
                              missing_materials=["별도 사업 규칙"], missing_information=["실제 신청일"])
        client, result = self.run_case(self.check("C001", "states_content"), response=response)
        self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
        self.assertEqual((result["decision"], result["draft_status"]),
                         ("external_material_required", "released_with_check_issues"))
        self.assertEqual(result["code_checks"]["issues"], ["answer_contains_unsupported_number"])
        self.assertFalse(result["fully_resolved"])
        public = public_result({"status": "completed", "result": result}, catalog={})
        twin = public_result({"status": "completed", "result": self.passed_twin(result)}, catalog={})
        self.assertEqual(public["status"], "code_checked_draft")
        self.assertEqual(public["response"]["answer"], ORDINANCE_SCOPE_NOTICE + "\n\n" + self.RELEASABLE_TEXT)
        self.assertEqual(public["missing_materials"], ["별도 사업 규칙"])
        self.assertEqual(public["missing_information"], ["실제 신청일"])
        self.assertEqual(public["external_material_status"], "외부 자료 확인 필요")
        self.assertEqual(public["scope_notice"], result["scope_notice"])
        self.assertFalse(public["fully_resolved"])
        self.assertNotIn("수치 검사 완료", public["message"])
        self.assertEqual({k: v for k, v in public.items() if k != "message"},
                         {k: v for k, v in twin.items() if k != "message"})
        for tamper in ("scope_notice", "fully_resolved"):
            with self.subTest(tamper=tamper):
                bad = copy.deepcopy(result)
                if tamper == "scope_notice":
                    bad[tamper]["text"] = "확인 완료"
                else:
                    bad[tamper] = True
                with self.assertRaises(ValueError):
                    public_result({"status": "completed", "result": bad}, catalog={})

    def test_released_public_result_rejects_nonreleasable_or_empty_issues(self):
        _, result = self.run_case(response=generation(self.RELEASABLE_TEXT))
        for issues in ([], ["claim_without_owned_citations"],
                       ["answer_contains_unsupported_number", "numbered_presentation_list"]):
            with self.subTest(issues=issues):
                tampered = copy.deepcopy(result)
                tampered["code_checks"]["issues"] = issues
                with self.assertRaises(ValueError):
                    public_result({"status": "completed", "result": tampered}, catalog={})
        tampered = copy.deepcopy(result)
        tampered["code_checks"]["status"] = "passed"
        with self.assertRaises(ValueError):
            public_result({"status": "completed", "result": tampered}, catalog={})

    def test_releasable_issue_mixed_with_other_issue_stays_held(self):
        for response, other in ((generation("질문에 따라 " + self.RELEASABLE_TEXT), "self_referential_commentary"),
                                (generation(self.RELEASABLE_TEXT, ids=[]), "claim_without_owned_citations")):
            with self.subTest(other=other):
                client, result = self.run_case(self.check("NONE", "no_provision"), response=response)
                self.assertEqual([kind for kind, _ in client.calls], ["draft"])
                self.assertEqual(result["draft_status"], "held")
                self.assertIn(other, result["code_checks"]["issues"])
                self.assertEqual(result["code_checks"]["status"], "failed")
                self.assertEqual(result["response"], {"answer": "확인 불가", "citation": [], "effective_date": []})
                self.assertNotIn("provision_check", result)
        client, result = self.run_case(self.check("NONE", "no_provision"),
                                       response=generation("질문에 따라 " + self.RELEASABLE_TEXT))
        self.assertEqual(result["code_checks"]["issues"],
                         ["answer_contains_unsupported_number", "self_referential_commentary"])
        self.assertEqual(result["response"], {"answer": "확인 불가", "citation": [], "effective_date": []})
        self.assertNotIn("provision_check", result)
        public = public_result({"status": "completed", "result": result}, catalog={})
        self.assertEqual(public["status"], "code_check_failed")
        self.assertNotIn("response", public)

    def test_releasable_issue_withheld_by_no_provision(self):
        from evaluate_local_rag import _outcome_draft_status, _withheld_no_provision_valid, SINGLE_RESPONSE_CONTRACT
        client, result = self.run_case(self.check("NONE", "no_provision"), response=generation(self.RELEASABLE_TEXT))
        self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
        self.assertEqual((result["decision"], result["draft_status"]), ("insufficient_evidence", "withheld_no_provision"))
        self.assertEqual(result["decision_adjustment"], {"from": "supported", "to": "insufficient_evidence",
                                                         "reason": "provision_check_no_provision"})
        self.assertEqual(result["code_checks"]["status"], "failed")
        self.assertEqual(result["code_checks"]["issues"], ["answer_contains_unsupported_number"])
        self.assertEqual(result["response"], {"answer": "확인 불가", "citation": [], "effective_date": []})
        self.assertTrue(_withheld_no_provision_valid(result))
        self.assertEqual(_outcome_draft_status(result, SINGLE_RESPONSE_CONTRACT), "not_generated")
        public = public_result({"status": "completed", "result": result}, catalog={})
        self.assertEqual(public["status"], "code_check_failed")
        self.assertNotIn("response", public)

    def test_releasable_issue_released_when_provision_check_fails(self):
        for failure in (RuntimeError("request_outcome_unknown_no_automatic_retry"),
                        {"asked_matter": "x", "provision_id": "C999", "basis": "no_provision"}):
            with self.subTest(failure=repr(failure)):
                client, result = self.run_case(failure, response=generation(self.RELEASABLE_TEXT))
                self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
                self.assertEqual((result["decision"], result["draft_status"]),
                                 ("supported", "released_with_check_issues"))
                self.assertEqual(result["provision_check"], {"status": "failed"})
                self.assertEqual(result["code_checks"]["issues"], ["answer_contains_unsupported_number"])
                self.assertEqual(result["response"], result["draft_response"])
                public = public_result({"status": "completed", "result": result}, catalog={})
                self.assertEqual(public["response"]["answer"], self.RELEASABLE_TEXT)

    def test_public_result_for_withheld_and_published_drafts(self):
        from local_rag_service import sync_view
        _, withheld = self.run_case(self.check("NONE", "no_provision"))
        public = public_result({"status": "completed", "result": withheld}, catalog={})
        self.assertEqual(public["status"], "insufficient_evidence")
        self.assertEqual(public["response"], {"answer": "확인 불가", "citation": []})
        view = sync_view({"status": "completed", "job_id": "a" * 64, "result": public})
        self.assertEqual((view["answer"], view["citation"], view["status"]), ("확인 불가", [], "completed"))
        _, published = self.run_case(self.check("NONE", "delegates_elsewhere"))
        shown = public_result({"status": "completed", "result": published}, catalog={})
        self.assertEqual(shown["status"], "code_checked_draft")
        self.assertEqual(shown["response"]["answer"], ANSWER)
        self.assertEqual(shown["response"], public_result({"status": "completed", "result": self.run_case()[1]},
                                                         catalog={})["response"])


class ProvisionCheckDeadlineTests(unittest.TestCase):
    """The provision check waits only until 75 s after the question started; failure stays fail-open."""
    QUESTION = "청년의 나이 기준은?"

    def run_case(self, *provision, started=None):
        client = FakeClient(generation(), provision=list(provision))
        result = assess_and_answer(self.QUESTION, retrieval(), client, started=started)
        return client, result

    def check(self, provision_id, basis):
        return {"asked_matter": "청년 나이 기준", "provision_id": provision_id, "basis": basis}

    def test_enough_time_sends_the_same_request_with_bounded_wait(self):
        import time
        import local_rag_answer as module
        base_client, baseline = self.run_case(self.check("NONE", "no_provision"))
        before = time.monotonic()
        client, result = self.run_case(self.check("NONE", "no_provision"), started=before - 30)
        self.assertEqual(client.calls, base_client.calls)
        self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
        self.assertEqual(result["audit"][-1]["request_sha256"], baseline["audit"][-1]["request_sha256"])
        self.assertEqual({k: v for k, v in result.items() if k != "audit"},
                         {k: v for k, v in baseline.items() if k != "audit"})
        self.assertEqual(result["decision_adjustment"]["reason"], "provision_check_no_provision")
        self.assertNotIn("deadline", result["audit"][-1])
        # Only the provision check gets a wait bound; it is the time left until 75 s.
        self.assertEqual(client.timeouts[0], ("draft", None))
        kind, timeout = client.timeouts[1]
        self.assertEqual(kind, "provision_check")
        self.assertLessEqual(timeout, module.PROVISION_CHECK_DEADLINE_SECONDS - 30)
        self.assertGreater(timeout, module.PROVISION_CHECK_DEADLINE_SECONDS - 30 - 5)
        self.assertLessEqual(base_client.timeouts[1][1], module.PROVISION_CHECK_DEADLINE_SECONDS)

    def test_too_little_time_skips_the_call_and_publishes_with_deadline_mark(self):
        import time
        import local_rag_answer as module
        _, baseline = self.run_case()
        for elapsed in (module.PROVISION_CHECK_DEADLINE_SECONDS - module.PROVISION_CHECK_MIN_SECONDS + 1, 500.0):
            with self.subTest(elapsed=elapsed):
                # Even a check that would answer no_provision is never asked.
                client, result = self.run_case(self.check("NONE", "no_provision"), started=time.monotonic() - elapsed)
                self.assertEqual([kind for kind, _ in client.calls], ["draft"])
                self.assertEqual(client.provision, [self.check("NONE", "no_provision")])
                self.assertEqual((result["decision"], result["draft_status"]), ("supported", "code_checked_draft"))
                self.assertEqual(result["response"], baseline["response"])
                self.assertNotIn("decision_adjustment", result)
                self.assertEqual(result["provision_check"], {"status": "failed", "reason": "deadline"})
                receipt = result["audit"][-1]
                self.assertEqual(receipt["stage"], "provision_check")
                self.assertEqual(receipt["request_sha256"], baseline["audit"][-1]["request_sha256"])
                self.assertEqual(receipt["error_type"], "deadline_skipped")
                self.assertEqual(receipt["deadline"]["outcome"], "skipped_not_sent")
                self.assertLess(receipt["deadline"]["remaining_seconds"], module.PROVISION_CHECK_MIN_SECONDS)
                self.assertGreaterEqual(receipt["deadline"]["remaining_seconds"], 0)
                self.assertNotIn("meta", receipt)
                public = public_result({"status": "completed", "result": result}, catalog={})
                self.assertEqual(public["response"]["answer"], ANSWER)

    def test_transport_timeout_is_a_fail_open_deadline_failure(self):
        _, baseline = self.run_case()
        error = RuntimeError("request_outcome_unknown_no_automatic_retry")
        error.transport_error_type = "ReadTimeout"
        client, result = self.run_case(error)
        self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
        self.assertEqual((result["decision"], result["draft_status"]), ("supported", "code_checked_draft"))
        self.assertEqual(result["response"], baseline["response"])
        self.assertEqual(result["provision_check"], {"status": "failed", "reason": "deadline"})
        receipt = result["audit"][-1]
        self.assertEqual(receipt["error_type"], "RuntimeError")
        self.assertEqual((receipt["deadline"]["outcome"], receipt["deadline"]["transport_error_type"]),
                         ("timeout", "ReadTimeout"))
        self.assertNotIn("parsed_response", receipt)

    def test_other_request_failures_keep_the_plain_failed_mark(self):
        for error in (RuntimeError("request_outcome_unknown_no_automatic_retry"), ValueError("uncertain_attempt")):
            with self.subTest(error=type(error).__name__):
                if isinstance(error, RuntimeError):
                    error.transport_error_type = "ConnectionError"
                _, result = self.run_case(error)
                self.assertEqual(result["provision_check"], {"status": "failed"})
                self.assertNotIn("deadline", result["audit"][-1])
                self.assertEqual(result["draft_status"], "code_checked_draft")

    def test_deadline_leaves_room_inside_the_service_sync_wait(self):
        import local_rag_answer as module
        import local_rag_service
        self.assertLessEqual(module.PROVISION_CHECK_DEADLINE_SECONDS + 5, local_rag_service.SYNC_WAIT_SECONDS)
        self.assertGreaterEqual(module.PROVISION_CHECK_MIN_SECONDS, 5)

    def test_prior_unknown_outcome_is_marked_without_storing_the_message(self):
        _, result = self.run_case(ValueError("uncertain_attempt_no_automatic_retry"))
        self.assertEqual(result["provision_check"], {"status": "failed", "reason": "prior_unknown_outcome"})
        self.assertEqual(result["draft_status"], "code_checked_draft")
        receipt = result["audit"][-1]
        self.assertEqual((receipt["stage"], receipt["error_type"], receipt["prior_attempt"]),
                         ("provision_check", "ValueError", "unknown_outcome"))
        self.assertNotIn("deadline", receipt)
        self.assertNotIn("uncertain_attempt_no_automatic_retry", json.dumps(result, ensure_ascii=False))
        # Any other message stays the plain failure mark.
        _, other = self.run_case(ValueError("uncertain_attempt"))
        self.assertEqual(other["provision_check"], {"status": "failed"})
        self.assertNotIn("prior_attempt", other["audit"][-1])


    def test_rate_limited_check_publishes_fail_open_with_reason(self):
        from local_rag_client import RateLimitedNoResend
        _, baseline = self.run_case()
        client, result = self.run_case(RateLimitedNoResend(58.0))
        self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
        self.assertEqual((result["decision"], result["draft_status"]), ("supported", "code_checked_draft"))
        self.assertEqual(result["response"], baseline["response"])
        self.assertNotIn("decision_adjustment", result)
        self.assertEqual(result["provision_check"], {"status": "failed", "reason": "rate_limited"})
        receipt = result["audit"][-1]
        self.assertEqual((receipt["stage"], receipt["error_type"]), ("provision_check", "RateLimitedNoResend"))
        self.assertEqual(receipt["rate_limit"], {"outcome": "rate_limited_no_resend", "http_status": 429,
                                                 "retry_wait_seconds": 58.0})
        self.assertNotIn("deadline", receipt)
        self.assertNotIn("prior_attempt", receipt)
        self.assertEqual(receipt["request_sha256"], baseline["audit"][-1]["request_sha256"])
        public = public_result({"status": "completed", "result": result}, catalog={})
        self.assertEqual(public["response"]["answer"], ANSWER)
        # Only the client's exact text is the rate-limit mark.
        _, other = self.run_case(RuntimeError("rate_limited"))
        self.assertEqual(other["provision_check"], {"status": "failed"})
        self.assertNotIn("rate_limit", other["audit"][-1])

    def test_server_error_check_publishes_the_draft_with_error_type_only(self):
        import time
        from local_rag_client import ProviderServerError
        _, baseline = self.run_case()
        client, result = self.run_case(ProviderServerError(503), started=time.monotonic())
        self.assertEqual([kind for kind, _ in client.calls], ["draft", "provision_check"])
        self.assertEqual((result["decision"], result["draft_status"]), ("supported", "code_checked_draft"))
        self.assertEqual(result["response"], baseline["response"])
        self.assertNotIn("decision_adjustment", result)
        self.assertEqual(result["provision_check"], {"status": "failed"})
        receipt = result["audit"][-1]
        self.assertEqual((receipt["stage"], receipt["error_type"]), ("provision_check", "ProviderServerError"))
        for key in ("deadline", "rate_limit", "prior_attempt"):
            self.assertNotIn(key, receipt)
        self.assertEqual(receipt["request_sha256"], baseline["audit"][-1]["request_sha256"])
        public = public_result({"status": "completed", "result": result}, catalog={})
        self.assertEqual(public["response"]["answer"], ANSWER)

    def test_real_client_429_under_deadline_never_sleeps_and_answer_stays_published(self):
        import tempfile
        import time
        from local_rag_client import Client
        sleeps, sent = [], []

        class Rejected:
            status_code, content, headers = 429, b"{}", {"Retry-After": "300"}

            def close(self):
                pass

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="client-test-") as temp:
            real = Client(temp, transport=lambda *a, **k: sent.append(k["timeout"]) or Rejected(),
                          sleeper=sleeps.append, clock=lambda: 1000.0)
            real._key = "fake-unit-test-secret"
            fake = FakeClient(generation())

            class Split:
                def request(self, kind, payload, timeout=None):
                    if kind == "provision_check":
                        return real.request(kind, payload, timeout=timeout)
                    return fake.request(kind, payload, timeout=timeout)

            _, baseline = self.run_case()
            result = assess_and_answer(self.QUESTION, retrieval(), Split(), started=time.monotonic())
        self.assertEqual((sleeps, len(sent), real.called), ([], 1, 1))
        self.assertEqual(result["provision_check"], {"status": "failed", "reason": "rate_limited"})
        self.assertEqual(result["audit"][-1]["rate_limit"]["retry_wait_seconds"], 300.0)
        self.assertEqual((result["draft_status"], result["response"]), ("code_checked_draft", baseline["response"]))


class DraftTransientRetryTests(unittest.TestCase):
    """A draft whose first attempt got a conclusive provider 5xx is resent once, inside the 75 s budget."""
    QUESTION = "청년의 나이 기준은?"

    def run_case(self, *responses, started=None):
        client = FakeClient(*responses)
        result = assess_and_answer(self.QUESTION, retrieval(), client, started=started)
        return client, result

    def server_error(self, status=500):
        from local_rag_client import ProviderServerError
        return ProviderServerError(status)

    def drafts(self, client):
        return [payload for kind, payload in client.calls if kind == "draft"]

    def test_server_error_then_success_publishes_the_same_answer(self):
        import time
        import local_rag_answer as module
        _, baseline = self.run_case(generation())
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                client, result = self.run_case(self.server_error(status), generation(), started=time.monotonic())
                self.assertEqual([kind for kind, _ in client.calls], ["draft", "draft", "provision_check"])
                first, second = self.drafts(client)
                self.assertEqual(module.packed(first), module.packed(second))
                self.assertEqual((result["decision"], result["draft_status"]), ("supported", "code_checked_draft"))
                self.assertEqual(result["response"], baseline["response"])
                # Only the retry carries a bound: the time left until the 75 s mark.
                self.assertEqual(client.timeouts[0], ("draft", None))
                kind, timeout = client.timeouts[1]
                self.assertEqual(kind, "draft")
                self.assertLessEqual(timeout, module.PROVISION_CHECK_DEADLINE_SECONDS)
                self.assertGreater(timeout, module.PROVISION_CHECK_DEADLINE_SECONDS - 5)
                drafts = [row for row in result["audit"] if row["stage"] == "draft"]
                self.assertEqual(len(drafts), 2)
                self.assertEqual(drafts[0]["request_sha256"], drafts[1]["request_sha256"])
                self.assertEqual(drafts[0]["request_sha256"], baseline["audit"][0]["request_sha256"])
                self.assertEqual(drafts[0]["error_type"], "ProviderServerError")
                self.assertNotIn("response", drafts[0])
                self.assertNotIn("transient_retry", drafts[0])
                self.assertEqual(drafts[1]["transient_retry"], {"http_status": status, "attempt": 2})
                self.assertEqual(drafts[1]["parsed_response"], baseline["audit"][0]["parsed_response"])
                self.assertEqual({k: v for k, v in result.items() if k != "audit"},
                                 {k: v for k, v in baseline.items() if k != "audit"})
                public = public_result({"status": "completed", "result": result}, catalog={})
                self.assertEqual(public, public_result({"status": "completed", "result": baseline}, catalog={}))

    def test_retry_failure_ends_as_draft_request_failed_without_a_third_call(self):
        import time
        from local_rag_client import RateLimitedNoResend
        timeout = RuntimeError("request_outcome_unknown_no_automatic_retry")
        timeout.transport_error_type = "ReadTimeout"
        for error in (ValueError("provider_http_error:503"), ValueError("second_rate_limit_response_stop"),
                      RateLimitedNoResend(60.0), timeout, self.server_error(500)):
            with self.subTest(error=str(error)):
                client, result = self.run_case(self.server_error(500), error, generation(), started=time.monotonic())
                self.assertEqual([kind for kind, _ in client.calls], ["draft", "draft"])
                self.assertEqual((result["decision"], result["error_code"]), ("processing_error", "draft_request_failed"))
                self.assertEqual(result["response"]["answer"], "확인 불가")
                drafts = [row for row in result["audit"] if row["stage"] == "draft"]
                self.assertEqual([row.get("error_type") for row in drafts], ["ProviderServerError", type(error).__name__])
                self.assertEqual(drafts[1]["transient_retry"], {"http_status": 500, "attempt": 2})

    def test_other_draft_failures_are_never_retried(self):
        import time
        unknown = RuntimeError("request_outcome_unknown_no_automatic_retry")
        unknown.transport_error_type = "ReadTimeout"
        for error in (unknown, ValueError("uncertain_attempt_no_automatic_retry"),
                      ValueError("provider_http_error:400"), ValueError("provider_http_error:404"),
                      ValueError("provider_http_error:500"), ValueError("unexpected_response_model"),
                      RuntimeError("provider_server_error"), RuntimeError("read_only_cache_miss:x")):
            with self.subTest(error=str(error)):
                client, result = self.run_case(error, generation(), started=time.monotonic())
                self.assertEqual([kind for kind, _ in client.calls], ["draft"])
                self.assertEqual(result["error_code"], "draft_request_failed")
                self.assertEqual(len(result["audit"]), 1)
                self.assertNotIn("transient_retry", result["audit"][0])
                self.assertNotIn("transient_retry_skipped", result["audit"][0])

    def test_too_little_time_left_skips_the_retry(self):
        import time
        import local_rag_answer as module
        for elapsed in (module.PROVISION_CHECK_DEADLINE_SECONDS - module.DRAFT_RETRY_MIN_SECONDS + 1, 500.0):
            with self.subTest(elapsed=elapsed):
                client, result = self.run_case(self.server_error(502), generation(), started=time.monotonic() - elapsed)
                self.assertEqual([kind for kind, _ in client.calls], ["draft"])
                self.assertEqual(result["error_code"], "draft_request_failed")
                self.assertEqual(len(result["audit"]), 1)
                skipped = result["audit"][0]["transient_retry_skipped"]
                self.assertEqual((skipped["http_status"], skipped["reason"], skipped["minimum_seconds"]),
                                 (502, "deadline", module.DRAFT_RETRY_MIN_SECONDS))
                self.assertLess(skipped["remaining_seconds"], module.DRAFT_RETRY_MIN_SECONDS)
                self.assertGreaterEqual(skipped["remaining_seconds"], 0)

    def test_chat_call_budget_blocks_the_retry(self):
        import local_rag_answer as module
        from unittest.mock import patch
        with patch.object(module, "MAX_CHAT_CALLS", 1):
            client, result = self.run_case(self.server_error(500), generation())
        self.assertEqual([kind for kind, _ in client.calls], ["draft"])
        self.assertEqual(result["error_code"], "draft_request_failed")
        self.assertEqual(result["audit"][0]["transient_retry_skipped"]["reason"], "chat_call_budget")
        # The operating budget (7) leaves room for one retry plus the provision check.
        self.assertGreaterEqual(module.MAX_CHAT_CALLS, 3)

    def test_real_client_500_then_200_resends_identical_bytes_without_sleep(self):
        import tempfile
        import time
        from local_rag_client import Client
        sleeps, sent = [], []
        # The provider body FakeClient would build for generation() (no-H schema: no empty selectors).
        draft = generation()
        for entry in draft["claims"] + draft["calculations"]:
            entry.pop("context_selections")
        body = json.dumps({"model": "solar-pro4", "usage": {"total_tokens": 10}, "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(draft, ensure_ascii=False)}}]},
            ensure_ascii=False).encode()

        class Reply:
            headers = {}

            def __init__(self, status, content):
                self.status_code, self.content = status, content

            def close(self):
                pass

        replies = [Reply(500, b"internal error"), Reply(200, body)]
        _, baseline = self.run_case(generation())
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="client-test-") as temp:
            def transport(*args, **kwargs):
                sent.append((kwargs["data"], kwargs["timeout"]))
                return replies.pop(0)
            real = Client(temp, transport=transport, sleeper=sleeps.append, clock=lambda: 1000.0)
            real._key = "fake-unit-test-secret"
            fake = FakeClient()

            class Split:
                def __init__(self, draft_client):
                    self.draft_client = draft_client

                def request(self, kind, payload, timeout=None):
                    if kind == "draft":
                        return self.draft_client.request(kind, payload, timeout=timeout)
                    return fake.request(kind, payload, timeout=timeout)

            result = assess_and_answer(self.QUESTION, retrieval(), Split(real), started=time.monotonic())
            self.assertEqual((sleeps, len(sent), real.called, replies), ([], 2, 2, []))
            self.assertEqual(sent[0][0], sent[1][0])
            self.assertEqual(sent[0][1], (10, 180))
            self.assertLessEqual(sent[1][1][1], 75.0)
            self.assertEqual((result["draft_status"], result["response"]), ("code_checked_draft", baseline["response"]))
            drafts = [row for row in result["audit"] if row["stage"] == "draft"]
            self.assertEqual(drafts[1]["transient_retry"], {"http_status": 500, "attempt": 2})
            self.assertEqual(Path(drafts[1]["meta"]["response_path"]).name, "attempt-2.response.json")
            # Read-only replay of the completed request sends nothing and gives the same answer.
            replay_client = Client(temp, read_only=True)
            replay = assess_and_answer(self.QUESTION, retrieval(), Split(replay_client), started=time.monotonic())
            self.assertEqual((len(sent), replay_client.called), (2, 0))
            self.assertEqual(replay["response"], result["response"])
            replay_drafts = [row for row in replay["audit"] if row["stage"] == "draft"]
            self.assertEqual(len(replay_drafts), 1)
            self.assertTrue(replay_drafts[0]["meta"]["cached"])

    def test_read_only_replay_of_a_lone_first_server_error_sends_nothing(self):
        import tempfile
        import time
        from local_rag_client import Client

        class Reply:
            status_code, content, headers = 503, b"unavailable", {}

            def close(self):
                pass

        sent = []
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="client-test-") as temp:
            real = Client(temp, transport=lambda *a, **k: sent.append(1) or Reply(), sleeper=self.fail,
                          clock=lambda: 1000.0)
            real._key = "fake-unit-test-secret"
            # Too little time left: the first 503 is kept and no retry is sent.
            first = assess_and_answer(self.QUESTION, retrieval(), real, started=time.monotonic() - 500.0)
            self.assertEqual((len(sent), first["error_code"]), (1, "draft_request_failed"))
            self.assertEqual(first["audit"][0]["transient_retry_skipped"]["http_status"], 503)
            replay_client = Client(temp, read_only=True)
            replay = assess_and_answer(self.QUESTION, retrieval(), replay_client, started=time.monotonic())
        self.assertEqual((len(sent), replay_client.called), (1, 0))
        self.assertEqual(replay["error_code"], "draft_request_failed")
        self.assertEqual([row.get("error_type") for row in replay["audit"]], ["CacheMiss"])


class DuplicateCitationIdMergeTests(unittest.TestCase):
    """Exact repeats in the model's own citation lists are merged once, before alias restoration."""
    QUESTION = "청년의 나이 기준은?"

    def comparable(self, result):
        # The draft receipt carries the raw provider body; everything else must match.
        value = copy.deepcopy(result)
        value.pop("duplicate_citation_ids_merged", None)
        for receipt in value["audit"]:
            if receipt["stage"] == "draft":
                for key in ("response", "parsed_response", "meta"):
                    receipt.pop(key, None)
            receipt.pop("meta", None)
        return value

    def test_repeated_claim_citation_is_merged_and_recorded(self):
        import local_rag_answer as module
        response = generation(ids=["C001", "C001"])
        result = assess_and_answer(self.QUESTION, retrieval(), FakeClient(response))
        self.assertEqual(result["draft_status"], "code_checked_draft", result)
        self.assertEqual(result["duplicate_citation_ids_merged"], [{"kind": "claim", "index": 0, "removed": ["C001"]}])
        self.assertEqual(result["generated_claims"][0]["citation_ids"], [CID])
        self.assertEqual(result["citation_repairs"], [])
        # The receipt keeps the model's original parsed response unchanged.
        self.assertEqual(result["audit"][0]["parsed_response"]["claims"][0]["citation_ids"], ["C001", "C001"])
        self.assertEqual(result["source_selection_trace"]["claims"][0]["citation_aliases"], ["C001"])
        plain = assess_and_answer(self.QUESTION, retrieval(), FakeClient(generation()))
        self.assertEqual(module.packed(self.comparable(result)), module.packed(self.comparable(plain)))
        shown = public_result({"status": "completed", "result": result}, catalog={})
        self.assertEqual(shown, public_result({"status": "completed", "result": plain}, catalog={}))

    def test_repeated_calculation_citation_is_merged(self):
        answer = ANSWER + " 상한과 하한의 차이는 20입니다."
        response = generation(answer, calculations=[{"expression": "39-19", "result": "20",
                                                     "citation_ids": ["C001", "C001"]}])
        result = assess_and_answer(self.QUESTION, retrieval(), FakeClient(response))
        self.assertNotEqual(result["decision"], "processing_error", result)
        self.assertEqual(result["duplicate_citation_ids_merged"],
                         [{"kind": "calculation", "index": 0, "removed": ["C001"]}])
        self.assertEqual(result["generated_calculations"][0]["citation_ids"], [CID])
        self.assertEqual(result["audit"][0]["parsed_response"]["calculations"][0]["citation_ids"], ["C001", "C001"])

    def test_repeated_context_selection_is_merged(self):
        selector = {"context_id": "H001", "paragraph": "1", "item": "2"}
        text = "65세 이상인 사람의 입장료를 면제할 수 있습니다."
        response = generation(text, ids=[], claims=[{"text": text, "citation_ids": [],
                                                     "context_selections": [selector, dict(selector)]}])
        result = assess_and_answer("감면 기준은?", selector_retrieval(), FakeClient(response))
        self.assertEqual(result["draft_status"], "code_checked_draft", result)
        self.assertEqual(result["duplicate_citation_ids_merged"],
                         [{"kind": "context_selection", "owner": "claim", "index": 0, "removed": [selector]}])
        self.assertEqual(result["source_selection_trace"]["claims"][0]["context_selections"], [selector])
        self.assertEqual(len(result["audit"][0]["parsed_response"]["claims"][0]["context_selections"]), 2)
        single = generation(text, ids=[], claims=[{"text": text, "citation_ids": [], "context_selections": [selector]}])
        plain = assess_and_answer("감면 기준은?", selector_retrieval(), FakeClient(single))
        self.assertEqual(result["generated_claims"], plain["generated_claims"])

    def test_draft_without_repeats_has_no_record_and_unchanged_output(self):
        import local_rag_answer as module
        for response, data, question in [(generation(), retrieval(), self.QUESTION),
                                         (generation(ids=[]), retrieval(), self.QUESTION)]:
            result = assess_and_answer(question, data, FakeClient(response))
            self.assertNotIn("duplicate_citation_ids_merged", result)
        draft = generation()
        before = copy.deepcopy(draft)
        self.assertEqual(module.merge_duplicate_generated_citations(draft), [])
        self.assertEqual(module.packed(draft), module.packed(before))

    def test_unknown_or_empty_ids_still_fail(self):
        import local_rag_answer as module
        for ids in (["C999", "C999"], ["", ""], ["C001", ""]):
            client = FakeClient(generation(ids=ids))
            result = assess_and_answer(self.QUESTION, retrieval(), client)
            self.assertEqual(result["decision"], "processing_error", (ids, result))
            self.assertNotIn("duplicate_citation_ids_merged", result)
            self.assertEqual(len(client.calls), 1)
        # Empty strings are never merged; the existing checks keep rejecting them.
        draft = {"claims": [{"text": "x", "citation_ids": ["", "", " "]}], "calculations": []}
        self.assertEqual(module.merge_duplicate_generated_citations(draft), [])
        self.assertEqual(draft["claims"][0]["citation_ids"], ["", "", " "])
        with self.assertRaises(module.AnswerError):
            module._unique_strings(draft["claims"][0]["citation_ids"], "invalid_claim_citation_ids")
