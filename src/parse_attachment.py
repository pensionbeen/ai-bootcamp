"""14개 첨부의 Document Parse 결과 확보. 기존 결과는 읽기만 하고 누락분만 호출합니다."""

import argparse
import io
import html
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values

import download_attachment as originals
from build_ordinance_corpus import digest, json_bytes, require, source_import
from project_paths import PROJECT_ROOT
from request_lock import RequestInProgress, request_lock

FOLDER = "data/processed/attachments/2026-10-01"
ENDPOINT = "https://api.upstage.ai/v1/document-digitization"
OPTIONS = {"model": "document-parse", "mode": "enhanced", "ocr": "force",
           "output_formats": json.dumps(["html", "text"]), "coordinates": "true",
           "base64_encoding": json.dumps(["table", "figure", "chart"])}
LEGACY_OPTIONS = {"model": "document-parse", "mode": "enhanced", "ocr": "force",
                  "output_formats": json.dumps(["html"]), "coordinates": "true"}
LEGACY = {"2171833:22740723": "data/processed/attachments/2171833_22740723.document_parse.json",
          "2171833:22740725": "data/processed/attachments/2171833_22740725_Parse.json"}


def result_path(item):
    return f"{FOLDER}/{item['attachment']['attachment_id'].replace(':', '_')}.document_parse.json"


def validate_response(response):
    require(isinstance(response, dict) and isinstance(response.get("model"), str)
            and response["model"].startswith("document-parse"), "Parse 모델 식별정보가 없습니다")
    pages = response.get("usage", {}).get("pages")
    require(type(pages) is int and 0 < pages <= 100, "Parse 페이지 수가 잘못됐습니다")
    elements = response.get("elements")
    require(isinstance(elements, list) and elements, "Parse 요소가 없습니다")
    require(isinstance(response.get("content", {}).get("html"), str) and response["content"]["html"].strip(),
            "Parse 전체 HTML이 없습니다")
    seen = set()
    for element in elements:
        require(isinstance(element, dict), "Parse 요소가 객체가 아닙니다")
        page, ident = element.get("page"), element.get("id")
        require(type(page) is int and 1 <= page <= pages and type(ident) is int and ident >= 0,
                "Parse 요소의 페이지/ID가 잘못됐습니다")
        require((page, ident) not in seen, "Parse 요소 ID가 같은 페이지에서 중복됐습니다")
        seen.add((page, ident))
        require(isinstance(element.get("category"), str) and element["category"], "Parse 요소 유형 누락")
        content = element.get("content", {})
        require(isinstance(content, dict) and isinstance(content.get("html"), str), "Parse 요소 HTML 누락")
        coordinates = element.get("coordinates")
        require(isinstance(coordinates, list) and len(coordinates) == 4, "Parse 좌표 누락")
        for point in coordinates:
            require(isinstance(point, dict) and all(type(point.get(k)) in (int, float)
                    and math.isfinite(point[k]) and 0 <= point[k] <= 1 for k in ("x", "y")),
                    "Parse 좌표 범위 오류")
        if element["category"] == "table":
            markup = content["html"].lower()
            require("<table" in markup and "</table>" in markup
                    and re.search(r"<tr(?:\s|>)", markup) and re.search(r"<t[dh](?:\s|>)", markup)
                    and (html.unescape(re.sub(r"<[^>]*>", "", markup)).strip() or "<img" in markup),
                    "표 요소의 HTML 구조가 없거나 표가 비어 있습니다")
    # 빈 페이지일 수 있으므로 요소가 없는 페이지를 성공/실패로 단정하지 않습니다.
    return {"pages": pages, "elements": len(elements),
            "tables": sum(e["category"] == "table" for e in elements),
            "figures": sum(e["category"] in ("figure", "chart") for e in elements),
            "pages_without_elements": sorted(set(range(1, pages + 1)) - {e["page"] for e in elements})}


