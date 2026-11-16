
from project_paths import PROJECT_ROOT
import hashlib
import json
from pathlib import Path


STEM = "2171833_22740725"


def build_search_data(evidence):
    attachment_id = evidence["attachment_id"]
    if attachment_id != "2171833:22740725":
        raise ValueError("이번 코드는 검토한 별표 2를 대상으로 합니다.")
    if evidence["table_link_status"] != "linked":
        raise ValueError("표의 원문 연결을 먼저 완료하세요.")

    source = evidence["source"]
    cells = evidence["extraction"]["table_cells"]
    context = evidence["extraction"]["context_blocks"]
    links = {
        item["table_order"]: item["parse_elements"]
        for item in evidence["table_links"]
    }

    # 같은 표의 같은 행을 묶습니다. 인덱스는 0부터 시작합니다.
    groups = {}
    for index, cell in enumerate(cells):
        if not cell["row_label"]:
            raise ValueError("행 이름이 없는 셀은 별도 검토가 필요합니다.")
        key = (cell["table_order"], cell["row_label"])
        groups.setdefault(key, []).append((index, cell))

    # 별표 2의 두 번째 논리 표는 비고 11의 하위 항목입니다.
    parents = [
        (index, block)
        for index, block in enumerate(context)
        if block["block_label"] == "비고 11"
    ]
    if len(parents) != 1:
        raise ValueError("비고 11의 상위 조건을 하나로 찾을 수 없습니다.")
    parent_index, parent = parents[0]

    parent_refs = [
        {"page": e["page"], "element_id": e["id"]}
        for e in evidence["parse_elements"]
        if e["page"] == 4 and e["id"] == 22
        and e["category"] == "paragraph"
    ]
    if len(parent_refs) != 1:
        raise ValueError("검토한 비고 11의 원문 요소가 없습니다.")

    chunks = []
    for number, ((order, row_label), items) in enumerate(
        groups.items(), start=1
    ):
        items.sort(key=lambda item: (item[1]["page_number"], item[0]))
        pages = sorted({cell["page_number"] for _, cell in items})
        refs = [
            dict(ref) for ref in links[order]
            if ref["page"] in pages
        ]
        if {ref["page"] for ref in refs} != set(pages):
            raise ValueError(f"원문 연결이 빠진 행입니다: {row_label}")

        lines = [
            source["ordin_name"],
            f'{source["attachment_label"]}: {source["attachment_title"]}',
        ]
        context_indices = []
        if order == 2:
            lines.append(f'상위 조건 — 비고 11: {parent["text"]}')
            refs.extend(parent_refs)
            context_indices.append(parent_index)

        lines.append(f"행: {row_label}")
        for _, cell in items:
            column = cell["column_header_path"] or "내용"
            lines.append(f'{column}: {cell["value_text"]}')
            if cell["unit_text"]:
                lines.append(f'단위: {cell["unit_text"]}')

        chunks.append({
            "chunk_id": f"{attachment_id}:table:{order}:chunk:{number:03d}",
            "attachment_id": attachment_id,
            "table_order": order,
            "row_label": row_label,
            "text": "\n".join(lines),
            "source_cell_indices": [index for index, _ in items],
            "source_context_indices": context_indices,
            "source_refs": refs,
            "context_attachment_id": attachment_id,
        })

    return {
        "schema_version": 1,
        "attachment_id": attachment_id,
        "source": source,
        "input_content_hash": evidence["input_content_hash"],
        "review_status": evidence["review_status"],
        "chunks": chunks,
        "attachment_context": {
            "attachment_id": attachment_id,
            "blocks": context,
        },
    }


def main():
    base_dir = PROJECT_ROOT
    folder = base_dir / "data" / "processed" / "attachments"
    input_path = folder / f"{STEM}.evidence.json"
    raw = input_path.read_bytes()
    evidence = json.loads(raw.decode("utf-8-sig"))

    result = build_search_data(evidence)
    result["evidence_file"] = input_path.name
    result["evidence_content_hash"] = hashlib.sha256(raw).hexdigest()

    output_path = folder / f"{STEM}.search_chunks.json"
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("검색 조각:", len(result["chunks"]))
    print("공통 문맥:", len(result["attachment_context"]["blocks"]))
    print("저장:", output_path)


