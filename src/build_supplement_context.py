"""6종 조례 부칙 원문과 본문 확인 경로를 보존합니다. 법적 적용 판단/API 호출 없음."""

from project_paths import PROJECT_ROOT
import argparse
import html
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date
from pathlib import Path

import build_ordinance_corpus as body

VERSION = "ordinance-supplements-v1"
REFERENCE = re.compile(r"제(?P<article>\d+)조(?:의(?P<branch>\d+))?(?:\s*제(?P<paragraph>\d+)항)?(?:\s*제(?P<item>\d+)호)?")
OWN_HEADING = re.compile(r"^[ \t]*제\d+조(?:의\d+)?\s*(?=\(|삭제|생략)", re.MULTILINE)
NOTE_CITATION = re.compile(r"조례\s*제(\d+)호\s*\((\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\)")
END_DATE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*까지")
DATE_LITERAL = re.compile(r"\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일|\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?")


def span_text(text, start, end):
    return {"source_span": {"start": start, "end": end}, "quote_text": text[start:end]}


def parse_blocks(raw, entry):
    root = ET.fromstring(raw)
    basic = root.find("./자치법규기본정보")
    current_date = basic.findtext("공포일자")
    current_number = int(basic.findtext("공포번호"))
    blocks, identities = [], set()
    for container_index, container in enumerate(root.findall("./부칙"), 1):
        children = list(container)
        body.require(len(children) % 3 == 0, "부칙 공포정보/내용의 묶음 누락")
        for offset in range(0, len(children), 3):
            triple = children[offset:offset + 3]
            body.require([n.tag for n in triple] == ["부칙공포일자", "부칙공포번호", "부칙내용"], "부칙 태그 순서 불일치")
            body.require(all(not list(n) and n.text is not None for n in triple), "부칙 필드는 단일 텍스트여야 합니다")
            day, number, text = [n.text for n in triple]
            iso_day = body.source_import.xml_date(day)
            body.require(number.isascii() and number.isdecimal(), "부칙 공포번호 형식 오류")
            identity = (day, int(number))
            body.require(identity not in identities, "중복 부칙 공포일/번호")
            identities.add(identity)
            body.require(iso_day <= entry["as_of_date"], "기준일 이후 공포 부칙은 별도 검토 필요")
            header = next((line.strip() for line in text.splitlines() if line.strip()), "")
            match = re.search(r"<\s*제\s*(\d+)호\s*,\s*(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})", header)
            body.require(header.startswith("부칙") and match is not None, "부칙 제목의 공포정보를 확인하세요")
            header_day = date(*map(int, match.groups()[1:])).isoformat()
            body.require(int(match.group(1)) == int(number) and header_day == iso_day, "부칙 제목과 공포정보 불일치")
            ordinal = len(blocks) + 1
            blocks.append({"supplement_id": f"{entry['ordin_seq']}:supplement:{ordinal:04d}",
                           "ordin_id": entry["ordin_id"], "ordin_seq": entry["ordin_seq"], "ordin_name": entry["ordin_name"],
                           "as_of_date": entry["as_of_date"], "source_url": entry["source_url"],
                           "selected_version_effective_date": entry["effective_date"],
                           "ordinance_xml_hash": entry["content_hash"], "supplement_promulgation_date": iso_day,
                           "promulgation_date_raw": day, "promulgation_number_raw": number,
                           "promulgation_number_normalized": str(int(number)), "title": header,
                           "source_text": text, "source_text_hash": body.digest(text.encode("utf-8")),
                           "source_location": {"raw_file": entry["raw_file"], "container_ordinal": container_index,
                                               "content_ordinal": offset // 3 + 1,
                                               "xml_path": f"./부칙[{container_index}]/부칙내용[{offset // 3 + 1}]"},
                           "is_current_amendment": day == current_date and int(number) == current_number,
                           "applicability_status": "not_determined", "source_review": "pending", "answer_allowed": False})
    body.require(len(blocks) == entry["supplement_block_count"], "부칙 블록 수와 manifest 불일치")
    body.require(sum(b["is_current_amendment"] for b in blocks) == 1, "현재 개정본의 부칙을 하나로 식별하지 못했습니다")
    return blocks


