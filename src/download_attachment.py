"""검증된 6종 XML의 첨부 원본 수집. 기본 실행은 오프라인 검사입니다."""

import argparse
import json
import re
import struct
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests

import build_ordinance_corpus as corpus
from extract_attachments import extract_attachments
from project_paths import PROJECT_ROOT
from request_lock import RequestInProgress, request_lock

MANIFEST = "data/raw/attachments/collection.manifest.json"
MAX_BYTES = 20 * 1024 * 1024
SOURCE_KEYS = ("ordin_id", "ordin_seq", "as_of_date", "effective_date",
               "promulgation_date", "source_url", "content_hash")


def safe_path(project, relative):
    project = Path(project).resolve(strict=True)
    path = project / relative
    corpus.require(path.resolve().is_relative_to(project) and not path.is_symlink(),
                   "첨부 경로가 프로젝트 밖이거나 심볼릭 링크입니다")
    return path


def normalize_url(url):
    parts = urlsplit(url or "")
    corpus.require(parts.scheme in ("http", "https") and parts.netloc == "www.law.go.kr"
                   and parts.path == "/flDownload.do" and not parts.fragment,
                   "공식 첨부 다운로드 주소가 아닙니다")
    query = parse_qs(parts.query, keep_blank_values=True)
    corpus.require(set(query) == {"flSeq", "flNm"}
                   and len(query["flSeq"]) == len(query["flNm"]) == 1
                   and re.fullmatch(r"[0-9]+", query["flSeq"][0]) and query["flNm"][0],
                   "첨부 다운로드 주소의 식별정보가 잘못됐습니다")
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


def validate_hwp(content, content_type=""):
    """HWP 5의 컨테이너/헤더만 검사합니다. 문서 내용의 정확성을 보증하지 않습니다."""
    corpus.require(0 < len(content) <= MAX_BYTES, "첨부 파일 크기가 허용 범위 밖입니다")
    corpus.require(not any(t in content_type.lower() for t in ("html", "json", "text/plain")),
                   "HWP 대신 오류 응답을 받았습니다")
    corpus.require(len(content) >= 512 and content[:8] == bytes.fromhex("d0cf11e0a1b11ae1"),
                   "HWP 컨테이너 헤더가 아닙니다")
    version, byte_order, shift = struct.unpack_from("<HHH", content, 26)
    corpus.require(byte_order == 0xFFFE and (version, shift) in ((3, 9), (4, 12))
                   and len(content) % (1 << shift) == 0,
                   "HWP 컨테이너 크기 또는 섹터 정보가 잘못됐습니다")
    corpus.require(b"HWP Document File\x00" in content and "FileHeader".encode("utf-16le") in content,
                   "HWP 문서 헤더 표식이 없습니다")


def catalog(project):
    manifest, documents, inputs = corpus.load_inputs(Path(project))
    items, seen = [], set()
    for entry, raw in documents:
        attachments = extract_attachments(ET.fromstring(raw), entry)
        corpus.require(len(attachments) == entry["attachment_count"], "XML 첨부 수 불일치")
        for ordinal, attachment in enumerate(attachments, start=1):
            ident = attachment["attachment_id"]
            corpus.require(ident not in seen and re.fullmatch(r"[0-9]+:[0-9]+", ident),
                           "중복 또는 잘못된 첨부 ID")
            seen.add(ident)
            corpus.require(attachment["file_type"] == "hwp", "지원하지 않는 첨부 형식: 검토 필요")
            normalize_url(attachment["download_url"])
            stem = f"data/raw/attachments/{entry['ordin_seq']}_{attachment['attachment_key']}"
            items.append({"ordin_name": entry["ordin_name"], "ordin_seq": entry["ordin_seq"],
                          "source": {key: entry[key] for key in SOURCE_KEYS},
                          "source_location": {"raw_file": entry["raw_file"],
                                              "xml_path": f"./별표단위[{ordinal}]"},
                          "attachment": attachment, "file_path": stem + ".hwp",
                          "metadata_path": stem + ".metadata.json"})
    corpus.require(len(items) == manifest["attachment_count"], "전체 첨부 수 불일치")
    return inputs, items


def validate_saved(project, item):
    path, meta_path = (safe_path(project, item[key]) for key in ("file_path", "metadata_path"))
    corpus.require(path.exists() == meta_path.exists(),
                   f"{item['attachment']['attachment_id']}: 원본/출처 파일 중 하나만 있습니다. 복구 검토가 필요합니다")
    if not path.exists():
        return None
    raw, meta_raw = path.read_bytes(), meta_path.read_bytes()
    metadata = json.loads(meta_raw)
    corpus.require(metadata["ordin_name"] == item["ordin_name"]
                   and all(metadata["metadata"].get(k) == v for k, v in item["source"].items())
                   and metadata["attachment"] == item["attachment"], "기존 첨부의 XML 출처가 다릅니다")
    download = metadata["download"]
    corpus.require(download["local_path"] == item["file_path"]
                   and download["content_hash"] == corpus.digest(raw)
                   and download["size_bytes"] == len(raw), "기존 첨부 경로·크기·해시 불일치")
    corpus.require(normalize_url(download["requested_url"]) == normalize_url(item["attachment"]["download_url"]),
                   "기존 첨부 요청 주소 불일치")
    if "final_url" in download:
        corpus.require(normalize_url(download["final_url"]) == normalize_url(download["requested_url"]),
                       "첨부 응답 주소 불일치")
    validate_hwp(raw, download.get("content_type", ""))
    return {"file_path": item["file_path"], "metadata_path": item["metadata_path"],
            "content_hash": corpus.digest(raw), "metadata_content_hash": corpus.digest(meta_raw),
            "size_bytes": len(raw)}


