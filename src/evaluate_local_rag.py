"""Run a predeclared local-corpus case set without exposing expectations to Solar."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

from ask_local_rag import run_question, DEFAULT_DATABASE
from public_rag_response import public_result
from local_rag_answer import merge_duplicate_generated_citations
from release_policy import RELEASABLE_CHECK_ISSUES, RELEASED_WITH_CHECK_ISSUES

LEGACY_CONTRACT='legacy-verified'
SINGLE_RESPONSE_CONTRACT='single-response'


def _releasable_checks(result):
    """Failed code checks whose issues are all releasable content checks (sent to the provision check)."""
    checks=result.get('code_checks',{});issues=checks.get('issues')
    return (checks.get('status')=='failed' and isinstance(issues,list) and bool(issues)
            and all(isinstance(issue,str) for issue in issues) and set(issues)<=RELEASABLE_CHECK_ISSUES)


def _released_valid(result):
    """A draft published although its only failed code checks are the releasable content checks."""
    return result.get('draft_status')==RELEASED_WITH_CHECK_ISSUES and _releasable_checks(result)


def _published(result,runtime_contract):
    return (result.get('draft_status') in ('code_checked_draft',RELEASED_WITH_CHECK_ISSUES) if runtime_contract==SINGLE_RESPONSE_CONTRACT
            else result.get('draft_status') in ('verified_draft','scope_limited_draft'))


def _public_published(public,runtime_contract):
    return (public.get('status')=='code_checked_draft' if runtime_contract==SINGLE_RESPONSE_CONTRACT
            else public.get('status') in ('verified_draft','scope_limited_draft'))


def execution_outcomes(case,runtime_contract):
    if runtime_contract not in (LEGACY_CONTRACT,SINGLE_RESPONSE_CONTRACT):raise ValueError('unknown_runtime_contract')
    outcomes=case.get('expected_outcomes',[{'decision':case['expected_decision'],'draft_status':case['expected_draft_status']}])
    mapped=[{**outcome,'draft_status':('code_checked_draft' if runtime_contract==SINGLE_RESPONSE_CONTRACT
             and outcome['draft_status'] in ('verified_draft','scope_limited_draft') else outcome['draft_status'])}
            for outcome in outcomes]
    if runtime_contract!=SINGLE_RESPONSE_CONTRACT:return mapped
    # Single-response partial answers: a clarification case may publish the ordinance-confirmed
    # scope as a draft while still asking for the missing user facts. The legal decision is unchanged.
    expanded=list(mapped)
    for outcome in mapped:
        if outcome['decision']=='needs_clarification' and outcome['draft_status']=='not_generated':
            if not any(c['decision']=='needs_clarification' and c['draft_status']=='code_checked_draft' for c in expanded):
                expanded.append({**outcome,'draft_status':'code_checked_draft'})
    return expanded


WITHHELD_NO_PROVISION='withheld_no_provision'


def _withheld_no_provision_valid(result):
    """Exact shape of a code-checked draft withheld by the pre-publication provision check."""
    adjustment=result.get('decision_adjustment')
    return (result.get('decision')=='insufficient_evidence'
            and isinstance(adjustment,dict) and set(adjustment)=={'from','to','reason'}
            and adjustment['from'] in ('supported','external_material_required','needs_clarification')
            and adjustment['to']=='insufficient_evidence' and adjustment['reason']=='provision_check_no_provision'
            and isinstance(result.get('provision_check'),dict)
            and result['provision_check'].get('provision_id')=='NONE'
            and result['provision_check'].get('basis')=='no_provision'
            and result.get('response')=={'answer':'확인 불가','citation':[],'effective_date':[]}
            and (result.get('code_checks',{}).get('status')=='passed' or _releasable_checks(result))
            and sum(row.get('stage')=='provision_check' for row in result.get('audit',[]))==1)


def _outcome_draft_status(result,runtime_contract):
    """A valid withheld draft is the existing not-generated insufficient outcome; nothing is published."""
    status=result.get('draft_status')
    if (runtime_contract==SINGLE_RESPONSE_CONTRACT and status==WITHHELD_NO_PROVISION
            and _withheld_no_provision_valid(result)):
        return 'not_generated'
    # A valid released draft is published exactly like a code-checked draft.
    if runtime_contract==SINGLE_RESPONSE_CONTRACT and _released_valid(result):
        return 'code_checked_draft'
    return status


def _used_claims(result,runtime_contract):
    if runtime_contract==SINGLE_RESPONSE_CONTRACT:
        if result.get('code_checks',{}).get('status')!='passed' and not _released_valid(result):return []
        # Actual generation provenance, never a self-supported semantic score.
        return result.get('generated_claims',[])
    return [claim for claim in result.get('verification',{}).get('independent_model_review',{}).get('claims',[])
            if claim.get('supported') is True]


def digest(raw):return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')


def validate_cases(project, path):
    raw=Path(path).read_bytes();fixture=json.loads(raw)
    if fixture.get('as_of_date')!='2026-10-01' or fixture.get('answer_allowed') is not False:
        raise ValueError('invalid_evaluation_scope')
    db=Path(project)/DEFAULT_DATABASE
    if digest(db.read_bytes())!=fixture['database_sha256']:
        raise ValueError('evaluation_database_changed')
    cases=fixture['cases']
    if not cases or len({c['id'] for c in cases})!=len(cases):
        raise ValueError('invalid_case_ids')
    connection=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        for case in cases:
            for anchor in case['source_anchors']:
                record=connection.execute('SELECT kind,source_json FROM chunks WHERE chunk_id=?',(anchor['chunk_id'],)).fetchone()
                if not record:raise ValueError('missing_case_anchor')
                source=json.loads(record[1])['source_record']
                key={'body':'quote_text','attachment':'primary_text','supplement':'source_text'}[record[0]]
                quote=source[key]
                if quote!=anchor['quote'] or digest(quote.encode('utf-8'))!=anchor['quote_sha256']:
                    raise ValueError('case_anchor_quote_changed')
                metadata=source.get('source',source)
                for key in ('ordin_seq','source_url','effective_date'):
                    # Supplement records carry the selected version's date only as
                    # selected_version_effective_date; read it with the same
                    # fallback the DB builder and answer code use.
                    value=(metadata.get('effective_date',metadata.get('selected_version_effective_date'))
                           if key=='effective_date' else metadata.get(key))
                    if key in anchor and anchor[key]!=value:
                        raise ValueError('case_anchor_identity_changed')
                    anchor.setdefault(key,value)
                # Derived only in memory from the hash-pinned DB. The frozen
                # fixture bytes and legal expectations remain unchanged.
                anchor['_validated_locator']={key:source.get(key) for key in ('article','paragraph','item')}
                anchor['_validated_source_span']=source.get('source_span')
                anchor['_validated_kind']=record[0]
            if 'expected_outcomes' in case:
                if not case['expected_outcomes'] or not isinstance(case.get('required_citations'),list):
                    raise ValueError('invalid_strict_case_contract')
            for anchor in case.get('context_anchors',[]):
                record=connection.execute('SELECT text,source_json FROM contexts WHERE context_id=?',(anchor['context_id'],)).fetchone()
                if not record or record[0]!=anchor['quote'] or digest(record[0].encode('utf-8'))!=anchor['quote_sha256']:
                    raise ValueError('case_context_anchor_changed')
                context=json.loads(record[1]);provenance=context.get('provenance',context)
                anchor['_validated_source_refs']=provenance.get('source_refs',[])
                owners={}
                for cid,source_json in connection.execute(
                        'SELECT c.chunk_id,c.source_json FROM chunk_context_links l JOIN chunks c ON c.chunk_id=l.chunk_id '
                        'WHERE l.context_id=? AND c.is_deleted=0',(anchor['context_id'],)):
                    source=json.loads(source_json)['source_record'];metadata=source.get('source',source)
                    owners[cid]={key:metadata.get(key) for key in
                                 ('ordin_seq','source_url','effective_date','as_of_date')}
                anchor['_validated_owners']=owners
    finally:connection.close()
    return fixture,digest(raw)


def used_citation_rows(result,runtime_contract):
    used={uid for claim in _used_claims(result,runtime_contract) for uid in claim.get('citation_ids',[])}
    return [row for row in (result.get('draft_response') or {}).get('citation',[])
            if row.get('unit_id') in used and not row.get('context_only')]


def anchor_in_selected(case,audit,runtime_contract=LEGACY_CONTRACT):
    result=audit.get('result',{});selected=set(result.get('selected_ids',[]))
    primary=case.get('expected_primary_id')
    if not primary:return True
    # A clarification has no answer/verifier claims; the gate's selected source
    # is the relevant evidence. A published draft requires actual claim use.
    if not _published(result,runtime_contract):
        if runtime_contract==SINGLE_RESPONSE_CONTRACT:
            # No claim has been generated. Check source arrival, not a fictional
            # model-selected citation or semantic support for the handling.
            anchor=next(a for a in case['source_anchors'] if a['chunk_id']==primary)
            for candidate in result.get('prepared_evidence',{}).get('primary_units',[]):
                if candidate.get('unit_id')!=primary:continue
                row={**candidate.get('source',{}),'unit_id':primary,'quote':candidate.get('text'),
                     'locator':candidate.get('locator',{}),'kind':candidate.get('kind')}
                if _used_source_matches_anchor(row,{},anchor):return True
            return False
        return primary in selected
    anchor=next(a for a in case['source_anchors'] if a['chunk_id']==primary)
    originals={row['unit_id']:row for row in result.get('source_citations',[])}
    used_rows=used_citation_rows(result,runtime_contract)
    context_anchors=case.get('context_anchors',[])
    if context_anchors:
        if not all(any(_used_context_matches_anchor(row,originals,context,selected) for row in used_rows)
                   for context in context_anchors):return False
        # An annex-level citation with a required note does not demand the fee
        # row that happened to retrieve that note. Explicit row requirements do.
        expected=[entry for entry in case.get('required_citations',[])
                  if entry.get('ordin_seq')==anchor.get('ordin_seq')]
        if (anchor.get('_validated_kind')=='attachment' and expected
                and all(entry.get('attachment') and entry.get('row_index') is None for entry in expected)
                and all(primary in context.get('_validated_owners',{}) for context in context_anchors)):
            return True
    for row in used_rows:
        if _used_source_matches_anchor(row,originals,anchor):return True
    return False


def _used_context_matches_anchor(row,originals,anchor,selected):
    """Require a used, DB-bound note; its retrieval carrier is not its identity."""
    if row.get('context_only') or row.get('kind')!='required_context':return False
    owner=row.get('parent_chunk_id');metadata=anchor.get('_validated_owners',{}).get(owner)
    if metadata is None or owner not in selected:return False
    origin_id=owner+'::context::'+anchor['context_id']
    original=originals.get(origin_id)
    if original is None or original.get('kind')!='required_context' or original.get('parent_chunk_id')!=owner:
        return False
    refs=anchor.get('_validated_source_refs')
    if not refs:return False
    for citation in (original,row):
        if not all(value is not None and citation.get(key)==value for key,value in metadata.items()):return False
        if citation.get('locator',{}).get('source_refs')!=refs:return False
        if citation.get('source_unit_id',anchor['context_id'])!=anchor['context_id']:return False
        if citation.get('quote')!=anchor['quote']:return False
    if row.get('unit_id')==origin_id and not row.get('source_origin_id'):return True
    return (row.get('source_origin_id')==origin_id
            and _exact_span(row.get('source_quote_span'),original['quote'])==(0,len(anchor['quote'])))


def _exact_span(span,text):
    if not isinstance(span,dict):return None
    start,end=span.get('start'),span.get('end')
    return (start,end) if type(start) is int and type(end) is int and 0<=start<end<=len(text) else None


def _body_context_record(source):
    """Validate a numbered or explicitly unnumbered provision independently."""
    try:
        proof=source['body_context_provenance']
        if (not isinstance(proof,dict) or set(proof)!={'contract','context_id','origin_id','source_record'}
                or proof['contract']!='single_body_paragraph_v1'):return None
        record=proof['source_record'];context=proof['context_id'];origin=proof['origin_id']
        quote=source['quote'];locator=source['locator'];span=record.get('source_span',{})
        start,end=span.get('start'),span.get('end');paragraph=record.get('paragraph')
        if (not isinstance(context,str) or not context or not isinstance(origin,str) or not origin
                or source.get('kind')!='required_context' or source.get('source_unit_id')!=context
                or source.get('unit_id')!=source.get('parent_chunk_id','')+'::context::'+context
                or record.get('chunk_id')!=origin or not isinstance(record.get('parent_id'),str)
                or not record['parent_id'].strip() or record['parent_id']==origin or record.get('is_deleted') is not False
                or not isinstance(quote,str) or not quote.strip() or record.get('quote_text')!=quote
                or 'paragraph' not in record
                or not (paragraph is None or (type(paragraph) is int and paragraph>0))
                or origin!=record['parent_id']+':'+('all' if paragraph is None else str(paragraph))
                or not isinstance(record.get('article'),str)
                or not re.fullmatch(r'제[1-9]\d*조(?:의[1-9]\d*)?',record['article']) or record.get('item') is not None
                or locator.get('article')!=record['article'] or locator.get('paragraph')!=paragraph
                or set(span)!={'start','end'} or type(start) is not int or type(end) is not int
                or start<0 or end-start!=len(quote) or locator.get('source_span')!=span
                or any(not record.get(key) or record[key]!=source.get(key)
                       for key in ('ordin_seq','source_url','effective_date','as_of_date'))
                or record['as_of_date']!='2026-10-01'
                or source.get('source_segments')!=[{'kind':'provision','paragraph':paragraph,
                    'start':0,'end':len(quote),'text':quote}]):return None
        return record
    except (KeyError,TypeError,AttributeError):
        return None


def body_context_provenance_errors(result):
    """Keep body identity and absolute offsets separate from local child spans."""
    originals={row.get('unit_id'):row for row in result.get('source_citations',[])}
    prepared={row.get('context_id'):row for row in result.get('prepared_evidence',{}).get('contexts',[])
              if 'body_context_provenance' in row}
    for source in originals.values():
        common=prepared.get(source.get('source_unit_id'))
        if common is not None and source.get('body_context_provenance')!=common['body_context_provenance']:
            return ['body_context_prepared_provenance_changed']
        if 'body_context_provenance' in source and _body_context_record(source) is None:
            return ['body_context_provenance_invalid']
        if 'body_context_provenance' in source and 'prepared_evidence' in result:
            if (common is None or common.get('text')!=source['quote'] or common.get('locator')!=source['locator']
                    or any(common.get('source',{}).get(key)!=source.get(key)
                           for key in ('ordin_seq','source_url','effective_date','as_of_date'))):
                return ['body_context_prepared_source_changed']
    for child in (result.get('draft_response') or {}).get('citation',[]):
        source=originals.get(child.get('source_origin_id'),{})
        proof=source.get('body_context_provenance')
        if proof is None:
            if 'body_context_provenance' in child:
                return ['body_context_provenance_without_origin']
            continue
        if child.get('body_context_provenance')!=proof:
            return ['body_context_child_provenance_changed']
        local=_exact_span(child.get('source_quote_span'),source.get('quote',''))
        span=source['locator']['source_span']
        if (local is None or child.get('locator',{}).get('source_span')!={
                'start':span['start']+local[0],'end':span['start']+local[1]}):
            return ['body_context_child_absolute_span_changed']
    return []


def _body_context_matches_anchor(source,anchor):
    record=_body_context_record(source)
    return bool(record and record['chunk_id']==anchor.get('chunk_id')
                and record['quote_text']==anchor.get('quote')
                and record['source_span']==anchor.get('_validated_source_span')
                and all(record.get(key)==anchor.get(key) for key in ('ordin_seq','source_url','effective_date'))
                and all(record.get(key)==anchor.get('_validated_locator',{}).get(key)
                        for key in ('article','paragraph')))


def _used_source_matches_anchor(row,originals,anchor):
    """Match legal source identity and actual source position, not runtime owner.

    An item from paragraph 1 can legitimately be reached through paragraph 3's
    owned full-article context. The owner ID must remain intact; it is not the
    legal paragraph identity. Unused candidates cannot establish this match.
    """
    if row.get('context_only'):return False
    if not all(row.get(key)==anchor.get(key) and anchor.get(key) is not None
               for key in ('ordin_seq','source_url','effective_date')):return False
    locator=row.get('locator',{});expected=anchor.get('_validated_locator',{})
    if expected.get('article'):
        if locator.get('article')!=expected['article'] or locator.get('paragraph')!=expected.get('paragraph'):
            return False
    if locator.get('item') is not None:
        printed=re.match(r'\s*(\d+(?:의\d+)?)\.\s',row.get('quote',''))
        if not printed or printed[1]!=str(locator['item']):return False
    if row.get('unit_id')==anchor['chunk_id'] and row.get('quote')==anchor['quote']:
        return True
    original=originals.get(row.get('source_origin_id'))
    if original is None:return False
    if not all(original.get(key)==row.get(key) for key in ('ordin_seq','source_url','effective_date','parent_chunk_id')):
        return False
    span=_exact_span(row.get('source_quote_span'),original.get('quote',''))
    if not span or original['quote'][span[0]:span[1]]!=row.get('quote'):return False
    if original['unit_id']==anchor['chunk_id'] and original['quote']==anchor['quote']:
        return True
    if 'body_context_provenance' in original:
        absolute=original.get('locator',{}).get('source_span',{})
        return (_body_context_matches_anchor(original,anchor)
                and row.get('body_context_provenance')==original['body_context_provenance']
                and locator.get('source_span')=={'start':absolute['start']+span[0],
                                                'end':absolute['start']+span[1]})
    # A complete article context uses positions in the source article. Pin its
    # paragraph range to the DB anchor before accepting a child of another owner.
    original_locator=original.get('locator',{})
    if (original.get('kind')!='required_context' or not expected.get('article')
            or original_locator.get('article')!=expected['article']):return False
    anchor_span=_exact_span(anchor.get('_validated_source_span'),original['quote'])
    if not anchor_span or original['quote'][anchor_span[0]:anchor_span[1]]!=anchor['quote']:return False
    if not anchor_span[0]<=span[0]<span[1]<=anchor_span[1]:return False
    segments=original.get('source_segments',[])
    return any(segment.get('kind')=='provision' and segment.get('paragraph')==expected.get('paragraph')
               and segment.get('start')==anchor_span[0] and segment.get('end')==anchor_span[1]
               and segment.get('text')==anchor['quote'] for segment in segments)


def parse_citation(label):
    if not isinstance(label,str):return None
    match=re.search(r'^(서울특별시.*?(?:조례|규칙))\s*(제\d+조(?:의\d+)?|별표\s*\d*)',label)
    if not match:return None
    tail=label[match.end():]
    para=re.search(r'제(\d+)항',tail)
    item=re.search(r'제(\d+)호(?:의(\d+))?',tail)
    row=re.search(r'행\s*인덱스\s*(\d+)',tail)
    return {'ordin_name':re.sub(r'\s+','',match[1]),
            'article':match[2] if match[2].startswith('제') else None,
            'paragraph':int(para[1]) if para else None,
            'item':item[1]+('의'+item[2] if item[2] else '') if item else None,
            'attachment':re.sub(r'\s+','',match[2]) if match[2].startswith('별표') else None,
            'row_index':int(row[1]) if row else None}


def matches_citation(label,expected):
    parsed=parse_citation(label)
    if not parsed or parsed['ordin_name']!=re.sub(r'\s+','',expected['ordin_name']):return False
    if expected.get('article') and expected.get('paragraph') is None:
        # Saved cases can still specify the exact underlying item.
        return (parsed['article'] == expected['article'] and parsed['paragraph'] is None
                and parsed['item'] is None)
    for key in ('article','paragraph','item','attachment','row_index'):
        required=expected.get(key)
        if required is not None:
            required=re.sub(r'\s+','',required) if isinstance(required,str) else required
            if parsed[key]!=required:return False
    return True


def _aligned(value,count):
    if isinstance(value,str) and value:return [value]*count
    if isinstance(value,list) and len(value)==count and all(isinstance(x,str) and x for x in value):return value
    raise ValueError('invalid_source_alignment')


def public_contract_errors(public,runtime_contract=LEGACY_CONTRACT):
    """Validate actual CLI JSON while preserving the existing metadata wrapper."""
    errors=[]
    if not isinstance(public,dict):return ['public_not_object']
    if public.get('as_of_date')!='2026-10-01':errors.append('wrong_as_of_date')
    if public.get('answer_allowed') is not False or public.get('final_answer_generated') is not False:
        errors.append('approval_flags_changed')
    status=public.get('status');response=public.get('response')
    if status in ('out_of_scope','insufficient_evidence'):
        if response!={'answer':'확인 불가','citation':[]}:errors.append('unknown_must_have_exactly_two_fields')
        return errors
    if not _public_published(public,runtime_contract):
        if response is not None:errors.append('held_or_error_has_response')
        if status=='needs_clarification' and not public.get('missing_information'):errors.append('missing_clarification_facts')
        return errors
    if not isinstance(response,dict) or set(response)!={'answer','citation','source_url','effective_date'}:
        return errors+['normal_must_have_exactly_four_fields']
    if not isinstance(response['answer'],str) or not response['answer'].strip():errors.append('invalid_answer')
    labels=response['citation']
    if not isinstance(labels,list) or not labels or not all(isinstance(x,str) and x for x in labels):
        return errors+['invalid_citation_array']
    try:urls=_aligned(response['source_url'],len(labels));dates=_aligned(response['effective_date'],len(labels))
    except ValueError:return errors+['invalid_source_alignment']
    sources=public.get('sources')
    if not isinstance(sources,list) or len(sources)!=len(labels):return errors+['invalid_sources_alignment']
    for label,url,date,source in zip(labels,urls,dates,sources):
        if not isinstance(source,dict) or (source.get('citation'),source.get('source_url'),source.get('effective_date'))!=(label,url,date):
            errors.append('source_metadata_not_aligned')
        parsed=parse_citation(label)
        if parsed and parsed['article'] and parsed['paragraph'] is None and parsed['item'] is not None:
            errors.append('unexpected_public_item_without_paragraph')
        if not url.startswith('https://www.law.go.kr/'):errors.append('untrusted_source_url')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date) or date>'2026-10-01':errors.append('invalid_effective_date')
    if status=='scope_limited_draft' or (status=='code_checked_draft' and public.get('missing_materials')):
        if (public.get('fully_resolved') is not False or public.get('external_material_status')!='외부 자료 확인 필요'
                or not public.get('missing_materials') or public.get('answer_scope')!='ordinance_only'):
            errors.append('external_boundary_lost')
    if public.get('clarification_required') is True:
        facts=public.get('missing_information')
        if (not isinstance(facts,list) or not facts
                or not all(isinstance(fact,str) and fact.strip() for fact in facts)
                or public.get('fully_resolved') is not False
                or public.get('answer_scope')!='ordinance_only'):
            errors.append('clarification_boundary_lost')
    return list(dict.fromkeys(errors))


def check_case_output(case,public,audit,runtime_contract=LEGACY_CONTRACT):
    """Mechanical checks only; semantic review is deliberately a separate result."""
    errors=public_contract_errors(public,runtime_contract);result=audit.get('result',{})
    outcomes=execution_outcomes(case,runtime_contract)
    if not any(result.get('decision')==o['decision'] and _outcome_draft_status(result,runtime_contract)==o['draft_status']
               for o in outcomes):
        errors.append('unexpected_decision_or_draft_status')
    if result.get('draft_status')==WITHHELD_NO_PROVISION and not _withheld_no_provision_valid(result):
        errors.append('withheld_no_provision_invariant_failed')
    if case.get('requires_missing_information') and not public.get('missing_information'):
        errors.append('user_fact_gap_lost')
    response=public.get('response') or {};sources=public.get('sources',[])
    missing=[]
    if _public_published(public,runtime_contract):
        used_rows=used_citation_rows(result,runtime_contract)
        for expected in case.get('required_citations',[]):
            if expected.get('item') is not None and expected.get('paragraph') is None:
                if not any(row.get('locator',{}).get('article')==expected['article']
                           and row['locator'].get('paragraph') is None
                           and str(row['locator'].get('item'))==str(expected['item'])
                           and all(row.get(key)==expected[key] for key in ('ordin_seq','source_url','effective_date'))
                           for row in used_rows):
                    errors.append('missing_used_item_citation')
            if not any(matches_citation(s.get('citation'),expected) and s.get('source_url')==expected['source_url']
                       and s.get('effective_date')==expected['effective_date'] for s in sources):
                missing.append(expected)
        if missing:errors.append('missing_precise_source_citation')
        allowed={(a.get('source_url'),a.get('effective_date')) for a in case.get('source_anchors',[]) if a.get('source_url')}
        if allowed and any((s.get('source_url'),s.get('effective_date')) not in allowed for s in sources):
            errors.append('unexpected_source_identity_or_date')
        for pattern in case.get('answer_checks',[]):
            if not re.search(pattern,response.get('answer','')):errors.append('missing_answer_pattern:'+pattern)
        # A correct required citation cannot excuse fabricated hierarchy elsewhere.
        for expected in case.get('required_citations',[]):
            if expected.get('article') and expected.get('paragraph') is None:
                for source in sources:
                    parsed=parse_citation(source.get('citation'))
                    if (parsed and parsed['ordin_name']==re.sub(r'\s+','',expected['ordin_name'])
                            and parsed['article']==expected['article'] and parsed['paragraph'] is not None):
                        errors.append('invented_paragraph')
    elif case.get('required_citations'):
        errors.append('expected_answer_not_published')
    if result.get('answer_allowed') is not False or result.get('final_answer_generated') is not False:
        errors.append('audit_approval_flags_changed')
    if runtime_contract==SINGLE_RESPONSE_CONTRACT and _published(result,runtime_contract):
        errors.extend(single_response_consistency_errors(public,result))
    if not anchor_in_selected(case,audit,runtime_contract):errors.append('primary_anchor_not_selected')
    errors.extend(runtime_source_errors(audit,case.get('source_anchors',[])))
    return {'errors':list(dict.fromkeys(errors)),'missing_citations':missing,
            'semantic_review':'pending','correctness_constraints':case.get('correctness_constraints',[]),
            'runtime_contract':runtime_contract,'expected_execution_outcomes':outcomes,
            'anchor_evidence_meaning':('actual_claim_used_source' if _published(result,runtime_contract)
                else 'retrieved_exact_source_only' if runtime_contract==SINGLE_RESPONSE_CONTRACT else 'gate_selected_source')}


def single_response_consistency_errors(public,result):
    errors=[];claims=result.get('generated_claims',[])
    if result.get('code_checks',{}).get('status')!='passed' and not _released_valid(result):
        errors.append('operating_code_checks_not_passed')
    if not claims or not isinstance(claims,list):return errors+['operating_claims_missing']
    by_id={row['unit_id']:row for row in (result.get('draft_response') or {}).get('citation',[])}
    for claim in claims:
        if (not isinstance(claim,dict) or set(claim)!={'text','citation_ids'} or not isinstance(claim['text'],str)
                or not claim['text'] or not isinstance(claim['citation_ids'],list) or not claim['citation_ids']):
            return errors+['operating_claim_shape_changed']
        if (len(set(claim['citation_ids']))!=len(claim['citation_ids'])
                or any(uid not in by_id or by_id[uid].get('context_only') for uid in claim['citation_ids'])):
            errors.append('operating_claim_has_invalid_citation')
    checks=result.get('code_checks',{})
    if checks.get('numeric_citation_scope')=='public_selected_citations_v1':
        selected=sorted({uid for claim in claims for uid in claim['citation_ids']})
        if checks.get('numeric_citation_ids')!=selected:
            errors.append('numeric_citations_differ_from_public_selection')
    body=(result.get('draft_response') or {}).get('answer')
    if result.get('claim_joiner')!='\n\n' or '\n\n'.join(c['text'] for c in claims)!=body:
        errors.append('operating_claim_text_not_answer')
    if result.get('claim_segments')!={'claim_'+str(i+1):c['text'] for i,c in enumerate(claims)}:
        errors.append('operating_claim_segments_changed')
    expected=body
    if result.get('decision')=='external_material_required':
        notice=result.get('scope_notice',{}).get('text')
        if not isinstance(notice,str) or not notice:errors.append('operating_scope_notice_missing')
        elif isinstance(body,str):expected=notice+'\n\n'+body
        if (public.get('fully_resolved') is not False or public.get('external_material_status')!='외부 자료 확인 필요'
                or not public.get('missing_materials') or public.get('answer_scope')!='ordinance_only'):
            errors.append('external_boundary_lost')
    if result.get('decision')=='needs_clarification':
        from public_rag_response import CLARIFICATION_NOTICE
        if isinstance(body,str):expected=CLARIFICATION_NOTICE+'\n\n'+body
        if (public.get('clarification_required') is not True
                or public.get('missing_information')!=result.get('missing_information')
                or not public.get('missing_information') or public.get('fully_resolved') is not False):
            errors.append('clarification_boundary_lost')
    if (public.get('response') or {}).get('answer')!=expected:errors.append('public_answer_differs_from_operating_response')
    return errors


def generation_selection_response(result):
    rows=[row.get('parsed_response') for row in result.get('audit',[]) if row.get('stage')=='draft'
          and isinstance(row.get('parsed_response'),dict)]
    return rows[-1] if rows else None


CITATION_REPAIR_FIELDS={'claim_index','reference_text','claim_span','added_citation_ids','original_citation_ids','reason'}
CITATION_REPAIR_REASONS=('attributed_provision_cited_by_other_claim','attributed_provision_in_catalog',
                         'stated_item_cited_by_other_claim')


def _citation_repair_additions(result,citations):
    """claim index -> [(original_citation_ids, added ids)] from result.citation_repairs; None if malformed."""
    repairs=result.get('citation_repairs')
    if repairs is None or repairs==[]:return {}
    claims=result.get('generated_claims')
    if not isinstance(repairs,list) or not isinstance(claims,list):return None
    added={}
    for repair in repairs:
        if not isinstance(repair,dict) or set(repair)!=CITATION_REPAIR_FIELDS:return None
        index,ids,original=repair['claim_index'],repair['added_citation_ids'],repair['original_citation_ids']
        if (type(index) is not int or not 0<=index<len(claims) or repair['reason'] not in CITATION_REPAIR_REASONS
                or not isinstance(original,list) or not isinstance(ids,list) or not ids or len(set(ids))!=len(ids)
                or any(not isinstance(uid,str) or uid not in citations or citations[uid].get('context_only') for uid in ids)):
            return None
        added.setdefault(index,[]).append((original,ids))
    return added


# Stored runs use sequence aliases C001; address aliases carry the unit's own
# 조/항/호 numbers (Ca5.p1.i17, Ca12-3.p2.i7-3, CA.a5.i3 with a document letter).
CITATION_ALIAS_PATTERN=r'C(?:\d+|(?:[A-Z]+\.)?a[1-9]\d*(?:-[1-9]\d*)?(?:\.p[1-9]\d*)?(?:\.i[1-9]\d*(?:-[1-9]\d*)?)?)'


def source_selection_errors(result):
    """Check saved raw selections against materialized sources without rerunning generation."""
    raw=generation_selection_response(result)
    fields=('source_selection_contract','context_source_catalog','source_selection_trace')
    raw_selectors=isinstance(raw,dict) and any('context_selections' in row
        for key in ('claims','calculations') for row in raw.get(key,[]) if isinstance(row,dict))
    if not raw_selectors and not any(key in result for key in fields):return []
    if (result.get('source_selection_contract')!='structured_context_selection_v1'
            or not isinstance(result.get('context_source_catalog'),dict)
            or not isinstance(result.get('source_selection_trace'),dict)):
        return ['source_selection_contract_incomplete']
    try:
        catalog=result['context_source_catalog'];trace=result['source_selection_trace']
        originals={row['unit_id']:row for row in result.get('source_citations',[])}
        citations={row['unit_id']:row for row in (result.get('draft_response') or {}).get('citation',[])}
        aliases=result.get('citation_aliases',{})
        for handle,source in catalog.items():
            if (not re.fullmatch(r'H\d+',handle) or source!=originals.get(source['unit_id'])
                    or source.get('kind')!='required_context' or not source.get('source_segments')
                    or not source.get('source_unit_id') or not source.get('locator',{}).get('article')):
                return ['source_selection_catalog_changed']
            owner=originals.get(source.get('parent_chunk_id'))
            if (not owner or owner.get('kind')=='required_context' or any(
                    source.get(key) is None or source.get(key)!=owner.get(key)
                    for key in ('ordin_seq','source_url','effective_date','as_of_date'))):
                return ['source_selection_catalog_owner_changed']
            paragraphs=[]
            for segment in source['source_segments']:
                span=_exact_span(segment,source['quote'])
                if not span or source['quote'][span[0]:span[1]]!=segment.get('text'):
                    return ['source_selection_original_span_changed']
                if segment.get('kind')=='provision':
                    paragraph=segment.get('paragraph')
                    if paragraph is not None and (type(paragraph) is not int or paragraph<=0):
                        return ['source_selection_paragraph_invalid']
                    paragraphs.append(paragraph)
            if not paragraphs or len(paragraphs)!=len(set(paragraphs)):
                return ['source_selection_paragraph_invalid']
        if raw is None:
            if any(result.get('generated_'+key) or trace.get(key)!=[] for key in ('claims','calculations')):
                return ['source_selection_raw_response_missing']
            return []
        added=_citation_repair_additions(result,citations)
        if added is None:return ['citation_repair_invalid']
        # The receipt keeps the model's repeats; recompute the runtime merge on a
        # copy and require result.duplicate_citation_ids_merged to match it exactly.
        raw=copy.deepcopy(raw)
        recorded=result.get('duplicate_citation_ids_merged')
        merge_changed=(merge_duplicate_generated_citations(raw)!=(recorded or [])
                       or ('duplicate_citation_ids_merged' in result and not recorded))
        for key in ('claims','calculations'):
            generated=result.get('generated_'+key,[]);raw_rows=raw.get(key);traced=trace.get(key)
            if not isinstance(raw_rows,list) or not isinstance(traced,list) or not len(raw_rows)==len(generated)==len(traced):
                return ['source_selection_row_count_changed']
            for index,(initial,final,binding) in enumerate(zip(raw_rows,generated,traced)):
                # Only result.citation_repairs may extend a claim's reconstructed citations (appended in order).
                repaired=added.get(index,[]) if key=='claims' else []
                expect=lambda ids:list(dict.fromkeys(ids+[uid for _,uids in repaired for uid in uids])) if repaired and isinstance(ids,list) else ids
                if any(original!=binding.get('citation_ids') for original,_ in repaired):
                    return ['citation_repair_invalid']
                text_keys=('text',) if key=='claims' else ('expression','result')
                if any(initial.get(k)!=final.get(k) for k in text_keys):return ['source_selection_text_changed']
                direct=initial.get('citation_ids')
                # No H catalog means the new schema deliberately has no selector
                # field. With real H candidates its omission still fails.
                selections=initial.get('context_selections',[] if not catalog else None)
                if (merge_changed or not isinstance(direct,list) or len(set(direct))!=len(direct)
                        or not isinstance(selections,list) or binding.get('citation_aliases')!=direct
                        or binding.get('context_selections')!=selections
                        or expect(binding.get('citation_ids'))!=final.get('citation_ids')):
                    return ['source_selection_trace_changed']
                resolved=binding.get('resolved_context_citations')
                if not isinstance(resolved,list) or len(resolved)!=len(selections):return ['source_selection_trace_changed']
                combined=[];seen=set()
                for alias in direct:
                    if not isinstance(alias,str) or not re.fullmatch(CITATION_ALIAS_PATTERN,alias) or alias not in aliases:
                        return ['source_selection_direct_alias_changed']
                    combined.append(aliases[alias])
                for selector,ids in zip(selections,resolved):
                    if (not isinstance(selector,dict) or set(selector)!={'context_id','paragraph','item'}
                            or not all(isinstance(selector[k],str) for k in selector)):
                        return ['source_selection_selector_invalid']
                    identity=tuple(selector[k] for k in ('context_id','paragraph','item'))
                    if identity in seen:return ['source_selection_duplicate_selector']
                    seen.add(identity);source=catalog.get(selector['context_id'])
                    if source is None:return ['source_selection_unknown_context']
                    paragraphs=[segment for segment in source['source_segments'] if segment.get('kind')=='provision'
                        and ('' if segment.get('paragraph') is None else str(segment['paragraph']))==selector['paragraph']]
                    if len(paragraphs)!=1:return ['source_selection_paragraph_invalid']
                    segment=paragraphs[0];start,end=segment['start'],segment['end'];text=segment['text']
                    if source['quote'][start:end]!=text:return ['source_selection_original_span_changed']
                    # Validate boundaries in the already structured source; do not invent a legal relationship.
                    markers=list(re.finditer(r'(?m)^[ \t]*([1-9]\d*(?:의[1-9]\d*)?)\.[ \t]+',text))
                    intro=(start,start+markers[0].start(),None) if markers and text[:markers[0].start()].strip() else None
                    if selector['item']:
                        matches=[(i,m) for i,m in enumerate(markers) if m[1]==selector['item']]
                        if len(matches)!=1:return ['source_selection_item_invalid']
                        index,marker=matches[0];right=markers[index+1].start() if index+1<len(markers) else len(text)
                        expected=([intro] if intro else [])+[(start+marker.start(),start+right,selector['item'])]
                    else:expected=([intro] if intro else []) if markers else [(start,end,None)]
                    if not expected or not isinstance(ids,list) or len(ids)!=len(expected):return ['source_selection_closure_changed']
                    for uid,(left,right,item) in zip(ids,expected):
                        child=citations.get(uid,{})
                        if (child.get('context_only') or child.get('source_origin_id')!=source['unit_id']
                                or child.get('source_quote_span')!={'start':left,'end':right}
                                or child.get('quote')!=source['quote'][left:right]
                                or any(child.get(k)!=source.get(k) for k in
                                       ('parent_chunk_id','source_unit_id','ordin_seq','source_url','effective_date','as_of_date'))
                                or child.get('locator',{}).get('article')!=source.get('locator',{}).get('article')
                                or child.get('locator',{}).get('paragraph')!=segment.get('paragraph')
                                or child.get('locator',{}).get('item')!=item):
                            return ['source_selection_materialized_source_changed']
                    combined.extend(ids)
                if (expect(list(dict.fromkeys(combined)))!=final.get('citation_ids')
                        or any(uid not in citations or citations[uid].get('context_only') for uid in combined)):
                    return ['source_selection_final_citations_changed']
        # A merge record on a draft with no rows is only checked here (row checks report it first otherwise).
        if merge_changed:return ['source_selection_trace_changed']
    except (KeyError,TypeError,ValueError,IndexError):
        return ['source_selection_invalid_structure']
    return []


def _reference_tuple(text):
    match=re.fullmatch(r'제\s*([1-9]\d*)\s*조(?:\s*의\s*([1-9]\d*))?(?:\s*제\s*([1-9]\d*)\s*항)?',text)
    if not match:
        # Source titles sometimes drop "조" ("제21제2항"); accepted only with a paragraph.
        typo=re.fullmatch(r'제\s*([1-9]\d*)\s*제\s*([1-9]\d*)\s*항',text)
        return ('제'+typo[1]+'조',int(typo[2])) if typo else None
    return ('제'+match[1]+'조'+('의'+match[2] if match[2] else ''),int(match[3]) if match[3] else None)


def locator_proof_errors(result):
    """A title reference proves only its exact claim span, never an amount or calculation."""
    proofs=result.get('code_checks',{}).get('locator_proofs',[])
    if not isinstance(proofs,list):return ['locator_proofs_invalid']
    originals={r['unit_id']:r for r in result.get('source_citations',[])}
    citations={r['unit_id']:r for r in (result.get('draft_response') or {}).get('citation',[])}
    try:
        for proof in proofs:
            index=proof['claim_index'];claims=result.get('generated_claims',[])
            if type(index) is not int or not 0<=index<len(claims):return ['locator_proof_claim_invalid']
            claim=claims[index];span=_exact_span(proof.get('claim_span'),claim['text'])
            source=originals.get(proof.get('source_context_id'),{});quote=source.get('quote','')
            source_span=_exact_span(proof.get('source_quote_span'),quote)
            if not span or not source_span:return ['locator_proof_span_invalid']
            reference=claim['text'][span[0]:span[1]];source_reference=quote[source_span[0]:source_span[1]]
            legal=_reference_tuple(reference)
            if (not legal or legal!=_reference_tuple(source_reference)
                    or legal!=(proof.get('article'),proof.get('paragraph')) or reference!=proof.get('reference_text')
                    or not re.match(r'\s*관련',claim['text'][span[1]:]) or not re.match(r'\s*관련',quote[source_span[1]:])
                    or proof.get('source_quote_sha256')!=digest(quote.encode('utf-8'))):
                return ['locator_proof_reference_changed']
            normalized='조' not in reference or '조' not in source_reference
            if proof.get('normalized',None)!=('missing_article_suffix' if normalized else None) or (
                    not normalized and 'normalized' in proof):
                return ['locator_proof_reference_changed']
            lines=[];offset=0
            for line in quote.splitlines(keepends=True):
                if line.strip():lines.append((offset,offset+len(line),line))
                offset+=len(line)
            kind=source.get('title_context_kind')
            title=(kind=='preceding_table_heading' and len(lines)==1
                   or kind=='ordered_attachment_text_across_pages' and lines
                   and re.search(r'\[\s*별\s*표(?:\s*\d+(?:\s*의\s*\d+)?)?\s*\]',lines[0][2]))
            allowed=lines if kind=='preceding_table_heading' else lines[:2]
            if not title or not any(left<=source_span[0]<source_span[1]<=right for left,right,_ in allowed):
                return ['locator_proof_not_a_title']
            if not any(entry.get('source_quote_span')==proof['source_quote_span']
                       and entry.get('reference_text')==source_reference
                       and (entry.get('article'),entry.get('paragraph'))==legal
                       for entry in source.get('title_locator_spans',[])):
                return ['locator_proof_unregistered_title_span']
            cid=proof.get('attachment_citation_id');row=citations.get(cid,{})
            owner=originals.get(row.get('parent_chunk_id'),{})
            if (cid not in claim['citation_ids'] or source.get('kind')!='required_context'
                    or row.get('kind')!='attachment' or owner.get('kind')!='attachment'):
                return ['locator_proof_uncited_attachment']
            if any(row.get(k)!=source.get(k) for k in
                   ('parent_chunk_id','ordin_seq','source_url','effective_date','as_of_date')) or any(
                   row.get(k)!=owner.get(k) for k in ('ordin_seq','source_url','effective_date','as_of_date')):
                return ['locator_proof_source_identity_changed']
            row_refs=row.get('locator',{}).get('source_refs',[]);source_refs=source.get('locator',{}).get('source_refs',[])
            aid=row.get('locator',{}).get('attachment_id')
            if not aid:
                ids={ref.get('attachment_id') for ref in row_refs}
                aid=next(iter(ids)) if len(ids)==1 else None
            captures={(ref.get('parse_file'),ref.get('parse_content_hash')) for ref in row_refs}
            if (not aid or not row_refs or not source_refs
                    or any(ref.get('attachment_id')!=aid for ref in row_refs+source_refs)
                    or any((ref.get('parse_file'),ref.get('parse_content_hash')) not in captures for ref in source_refs)
                    or any(not file or not hash_value for file,hash_value in captures)):
                return ['locator_proof_attachment_capture_changed']
    except (KeyError,TypeError,ValueError,IndexError):return ['locator_proofs_invalid']
    return []


def metadata_proof_errors(result):
    """Independently bind typed labels to claim spans and their original sources."""
    checks=result.get('code_checks',{})
    if not any(key in checks for key in ('metadata_proof_contract','metadata_proofs')):return []
    if (checks.get('metadata_proof_contract')!='typed_source_metadata_spans_v1'
            or not isinstance(checks.get('metadata_proofs'),list)):
        return ['metadata_proof_contract_invalid']
    fields=('ordin_seq','source_url','effective_date','as_of_date')
    base={'kind','claim_index','claim_span','reference_text'}
    annex=re.compile(r'(?P<label>별\s*표\s*[1-9]\d*(?:\s*의\s*[1-9]\d*)?)'
                     r'(?=\s|[()\[\]「」,.;:]|의(?!\s*\d)|은|는|을|를|에|$)')
    date=re.compile(r'\([ \t]*(?P<label>2026-10-01[ \t]+기준)[ \t]*\)')
    try:
        original_rows=result.get('source_citations',[])
        citation_rows=(result.get('draft_response') or {}).get('citation',[])
        originals={row['unit_id']:row for row in original_rows}
        citations={row['unit_id']:row for row in citation_rows}
        if len(originals)!=len(original_rows) or len(citations)!=len(citation_rows):
            return ['metadata_proof_duplicate_source']
        occupied={}

        def binding(uid):
            row=citations.get(uid,{})
            owner=originals.get(row.get('parent_chunk_id'),{})
            metadata={key:row.get(key) for key in fields}
            if (not row or row.get('context_only') or not owner
                    or owner.get('kind')=='required_context'
                    or any(not value or owner.get(key)!=value for key,value in metadata.items())):
                return None
            # A child label still belongs to the exact original cited span.
            origin=originals.get(row.get('source_origin_id') or uid,{})
            span=_exact_span(row.get('source_quote_span'),origin.get('quote',''))
            if (not origin or any(origin.get(key)!=value for key,value in metadata.items())
                    or origin.get('parent_chunk_id')!=owner['unit_id']
                    or (row.get('source_origin_id') and
                        (not span or origin['quote'][span[0]:span[1]]!=row.get('quote')))
                    or (not row.get('source_origin_id') and row.get('quote')!=origin.get('quote'))):
                return None
            return {'citation_id':uid,'owner_primary_id':owner['unit_id'],'source_metadata':metadata}

        for proof in checks['metadata_proofs']:
            kind=proof.get('kind')
            expected=(base|{'metadata_field','metadata_value','source_bindings'} if kind=='as_of_date'
                else base|{'citation_id','owner_primary_id','attachment_id','source_context_id',
                          'source_quote_span','source_quote_sha256','source_metadata'} if kind=='attachment_label' else None)
            if expected is None:return ['metadata_proof_unsupported_kind']
            if set(proof)!=expected:return ['metadata_proof_shape_changed']
            index=proof['claim_index'];claims=result.get('generated_claims',[])
            if type(index) is not int or not 0<=index<len(claims):return ['metadata_proof_claim_invalid']
            claim=claims[index];text=claim['text'];span=_exact_span(proof['claim_span'],text)
            if not span or text[span[0]:span[1]]!=proof['reference_text']:
                return ['metadata_proof_claim_span_changed']
            if any(max(span[0],left)<min(span[1],right) for left,right in occupied.get(index,[])):
                return ['metadata_proof_duplicate_or_overlapping_span']
            occupied.setdefault(index,[]).append(span)
            ids=claim['citation_ids']
            if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)):
                return ['metadata_proof_claim_citations_invalid']
            if kind=='as_of_date':
                if (proof['metadata_field']!='as_of_date' or proof['metadata_value']!='2026-10-01'
                        or re.search(r'시행|공포|개정|폐지|발효|적용일',text)
                        or not any(match.span('label')==span for match in date.finditer(text))):
                    return ['metadata_proof_asof_not_platform_label']
                bindings=[binding(uid) for uid in ids]
                if (any(value is None or value['source_metadata']['as_of_date']!='2026-10-01' for value in bindings)
                        or proof['source_bindings']!=bindings):
                    return ['metadata_proof_asof_source_binding_changed']
                continue
            if not any(match.span('label')==span for match in annex.finditer(text)):
                return ['metadata_proof_attachment_not_label']
            uid=proof['citation_id'];bound=binding(uid)
            if (uid not in ids or bound is None or proof['owner_primary_id']!=bound['owner_primary_id']
                    or proof['source_metadata']!=bound['source_metadata']):
                return ['metadata_proof_attachment_source_binding_changed']
            row=citations[uid];owner=originals[bound['owner_primary_id']]
            source=originals.get(proof['source_context_id'],{})
            if (owner.get('kind')!='attachment' or row.get('kind') not in ('attachment','required_context')
                    or source.get('kind')!='required_context' or source.get('parent_chunk_id')!=owner['unit_id']
                    or source.get('title_context_kind') not in ('ordered_attachment_text_across_pages','preceding_table_heading')
                    or any(source.get(key)!=value for key,value in bound['source_metadata'].items())):
                return ['metadata_proof_attachment_heading_identity_changed']
            aid=owner.get('locator',{}).get('attachment_id')
            refs=owner.get('locator',{}).get('source_refs',[])
            captures={(ref.get('parse_file'),ref.get('parse_content_hash')) for ref in refs}
            row_refs=row.get('locator',{}).get('source_refs',[])
            source_refs=source.get('locator',{}).get('source_refs',[])
            if (not aid or aid!=proof['attachment_id'] or not refs or not row_refs or not source_refs
                    or any(not path or not value for path,value in captures)
                    or any(ref.get('attachment_id')!=aid for ref in refs+row_refs+source_refs)
                    or any((ref.get('parse_file'),ref.get('parse_content_hash')) not in captures
                           for ref in row_refs+source_refs)):
                return ['metadata_proof_attachment_capture_changed']
            quote=source['quote'];source_span=_exact_span(proof['source_quote_span'],quote)
            first=re.search(r'(?m)^.*\S.*$',quote)
            if (not source_span or not first or proof['source_quote_sha256']!=digest(quote.encode('utf-8'))):
                return ['metadata_proof_heading_span_changed']
            wanted=re.sub(r'\s+','',proof['reference_text'])
            matches=list(re.finditer(r'\[[ \t]*(?P<label>별[ \t]*표[ \t]*[1-9]\d*'
                r'(?:[ \t]*의[ \t]*[1-9]\d*)?)[ \t]*\]',first[0]))
            if len(matches)!=1 or not any((first.start()+match.start('label'),first.start()+match.end('label'))==source_span
                       and re.sub(r'\s+','',match['label'])==wanted for match in matches):
                return ['metadata_proof_heading_label_changed']
    except (KeyError,TypeError,ValueError,IndexError,AttributeError):
        return ['metadata_proofs_invalid']
    return []


def runtime_source_errors(audit,anchors):
    """Verify child source spans independently from the answer implementation."""
    result=audit.get('result',{})
    errors=(source_selection_errors(result)+locator_proof_errors(result)+metadata_proof_errors(result)
            +body_context_provenance_errors(result))
    if result.get('citation_contract')!='exact_source_subunits_v1':return errors
    originals={r['unit_id']:r for r in result.get('source_citations',[])}
    anchor_map={a['chunk_id']:a for a in anchors}
    for origin_id,original in originals.items():
        if origin_id in anchor_map and original.get('quote')!=anchor_map[origin_id]['quote']:
            errors.append('runtime_parent_changed_from_database_anchor')
        proof=original.get('body_context_provenance',{})
        if isinstance(proof,dict) and proof.get('origin_id') in anchor_map:
            if not _body_context_matches_anchor(original,anchor_map[proof['origin_id']]):
                errors.append('runtime_body_context_changed_from_database_anchor')
    for child in (result.get('draft_response') or {}).get('citation',[]):
        origin_id=child.get('source_origin_id')
        if not origin_id:continue
        original=originals.get(origin_id);span=child.get('source_quote_span',{})
        start,end=span.get('start'),span.get('end')
        if (original is None or type(start) is not int or type(end) is not int
                or not 0<=start<end<=len(original.get('quote',''))
                or original['quote'][start:end]!=child.get('quote')):
            errors.append('runtime_child_source_span_mismatch');continue
        for key in ('ordin_seq','source_url','effective_date','parent_chunk_id'):
            if child.get(key)!=original.get(key):errors.append('runtime_child_owner_or_identity_changed')
        locator=child.get('locator',{});item=locator.get('item')
        if item is not None:
            printed=re.match(r'\s*(\d+(?:의\d+)?)\.\s',child['quote'])
            if not printed or printed[1]!=str(item):errors.append('runtime_child_item_number_mismatch')
        if original.get('kind')=='body' and locator.get('paragraph')!=original.get('locator',{}).get('paragraph'):
            errors.append('runtime_child_paragraph_changed')
        segments=original.get('source_segments',[])
        mandatory=(child.get('context_only') is True and re.search(r'::mandatory::\d+$',child.get('unit_id','')))
        valid_mandatory=mandatory and any(s.get('kind')!='provision' and s.get('start')==start and s.get('end')==end
                                         and s.get('text')==child['quote'] for s in segments)
        valid_provision=any(s.get('kind')=='provision' and s.get('start',-1)<=start and s.get('end',-1)>=end
                            and s.get('paragraph')==locator.get('paragraph') for s in segments)
        if segments and not (valid_mandatory or valid_provision):
            errors.append('runtime_context_paragraph_span_mismatch')
    return list(dict.fromkeys(errors))


def read_cli_json(project,question,python_executable=None):
    """Read cached data through the real CLI. --check-only guarantees no API calls."""
    completed=subprocess.run([str(python_executable or sys.executable),'-B','-X','utf8',str(Path(project)/'run.py'),
                              'ask','--question',question,'--check-only','--json'],cwd=project,
                             capture_output=True,text=True,encoding='utf-8',timeout=60,check=False)
    try:value=json.loads(completed.stdout)
    except (ValueError,TypeError):raise ValueError('cli_stdout_not_single_json') from None
    if not isinstance(value,dict) or value.get('called')!=0:raise ValueError('cli_check_only_called_api')
    if completed.returncode not in (0,1):raise ValueError('cli_unexpected_exit_code')
    expected_code=1 if value.get('status') in ('processing_error','verification_failed','code_check_failed') else 0
    if completed.returncode!=expected_code:raise ValueError('cli_status_exit_code_mismatch')
    return value


def evaluate(project,cases_file,check_only=False,verify_cli=False,case_ids=None,
             runtime_contract=SINGLE_RESPONSE_CONTRACT,development_review=False):
    if runtime_contract not in (LEGACY_CONTRACT,SINGLE_RESPONSE_CONTRACT):raise ValueError('unknown_runtime_contract')
    if development_review and runtime_contract!=SINGLE_RESPONSE_CONTRACT:raise ValueError('development_review_requires_single_response')
    project=Path(project).resolve(strict=True)
    fixture,fixture_hash=validate_cases(project,cases_file)
    if case_ids:
        requested=set(case_ids)
        if requested-{c['id'] for c in fixture['cases']}:raise ValueError('unknown_evaluation_case_id')
        fixture={**fixture,'cases':[c for c in fixture['cases'] if c['id'] in requested]}
    first=run_question(project,fixture['cases'][0]['question'],check_only=True)
    if first['status']=='processing_error':raise ValueError('evaluation_preflight_failed')
    identity=digest(encoded({'cases_sha256':fixture_hash,'code':first['code']['sha256'],
                             'evaluation_code':digest(Path(__file__).read_bytes()),
                             'case_ids':[c['id'] for c in fixture['cases']], 'verify_cli':verify_cli,
                             'runtime_contract':runtime_contract,'development_review':development_review,
                             'development_review_code':digest((Path(__file__).parent/'development_rag_review.py').read_bytes())
                                 if development_review else None}))
    folder=project/'data/processed/local_rag/2026-10-01/expanded_evaluations'/identity[:20]
    report={'schema_version':'expanded-local-evaluation-v1','identity':identity,'cases_sha256':fixture_hash,
            'database_sha256':fixture['database_sha256'],'pipeline_code':first['code'],
            'as_of_date':'2026-10-01','case_count':len(fixture['cases']),'cases':[],
            'called':0,'answer_allowed':False,'new_passage_calls':0,'status':'running',
            'independent_semantic_review':'pending','output_file':str(folder/'report.json')}
    report['actual_cli_checked']=verify_cli
    report.update(runtime_contract=runtime_contract,development_review_enabled=development_review,
                  operating_called=0,development_called=0,development_passed_count=0,
                  operating_and_development_reviews_are_separate=True)
    report['case_timing_meaning']='elapsed_seconds includes operating response, CLI/local checks, and explicit development review when enabled; operating_elapsed_seconds excludes development review; called counts operating API calls including a query embedding when needed'
    report['cohort_counts']={name:sum(c.get('cohort','legacy')==name for c in fixture['cases'])
                             for name in sorted({c.get('cohort','legacy') for c in fixture['cases']})}
    started=time.monotonic()
    for case in fixture['cases']:
        print(json.dumps({'event':'case_start','id':case['id']},ensure_ascii=False),flush=True)
        case_started=time.monotonic()
        audit=run_question(project,case['question'],check_only=check_only)
        result=audit.get('result',{})
        public_error=None
        try:
            public=read_cli_json(project,case['question']) if verify_cli else public_result(audit,project)
            if verify_cli:
                expected_public=public_result(audit,project)
                if ({k:v for k,v in public.items() if k!='called'}!=
                        {k:v for k,v in expected_public.items() if k!='called'}):
                    public_error='cli_response_differs_from_evaluated_audit'
        except (ValueError,KeyError,TypeError,OSError,subprocess.TimeoutExpired):
            public={};public_error='public_contract_failed'
        checks=check_case_output(case,public,audit,runtime_contract)
        passed=audit.get('status')=='completed' and public_error is None and not checks['errors']
        operating_elapsed=round(time.monotonic()-case_started,2)
        review=None
        if development_review:
            if audit.get('status')=='completed' and public_error is None:
                from development_rag_review import review_saved_audit
                try:review=review_saved_audit(project,audit,public,check_only=check_only)
                except (ValueError,RuntimeError,KeyError,TypeError,OSError) as exc:
                    review={'status':'processing_error','passed':False,'called':0,
                            'error_code':str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__}
            else:review={'status':'not_run','passed':False,'called':0,'reason':'operating_response_unavailable'}
        item={'case_id':case['id'],'question':case['question'],'expected_decision':case['expected_decision'],
              'expected_draft_status':case['expected_draft_status'],'decision':result.get('decision'),
              'draft_status':result.get('draft_status'),'passed_automated_checks':passed,
              'called':audit.get('called',0),'audit_path':audit.get('output_paths',{}).get('audit'),
              'public_result':public,'error_code':public_error or result.get('error_code') or audit.get('error'),
              'independent_semantic_review':'pending','cohort':case.get('cohort','legacy'),
              'contract_and_content_checks':checks,'elapsed_seconds':round(time.monotonic()-case_started,2)}
        item.update(runtime_contract=runtime_contract,operating_elapsed_seconds=operating_elapsed,
                    development_review=review,development_called=review.get('called',0) if review else 0,
                    development_elapsed_seconds=review.get('elapsed_seconds') if review else None,
                    development_review_passed=review.get('passed') if review else None)
        report['cases'].append(item);report['called']+=item['called']
        report['operating_called']+=item['called'];report['development_called']+=item['development_called']
        report['development_passed_count']=sum(c['development_review_passed'] is True for c in report['cases'])
        report['total_called']=report['operating_called']+report['development_called']
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        report['passed_count']=sum(c['passed_automated_checks'] for c in report['cases'])
        report['cohorts']={name:{'case_count':sum(c['cohort']==name for c in report['cases']),
                                  'passed_automated_checks':sum(c['cohort']==name and c['passed_automated_checks'] for c in report['cases'])}
                            for name in report['cohort_counts']}
        if not check_only:
            folder.mkdir(parents=True,exist_ok=True)
            (folder/'report.json').write_bytes(encoded(report))
        print(json.dumps({k:item[k] for k in ['case_id','decision','draft_status','passed_automated_checks','called','elapsed_seconds','error_code']},ensure_ascii=False),flush=True)
        if not check_only and (audit.get('status')=='processing_error' or result.get('error_code') in
                              ('gate_request_failed','draft_request_failed','verify_request_failed')):
            report['status']='stopped_on_processing_error';break
    else:report['status']='checked' if check_only else 'completed'
    report['elapsed_seconds']=round(time.monotonic()-started,2)
    if not check_only:
        (folder/'report.json').write_bytes(encoded(report))
        (folder/'report.manifest.json').write_bytes(encoded({'identity':identity,'report_sha256':digest(encoded(report)),
                                                            'answer_allowed':False}))
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--cases',type=Path)
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--verify-cli',action='store_true',help='check cached actual run.py --json output without API calls')
    parser.add_argument('--case-id',action='append',help='run only declared IDs; omitted cases remain unevaluated')
    parser.add_argument('--runtime-contract',choices=(LEGACY_CONTRACT,SINGLE_RESPONSE_CONTRACT),default=SINGLE_RESPONSE_CONTRACT,
                        help='explicit status adapter; fixed legal expectations remain unchanged')
    parser.add_argument('--development-review',action='store_true',help='explicit Solar gate/verify review of the same operating response')
    args=parser.parse_args(argv)
    cases=args.cases or args.project/'data/processed/local_rag/2026-10-01/prerelease/expanded_cases.json'
    result=evaluate(args.project,cases,args.check_only,args.verify_cli,args.case_id,args.runtime_contract,args.development_review)
    print(json.dumps({k:v for k,v in result.items() if k!='cases'},ensure_ascii=False,indent=2))
    passed=result['passed_count']==result['case_count']
    if args.development_review:passed=passed and result['development_passed_count']==result['case_count']
    return 0 if passed else 2


if __name__=='__main__':raise SystemExit(main())
