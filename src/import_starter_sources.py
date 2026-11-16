"""검토한 스타터킷 6종 XML을 원본 그대로 가져옵니다. 네트워크/API 호출 없음.

로컬 식별정보·날짜·해시를 검사하고, 별도로 완료된 공식 판본 대조 결과를
보존합니다. 원문 내용 검수와 조문별 적용 판단은 별도 작업이며,
manifest의 검토 상태 표시는 아직 기존 파서의 처리 중단 장치가 아닙니다.
"""

from project_paths import PROJECT_ROOT

import argparse
import hashlib
import json
import os
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


AS_OF_DATE = "2026-10-01"
MANIFEST_NAME = "ordinances.manifest.json"
VERIFIED_NOTE = "로컬 식별정보·복사 무결성과 공식 현행·연혁 목록의 판본 대조 완료. 제목 변경 연혁의 완전 수집, 조문별 부분 시행·경과조치 및 원문 내용 검수는 별도 확인 대상. 상태 필드는 기존 파서의 실행 차단 장치가 아님."
# 이번에 읽어 대조한 제공 자료를 고정합니다. 새 판본은 별도로 검토해야 합니다.
SCOPE = (
    ("서울특별시 주차장 설치 및 관리 조례", "2000351", "2171833", "2026-10-01", "b6386e7889e5d094fee87cb670510c447a87ba39be09201c3cdc69788c4ff375"),
    ("서울특별시 동물보호 조례", "2000328", "2171829", "2026-10-01", "ed35e38b11f0fd5ee2d1b17f6b096d509496f32447967106e4687eaab3ced7fa"),
    ("서울특별시 자전거이용 활성화에 관한 조례", "2001697", "2171767", "2026-10-01", "9fbf35b3484fed1ee0e0c6e3486b89f49bae4c112ff01b0da7834dd749d03d7d"),
    ("서울특별시 도시공원 조례", "2000250", "2147709", "2026-07-13", "1af2923e532fef4f192d3501901652bbf781d4c23fe9055a2e71bcd1b55b202f"),
    ("서울특별시 청년 기본 조례", "2018334", "2054497", "2025-07-14", "62e21c6a53507d184e6f66d628b7addfd0d88d53e1821e338a5053a0c365ff2a"),
    ("서울특별시 금연환경 조성 및 간접흡연 피해방지조례", "215573", "2171803", "2026-10-01", "429170cc5bfcd47acb16e7e3205e48347d698aa182176f5d1c2dda2dd07a06d5"),
)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_json(raw):
    return json.loads(raw.decode("utf-8-sig"))


def one_text(parent, field):
    nodes = parent.findall(field)
    if len(nodes) != 1 or not (nodes[0].text or "").strip():
        raise ValueError(f"필수 필드는 정확히 하나여야 합니다: {field}")
    return nodes[0].text.strip()


def xml_date(value):
    if len(value) != 8 or not value.isascii() or not value.isdecimal():
        raise ValueError(f"잘못된 XML 날짜: {value}")
    return datetime.strptime(value, "%Y%m%d").date().isoformat()


def inspect_xml(raw, expected):
    name, ordin_id, seq, expected_date, expected_hash = expected
    root = ET.fromstring(raw)
    infos = root.findall("./자치법규기본정보")
    if len(infos) != 1:
        raise ValueError(f"{seq}: 기본정보는 정확히 하나여야 합니다.")
    info = infos[0]
    required = {
        "자치법규명": name,
        "지자체기관명": "서울특별시",
        "자치법규ID": ordin_id,
        "자치법규일련번호": seq,
        "자치법규종류": "C0001",
    }
    for field, value in required.items():
        if one_text(info, field) != value:
            raise ValueError(f"{seq}: {field}이 검토한 대상과 다릅니다.")
    effective = xml_date(one_text(info, "시행일자"))
    promulgation = xml_date(one_text(info, "공포일자"))
    if effective > AS_OF_DATE or promulgation > AS_OF_DATE:
        raise ValueError(f"{seq}: 기준일 이후 시행/공포 문서입니다.")
    if effective != expected_date or promulgation != expected_date:
        raise ValueError(f"{seq}: 검토한 시행일/공포일과 다릅니다.")
    if sha256(raw) != expected_hash:
        raise ValueError(f"{seq}: 검토한 파일의 SHA-256과 다릅니다.")
    articles = [n for n in root.findall("./조문/조") if n.findtext("조문여부") == "Y"]
    if not articles:
        raise ValueError(f"{seq}: 실제 조문이 없습니다.")
    attachments = []
    keys = set()
    for node in root.findall("./별표단위"):
        key = node.get("별표키")
        if not key or key in keys:
            raise ValueError(f"{seq}: 첨부 키가 없거나 중복되었습니다.")
        keys.add(key)
        # 무번호 0000, 중간 번호 생략, 별도도 그대로 보존합니다.
        attachments.append({
            "attachment_id": f"{seq}:{key}",
            "attachment_type": one_text(node, "별표구분"),
            "attachment_no": one_text(node, "별표번호"),
            "attachment_branch_no": node.findtext("별표가지번호", "00"),
            "title": node.findtext("별표제목", ""),
        })
    core = {
        "as_of_date": AS_OF_DATE,
        "effective_date": effective,
        "promulgation_date": promulgation,
        "ordin_id": ordin_id,
        "ordin_seq": seq,
        "source_url": f"https://www.law.go.kr/ordinInfoP.do?ordinSeq={seq}",
        "content_hash": sha256(raw),
    }
    details = {
        "ordin_name": name,
        "organization": "서울특별시",
        "article_count_including_deleted": len(articles),
        "supplement_block_count": len(root.findall("./부칙/부칙내용")),
        "attachment_count": len(attachments),
        "attachments": attachments,
    }
    return core, details


