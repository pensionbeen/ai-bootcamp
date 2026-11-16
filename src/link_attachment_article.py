"""별표 2의 원문 근거에 관련 조·항을 연결합니다. API 호출이나 답변 생성은 없습니다."""

from project_paths import PROJECT_ROOT

import argparse
import copy
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

from evidence_gate import (
    ATTACHMENT_ID, STEM, GateError, digest_bytes, load_corpus, normalized,
    validate_decision,
)
from search_attachment_chunks import read_json, save_json

PARAGRAPH_START = re.compile(r"^[ \t]*([①-⑳㉑-㉟㊱-㊿])", re.MULTILINE)
ANNEX_REFERENCE = re.compile(r"별표\s*2(?!\d|\s*의\s*\d)")
TITLE_REFERENCE = re.compile(r"\((제\d+조(?:의\d+)?)\s*제(\d+)항\s*관련\)")


def exactly_one(items, code):
    if len(items) != 1:
        raise GateError(code)
    return items[0]


def raw_paragraphs(article):
    heading = article["article"] + (f'({article["title"]})' if article["title"] else "")
    if not article["text"].startswith(heading):
        raise GateError("invalid_article_heading")
    body = article["text"][len(heading):].strip()
    lines = body.splitlines()
    while lines and lines[-1].strip().startswith("[") and lines[-1].strip().endswith("]"):
        lines.pop()
    body = "\n".join(lines).strip()
    markers = list(PARAGRAPH_START.finditer(body))
    if not markers:
        return [(None, body)]
    numbers = [int(unicodedata.numeric(m.group(1))) for m in markers]
    if body[:markers[0].start()].strip() or numbers != list(range(1, len(markers) + 1)):
        raise GateError("ambiguous_paragraph_numbers")
    return [(numbers[i], body[m.start():markers[i + 1].start()
                             if i + 1 < len(markers) else len(body)].strip())
            for i, m in enumerate(markers)]


