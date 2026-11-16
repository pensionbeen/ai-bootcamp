"""Public draft contract. Raw gate rationale, raw receipts and held text stay private."""
import copy
import json
from pathlib import Path
import re

try:  # runtime: sys.path holds src/, module imported as public_rag_response
    from release_policy import RELEASABLE_CHECK_ISSUES, RELEASED_WITH_CHECK_ISSUES
except ModuleNotFoundError:  # evaluation tools: sys.path holds only the project root, imported as src.public_rag_response
    from .release_policy import RELEASABLE_CHECK_ISSUES, RELEASED_WITH_CHECK_ISSUES

ASOF = '2026-10-01'
ORDINANCE_SCOPE_NOTICE = ('제공된 조례 문언에 한정한 설명입니다. 외부 자료 확인이 필요하며, '
                         '개별 사안의 실제 요금·자격·적용 여부는 아직 확정되지 않았습니다.')
CLARIFICATION_NOTICE = ('조례에서 확인되는 범위의 설명입니다. '
                        '개별 사안의 적용 여부는 추가 사실 확인이 필요합니다.')
MESSAGES = {
    'needs_clarification': '적용에 필요한 사실을 추가로 확인해야 합니다.',
    'external_material_required': '필요한 외부 자료가 아직 확인되지 않았습니다.',
    'insufficient_evidence': '현재 검색 근거만으로 답변을 확인할 수 없습니다.',
    'out_of_scope': '2026-10-01 기준 서울시 본청 6종 조례의 범위 밖입니다.',
    'verification_failed': '초안이 검증을 통과하지 못해 답변을 보류했습니다.',
    'code_check_failed': '초안이 출처·형식·수치 검사를 통과하지 못해 답변을 보류했습니다.',
    'processing_error': '처리 중 오류가 발생했습니다. 근거가 없다는 뜻은 아닙니다.',
    'not_cached': '아직 저장된 질문 결과가 없습니다.',
}


def scope_boundary_notice(missing_materials, missing_information):
    """Code-owned processing state, never a substitute for ordinance evidence."""
    if (not isinstance(missing_materials, list) or not missing_materials or
            not all(isinstance(value, str) and value.strip() for value in missing_materials)):
        raise ValueError('scope_limited_draft_without_external_boundary')
    if (not isinstance(missing_information, list) or
            not all(isinstance(value, str) and value.strip() for value in missing_information)):
        raise ValueError('invalid_scoped_missing_facts')
    return {'provenance': 'processing_status', 'text': ORDINANCE_SCOPE_NOTICE,
            'answer_scope': 'ordinance_only', 'external_material_status': '외부 자료 확인 필요',
            'fully_resolved': False, 'missing_materials': copy.deepcopy(missing_materials),
            'missing_information': copy.deepcopy(missing_information)}


def _attachment_label(attachment):
    # The source format uses 0000/00 for an unnumbered annex, not annex 0.
    if (attachment.get('attachment_type') == '별표' and
            attachment.get('attachment_no') == '0000' and
            attachment.get('attachment_branch_no') == '00'):
        return '별표'
    return attachment['label']


def attachment_catalog(project):
    manifest = json.loads((Path(project)/'data/raw/attachments/collection.manifest.json').read_bytes())
    if manifest.get('as_of_date') != ASOF:
        raise ValueError('attachment_catalog_scope_mismatch')
    return {row['attachment']['attachment_id']: {
        'ordin_name': row['ordin_name'], 'label': _attachment_label(row['attachment']),
        'source_url': row['source']['source_url'], 'effective_date': row['source']['effective_date']}
        for row in manifest['attachments']}


def citation_label(row, catalog, owners=None, detail=True):
    """Source label of one immutable citation row.

    detail=True (default) keeps the full locator: item, annex page/row positions and
    the required-context marker. detail=False is the public reader label used by
    render_sources: items fold into their paragraph (or article, when it has no
    paragraph), annex rows into the annex, and a required context into its owner's
    label. Every locator part is validated in both modes.
    """
    locator = row.get('locator', {})
    if locator.get('article'):
        if not row.get('ordin_name'):
            raise ValueError('citation_missing_ordinance_name')
        label = row['ordin_name']+' '+locator['article']
        paragraph = locator.get('paragraph')
        if paragraph is not None:
            label += ' '+_provision_label(paragraph, '항')
        if locator.get('item') is not None:
            item_label = _provision_label(locator['item'], '호')
            if paragraph is not None and detail:
                label += ' '+item_label
        return label
    if locator.get('supplement_id'):
        if not row.get('ordin_name') or not locator.get('title'):
            raise ValueError('citation_missing_supplement_label')
        return row['ordin_name']+' '+locator['title']
    aid = locator.get('attachment_id')
    refs = locator.get('source_refs', [])
    if not aid:
        ids = {ref.get('attachment_id') for ref in refs if ref.get('attachment_id')}
        aid = next(iter(ids)) if len(ids) == 1 else None
    if aid:
        meta = catalog.get(aid)
        if not meta or meta['source_url'] != row.get('source_url') or meta['effective_date'] != row.get('effective_date'):
            raise ValueError('attachment_citation_identity_mismatch')
        label = meta['ordin_name']+' '+meta['label']
        locations=[]
        for ref in refs:
            if ref.get('attachment_id') != aid:
                raise ValueError('mixed_attachment_citation')
            place = (str(ref['page'])+'쪽') if ref.get('page') is not None else ''
            if ref.get('row_index') is not None:
                place += ' 행 인덱스 '+str(ref['row_index'])
            if place and place not in locations:
                locations.append(place)
        return label+(' — '+', '.join(locations) if locations and detail else '')
    # Some reviewed ditto/unit contexts have source provenance but no standalone
    # article/table locator. Cite their exact owning bundle, without inventing a
    # statutory article or moving the context to a different source/date.
    parent=(owners or {}).get(row.get('parent_chunk_id'))
    if (row.get('kind')=='required_context' and parent and parent.get('kind')!='required_context'
            and parent.get('source_url')==row.get('source_url')
            and parent.get('effective_date')==row.get('effective_date')):
        return citation_label(parent,catalog,detail=detail)+(' — 필수 문맥' if detail else '')
    raise ValueError('citation_missing_structured_locator')