def regular_bytes(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"일반 파일이 아닙니다: {path.name}")
    return path.read_bytes()


def preserve_version_review(previous, manifest, project_dir):
    """완료된 외부 판본 대조는 보고서·원본 연결을 검사한 뒤 보존합니다."""
    if previous.get("as_of_version_status") == "pending":
        return
    status = "verified_official_catalog"
    if previous.get("as_of_version_status") != status:
        raise ValueError("알 수 없는 판본 검토 상태입니다.")
    ref = previous.get("as_of_verification", {})
    expected_path = f"data/raw/version_checks/{AS_OF_DATE}/version_report.json"
    if set(ref) != {"report_file", "report_content_hash"} or ref["report_file"] != expected_path:
        raise ValueError("판본 검증 보고서 참조가 잘못되었습니다.")
    path = (project_dir / expected_path).resolve()
    if not path.is_relative_to(project_dir):
        raise ValueError("검증 보고서가 프로젝트 밖을 가리킵니다.")
    raw = regular_bytes(path)
    if sha256(raw) != ref["report_content_hash"]:
        raise ValueError("판본 검증 보고서의 해시가 다릅니다.")
    report = read_json(raw)
    if (report.get("status") != status or report.get("as_of_date") != AS_OF_DATE
            or report.get("source_review") != "pending" or report.get("production_ready") is not False
            or report.get("evidence_file") != "version_evidence.json"):
        raise ValueError("판본 검증 보고서의 범위/상태가 다릅니다.")
    if sha256(regular_bytes(path.parent / "version_evidence.json")) != report.get("evidence_content_hash"):
        raise ValueError("공식 조회 근거 파일의 해시가 다릅니다.")
    checked = report.get("ordinances", [])
    by_seq = {item["ordin_seq"]: item for item in checked}
    if len(checked) != len(by_seq) or set(by_seq) != {r["ordin_seq"] for r in manifest["ordinances"]}:
        raise ValueError("검증 보고서의 조례 집합이 다릅니다.")
    for record in manifest["ordinances"]:
        result = by_seq[record["ordin_seq"]]
        keys = ("ordin_id", "ordin_seq", "ordin_name", "content_hash", "metadata_content_hash", "effective_date", "promulgation_date", "source_url")
        if any(result.get(key) != record[key] for key in keys) or result.get("as_of_version_status") != status:
            raise ValueError("검증 보고서와 현재 원본/metadata가 다릅니다.")
        record["as_of_version_status"] = status
    manifest["as_of_version_status"] = status
    manifest["as_of_verification"] = dict(ref)
    manifest["review_note"] = VERIFIED_NOTE


