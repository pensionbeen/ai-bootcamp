"""저장된 공식 API 근거를 원본 목록과 대조하는 오프라인 판본 검사.

현재 ID 조회와 선택본도 일치하는 경우만 완료 처리합니다. 과거 다른 제목의
전체 연혁이나 조문별 적용·경과조치를 자동 판정하는 도구는 아닙니다.
"""
import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

LIST_API = "https://www.law.go.kr/DRF/lawSearch.do"
BODY_API = "https://www.law.go.kr/DRF/lawService.do"
STATUS = "verified_official_catalog"
REVIEW_NOTE = "로컬 식별정보·복사 무결성과 공식 현행·연혁 목록의 판본 대조 완료. 제목 변경 연혁의 완전 수집, 조문별 부분 시행·경과조치 및 원문 내용 검수는 별도 확인 대상. 상태 필드는 기존 파서의 실행 차단 장치가 아님."


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def parse_day(value):
    if not isinstance(value, str) or len(value) != 8 or not value.isascii() or not value.isdecimal():
        raise ValueError("공식 목록에 유효하지 않은 날짜가 있습니다.")
    return datetime.strptime(value, "%Y%m%d").date().isoformat()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_record(record, entry, as_of):
    require(entry["ordin_id"] == record["ordin_id"] and entry["selected_seq"] == record["ordin_seq"], "선택한 조례 ID/판본이 다릅니다.")
    require(entry["name"] == record["ordin_name"], "근거의 조례명이 다릅니다.")
    require(entry["local_xml_hash"] == record["content_hash"], "수집 근거의 XML 해시가 다릅니다.")
    catalogs = entry["catalogs"]
    require(len(catalogs) == 2 and {c["nw"] for c in catalogs} == {1, 2}, "현행·연혁 조회가 모두 있어야 합니다.")
    merged, current_rows = {}, []
    for catalog in catalogs:
        rows = []
        require(catalog["pages"], "수집한 목록 페이지가 없습니다.")
        for page_number, page in enumerate(catalog["pages"], start=1):
            expected_params = {"target": "ordin", "type": "XML", "nw": catalog["nw"], "query": record["ordin_name"], "org": "6110000", "knd": "30001", "display": 100, "page": page_number, "sort": "efdes"}
            require(page["endpoint"] == LIST_API and page["request_parameters"] == expected_params, "공식 목록 요청 조건이 다릅니다.")
            require(page["page"] == page_number and page["total_count"] == catalog["total_count"], "페이지 번호/전체 건수가 일관되지 않습니다.")
            rows.extend(page["records"])
        require(len(rows) == catalog["total_count"], "목록을 마지막 페이지까지 수집하지 않았습니다.")
        seqs = [row["자치법규일련번호"] for row in rows]
        require(all(seqs) and len(seqs) == len(set(seqs)), "목록의 일련번호가 없거나 중복되었습니다.")
        selected = [row for row in rows if row["자치법규ID"] == record["ordin_id"]]
        require(selected == catalog["matched_id_records"], "ID로 다시 고른 목록이 저장 근거와 다릅니다.")
        if catalog["nw"] == 1:
            current_rows = selected
        for row in selected:
            require(row["지자체기관명"] == "서울특별시" and row["자치법규종류"] == "조례", "기관/종류가 다른 판본입니다.")
            # 같은 ID의 과거 판본에는 이름 변경/띄어쓰기 차이가 있을 수 있습니다.
            # 선택할 기준일 후보의 제목은 아래에서 정확히 대조합니다.
            require(row["자치법규명"], "과거 판본의 제목이 없습니다.")
            require(row["제개정구분명"] and row["공포번호"], "개정 구분/공포번호가 없습니다.")
            parse_day(row["시행일자"])
            parse_day(row["공포일자"])
            seq = row["자치법규일련번호"]
            require(seq not in merged or merged[seq] == row, "같은 일련번호에 상충하는 목록 정보가 있습니다.")
            merged[seq] = row
    require(len(current_rows) == 1 and current_rows[0]["자치법규일련번호"] == record["ordin_seq"], "현행 목록과 선택본이 다릅니다. 과거 판본의 별도 검토가 필요합니다.")
    eligible = [r for r in merged.values() if parse_day(r["시행일자"]) <= as_of and parse_day(r["공포일자"]) <= as_of]
    require(eligible, "기준일 이내 시행·공포된 후보가 없습니다.")
    require(not any("폐지" in r["제개정구분명"] for r in eligible), "기준일 이내 폐지 관련 기록이 있어 별도 검토가 필요합니다.")
    latest_date = max(parse_day(r["시행일자"]) for r in eligible)
    latest = [r for r in eligible if parse_day(r["시행일자"]) == latest_date]
    require(len(latest) == 1, "최종 시행일이 같은 복수 판본이 있습니다. 번호로 임의 선택하지 않습니다.")
    candidate = latest[0]
    require(candidate["자치법규일련번호"] == record["ordin_seq"], "기준일 후보가 저장한 판본과 다릅니다.")
    require(candidate["자치법규명"] == record["ordin_name"], "기준일 후보의 조례명이 다릅니다.")
    require(not any(parse_day(r["공포일자"]) > record["promulgation_date"] for r in eligible), "선택본보다 나중에 공포된 기준일 내 판본이 있습니다.")
    require(latest_date == record["effective_date"] and parse_day(candidate["공포일자"]) == record["promulgation_date"], "선택본의 날짜와 목록의 날짜가 다릅니다.")
    current = entry["current_id_body"]
    require(current["endpoint"] == BODY_API and current["request_parameters"] == {"target": "ordin", "type": "XML", "ID": record["ordin_id"]}, "현재 본문의 ID 요청 조건이 다릅니다.")
    info = current["info"]
    expected = {"자치법규명": record["ordin_name"], "지자체기관명": "서울특별시", "자치법규ID": record["ordin_id"], "자치법규일련번호": record["ordin_seq"], "시행일자": record["effective_date"].replace("-", ""), "공포일자": record["promulgation_date"].replace("-", ""), "공포번호": candidate["공포번호"], "자치법규종류": "C0001"}
    require(all(info.get(k) == v for k, v in expected.items()), "현행 ID 본문과 선택본의 기본정보가 다릅니다.")
    require(current["response_sha256"] == record["content_hash"], "현행 ID 본문과 로컬 XML이 바이트 단위로 다릅니다.")
    return {
        "ordin_id": record["ordin_id"], "ordin_seq": record["ordin_seq"],
        "ordin_name": record["ordin_name"], "content_hash": record["content_hash"],
        "metadata_content_hash": record["metadata_content_hash"],
        "effective_date": record["effective_date"], "promulgation_date": record["promulgation_date"],
        "source_url": record["source_url"], "as_of_version_status": STATUS,
        "unique_catalog_versions": len(merged), "as_of_candidates": len(eligible),
        "observed_historical_titles": sorted({r["자치법규명"] for r in merged.values()}),
        "newer_eligible_versions": 0, "latest_date_ties": 1,
        "current_body_byte_equal": True,
        "previous_version": next(iter(sorted((r for r in eligible if r["자치법규일련번호"] != record["ordin_seq"]), key=lambda r: (r["시행일자"], r["공포일자"]), reverse=True)), None),
    }