def load_related_provisions(project_dir, corpus):
    source = corpus["document"]["source"]
    data = project_dir / "data"
    paths = {
        "ordinance_xml": data / "raw" / "2171833.xml",
        "articles": data / "processed" / "2171833.articles.json",
        "paragraph_chunks": data / "processed" / "2171833.chunks.json",
        "attachments": data / "processed" / "2171833.attachments.json",
    }
    xml_bytes = paths["ordinance_xml"].read_bytes()
    xml_hash = digest_bytes(xml_bytes)
    if xml_hash != source["ordinance_xml_content_hash"]:
        raise GateError("ordinance_xml_hash_mismatch")
    root = ET.fromstring(xml_bytes)
    basic = root.find("자치법규기본정보")
    if basic is None:
        raise GateError("missing_ordinance_metadata")
    fields = {
        "지자체기관명": "서울특별시", "자치법규ID": source["ordin_id"],
        "자치법규일련번호": source["ordin_seq"], "자치법규명": source["ordin_name"],
        "시행일자": source["effective_date"].replace("-", ""),
    }
    if any(basic.findtext(k) != v for k, v in fields.items()):
        raise GateError("ordinance_identity_mismatch")
    articles = read_json(paths["articles"])
    attachments = read_json(paths["attachments"])
    chunks = read_json(paths["paragraph_chunks"])
    metadata_fields = ("ordin_id", "ordin_seq", "as_of_date", "effective_date", "source_url")
    for document in (articles, attachments):
        if (document["ordin_name"] != source["ordin_name"]
                or document["metadata"]["content_hash"] != xml_hash
                or any(document["metadata"].get(k) != source[k] for k in metadata_fields)):
            raise GateError("processed_source_metadata_mismatch")
    original_articles = []
    for node in root.findall("./조문/조"):
        if node.findtext("조문여부") != "Y":
            continue
        text = node.findtext("조내용", "").strip()
        number = re.match(r"^(제\d+조(?:의\d+)?)", text)
        if number is None:
            raise GateError("invalid_raw_article_number")
        original_articles.append({"article_code": node.findtext("조문번호"),
                                  "article": number.group(1), "title": node.findtext("조제목", ""),
                                  "text": text})
    if original_articles != articles["articles"]:
        raise GateError("article_text_differs_from_xml")
    attachment = exactly_one([a for a in attachments["attachments"]
                              if a["attachment_id"] == ATTACHMENT_ID], "attachment_not_unique")
    raw_attachment = exactly_one([a for a in root.iter("별표단위")
                                  if a.attrib.get("별표키") == attachment["attachment_key"]],
                                 "raw_attachment_not_unique")
    if (raw_attachment.findtext("별표제목") != attachment["title"]
            or attachment["title"] != source["attachment_title"]
            or raw_attachment.findtext("별표구분") != "별표"
            or raw_attachment.findtext("별표번호") != "0002"
            or raw_attachment.findtext("별표가지번호") != "00"):
        raise GateError("attachment_identity_mismatch")
    declared = exactly_one(TITLE_REFERENCE.findall(attachment["title"]), "ambiguous_related_article")
    declared_article, declared_paragraph_text = declared
    declared_paragraph = int(declared_paragraph_text)
    expected_related_text = f"({declared_article}제{declared_paragraph}항 관련)"
    if normalized(corpus["evidence"]["extraction"]["related_article_text"]) != normalized(expected_related_text):
        raise GateError("extracted_related_article_mismatch")

    # 원본 XML 전체에서 명시적인 별표 2 참조를 찾아 보존합니다.
    # 제목에 적힌 조·항과 다른 참조 조·항은 자동으로 동일한 적용조건으로 간주하지 않습니다.
    provisions = []
    for article in original_articles:
        for paragraph, text in raw_paragraphs(article):
            if not ANNEX_REFERENCE.search(text):
                continue
            chunk_id = f'2171833:{article["article_code"]}:{paragraph if paragraph is not None else "all"}'
            chunk = exactly_one([c for c in chunks if c["chunk_id"] == chunk_id], "paragraph_chunk_not_unique")
            if (chunk["text"] != text or chunk["article"] != article["article"]
                    or chunk["paragraph"] != paragraph or chunk["article_code"] != article["article_code"]
                    or chunk["ordin_name"] != source["ordin_name"] or chunk["is_deleted"]
                    or chunk["content_hash"] != xml_hash
                    or any(chunk.get(k) != source[k] for k in metadata_fields)):
                raise GateError("paragraph_chunk_differs_from_source")
            is_declared = article["article"] == declared_article and paragraph == declared_paragraph
            provisions.append({
                "chunk_id": chunk_id, "article": article["article"], "paragraph": paragraph,
                "article_title": article["title"], "quote": text,
                "relationship": "explicit_annex_reference",
                "declared_in_attachment_title": is_declared,
                "use": "declared_related_provision" if is_declared else "additional_context_requires_selection",
                "source_location": {"file": paths["ordinance_xml"].name,
                                    "article_code": article["article_code"], "paragraph": paragraph},
            })
    primary = exactly_one([p for p in provisions if p["declared_in_attachment_title"]],
                         "declared_provision_does_not_refer_to_annex")
    inputs = {key: {"file": path.name, "content_hash": digest_bytes(path.read_bytes())}
              for key, path in paths.items()}
    return primary, provisions, inputs


def build_links(project_dir, corpus):
    primary, provisions, inputs = load_related_provisions(project_dir, corpus)
    units = corpus["units"]
    links = {}
    for uid, unit in units.items():
        location = f'행: {unit["label"]}, 열: {unit["column"]}' if unit["kind"] == "cell" else unit["label"]
        links[uid] = {
            "unit_id": uid, "attachment_id": ATTACHMENT_ID,
            "direct_evidence": copy.deepcopy(unit),
            "related_provision_id": primary["chunk_id"],
            "citation_label": (f'{corpus["document"]["source"]["ordin_name"]} '
                               f'{primary["article"]} 제{primary["paragraph"]}항 관련 별표 2 — {location}'),
        }
    return {
        "schema_version": 1, "attachment_id": ATTACHMENT_ID,
        "link_status": "verified_against_saved_xml",
        "source": corpus["document"]["source"],
        "review_status": corpus["evidence"]["review_status"],
        "input_binding_status": corpus["evidence"]["input_binding_status"],
        "answer_allowed": False,
        "publication_blocks": ["no_final_answer_generation"] + (
            ["source_review_pending"] if corpus["evidence"]["review_status"] != "approved" else []),
        "primary_provision_id": primary["chunk_id"], "related_provisions": provisions,
        "evidence_links": links,
        "search_chunk_links": [{"chunk_id": c["chunk_id"], "related_provision_id": primary["chunk_id"],
                                "direct_unit_ids": [f"{ATTACHMENT_ID}:cell:{i}" for i in c["source_cell_indices"]],
                                "required_context_ids": [f"{ATTACHMENT_ID}:context:{i}" for i in c["source_context_indices"]]}
                               for c in corpus["chunks"].values()],
        "input_files": inputs,
        "evidence_content_hash": corpus["document"]["evidence_content_hash"],
        "search_chunks_content_hash": corpus["embeddings"]["input_content_hash"],
        "notes": ["별표의 수치·조건은 direct_evidence의 원문에서 가져옵니다.",
                  "관련 조항은 별표를 참조하는 문장입니다. 별표 수치가 조항 문장에 있다고 표시하지 않습니다.",
                  "추가 참조 조항은 질문별 적용 여부를 검토해야 하며 자동으로 인용하지 않습니다.",
                  "채점용 citation 배열의 최종 원소 형식은 이 내부 연결 형식과 별도로 정해야 합니다."],
    }