# 배치 후보 생성은 위의 별표 2 실습 결과를 덮어쓰지 않습니다.
import html
import re

import build_attachment_evidence as layout_evidence
import build_ordinance_corpus as body
import download_attachment as originals
import parse_attachment as parse
from request_lock import request_lock

FOLDER = parse.FOLDER + "/chunks"
CORRECTIONS_FILE = FOLDER + "/corrections.json"
CORRECTIONS_SHA256 = "e536f529719904b84a26e4bb66b3c1a82f828e4ff65b38180df29eecd438c48a"


# 행·열·셀 ID는 0부터 시작하며 병합 셀은 같은 원본 ID를 참조합니다.
# 격자 모양이 정상이라는 사실만으로 추출 내용의 정확성을 확정하지 않습니다.

import html as _table_html
import re as _table_re
from html.parser import HTMLParser as _TableHTMLParser

from build_attachment_evidence import LayoutHTML


_TABLE_MAX_SPAN = 1000
_TABLE_MAX_ROWS = 10000
_TABLE_MAX_COLUMNS = 1000
_TABLE_MAX_GRID_SLOTS = 1_000_000


class _TableGridParser(_TableHTMLParser):
    """Require explicit, unambiguous table/row/cell boundaries."""

    _sections = {"thead", "tbody", "tfoot"}
    _structural = {"table", "tr", "td", "th", "caption", "thead", "tbody", "tfoot"}

    def __init__(self, markup):
        super().__init__(convert_charrefs=False)
        self.markup = markup
        self.line_starts = [0] + [i + 1 for i, char in enumerate(markup) if char == "\n"]
        self.rows = []
        self.cell = None
        self.row = None
        self.section = None
        self.caption = False
        self.caption_seen = False
        self.table_open = False
        self.table_seen = False
        self.table_closed = False
        self.cell_count = 0

    def fail(self, reason):
        raise ValueError("표 격자 검토 필요: " + reason)

    def _source_offset(self):
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def span(self, attrs, name):
        values = [value for key, value in attrs if key == name]
        if not values:
            return 1
        if len(values) != 1 or values[0] is None:
            self.fail(name + " 속성이 중복되었거나 값이 없습니다")
        value = values[0].strip()
        if not _table_re.fullmatch(r"[0-9]+", value):
            self.fail(name + " 값이 양의 정수가 아닙니다")
        number = int(value)
        if not 1 <= number <= _TABLE_MAX_SPAN:
            self.fail(name + " 값이 허용 범위 밖입니다")
        return number

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self.table_seen:
                self.fail("중첩 표 또는 여러 표가 있습니다")
            self.table_seen = self.table_open = True
            return
        if tag == "tr":
            if not self.table_open or self.row is not None or self.cell is not None or self.caption:
                self.fail("행이 중첩되었거나 표 밖에 있습니다")
            if len(self.rows) >= _TABLE_MAX_ROWS:
                self.fail("행 수가 허용 범위 밖입니다")
            self.row = len(self.rows)
            self.rows.append([])
            return
        if tag in ("td", "th"):
            if not self.table_open or self.row is None or self.cell is not None or self.caption:
                self.fail("셀이 중첩되었거나 행 밖에 있습니다")
            self.cell = {
                "cell_id": self.cell_count,
                "row": self.row,
                "column": None,
                "rowspan": self.span(attrs, "rowspan"),
                "colspan": self.span(attrs, "colspan"),
                "tag": tag,
                "inner_start": self._source_offset() + len(self.get_starttag_text()),
            }
            self.cell_count += 1
            return
        if tag in self._sections:
            if (not self.table_open or self.section is not None or self.row is not None
                    or self.cell is not None or self.caption):
                self.fail("표 구역이 중첩되었거나 잘못된 위치에 있습니다")
            self.section = tag
            return
        if tag == "caption":
            if (not self.table_open or self.caption_seen or self.section is not None
                    or self.row is not None or self.cell is not None or self.rows):
                self.fail("표 제목이 중복되었거나 잘못된 위치에 있습니다")
            self.caption = self.caption_seen = True
            return
        # Inline markup inside cells is preserved verbatim. Formatting cannot
        # create additional rows or cells, which are handled above.
        if self.cell is None and not self.caption:
            self.fail("셀 밖에 지원하지 않는 태그가 있습니다: " + tag)

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self.cell is None or self.cell["tag"] != tag:
                self.fail("셀의 시작/종료 태그가 맞지 않습니다")
            cell = self.cell
            inner_start = cell.pop("inner_start")
            cell["html"] = self.markup[inner_start:self._source_offset()]
            cell["text"] = LayoutHTML(cell["html"]).text
            self.rows[self.row].append(cell)
            self.cell = None
            return
        if tag == "tr":
            if self.row is None or self.cell is not None:
                self.fail("행 또는 셀 종료 태그가 없습니다")
            self.row = None
            return
        if tag in self._sections:
            if self.section != tag or self.row is not None or self.cell is not None or self.caption:
                self.fail("표 구역의 시작/종료 태그가 맞지 않습니다")
            self.section = None
            return
        if tag == "caption":
            if not self.caption:
                self.fail("표 제목의 시작/종료 태그가 맞지 않습니다")
            self.caption = False
            return
        if tag == "table":
            if (not self.table_open or self.row is not None or self.cell is not None
                    or self.section is not None or self.caption):
                self.fail("표, 행 또는 셀이 닫히지 않았습니다")
            self.table_open = False
            self.table_closed = True
            return
        if self.cell is None and not self.caption:
            self.fail("셀 밖에 지원하지 않는 종료 태그가 있습니다: " + tag)

    def handle_startendtag(self, tag, attrs):
        if tag in self._structural:
            self.fail("표 구조 태그는 시작/종료를 명시해야 합니다: " + tag)
        if self.cell is None and not self.caption:
            self.fail("셀 밖에 지원하지 않는 태그가 있습니다: " + tag)

    def handle_data(self, data):
        if self.cell is None and not self.caption and data.strip():
            self.fail("셀 밖에 내용이 있습니다")

    def handle_entityref(self, name):
        self.handle_data(_table_html.unescape("&" + name + ";"))

    def handle_charref(self, name):
        self.handle_data(_table_html.unescape("&#" + name + ";"))

    def handle_decl(self, decl):
        self.fail("문서 선언은 표 조각에 허용하지 않습니다")

    def handle_pi(self, data):
        self.fail("처리 명령은 표 조각에 허용하지 않습니다")


