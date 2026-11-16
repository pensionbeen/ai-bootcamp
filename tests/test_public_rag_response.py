import copy
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from public_rag_response import public_result,scope_boundary_notice,ORDINANCE_SCOPE_NOTICE,CLARIFICATION_NOTICE,attachment_catalog,citation_label


def audit():
    row={'unit_id':'x:1','parent_chunk_id':'x:1','kind':'body','ordin_name':'서울특별시 청년 기본 조례',
         'locator':{'article':'제3조','paragraph':None,'item':1},'source_url':'https://www.law.go.kr/ordinInfoP.do?ordinSeq=2054497',
         'effective_date':'2025-07-14','effective_date_meaning':'source_ordinance_effective_date'}
    return {'status':'completed','result':{'decision':'supported','draft_status':'verified_draft',
           'verification':{'status':'passed'},'answer_allowed':False,'final_answer_generated':False,
           'reason':'UNVERIFIED_SECRET_GATE_TEXT','audit':['PRIVATE_RECEIPT'],
           'draft_response':{'answer':'원문에 근거한 초안','citation':[row]}}}


def scoped_audit():
    value=audit()
    result=value['result']
    result.update(decision='external_material_required',draft_status='scope_limited_draft',
                  answer_scope='ordinance_only',fully_resolved=False,
                  missing_materials=['미확인 적용 규칙'],missing_information=['추가 확인 사항'])
    result['scope_notice']=scope_boundary_notice(result['missing_materials'],result['missing_information'])
    result['verification']['independent_model_review']={flag:True for flag in (
        'supported','complete','citations_sufficient','conditions_satisfied','scope_correct','units_correct',
        'calculation_correct','external_boundary_preserved')}
    return value


def partial_audit():
    value=audit();result=value['result'];body=result['draft_response']['answer']
    claims=[{'text':body,'citation_ids':['x:1']}]
    result.update(decision='needs_clarification',draft_status='code_checked_draft',
                  answer_scope='ordinance_only',fully_resolved=False,
                  missing_materials=[],missing_information=['취소 사유와 사용 예정일'],
                  generated_claims=claims,claim_joiner='\n\n',
                  code_checks={'status':'passed','issues':[],'checked_claims':copy.deepcopy(claims),
                               'semantic_verification':'not_run'},
                  semantic_verification='not_run',verification={'status':'not_run'},
                  citation_contract='exact_source_subunits_v1')
    return value


