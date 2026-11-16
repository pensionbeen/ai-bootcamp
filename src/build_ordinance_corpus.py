"""검증된 6종 XML 본문을 원문 보존 조문·항 단위로 가공합니다. API 호출 없음.

python src/build_ordinance_corpus.py --check-only
python src/build_ordinance_corpus.py

기존 주차장 별표 데모 산출물은 유지합니다. 새 산출물은
data/processed/ordinances/<기준일>/ 아래에 저장합니다.
"""

from project_paths import PROJECT_ROOT
import argparse
import copy
import hashlib
import html
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

import import_starter_sources as source_import

PIPELINE_VERSION = "ordinance-body-v1"
PARAGRAPH_START = re.compile(r"^[ \t]*([①-⑳㉑-㉟㊱-㊿])", re.MULTILINE)
ARTICLE_START = re.compile(r"제([0-9]+)조(?:의([0-9]+))?")
DELETED = re.compile(r"삭제(?:\s*<[^>]*>)*")
SPAN_DESCRIPTION = "XML 조내용을 파싱한 Python 문자열의 문자 인덱스 [start,end); XML 바이트 위치가 아님"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def split_article(article):
    """조 제목·본문·말미 주석을 원문 문자 구간으로 나눕니다. 의미를 추정하지 않음."""
    text = article["source_text"]
    heading = article["article"] + (f"({article['title']})" if article["title"] else "")
    first = len(text) - len(text.lstrip())
    require(text.startswith(heading, first), f"{article['article']}: 제목 형식 불일치")
    body_start = first + len(heading)
    # 대괄호 행을 '개정 이력'이라고 단정하지 않습니다. 적용 조건일 수도 있어
    # 원문 구간으로 보존하고 모든 자식 조각에 함께 전달합니다.
    lines = text.splitlines(keepends=True)
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    note_start, notes = len(text), []
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if not stripped:
            continue
        if starts[index] >= body_start and stripped.startswith("[") and stripped.endswith("]"):
            notes.insert(0, stripped)
            note_start = starts[index]
        else:
            break
    body_end = note_start if notes else len(text)
    body = text[body_start:body_end]
    require(body.strip(), f"{article['article']}: 빈 본문")
    markers = list(PARAGRAPH_START.finditer(body))
    numbers = [int(unicodedata.numeric(m.group(1))) for m in markers]
    if markers:
        require(not body[:markers[0].start()].strip(), f"{article['article']}: 첫 항 앞의 본문을 확인하세요")
        require(numbers == list(range(1, len(numbers) + 1)), f"{article['article']}: 항 번호 순서를 확인하세요")
        bounds = [body_start] + [body_start + m.start() for m in markers[1:]] + [body_end]
    else:
        numbers, bounds = [None], [body_start, body_end]

    segments = [{"kind": "heading", "start": 0, "end": body_start, "text": text[:body_start]}]
    chunks = []
    for index, paragraph in enumerate(numbers):
        start, end = bounds[index:index + 2]
        quote = text[start:end]
        content = quote.strip()
        deletion_body = content[1:].strip() if paragraph is not None else content
        require(deletion_body, f"{article['article']}: 빈 항")
        chunks.append({"paragraph": paragraph, "source_span": {"start": start, "end": end},
                       "quote_text": quote, "text": content,
                       "is_deleted": DELETED.fullmatch(deletion_body) is not None})
        segments.append({"kind": "provision", "start": start, "end": end,
                         "text": quote, "paragraph": paragraph})
    if body_end < len(text):
        segments.append({"kind": "notes", "start": body_end, "end": len(text), "text": text[body_end:]})
    cursor = 0
    for segment in segments:
        require(segment["start"] == cursor and segment["end"] > cursor, "원문 구간의 누락/중복")
        require(segment["text"] == text[segment["start"]:segment["end"]], "원문 구간 불일치")
        cursor = segment["end"]
    require(cursor == len(text) and "".join(s["text"] for s in segments) == text, "원문 복원 실패")
    return {"segments": segments, "chunks": chunks, "article_notes": notes,
            "temporal_review_required": any(re.search(r"유효|시행|적용|경과", note) for note in notes)}