def parse_table(markup):
    """Return exact source cells and their rectangular, span-expanded grid.

    ``header_rows`` counts physical rows before the first row containing a
    ``td``. Later ``th`` cells remain row labels, not new column headers.
    A table beginning with ``td`` has zero header rows; this does not infer any
    missing headers or a continuation relationship with another table.
    """
    if not isinstance(markup, str) or not markup.strip():
        raise ValueError("표 HTML이 없습니다")
    parsed = _TableGridParser(markup)
    parsed.feed(markup)
    parsed.close()
    if (not parsed.table_seen or not parsed.table_closed or parsed.table_open
            or parsed.row is not None or parsed.cell is not None
            or parsed.section is not None or parsed.caption):
        parsed.fail("표 구조가 완전히 닫히지 않았습니다")
    if not parsed.rows or not parsed.cell_count:
        parsed.fail("행 또는 셀이 없습니다")

    row_count = len(parsed.rows)
    header_rows = next((index for index, row in enumerate(parsed.rows)
                        if any(cell["tag"] == "td" for cell in row)), row_count)
    occupied, cells = {}, []
    column_count = 0
    for row_index, row in enumerate(parsed.rows):
        column = 0
        for cell in row:
            while (row_index, column) in occupied:
                column += 1
            end_row = row_index + cell["rowspan"]
            end_column = column + cell["colspan"]
            if end_row > row_count:
                parsed.fail("rowspan이 마지막 행을 넘어갑니다")
            if end_column > _TABLE_MAX_COLUMNS or row_count * end_column > _TABLE_MAX_GRID_SLOTS:
                parsed.fail("펼친 표의 크기가 허용 범위 밖입니다")
            cell["column"] = column
            for target_row in range(row_index, end_row):
                for target_column in range(column, end_column):
                    key = target_row, target_column
                    if key in occupied:
                        parsed.fail("병합 셀이 기존 셀과 겹칩니다")
                    occupied[key] = cell["cell_id"]
            cells.append(cell)
            column = end_column
            column_count = max(column_count, end_column)

    expected_slots = row_count * column_count
    if len(occupied) != expected_slots:
        parsed.fail("행마다 열 수가 다르거나 빈 격자 위치가 있습니다")
    grid = [[occupied[row, column] for column in range(column_count)]
            for row in range(row_count)]
    return {"cells": cells, "grid": grid, "header_rows": header_rows,
            "rows": row_count, "columns": column_count}