class PublicTests(unittest.TestCase):
    def test_known_partial_answer_keeps_citations_and_missing_user_facts(self):
        value=partial_audit();before=copy.deepcopy(value)
        output=public_result(value,catalog={})
        self.assertEqual(output['response']['answer'],CLARIFICATION_NOTICE+'\n\n원문에 근거한 초안')
        self.assertEqual(output['response']['citation'],['서울특별시 청년 기본 조례 제3조'])
        self.assertEqual(output['missing_information'],['취소 사유와 사용 예정일'])
        self.assertTrue(output['clarification_required'])
        self.assertFalse(output['fully_resolved'])
        self.assertNotIn('external_material_status',output)
        self.assertEqual(value,before)

    def test_partial_answer_cannot_hide_missing_or_conflicting_boundaries(self):
        mutations=[('missing_information',[]),('missing_information',[' ']),
                   ('missing_materials',['외부 규칙']),('fully_resolved',True),
                   ('answer_scope','all_materials')]
        for key,replacement in mutations:
            with self.subTest(key=key):
                value=partial_audit();value['result'][key]=replacement
                with self.assertRaisesRegex(ValueError,'partial_clarification_boundary_lost'):
                    public_result(value,catalog={})

    def test_only_verified_content_is_exposed(self):
        output=public_result(audit(),catalog={})
        self.assertEqual(set(output['response']),{'answer','citation','source_url','effective_date'})
        self.assertNotIn('UNVERIFIED',str(output))
        self.assertNotIn('PRIVATE',str(output))
        self.assertEqual(output['response']['effective_date'],'2025-07-14')
        self.assertFalse(output['answer_allowed'])
        self.assertEqual(output['response']['answer'],'원문에 근거한 초안')

    def test_held_and_error_do_not_masquerade_as_absence(self):
        value=audit();value['result']['draft_status']='held'
        output=public_result(value,catalog={})
        self.assertEqual(output['status'],'verification_failed')
        self.assertNotIn('response',output)
        output=public_result({'status':'processing_error','error':'SECRET'})
        self.assertNotIn('SECRET',str(output));self.assertNotIn('response',output)

    def test_multiple_sources_keep_all_dates(self):
        value=audit();row=copy.deepcopy(value['result']['draft_response']['citation'][0])
        row.update(ordin_name='서울특별시 도시공원 조례',effective_date='2026-07-13',
                   source_url='https://www.law.go.kr/ordinInfoP.do?ordinSeq=2147709')
        value['result']['draft_response']['citation'].append(row)
        output=public_result(value,catalog={})
        self.assertEqual(output['response']['effective_date'],['2025-07-14','2026-07-13'])
        self.assertEqual(len(output['response']['source_url']),2)

    def test_unlocated_mandatory_context_stays_with_its_owner(self):
        value=audit();context=copy.deepcopy(value['result']['draft_response']['citation'][0])
        context.update(unit_id='x:1::context::unit',kind='required_context',locator={})
        value['result']['draft_response']['citation'].append(context)
        output=public_result(value,catalog={})
        # The context folds into its owner's public label and is deduplicated with it.
        self.assertEqual(output['response']['citation'],['서울특별시 청년 기본 조례 제3조'])
        self.assertEqual(len(output['sources']),1)
        context['source_url']='https://www.law.go.kr/different'
        with self.assertRaises(ValueError):public_result(value,catalog={})

    def test_attachment_label_must_match_source_identity(self):
        value=audit();row=value['result']['draft_response']['citation'][0]
        row['locator']={'attachment_id':'A','source_refs':[{'attachment_id':'A','page':1,'row_index':4}]}
        catalog={'A':{'ordin_name':'조례','label':'별표 1','source_url':row['source_url'],'effective_date':'2025-07-14'}}
        output=public_result(value,catalog=catalog)
        self.assertEqual(output['response']['citation'],['조례 별표 1'])
        catalog['A']['source_url']='https://www.law.go.kr/different'
        with self.assertRaises(ValueError):public_result(value,catalog=catalog)

    def test_catalog_formats_only_the_explicit_unnumbered_annex_sentinel(self):
        cases=[('0000','00','별표 0','별표'),
               ('0002','00','별표 2','별표 2'),
               ('0002','01','별표 2의1','별표 2의1'),
               (None,None,'별표 0','별표 0')]
        for number,branch,raw_label,expected in cases:
            with self.subTest(number=number,branch=branch):
                value=audit();row=value['result']['draft_response']['citation'][0]
                row['locator']={'attachment_id':'A','source_refs':[{'attachment_id':'A','page':1,'row_index':4}]}
                manifest={'as_of_date':'2026-10-01','attachments':[{
                    'ordin_name':'조례','source':{'source_url':row['source_url'],'effective_date':row['effective_date']},
                    'attachment':{'attachment_id':'A','attachment_type':'별표','attachment_no':number,
                                  'attachment_branch_no':branch,'label':raw_label}}]}
                encoded=json.dumps(manifest,ensure_ascii=False).encode('utf-8')
                with patch('public_rag_response.Path.read_bytes',return_value=encoded):
                    catalog=attachment_catalog('unused')
                output=public_result(value,catalog=catalog)
                self.assertEqual(output['response']['citation'],['조례 '+expected])
                self.assertEqual(output['response']['source_url'],row['source_url'])
                self.assertEqual(output['response']['effective_date'],row['effective_date'])
                self.assertEqual(manifest['attachments'][0]['attachment']['label'],raw_label)

    def test_narrowed_unlocated_context_can_use_its_original_owner_for_the_label(self):
        value=audit();result=value['result']
        original=copy.deepcopy(result['draft_response']['citation'][0])
        child=copy.deepcopy(original);child['unit_id']='x:1::item::1'
        context=copy.deepcopy(original)
        context.update(unit_id='x:1::context::unit',kind='required_context',locator={})
        result['source_citations']=[original]
        result['draft_response']['citation']=[child,context]
        result['citation_contract']='exact_source_subunits_v1'
        result['verification']['independent_model_review']={flag:True for flag in (
            'supported','complete','citations_sufficient','conditions_satisfied','scope_correct','units_correct','calculation_correct')}
        result['verification']['independent_model_review']['claims']=[{'citation_ids':[context['unit_id']]}]
        output=public_result(value,catalog={})
        self.assertEqual(output['response']['citation'],['서울특별시 청년 기본 조례 제3조'])
        self.assertEqual(len(output['sources']),1)

    def test_only_absence_and_scope_get_two_field_fallback(self):
        for decision in ('insufficient_evidence','out_of_scope'):
            output=public_result({'status':'completed','result':{'decision':decision}})
            self.assertEqual(output['response'],{'answer':'확인 불가','citation':[]})
        for decision in ('needs_clarification','external_material_required','processing_error'):
            output=public_result({'status':'completed','result':{'decision':decision}})
            self.assertNotIn('response',output)

    def test_scope_limited_answer_retains_external_requirement(self):
        value=audit();value['result'].update(decision='external_material_required',
              draft_status='scope_limited_draft',missing_materials=['참조한 시행령 원문'],missing_information=['최초 허가일'],
              answer_scope='ordinance_only',fully_resolved=False,
              scope_notice=scope_boundary_notice(['참조한 시행령 원문'],['최초 허가일']))
        value['result']['verification']['independent_model_review']={flag:True for flag in (
            'supported','complete','citations_sufficient','conditions_satisfied','scope_correct','units_correct',
            'calculation_correct','external_boundary_preserved')}
        output=public_result(value,catalog={})
        self.assertEqual(output['external_material_status'],'외부 자료 확인 필요')
        self.assertFalse(output['fully_resolved'])
        self.assertFalse(output['answer_allowed'])
        self.assertEqual(output['missing_information'],['최초 허가일'])
        self.assertEqual(output['response']['answer'],ORDINANCE_SCOPE_NOTICE+'\n\n원문에 근거한 초안')
        self.assertEqual(value['result']['draft_response']['answer'],'원문에 근거한 초안')
        value['result']['missing_materials']=[]
        with self.assertRaises(ValueError):public_result(value,catalog={})

    def test_scope_prefix_is_fixed_and_mandatory_independent_of_model_body(self):
        value=scoped_audit()
        body='조례의 일반 규정과 단서만 설명합니다.'
        value['result']['draft_response']['answer']=body
        output=public_result(value,catalog={})
        self.assertEqual(output['response']['answer'],ORDINANCE_SCOPE_NOTICE+'\n\n'+body)
        self.assertEqual(output['scope_notice']['provenance'],'processing_status')
        self.assertEqual(output['missing_materials'],['미확인 적용 규칙'])
        self.assertEqual(output['missing_information'],['추가 확인 사항'])
        self.assertEqual(value['result']['draft_response']['answer'],body)
        self.assertFalse(output['fully_resolved'])
        self.assertFalse(output['answer_allowed'])
        self.assertEqual(output['response']['citation'],['서울특별시 청년 기본 조례 제3조'])

    def test_missing_or_tampered_notice_cannot_publish_scoped_body(self):
        missing=scoped_audit();missing['result'].pop('scope_notice')
        changed=scoped_audit();changed['result']['scope_notice']['text']='자료 확인 완료'
        dropped=scoped_audit();dropped['result']['scope_notice']['missing_materials']=[]
        resolved=scoped_audit();resolved['result']['fully_resolved']=True
        for value in (missing,changed,dropped,resolved):
            with self.assertRaisesRegex(ValueError,'scope_notice_invariant_failed'):
                public_result(value,catalog={})

    def test_false_independent_verifier_flags_never_publish(self):
        for flag in ('citations_sufficient','calculation_correct','external_boundary_preserved'):
            value=scoped_audit()
            value['result']['verification']['independent_model_review'][flag]=False
            with self.assertRaises(ValueError):public_result(value,catalog={})


