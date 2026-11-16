"""고정된 6개 질문과 4개 잘못된 답으로 답변 초안 생성을 시험합니다."""

import argparse
import json
from pathlib import Path

import requests

from answer_attachment import (
    FALLBACK_RESPONSE, VERIFY_PROMPT, VERIFY_SCHEMA, call_solar, failure_audit,
    generate_draft, prepare_input, save_draft, validate_verification,
    preserve_required_conditions, validate_required_conditions,
)
from evidence_gate import GateError, digest_json, load_corpus
from link_attachment_article import build_links
from search_attachment_chunks import read_json, save_json

CASES = {
    "school_city_ordinance_with_district_name": {"status": "draft", "terms": ["학교", "250"]},
    "public_office_specific_row": {"status": "draft", "terms": ["공공업무시설", "200"]},
    "note_11_parent_condition": {"status": "draft", "terms": ["도시형", "공동이용", "아닌", "준주거", "상업", "40", "이상"]},
    "parking_fee_not_in_current_evidence": {"status": "draft", "fallback": True},
    "warehouse_count_missing_area": {"status": "needs_clarification"},
    "explicit_district_enacted_ordinance": {"status": "draft", "fallback": True},
}
# 이 기대값과 잘못된 답은 생성 프롬프트에 들어가지 않습니다.
NEGATIVE_CASES = [
    {"id": "public_office_wrong_subtype_number", "case": "public_office_specific_row",
     "answer": "공공업무시설의 설치기준은 시설면적 100m²당 1대입니다."},
    {"id": "note11_reversed_target", "case": "note_11_parent_condition", "attach_source_conditions": True,
     "answer": "도시형 생활주택의 주차장 일부를 승용차공동이용 지원에 사용하는 경우, 준주거지역 또는 상업지역에서는 승용차공동이용 지원용 주차구획을 총수의 40% 이상 설치해야 합니다."},
    {"id": "note11_missing_parent_conditions", "case": "note_11_parent_condition", "expect_local_rejection": True,
     "answer": "준주거지역 또는 상업지역에서는 주차단위구획 총수의 40% 이상을 설치해야 합니다."},
    {"id": "school_wrong_facility", "case": "school_city_ordinance_with_district_name",
     "answer": "창고시설의 설치기준은 시설면적 250m²당 1대입니다."},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    corpus = load_corpus(args.project_dir)
    links = build_links(args.project_dir, corpus)
    result_dir = args.project_dir / "data" / "processed" / "attachments" / "linked_gate_results"
    report = {"case_contract_hash": digest_json({"cases": CASES, "negative_cases": NEGATIVE_CASES}),
              "test_kind": "development_regression_not_accuracy_estimate", "cases": [], "negative_checks": []}
    report_path = args.output_dir / "evaluation.json"
    for case_id, expected in CASES.items():
        print("답변 시험:", case_id, flush=True)
        gate = read_json(result_dir / f"{case_id}.result.json")
        try:
            audit = generate_draft(args.project_dir, args.output_dir / "cache", gate, corpus, links)
        except (GateError, ValueError, TypeError, KeyError, OSError, requests.RequestException) as exc:
            audit = failure_audit(gate["question"], exc)
        save_draft(args.output_dir, case_id, audit)
        failures = []
        if audit["status"] != expected["status"]:
            failures.append("status_mismatch")
        if expected.get("fallback") and audit.get("response") != FALLBACK_RESPONSE:
            failures.append("fallback_contract_mismatch")
        if expected["status"] == "needs_clarification":
            if audit.get("response") is not None or "시설면적" not in " ".join(audit.get("missing_information", [])):
                failures.append("missing_clarification")
        if "terms" in expected:
            response = audit.get("response") or {}
            if set(response) != {"answer", "citation", "source_url", "effective_date"}:
                failures.append("normal_schema_mismatch")
            if any(t not in response.get("answer", "") for t in expected["terms"]):
                failures.append("answer_key_content_missing")
            if not any("제20조 제1항" in s for s in response.get("citation", [])):
                failures.append("article_paragraph_citation_missing")
            if not any("조례 별표 2" in s for s in response.get("citation", [])):
                failures.append("separate_annex_citation_missing")
            if response.get("effective_date") != "2026-10-01":
                failures.append("effective_date_mismatch")
        entry = {"case_id": case_id, "status": audit["status"], "passed": not failures, "failures": failures}
        report["cases"].append(entry)
        save_json(report_path, report)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        if audit["status"] == "error":
            print("오류:", audit["error_code"], flush=True)
            raise SystemExit(1)
    for case in NEGATIVE_CASES:
        print("잘못된 답 거절 시험:", case["id"], flush=True)
        gate = read_json(result_dir / f'{case["case"]}.result.json')
        _, model_input = prepare_input(gate, corpus, links)
        try:
            candidate_answer = case["answer"]
            if case.get("attach_source_conditions"):
                candidate_answer = preserve_required_conditions(candidate_answer, model_input)
            try:
                validate_required_conditions(candidate_answer, model_input)
            except GateError as exc:
                entry = {"case_id": case["id"], "passed": case.get("expect_local_rejection", False),
                         "rejected_by": "source_condition_validator", "reason": str(exc)}
                report["negative_checks"].append(entry)
                save_json(report_path, report)
                print(json.dumps(entry, ensure_ascii=False), flush=True)
                continue
            verification, call = call_solar(args.project_dir, args.output_dir / "cache", "answer_verify",
                                           VERIFY_PROMPT, {**model_input, "candidate_answer": candidate_answer}, VERIFY_SCHEMA)
            passed = not validate_verification(verification)
            entry = {"case_id": case["id"], "passed": passed, "verification": verification, "call": call}
        except (GateError, ValueError, TypeError, KeyError, OSError, requests.RequestException) as exc:
            entry = {"case_id": case["id"], "passed": False,
                     "error_code": str(exc) if isinstance(exc, GateError) else type(exc).__name__}
        report["negative_checks"].append(entry)
        save_json(report_path, report)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        if "error_code" in entry:
            raise SystemExit(1)
    report["passed"] = sum(c["passed"] for c in report["cases"])
    report["negative_rejected"] = sum(c["passed"] for c in report["negative_checks"])
    save_json(report_path, report)
    print("검사 보고서:", report_path, flush=True)
    if report["passed"] != len(CASES) or report["negative_rejected"] != len(NEGATIVE_CASES):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