def load_corrections(project, documents, inputs):
    raw = originals.safe_path(project, CORRECTIONS_FILE).read_bytes()
    body.require(body.digest(raw) == CORRECTIONS_SHA256, "검토한 교정 데이터가 변경됐습니다")
    data = json.loads(raw)
    findings_raw = originals.safe_path(project, parse.FOLDER + "/review_findings.json").read_bytes()
    findings = {f["finding_id"]: f for f in json.loads(findings_raw)["findings"]}
    body.require(data["review_findings_content_hash"] == body.digest(findings_raw)
                 and data["source_review"] == "pending" and data["answer_allowed"] is False,
                 "교정 데이터의 검토 상태/출처 불일치")
    corrections = {}
    by_id = {d["attachment_id"]: d for d in documents}
    for correction in data["corrections"]:
        finding = findings[correction["finding_id"]]
        doc = by_id[correction["attachment_id"]]
        key = (correction["attachment_id"], correction["page"], correction["element_id"])
        body.require(key not in corrections and correction["answer_allowed"] is False, "교정 중복/상태 오류")
        element = next(e for e in doc["elements"] if (e["page"], e["element_id"]) == key[1:])
        for field in ("attachment_id", "page", "element_id", "parse_file", "parse_content_hash", "crop_content_hash"):
            body.require(correction[field] == finding[field], "교정 원본 위치 불일치")
        body.require(body.digest(originals.safe_path(project, correction["layout_file"]).read_bytes()) == correction["layout_content_hash"]
                     and doc["source"]["content_hash"] == correction["xml_content_hash"]
                     and body.digest(element["html"].encode("utf-8")) == correction["original_html_hash"]
                     and body.digest(element["text"].encode("utf-8")) == correction["original_text_hash"],
                     "교정 대상 원문 해시 불일치")
        # 상위 evidence 검사에서 이미지/인용 해시도 확인합니다.
        corrections[key] = correction
    body.require({c["finding_id"] for c in corrections.values()} == set(findings), "교정되지 않은 기존 검토 항목")
    inputs[CORRECTIONS_FILE] = body.digest(raw)
    return corrections


def source_ref(doc, element):
    return {"attachment_id": doc["attachment_id"], "parse_file": doc["parse_file"],
            "parse_content_hash": doc["parse_content_hash"], "page": element["page"],
            "element_id": element["element_id"], "coordinates": element["coordinates"]}


def add_context(output, key, text, refs, kind):
    ident = output["attachment_id"] + ":context:" + key
    value = {"context_id": ident, "kind": kind, "text": text, "source_refs": refs}
    existing = next((c for c in output["contexts"] if c["context_id"] == ident), None)
    body.require(existing is None or existing == value, "문맥 ID 충돌")
    if existing is None:
        output["contexts"].append(value)
    return ident