def analyze_block(block, chunks):
    text = block["source_text"]
    heading_starts = {m.start() + len(m.group()) - len(m.group().lstrip()) for m in OWN_HEADING.finditer(text)}
    signals = []
    for label, pattern in (("commencement", r"시행"), ("application", r"적용례|적용"),
                           ("transition", r"경과조치|종전"), ("validity", r"유효|효력을|까지 사용할"),
                           ("other_law_amendment", r"다른\s*조례.*개정"), ("omitted_text", r"생략")):
        if re.search(pattern, text):
            signals.append(label)
    # 범위가 모호한 문구는 현행 번호에 자동 연결하지 않습니다.
    ambiguous = bool(re.search(r"다른\s*조례|[「」｢｣]|법률|법\s*제\d|시행령|시행규칙|생략|같은\s*조|부칙\s*제|(?:조|항|호)\s*(?:부터|까지|및|내지|~|∼)", text)
                     or "조례" in block["title"])
    mentions, links = [], []
    for match in REFERENCE.finditer(text):
        article = f"제{int(match['article'])}조" + (f"의{int(match['branch'])}" if match["branch"] else "")
        paragraph = int(match["paragraph"]) if match["paragraph"] else None
        item = int(match["item"]) if match["item"] else None
        mention = {**span_text(text, match.start(), match.end()), "article": article,
                   "paragraph": paragraph, "item": item, "target_chunk_ids": [],
                   "resolution": "unresolved_reference", "applicability_status": "not_determined"}
        if match.start() in heading_starts:
            mention["resolution"] = "supplement_own_heading"
        elif not block["is_current_amendment"]:
            mention["resolution"] = "historical_reference_requires_version_review"
        elif ambiguous or item is not None:
            mention["resolution"] = "scope_requires_review"
        else:
            targets = [c for c in chunks if c["article"] == article and
                       (paragraph is None or c["paragraph"] == paragraph)]
            if targets:
                mention["target_chunk_ids"] = [c["chunk_id"] for c in targets]
                mention["resolution"] = "exact_current_text_reference"
                links.append({"supplement_id": block["supplement_id"], **mention,
                              "relationship": "explicit_number_reference_in_current_amendment",
                              "answer_allowed": False})
            else:
                mention["resolution"] = "target_not_found"
        mentions.append(mention)
    return {"kind_signals": signals, "signals_are_legal_classification": False,
            "scope_requires_review": ambiguous, "mentions": mentions, "textual_links": links,
            "date_expressions": [span_text(text, m.start(), m.end()) for m in DATE_LITERAL.finditer(text)],
            "date_interpretation": "literal_only_no_effective_date_inferred"}


def link_article_notes(articles, blocks):
    links = []
    for article in articles:
        for note in article["article_notes"]:
            for match in NOTE_CITATION.finditer(note):
                number, y, m, d = match.groups()
                day = date(int(y), int(m), int(d)).isoformat()
                targets = [b for b in blocks if b["supplement_promulgation_date"] == day and
                           b["promulgation_number_normalized"] == str(int(number))]
                body.require(len(targets) <= 1, "조문 주석의 공포정보가 복수 부칙을 가리킵니다")
                target = targets[0] if targets else None
                note_dates = sorted({date(*map(int, m.groups())).isoformat() for m in END_DATE.finditer(note)})
                block_dates = sorted({date(*map(int, m.groups())).isoformat() for m in END_DATE.finditer(target["source_text"])}) if target else []
                discrepancy = bool(note_dates and block_dates and note_dates != block_dates)
                start = article["source_text"].find(note)
                body.require(start >= 0, "조문 주석의 원문 위치를 찾지 못했습니다")
                links.append({"article_id": article["article_id"], "article": article["article"],
                              "chunk_ids": article["chunk_ids"], "note": note,
                              "note_source_span": {"start": start, "end": start + len(note)},
                              "supplement_id": target["supplement_id"] if target else None,
                              "relationship": "explicit_promulgation_citation" if target else "cited_supplement_missing",
                              "note_stated_end_dates": note_dates, "block_stated_end_dates": block_dates,
                              "different_stated_end_dates": discrepancy,
                              "comparison_scope": "whole_block_date_strings_only_not_legal_conflict_determination",
                              "applicability_status": "not_determined", "answer_allowed": False})
    return links


def load_body_outputs(project, manifest, input_hashes):
    folder = Path("data/processed/ordinances") / manifest["as_of_date"]
    # 기존 본문을 현재 XML에서 다시 생성하여 저장 결과와 정확히 비교합니다.
    expected, _ = body.prepare(project)
    loaded = {}
    for name, raw in expected.items():
        relative = (folder / name).as_posix()
        path = project / relative
        body.require(path.resolve().is_relative_to(project) and not path.is_symlink(), "본문 산출물 경로 오류")
        actual = path.read_bytes()
        body.require(actual == raw, f"본문 산출물이 검증한 결과와 다릅니다: {name}")
        input_hashes[relative] = body.digest(actual)
        if name.endswith(".articles.json") or name.endswith(".chunks.json"):
            loaded[name] = json.loads(actual)
    return loaded


