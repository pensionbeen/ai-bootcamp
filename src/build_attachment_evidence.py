
from project_paths import PROJECT_ROOT
import hashlib
import json
from pathlib import Path


STEM = "2171833_22740725"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def build_table_links(attachment_id, elements, cells):
    # 이번에 원문과 대조한 별표 2의 연결입니다.
    if attachment_id != "2171833:22740725":
        raise ValueError("이 연결 정보는 현재 별표 2에만 사용할 수 있습니다.")

    links = [
        {
            "table_order": 1,
            "parse_elements": [
                {"page": 1, "element_id": 2},
                {"page": 2, "element_id": 3},
            ],
        },
        {
            "table_order": 2,
            "parse_elements": [
                {"page": 4, "element_id": 23},
            ],
        },
    ]

    parse_tables = {
        (e["page"], e["id"])
        for e in elements
        if e.get("category") == "table"
    }
    linked_sources = set()
    linked_pairs = set()

    for link in links:
        for source in link["parse_elements"]:
            key = (source["page"], source["element_id"])
            if key not in parse_tables:
                raise ValueError(f"연결할 원문 표가 없습니다: {key}")
            if key in linked_sources:
                raise ValueError(f"원문 표가 중복 연결됐습니다: {key}")

            linked_sources.add(key)
            linked_pairs.add((link["table_order"], source["page"]))

    if linked_sources != parse_tables:
        raise ValueError("연결되지 않은 Parse 표가 있습니다.")

    extract_pairs = {
        (cell["table_order"], cell["page_number"])
        for cell in cells
    }
    if linked_pairs != extract_pairs:
        raise ValueError("Extract의 표·페이지와 원문 연결이 다릅니다.")

    return links

def build_evidence(base_dir, parse_path, extract_path):
    raw_dir = base_dir / "data" / "raw" / "attachments"
    hwp_path = raw_dir / f"{STEM}.hwp"
    record = read_json(raw_dir / f"{STEM}.metadata.json")
    parsed = read_json(parse_path)
    extracted = read_json(extract_path)

    input_hash = file_hash(hwp_path)
    if input_hash != record["download"]["content_hash"]:
        raise ValueError("HWP 파일과 다운로드 기록의 해시가 다릅니다.")

    attachment = record["attachment"]
    metadata = record["metadata"]
    expected_id = STEM.replace("_", ":")
    if attachment["attachment_id"] != expected_id:
        raise ValueError("첨부자료 ID가 파일명과 다릅니다.")

    elements = parsed.get("elements")
    if not isinstance(elements, list) or not elements:
        raise ValueError("Parse 결과에 문서 요소 목록이 없습니다.")

    for key in ("table_cells", "context_blocks", "figures"):
        if not isinstance(extracted.get(key), list):
            raise ValueError(f"Extract의 {key}가 목록이 아닙니다.")

    return {
        "schema_version": 1,
        "attachment_id": attachment["attachment_id"],
        "source": {
            "ordin_name": record["ordin_name"],
            "ordin_id": metadata["ordin_id"],
            "ordin_seq": metadata["ordin_seq"],
            "as_of_date": metadata["as_of_date"],
            "effective_date": metadata["effective_date"],
            "promulgation_date": metadata["promulgation_date"],
            "source_url": metadata["source_url"],
            "attachment_label": attachment["label"],
            "attachment_title": attachment["title"],
            "ordinance_xml_content_hash": metadata["content_hash"],
        },
        "input_content_hash": input_hash,
        "input_binding_status": "manual_export",
        "result_files": {
            "parse": {
                "file": parse_path.name,
                "content_hash": file_hash(parse_path),
            },
            "extract": {
                "file": extract_path.name,
                "content_hash": file_hash(extract_path),
            },
        },
        "parse_model": parsed.get("model"),
        "parse_elements": elements,
        "extraction": extracted,
                "table_links": build_table_links(
            attachment["attachment_id"],
            elements,
            extracted["table_cells"],
        ),
        "table_link_method": "manual_reviewed",
        "table_link_status": "linked",
        "review_status": "pending",
    }