def make_chunk(output, suffix, label, text, refs, contexts, kind, fields=None, correction=None):
    chunk = {"chunk_id": output["attachment_id"] + ":candidate:" + suffix,
             "attachment_id": output["attachment_id"], "label": label, "kind": kind,
             "text": text, "source_refs": refs, "required_context_ids": list(dict.fromkeys(contexts)),
             "related_body_chunk_ids": [r["chunk_id"] for r in output["related_body"]["body_references"]],
             "input_binding_status": output["input_binding_status"], "source_review": "pending",
             "embedding_allowed": False, "answer_allowed": False,
             "fields": fields or [], "correction_id": correction}
    output["chunks"].append(chunk)
    output.setdefault("context_contract", {})[chunk["chunk_id"]] = list(chunk["required_context_ids"])
    return chunk


def header_path(table, column):
    ids = []
    for row in range(table["header_rows"]):
        ident = table["grid"][row][column]
        if ident not in ids:
            ids.append(ident)
    return [table["cells"][i]["text"] for i in ids if table["cells"][i]["text"]]


def table_candidates(output, doc, element, markup, common_contexts, correction=None):
    table = parse_table(markup)
    body.require(table["header_rows"] > 0, "머리글 없는 표는 별도 검토가 필요합니다")
    footer_rows, footer_contexts = set(), []
    for r in range(table["header_rows"], table["rows"]):
        unique = list(dict.fromkeys(table["grid"][r]))
        if len(unique) == 1 and table["cells"][unique[0]]["text"].startswith(("※", "비고", "(주)")):
            footer_rows.add(r)
            cell = table["cells"][unique[0]]
            footer_contexts.append(add_context(output, f"p{element['page']}-e{element['element_id']}-footer{r}",
                                              cell["text"], [source_ref(doc, element)], "table_note"))
    for r in range(table["header_rows"], table["rows"]):
        if r in footer_rows:
            continue
        fields, lines = [], []
        for ident in dict.fromkeys(table["grid"][r]):
            cell = table["cells"][ident]
            paths = [header_path(table, c) for c in range(cell["column"], cell["column"] + cell["colspan"])]
            paths = [list(p) for p in dict.fromkeys(tuple(p) for p in paths)]
            field = {"column_paths": paths, "value": cell["text"], "original_cell_html": cell["html"],
                     "cell_origin": {k: cell[k] for k in ("row", "column", "rowspan", "colspan")},
                     "cell_origin_basis": "corrected_html" if correction else "parse_html"}
            # 동문 기호는 값을 바꾸지 않고 동일 열의 앞 셀을 참조합니다. 빈 셀은 상속하지 않습니다.
            if cell["text"].strip() in ("〃", "″", '"'):
                for previous in range(r - 1, table["header_rows"] - 1, -1):
                    other = table["cells"][table["grid"][previous][cell["column"]]]
                    if other["text"].strip() and other["text"].strip() not in ("〃", "″", '"'):
                        field["ditto_reference"] = {"value": other["text"], "row": other["row"], "column": other["column"]}
                        break
                body.require("ditto_reference" in field, "동문 기호의 선행 셀이 없습니다")
            fields.append(field)
            heading = " | ".join(" > ".join(p) for p in paths) or "열"
            value = field["value"] if field["value"] else "[원문 빈 셀: 값 추정 안 함]"
            if "ditto_reference" in field:
                value += " [동일 열 앞 셀: " + field["ditto_reference"]["value"] + "]"
            lines.append(heading + ": " + value)
        label = " / ".join(f["value"] for f in fields[:3] if f["value"])
        suffix = f"p{element['page']}-e{element['element_id']}-r{r}"
        ref = {**source_ref(doc, element), "representation": "corrected_html" if correction else "parse_html", "row_index": r}
        chunk = make_chunk(output, suffix, label, "\n".join(lines), [ref], common_contexts + footer_contexts,
                           "table_row", fields, correction)
        chunk["split_policy"] = "row_with_all_merged_cells_and_full_header_paths; internal value lists not split"


def corrected_records(output, doc, element, correction, common_contexts):
    for index, record in enumerate(correction["records"]):
        context = add_context(output, correction["finding_id"] + "-" + str(index), record["context"],
                              [source_ref(doc, element)], "reviewed_record_context")
        fields = [{"name": k, "value": v} for k, v in record["fields"].items()]
        make_chunk(output, correction["finding_id"] + "-" + str(index), record["label"],
                   "\n".join(k + ": " + v for k, v in record["fields"].items()),
                   [{**source_ref(doc, element), "representation": "image_and_xml_transcription",
                     "correction_record_index": index}], common_contexts + [context], "corrected_record", fields,
                   correction["finding_id"])