def preview(report, documents, contexts):
    esc = lambda v: html.escape(str(v))
    rows, sections = [], []
    for doc in documents:
        rows.append(f"<tr><td>{esc(doc['ordin_name'])}</td><td>{len(doc['blocks'])}</td></tr>")
        entries = []
        for block in doc["blocks"]:
            badge = "현재 개정본에 붙은 부칙" if block["is_current_amendment"] else "과거 개정 이력 · 적용 검토 필요"
            analysis = block["analysis"]
            direct = "<p>본문 번호 참조: " + esc(", ".join(l["quote_text"] for l in analysis["textual_links"])) + "</p>" if analysis["textual_links"] else ""
            entries.append(f"<details id='{esc(block['supplement_id'])}'><summary>{esc(block['supplement_promulgation_date'])} · 제{esc(block['promulgation_number_raw'])}호 — {badge}</summary>{direct}<pre>{esc(block['source_text'])}</pre><p class='muted'>공포일은 부칙 자체의 시행일을 계산한 값이 아닙니다. 날짜·적용 여부는 원문 확인 대상입니다.</p></details>")
        sections.append(f"<h2>{esc(doc['ordin_name'])}</h2>{''.join(entries)}")
    note_entries = []
    for context in contexts:
        for link in context["article_note_links"]:
            target = link["supplement_id"]
            warning = "<strong class='warning'>주석과 부칙에 적힌 종료 날짜가 다릅니다. 원문을 함께 검토해야 합니다.</strong>" if link["different_stated_end_dates"] else "적용 대상·현재 효력은 검토 대기입니다."
            note_entries.append(f"<article><h3>{esc(context['ordin_name'])} {esc(link['article'])}</h3><pre>{esc(link['note'])}</pre><p>{warning}</p><p>주석의 날짜: {esc(link['note_stated_end_dates'])} / 부칙의 날짜: {esc(link['block_stated_end_dates'])}</p><a href='#{esc(target)}'>공포일·번호로 연결된 부칙 보기</a></article>")
    return ("<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>조례 부칙·본문 연결 확인</title><style>body{max-width:1050px;margin:40px auto;padding:0 24px;background:#f5f7fb;color:#182338;font:16px/1.7 system-ui}h1{line-height:1.3}h2{margin-top:40px}table{border-collapse:collapse;width:100%;background:white}th,td{padding:10px;border-bottom:1px solid #dce2ed;text-align:left}details,article,.intro{background:white;border:1px solid #dce2ed;padding:16px;margin:12px 0;border-radius:8px}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.intro{background:#eaf2ff}.muted{color:#59667b;font-size:14px}.warning{color:#944608}</style><h1>조례 부칙과 본문 연결 확인</h1><div class='intro'><b>" + str(report["block_count"]) + "개 공포일·공포번호별 부칙 원문을 보존했습니다.</b><p>한 부칙 안의 시행일·예외·경과조치가 떨어지지 않도록 블록 전체를 한 확인 단위로 유지합니다. 현재 개정본의 부칙과 과거 이력을 구분하고, 모든 본문 조각에서 해당 조례의 부칙 목록을 찾을 수 있게 연결했습니다.</p><p>연결은 원문 위치와 출처를 확인한 것입니다. 과거 부칙이 현재 조문에도 적용된다는 판단은 하지 않았습니다. 현재 개정본의 일반 시행문도 모든 조항의 시행일을 확정하는 근거로 자동 사용하지 않습니다.</p></div><h2>먼저 검토할 본문 주석</h2>" + "".join(note_entries) + "<h2>조례별 부칙 수</h2><table><tr><th>조례</th><th>부칙 블록</th></tr>" + "".join(rows) + "</table>" + "".join(sections) + "</html>").encode("utf-8")