def main():
    base_dir = PROJECT_ROOT
    output_dir = base_dir / "data" / "processed" / "attachments"
    parse_path = output_dir / f"{STEM}_Parse.json"
    extract_path = output_dir / f"{STEM}_Extract.json"

    evidence = build_evidence(base_dir, parse_path, extract_path)
    output_path = output_dir / f"{STEM}.evidence.json"
    output_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result = evidence["extraction"]
    print("첨부 ID:", evidence["attachment_id"])
    print("Parse 요소:", len(evidence["parse_elements"]))
    print("표 기록:", len(result["table_cells"]))
    print("문맥 기록:", len(result["context_blocks"]))
    print("표 연결 수:", len(evidence["table_links"]))
    print("표 연결 상태:", evidence["table_link_status"])
    print("저장:", output_path)


# 아래 배치 경로는 기존 별표 2의 수동 Extract/논리 표 연결과 독립적입니다.
# Parse 요소를 검색 조각 또는 검수 완료 근거로 격상하지 않습니다.
import base64
import html as html_module
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser

import build_ordinance_corpus as body_corpus
import download_attachment as originals
import parse_attachment as parser_batch

BATCH_MANIFEST = parser_batch.FOLDER + "/evidence.manifest.json"


class LayoutHTML(HTMLParser):
    """HTML 구조를 유지한 안전한 미리보기와 비교용 텍스트를 만듭니다."""
    allowed = set("p h1 h2 h3 h4 h5 h6 table thead tbody tfoot tr th td caption figure figcaption header footer div span br b strong i em u sub sup ul ol li del s".split())

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.parts, self.plain, self.spans = [], [], []
        self.rows = self.cells = 0
        self.hidden = 0
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden:
            return
        if tag not in self.allowed:
            return
        attributes = []
        for name, value in attrs:
            if tag in ("td", "th") and name in ("rowspan", "colspan") and value and value.isdigit() and 0 < int(value) <= 1000:
                attributes.append(f' {name}="{int(value)}"')
                self.spans.append({"attribute": name, "value": int(value)})
        self.parts.append("<" + tag + "".join(attributes) + ">")
        if tag == "tr":
            self.rows += 1
        if tag in ("td", "th"):
            self.cells += 1
        if tag not in ("span", "b", "strong", "i", "em", "u", "sub", "sup", "del", "s"):
            self.plain.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
            return
        if not self.hidden and tag in self.allowed:
            self.parts.append("</" + tag + ">")
            if tag in ("td", "th", "tr", "p", "li", "div", "header", "footer"):
                self.plain.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(html_module.escape(data))
            self.plain.append(data)

    @property
    def text(self):
        return re.sub(r"\s+", " ", "".join(self.plain)).strip()


def numeric_tokens(text):
    return Counter(re.findall(r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?", unicodedata.normalize("NFKC", text)))


def compare_xml_text(xml_text, parsed_text):
    xml_numbers, parse_numbers = numeric_tokens(xml_text), numeric_tokens(parsed_text)
    normalize = lambda s: re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))
    return {"method": "whitespace_normalized_text_and_numeric_multiset_only",
            "normalized_text_equal": normalize(xml_text) == normalize(parsed_text),
            "xml_text_length": len(xml_text), "parsed_text_length": len(parsed_text),
            "numbers_only_or_more_in_xml": dict(xml_numbers - parse_numbers),
            "numbers_only_or_more_in_parse": dict(parse_numbers - xml_numbers),
            "is_accuracy_score": False, "semantic_correctness": "not_determined",
            "warning": "숫자의 빈도 차이는 검토 단서입니다. 표의 행·열 대응, 단위, 예외·조건의 정확성을 증명하지 않습니다."}


def crop_data_url(element):
    value = element.get("base64_encoding")
    if not value:
        return None
    body_corpus.require(isinstance(value, str), "원문 크롭 이미지 형식 오류")
    raw = base64.b64decode(value, validate=True)
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif raw.startswith(b"\xff\xd8\xff"):
        kind = "jpeg"
    else:
        raise ValueError("지원하지 않는 크롭 이미지 형식")
    return f"data:image/{kind};base64,{value}"