def link_gate_result(result, links, corpus):
    if result.get("gate_status") != "ok":
        raise GateError("gate_result_not_successful")
    if (result.get("source") != links["source"]
            or result.get("evidence_content_hash") != links["evidence_content_hash"]
            or result.get("search_chunks_content_hash") != links["search_chunks_content_hash"]):
        raise GateError("gate_result_source_mismatch")
    if result.get("answer_allowed") is not False or result.get("review_status") != links["review_status"]:
        raise GateError("unexpected_gate_publication_state")
    validated = validate_decision({
        "decision": result["decision"], "reason": result["reason"],
        "missing_information": result["missing_information"],
        "evidence": [{k: c[k] for k in ("unit_id", "quote", "role")} for c in result["evidence"]],
    }, corpus["units"])
    primary = exactly_one([p for p in links["related_provisions"]
                           if p["chunk_id"] == links["primary_provision_id"]], "primary_provision_missing")
    candidates = []
    if validated["decision"] == "supported":
        for cited in validated["evidence"]:
            link = links["evidence_links"][cited["unit_id"]]
            candidates.append({
                "label": link["citation_label"], "article": primary["article"],
                "paragraph": primary["paragraph"], "related_provision": copy.deepcopy(primary),
                "attachment_evidence": copy.deepcopy(cited),
                "source_url": links["source"]["source_url"],
                "effective_date": links["source"]["effective_date"],
            })
    return {**copy.deepcopy(result), "evidence": validated["evidence"],
            "citation_candidates": candidates, "citation_status": "internal_candidates_only",
            "answer_allowed": False, "article_link_status": links["link_status"]}


# 배치 연결은 위의 별표 2 데모와 별개입니다. 숫자·종류가 명시된 원문
# 참조만 보존하며, 이름 유사도나 제목 오타 교정으로 연결하지 않습니다.
BATCH_REFERENCE = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?P<kind>별표|별도)"
    r"(?:\s*(?:제\s*)?(?P<number>[0-9]+)"
    r"(?:\s*의\s*(?P<branch>[0-9]+))?(?:\s*호\s*서식)?)?"
)
BATCH_TITLE_REFERENCE = re.compile(
    r"\((?P<article>제[0-9]+조(?:의[0-9]+)?)"
    r"\s*(?:제\s*(?P<paragraph>[0-9]+)\s*항)?\s*관련\)"
)
EXTERNAL_ANNEX_OWNER = re.compile(
    r"(?:「[^」\n]+」|(?<![가-힣])(?:같은\s*법\s*시행규칙|같은\s*법\s*시행령|"
    r"법\s*시행규칙|법\s*시행령|같은\s*법|같은\s*영|시행규칙|시행령|영|법))"
    r"\s*(?:제\s*[0-9]+\s*조(?:의\s*[0-9]+)?"
    r"(?:\s*제\s*[0-9]+\s*항)?(?:\s*제\s*[0-9]+\s*호)?\s*)?$"
)


