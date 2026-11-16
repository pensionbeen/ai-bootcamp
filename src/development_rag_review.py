"""Explicit Solar review of an immutable, already generated operating response.

This module is not part of ask/production. Expectations and fixture answers are
never accepted as review inputs. No generation request is made here.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import time

import local_rag_answer as runtime


SCHEMA_VERSION = 'development-rag-review-v1'
MODEL = 'solar-pro4'
PUBLIC_REVIEW_CONTRACT = 'whole_answer_selected_citations_v1'
PUBLIC_REVIEW_FLAGS = ('supported','complete','citations_sufficient','conditions_satisfied',
                       'scope_correct','units_correct','calculation_correct','boundary_preserved')
GATE_NOTE = '''
개발 평가다. response_under_review는 운영에서 이미 생성된 평가 대상이며 정답이 아니다.
동일 질문과 제공 근거로 판정하고 답변을 새로 작성하지 않는다. 원래 응답의 정보 부족·외부
자료 유보 상태를 함께 검토하되 그 결론을 따라야 하는 것은 아니다.
response_handling_correct는 운영의 실제 공개 처리 결과가 적절한지를 판정한다.
missing_information_correct와 missing_materials_correct는 운영이 제시한 각각의 실제 목록이
필요하고 충분한지 판정한다. decision 이름이 같다는 이유만으로 true로 표시하지 않는다.
이미 질문에 있는 사실을 다시 요구하거나 실제 누락된 사실·자료를 다른 항목으로 바꿔
묻는 응답은 false와 response_handling_issues로 거절한다. 목록을 고쳐서 통과시키지 않는다.
'''
VERIFY_NOTE = '''
개발 평가다. claim_segments와 operating_claim_citations는 운영 응답의 변경 불가능한 주장과
그 주장이 실제 사용한 인용이다. 각 주장에 지정된 원래 인용 전체가 적절한지 검토한다.
citation_ids에는 해당 주장의 원래 ID 집합을 빠짐없이 그대로 반환한다. 다른 근거를 추가,
삭제, 교체하거나 다른 주장에 재귀속해 응답을 고치지 않는다. 잘못 인용한 주장은 false와
issues로 거절한다. operating_calculations의 식·결과·원래 인용도 바꾸지 않는다.
검사 전용 문맥으로 누락된 조건·예외를 찾을 수 있으나 운영 응답의 근거를 보충할 수 없다.
public_contract_review는 별도로 공개 답 전체와 실제 선택된 cited_evidence 집합을 평가한다.
주장별 ID 반복 여부만으로 공개 답을 거절하지 않는다. 각 주장에 실제 적힌 ID는 위 진단에서
그대로 검사한다. public supported는 정확한 대상·행·판본의 근거가 전체 답의 각 주장을
지원하는지, complete는 질문이 요구한 확인 가능한 부분의 누락이 없는지 판단한다.
citations_sufficient는 실제 공개 목록의 정확성과 충분성이다. 추가 인용 자체는 오류가
아니지만 무관하거나 잘못된 근거는 거절한다. 조건·예외, 범위, 단위, 산술도 각각 검사한다.
boundary_preserved는 미확인 외부 자료나 사용자 사실의 결론을 유보하면서 조례로 확인되는
부분은 충분히 답했는지 평가한다. 미선택 후보나 verification_only로 답 근거를 채우지 않는다.
public_contract_review의 issues에는 이 공개 계약의 문제만 쓰며, 내부 claim ID의 배치만
다르다는 진단은 넣지 않는다. 두 결과가 다를 수 있고 원답이나 인용을 고쳐서는 안 된다.
'''


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))+'\n').encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def _unique_ids(values):
    return (isinstance(values, list) and bool(values)
            and all(isinstance(v, str) and v for v in values) and len(values)==len(set(values)))


def response_snapshot(audit, public, catalog=None):
    """Freeze actual text, per-claim citations and source identity, not self scores."""
    result=audit.get('result', {})
    require(audit.get('status')=='completed', 'operating_audit_not_completed')
    require(result.get('answer_allowed') is False and result.get('final_answer_generated') is False,
            'operating_approval_flags_changed')
    claims=copy.deepcopy(result.get('generated_claims', []))
    calculations=copy.deepcopy(result.get('generated_calculations', []))
    generated=result.get('draft_status')=='code_checked_draft'
    answer=(result.get('draft_response') or {}).get('answer', '')
    expected_status='code_checked_draft' if generated else result.get('decision')
    require(public.get('status')==expected_status, 'public_status_differs_from_operating_response')
    require(all(public.get(key,[])==result.get(key,[]) for key in ('missing_information','missing_materials')),
            'public_requirements_differ_from_operating_response')
    if generated:
        require(result.get('code_checks', {}).get('status')=='passed', 'operating_code_checks_not_passed')
        require(isinstance(claims, list) and bool(claims), 'operating_claims_missing')
        require(result.get('claim_joiner')=='\n\n', 'operating_claim_joiner_changed')
        for claim in claims:
            require(isinstance(claim, dict) and set(claim)=={'text', 'citation_ids'}, 'operating_claim_shape_changed')
            require(isinstance(claim['text'], str) and claim['text'] and claim['text']==claim['text'].strip(),
                    'operating_claim_text_changed')
            require(_unique_ids(claim['citation_ids']), 'operating_claim_citations_invalid')
        require('\n\n'.join(c['text'] for c in claims)==answer, 'operating_claim_text_not_answer')
        segments={'claim_'+str(i+1):c['text'] for i,c in enumerate(claims)}
        require(result.get('claim_segments')==segments, 'operating_claim_segments_changed')
        expected_public=answer
        if result.get('decision')=='external_material_required':
            notice=result.get('scope_notice', {}).get('text')
            require(isinstance(notice, str) and notice, 'operating_scope_notice_missing')
            expected_public=notice+'\n\n'+answer
        if result.get('decision')=='needs_clarification':
            from public_rag_response import CLARIFICATION_NOTICE
            require(public.get('clarification_required') is True
                    and public.get('fully_resolved') is False
                    and bool(public.get('missing_information')), 'clarification_boundary_lost')
            expected_public=CLARIFICATION_NOTICE+'\n\n'+answer
        require((public.get('response') or {}).get('answer')==expected_public,
                'public_answer_differs_from_operating_response')
    else:
        require(not claims and not calculations and not answer, 'unpublished_operating_claims_present')
        require(result.get('decision') in ('out_of_scope', 'insufficient_evidence', 'needs_clarification',
                                           'external_material_required'), 'operating_response_not_reviewable')
    citations=copy.deepcopy((result.get('draft_response') or {}).get('citation', []))
    by_id={c['unit_id']:c for c in citations}
    require(len(by_id)==len(citations), 'duplicate_operating_citation')
    used={uid for claim in claims for uid in claim['citation_ids']}
    require(used<=set(by_id), 'operating_claim_has_unknown_citation')
    require(all(not by_id[uid].get('context_only') for uid in used), 'operating_claim_uses_context_only')
    if generated:
        from public_rag_response import render_sources
        owners={row['unit_id']:row for row in result.get('source_citations',[])+citations
                if row.get('kind')!='required_context'}
        expected_sources=render_sources([row for row in citations if row['unit_id'] in used],catalog or {},owners)
        fields=('citation','source_url','effective_date')
        shown=public.get('sources')
        require(isinstance(shown,list) and all(isinstance(row,dict) for row in shown)
                and [tuple(row.get(key) for key in fields) for row in shown]
                ==[tuple(row[key] for key in fields) for row in expected_sources],
                'public_sources_differ_from_selected_citations')
        for field in fields:
            expected=[row[field] for row in expected_sources]
            if field!='citation' and expected and len(set(expected))==1:expected=expected[0]
            require((public.get('response') or {}).get(field)==expected,
                    'public_sources_differ_from_selected_citations')
    require(isinstance(calculations, list), 'operating_calculations_invalid')
    for calculation in calculations:
        require(isinstance(calculation, dict) and set(calculation)=={'expression','result','citation_ids'}
                and _unique_ids(calculation['citation_ids']) and set(calculation['citation_ids'])<=used,
                'operating_calculation_citations_invalid')
    prepared=copy.deepcopy(result.get('prepared_evidence'))
    require(isinstance(prepared, dict) and prepared.get('as_of_date')=='2026-10-01',
            'operating_prepared_evidence_missing')
    require(all(isinstance(prepared.get(key), list) for key in ('primary_units','contexts','condition_candidates'))
            and isinstance(prepared.get('external_tasks'), dict), 'operating_prepared_evidence_invalid')
    snapshot={'question':audit['question'], 'as_of_date':'2026-10-01', 'decision':result.get('decision'),
              'draft_status':result.get('draft_status'), 'answer':answer, 'claims':claims,
              'calculations':calculations, 'citations':citations,
              'source_citations':copy.deepcopy(result.get('source_citations', [])),
              'condition_sources':copy.deepcopy(result.get('condition_sources', [])),
              'prepared_evidence':prepared, 'verification_only':copy.deepcopy(result.get('verification_only')),
              'selected_ids':copy.deepcopy(result.get('selected_ids', [])),
              'missing_information':copy.deepcopy(result.get('missing_information', [])),
              'missing_materials':copy.deepcopy(result.get('missing_materials', [])),
              'scope_notice':copy.deepcopy(result.get('scope_notice')),
              'answer_scope':result.get('answer_scope'), 'fully_resolved':result.get('fully_resolved'),
              'public_response':{k:copy.deepcopy(v) for k,v in public.items() if k!='called'},
              'pipeline_code':copy.deepcopy(audit.get('code')), 'database':copy.deepcopy(audit.get('database'))}
    from evaluate_local_rag import generation_selection_response
    snapshot.update(source_selection_contract=result.get('source_selection_contract'),
                    context_source_catalog=copy.deepcopy(result.get('context_source_catalog')),
                    source_selection_trace=copy.deepcopy(result.get('source_selection_trace')),
                    generation_selection_response=copy.deepcopy(generation_selection_response(result)),
                    locator_proofs=copy.deepcopy(result.get('code_checks',{}).get('locator_proofs',[])),
                    metadata_proof_contract=result.get('code_checks',{}).get('metadata_proof_contract'),
                    metadata_proofs=copy.deepcopy(result.get('code_checks',{}).get('metadata_proofs',[])))
    if generated:
        require(isinstance(snapshot['verification_only'], dict), 'operating_review_context_missing')
    return snapshot


def _safe_receipts(receipts):
    """Raw provider reasoning stays in its existing cache, never in this report."""
    safe=[]
    for entry in receipts:
        row={k:copy.deepcopy(entry[k]) for k in ('stage','request_sha256','format_repair','budget_expansion',
                                                'meta','error_type','validation_error','parsed_response') if k in entry}
        response=entry.get('response', {})
        if isinstance(response, dict):
            row['usage']=copy.deepcopy(response.get('usage'))
            row['finish_reasons']=[choice.get('finish_reason') for choice in response.get('choices', [])]
        safe.append(row)
    return safe


def _same_calculations(actual, expected):
    def canonical(values):
        return [{'expression':c.get('expression'), 'result':c.get('result'),
                 'citation_ids':sorted(c.get('citation_ids', []))} for c in values]
    return (isinstance(actual, list) and all(_unique_ids(c.get('citation_ids', [])) for c in actual)
            and canonical(actual)==canonical(expected))


def review_response(audit, public, client, *, source_audit_sha256=None, catalog=None):
    """Review this response only. Two explicit model stages, no draft or retry."""
    original_digest=digest(audit)
    started=time.monotonic();receipts=[];called_before=getattr(client,'called',None)
    report={'schema_version':SCHEMA_VERSION, 'status':'processing_error', 'passed':False,
            'gate_decision_consistent':None, 'gate_response_handling_passed':None,'model_verification_passed':None,
            'operating_response_changed':False, 'model':MODEL, 'new_generation_calls':0,
            'answer_allowed':False, 'final_answer_generated':False}
    try:
        require(runtime.MODEL==MODEL, 'development_model_must_be_solar_pro4')
        require(all(hasattr(runtime,name) for name in ('model_citations','compact_model_input',
                'restore_verification_citations','_coverage_repair_schema','_verify_result')),
                'development_runtime_contract_unavailable')
        snapshot=response_snapshot(audit, public,catalog=catalog)
        # Reuse the independent exact-span check; no fixture/gold enters prompts.
        from evaluate_local_rag import __file__ as evaluator_file, runtime_source_errors
        from public_rag_response import __file__ as public_file
        require(not runtime_source_errors(audit, []), 'operating_source_integrity_failed')
        identity={'schema_version':SCHEMA_VERSION, 'model':MODEL, 'snapshot_sha256':digest(snapshot),
                  'original_audit_sha256':source_audit_sha256 or original_digest,
                  'review_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'source_integrity_code_sha256':hashlib.sha256(Path(evaluator_file).read_bytes()).hexdigest(),
                  'public_render_code_sha256':hashlib.sha256(Path(public_file).read_bytes()).hexdigest(),
                  'runtime_review_code_sha256':hashlib.sha256(Path(runtime.__file__).read_bytes()).hexdigest()}
        report.update(identity=digest(identity), identity_components=identity,
                      answer_sha256=digest(snapshot['answer']), claims_sha256=digest(snapshot['claims']),
                      sources_sha256=digest({'originals':snapshot['source_citations'], 'citations':snapshot['citations']}),
                      public_response_sha256=digest(snapshot['public_response']),
                      operating_decision=snapshot['decision'], operating_draft_status=snapshot['draft_status'])
        data=copy.deepcopy(snapshot['prepared_evidence']);data['question']=snapshot['question']
        data['response_under_review']={k:copy.deepcopy(snapshot[k]) for k in
                                      ('decision','answer','missing_information','missing_materials','public_response')}
        ids=[p['unit_id'] for p in data['primary_units']]
        units={p['unit_id']:{'kind':p['kind'], 'dependency_primary_ids':p.get('dependency_primary_ids', [])}
               for p in data['primary_units']}
        conditions={c['condition']['condition_id']:c for c in data['condition_candidates']}
        # Lock existing repair/capacity budgets; output-limit or format failure
        # is a reported failure, not a second chance to rewrite the judgment.
        repair={'used':False, 'capacity_used':True, 'stages':{'gate':True,'draft':True,'verify':True}}
        gate_schema=runtime._gate_schema(ids,conditions,data['external_tasks'])
        gate_schema=runtime.obj({**gate_schema['properties'],
            **{key:runtime.BOOLEAN for key in ('response_handling_correct','missing_information_correct','missing_materials_correct')},
            'response_handling_issues':runtime.STRINGS})
        gate=runtime._call(client,'gate',runtime.GATE_PROMPT+GATE_NOTE,data,gate_schema,receipts,repair)
        gate=runtime._normalize_gate(gate, conditions, data.get('expansion_attempted', False))
        runtime._validate_gate(gate, units, conditions, data.get('expansion_attempted', False), data['external_tasks'])
        report['gate']=copy.deepcopy(gate)
        report['gate_decision_consistent']=gate['decision']==snapshot['decision']
        report['gate_response_handling_passed']=(all(gate[key] is True for key in
            ('response_handling_correct','missing_information_correct','missing_materials_correct'))
            and not gate['response_handling_issues'])
        if snapshot['draft_status']!='code_checked_draft':
            report.update(status='completed', passed=report['gate_decision_consistent'] and report['gate_response_handling_passed'],
                          review_kind='decision_review_without_generated_answer')
            return report
        used={uid for c in snapshot['claims'] for uid in c['citation_ids']}
        citations=[c for c in snapshot['citations'] if c['unit_id'] in used]
        drafting={'question':snapshot['question'], 'as_of_date':snapshot['as_of_date'],
                  'cited_evidence':runtime.model_citations(citations), 'condition_candidates':[], 'condition_review':[]}
        compact,crosswalk=runtime.compact_model_input(drafting)
        inverse={uid:alias for alias,uid in crosswalk.items()}
        segments={'claim_'+str(i+1):c['text'] for i,c in enumerate(snapshot['claims'])}
        locked={key:[inverse[uid] for uid in claim['citation_ids']]
                for key,claim in zip(segments,snapshot['claims'])}
        scope_limited=snapshot['decision']=='external_material_required'
        schema=runtime._coverage_repair_schema(list(crosswalk), segments, scope_limited)
        schema=runtime.obj({**schema['properties'],'public_contract_review':runtime.obj({
            **{key:runtime.BOOLEAN for key in PUBLIC_REVIEW_FLAGS},'issues':runtime.STRINGS})})
        for key,aliases in locked.items():
            schema['properties']['claims']['properties'][key]['properties']['citation_ids']['items']['enum']=aliases
        calculations=copy.deepcopy(snapshot['calculations'])
        for calculation in calculations:
            calculation['citation_ids']=[inverse[uid] for uid in calculation['citation_ids']]
        verify_data={**compact, 'draft_answer':snapshot['answer'], 'claim_segments':segments,
                     'operating_claim_citations':locked, 'operating_calculations':calculations,
                     'public_response_under_review':copy.deepcopy(snapshot['public_response']),
                     'verification_only':copy.deepcopy(snapshot['verification_only']),
                     'development_gate':copy.deepcopy(gate)}
        prompt=runtime.VERIFY_PROMPT+runtime.SHARED_EVIDENCE_PROMPT+VERIFY_NOTE
        if scope_limited:
            prompt+=runtime.SCOPE_LIMITED_VERIFY_PROMPT
            verify_data.update(answer_scope='ordinance_only', fully_resolved=False,
                               scope_notice=snapshot['scope_notice'], missing_materials=snapshot['missing_materials'],
                               missing_information=snapshot['missing_information'])
        reviewed=runtime._call(client, 'verify', prompt, verify_data, schema, receipts, repair)
        public_review=copy.deepcopy(reviewed['public_contract_review'])
        reviewed=runtime.restore_verification_citations(reviewed, crosswalk, list(crosswalk))
        for key,claim in zip(segments,snapshot['claims']):
            actual=reviewed['claims'][key]['citation_ids']
            require(_unique_ids(actual) and set(actual)==set(claim['citation_ids']),
                    'development_judge_changed_operating_citations')
            # Sets may be returned in another order; proof replay uses the
            # original immutable claim's citation sequence after equality.
            reviewed['claims'][key]['citation_ids']=list(claim['citation_ids'])
        require(_same_calculations(reviewed['calculations'], snapshot['calculations']),
                'development_judge_changed_operating_calculations')
        reviewed['claims']=[{'text':text, **reviewed['claims'][key]} for key,text in segments.items()]
        source_options=({'source_citations':snapshot['source_citations']}
                        if snapshot['source_selection_contract'] or snapshot['locator_proofs']
                        or snapshot['metadata_proof_contract'] or snapshot['metadata_proofs'] else {})
        verification=runtime._verify_result(reviewed, snapshot['answer'], citations, snapshot['question'],
                                             scope_limited, **source_options)
        if snapshot['metadata_proof_contract']:
            require(verification.get('metadata_proof_contract')==snapshot['metadata_proof_contract']
                    and verification.get('metadata_proofs')==snapshot['metadata_proofs'],
                    'development_metadata_proof_replay_changed')
        public_checks=runtime._check_claims(snapshot['claims'],snapshot['calculations'],snapshot['answer'],
                                           citations,snapshot['question'],snapshot['source_citations'])
        public_passed=(all(public_review[key] is True for key in PUBLIC_REVIEW_FLAGS)
                       and not public_review['issues'] and public_checks['status']=='passed')
        report.update(status='completed', review_kind='same_operating_answer_and_citations',
                      public_review_contract=PUBLIC_REVIEW_CONTRACT,public_contract_review=public_review,
                      public_code_checks=public_checks,public_contract_passed=public_passed,
                      claim_binding_passed=verification['status']=='passed',
                      verification=verification, model_verification_passed=verification['status']=='passed')
        report['passed']=(report['gate_decision_consistent'] and report['gate_response_handling_passed']
                          and report['public_contract_passed'])
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        report['error_code']=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
    finally:
        report['receipts']=_safe_receipts(receipts)
        called_after=getattr(client,'called',None)
        client_counter=(type(called_before) is int and type(called_after) is int and called_after>=called_before)
        report['called']=(called_after-called_before if client_counter
                          else sum(x.get('meta', {}).get('called', 0) for x in receipts))
        report['called_source']='client_attempt_counter_delta' if client_counter else 'receipt_metadata'
        report['elapsed_seconds']=round(time.monotonic()-started, 3)
        report['operating_response_changed']=digest(audit)!=original_digest
        if report['operating_response_changed']:
            report.update(status='processing_error', passed=False, error_code='operating_response_mutated')
    return report


def review_saved_audit(project, audit, public, *, check_only=False):
    """Explicit evaluator entry. Never modifies the operating audit or its manifest."""
    from local_rag_client import Client
    project=Path(project).resolve(strict=True)
    audit_path=Path(audit.get('output_paths', {}).get('audit', ''))
    require(audit_path.is_file() and audit_path.resolve().is_relative_to(project), 'saved_operating_audit_required')
    original_bytes=audit_path.read_bytes();stored=json.loads(original_bytes)
    manifest_path=Path(stored.get('output_paths',{}).get('manifest',audit_path.with_name('audit.manifest.json')))
    require(manifest_path.is_file() and manifest_path.resolve().is_relative_to(project), 'saved_operating_manifest_required')
    manifest=json.loads(manifest_path.read_bytes())
    require(isinstance(stored.get('run_identity'),str) and bool(stored['run_identity'])
            and manifest.get('run_identity')==stored['run_identity']
            and manifest.get('audit_sha256')==hashlib.sha256(original_bytes).hexdigest(),
            'saved_operating_audit_hash_mismatch')
    # run_question may add cache/call metadata; only the immutable result counts.
    require(all(stored.get(key)==audit.get(key) for key in
                ('result','question','run_identity','code','database','as_of_date')),
            'saved_operating_response_changed')
    client=Client(project, check_only=check_only)
    from public_rag_response import attachment_catalog
    rows=(audit['result'].get('draft_response') or {}).get('citation',[])
    needs_catalog=any(row.get('locator',{}).get('attachment_id') or row.get('locator',{}).get('source_refs') for row in rows)
    catalog=attachment_catalog(project) if needs_catalog else {}
    review=review_response(audit, public, client, source_audit_sha256=hashlib.sha256(original_bytes).hexdigest(),catalog=catalog)
    if audit_path.read_bytes()!=original_bytes:
        review.update(status='processing_error',passed=False,error_code='saved_operating_audit_changed_during_review')
        return review
    if not check_only and review.get('identity'):
        folder=project/'data/processed/local_rag/2026-10-01/development_reviews'/review['identity'][:20]
        try:
            folder.mkdir(parents=True, exist_ok=True)
            review['output_file']=str(folder/'report.json')
            (folder/'report.json').write_bytes(encoded(review))
        except OSError:
            review.update(status='processing_error',passed=False,error_code='development_report_write_failed')
    return review


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--check-only', action='store_true', help='cached review only; no API or report writes')
    args=parser.parse_args(argv)
    from public_rag_response import public_result
    audit=json.loads(args.audit.read_bytes())
    audit={**audit, 'output_paths':{**audit.get('output_paths', {}), 'audit':str(args.audit.resolve())}}
    report=review_saved_audit(args.project, audit, public_result(audit, args.project), check_only=args.check_only)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 2


if __name__=='__main__':
    raise SystemExit(main())