def batch_document(item, parsed, links):
    elements, flags = [], ["content_review_pending"]
    if parsed["input_binding_status"] == "manual_export_unverified":
        flags.append("manual_input_binding_unverified")
    if item["attachment"]["attachment_type"] == "별도":
        flags.append("drawing_visual_review_required")
    if parsed["pages_without_elements"]:
        flags.append("pages_without_elements_review_required")
    for index, element in enumerate(parsed["response"]["elements"]):
        markup = element["content"]["html"]
        layout = LayoutHTML(markup)
        text = element["content"].get("text") or layout.text
        crop = crop_data_url(element)
        record = {"page": element["page"], "element_id": element["id"],
                  "category": element["category"], "coordinates": element["coordinates"],
                  "source_element_index": index, "html": markup, "text": text,
                  "text_method": "provider_text" if element["content"].get("text") else "html_text_projection",
                  "crop_image_available": crop is not None, "is_search_chunk": False}
        if element["category"] == "table":
            record["table_structure"] = {"html_rows": layout.rows, "html_cells": layout.cells,
                                         "merged_cell_attributes": layout.spans,
                                         "multipage_merge": "not_performed"}
            flags.append("table_cells_units_conditions_review_required")
            if not crop:
                flags.append("table_crop_unavailable")
        if element["category"] in ("figure", "chart"):
            record["description_status"] = "model_generated_unverified"
            flags.append("figure_description_review_required")
            if not crop:
                flags.append("figure_crop_unavailable")
        elements.append(record)
    parsed_text = "\n".join(e["text"] for e in elements)
    comparison = compare_xml_text(item["attachment"]["raw_text"], parsed_text)
    if comparison["numbers_only_or_more_in_xml"] or comparison["numbers_only_or_more_in_parse"]:
        flags.append("numeric_difference_review_required")
    if not comparison["normalized_text_equal"]:
        flags.append("xml_parse_text_difference")
    return {"schema_version": "attachment-layout-evidence-v1", "attachment_id": item["attachment"]["attachment_id"],
            "source": item["source"], "source_location": item["source_location"],
            "ordin_name": item["ordin_name"], "attachment": item["attachment"],
            "input_binding_status": parsed["input_binding_status"],
            "parse_file": parsed["file"], "parse_content_hash": parsed["content_hash"],
            "parse_model": parsed["response"]["model"], "page_count": parsed["pages"],
            "elements": elements, "related_body": links, "xml_comparison": comparison,
            "review_flags": sorted(set(flags)), "source_review": "pending", "answer_allowed": False,
            "is_embedding_input": False}