def inspect(project):
    inputs, items = catalog(project)
    # 마지막 대상에 충돌이 있어도 첫 다운로드 전에 중단합니다.
    records = [validate_saved(project, item) for item in items]
    return inputs, items, records


def collection_bytes(inputs, items, records):
    corpus.require(len(items) == len(records) and all(records), "첨부 수집이 완료되지 않았습니다")
    return corpus.json_bytes({"pipeline_version": "attachment-originals-v1",
        "as_of_date": corpus.source_import.AS_OF_DATE, "input_files": inputs,
        "raw_collection_complete": True, "source_review": "pending", "production_ready": False,
        "document_content_review": "pending", "article_linking": "pending",
        "format_check": "HWP container/header/size only; full document rendering not checked",
        "attachment_count": len(items),
        "attachments": [{**item, **record,
            "display_label": (item["attachment"]["attachment_type"] + " (번호 없음)"
                              if item["attachment"]["attachment_no"] == "0000"
                              else item["attachment"]["label"])} for item, record in zip(items, records)]})


def check_collection(project, require_complete=False):
    inputs, items, records = inspect(project)
    path = safe_path(project, MANIFEST)
    if path.exists():
        corpus.require(path.read_bytes() == collection_bytes(inputs, items, records),
                       "첨부 수집 완료 목록과 현재 원본·출처가 다릅니다")
    elif require_complete:
        raise ValueError("첨부 수집 완료 목록이 없습니다")
    return {"total": len(items), "present": sum(r is not None for r in records),
            "missing": sum(r is None for r in records), "manifest_verified": path.exists()}


def fetch_hwp(url):
    requested = normalize_url(url)
    # 자동 리디렉션을 막아 다른 주소의 문서를 같은 첨부로 저장하지 않습니다.
    started = time.monotonic()
    try:
        with requests.get(requested, timeout=(10, 60), stream=True, allow_redirects=False,
                          headers={"Accept-Encoding": "identity"}) as response:
            corpus.require(response.status_code == 200, f"첨부 다운로드 HTTP {response.status_code}")
            corpus.require(normalize_url(response.url) == requested, "첨부 응답 주소 불일치")
            size, pieces = 0, []
            for piece in response.iter_content(chunk_size=64 * 1024):
                size += len(piece)
                corpus.require(size <= MAX_BYTES and time.monotonic() - started < 90,
                               "첨부 다운로드 크기/시간 제한 초과")
                pieces.append(piece)
            content = b"".join(pieces)
            declared = response.headers.get("Content-Length")
            if declared is not None and not response.headers.get("Content-Encoding"):
                corpus.require(declared.isdigit() and int(declared) == len(content), "다운로드 길이가 응답 헤더와 다릅니다")
            content_type = response.headers.get("Content-Type", "")
            validate_hwp(content, content_type)
            return content, {"requested_url": requested, "final_url": requested,
                             "content_type": content_type}
    except requests.RequestException:
        raise ValueError("첨부 다운로드 연결 실패. 완료된 파일은 재사용되므로 재실행할 수 있습니다") from None


def collect(project):
    project = Path(project).resolve(strict=True)
    with request_lock(safe_path(project, "data/raw/attachments/.collection.lock")):
        before, items, records = inspect(project)
        manifest_path = safe_path(project, MANIFEST)
        if manifest_path.exists():
            result = check_collection(project, require_complete=True)
            return {**result, "downloaded": 0, "reused": result["present"]}
        reused = sum(r is not None for r in records)
        for item, existing in zip(items, records):
            if existing is not None:
                continue
            label = (item["attachment"]["attachment_type"] + " (번호 없음)"
                     if item["attachment"]["attachment_no"] == "0000" else item["attachment"]["label"])
            print(f"수집: {item['ordin_name']} / {label}", flush=True)
            content, details = fetch_hwp(item["attachment"]["download_url"])
            validate_hwp(content, details.get("content_type", ""))
            metadata = {"ordin_name": item["ordin_name"], "metadata": item["source"],
                        "attachment": item["attachment"], "download": {**details,
                        "local_path": item["file_path"], "size_bytes": len(content),
                        "content_hash": corpus.digest(content),
                        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}}
            # 배타적 새 파일 저장. 쌍 저장 도중 중단되면 다음 실행에서 명시적으로 거부합니다.
            corpus.source_import.write_new(safe_path(project, item["file_path"]), content)
            corpus.source_import.write_new(safe_path(project, item["metadata_path"]), corpus.json_bytes(metadata))
        after, current_items, current_records = inspect(project)
        corpus.require(before == after and items == current_items, "수집 중 입력 원본이 변경됐습니다")
        for old, current in zip(records, current_records):
            corpus.require(old is None or old == current, "수집 중 기존 첨부가 변경됐습니다")
        corpus.source_import.write_new(manifest_path, collection_bytes(after, current_items, current_records))
        result = check_collection(project, require_complete=True)
        return {**result, "downloaded": len(items) - reused, "reused": reused}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=PROJECT_ROOT)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--collect", action="store_true", help="누락된 원본만 공식 사이트에서 다운로드")
    action.add_argument("--check-only", action="store_true", help="현재 원본·출처 검사만 수행 (기본)")
    args = parser.parse_args(argv)
    try:
        result = collect(args.project_dir) if args.collect else check_collection(args.project_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, RequestInProgress) as exc:
        print(f"첨부 수집/검사 중단: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