def legacy_candidates(project, output, doc, inputs):
    relative = "data/processed/attachments/2171833_22740725.search_chunks.json"
    evidence_file = "data/processed/attachments/2171833_22740725.evidence.json"
    raw, evidence_raw = (originals.safe_path(project, p).read_bytes() for p in (relative, evidence_file))
    saved, legacy_evidence = json.loads(raw), json.loads(evidence_raw)
    expected = build_search_data(legacy_evidence)
    expected.update({"evidence_file": Path(evidence_file).name, "evidence_content_hash": body.digest(evidence_raw)})
    body.require(saved == expected, "별표 2의 기존 검색 조각과 근거가 다릅니다")
    inputs[relative], inputs[evidence_file] = body.digest(raw), body.digest(evidence_raw)
    indexed = {(e["page"], e["element_id"]): e for e in doc["elements"]}
    refs = [source_ref(doc, e) for e in doc["elements"] if e["category"] not in ("table", "figure", "chart")]
    context = add_context(output, "legacy-all-notes", "\n".join(b["text"] for b in saved["attachment_context"]["blocks"]), refs,
                          "legacy_full_attachment_conditions")
    for number, chunk in enumerate(saved["chunks"]):
        source_refs = [source_ref(doc, indexed[(r["page"], r["element_id"])]) for r in chunk["source_refs"]]
        item = make_chunk(output, "legacy-" + str(number), chunk["row_label"], chunk["text"], source_refs,
                          [context], "legacy_logical_row")
        item["legacy_chunk_id"] = chunk["chunk_id"]
        item["hold_reason"] = "manual_export_input_binding_unverified"


def document_candidates(project, doc, corrections, inputs):
    output = {"attachment_id": doc["attachment_id"], "ordin_name": doc["ordin_name"],
              "title": doc["attachment"]["title"], "source": doc["source"],
              "input_binding_status": doc["input_binding_status"], "related_body": doc["related_body"],
              "contexts": [], "chunks": [], "source_review": "pending", "answer_allowed": False}
    if doc["input_binding_status"] == "manual_export_unverified":
        legacy_candidates(project, output, doc, inputs)
        return output
    non_tables = [e for e in doc["elements"] if e["category"] not in ("table", "figure", "chart")]
    # 페이지를 넘는 비고도 순서를 보존한 한 문맥으로 유지합니다.
    context = add_context(output, "attachment-text", "\n".join(layout_evidence.LayoutHTML(e["html"]).text for e in non_tables),
                          [source_ref(doc, e) for e in non_tables], "ordered_attachment_text_across_pages")
    preceding_heading = None
    for element in doc["elements"]:
        if element["category"].startswith("heading") or element["category"] == "caption":
            preceding_heading = element
        key = (doc["attachment_id"], element["page"], element["element_id"])
        correction = corrections.get(key)
        if correction and correction["operation"] in ("replace_figure_with_records", "replace_table_with_records"):
            corrected_records(output, doc, element, correction, [context])
        elif element["category"] == "table":
            markup = correction["corrected_html"] if correction else element["html"]
            contexts = [context]
            if preceding_heading is not None:
                contexts.append(add_context(output, f"p{element['page']}-e{element['element_id']}-heading",
                                            layout_evidence.LayoutHTML(preceding_heading["html"]).text,
                                            [source_ref(doc, preceding_heading)], "preceding_table_heading"))
            table_candidates(output, doc, element, markup, contexts, correction["finding_id"] if correction else None)
        elif element["category"] in ("figure", "chart"):
            chunk = make_chunk(output, f"p{element['page']}-e{element['element_id']}-image", doc["attachment"]["title"],
                               doc["attachment"]["title"], [source_ref(doc, element)], [context], "image_metadata_only")
            chunk["hold_reason"] = "unverified_visual_content_excluded_from_text"
    body.require(output["chunks"], "검색 후보가 없는 첨부")
    return output