def verify(manifest, evidence, manifest_raw, evidence_raw, project):
    require(manifest["as_of_date"] == evidence["as_of_date"], "기준일이 다릅니다.")
    require(digest(manifest_raw) == evidence["manifest_content_hash_before_check"], "조회 이후 원본 목록이 변경됐습니다.")
    records = manifest["ordinances"]
    require(len(records) == manifest["ordinance_count"] == 6, "6종 원본 목록이 필요합니다.")
    entries = {e["ordin_id"]: e for e in evidence["ordinances"]}
    require(len(entries) == len(evidence["ordinances"]) == 6 and set(entries) == {r["ordin_id"] for r in records}, "검증 근거의 조례 집합이 다릅니다.")
    results = []
    for record in records:
        for field, hash_field in (("raw_file", "content_hash"), ("metadata_file", "metadata_content_hash")):
            path = (project / record[field]).resolve()
            require(path.is_relative_to(project.resolve()), "원본 경로가 프로젝트 밖을 가리킵니다.")
            require(digest(path.read_bytes()) == record[hash_field], "원본 또는 metadata가 변경됐습니다.")
        results.append(check_record(record, entries[record["ordin_id"]], manifest["as_of_date"]))
    return {
        "schema_version": 1, "as_of_date": manifest["as_of_date"],
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": STATUS, "input_manifest_content_hash": digest(manifest_raw),
        "evidence_file": "version_evidence.json", "evidence_content_hash": digest(evidence_raw),
        "verification_scope": "제목 검색의 현행·연혁 전체 페이지 + 정확한 ID/기관 대조 + 기준일 내 단일 최종 시행일 후보 + 현행 ID 본문의 원본 바이트 일치",
        "limitations": ["제목이 달랐던 과거 연혁 전체를 수집했다는 뜻은 아님", "조문별 부분 시행·적용례·경과조치 해석은 별도 검수", "원문 추출·청킹·답변 정확도 검수는 미완료"],
        "source_review": "pending", "production_ready": False, "ordinances": results,
    }


def prepare_files(project, evidence_path, output):
    manifest_raw = (project / "data/raw/ordinances.manifest.json").read_bytes()
    evidence_raw = evidence_path.read_bytes()
    manifest, evidence = json.loads(manifest_raw), json.loads(evidence_raw)
    report = verify(manifest, evidence, manifest_raw, evidence_raw, project)
    report_raw = json_bytes(report)
    updated = copy.deepcopy(manifest)
    updated["as_of_version_status"] = STATUS
    updated["as_of_verification"] = {"report_file": f"data/raw/version_checks/{manifest['as_of_date']}/version_report.json", "report_content_hash": digest(report_raw)}
    updated["review_note"] = REVIEW_NOTE
    for record in updated["ordinances"]:
        record["as_of_version_status"] = STATUS
    output.mkdir(parents=True, exist_ok=True)
    for name, raw in (("manifest.before.json", manifest_raw), ("version_evidence.json", evidence_raw), ("version_report.json", report_raw), ("manifest.verified.json", json_bytes(updated))):
        (output / name).write_bytes(raw)
    for record in report["ordinances"]:
        print(f"{record['ordin_name']} | {record['unique_catalog_versions']}개 판본 대조 | 선택 {record['ordin_seq']} 일치")
    print("판본 대조 완료: 6/6 / 원문 내용 검수: pending / 운영 준비: false")
    print("검토용 결과 저장:", output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare_files(args.project_dir.resolve(), args.evidence, args.output_dir)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise SystemExit(f"판본 대조 중단: {exc}") from None