def read_parse(project, item, collection_hash):
    ident = item["attachment"]["attachment_id"]
    relative = LEGACY.get(ident, result_path(item))
    path = originals.safe_path(project, relative)
    if ident in LEGACY:
        require(not originals.safe_path(project, result_path(item)).exists(), "기존/신규 Parse 결과가 중복됐습니다")
    if not path.exists():
        require(ident not in LEGACY, "재사용할 기존 Parse 파일이 없습니다")
        return None
    raw = path.read_bytes()
    record = json.loads(raw)
    meta = json.loads(originals.safe_path(project, item["metadata_path"]).read_bytes())
    hwp_hash = meta["download"]["content_hash"]
    if ident == "2171833:22740723":
        require(record["input_content_hash"] == hwp_hash and record["source"] == meta
                and record["request_options"] == LEGACY_OPTIONS, "기존 API Parse 출처/설정 불일치")
        binding = "legacy_api_input_hash"
        response = record["response"]
        support_files = {}
    elif ident == "2171833:22740725":
        # 이 파일명은 예전 수동 내보내기 기록과 연결합니다. 서버 입력을 증명한 것으로 격상하지 않습니다.
        evidence_path = "data/processed/attachments/2171833_22740725.evidence.json"
        evidence_raw = originals.safe_path(project, evidence_path).read_bytes()
        evidence = json.loads(evidence_raw)
        require(evidence["attachment_id"] == ident and evidence["input_content_hash"] == hwp_hash
                and evidence["input_binding_status"] == "manual_export"
                and evidence["result_files"]["parse"]["content_hash"] == digest(raw),
                "기존 수동 Parse 참조 기록 불일치")
        binding = "manual_export_unverified"
        response, support_files = record, {evidence_path: digest(evidence_raw)}
    else:
        require(record["attachment_id"] == ident and record["input_content_hash"] == hwp_hash
                and record["metadata_content_hash"] == digest(originals.safe_path(project, item["metadata_path"]).read_bytes())
                and record["collection_content_hash"] == collection_hash
                and record["request_options"] == OPTIONS and record["endpoint"] == ENDPOINT
                and record["input_binding_status"] == "api_uploaded_bytes", "Parse 입력/설정 연결 불일치")
        binding, response, support_files = record["input_binding_status"], record["response"], {}
    stats = validate_response(response)
    return {"file": relative, "content_hash": digest(raw), "input_binding_status": binding,
            "support_files": support_files, "response": response, **stats}


def inventory(project):
    originals.check_collection(project, require_complete=True)
    _, items = originals.catalog(project)
    collection_hash = digest(originals.safe_path(project, originals.MANIFEST).read_bytes())
    parsed = [read_parse(project, item, collection_hash) for item in items]
    return collection_hash, items, parsed


def check(project):
    _, items, parsed = inventory(project)
    return {"total": len(items), "parsed": sum(p is not None for p in parsed),
            "missing": sum(p is None for p in parsed),
            "manual_input_binding_pending": sum(p is not None and p["input_binding_status"] == "manual_export_unverified" for p in parsed)}


def call_api(raw, filename, api_key):
    try:
        with requests.post(ENDPOINT, headers={"Authorization": f"Bearer {api_key}"},
                           files={"document": (filename, io.BytesIO(raw), "application/x-hwp")},
                           data=OPTIONS, timeout=(10, 180), allow_redirects=False) as response:
            require(response.status_code == 200, f"Document Parse HTTP {response.status_code}; 자동 재시도하지 않습니다")
            require(len(response.content) <= 100 * 1024 * 1024, "Parse 응답 크기 제한 초과")
            return response.json()
    except (requests.RequestException, requests.exceptions.JSONDecodeError):
        raise ValueError("Document Parse 연결/응답 오류. 처리 여부 확인 전 자동 재호출하지 않습니다") from None