def parse_articles(raw, entry):
    root = ET.fromstring(raw)
    articles, headings, chapter_path = [], [], []
    seen_codes, seen_labels = set(), set()
    for ordinal, node in enumerate(root.findall("./조문/조"), start=1):
        flag = node.findtext("조문여부")
        source_text = node.findtext("조내용", "")
        require(len(node.findall("조내용")) == 1 and not list(node.find("조내용")), "조내용이 단일 텍스트가 아닙니다")
        if flag == "N":
            heading = source_text.strip()
            require(heading, "빈 장·절 제목")
            level = re.match(r"제\d+([편장절관])", heading)
            require(level is not None, "알 수 없는 비조문 항목")
            ranks = {"편": 0, "장": 1, "절": 2, "관": 3}
            rank = ranks[level.group(1)]
            chapter_path = [x for x in chapter_path if x[0] < rank] + [(rank, heading)]
            headings.append({"xml_node_ordinal": ordinal, "source_text": source_text})
            continue
        require(flag == "Y", "알 수 없는 조문여부 값")
        match = ARTICLE_START.match(source_text.lstrip())
        require(match is not None, "조 번호를 읽지 못했습니다")
        label = match.group(0)
        code = node.findtext("조문번호")
        expected_code = f"{int(match.group(1)):04d}{int(match.group(2) or 0):02d}"
        require(code == expected_code, f"{label}: 조 번호와 XML 코드가 다릅니다")
        require(code not in seen_codes and label not in seen_labels, "중복 조문 코드/번호")
        seen_codes.add(code)
        seen_labels.add(label)
        article = {"article_id": f"{entry['ordin_seq']}:{code}", "article_code": code,
                   "article": label, "title": node.findtext("조제목", ""),
                   "chapter_path": [x[1] for x in chapter_path], "source_text": source_text,
                   "source_text_hash": digest(source_text.encode("utf-8")),
                   "source_location": {"raw_file": entry["raw_file"], "xml_node_ordinal": ordinal,
                                       "xml_path": f"./조문/조[{ordinal}]/조내용"}}
        article.update(split_article(article))
        articles.append(article)
    require(len(articles) == entry["article_count_including_deleted"], "manifest의 조문 수와 다릅니다")
    return articles, headings


def load_inputs(project):
    project = project.resolve(strict=True)
    inputs = {}

    def read(relative):
        path = project / relative
        require(path.resolve().is_relative_to(project) and not path.is_symlink(), "입력 경로가 프로젝트 밖입니다")
        raw = path.read_bytes()
        inputs[relative] = digest(raw)
        return raw

    manifest = json.loads(read("data/raw/ordinances.manifest.json"))
    require(manifest["as_of_date"] == source_import.AS_OF_DATE, "기준일 불일치")
    require(manifest["as_of_version_status"] == "verified_official_catalog", "공식 판본 확인이 필요합니다")
    require(manifest["source_review"] == "pending" and manifest["production_ready"] is False, "예상 밖 검수 상태")
    require(manifest["ordinance_count"] == len(manifest["ordinances"]) == len(source_import.SCOPE), "대상 조례 수 불일치")
    scope = {r[2]: r for r in source_import.SCOPE}
    require({r["ordin_seq"] for r in manifest["ordinances"]} == set(scope), "대상 판본 집합 불일치")
    # 수집 시점 metadata의 pending 값이 아닌, 현재 manifest의 검증 상태를 사용.
    checked = copy.deepcopy(manifest)
    source_import.preserve_version_review(manifest, checked, project)
    require(checked == manifest, "판본 확인 보고서와 manifest의 상태 불일치")
    report_path = manifest["as_of_verification"]["report_file"]
    read(report_path)
    read(str(Path(report_path).parent.joinpath("version_evidence.json").as_posix()))
    documents = []
    for entry in manifest["ordinances"]:
        raw = read(entry["raw_file"])
        meta_raw = read(entry["metadata_file"])
        metadata = json.loads(meta_raw)
        core, details = source_import.inspect_xml(raw, scope[entry["ordin_seq"]])
        require(all(entry.get(k) == v for k, v in {**core, **details}.items()), "manifest의 원본 식별정보 불일치")
        require(all(metadata.get(k) == v for k, v in core.items()), "metadata 출처 불일치")
        require(digest(meta_raw) == entry["metadata_content_hash"], "metadata 해시 불일치")
        documents.append((entry, raw))
    return manifest, documents, inputs