def _batch_title_mentions(title):
    return [{"article": m["article"],
             "paragraph": int(m["paragraph"]) if m["paragraph"] else None,
             "reference_text": m.group(),
             "reference_span": {"start": m.start(), "end": m.end()}}
            for m in BATCH_TITLE_REFERENCE.finditer(title)]


def _batch_reference_links(items, documents, chunks_by_seq):
    """검증된 본문에 적용하는 순수 연결 규칙. reference_span은 quote_text 기준."""
    links, by_seq, declared = {}, {}, {}
    for item in items:
        attachment = item["attachment"]
        ident = attachment["attachment_id"]
        if ident in links:
            raise ValueError("배치 첨부 ID가 중복됩니다")
        declared[ident] = _batch_title_mentions(attachment["title"])
        links[ident] = {"body_references": [], "title_reference_status": "not_checked",
                        "title_references": [], "review_notes": [], "answer_allowed": False}
        by_seq.setdefault(item["ordin_seq"], []).append(item)

    for seq, seq_items in by_seq.items():
        for chunk in chunks_by_seq[seq]:
            if chunk["is_deleted"]:
                continue
            text = chunk["quote_text"]
            for match in BATCH_REFERENCE.finditer(text):
                kind, number, branch = match["kind"], match["number"], match["branch"]
                # '별도로 정한다', '별도의 규정'은 첨부가 아닙니다.
                if number is None and kind != "별표":
                    continue
                # 바로 앞의 소유 법령만 봅니다. 같은 문장의 앞부분에서 법을
                # 인용했다는 이유로 이 조례의 별표까지 외부 참조로 보지 않습니다.
                prefix = text[:match.start()].rsplit("\n", 1)[-1]
                if EXTERNAL_ANNEX_OWNER.search(prefix):
                    continue
                same_kind = [i for i in seq_items if i["attachment"]["attachment_type"] == kind]
                if number is None:
                    # 번호 없는 '별표'는 유일한 무번호 첨부이고, 제목의 조·항과
                    # 본문의 참조 위치가 함께 일치할 때만 연결합니다.
                    candidates = [i for i in same_kind
                                  if i["attachment"]["attachment_no"] == "0000"
                                  and i["attachment"]["attachment_branch_no"] == "00"]
                    if len(same_kind) != 1 or len(candidates) != 1:
                        continue
                    title_refs = declared[candidates[0]["attachment"]["attachment_id"]]
                    if len(title_refs) != 1 or not (
                        title_refs[0]["article"] == chunk["article"] and
                        (title_refs[0]["paragraph"] is None or title_refs[0]["paragraph"] == chunk["paragraph"])
                    ):
                        continue
                else:
                    # '별표 1부터 ...' 같은 범위는 끝점만 확정하지 않습니다.
                    line = text[text.rfind("\n", 0, match.start()) + 1:].split("\n", 1)[0]
                    if re.search(r"별(?:표|도)\s*(?:제\s*)?[0-9]+\s*(?:부터|내지|~|∼)", line):
                        continue
                    candidates = [i for i in same_kind
                                  if int(i["attachment"]["attachment_no"]) == int(number)
                                  and int(i["attachment"]["attachment_branch_no"]) == int(branch or 0)]
                    # 0000은 '별표 0'이라는 번호가 아니라 원본의 무번호 표기입니다.
                    if int(number) == 0 or len(candidates) != 1:
                        continue
                ident = candidates[0]["attachment"]["attachment_id"]
                links[ident]["body_references"].append({
                    "chunk_id": chunk["chunk_id"], "article": chunk["article"],
                    "paragraph": chunk["paragraph"], "quote_text": text,
                    "reference_text": match.group(),
                    "reference_span": {"start": match.start(), "end": match.end()},
                    "method": "explicit_body_reference",
                })

    for item in items:
        ident = item["attachment"]["attachment_id"]
        result, title = links[ident], item["attachment"]["title"]
        refs = declared[ident]
        article_labels = {a["article"] for a in documents[item["ordin_seq"]]["articles"]}
        if len(refs) != 1:
            result["title_reference_status"] = "unparsed_title_reference" if "관련" in title else "no_title_reference"
            result["review_notes"].append("제목의 관련 조문을 확정하지 않았습니다. 원문 오타·복수 참조 등을 직접 확인하며 자동 교정하지 않습니다.")
        else:
            ref = refs[0]
            targets = [r["chunk_id"] for r in result["body_references"]
                       if r["article"] == ref["article"] and
                       (ref["paragraph"] is None or r["paragraph"] == ref["paragraph"])]
            result["title_references"] = [{**ref, "target_chunk_ids": sorted(set(targets)),
                                            "method": "explicit_attachment_title_reference"}]
            if ref["article"] not in article_labels:
                result["title_reference_status"] = "title_target_not_found"
            elif not targets:
                result["title_reference_status"] = "title_body_reference_not_confirmed"
            else:
                result["title_reference_status"] = "matched_body_reference"
            if not targets:
                result["review_notes"].append("제목에 적힌 조문과 이 첨부를 명시적으로 참조하는 본문을 양방향 확인하지 못했습니다.")
        if not result["body_references"]:
            result["review_notes"].append("이 조례 본문에서 확정 가능한 명시 참조를 찾지 못했습니다. 이름 유사도로 연결하지 않습니다.")
        result["review_notes"].append("원문 참조 위치만 연결했습니다. 첨부 내용·법적 적용 여부 검수와 질문별 근거 선택은 별도입니다.")
    return links


