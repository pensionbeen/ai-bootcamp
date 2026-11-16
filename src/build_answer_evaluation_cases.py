"""Build local-only gold fixtures from the read-only DB and supplied starter kit.

The generated quotes and sample source stay in ignored data/. No API is called.
Expectations are evaluation inputs only and must never be sent to Solar.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

ASOF = '2026-10-01'
DATABASE = 'data/processed/rag/2026-10-01/71ce40874477c9a4bc9d/rag.sqlite3'
OUTPUT = 'data/processed/local_rag/2026-10-01/citation_polish/cases.json'
LAWS = {
    'y': ('2054497', '서울특별시 청년 기본 조례', '2025-07-14'),
    'p': ('2171833', '서울특별시 주차장 설치 및 관리 조례', '2026-10-01'),
    'a': ('2171829', '서울특별시 동물보호 조례', '2026-10-01'),
    'b': ('2171767', '서울특별시 자전거이용 활성화에 관한 조례', '2026-10-01'),
    'f': ('2171803', '서울특별시 금연환경 조성 및 간접흡연 피해방지조례', '2026-10-01'),
    'c': ('2147709', '서울특별시 도시공원 조례', '2026-07-13'),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def cite(law, article=None, paragraph=None, item=None, attachment=None, row=None):
    seq, name, effective = LAWS[law]
    return {'ordin_seq': seq, 'ordin_name': name, 'effective_date': effective,
            'source_url': 'https://www.law.go.kr/ordinInfoP.do?ordinSeq='+seq,
            'article': article, 'paragraph': paragraph, 'item': item,
            'attachment': attachment, 'row_index': row}


def case(cid, ids, citations, expectation, checks=(), outcome='supported', question=None):
    outcomes = {'supported': [('supported', 'verified_draft')],
                'external': [('external_material_required', 'scope_limited_draft')],
                'clarify': [('needs_clarification', 'not_generated')],
                'clarify_or_external': [('needs_clarification', 'not_generated'),
                                        ('external_material_required', 'scope_limited_draft')],
                'unknown': [('insufficient_evidence', 'not_generated')],
                'outside': [('out_of_scope', 'not_generated')],
                'historical': [('out_of_scope', 'not_generated'),
                               ('insufficient_evidence', 'not_generated')],
                'general_or_scoped': [('supported', 'verified_draft'),
                                      ('external_material_required', 'scope_limited_draft')]}
    return {'id': cid, 'question': question, 'cohort': 'starter' if question is None else 'confusion',
            'anchor_ids': ids, 'required_citations': citations,
            'expected_outcomes': [{'decision': d, 'draft_status': s} for d, s in outcomes[outcome]],
            'correctness_constraints': expectation, 'answer_checks': list(checks),
            'answer_allowed': False, 'independent_semantic_review': 'pending'}


def definitions():
    y='2054497:000300:all'; p='2171833:000700:1'; a='2171829:002800:all'
    b='2171767:000900:1'; park='2147709:002000:1'
    fee='2171833:22740723:candidate:p1-e3-r3'
    bike='2171767:22728501:candidate:p1-e3-r1'
    rows = [
        case('S1',[fee,'2171833:000600:1'],[cite('p',attachment='별표 1',row=3)],
             ['1급지 노상은 5분당 500원이다. 같은 행의 노외 400원이나 야간 5,000원을 답하지 않는다.'],[r'500\s*원',r'5\s*분']),
        case('S2',['2171803:001000:1','2171803:000500:4'],[cite('f','제10조',1)],
             ['조례에 따라 지정된 금연구역 흡연 과태료는 10만원이다. 모든 서울 장소로 확장하지 않는다.'],[r'(10\s*만|100,?000)\s*원']),
        case('S3',[y],[cite('y','제3조',item='1')],
             ['청년은 19세 이상 39세 이하이다. 관계 법령·조례의 별도 규정 단서를 보존한다.',
              '제3조는 항이 없다. 근거는 제1호이며 공개 출처는 제3조까지만 표시한다. 시행일은 2025-07-14이다.'],[r'19\s*세',r'39\s*세',r'(따로|별도|다른|관계)']),
        case('C1',[park],[cite('c','제20조',1,'2')],
             ['65세 이상은 입장료를 면제할 수 있는 대상이다. 운동시설 이용료까지 전액 면제로 바꾸지 않는다.'],[r'65\s*세',r'면제']),
        case('C2',[p],[cite('p','제7조',1,'2')],
             ['경형차 50% 감면 가능, 지하철 환승주차장에서 환승 목적이면 80%, 1일 1회 최초 3시간 면제의 조건을 구별한다.'],[r'(50\s*%|50\s*퍼센트|100분의\s*50)',r'(환승|50)']),
        case('C3',[a],[cite('a','제28조',item='2')],
             ['유기견 입양·기증 후 등록수수료는 전액 감면 가능하다. 등록대행 수수료는 제외한다. 항을 만들지 않는다.'],[r'(전액|모두)',r'등록대행',r'(제외|대상.*아니|대상.*않)']),
        case('M1',['2171833:000700:3',p],[cite('p','제7조',3),cite('p','제7조',1,'1'),cite('p','제7조',1,'7')],
             ['둘을 중복 적용하지 않고 높은 하나만 적용한다. 장애인 요건 충족 시80%, 다둥이카드50%이며 장애인표지·탑승·증빙 조건을 함부로 생략한 개인자격 확정은 금지한다.'],[r'(하나|한\s*가지)',r'(80\s*%|80\s*퍼센트|100분의\s*80)',r'(50\s*%|50\s*퍼센트|100분의\s*50)']),
        case('M2',['2147709:002203:all','2147709:002300:4'],[cite('c','제22조의3'),cite('c','제23조',4)],
             ['어린이공원은 금주구역으로 지정할 수 있다. 지정된 금주구역 음주는10만원. 어린이공원이라는 이유만으로 자동 지정되었다고 단정하지 않는다.'],[r'지정',r'(10\s*만|100,?000)\s*원'],outcome='general_or_scoped'),
        case('M3',[b,bike],[cite('b','제9조',1),cite('b',attachment='별표',row=1)],
             ['원칙 무료. 민간단체 위탁 등 특별한 사유에 별표 범위 내 규칙으로 요금 부과 가능.',
              '별표는 1시간200원/1일1,000원/월15,000원. 이 표를 실제 모든 주차장 확정요금이라고 설명하지 않는다.'],[r'무료',r'200',r'1,?000',r'15,?000',r'(규칙|특별|위탁)'],outcome='general_or_scoped'),
        case('T1',[],[],['제공된 6종 조례만으로 안전모 미착용 과태료를 확인하지 못한다. 외부 법률 기억으로 금액을 보충하지 않는다.'],outcome='unknown'),
        case('T2',[],[],['강남구 자체 규칙은 본청6종 범위 밖이다. 정확히 확인 불가와 빈 citation을 반환한다.'],outcome='outside'),
        case('N01_youth_age_edges',[y],[cite('y','제3조',item='1')],
             ['일반 정의만 보면18·40은 제외,19·39는 포함. 다른 관계 법령·조례 단서를 보존한다. 개별 사업 자격으로 확대하지 않는다.'],[r'18',r'19',r'39',r'40',r'(따로|별도|다른|관계)'],
             question='2026-10-01 기준 청년 기본 조례의 일반 정의만 보면 18세·19세·39세·40세 중 어느 나이가 청년에 포함되나요? 개별 사업 자격이 아니라 나이 경계와 단서를 묻습니다.'),
        case('N02_veteran_not_automatic',[y,'2054497:002400:all'],[cite('y','제3조',item='1'),cite('y','제24조')],
             ['의무복무 제대군인이 청년정책에 참여하는 경우 복무기간을 고려해 최대3세 범위에서 연장 적용할 수 있다. 모든40세 제대군인에게 자동 적용한다고 단정하지 않는다.'],[r'(세\s*살|3\s*세)',r'복무',r'(수 있|가능)'],
             question='청년 기본 조례상 40세 의무복무 제대군인은 모두 자동으로 청년이 되나요? 일반 정의와 제대군인 연령 상한 특례의 문언·조건만 비교해 주세요.'),
        case('N03_parking_column_swap',[fee,'2171833:000600:1'],[cite('p',attachment='별표 1',row=3)],
             ['1급지 노상500원과 노외400원을 각각5분당으로 대응한다. 1일주차·월정기권 수치와 혼합하지 않는다.'],[r'노상',r'노외',r'500',r'400',r'5\s*분'],
             question='주차장 조례 별표 1에서 1급지 노상주차장과 노외주차장의 1회 주차 5분당 요금을 각각 비교해 주세요. 일주차권이나 월정기권 요금은 묻지 않습니다.'),
        case('N04_market_grade_exception',[p],[cite('p','제7조',1,'8')],
             ['전통시장 구매 확인 차량 최초2시간50% 일반 감면은1급지 소재 주차장에서는 적용하지 않는다. 1급지도50%라고 답하면 실패다.'],[r'1\s*급지',r'(않|아니|제외|없)'],
             question='전통시장 구매 영수증을 제시하면 서울시 1급지 공영주차장에서도 최초 2시간 50% 감면을 받을 수 있다는 말이 맞나요? 다른 감면사유는 없고 제7조의 해당 규정만 묻습니다.'),
        case('N05_warehouse_minimum_maximum',['2171833:22740725:candidate:legacy-8','2171833:22740727:candidate:p1-e2-r7','2171833:002000:1','2171833:002100:2'],
             [cite('p',attachment='별표 2'),cite('p',attachment='별표 3',row=7)],
             ['별표2 창고 설치기준267㎡당1대와 별표3 최고한도534㎡당1대를 구별한다. 설치제한지역의 별표3를 모든 창고에 전용하지 않는다. 실제 대수를 계산하지 않는다.'],[r'267',r'534',r'(최고|한도)',r'(제한지역|제한\s*지역)'],
             question='창고시설의 부설주차장 기준이 267㎡당 1대인지 534㎡당 1대인지 헷갈립니다. 별표 2의 설치기준과 별표 3의 최고한도를 각각 설명하고 적용범위를 구분해 주세요. 특정 건물 대수 계산은 아닙니다.'),
        case('N06_adoption_agency_exclusion',[a],[cite('a','제28조',item='2')],
             ['등록수수료 전액 감면 가능과 등록대행 수수료 제외를 구분한다. 모두 무료라는 문장을 승인하지 않는다.'],[r'등록대행',r'(제외|대상.*아니|대상.*않)'],
             question='유기견을 입양해서 등록하면 동물 등록수수료뿐 아니라 등록대행 수수료까지 전액 감면된다는 뜻인가요? 동물보호 조례 문언만으로 구분해 주세요.'),
        case('N07_education_internal_chip',[a],[cite('a','제28조',item='8')],
             ['시장 또는 구청장이 인정하는 입양교육 수료와 내장형 무선식별장치 조건이 모두 필요하다. 외장형에도 전액이라고 답하지 않는다.'],[r'내장형',r'(시장|구청장)',r'교육'],
             question='시장 또는 구청장이 인정하는 반려동물 입양 교육을 수료하면 외장형 무선식별장치로 등록해도 전액 감면인가요? 해당 감면의 장치 조건만 확인해 주세요.'),
        case('N08_subsidized_bike_free',['2171767:000900:2'],[cite('b','제9조',2)],
             ['법 제11조제2항으로 설치해 시비를 지원받은 자전거주차장은 무료로 하여야 한다. 일반 별표 유료금액을 바로 적용하지 않는다.'],[r'무료'],
             question='자전거이용 활성화 조례 제9조제2항에 해당하여 설치비로 시비를 지원받은 자전거주차장에도 별표의 시간당 200원을 받아야 하나요? 그 항의 규칙만 묻습니다.'),
        case('N09_bike_actual_rate_unverified',[b,bike],[cite('b','제9조',1),cite('b',attachment='별표',row=1)],
             ['민간위탁 특별사유가 있어도 실제 부과요금은 별도 규칙 확인이 필요하다. 별표200원만으로 반드시200원이라고 확정하지 않는다.','외부 자료 확인 필요/미해결을 유지하고 조례의 무료 원칙과 규칙 위임·별표 범위를 설명한다.'],[r'무료',r'규칙'],outcome='external',
             question='민간단체에 위탁된 서울시 자전거주차장은 오늘 실제로 반드시 시간당 200원을 받는 건가요? 적용 규칙과 해당 주차장의 요금 고시는 제공하지 않았습니다. 조례로 확인되는 부분과 아직 확인할 부분을 나눠 주세요.'),
        case('N10_lit_tobacco_possession',['2171803:000200:all','2171803:000500:4','2171803:001000:1'],[cite('f','제2조',item='2'),cite('f','제5조',4),cite('f','제10조',1)],
             ['불붙은 담배를 소지하는 것도 흡연에 포함. 지정된 금연구역이면 금지 및10만원 과태료 규정. 실제 피우지 않았으니 제외라고 답하지 않는다.'],[r'소지',r'(10\s*만|100,?000)\s*원'],
             question='조례에 따라 지정된 금연구역에서 불이 붙은 담배를 손에 들고만 있고 입으로 피우지는 않으면 흡연 금지와 과태료 규정에서 제외되나요? 정의와 과태료 조항을 함께 설명해 주세요.'),
        case('N11_missing_smoking_location',['2171803:000500:1','2171803:000500:3','2171803:000500:4','2171803:001000:1'],[],
             ['출입구부터 거리·실제 위치와 지정 고시 등 확인 없이 과태료 대상이라고 확정하지 않는다. 개인 사실 누락을 근거 없음으로 바꾸지 않는다.'],outcome='clarify_or_external',
             question='저는 서울 지하철역 근처에서 담배를 피웠습니다. 정확한 위치와 출입구부터 거리, 그곳의 금연구역 지정 여부는 알려드리지 않았는데 제게 과태료 10만원이 부과되는지 확정해 주세요.'),
        case('N12_park_entry_sports_split',[park,'2147709:002000:3'],[cite('c','제20조',1,'2'),cite('c','제20조',3,'1')],
             ['65세 이상 입장료 면제 가능과 별표2 운동시설 이용료50%범위 감면 가능을 구분한다. 규칙을 확인하지 않고 운동시설까지 전액 면제라고 답하지 않는다.'],[r'입장료',r'운동시설',r'(50\s*%|50\s*퍼센트|100분의\s*50)',r'규칙'],outcome='general_or_scoped',
             question='65세 이상이면 서울시 도시공원의 입장료와 별표 2 운동시설 이용료가 모두 전액 면제인가요? 실제 할인액이 아니라 제20조의 두 규정과 규칙에 맡긴 부분을 비교해 주세요.'),
        case('N13_table_definition_transfer',['2147709:22415977:candidate:p1-e2-r1'],[cite('c',attachment='별표 1')],
             ['별표1의 청소년13~18세와 어른19~65세미만 정의는 그 표의 문맥이다. 별표2 운동시설의 청소년 나이에 자동 전용하지 않는다. 별도 적용자료 미확인을 유지한다.'],[r'(13|18)',r'별표\s*1'],outcome='external',
             question='도시공원 별표 1 비고에서 청소년은 13~18세라고 했으니 별표 2 운동시설 요금의 청소년도 무조건 같은 나이라고 확정해도 되나요? 별표 2의 연령 적용 규칙은 제공하지 않았습니다.'),
        case('N14_historical_version',[],[],
             ['2020-01-01 당시의 정의를 현재2025 시행판본으로 소급 확정하지 않는다. 제공된 기준일 범위에서 확인 불가 응답.'],outcome='historical',
             question='2020년 1월 1일 당시 시행 중이던 서울시 청년 기본 조례의 청년 연령을 알려 주세요. 2025년 시행 현행 판본의 숫자를 과거 정답으로 대신하지 마세요.'),
    ]
    next(row for row in rows if row['id']=='N11_missing_smoking_location')['requires_missing_information']=True
    next(row for row in rows if row['id']=='N13_table_definition_transfer')['context_anchor_ids']=['ctx-993629f70a3ad1a136f3']
    return rows


def source_anchor(connection, chunk_id):
    row=connection.execute('SELECT kind,source_json FROM chunks WHERE chunk_id=?',(chunk_id,)).fetchone()
    if row is None:
        raise ValueError('missing_case_anchor:'+chunk_id)
    wrapper=json.loads(row[1]); source=wrapper['source_record']
    quote=source[{'body':'quote_text','attachment':'primary_text','supplement':'source_text'}[row[0]]]
    metadata=source.get('source',source)
    return {'chunk_id':chunk_id,'kind':row[0],'quote':quote,'quote_sha256':sha(quote.encode('utf-8')),
            'source_unit_id':wrapper.get('source_unit_id'),
            **{key:metadata[key] for key in ('ordin_seq','source_url','effective_date')}}


def build(project, starter_kit, output=None):
    project=Path(project).resolve(strict=True); starter=Path(starter_kit).resolve(strict=True)
    database=project/DATABASE
    sample_path=starter/'sample_questions.json'; sample_raw=sample_path.read_bytes()
    samples=json.loads(sample_raw)
    expected_ids=['S1','S2','S3','C1','C2','C3','M1','M2','M3','T1','T2']
    if not isinstance(samples,list) or [s.get('id') for s in samples]!=expected_ids:
        raise ValueError('unexpected_starter_case_inventory')
    indexed={s['id']:s for s in samples}; rows=definitions()
    connection=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
    try:
        for row in rows:
            if row['cohort']=='starter':
                sample=indexed[row['id']]
                if not isinstance(sample.get('question'),str) or not isinstance(sample.get('gold'),list):
                    raise ValueError('invalid_starter_case')
                row['question']=sample['question']; row['starter_reference']=sample
            row['source_anchors']=[source_anchor(connection,cid) for cid in row.pop('anchor_ids')]
            row['context_anchors']=[]
            for context_id in row.pop('context_anchor_ids',[]):
                context=connection.execute('SELECT text FROM contexts WHERE context_id=?',(context_id,)).fetchone()
                if context is None:raise ValueError('missing_case_context_anchor')
                row['context_anchors'].append({'context_id':context_id,'quote':context[0],
                                               'quote_sha256':sha(context[0].encode('utf-8'))})
            row['expected_primary_id']=row['source_anchors'][0]['chunk_id'] if row['source_anchors'] else None
            row['supporting_primary_ids']=[a['chunk_id'] for a in row['source_anchors'][1:]]
            row['expected_decision']=row['expected_outcomes'][0]['decision']
            row['expected_draft_status']=row['expected_outcomes'][0]['draft_status']
    finally:
        connection.close()
    result={'schema_version':'answer-citation-evaluation-v2','as_of_date':ASOF,
            'answer_allowed':False,'final_answer_generated':False,
            'database_sha256':sha(database.read_bytes()),'case_count':len(rows),
            'cohort_counts':{'starter':11,'confusion':14},
            'starter_sample_sha256':sha(sample_raw),'starter_sample_path':str(sample_path),
            'contract':{'normal_response_keys':['answer','citation','source_url','effective_date'],
                        'unknown_response':{'answer':'확인 불가','citation':[]},
                        'metadata':'run.py --json wrapper preserved; only nested response uses answer contract',
                        'multiple_sources':'URL/date strings if uniform, otherwise arrays aligned with citation',
                        'starter_difference':'starter UNKNOWN has four keys with empty URL/date; user contract uses exactly two; article-only public labels for items without a numbered paragraph'},
            'semantic_scoring':'automated checks are not semantic accuracy; independent review remains pending',
            'cases':rows}
    target=Path(output) if output else project/OUTPUT
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result,target


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--starter-kit',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    fixture,path=build(args.project,args.starter_kit,args.output)
    print(json.dumps({'case_count':fixture['case_count'],'cohort_counts':fixture['cohort_counts'],
                      'called':0,'output':str(path)},ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