PARK='서울특별시 도시공원 조례'
PARK_URL='https://www.law.go.kr/ordinInfoP.do?ordinSeq=2147709'
PARK_DATE='2026-07-13'
YOUTH_URL='https://www.law.go.kr/ordinInfoP.do?ordinSeq=2054497'
# Copied verbatim from the competition starter-kit check.py (CITE); the kit scores by
# (ordinance, article/annex, paragraph) and ignores items and row positions.
KIT_CITE_PATTERN=r"(서울특별시.*?(?:조례|규칙))\s*(제\d+조(?:의\d+)?|별표\s*\d*|별도\s*\d+)\s*(제\d+항)?"
# Optional: set STARTER_KIT_CHECK to the starter-kit check.py path to also compare with the kit file.
KIT_CHECK=Path(os.environ['STARTER_KIT_CHECK']) if os.environ.get('STARTER_KIT_CHECK') else None


def kit_parse(label):
    match=re.search(KIT_CITE_PATTERN,label.replace('\u00a0',' '))
    if not match:return None
    return re.sub(r'\s+','',match[1]),re.sub(r'\s+','',match[2]),match[3]


def kit_score(labels,gold):
    def matched(pred,answer):
        p,g=kit_parse(pred),kit_parse(answer)
        if not p or not g or p[:2]!=g[:2]:return 0.0
        return 1.0 if g[2] is None or p[2]==g[2] else 0.5
    return sum(max((matched(p,a) for p in labels for a in alts),default=0.0) for alts in gold)/len(gold)