def process_document(entry, raw):
    articles, headings = parse_articles(raw, entry)
    source = {key: entry[key] for key in ("ordin_id", "ordin_seq", "ordin_name", "as_of_date",
              "effective_date", "promulgation_date", "source_url", "content_hash", "metadata_content_hash")}
    source["as_of_version_status"] = "verified_official_catalog"
    source["source_review"] = "pending"
    chunks = []
    for article in articles:
        child_ids = []
        for piece in article.pop("chunks"):
            paragraph = piece["paragraph"]
            label = f"{entry['ordin_name']} {article['article']}" + (f" 제{paragraph}항" if paragraph is not None else "")
            chunk_id = f"{article['article_id']}:{paragraph if paragraph is not None else 'all'}"
            notes = article["article_notes"]
            embedding_text = "\n".join([label, article["title"], *article["chapter_path"], piece["text"]])
            if notes:
                embedding_text += "\n[함께 확인할 조문 말미 주석]\n" + "\n".join(notes)
            chunks.append({**source, **piece, "chunk_id": chunk_id, "parent_id": article["article_id"],
                           "article": article["article"], "article_code": article["article_code"],
                           "title": article["title"], "label": label, "article_notes": notes,
                           "embedding_text": embedding_text, "embedding_status": "not_created",
                           "required_context_ids": [article["article_id"]],
                           "temporal_review_required": article["temporal_review_required"],
                           "retrieval_use": "deletion_status_only" if piece["is_deleted"] else "candidate_requires_parent_context",
                           "answer_allowed": False})
            child_ids.append(chunk_id)
        article["chunk_ids"] = child_ids
    ids = [c["chunk_id"] for c in chunks]
    require(len(ids) == len(set(ids)), "중복 검색 조각 ID")
    by_id = {a["article_id"]: a for a in articles}
    for chunk in chunks:
        parent = by_id[chunk["parent_id"]]
        span = chunk["source_span"]
        require(parent["source_text"][span["start"]:span["end"]] == chunk["quote_text"], "인용문 원문 대조 실패")
        require(all(note in chunk["embedding_text"] for note in parent["article_notes"]), "검색 텍스트의 주석 누락")
    summary = {"ordin_name": entry["ordin_name"], "ordin_seq": entry["ordin_seq"],
               "article_count": len(articles), "chunk_count": len(chunks),
               "numbered_paragraph_chunks": sum(c["paragraph"] is not None for c in chunks),
               "unnumbered_article_chunks": sum(c["paragraph"] is None for c in chunks),
               "deleted_articles": sum(c["is_deleted"] and c["paragraph"] is None for c in chunks),
               "deleted_paragraphs": sum(c["is_deleted"] and c["paragraph"] is not None for c in chunks),
               "temporal_review_articles": [a["article"] for a in articles if a["temporal_review_required"]],
               "heading_count": len(headings), "reconstructed_articles": len(articles),
               "longest_chunk_characters": max(len(c["text"]) for c in chunks),
               "supplement_blocks_not_chunked": entry["supplement_block_count"],
               "attachments_not_processed_here": entry["attachment_count"]}
    document = {"schema_version": 2, "pipeline_version": PIPELINE_VERSION, "source": source,
                "span_semantics": SPAN_DESCRIPTION, "headings": headings, "articles": articles}
    return document, chunks, summary