def load_batch_links(project, items):
    """6종 첨부와 검증된 본문을 연결하고 읽은 모든 자료의 해시를 반환합니다.

    body_references의 reference_span은 같은 레코드 quote_text의 문자 위치이며,
    title_references의 reference_span은 첨부 원본 title의 문자 위치입니다.
    기존 별표 2 데모 자료나 CLI 결과는 변경하지 않습니다.
    """
    import build_ordinance_corpus as body
    import download_attachment as originals

    project = Path(project).resolve(strict=True)
    input_hashes, catalog_items = originals.catalog(project)
    body.require(items == catalog_items, "배치 첨부 목록이 검증한 XML 목록과 다릅니다")
    expected, report = body.prepare(project)
    for relative, expected_hash in report["input_files"].items():
        body.require(relative not in input_hashes or input_hashes[relative] == expected_hash,
                     "첨부·본문 검증 사이에 입력이 변경됐습니다")
        input_hashes[relative] = expected_hash
    documents, chunks = {}, {}
    folder = Path("data/processed/ordinances") / report["as_of_date"]
    for name, raw in expected.items():
        relative = (folder / name).as_posix()
        actual = originals.safe_path(project, relative).read_bytes()
        body.require(actual == raw, f"저장된 본문이 XML 검증 결과와 다릅니다: {name}")
        input_hashes[relative] = digest_bytes(actual)
        if name.endswith(".articles.json"):
            documents[name.split(".", 1)[0]] = json.loads(actual)
        elif name.endswith(".chunks.json"):
            chunks[name.split(".", 1)[0]] = json.loads(actual)
    links = _batch_reference_links(items, documents, chunks)
    for relative, expected_hash in input_hashes.items():
        body.require(digest_bytes(originals.safe_path(project, relative).read_bytes()) == expected_hash,
                     "배치 연결 중 입력 파일이 변경됐습니다")
    return links, input_hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--gate-results-dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir or args.project_dir / "data" / "processed" / "attachments"
    try:
        corpus = load_corpus(args.project_dir)
        links = build_links(args.project_dir, corpus)
        output_path = output_dir / f"{STEM}.article_links.json"
        save_json(output_path, links)
        result_count = 0
        if args.gate_results_dir:
            for path in sorted(args.gate_results_dir.glob("*.result.json")):
                linked = link_gate_result(read_json(path), links, corpus)
                save_json(output_dir / "linked_gate_results" / path.name, linked)
                result_count += 1
        print("관련 본문:", ", ".join(f'{p["article"]} 제{p["paragraph"]}항' for p in links["related_provisions"]))
        print("별표 제목에 명시된 연결:", links["primary_provision_id"])
        print("연결한 근거 단위:", len(links["evidence_links"]))
        print("연결한 검색 조각:", len(links["search_chunk_links"]))
        print("기존 판정 결과 연결:", result_count)
        print("검수 상태:", links["review_status"])
        print("저장:", output_path)
    except (GateError, ValueError, KeyError, TypeError, OSError, ET.ParseError) as exc:
        code = str(exc) if isinstance(exc, GateError) else type(exc).__name__
        print("연결 실패:", code)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
