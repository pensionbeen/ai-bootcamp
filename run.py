"""서울시 조례 RAG의 수집·검증·가공 단계를 실행합니다."""
import argparse
import json
import sys
import unittest
from pathlib import Path
from xml.etree.ElementTree import ParseError

PROJECT_ROOT = Path(__file__).resolve().parent
# Windows spawn 자식도 처리 모듈과 테스트 함수를 같은 이름으로 찾게 합니다.
for folder in ("src", "tests"):
    location = str(PROJECT_ROOT / folder)
    if location not in sys.path:
        sys.path.insert(0, location)

from request_lock import RequestInProgress


def check_saved_outputs(module, folder):
    expected, report = module.prepare(PROJECT_ROOT)
    for name, raw in expected.items():
        path = folder / name
        if not path.is_file():
            raise ValueError(f"저장된 산출물이 없습니다: {path.relative_to(PROJECT_ROOT)}. python run.py build로 생성하세요.")
        if path.is_symlink() or path.read_bytes() != raw:
            raise ValueError(f"저장된 산출물이 원본과 다릅니다: {path.relative_to(PROJECT_ROOT)}")
    return report, len(expected)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog="원본·별표·모델 호출 도구는 README의 개별 실행 안내를 참고하세요.")
    commands = parser.add_subparsers(dest="command", title="명령")
    commands.add_parser("check", help="원본과 저장된 본문·부칙 결과 검사 (API 호출·파일 쓰기 없음)")
    commands.add_parser("build", help="검증된 6종 원본에서 본문 → 부칙 순서로 가공 (API 호출 없음)")
    commands.add_parser("test", help="저장된 자료와 모의 응답으로 전체 오프라인 테스트 실행")
    collection = commands.add_parser("collect-attachments", help="6종 조례의 누락된 첨부 원본 수집")
    collection.add_argument("--check-only", action="store_true", help="다운로드 없이 보유·누락 상태만 확인")
    parsing = commands.add_parser("parse-attachments", help="누락된 첨부 Parse 후 원문 대조·본문 참조 검토자료 생성 (Upstage 사용)")
    parsing.add_argument("--check-only", action="store_true", help="API 호출·파일 쓰기 없이 기존 결과만 검사")
    chunking = commands.add_parser("chunk-attachments", help="교정본과 필수 문맥을 연결한 첨부 검색 후보 생성 (오프라인)")
    chunking.add_argument("--check-only", action="store_true", help="저장된 검색 후보·교정본을 쓰기 없이 검사")
    readiness = commands.add_parser("prepare-embeddings", help="검토한 첨부만 임베딩 입력으로 준비 (오프라인·벡터 생성 안 함)")
    readiness.add_argument("--check-only", action="store_true", help="임베딩 입력·보류 판정 검사만 수행")
    embeddings = commands.add_parser("embed-attachments", help="검토한 첨부만 Upstage passage 임베딩 후 JSON 저장")
    embeddings.add_argument("--check-only", action="store_true", help="API 호출·쓰기 없이 저장된 벡터와 출처 검사")
    embeddings.add_argument("--env-file", type=Path, help="API 키 설정 파일 (기본: 프로젝트 .env)")
    search = commands.add_parser("search-attachments", help="첨부 93개에서 질문 검색과 근거 판단 (기존62개 경로도 선택 가능)")
    search.add_argument("--question", required=True)
    search.add_argument("--check-only", action="store_true", help="API 호출·쓰기 없이 기존 질문 결과 검사")
    search.add_argument("--env-file", type=Path)
    search.add_argument("--corpus", choices=("combined93", "reviewed62"), default="combined93")
    evaluation = commands.add_parser("evaluate-search", help="고정된 질문으로 검색·근거판단 평가 (query 임베딩·Solar 사용)")
    evaluation.add_argument("--check-only", action="store_true", help="API 호출·쓰기 없이 저장된 평가 검사")
    evaluation.add_argument("--env-file", type=Path)
    evaluation.add_argument("--corpus", choices=("combined93", "reviewed62"), default="combined93")
    asking = commands.add_parser("ask", help="본문·부칙·첨부 전체 DB 검색 후 Solar 초안 생성·검증")
    asking.add_argument("--question", required=True)
    asking.add_argument("--check-only", action="store_true", help="API 호출·쓰기 없이 DB와 저장 질문 상태 확인")
    asking.add_argument("--env-file", type=Path)
    asking.add_argument("--db", type=Path, help="기본 2026-10-01 로컬 DB 대신 사용할 DB")
    asking.add_argument("--json", action="store_true", help="감사 원문을 제외한 공개 초안 JSON 출력")
    serving = commands.add_parser("serve", help="로컬 /ask·/health·/jobs API 실행 (127.0.0.1 전용)")
    serving.add_argument("--port", type=int, default=8000)
    serving.add_argument("--host", default="127.0.0.1", help="기본 127.0.0.1; 그 외 주소는 채점 환경 전용")
    serving.add_argument("--workers", type=int, help="동시에 처리할 질문 수 1~16 (기본 LOCAL_RAG_WORKERS 또는 4)")
    serving.add_argument("--queue-size", type=int, help="대기할 수 있는 질문 수 0~64 (기본 LOCAL_RAG_QUEUE_SIZE 또는 12)")
    coverage = commands.add_parser("condition-coverage", help="434개 조건의 원문·검색 연결 상태 저장")
    coverage.add_argument("--check-only", action="store_true", help="기존 연결 보고서 확인, API·쓰기 없음")
    local_eval = commands.add_parser("evaluate-local", help="6종 전체 DB의 추가 대표 사례 평가")
    local_eval.add_argument("--check-only", action="store_true", help="저장 질문만 확인, API·쓰기 없음")
    local_eval.add_argument("--cases", type=Path)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "test":
        suite = unittest.defaultTestLoader.discover(str(PROJECT_ROOT / "tests"), pattern="test_*.py")
        if suite.countTestCases() == 0:
            print("실행할 테스트가 없습니다.", file=sys.stderr)
            return 1
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    from evidence_gate import GateError
    try:
        if args.command == "serve":
            import local_rag_service
            options=["--project",str(PROJECT_ROOT),"--port",str(args.port),"--host",args.host]
            if args.workers is not None:options.extend(["--workers",str(args.workers)])
            if args.queue_size is not None:options.extend(["--queue-size",str(args.queue_size)])
            return local_rag_service.main(options)
        if args.command == "condition-coverage":
            import condition_coverage
            report=condition_coverage.run_coverage(PROJECT_ROOT,check_only=args.check_only)
            print(json.dumps(report,ensure_ascii=False,indent=2))
            return 0
        if args.command == "evaluate-local":
            import evaluate_local_rag
            options=["--project",str(PROJECT_ROOT)]
            if args.check_only:options.append("--check-only")
            if args.cases:options.extend(["--cases",str(args.cases)])
            return evaluate_local_rag.main(options)
        if args.command == "ask":
            import ask_local_rag
            from public_rag_response import public_result
            audit = ask_local_rag.run_question(PROJECT_ROOT, args.question, args.db,
                                               args.check_only, args.env_file)
            output=public_result(audit,PROJECT_ROOT)
            output['called']=audit.get('called',0)
            if args.json:
                print(json.dumps(output,ensure_ascii=False,indent=2))
            else:
                print(f"상태: {output['status']} / 이번 API 호출 {output['called']}회")
                print(output['message'])
                if output.get('response'):
                    print(output['response']['answer'])
                    for citation in output['response']['citation']:
                        print("출처:",citation)
                for item in output.get('missing_information', []):
                    print("질문 조건 확인 필요:", item)
                for item in output.get('missing_materials', []):
                    print("외부 자료 확인 필요:", item)
                print("최종 답변 허용: false")
                if audit.get('output_paths'):
                    print("질문 기록:", audit['output_paths']['audit'])
            return 1 if output['status'] in ('processing_error','verification_failed','code_check_failed') else 0
        import download_attachment as attachments
        if args.command in ("search-attachments", "evaluate-search"):
            if args.corpus == "combined93":
                import search_combined_attachments as search
                search_function = search.search_combined
                searchable_count = 93
            else:
                import search_attachment_chunks as search
                search_function = search.search_reviewed
                searchable_count = 62
            if args.command == "search-attachments":
                audit = search_function(PROJECT_ROOT, args.question, args.env_file, args.check_only)
                result = audit["result"]
                print(f"근거 판정: {result['decision']} / 이번 API 호출 {audit['called']}회")
                print(result["user_message"])
                for item in result["missing_information"]:
                    print("확인 필요:", item)
                for item in result.get("required_external_materials", []):
                    print("외부 자료 확인 필요:", item)
                for item in result.get("evidence", []):
                    if "primary_quote" in item:
                        print("근거:", item["primary_quote"])
                        print("출처:", item["source"]["source_url"])
                print("저장:", audit["output_file"])
            else:
                result = search.run_search_evaluation(PROJECT_ROOT, args.env_file, args.check_only)
                print(f"검색 평가 {result['passed_count']}/{result['case_count']}개 통과 / 응답 검증 오류 {result['validation_error_count']}개 / 잘못된 근거 있음 판정 {result['false_supported_count']}개 / 이번 API 호출 {result['called']}회")
            print(f"검색 가능한 {searchable_count}개 범위의 검증이며 최종 답변은 생성하지 않았습니다.")
            if args.command == "evaluate-search" and result.get("output_file"):
                print("평가 결과:", result["output_file"])
            return 2 if args.command == "evaluate-search" and result["passed_count"] != result["case_count"] else 0
        if args.command == "embed-attachments":
            import embed_attachment_chunks as embedding
            result = embedding.run_embeddings(PROJECT_ROOT, env_file=args.env_file, check_only=args.check_only)
            print(f"임베딩 {result['embedded_count']}개 / {result['dimension']}차원 / 이번 API 호출 {result['called']}회")
            print("검토된 텍스트·문맥·출처 연결을 검증했습니다. JSON 저장 완료이며 DB 적재·검색 평가·답변 허용은 별도 단계입니다.")
            return 0
        if args.command == "prepare-embeddings":
            import embed_attachment_chunks as embedding
            result = embedding.run_readiness(PROJECT_ROOT, check_only=args.check_only)
            print(f"후보 {result['candidate_count']}개 / 임베딩 입력 준비 {result['ready_count']}개 / 보류 {result['held_count']}개")
            print("단위·비고·필수 본문 조건 보존. API 호출·새 임베딩·DB 저장은 하지 않았으며 답변 허용은 별도 검토입니다.")
            return 0
        if args.command == "chunk-attachments":
            import build_attachment_search_chunks as chunks
            result = chunks.run_batch(PROJECT_ROOT, check_only=args.check_only)
            print(f"첨부 검색 후보 {result['chunk_count']}개 / 교정본 {result['correction_count']}건 / "
                  f"수동 출처 보류 {result['manual_binding_held_chunks']}개 / 그림 목록용 {result['image_metadata_only_chunks']}개")
            print("필수 문맥·본문 연결 검증 완료. 임베딩·답변 사용 전이며 전체 내용 검수는 대기 상태입니다.")
            return 0
        if args.command == "collect-attachments":
            return attachments.main(["--project-dir", str(PROJECT_ROOT),
                                     "--check-only" if args.check_only else "--collect"])
        if args.command == "parse-attachments":
            import parse_attachment as parse
            import build_attachment_evidence as evidence
            if args.check_only:
                print("첨부 Parse 상태:", parse.check(PROJECT_ROOT))
                if attachments.safe_path(PROJECT_ROOT, evidence.BATCH_MANIFEST).exists():
                    print("첨부 근거·연결 검증:", evidence.run_batch(PROJECT_ROOT, check_only=True)["attachment_count"], "개")
                else:
                    print("첨부 검토자료가 아직 없습니다.")
            else:
                print("첨부 Parse:", parse.parse_all(PROJECT_ROOT))
                report = evidence.run_batch(PROJECT_ROOT)
                print(f"검토자료 저장: {report['attachment_count']}첨부 / {report['page_count']}페이지 / "
                      f"본문 참조 {report['body_reference_count']}건 / 확인된 추출 문제 {report['known_blocking_findings']}건. 내용 검수·임베딩·답변 사용 전입니다.")
            return 0
        import build_ordinance_corpus as body
        import build_supplement_context as supplements
        if args.command == "check":
            folder = PROJECT_ROOT / "data/processed/ordinances" / body.source_import.AS_OF_DATE
            body_report, body_files = check_saved_outputs(body, folder)
            supplement_report, supplement_files = check_saved_outputs(supplements, folder / "supplements")
            reviews = supplements.validate_review_records(PROJECT_ROOT)
            print(f"검사 완료: 본문 {body_report['article_count']}조·{body_report['chunk_count']}조각 / "
                  f"부칙 {supplement_report['block_count']}묶음 / 저장된 산출물 {body_files + supplement_files}개 일치")
            print("검수 대기 상태 유지. 개별 조항의 적용 여부·답변 정확도를 확정한 결과는 아닙니다.")
            print(f"별도 연혁 검토 기록 {len(reviews)}건: 근거·인용·현재 조각 연결 일치 (답변기 연결 전)")
            coverage = attachments.check_collection(PROJECT_ROOT)
            print(f"첨부 원본 {coverage['present']}/{coverage['total']}개 검증 / 누락 {coverage['missing']}개 / "
                  f"수집 완료 목록 검증 {coverage['manifest_verified']}")
            import build_attachment_evidence as evidence
            if attachments.safe_path(PROJECT_ROOT, evidence.BATCH_MANIFEST).exists():
                extraction = evidence.run_batch(PROJECT_ROOT, check_only=True)
                print(f"첨부 추출·참조 {extraction['attachment_count']}개 검증 / "
                      f"본문 참조 {extraction['body_reference_count']}건 / 원본 추출에서 발견한 문제 {extraction['known_blocking_findings']}건")
            import build_attachment_search_chunks as chunks
            if attachments.safe_path(PROJECT_ROOT, chunks.FOLDER + "/chunks.manifest.json").exists():
                candidate_report = chunks.run_batch(PROJECT_ROOT, check_only=True)
                print(f"별도 교정본 {candidate_report['correction_count']}건 / 검색 후보 {candidate_report['chunk_count']}개 검증 (전체 내용 검수 대기)")
            import embed_attachment_chunks as embedding
            if attachments.safe_path(PROJECT_ROOT, embedding.READINESS_FOLDER + "/readiness.manifest.json").exists():
                readiness_report = embedding.run_readiness(PROJECT_ROOT, check_only=True)
                print(f"검토된 임베딩 입력 {readiness_report['ready_count']}개 / 보류 {readiness_report['held_count']}개 검증 (입력 검토 기록·답변 보류)")
            vector_folder = attachments.safe_path(PROJECT_ROOT, embedding.BATCH_FOLDER)
            if (vector_folder / "embeddings.manifest.json").exists() or (vector_folder.exists() and any(vector_folder.glob("*.attempt.json"))):
                vector_report = embedding.check_embeddings(PROJECT_ROOT)
                print(f"첨부 임베딩 {vector_report['embedded_count']}개 / {vector_report['dimension']}차원 검증 (JSON 저장·답변 보류)")
            import search_attachment_chunks as search
            if attachments.safe_path(PROJECT_ROOT, search.SEARCH_FOLDER + "/evaluation.manifest.json").exists():
                search_report = search.run_search_evaluation(PROJECT_ROOT, check_only=True)
                print(f"질문 검색 평가 기록 일치: {search_report['passed_count']}/{search_report['case_count']}개 통과·응답 검증 오류 {search_report['validation_error_count']}개 (기록 무결성과 평가 통과는 별도·답변 보류)")
            combined_folder = PROJECT_ROOT / "data/processed/attachments/2026-10-01/combined_search/evaluations"
            if combined_folder.exists() and any(combined_folder.glob("*.report.json")):
                import search_combined_attachments as combined_search
                combined_report = combined_search.run_search_evaluation(PROJECT_ROOT, check_only=True)
                print(f"93개 첨부 추가 질문 평가 일치: {combined_report['passed_count']}/{combined_report['case_count']}개 통과 / 외부 자료 확인 과제 {combined_report['external_task_count']}개")
        else:
            print("[1/2] 본문 가공")
            body.run(PROJECT_ROOT)
            print("[2/2] 부칙 원문·본문 연결 가공")
            supplements.run(PROJECT_ROOT)
            print("완료: 본문·부칙 자료. 전체 첨부 처리·임베딩·DB·답변은 별도 단계입니다.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, ParseError, RequestInProgress, GateError) as exc:
        print(f"{args.command} 중단: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