def assemble_chunk(document, chunk):
    """검색 시 숫자만 꺼내지 않고 연결된 문맥과 본문을 함께 복원하는 계약입니다."""
    body.require(chunk in document["chunks"] and chunk["attachment_id"] == document["attachment_id"], "다른 첨부의 조각")
    body.require(chunk["required_context_ids"] == document["context_contract"][chunk["chunk_id"]]
                 and chunk["required_context_ids"], "필수 문맥 연결이 변경됐습니다")
    registry = {c["context_id"]: c for c in document["contexts"]}
    body.require(len(registry) == len(document["contexts"]), "중복 문맥 ID")
    parts = [document["ordin_name"], document["title"], chunk["text"]]
    for ident in chunk["required_context_ids"]:
        body.require(ident in registry, "필수 문맥이 빠졌습니다")
        parts.append("[함께 확인할 문맥]\n" + registry[ident]["text"])
    body_refs = document["related_body"]["body_references"]
    body.require(chunk["related_body_chunk_ids"] == [r["chunk_id"] for r in body_refs], "관련 본문 연결 누락")
    for ref in body_refs:
        parts.append("[명시 참조 본문: 적용 여부는 별도 판단]\n" + ref["quote_text"])
    return "\n\n".join(parts)


def preview_html(documents, report, corrections):
    esc = html.escape
    kind_labels = {"table_row": "표의 행", "corrected_record": "원문으로 교정한 항목",
                   "legacy_logical_row": "기존 논리 표의 행", "image_metadata_only": "그림 목록용"}
    correction_labels = {"family_parking_dimension_misattribution": "가족배려 구획 유형별 치수",
                         "camping_fee_cell_line_boundaries_lost": "캠핑장 연령별 요금",
                         "delegation_table_vertical_alignment_lost": "사무·근거법령·수임기관 대응"}
    counts = "".join(f'<tr><td>{esc(d["ordin_name"])}</td><td>{esc(d["title"])}</td><td>{len(d["chunks"])}</td></tr>' for d in documents)
    panels = []
    for d in documents:
        chunks = "".join(f'<details><summary>{esc(c["label"][:130])} · {kind_labels[c["kind"]]}</summary><h4>검색 단위</h4><pre>{esc(c["text"])}</pre><details><summary>필수 문맥·관련 본문을 포함해 보기</summary><pre>{esc(assemble_chunk(d, c))}</pre></details><p>원문 위치: {esc(", ".join("p." + str(r["page"]) + " 요소 " + str(r["element_id"]) for r in c["source_refs"]))}</p><p>문맥 {len(c["required_context_ids"])}묶음 · 검수 대기 · 답변 사용 보류</p></details>' for c in d["chunks"])
        panels.append(f'<section><h2>{esc(d["ordin_name"])} · {esc(d["title"])}</h2>{chunks}</section>')
    corrections_html = "".join(f'<li>{correction_labels[c["finding_id"]]} — 이미지·XML 대조 후 별도 교정본 작성</li>' for c in corrections.values())
    return ('''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>첨부 검색 단위와 교정 결과</title><style>body{font-family:system-ui,sans-serif;background:#f5f7fb;color:#172635;max-width:1200px;margin:auto;padding:26px;line-height:1.7}section{background:white;border:1px solid #d8e1ec;border-radius:12px;padding:22px;margin:24px 0}table{border-collapse:collapse;width:100%}td,th{border:1px solid #becbda;padding:8px}details{border-top:1px solid #d8e1ec;padding:12px 0}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit;background:#eef3f9;padding:14px}p{overflow-wrap:anywhere}</style><h1>첨부를 어떤 검색 단위로 나눴는가</h1>'''
            + f'<p>14개 첨부 → 후보 {report["chunk_count"]}개. 고정 개수가 아니라 표의 행·검증한 관계·기존 논리 표 구조에 따라 달라집니다. 검수 완료와 임베딩을 뜻하지 않습니다.</p>'
            + '<p>병합된 머리글과 비고를 함께 보존합니다. 빈 칸을 0으로 바꾸지 않고, 한 셀의 여러 값을 임의로 짝짓지 않습니다. 미검증 그림 설명은 검색 텍스트에서 제외했습니다.</p>'
            + '<p><a href="../review.html">원본 추출·이미지 대조 화면</a></p><h2>별도 교정본</h2><ul>' + corrections_html + '</ul><table><tr><th>조례</th><th>첨부</th><th>후보 수</th></tr>' + counts + '</table>' + "".join(panels) + '</html>').encode("utf-8")