def review_html(documents, parsed_results):
    esc = html_module.escape
    sections, navigation = [], []
    for doc, parsed in zip(documents, parsed_results):
        ident = doc["attachment_id"]
        label = "별표 (번호 없음)" if doc["attachment"]["attachment_no"] == "0000" else doc["attachment"]["label"]
        title = doc["ordin_name"] + " · " + label
        navigation.append(f'<a href="#{esc(ident)}">{esc(title)}</a>')
        refs = doc["related_body"]["body_references"]
        references = "".join(f'<li>{esc(r["article"])} {("제" + str(r["paragraph"]) + "항") if r["paragraph"] is not None else ""}<pre>{esc(r["quote_text"])}</pre></li>' for r in refs)
        details = []
        for e, original in zip(doc["elements"], parsed["response"]["elements"]):
            crop = crop_data_url(original)
            image = f'<img loading="lazy" alt="원문 영역 이미지" src="{crop}">' if crop else '<p class="muted">이 요소에는 원문 영역 이미지가 없습니다.</p>'
            details.append(f'<details><summary>p.{e["page"]} · 요소 {e["element_id"]} · {esc(e["category"])}</summary><div class="columns"><div><h4>추출한 구조·내용</h4>{"".join(LayoutHTML(e["html"]).parts)}</div><div><h4>HWP를 변환한 원문 영역</h4>{image}</div></div></details>')
        comparison = doc["xml_comparison"]
        findings_html = "".join(f'<div class="badge"><b>{esc(f["title"])}</b><p>{esc(f["observation"])}</p><pre>{esc(f["extracted_quote"])}</pre><p>{esc(f["required_action"])}</p></div>' for f in doc.get("review_findings", []))
        sections.append(f'''<section id="{esc(ident)}"><h2>{esc(title)}</h2><p>{esc(doc["attachment"]["title"])}</p>
<p>{doc["page_count"]}페이지 · {len(doc["elements"])}문서 요소 · 본문 명시 참조 {len(refs)}건</p>
<p class="badge">검수 대기 · 답변 사용 보류</p><p>입력 연결: {esc(doc["input_binding_status"])}</p>
{findings_html}
<details><summary>본문 연결과 제목 참조 상태</summary><p>{esc(doc["related_body"]["title_reference_status"])}</p><ul>{references}</ul><pre>{esc(json.dumps(doc["related_body"].get("review_notes", []), ensure_ascii=False, indent=2))}</pre></details>
<details><summary>XML 원문과 숫자 차이 보기</summary><p>숫자의 개수 차이는 오류 확정이나 정확도 점수가 아닙니다.</p><div class="columns"><div><h4>XML 별표내용</h4><pre>{esc(doc["attachment"]["raw_text"])}</pre></div><div><h4>XML에 더 많은 숫자</h4><pre>{esc(json.dumps(comparison["numbers_only_or_more_in_xml"], ensure_ascii=False, indent=2))}</pre><h4>Parse에 더 많은 숫자</h4><pre>{esc(json.dumps(comparison["numbers_only_or_more_in_parse"], ensure_ascii=False, indent=2))}</pre></div></div></details>
{"".join(details)}</section>''')
    return ('''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'">
<title>조례 첨부 내용·출처 검토</title><style>body{font-family:system-ui,sans-serif;background:#f5f7fb;color:#172635;max-width:1250px;margin:auto;padding:28px;line-height:1.7}h1,h2{line-height:1.35}section{background:white;padding:24px;margin:24px 0;border:1px solid #d9e1eb;border-radius:12px}nav{display:flex;gap:8px;flex-wrap:wrap}nav a{background:#e8eef8;color:#174f8c;padding:6px 10px;border-radius:5px;text-decoration:none}details{border-top:1px solid #d9e1eb;padding:12px 0}summary{cursor:pointer;font-weight:600}.columns{display:grid;grid-template-columns:1fr 1fr;gap:24px}.columns>div{min-width:0;overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit}table{border-collapse:collapse;font-size:14px}th,td{border:1px solid #8293a6;padding:7px}img{max-width:100%;height:auto}.badge{background:#fff0cd;color:#704b00;padding:6px 12px;display:inline-block;border-radius:5px}.muted{color:#65758a}@media(max-width:800px){.columns{grid-template-columns:1fr}body{padding:12px}}</style>
<h1>서울시 조례 첨부 검토</h1><p>14개 원본의 Parse 내용·표 구조·그림과 본문 참조를 확인합니다. 문서 요소 수는 검색 조각 수가 아닙니다. 모든 자료는 내용 검수 대기이며 임베딩·답변에 아직 사용하지 않습니다.</p>
<p>표의 행·열, 병합 셀, 숫자·단위·예외 조건을 함께 대조하세요. 도면의 생성된 설명은 원문 그림과 별도로 검토해야 합니다. 표 이미지는 문서 영역 크롭으로 전체 페이지 렌더링을 대신하지 않습니다.</p><nav>''' + "".join(navigation) + "</nav>" + "".join(sections) + "</html>").encode("utf-8")


def load_review_findings(project, items, parsed, inputs):
    relative = parser_batch.FOLDER + "/review_findings.json"
    path = originals.safe_path(project, relative)
    if not path.exists():
        return {}
    raw = path.read_bytes()
    review = json.loads(raw)
    body_corpus.require(review["schema_version"] == "attachment-extraction-review-v1"
                        and review["reviewer_type"] == "assistant_visual_spot_check"
                        and review["answer_allowed"] is False, "첨부 검토 기록 상태 오류")
    by_id = {item["attachment"]["attachment_id"]: result for item, result in zip(items, parsed)}
    seen, result = set(), {}
    for finding in review["findings"]:
        body_corpus.require(finding["finding_id"] not in seen and finding["answer_allowed"] is False
                            and finding["status"] == "unresolved", "첨부 검토 항목 상태 오류")
        seen.add(finding["finding_id"])
        source = by_id[finding["attachment_id"]]
        body_corpus.require(source["file"] == finding["parse_file"] and source["content_hash"] == finding["parse_content_hash"],
                            "첨부 검토 Parse 해시 불일치")
        elements = [e for e in source["response"]["elements"] if e["page"] == finding["page"] and e["id"] == finding["element_id"]]
        body_corpus.require(len(elements) == 1, "첨부 검토 원문 요소 누락")
        element = elements[0]
        body_corpus.require(finding["content_field"] in ("html", "text") and finding["extracted_quote"]
                            and finding["extracted_quote"] in element["content"][finding["content_field"]], "첨부 검토 인용 불일치")
        crop = base64.b64decode(element["base64_encoding"], validate=True)
        body_corpus.require(body_corpus.digest(crop) == finding["crop_content_hash"], "첨부 검토 이미지 해시 불일치")
        result.setdefault(finding["attachment_id"], []).append(finding)
    inputs[relative] = body_corpus.digest(raw)
    return result