def prepare(source_dir, project_dir):
    """전체 검사를 통과한 경우에만 쓰기 계획을 반환합니다. 여기서는 쓰지 않음."""
    source_dir = source_dir.resolve(strict=True)
    project_dir = project_dir.resolve(strict=True)
    raw_dir = project_dir / "data" / "raw"
    if not raw_dir.resolve().is_relative_to(project_dir):
        raise ValueError("저장 경로가 프로젝트 밖을 가리킵니다.")
    if source_dir == raw_dir.resolve():
        raise ValueError("제공 원본 폴더와 프로젝트 저장 폴더가 같을 수 없습니다.")
    expected_names = {f"{item[2]}.xml" for item in SCOPE}
    actual_names = {p.name for p in source_dir.iterdir() if p.suffix.lower() == ".xml"}
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise ValueError(f"원본 6종 목록 불일치. 누락={missing}, 예상 밖={extra}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records, pending, reused = [], [], 0
    for expected in SCOPE:
        seq = expected[2]
        source = source_dir / f"{seq}.xml"
        raw = regular_bytes(source)
        core, details = inspect_xml(raw, expected)
        target = raw_dir / source.name
        metadata_path = target.with_suffix(".metadata.json")
        if target.exists() != metadata_path.exists():
            raise ValueError(f"{seq}: XML/metadata 중 하나만 있습니다. 불완전한 저장을 확인하세요.")
        if target.exists():
            if regular_bytes(target) != raw:
                raise ValueError(f"{seq}: 기존 XML이 달라 덮어쓰지 않습니다.")
            metadata_raw = regular_bytes(metadata_path)
            metadata = read_json(metadata_raw)
            if not isinstance(metadata, dict) or any(metadata.get(k) != v for k, v in core.items()):
                raise ValueError(f"{seq}: 기존 metadata의 출처/날짜/해시가 다릅니다.")
            reused += 1
        else:
            metadata = {
                **core,
                "fetched_at": None,  # 제공 파일의 원격 수집 시각은 알 수 없습니다.
                "imported_at": now,
                "acquisition_method": "local_starter_kit",
                "import_source_file": str(source),
                "as_of_version_status": "pending",
                "source_review": "pending",
            }
            metadata_raw = json_bytes(metadata)
            pending.extend([(target, raw), (metadata_path, metadata_raw)])
        records.append({
            **core, **details,
            "raw_file": f"data/raw/{target.name}",
            "metadata_file": f"data/raw/{metadata_path.name}",
            "metadata_content_hash": sha256(metadata_raw),
            "input_content_hash": sha256(raw),
            "as_of_version_status": "pending",
            "source_review": "pending",
        })

    manifest_path = raw_dir / MANIFEST_NAME
    previous = read_json(regular_bytes(manifest_path)) if manifest_path.exists() else None
    if previous is not None and not isinstance(previous, dict):
        raise ValueError("기존 원본 목록의 형식이 잘못되었습니다.")
    manifest = {
        "schema_version": 1,
        "as_of_date": AS_OF_DATE,
        "created_at": previous.get("created_at", now) if previous else now,
        "validation_status": "local_consistency_verified",
        "validation_scope": ["exact_scope_and_identity", "date_fields", "reviewed_file_hash", "destination_byte_identity"],
        "as_of_version_status": "pending",
        "source_review": "pending",
        "production_ready": False,
        "review_note": "로컬 파일의 식별정보·날짜·복사 일치 확인. 기준일 최신 유효 판본과 원문 내용 검수는 미완료. 상태 필드는 기존 파서의 실행 차단 장치가 아님.",
        "ordinance_count": len(records),
        "article_count_including_deleted": sum(r["article_count_including_deleted"] for r in records),
        "supplement_block_count": sum(r["supplement_block_count"] for r in records),
        "attachment_count": sum(r["attachment_count"] for r in records),
        "ordinances": records,
    }
    if previous is not None:
        preserve_version_review(previous, manifest, project_dir)
    if previous is not None and previous != manifest:
        raise ValueError("기존 원본 목록이 현재 입력·metadata·검토 상태와 다릅니다. 자동 교체하지 않습니다.")
    if previous is None:
        pending.append((manifest_path, json_bytes(manifest)))
    return manifest, pending, reused


def write_new(path, raw):
    """완성된 임시파일을 기존 경로를 덮지 않는 방식으로 공개합니다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            # Windows의 rename은 대상이 있으면 실패합니다(replace와 다름).
            # 일반 파일 이동을 사용해 하드링크 권한에 의존하지 않습니다.
            os.rename(temp, path)
        else:
            os.link(temp, path)  # POSIX rename은 덮어쓰므로 배타적 링크를 사용.
    finally:
        temp.unlink(missing_ok=True)
    if regular_bytes(path) != raw:
        raise ValueError(f"저장 후 바이트 검사가 실패했습니다: {path.name}")


def run(source_dir, project_dir, check_only=False):
    manifest, pending, reused = prepare(source_dir, project_dir)
    if not check_only:
        # XML·metadata를 먼저 저장하고 완료 목록은 마지막에 저장합니다.
        # 다중 파일 전체가 한 트랜잭션은 아닙니다. 중간 실패 시 불완전 쌍은 재실행에서 중단합니다.
        for path, raw in pending:
            if path.name == MANIFEST_NAME:
                for record in manifest["ordinances"]:
                    for field, hash_field in (("raw_file", "content_hash"), ("metadata_file", "metadata_content_hash")):
                        if sha256(regular_bytes(project_dir / record[field])) != record[hash_field]:
                            raise ValueError("완료 목록 저장 전 파일이 변경되었습니다.")
            write_new(path, raw)
    print("모드:", "사전 확인 (저장 안 함)" if check_only else "로컬 원본 가져오기")
    print(f"조례: {manifest['ordinance_count']}종 / 기존 재사용: {reused}종 / 신규: {len(SCOPE) - reused}종")
    for record in manifest["ordinances"]:
        print(f"- {record['ordin_name']} | 시행 {record['effective_date']} | 조문 {record['article_count_including_deleted']} | 첨부 {record['attachment_count']}")
    print(f"합계: 조문 {manifest['article_count_including_deleted']} / 부칙 블록 {manifest['supplement_block_count']} / 첨부 {manifest['attachment_count']}")
    print(f"기준일 판본 대조: {manifest['as_of_version_status']} / 원문 내용 검수: pending")
    print("목록:", project_dir.resolve() / "data" / "raw" / MANIFEST_NAME)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="스타터킷 data/raw 폴더")
    parser.add_argument("--project-dir", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--check-only", action="store_true", help="검증·출력만 하고 저장하지 않음")
    args = parser.parse_args()
    try:
        run(args.source_dir, args.project_dir, args.check_only)
    except (OSError, ValueError, ET.ParseError) as exc:
        raise SystemExit(f"원본 가져오기 중단: {exc}") from None


if __name__ == "__main__":
    main()