def _provision_label(value, suffix):
    """Format only explicit legal locator identities, including inserted items."""
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str):
        raise ValueError('invalid_citation_provision')
    text = re.sub(r'\s+', '', value)
    match = re.fullmatch(r'(?:제)?([1-9]\d*)(?:'+suffix+r')?(?:의([1-9]\d*))?', text)
    if match is None:
        raise ValueError('invalid_citation_provision')
    return '제'+match[1]+suffix+('의'+match[2] if match[2] else '')


def render_sources(citation_rows, catalog, owners=None):
    """Render only the selected immutable source rows, retaining first-occurrence order.

    Rows that fold to the same public label/source/date (items of one paragraph,
    rows of one annex) become one source entry, so citation/source_url/effective_date
    stay index-aligned.
    """
    sources=[];seen=set()
    for row in citation_rows:
        label=citation_label(row,catalog,owners,detail=False)
        url,date=row.get('source_url'),row.get('effective_date')
        if not isinstance(url,str) or not url.startswith('https://www.law.go.kr/'):
            raise ValueError('invalid_public_source_url')
        if not isinstance(date,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date) or date>ASOF:
            raise ValueError('invalid_public_effective_date')
        key=(label,url,date)
        if key not in seen:
            seen.add(key)
            sources.append({'citation':label,'source_url':url,'effective_date':date,
                            'date_meaning':row.get('effective_date_meaning','source_ordinance_effective_date')})
    return sources