def park_row(uid,locator,**extra):
    return {'unit_id':uid,'parent_chunk_id':extra.pop('parent','p:'+uid),'kind':extra.pop('kind','body'),
            'ordin_name':PARK,'locator':locator,'source_url':PARK_URL,'effective_date':PARK_DATE,
            'effective_date_meaning':'source_ordinance_effective_date',**extra}


def with_rows(rows):
    value=audit();value['result']['draft_response']['citation']=rows
    return value


class PublicCitationLabelTests(unittest.TestCase):
    def test_detailed_label_stays_the_default_and_public_label_folds(self):
        catalog={'A3':{'ordin_name':PARK,'label':'별표 3','source_url':PARK_URL,'effective_date':PARK_DATE}}
        item=park_row('i',{'article':'제18조','paragraph':4,'item':'2의3'})
        annex=park_row('t',{'attachment_id':'A3','source_refs':[{'attachment_id':'A3','page':1,'row_index':2}]})
        context=park_row('c',{},kind='required_context',parent='t')
        cases=[(item,PARK+' 제18조 제4항 제2호의3',PARK+' 제18조 제4항'),
               (annex,PARK+' 별표 3 — 1쪽 행 인덱스 2',PARK+' 별표 3'),
               (context,PARK+' 별표 3 — 1쪽 행 인덱스 2 — 필수 문맥',PARK+' 별표 3')]
        for row,detailed,public in cases:
            with self.subTest(row=row['unit_id']):
                self.assertEqual(citation_label(row,catalog,{'t':annex}),detailed)
                self.assertEqual(citation_label(row,catalog,{'t':annex},detail=False),public)

    def test_items_fold_into_their_paragraph_in_first_occurrence_order(self):
        rows=[park_row('p1',{'article':'제24조','paragraph':1,'item':None}),
              park_row('p2',{'article':'제24조','paragraph':2,'item':None})]
        rows+=[park_row('p2:'+str(n),{'article':'제24조','paragraph':2,'item':n}) for n in range(1,12)]
        rows.insert(1,park_row('p2:9의2',{'article':'제24조','paragraph':'제2항','item':'제9호의2'}))
        output=public_result(with_rows(rows),catalog={})
        self.assertEqual(output['response']['citation'],[PARK+' 제24조 제1항',PARK+' 제24조 제2항'])
        self.assertEqual(output['response']['source_url'],PARK_URL)
        self.assertEqual(output['response']['effective_date'],PARK_DATE)
        self.assertEqual([s['citation'] for s in output['sources']],output['response']['citation'])

    def test_item_without_paragraph_folds_into_the_article(self):
        rows=[park_row('a:'+str(n),{'article':'제2조','paragraph':None,'item':n}) for n in (1,3,5)]
        output=public_result(with_rows(rows),catalog={})
        self.assertEqual(output['response']['citation'],[PARK+' 제2조'])

    def test_folded_item_and_paragraph_are_still_validated(self):
        for locator in ({'article':'제2조','paragraph':None,'item':'가목'},
                        {'article':'제2조','paragraph':1,'item':0},
                        {'article':'제2조','paragraph':'둘째','item':1}):
            with self.subTest(locator=locator):
                with self.assertRaisesRegex(ValueError,'invalid_citation_provision'):
                    public_result(with_rows([park_row('a',locator)]),catalog={})

    def test_annex_rows_and_their_required_context_render_as_one_annex(self):
        catalog={'A3':{'ordin_name':PARK,'label':'별표 3','source_url':PARK_URL,'effective_date':PARK_DATE}}
        annex=lambda uid,row:park_row(uid,{'attachment_id':'A3','source_refs':[
            {'attachment_id':'A3','page':1,'row_index':row}]})
        owner=annex('t:2',2)
        context=park_row('t:2::context',{},kind='required_context',parent='t:2')
        rows=[park_row('s17',{'article':'제17조','paragraph':None,'item':None}),owner,context,annex('t:4',4),
              park_row('s18',{'article':'제18조','paragraph':3,'item':1})]
        value=with_rows(rows);value['result']['source_citations']=[owner]
        output=public_result(value,catalog=catalog)
        self.assertEqual(output['response']['citation'],[PARK+' 제17조',PARK+' 별표 3',PARK+' 제18조 제3항'])
        self.assertNotIn('행 인덱스',str(output));self.assertNotIn('필수 문맥',str(output))

    def test_annex_identity_and_mixed_refs_are_still_rejected(self):
        catalog={'A3':{'ordin_name':PARK,'label':'별표 3','source_url':PARK_URL,'effective_date':PARK_DATE}}
        mixed=park_row('t',{'attachment_id':'A3','source_refs':[{'attachment_id':'A3','page':1,'row_index':1},
                                                                {'attachment_id':'B','page':1,'row_index':1}]})
        with self.assertRaisesRegex(ValueError,'mixed_attachment_citation'):
            public_result(with_rows([mixed]),catalog=catalog)
        other=park_row('t',{'attachment_id':'A3','source_refs':[]},effective_date='2025-01-01')
        with self.assertRaisesRegex(ValueError,'attachment_citation_identity_mismatch'):
            public_result(with_rows([other]),catalog=catalog)

    def test_paired_arrays_stay_aligned_after_deduplication(self):
        youth=lambda uid,item:{**park_row(uid,{'article':'제3조','paragraph':None,'item':item}),
                               'ordin_name':'서울특별시 청년 기본 조례','source_url':YOUTH_URL,'effective_date':'2025-07-14'}
        rows=[park_row('a',{'article':'제5조','paragraph':1,'item':1}),youth('y1',1),
              park_row('b',{'article':'제5조','paragraph':1,'item':2}),youth('y2',2),
              park_row('c',{'article':'제6조','paragraph':None,'item':None})]
        output=public_result(with_rows(rows),catalog={})
        response=output['response']
        self.assertEqual(response['citation'],[PARK+' 제5조 제1항','서울특별시 청년 기본 조례 제3조',PARK+' 제6조'])
        self.assertEqual(response['source_url'],[PARK_URL,YOUTH_URL,PARK_URL])
        self.assertEqual(response['effective_date'],[PARK_DATE,'2025-07-14',PARK_DATE])
        self.assertEqual([(s['citation'],s['source_url'],s['effective_date']) for s in output['sources']],
                         list(zip(response['citation'],response['source_url'],response['effective_date'])))

    def test_kit_parse_keys_and_score_are_unchanged_by_folding(self):
        if KIT_CHECK is not None and KIT_CHECK.is_file():
            self.assertIn(KIT_CITE_PATTERN,KIT_CHECK.read_text(encoding='utf-8'))
        catalog={'A3':{'ordin_name':PARK,'label':'별표 3','source_url':PARK_URL,'effective_date':PARK_DATE}}
        rows=[park_row('s18:3',{'article':'제18조','paragraph':3,'item':None}),
              park_row('s18:3:1',{'article':'제18조','paragraph':3,'item':1}),
              park_row('s18:4:2',{'article':'제18조','paragraph':4,'item':2}),
              park_row('s2:1',{'article':'제2조','paragraph':None,'item':1}),
              park_row('t:2',{'attachment_id':'A3','source_refs':[{'attachment_id':'A3','page':1,'row_index':2}]}),
              park_row('t:2::context',{},kind='required_context',parent='t:2')]
        value=with_rows(rows);value['result']['source_citations']=[rows[4]]
        # Labels the previous renderer produced for the same rows.
        before=[PARK+' 제18조 제3항',PARK+' 제18조 제3항 제1호',PARK+' 제18조 제4항 제2호',PARK+' 제2조',
                PARK+' 별표 3 — 1쪽 행 인덱스 2',PARK+' 별표 3 — 1쪽 행 인덱스 2 — 필수 문맥']
        after=public_result(value,catalog=catalog)['response']['citation']
        self.assertEqual(after,[PARK+' 제18조 제3항',PARK+' 제18조 제4항',PARK+' 제2조',PARK+' 별표 3'])
        self.assertEqual({kit_parse(c) for c in after},{kit_parse(c) for c in before})
        self.assertNotIn(None,{kit_parse(c) for c in after})
        gold=[[PARK+' 제18조 제4항'],[PARK+' 별표 3',PARK+' 제17조'],[PARK+' 제2조 제1항'],[PARK+' 제18조 제3항']]
        self.assertEqual(kit_score(after,gold),kit_score(before,gold))