def prepare_batch(project):
    project = Path(project).resolve(strict=True)
    layout_evidence.run_batch(project, check_only=True)
    old_manifest_raw = originals.safe_path(project, layout_evidence.BATCH_MANIFEST).read_bytes()
    old_manifest = json.loads(old_manifest_raw)
    inputs = {**old_manifest["input_files"], layout_evidence.BATCH_MANIFEST: body.digest(old_manifest_raw)}
    documents = []
    for name, expected in old_manifest["output_files"].items():
        relative = parse.FOLDER + "/" + name
        raw = originals.safe_path(project, relative).read_bytes()
        body.require(body.digest(raw) == expected, "기존 layout 산출물 해시 불일치")
        inputs[relative] = expected
        if name.endswith(".layout_evidence.json"):
            documents.append(json.loads(raw))
    corrections = load_corrections(project, documents, inputs)
    candidates = [document_candidates(project, d, corrections, inputs) for d in documents]
    all_chunks = [c for d in candidates for c in d["chunks"]]
    body.require(len({c["chunk_id"] for c in all_chunks}) == len(all_chunks), "검색 후보 ID 중복")
    lengths = [len(assemble_chunk(d, c)) for d in candidates for c in d["chunks"]]
    report = {"attachment_count": len(candidates), "chunk_count": len(all_chunks), "correction_count": len(corrections),
              "manual_binding_held_chunks": sum(c["input_binding_status"] == "manual_export_unverified" for c in all_chunks),
              "image_metadata_only_chunks": sum(c["kind"] == "image_metadata_only" for c in all_chunks),
              "max_text_with_context_characters": max(lengths), "source_review": "pending", "answer_allowed": False,
              "embedding_performed": False, "by_attachment": [{"attachment_id": d["attachment_id"], "title": d["title"],
                "chunks": len(d["chunks"]), "contexts": len(d["contexts"])} for d in candidates]}
    data = {"schema_version": "attachment-search-candidates-v1", "source_review": "pending", "answer_allowed": False,
            "documents": candidates}
    outputs = {"candidate_chunks.json": body.json_bytes(data), "chunk_report.json": body.json_bytes(report),
               "preview.html": preview_html(candidates, report, corrections)}
    manifest = {"pipeline_version": "attachment-search-candidates-v1", "input_files": inputs,
                "output_files": {name: body.digest(raw) for name, raw in outputs.items()}, "report": report}
    outputs["chunks.manifest.json"] = body.json_bytes(manifest)
    return outputs, report


def run_batch(project, check_only=False):
    if check_only:
        outputs, report = prepare_batch(project)
        for name, raw in outputs.items():
            body.require(originals.safe_path(project, FOLDER + "/" + name).read_bytes() == raw, "검색 후보 산출물 불일치: " + name)
        return report
    with request_lock(originals.safe_path(project, FOLDER + "/.chunks.lock")):
        outputs, report = prepare_batch(project)
        for name, raw in outputs.items():
            path = originals.safe_path(project, FOLDER + "/" + name)
            body.require(not path.exists() or path.read_bytes() == raw, "기존 검색 후보 충돌: " + name)
        manifest = json.loads(outputs["chunks.manifest.json"])
        for relative, expected in manifest["input_files"].items():
            body.require(body.digest(originals.safe_path(project, relative).read_bytes()) == expected, "검색 후보 준비 중 입력 변경")
        for name, raw in outputs.items():
            path = originals.safe_path(project, FOLDER + "/" + name)
            if not path.exists():
                body.source_import.write_new(path, raw)
        return run_batch(project, check_only=True)


if __name__ == "__main__":
    main()
