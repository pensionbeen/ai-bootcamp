"""공식 현행/연혁 목록과 ID 조회 결과를 읽어 검증 근거로 저장합니다.

원본·metadata·manifest는 변경하지 않습니다. OC와 API 상세링크는 저장/출력하지 않습니다.
"""
import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values

LIST_API = "https://www.law.go.kr/DRF/lawSearch.do"
BODY_API = "https://www.law.go.kr/DRF/lawService.do"
FIELDS = ("자치법규명", "지자체기관명", "자치법규ID", "자치법규일련번호", "시행일자", "공포일자", "공포번호", "제개정구분명", "자치법규종류", "참조데이터구분")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fields(node):
    return {name: node.findtext(name, "").strip() for name in FIELDS}


def fetch(session, oc, endpoint, params, output):
    fingerprint = digest(json.dumps([endpoint, params], sort_keys=True, ensure_ascii=False).encode())
    cache = output / "responses" / f"{fingerprint}.json"
    if cache.exists():
        saved = read_json(cache)
        if saved["endpoint"] != endpoint or saved["request_parameters"] != params:
            raise ValueError("저장된 요청 근거가 현재 요청과 다릅니다.")
        return saved
    try:
        response = session.get(endpoint, params={"OC": oc, **params}, timeout=(10, 60))
    except requests.RequestException as exc:
        raise RuntimeError(f"법령 API 연결 실패: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise RuntimeError(f"법령 API HTTP {response.status_code}")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        raise RuntimeError("법령 API가 유효한 XML을 반환하지 않았습니다.") from None
    saved = {
        "endpoint": endpoint,
        "request_parameters": params,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "response_sha256": digest(response.content),
        "root_tag": root.tag,
    }
    if endpoint == LIST_API:
        if root.find("totalCnt") is None or root.find("page") is None:
            raise ValueError("목록 응답의 페이지/전체 건수 정보가 없습니다.")
        saved.update(total_count=int(root.findtext("totalCnt")), page=int(root.findtext("page")), records=[fields(n) for n in root.findall("law")])
    else:
        info = root.findall("./자치법규기본정보")
        if len(info) != 1:
            raise ValueError("본문 응답의 기본정보가 하나가 아닙니다.")
        # 본문 자체는 필요한 무결성 비교만 수행합니다. 원본을 다시 쓰지 않습니다.
        saved.update(info=fields(info[0]), last_supplement=[n.text or "" for n in root.findall("./부칙/부칙내용")[-1:]])
    save_json(cache, saved)
    return saved


def collect(project, output):
    manifest_path = project / "data/raw/ordinances.manifest.json"
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    oc = dotenv_values(project / ".env").get("LAW_OC")
    if not oc:
        raise ValueError("프로젝트 .env에 LAW_OC가 없습니다.")
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with requests.Session() as session:
        for record in manifest["ordinances"]:
            raw_hash = digest((project / record["raw_file"]).read_bytes())
            if raw_hash != record["content_hash"]:
                raise ValueError("목록 작성 이후 원본 XML이 변경되었습니다.")
            entry = {
                "ordin_id": record["ordin_id"], "selected_seq": record["ordin_seq"],
                "name": record["ordin_name"], "local_xml_hash": raw_hash, "catalogs": [],
            }
            for nw in (1, 2):
                pages, all_rows, expected_count = [], [], None
                for page in range(1, 31):
                    params = {
                        "target": "ordin", "type": "XML", "nw": nw,
                        "query": record["ordin_name"], "org": "6110000", "knd": "30001",
                        "display": 100, "page": page, "sort": "efdes",
                    }
                    item = fetch(session, oc, LIST_API, params, output)
                    if item["page"] != page or item["total_count"] < 0:
                        raise ValueError("잘못된 응답 페이지/건수입니다.")
                    if expected_count is None:
                        expected_count = item["total_count"]
                    elif expected_count != item["total_count"]:
                        raise ValueError("수집 중 전체 건수가 바뀌었습니다. 새 수집 폴더로 다시 확인하세요.")
                    pages.append(item)
                    all_rows.extend(item["records"])
                    if len(all_rows) >= expected_count:
                        break
                    if not item["records"]:
                        raise ValueError("전체 건수를 채우기 전에 빈 페이지를 받았습니다.")
                else:
                    raise ValueError("페이지 상한에 도달했습니다. 전체 연혁을 확인하지 못했습니다.")
                if len(all_rows) != expected_count:
                    raise ValueError("전체 건수와 수집한 목록 건수가 다릅니다.")
                seqs = [row["자치법규일련번호"] for row in all_rows]
                if len(seqs) != len(set(seqs)):
                    raise ValueError("목록 페이지에 중복 판본이 있습니다.")
                matched = [row for row in all_rows if row["자치법규ID"] == record["ordin_id"]]
                entry["catalogs"].append({"nw": nw, "total_count": expected_count, "pages": pages, "matched_id_records": matched})
                print(f"{record['ordin_name']} | nw={nw} | 전체 {expected_count}건 / 같은 ID {len(matched)}건", flush=True)
            current = fetch(session, oc, BODY_API, {"target": "ordin", "type": "XML", "ID": record["ordin_id"]}, output)
            entry["current_id_body"] = current
            entry["current_body_byte_equal"] = current["response_sha256"] == raw_hash
            results.append(entry)
    evidence = {
        "schema_version": 1, "as_of_date": manifest["as_of_date"],
        "manifest_content_hash_before_check": digest(manifest_raw),
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "official_nw1_nw2_complete_title_search_plus_current_body_by_id",
        "coverage_note": "현행·연혁 제목 검색을 완전 페이지 수집하고 ID로 대조. 제목이 달랐던 연혁은 누락될 수 있으므로 현재 ID 본문과도 비교. 법적 적용·경과조치 해석은 별도 검수.",
        "ordinances": results,
    }
    save_json(output / "version_evidence.json", evidence)
    print("근거 저장:", output / "version_evidence.json")
    for item in results:
        current_seq = item["current_id_body"]["info"]["자치법규일련번호"]
        print(f"{item['name']} | 선택 {item['selected_seq']} / 현행 ID 조회 {current_seq} / XML 바이트 일치 {item['current_body_byte_equal']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        collect(args.project_dir.resolve(), args.output_dir.resolve())
    except (ValueError, OSError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