def released_audit():
    value=partial_audit();result=value['result']
    result.update(decision='supported',draft_status='released_with_check_issues',
                  missing_information=[])
    result['code_checks'].update(status='failed',issues=['answer_contains_unsupported_number'])
    return value


# Imports src.public_rag_response with only the project root on sys.path, the way the
# evaluation collect_*.py scripts do, and publishes a released draft.
PACKAGE_IMPORT_SCRIPT='''
import json,sys
root,audit=sys.argv[1],json.loads(sys.argv[2])
sys.path.insert(0,root)
assert all(not p.replace('\\\\','/').rstrip('/').endswith('/src') for p in sys.path),sys.path
from src.public_rag_response import public_result
print(json.dumps({'output':public_result(audit,catalog={}),
                  'top_level':sorted(m for m in ('release_policy','public_rag_response','local_rag_answer') if m in sys.modules),
                  'package':sorted(m for m in sys.modules if m.startswith('src.'))},ensure_ascii=False))
'''


class ReleasePolicyImportTests(unittest.TestCase):
    def test_released_draft_publishes_through_top_level_import(self):
        output=public_result(released_audit(),catalog={})
        self.assertEqual(output['status'],'code_checked_draft')
        self.assertEqual(output['response']['answer'],'원문에 근거한 초안')

    def test_released_draft_publishes_through_package_import_without_src_on_path(self):
        import subprocess
        root=Path(__file__).resolve().parents[1]
        run=subprocess.run([sys.executable,'-B','-I','-X','utf8','-c',PACKAGE_IMPORT_SCRIPT,str(root),
                            json.dumps(released_audit(),ensure_ascii=False)],
                           capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertEqual(run.returncode,0,run.stderr)
        value=json.loads(run.stdout)
        self.assertEqual(value['top_level'],[])
        self.assertEqual(value['package'],['src.public_rag_response','src.release_policy'])
        self.assertEqual(value['output'],public_result(released_audit(),catalog={}))
        self.assertEqual(value['output']['status'],'code_checked_draft')

    def test_release_constants_are_one_shared_object(self):
        import evaluate_local_rag,local_rag_answer,public_rag_response,release_policy
        for module in (local_rag_answer,public_rag_response,evaluate_local_rag):
            self.assertIs(module.RELEASABLE_CHECK_ISSUES,release_policy.RELEASABLE_CHECK_ISSUES)
            self.assertIs(module.RELEASED_WITH_CHECK_ISSUES,release_policy.RELEASED_WITH_CHECK_ISSUES)
        self.assertIs(local_rag_answer.ATTRIBUTION_ISSUE,release_policy.ATTRIBUTION_ISSUE)
        self.assertIs(local_rag_answer.INTRO_ONLY_ISSUE,release_policy.INTRO_ONLY_ISSUE)
        self.assertEqual(release_policy.RELEASABLE_CHECK_ISSUES,frozenset({
            'answer_contains_unsupported_number','unverified_derived_calculation',
            'claim_cites_only_intro_of_itemized_paragraph','obligation_strength_mismatch',
            'claim_attributes_uncited_provision'}))
        self.assertEqual(release_policy.RELEASED_WITH_CHECK_ISSUES,'released_with_check_issues')


if __name__=='__main__':unittest.main()