def prepare(project):
    project = project.resolve(strict=True)
    manifest, inputs, input_hashes = body.load_inputs(project)
    corpus = load_body_outputs(project, manifest, input_hashes)
    files, docs, contexts, summaries = {}, [], [], []
    for entry, raw in inputs:
        seq = entry["ordin_seq"]
        articles = corpus[f"{seq}.articles.json"]["articles"]
        chunks = corpus[f"{seq}.chunks.json"]
        blocks = parse_blocks(raw, entry)
        for block in blocks:
            block["analysis"] = analyze_block(block, chunks)
            block["retrieval_text"] = f"{entry['ordin_name']}\n" + block["source_text"]
            block["retrieval_use"] = "context_review_only"
            block["embedding_status"] = "not_created"
        current = next(b for b in blocks if b["is_current_amendment"])
        textual_links = [link for block in blocks for link in block["analysis"]["textual_links"]]
        note_links = link_article_notes(articles, blocks)
        doc = {"schema_version": 1, "pipeline_version": VERSION, "ordin_name": entry["ordin_name"],
               "ordin_seq": seq, "as_of_date": entry["as_of_date"], "span_semantics": body.SPAN_DESCRIPTION.replace("조내용", "부칙내용"),
               "blocks": blocks, "source_review": "pending", "production_ready": False}
        context = {"schema_version": 1, "ordin_name": entry["ordin_name"], "ordin_seq": seq,
                   "registry_id": f"{seq}:supplements", "supplement_ids": [b["supplement_id"] for b in blocks],
                   "current_amendment_supplement_id": current["supplement_id"],
                   "textual_links": textual_links, "article_note_links": note_links,
                   "body_contexts": [{"chunk_id": c["chunk_id"], "parent_id": c["parent_id"],
                                      "supplement_registry_id": f"{seq}:supplements",
                                      "current_amendment_context_id": current["supplement_id"],
                                      "explicit_reference_context_ids": sorted({l["supplement_id"] for l in textual_links if c["chunk_id"] in l["target_chunk_ids"]}),
                                      "article_note_context_ids": sorted({l["supplement_id"] for l in note_links if c["chunk_id"] in l["chunk_ids"] and l["supplement_id"]}),
                                      "history_review_required": True, "applicability_status": "not_determined",
                                      "answer_allowed": False} for c in chunks],
                   "context_policy": "공통 개정본 문맥/명시적 원문 참조/전체 이력 탐색 경로. 모든 부칙을 현재 유효 근거로 전달하는 규칙이 아님.",
                   "source_review": "pending", "production_ready": False, "answer_allowed": False}
        summary = {"ordin_name": entry["ordin_name"], "ordin_seq": seq, "block_count": len(blocks),
                   "body_context_count": len(chunks), "current_textual_links": len(textual_links),
                   "article_note_links": sum(l["supplement_id"] is not None for l in note_links),
                   "different_end_date_cases": sum(l["different_stated_end_dates"] for l in note_links),
                   "mention_status_counts": dict(Counter(m["resolution"] for b in blocks for m in b["analysis"]["mentions"]))}
        docs.append(doc)
        contexts.append(context)
        summaries.append(summary)
        files[f"{seq}.supplements.json"] = body.json_bytes(doc)
        files[f"{seq}.body_contexts.json"] = body.json_bytes(context)
    report = {"schema_version": 1, "pipeline_version": VERSION, "as_of_date": manifest["as_of_date"],
              "validation_status": "exact_source_and_textual_context_checked",
              "input_files": input_hashes, "block_count": sum(s["block_count"] for s in summaries),
              "body_context_count": sum(s["body_context_count"] for s in summaries),
              "ordinances": summaries, "source_review": "pending", "production_ready": False,
              "applicability_status": "not_determined", "embedding_status": "not_created", "answer_allowed": False,
              "limitations": ["부칙 블록 수는 부칙 조항 수가 아니라 공포일·번호별 내용 블록 수",
                              "과거 공포번호의 부칙도 현재 XML에 수록된 내용이며 후속 개정이 반영됐을 수 있음",
                              "공포일과 각 부칙의 시행일은 별개이며 상대 날짜·예외 적용은 자동 계산하지 않음",
                              "과거 부칙의 조 번호와 현재 본문 번호의 동일성은 확인하지 않음",
                              "주석·부칙의 종료 날짜 문자열 차이는 법적 모순 확정이 아닌 검토 신호",
                              "기존 본문·별표 답변기는 아직 이 새 문맥 자료를 사용하지 않음"]}
    body.require(report["block_count"] == manifest["supplement_block_count"], "전체 부칙 수 불일치")
    files["supplement_report.json"] = body.json_bytes(report)
    files["supplement_preview.html"] = preview(report, docs, contexts)
    files["supplement.manifest.json"] = body.json_bytes({"schema_version": 1, "pipeline_version": VERSION,
         "input_files": input_hashes, "output_files": {name: body.digest(raw) for name, raw in files.items()},
         "source_review": "pending", "production_ready": False, "answer_allowed": False})
    return files, report