def public_result(audit, project=None, catalog=None):
    """A verified draft is deliberately distinct from final answer approval.

    The nested response has four fields for supported drafts, exactly two for
    insufficient/out-of-scope. Multiple URL/date values remain aligned with the
    citation array; no first-source-only shortcut is allowed.
    """
    base = {'as_of_date': ASOF, 'answer_allowed': False, 'final_answer_generated': False}
    if audit.get('status') == 'processing_error':
        return {**base, 'status': 'processing_error', 'message': MESSAGES['processing_error']}
    result = audit.get('result')
    if result is None:
        return {**base, 'status': 'not_cached', 'message': MESSAGES['not_cached']}
    decision = result.get('decision')
    draft_status=result.get('draft_status')
    # A draft released with only releasable content-check issues is published like a
    # code-checked draft (same public status and body); only the message states that some
    # numeric/citation-scope checks failed. The audit keeps the failed checks.
    released=draft_status==RELEASED_WITH_CHECK_ISSUES
    code_checked=draft_status=='code_checked_draft' or released
    scoped=(decision=='external_material_required' and (draft_status=='scope_limited_draft' or code_checked))
    partial=(decision=='needs_clarification' and code_checked)
    if (decision == 'supported' and (draft_status == 'verified_draft' or code_checked)) or scoped or partial:
        if (result.get('answer_allowed') is not False
                or result.get('final_answer_generated') is not False):
            raise ValueError('draft_approval_invariant_failed')
        model_review=result.get('verification',{}).get('independent_model_review')
        if code_checked:
            checks=result.get('code_checks',{})
            claims=result.get('generated_claims')
            if released:
                issues=checks.get('issues')
                checks_ok=(checks.get('status')=='failed' and isinstance(issues,list) and bool(issues)
                           and all(isinstance(issue,str) for issue in issues)
                           and set(issues)<=RELEASABLE_CHECK_ISSUES)
            else:
                checks_ok=checks.get('status')=='passed' and not checks.get('issues')
            if (not checks_ok or not isinstance(claims,list) or not claims
                    or checks.get('checked_claims')!=claims or result.get('semantic_verification')!='not_run'
                    or result.get('verification',{}).get('status')!='not_run'
                    or result.get('citation_contract')!='exact_source_subunits_v1'
                    or checks.get('semantic_verification')!='not_run'):
                raise ValueError('code_checked_draft_invariant_failed')
        elif result.get('verification',{}).get('status')!='passed':
            raise ValueError('draft_approval_invariant_failed')
        if not code_checked and isinstance(model_review,dict):
            flags=('supported','complete','citations_sufficient','conditions_satisfied',
                   'scope_correct','units_correct','calculation_correct')
            if any(model_review.get(flag) is not True for flag in flags):
                raise ValueError('draft_approval_invariant_failed')
        if scoped and not code_checked and (not isinstance(model_review,dict) or model_review.get('external_boundary_preserved') is not True):
            raise ValueError('scope_notice_invariant_failed')
        draft = result.get('draft_response')
        if not isinstance(draft, dict) or not isinstance(draft.get('answer'), str) or not draft['answer'].strip():
            raise ValueError('missing_verified_draft')
        if code_checked and (result.get('claim_joiner')!='\n\n' or
                draft['answer']!='\n\n'.join(claim['text'] for claim in claims)):
            raise ValueError('public_generated_claim_text_mismatch')
        catalog = catalog if catalog is not None else attachment_catalog(project)
        owners={row['unit_id']:row for row in result.get('source_citations',[]) + draft.get('citation',[])
                if row.get('kind')!='required_context'}
        citation_rows = draft.get('citation', [])
        if result.get('citation_contract') == 'exact_source_subunits_v1':
            if not code_checked and not isinstance(model_review, dict):
                raise ValueError('missing_source_subunit_review')
            used_claims=claims if code_checked else model_review.get('claims', [])
            selected = {uid for claim in used_claims for uid in claim.get('citation_ids', [])}
            known = {row['unit_id']: row for row in citation_rows}
            if not selected or not selected <= set(known):
                raise ValueError('invalid_public_claim_citations')
            if any(known[uid].get('context_only') for uid in selected):
                raise ValueError('unrefined_public_claim_citation')
            citation_rows = [row for row in citation_rows if row['unit_id'] in selected]
        sources=render_sources(citation_rows,catalog,owners)
        if not sources:
            raise ValueError('verified_draft_without_public_citations')
        response={'answer':draft['answer'],'citation':[s['citation'] for s in sources],
                  'source_url':[s['source_url'] for s in sources],
                  'effective_date':[s['effective_date'] for s in sources]}
        # Preserve the historical single-source string contract when unambiguous.
        for field in ('source_url','effective_date'):
            if len(set(response[field]))==1:
                response[field]=response[field][0]
        message=('출처·형식 검사 초안 · 수치·인용 범위 검사 일부 미통과 · 의미 검증 미실행 · 최종 답변 승인 전' if released else
                 '출처·형식·수치 검사 완료 초안 · 의미 검증 미실행 · 최종 답변 승인 전' if code_checked else
                 '조례 범위의 검증된 초안 · 최종 답변 승인 전')
        output={**base,'status':'code_checked_draft' if released else draft_status,'message':message,
                'response':response,'sources':sources,'answer_scope':'ordinance_only'}
        if code_checked:
            output.update(semantic_verification='not_run',fully_resolved=False)
        if scoped:
            missing=result.get('missing_materials',[])
            facts=result.get('missing_information',[])
            notice=scope_boundary_notice(missing,facts)
            if (result.get('scope_notice') != notice or result.get('fully_resolved') is not False
                    or result.get('answer_scope') != 'ordinance_only'):
                raise ValueError('scope_notice_invariant_failed')
            # The verified model body contains legal propositions only. This
            # mandatory prefix comes from unresolved processing state, with no
            # invented amount, user-fact interpretation, or ordinance citation.
            output['response']['answer']=notice['text']+'\n\n'+draft['answer']
            output.update(missing_materials=copy.deepcopy(missing),external_material_status='외부 자료 확인 필요',
                          fully_resolved=False,scope_notice=notice)
            if facts:output['missing_information']=copy.deepcopy(facts)
        if partial:
            facts=result.get('missing_information')
            if (not isinstance(facts,list) or not facts
                    or not all(isinstance(fact,str) and fact.strip() for fact in facts)
                    or result.get('missing_materials')!=[]
                    or result.get('fully_resolved') is not False
                    or result.get('answer_scope')!='ordinance_only'):
                raise ValueError('partial_clarification_boundary_lost')
            output['response']['answer']=CLARIFICATION_NOTICE+'\n\n'+draft['answer']
            output.update(missing_information=copy.deepcopy(facts),clarification_required=True,
                          fully_resolved=False)
        return output
    if result.get('schema_version')=='local-rag-single-generation-v1' and result.get('draft_generated'):
        return {**base,'status':'code_check_failed','message':MESSAGES['code_check_failed']}
    if decision=='supported':
        return {**base,'status':'verification_failed','message':MESSAGES['verification_failed']}
    if decision not in MESSAGES:
        decision='processing_error'
    output={**base,'status':decision,'message':MESSAGES[decision]}
    for field in ('missing_information','missing_materials'):
        values=result.get(field,[])
        if values:
            if not isinstance(values,list) or not all(isinstance(v,str) for v in values):
                raise ValueError('invalid_missing_requirements')
            output[field]=copy.deepcopy(values)
    if decision in ('out_of_scope','insufficient_evidence'):
        output['response']={'answer':'확인 불가','citation':[]}
    return output