def preview_html(report, documents, chunks):
    esc = lambda value: html.escape(str(value))
    rows = "".join(f"<tr><td>{esc(r['ordin_name'])}</td><td>{r['article_count']}</td><td>{r['chunk_count']}</td><td>{r['deleted_articles']}/{r['deleted_paragraphs']}</td></tr>" for r in report["ordinances"])
    grouped = {}
    for chunk in chunks:
        grouped.setdefault(chunk["parent_id"], []).append(chunk)
    items = []
    for document in documents:
        source = document["source"]
        article_html = []
        for article in document["articles"]:
            children = grouped[article["article_id"]]
            cards = []
            for c in children:
                kind = "삭제 표시: 삭제 여부 확인용" if c["is_deleted"] else "본문 후보: 부모 조문과 함께 확인"
                span = c["source_span"]
                cards.append(f"<section class='chunk'><b>{esc(c['label'])}</b><p class='meta'>{kind} · 문자 구간 [{span['start']}, {span['end']})</p><pre>{esc(c['quote_text'])}</pre></section>")
            note_html = ""
            if article["article_notes"]:
                note_html = "<aside><b>모든 조각에 함께 보존한 말미 주석</b><pre>" + esc("\n".join(article["article_notes"])) + "</pre></aside>"
            if article["temporal_review_required"]:
                note_html += "<p class='warning'>시행·유효기간 관련 표현이 있습니다. 적용 여부는 아직 판단하지 않았습니다.</p>"
            article_html.append(f"<details><summary>{esc(article['article'])} {esc(article['title'])} → {len(children)}개 검색 단위</summary><p class='meta'>{esc(' / '.join(article['chapter_path']))}</p><details class='original'><summary>분리 전 원문</summary><pre>{esc(article['source_text'])}</pre></details>{''.join(cards)}{note_html}</details>")
        items.append(f"<h2>{esc(source['ordin_name'])}</h2><p>판본 시행일 {esc(source['effective_date'])} · <a href='{esc(source['source_url'])}'>공식 출처</a></p>{''.join(article_html)}")
    return ("<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>서울시 조례 본문 분리 확인</title><style>body{max-width:1050px;margin:40px auto;padding:0 24px;background:#f6f8fb;color:#172234;font:16px/1.7 system-ui,sans-serif}h1{line-height:1.3}h2{margin-top:48px}table{width:100%;border-collapse:collapse;background:white}td,th{padding:12px;text-align:left;border-bottom:1px solid #dce2eb}details{background:white;border:1px solid #dce2eb;border-radius:8px;margin:12px 0;padding:14px}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.meta{font-size:14px;color:#586476}.chunk{border-left:4px solid #3574b5;padding:8px 18px;margin:18px 0;background:#f4f8ff}aside{background:#fff8e6;padding:14px}.warning{color:#8b4500}.intro{padding:20px;background:#e9f1ff;border-radius:10px}.original{background:#fafafa}</style><h1>6종 조례 · 본문 분리 확인</h1><p>기준일 2026-10-01 · 원문 구조를 보존한 검색 후보 자료</p><div class='intro'><b>" + str(report["article_count"]) + "개 조문을 " + str(report["chunk_count"]) + "개 검색 단위로 나눴습니다.</b><p>항 번호가 있으면 항별로, 없으면 조문 본문 전체를 한 단위로 유지합니다. 호·목은 상위 항에 포함합니다. 분리 수는 각 조례의 실제 구조에 따라 결정됩니다.</p><p>아래 조문을 펼치면 분리 전 원문과 각 조각을 비교할 수 있습니다. 모든 조문의 제목·본문·주석을 공백까지 복원하여 원문과 대조했습니다.</p><p>임베딩·검색·답변 연결 전 자료입니다. 부칙과 별표의 추가 처리는 이번 범위에 포함하지 않았습니다. 개별 조항의 법적 적용 여부는 검수 대기입니다.</p></div><h2>조례별 결과</h2><table><thead><tr><th>조례</th><th>조문</th><th>검색 단위</th><th>삭제 조/항</th></tr></thead><tbody>" + rows + "</tbody></table>" + "".join(items) + "</html>").encode("utf-8")


