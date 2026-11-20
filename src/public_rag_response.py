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
SOURCE_SCOPE_NOTICES = {
    'partial_allowance': '구체적인 지급 조건은 해당 사업의 지급 기준을 확인해야 합니다.',
    'delegated_fee': '정확한 금액은 해당 기준의 원문을 확인해야 합니다.',
}
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


def scope_boundary_notice(missing_materials, missing_information, *, allowance=False, source_kind=None):
    """Code-owned processing state, never a substitute for ordinance evidence."""
    if (not isinstance(missing_materials, list) or not missing_materials or
            not all(isinstance(value, str) and value.strip() for value in missing_materials)):
        raise ValueError('scope_limited_draft_without_external_boundary')
    if (not isinstance(missing_information, list) or
            not all(isinstance(value, str) and value.strip() for value in missing_information)):
        raise ValueError('invalid_scoped_missing_facts')
    notice = SOURCE_SCOPE_NOTICES.get('partial_allowance' if allowance else source_kind, ORDINANCE_SCOPE_NOTICE)
    return {'provenance': 'processing_status', 'text': notice,
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


def _confirmed_withheld_absence(result):
    """Accept the typed NONE verdict only when the completed check receipt agrees."""
    checks = result.get('code_checks')
    if not isinstance(checks, dict):
        return False
    checks_ok = checks.get('status') == 'passed' and checks.get('issues') == []
    adjustment = result.get('decision_adjustment')
    verdict = result.get('provision_check')
    if (not checks_ok or result.get('schema_version') != 'local-rag-single-generation-v1'
            or result.get('draft_generated') is not True or result.get('decision') != 'insufficient_evidence'
            or result.get('answer_allowed') is not False or result.get('final_answer_generated') is not False
            or not isinstance(adjustment, dict) or set(adjustment) != {'from', 'to', 'reason'}
            or adjustment['from'] not in ('supported', 'external_material_required', 'needs_clarification')
            or adjustment['to'] != 'insufficient_evidence' or adjustment['reason'] != 'provision_check_no_provision'
            or not isinstance(verdict, dict) or set(verdict) != {'asked_matter', 'provision_id', 'basis'}
            or not isinstance(verdict['asked_matter'], str) or not verdict['asked_matter'].strip()
            or verdict['provision_id'] != 'NONE' or verdict['basis'] != 'no_provision'):
        return False
    stages = result.get('audit')
    if not isinstance(stages, list) or not stages or not all(isinstance(row, dict) for row in stages):
        return False
    receipt = stages[-1]
    return (sum(row.get('stage') == 'provision_check' for row in stages) == 1
            and receipt.get('stage') == 'provision_check' and receipt.get('parsed_response') == verdict
            and isinstance(receipt.get('response'), dict)
            and not any(key in receipt for key in ('error_type', 'validation_error', 'deadline',
                                                   'prior_attempt', 'rate_limit', 'purpose')))


_HOURS_QUESTION = re.compile(
    r"(?:(?:서울특별시|서울시|서울)(?:(?:가|에서) (?:설치한|운영하는)|의)? )?"
    r"(?P<facility>[가-힣]+?(?: [가-힣]+?){0,4}?)"
    r"(?:(?:에|를|을) (?:(?:아침|오전|오후|낮|저녁)에 )?(?:가|방문하|이용하)려고 (?:합니다|해요)\. "
    r"(?:(?:조례상|조례에서) )?|(?:에서는|에서|의|은|는|이|가)? )"
    r"(?:(?:운영|개방|이용|개장|폐장|문(?:을)? (?:여는|닫는))\s*시간(?:은|이)? "
    r"(?:어떻게 (?:되나요|되죠|됩니까|돼요)|정해져 있(?:나요|습니까)|언제(?:인가요|예요)|몇 시인가요)"
    r"|몇 시(?:부터 몇 시까지|부터|까지|에) (?:(?:운영|개방)(?:하나요|합니까|되나요|해요|돼요)"
    r"|문(?:을)? (?:여나요|닫나요|열어요|닫아요)))[?？.]?"
)
_OPERATING_RULE = re.compile(
    r"(?:그 밖에 )?(?P<facilities>[가-힣 ]+(?:[ㆍ·][가-힣 ]+)*)의 "
    r"(?:(?:[가-힣]{2,8})(?: 및 |, |ㆍ|·))*"
    r"(?:운영|개방)(?:방법|시간| 기준| 방법|에 필요한 사항)(?: 등)?(?:은|을) "
    r"(?:[가-힣 ]+에 준하여 )?[가-힣]+(?:이|가) 따로 정(?:한다|할 수 있다)\."
)
_SPECIFIC_HOURS = re.compile(r"\d+\s*시(?!설)|[가-힣]+\s*시부터|(?:개방|운영|이용)\s*시간|개장|폐장|휴관|휴무|"
                             r"일출|일몰|오전|오후|야간|주간|연중무휴|상시 개방|항상 개방|"
                             r"(?<!\d)\d{1,2}:\d{2}(?!\d)|휴일|주말|휴장|휴식일|입장\s*(?:시간|마감)")
_ACTOR = r"(?:시장|구청장|군수|도지사)"
_HOURS_TERM = r"(?:운영|개방|이용)(?: (?:시작|종료))?\s*(?:시간|시각)"
_HOURS_AND_INSTALLATION = re.compile(
    rf"(?:(?:서울시|서울특별시|서울) )?(?P<facility>[가-힣]+(?: [가-힣]+){{0,4}}?)(?:의|은|는) "
    rf"(?:(?:{_HOURS_TERM}(?:과|와) 설치 주체|설치 주체(?:와| 및) {_HOURS_TERM})(?:를|을)? "
    rf"(?:(?:함께|같이) )?(?:알려 ?주세요|궁금합니다)|"
    rf"(?P<actor>{_ACTOR})(?:도|이|가) 설치할 수 (?:있나요|있습니까)\? "
    rf"{_HOURS_TERM}(?:도|이|은)? 조례에 (?:나오나요|정해져 있나요))"
    r"[?.？]?"
)


def _installation_rule(facility, actor, hours, originals):
    """Keep the entire single operative sentence, including any stated purpose."""
    pattern = re.compile(rf"(?P<actors>{_ACTOR}(?: 또는 {_ACTOR})*)[은는] "
                         rf"(?:[^.?!\n]+?(?:위해|위하여) )?{re.escape(facility)}[을를] "
                         r"설치(?:[·ㆍ ]운영)?(?:할 수 있다|하여야 한다|한다)\.")
    matches = []
    for row in originals:
        if (row['kind'] != 'body' or row['unit_id'] != row['parent_chunk_id'] or row.get('context_only')
                or row['locator'].get('article') != hours['locator'].get('article')
                or any(row.get(k) != hours.get(k) for k in ('ordin_seq','source_url','effective_date'))):
            continue
        text = _paragraph_wording(row)
        if match := pattern.fullmatch(text):
            if not actor or actor in match['actors'].split(' 또는 '):
                matches.append((text, row))
    return matches[0] if len(matches) == 1 else None


def _delegated_hours(question, result, selected):
    """Publish the ordinance-wide answer even when the individual facility's hours are unknown."""
    check = result.get('provision_check') or {}
    if (result.get('draft_status') != 'code_checked_draft'
            or result.get('decision') not in ('supported', 'external_material_required', 'needs_clarification')
            or result.get('generated_calculations') or result.get('error_code')):
        return None
    receipts = result.get('audit') or []
    if (not isinstance(receipts, list) or not receipts or not isinstance(receipts[-1], dict)
            or not isinstance(check, dict) or check.get('basis') not in ('states_content', 'delegates_elsewhere')):
        return None
    last = receipts[-1]
    if (last.get('stage') != 'provision_check' or last.get('parsed_response') != check
            or not isinstance(last.get('response'), dict)
            or any(k in last for k in ('deadline','validation_error','error_type','rate_limit','prior_attempt','purpose'))):
        return None
    uid = result.get('citation_aliases', {}).get(check.get('provision_id'))
    row = next((r for r in selected if r['unit_id'] == uid and r['kind'] == 'body'), None)
    if not row or any(any(r.get(k) != row.get(k) for k in ('ordin_seq', 'source_url', 'effective_date')) for r in selected):
        return None
    originals = result.get('source_citations', [])
    combined = _HOURS_AND_INSTALLATION.fullmatch(" ".join(question.split()))
    if combined:
        rule = _facility_hours_rule(combined['facility'], originals, originals)
    else:
        rule = operating_hours_evidence(question, originals, originals)
    if not rule:
        return None
    text, hours = rule
    parts = [hours]
    accepted = {('delegates_elsewhere', hours['unit_id'])}
    if combined:
        grant = _installation_rule(combined['facility'], combined['actor'], hours, originals)
        if not grant:
            return None
        # The named actor and delegated hours are two separate propositions.
        # Both full source units must already be owned by the checked claims.
        parts.append(grant[1])
        accepted.add(('states_content', grant[1]['unit_id']))
        text += " " + re.sub(r"있다\.$|한다\.$", lambda m: "있습니다." if m[0] == "있다." else "합니다.", grant[0])
    selected_by_id = {r['unit_id']: r for r in selected}
    if ((check.get('basis'), uid) not in accepted or any(
            original['unit_id'] not in selected_by_id or any(
                selected_by_id[original['unit_id']].get(k) != original.get(k)
                for k in ('parent_chunk_id','ordin_seq','source_url','effective_date','quote','locator'))
            for original in parts)):
        return None
    return text + " 실제 운영 시각은 해당 시설 안내에서 확인해 주세요.", parts


def operating_hours_evidence(question, candidates, originals):
    """Find one unconditional operating rule for the single facility and detail requested."""
    query = _HOURS_QUESTION.fullmatch(" ".join(question.split()))
    if not query or not originals:
        return None
    return _facility_hours_rule(query['facility'], candidates, originals)


def _facility_hours_rule(facility, candidates, originals, *, use_owner_context=False):
    name = facility.replace(" ", "")
    matches = []
    for row in candidates:
        if row['kind'] != 'body' or row.get('context_only') or row['unit_id'] != row['parent_chunk_id']:
            continue
        text = _paragraph_wording(row)
        rule = _OPERATING_RULE.fullmatch(text)
        if not rule:
            continue
        facilities = re.split(r"[ㆍ·]", rule['facilities'].replace(" ", ""))
        if name not in facilities:
            # An elided common prefix must also be attested as the whole facility name
            # in this article, not guessed solely from a suffix such as '센터'.
            if not any(name.endswith(part) and len(name[:-len(part)]) >= 2
                       and facilities[0].startswith(name[:-len(part)]) for part in facilities[1:]):
                continue
            direct_name = any(r['kind'] == 'body' and r.get('source_url') == row.get('source_url')
                       and r['locator'].get('article') == row['locator'].get('article')
                       and name in re.sub(r"\s+", "", r['quote'] + r['locator'].get('title', '')) for r in originals)
            if not direct_name and not (use_owner_context and _name_in_owner_article(name, row, originals)):
                continue
        matches.append((text, row))
    if len(matches) != 1:
        return None
    text, row = matches[0]
    if any(_SPECIFIC_HOURS.search(r['quote']) for r in originals
           if r.get('ordin_seq') == row.get('ordin_seq')):
        return None
    answer = text.removesuffix(".")
    answer = re.sub(r"정한다$", "정합니다", answer)
    answer = re.sub(r"있다$", "있습니다", answer)
    return "구체적인 운영 시간은 이 조항에 나와 있지 않습니다. " + answer + ".", row


def _paragraph_wording(row):
    text = re.sub(r"^\s*[①-⑳]\s*", "", row['quote']).strip()
    return re.sub(r"\s*<개정[\d.,\s]+>\s*$", "", text)


def _name_in_owner_article(name, row, originals):
    full_name = re.compile(r"(?<![가-힣])" + r"\s*".join(map(re.escape, name))
                           + r"(?=[^가-힣]|(?:은|는|을|를|의|이|가|에|에서)(?:\s|$)|$)")
    for context in originals:
        if (context['kind'] != 'required_context' or context.get('parent_chunk_id') != row['unit_id']
                or context['locator'].get('article') != row['locator'].get('article')
                or any(context.get(k) != row.get(k) for k in ('ordin_seq', 'source_url', 'effective_date', 'as_of_date'))
                or not context.get('source_unit_id')
                or context.get('unit_id') != row['unit_id'] + '::context::' + context['source_unit_id']):
            continue
        for segment in context.get('source_segments', []):
            start, end = segment.get('start'), segment.get('end')
            if (segment.get('kind') == 'provision' and type(start) is int and type(end) is int
                    and 0 <= start < end <= len(context['quote']) and context['quote'][start:end] == segment.get('text')
                    and full_name.search(segment['text'])):
                return True
    return False


def _same_facility_expense(clause, name):
    if re.search(r"다른\s*(?:시설|곳)|별개|타\s*시설", clause):
        return False
    words = list(re.finditer(r"[가-힣]+", clause))
    for index, token in enumerate(words):
        word = token[0]
        noun = re.sub(r"(?:에서는|에서|에는|에게|에도|은|는|이|가|의|을|를|에|도)$", "", word)
        if len(noun) < 2 or not (name.endswith(noun) or noun == '시설'):
            continue
        # The facility must be the thing being funded or installed. A facility
        # that is merely a landmark for another project cannot establish scope.
        if not re.match(r"\s*(?:(?:새로|직접|더|추가로)\s+)?"
                        r"(?:설치|조성|확충|증설|건립|운영|만들|늘리|늘릴|비용|경비|설립)", clause[token.end():]):
            continue
        full_mention = re.sub(r"\s+", "", clause[:token.start()] + noun).endswith(name)
        if (full_mention or index == 0 or words[index - 1][0] in ('그', '이', '해당', '같은', '새')
                or words[index - 1][0].endswith(('이', '가', '은', '는', '에서'))):
            return True
    return False


def hours_and_funding_claims(question, citations, originals, crosswalk):
    """Offer two independently sourced claims to the writer when both topics apply."""
    text = " ".join(question.split())
    if not re.search(r"지원|보조|융자", text):
        return None
    # An extension of operating hours can simply describe the expense.
    # Require an actual question about time, or a coordinated time noun and a
    # distributive request. Keep the original question for the writer in both cases.
    clock = re.search(r"(?:몇\s*시(?:부터 몇\s*시)?(?:부터|까지|에)|언제까지)\s*"
                      r"(?:문을?\s*)?(?:운영|개방|이용|열|여|닫|폐장|개장)", text)
    topic = re.search(r"(?:운영|개방|이용|폐장)\s*(?:시간|시각)(?P<particle>은|는|이|을|과|와)?", text)
    coordinated = topic and topic['particle'] in ('과', '와')
    asks_time = (clock
                 or topic and re.match(r"\s*(?:누가|어떻게|언제|몇\s*시|조례에\s*정)", text[topic.end():])
                 or coordinated and re.search(r"각각|둘\s*다|두\s*가지", text))
    if not asks_time:
        return None
    # The two proposals below share one jurisdiction. A question explicitly
    # assigning the issues to different administrations needs the full writer.
    jurisdictions = re.findall(r"(?<![가-힣])([가-힣]{2,8}(?:특별자치도|시청|도청|시))(?=[은는이가의에\s?.,]|$)", text)
    if any(place not in ('서울시', '서울특별시', '서울시청') for place in jurisdictions):
        return None
    # Require the fiscal clause itself to refer back to the same facility. The
    # presence of its name in an independent schedule question is insufficient.
    clauses = [p.strip() for p in re.split(r"[?？.!]+|(?:운영|개방|이용|폐장)\s*(?:시간|시각)(?:과|와)\s+", text)
               if p.strip()]
    expense = [part for part in clauses if re.search(r"지원|보조|융자", part)]
    if len(expense) != 1 or len(clauses) == 1 or not re.search(r"비용|경비|[가-힣]+비(?:를|도|에|가|는|\s)", expense[0]):
        return None
    compact = re.sub(r"\s+", "", text)
    aliases = {uid: alias for alias, uid in crosswalk.items()}
    bodies = [r for r in citations if r['kind'] == 'body' and not r.get('context_only')
              and r['unit_id'] == r['parent_chunk_id'] and r['unit_id'] in aliases]
    pairs = []
    for row in bodies:
        operating = _OPERATING_RULE.fullmatch(_paragraph_wording(row))
        if not operating:
            continue
        names = re.split(r"[ㆍ·]", operating['facilities'].replace(" ", ""))
        # An abbreviated list member ("수리센터") may share a common prefix
        # with its first member. Match the scope in the question and require
        # the full name to occur elsewhere in the same article.
        full_names = [names[0]] + [names[0][:i] + short for short in names[1:] for i in range(2, len(names[0]))]
        name = next((n for n in sorted(full_names, key=len, reverse=True) if n in compact), None)
        if not name or not _same_facility_expense(expense[0], name):
            continue
        schedule = _facility_hours_rule(name, originals, originals, use_owner_context=True)
        if not schedule or schedule[1]['unit_id'] != row['unit_id'] or schedule[1]['quote'] != row['quote']:
            continue
        grants = [r for r in bodies if r['unit_id'] != row['unit_id']
                  and all(r.get(k) == row.get(k) for k in ('ordin_seq', 'source_url', 'effective_date'))
                  and r['locator'].get('article') == row['locator'].get('article')
                  and (name in re.sub(r"\s+", "", r['quote']) or operating['facilities'] in r['quote'])
                  and re.search(r"경비|비용|설치비|운영비", r['quote'])
                  and re.search(r"(?:지원|보조|융자)할 수 있다\.$", _paragraph_wording(r))]
        if len(grants) != 1:
            continue
        grant = grants[0]
        funding = re.sub(r"할 수 있다\.$", "할 수 있습니다.", _paragraph_wording(grant))
        installation_grant = (r"(?:시장|도지사|군수|구청장)[은는] (?:[^,.;]+ 노력하여야 하며, )?"
                              r"[가-힣 ]+(?:에게|에 대하여) 예산의 범위(?: 안)?에서 "
                              + r"\s*".join(map(re.escape, name)) + r" 설치에 필요한 경비를 지원할 수 있습니다\.")
        if (re.fullmatch(installation_grant, funding) and funding.count("지원") == 1
                and re.search(r"연장|늘리|늘릴|확충|증설", text)):
            funding += " 설치에 들지 않는 비용까지 이 항에서 지원한다고 정한 것은 아닙니다."
        pairs.append([{"text": schedule[0], "citation_ids": [aliases[row['unit_id']]]},
                      {"text": funding, "citation_ids": [aliases[grant['unit_id']]]}])
    return pairs[0] if len(pairs) == 1 else None


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
        display_answer = draft['answer']
        hours = _delegated_hours(audit.get('question') or '', result, citation_rows)
        if hours:
            display_answer, citation_rows = hours
        sources=render_sources(citation_rows,catalog,owners)
        if not sources:
            raise ValueError('verified_draft_without_public_citations')
        response={'answer':display_answer,'citation':[s['citation'] for s in sources],
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
            source_kind=result.get('evidence_selection', {}).get('source_first', {}).get('kind')
            notice=scope_boundary_notice(missing,facts,source_kind=source_kind)
            if (result.get('scope_notice') != notice or result.get('fully_resolved') is not False
                    or result.get('answer_scope') != 'ordinance_only'):
                raise ValueError('scope_notice_invariant_failed')
            # The verified model body contains legal propositions only. This
            # mandatory prefix comes from unresolved processing state, with no
            # invented amount, user-fact interpretation, or ordinance citation.
            if not hours:
                output['response']['answer']=(display_answer+' '+notice['text'] if source_kind in SOURCE_SCOPE_NOTICES
                                              else notice['text']+'\n\n'+display_answer)
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
            if not hours:
                output['response']['answer']=CLARIFICATION_NOTICE+'\n\n'+draft['answer']
            output.update(missing_information=copy.deepcopy(facts),clarification_required=True,
                          fully_resolved=False)
        return output
    if draft_status == 'withheld_no_provision':
        if audit.get('status') == 'completed' and _confirmed_withheld_absence(result):
            return {**base, 'status': 'insufficient_evidence', 'message': MESSAGES['insufficient_evidence'],
                    'response': {'answer': '확인 불가', 'citation': []}}
        return {**base, 'status': 'code_check_failed', 'message': MESSAGES['code_check_failed']}
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