def parse_all(project, env_file=None, limit=None, retry_unresolved=False):
    project = Path(project).resolve(strict=True)
    with request_lock(originals.safe_path(project, FOLDER + "/.parse.lock")):
        collection_hash, items, parsed = inventory(project)
        collected = {entry["attachment"]["attachment_id"]: entry for entry in
                     json.loads(originals.safe_path(project, originals.MANIFEST).read_bytes())["attachments"]}
        missing = [(i, p) for i, p in zip(items, parsed) if p is None]
        if not missing:
            return {**check(project), "called": 0}
        # 미완료 시도는 자동 재과금을 피하기 위해 전체 요청 전에 확인합니다.
        for item, _ in missing:
            attempt = originals.safe_path(project, result_path(item) + ".attempt.json")
            if attempt.exists():
                prior = json.loads(attempt.read_bytes())
                require(prior["input_content_hash"] == json.loads(originals.safe_path(project, item["metadata_path"]).read_bytes())["download"]["content_hash"]
                        and prior["request_options"] == OPTIONS and prior["collection_content_hash"] == collection_hash,
                        "미완료 Parse 시도의 입력/설정이 다릅니다")
                require(retry_unresolved, "응답 저장이 끝나지 않은 API 시도가 있습니다. 처리 여부 확인 후 --retry-unresolved로 재시도하세요")
        api_key = os.getenv("UPSTAGE_API_KEY") or dotenv_values(env_file or project / ".env").get("UPSTAGE_API_KEY")
        require(bool(api_key), "UPSTAGE_API_KEY 설정이 없습니다")
        called = 0
        for item, _ in missing[:limit]:
            path = originals.safe_path(project, item["file_path"])
            raw = path.read_bytes()
            meta_raw = originals.safe_path(project, item["metadata_path"]).read_bytes()
            require(digest(raw) == json.loads(meta_raw)["download"]["content_hash"], "전송 직전 첨부 해시 불일치")
            expected = collected[item["attachment"]["attachment_id"]]
            require(digest(raw) == expected["content_hash"] and digest(meta_raw) == expected["metadata_content_hash"],
                    "전송 직전 수집 완료 목록과 원본/출처가 다릅니다")
            record = {"attachment_id": item["attachment"]["attachment_id"], "input_content_hash": digest(raw),
                      "metadata_content_hash": digest(meta_raw), "collection_content_hash": collection_hash,
                      "input_binding_status": "api_uploaded_bytes", "endpoint": ENDPOINT,
                      "request_options": OPTIONS, "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            output = originals.safe_path(project, result_path(item))
            attempt = originals.safe_path(project, result_path(item) + ".attempt.json")
            if not attempt.exists():
                source_import.write_new(attempt, json_bytes(record))
            label = "별표 (번호 없음)" if item["attachment"]["attachment_no"] == "0000" else item["attachment"]["label"]
            print(f"Parse 요청: {item['ordin_name']} / {label}", flush=True)
            response = call_api(raw, path.name, api_key)
            # 응답은 검증 실패 시에도 보존하여 같은 파일을 무심코 다시 과금하지 않습니다.
            record["response"] = response
            record["received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            source_import.write_new(output, json_bytes(record))
            stats = validate_response(response)
            require(digest(path.read_bytes()) == record["input_content_hash"]
                    and digest(originals.safe_path(project, item["metadata_path"]).read_bytes()) == record["metadata_content_hash"]
                    and digest(originals.safe_path(project, originals.MANIFEST).read_bytes()) == collection_hash,
                    "Parse 처리 중 입력이 변경됐습니다")
            called += 1
            print(f"저장: {stats['pages']}페이지 / {stats['elements']}요소 / 표 {stats['tables']} / 그림 {stats['figures']}", flush=True)
        return {**check(project), "called": called}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--env-file", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--parse", action="store_true")
    action.add_argument("--check-only", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retry-unresolved", action="store_true")
    args = parser.parse_args(argv)
    try:
        require(args.limit is None or args.limit > 0, "limit은 양수여야 합니다")
        result = parse_all(args.project_dir, args.env_file, args.limit, args.retry_unresolved) if args.parse else check(args.project_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RequestInProgress) as exc:
        print(f"Parse 중단: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