def prepare_batch(project):
    from link_attachment_article import load_batch_links
    collection_hash, items, parsed = parser_batch.inventory(project)
    body_corpus.require(all(parsed), "미처리 첨부가 있습니다. 먼저 parse-attachments를 실행하세요")
    links, inputs = load_batch_links(project, items)
    inputs[originals.MANIFEST] = collection_hash
    findings_by_id = load_review_findings(project, items, parsed, inputs)
    documents, outputs = [], {}
    for item, result in zip(items, parsed):
        inputs[result["file"]] = result["content_hash"]
        inputs.update(result["support_files"])
        for key in ("file_path", "metadata_path"):
            inputs[item[key]] = body_corpus.digest(originals.safe_path(project, item[key]).read_bytes())
        doc = batch_document(item, result, links[item["attachment"]["attachment_id"]])
        if doc["attachment_id"] in findings_by_id:
            doc["review_findings"] = findings_by_id[doc["attachment_id"]]
            doc["review_flags"].append("known_extraction_issue_requires_resolution")
        documents.append(doc)
        outputs[doc["attachment_id"].replace(":", "_") + ".layout_evidence.json"] = body_corpus.json_bytes(doc)
    report = {"attachment_count": len(documents), "page_count": sum(d["page_count"] for d in documents),
              "element_count": sum(len(d["elements"]) for d in documents),
              "table_elements": sum(e["category"] == "table" for d in documents for e in d["elements"]),
              "figure_elements": sum(e["category"] in ("figure", "chart") for d in documents for e in d["elements"]),
              "body_reference_count": sum(len(d["related_body"]["body_references"]) for d in documents),
              "manual_input_binding_pending": sum(d["input_binding_status"] == "manual_export_unverified" for d in documents),
              "content_review_pending": len(documents), "answer_allowed": False,
              "known_blocking_findings": sum(len(d.get("review_findings", [])) for d in documents),
              "documents": [{"attachment_id": d["attachment_id"], "pages": d["page_count"], "elements": len(d["elements"]),
                             "review_flags": d["review_flags"], "title_reference_status": d["related_body"]["title_reference_status"]} for d in documents]}
    outputs["extraction_report.json"] = body_corpus.json_bytes(report)
    outputs["review.html"] = review_html(documents, parsed)
    manifest = {"pipeline_version": "attachment-layout-evidence-v1", "input_files": inputs,
                "output_files": {name: body_corpus.digest(raw) for name, raw in outputs.items()},
                "source_review": "pending", "answer_allowed": False, "report": report}
    outputs["evidence.manifest.json"] = body_corpus.json_bytes(manifest)
    return outputs, report


def run_batch(project, check_only=False):
    from request_lock import request_lock
    # 검사 모드는 잠금 파일도 만들지 않습니다.
    if check_only:
        outputs, report = prepare_batch(project)
        for name, raw in outputs.items():
            body_corpus.require(originals.safe_path(project, parser_batch.FOLDER + "/" + name).read_bytes() == raw,
                                "첨부 근거 자료가 입력/코드와 다릅니다: " + name)
        return report
    with request_lock(originals.safe_path(project, parser_batch.FOLDER + "/.evidence.lock")):
        outputs, report = prepare_batch(project)
        for name, raw in outputs.items():
            path = originals.safe_path(project, parser_batch.FOLDER + "/" + name)
            body_corpus.require(not path.exists() or path.read_bytes() == raw, "기존 첨부 근거 자료 충돌: " + name)
        manifest = json.loads(outputs["evidence.manifest.json"])
        for relative, expected in manifest["input_files"].items():
            body_corpus.require(body_corpus.digest(originals.safe_path(project, relative).read_bytes()) == expected,
                                "근거 자료 준비 중 입력 변경")
        for name, raw in outputs.items():
            path = originals.safe_path(project, parser_batch.FOLDER + "/" + name)
            if not path.exists():
                body_corpus.source_import.write_new(path, raw)
        return run_batch(project, check_only=True)


if __name__ == "__main__":
    main()