def validate_review_record(record, read_file):
    """검토 기록의 출처·인용·현재 조각 연결만 검사합니다. 법률 판단을 재수행하지 않음."""
    body.require(record["schema_version"] == 1 and record["case_status"] == "resolved_by_official_amendment_text", "연혁 검토 기록 형식 오류")
    body.require(record["source_review"] == "pending" and record["answer_allowed"] is False
                 and record["answer_generator_connected"] is False, "연혁 검토가 전체 승인 상태로 바뀌었습니다")
    inputs = {name: read_file(name) for name in record["input_files"]}
    body.require(all(body.digest(raw) == record["input_files"][name] for name, raw in inputs.items()), "연혁 검토 근거 해시 불일치")
    evidence_ids = set()
    body.require(record["evidence_quotes"], "연혁 검토 인용이 없습니다")
    for quote in record["evidence_quotes"]:
        body.require(quote["evidence_id"] not in evidence_ids, "연혁 검토 인용 ID 중복")
        evidence_ids.add(quote["evidence_id"])
        body.require(quote["file"] in inputs, "해시로 연결되지 않은 인용 파일")
        node = ET.fromstring(inputs[quote["file"]]).find(quote["xml_path"])
        body.require(node is not None and node.text is not None, "연혁 검토 인용 위치 없음")
        start, end = quote["source_span"]["start"], quote["source_span"]["end"]
        body.require(type(start) is int and type(end) is int and 0 <= start < end <= len(node.text), "연혁 검토 인용 구간 오류")
        body.require(node.text[start:end] == quote["quote"], "연혁 검토 인용문 불일치")
    body.require(record["findings"] and all(f["evidence_ids"] and set(f["evidence_ids"]) <= evidence_ids for f in record["findings"]), "결론의 근거 인용 연결 오류")
    body.require(record["as_of_date"] == body.source_import.AS_OF_DATE, "연혁 검토 기준일 불일치")
    chunks_file = f"data/processed/ordinances/{record['as_of_date']}/{record['selected_ordin_seq']}.chunks.json"
    body.require(chunks_file in inputs, "현재 검색 조각의 해시 연결 누락")
    chunks = {c["chunk_id"]: c for c in json.loads(inputs[chunks_file])}
    body.require(record["context_chunk_ids"] and all(cid in chunks and chunks[cid]["parent_id"] == record["selected_article_id"] for cid in record["context_chunk_ids"]), "연혁 검토의 현재 조문 연결 불일치")
    return record["case_id"]


def validate_review_records(project):
    project = project.resolve(strict=True)

    def read(relative):
        path = project / relative
        body.require(path.resolve().is_relative_to(project) and not path.is_symlink(), "연혁 검토 경로 범위 오류")
        return path.read_bytes()

    cases = []
    for path in sorted((project / "data/processed/reviews").glob("*.review.json")):
        record = json.loads(read(path.relative_to(project).as_posix()))
        case_id = validate_review_record(record, read)
        body.require(case_id not in cases, "연혁 검토 case ID 중복")
        cases.append(case_id)
    return cases


def run(project, output=None, check_only=False):
    project = project.resolve(strict=True)
    files, report = prepare(project)
    output = (output or project / "data/processed/ordinances" / report["as_of_date"] / "supplements").resolve()
    for name, raw in files.items():
        path = output / name
        if path.exists():
            body.require(path.is_file() and not path.is_symlink() and path.read_bytes() == raw, f"기존 파일 내용 충돌: {name}")

    def check_inputs():
        for relative, expected in report["input_files"].items():
            body.require(body.digest((project / relative).read_bytes()) == expected, "검증 후 입력 파일 변경")

    check_inputs()
    if not check_only:
        for name, raw in files.items():
            if name == "supplement.manifest.json":
                check_inputs()
                for other, data in files.items():
                    if other != name:
                        body.require((output / other).read_bytes() == data, "완료 목록 저장 전 산출물 불일치")
            if not (output / name).exists():
                body.source_import.write_new(output / name, raw)
        body.require(all((output / n).read_bytes() == r for n, r in files.items()), "저장 후 대조 실패")
    for s in report["ordinances"]:
        print(f"{s['ordin_name']}: 부칙 {s['block_count']} / 본문 문맥 연결 {s['body_context_count']} / 명시 참조 {s['current_textual_links']} / 주석 출처 {s['article_note_links']}")
    print(f"총 부칙 {report['block_count']} / 본문 문맥 {report['body_context_count']} / 날짜 표현 차이 {sum(s['different_end_date_cases'] for s in report['ordinances'])}건")
    print("적용 여부 판단 안 함 / 임베딩 생성 안 함 /", "검사만" if check_only else "저장 완료")
    print("결과:", output)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        run(args.project_dir, args.output_dir, args.check_only)
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as exc:
        raise SystemExit(f"부칙 가공 중단: {exc}") from None