def prepare(project):
    manifest, inputs, input_hashes = load_inputs(project)
    files, documents, all_chunks, summaries = {}, [], [], []
    for entry, raw in inputs:
        document, chunks, summary = process_document(entry, raw)
        documents.append(document)
        all_chunks.extend(chunks)
        summaries.append(summary)
        files[f"{entry['ordin_seq']}.articles.json"] = json_bytes(document)
        files[f"{entry['ordin_seq']}.chunks.json"] = json_bytes(chunks)
    require(len({c['chunk_id'] for c in all_chunks}) == len(all_chunks), "전체 검색 조각 ID 중복")
    require(sum(s["article_count"] for s in summaries) == manifest["article_count_including_deleted"], "전체 조문 수 불일치")
    report = {"schema_version": 1, "pipeline_version": PIPELINE_VERSION, "as_of_date": manifest["as_of_date"],
              "validation_status": "exact_source_reconstruction_passed", "source_review": "pending",
              "production_ready": False, "embedding_status": "not_created", "answer_allowed": False,
              "input_files": input_hashes, "span_semantics": SPAN_DESCRIPTION,
              "article_count": sum(s["article_count"] for s in summaries), "chunk_count": len(all_chunks),
              "chunking_rules": ["항 번호가 있으면 항별 분리", "항 번호가 없으면 조문 본문 전체 유지",
                                 "호·목은 상위 항에 포함", "조 제목·장 제목·말미 주석·부모 조문 연결 보존",
                                 "삭제 조·항은 삭제 여부 확인용으로 표시", "글자 수에 따른 강제 분할 없음"],
              "limitations": ["형식·원문 보존 검사이며 법적 적용 판단이나 답변 정확도 검수가 아님",
                              "부칙·첨부자료 전체 가공 및 조문 사이 참조 해석은 별도 단계",
                              "항 단위가 독립적으로 충분한 근거라는 뜻은 아님: 부모 조문을 함께 확인",
                              "embedding_text는 임베딩 입력용 문자열이며 벡터는 아직 만들지 않음"],
              "ordinances": summaries}
    files["chunking_report.json"] = json_bytes(report)
    files["chunking_preview.html"] = preview_html(report, documents, all_chunks)
    index = {"schema_version": 1, "pipeline_version": PIPELINE_VERSION, "as_of_date": manifest["as_of_date"],
             "input_files": input_hashes, "article_count": report["article_count"], "chunk_count": len(all_chunks),
             "production_ready": False, "source_review": "pending", "embedding_status": "not_created",
             "output_files": {name: digest(raw) for name, raw in files.items()}}
    files["corpus.manifest.json"] = json_bytes(index)
    return files, report


def run(project, output=None, check_only=False):
    project = project.resolve(strict=True)
    files, report = prepare(project)
    output = (output or project / "data/processed/ordinances" / report["as_of_date"]).resolve()
    # 모든 충돌을 먼저 확인합니다. 동일 입력으로 재실행하면 파일을 쓰지 않습니다.
    reused = 0
    for name, raw in files.items():
        target = output / name
        if target.exists():
            require(target.is_file() and not target.is_symlink() and target.read_bytes() == raw,
                    f"내용이 다른 기존 파일을 덮어쓰지 않습니다: {name}")
            reused += 1
    for relative, expected in report["input_files"].items():
        require(digest((project / relative).read_bytes()) == expected, "저장 직전 입력이 변경됐습니다")
    if not check_only:
        for name, raw in files.items():
            target = output / name
            if name == "corpus.manifest.json":
                for earlier, expected_raw in files.items():
                    if earlier != name:
                        require((output / earlier).read_bytes() == expected_raw, "완료 목록 저장 전 산출물 변경")
                for relative, expected in report["input_files"].items():
                    require(digest((project / relative).read_bytes()) == expected, "완료 목록 저장 전 입력 변경")
            if not target.exists():
                source_import.write_new(target, raw)
        for name, raw in files.items():
            require((output / name).read_bytes() == raw, "저장 후 대조 실패")
    for summary in report["ordinances"]:
        print(f"{summary['ordin_name']}: {summary['article_count']}조 → {summary['chunk_count']}조각 / 삭제 조 {summary['deleted_articles']}·항 {summary['deleted_paragraphs']}")
    print(f"원문 정확 복원: {report['article_count']}/{report['article_count']} / 검색 후보: {report['chunk_count']}")
    print(f"모드: {'검사만' if check_only else '저장'} / 동일 파일 재사용 {reused}/{len(files)}")
    print("임베딩 생성 안 함 / 원문 내용·법적 적용 검수 pending")
    print("결과 경로:", output)
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
        raise SystemExit(f"본문 가공 중단: {exc}") from None
