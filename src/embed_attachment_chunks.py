
from project_paths import PROJECT_ROOT
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv, dotenv_values


STEM = "2171833_22740725"
MODEL = "solar-embedding-2-passage"
QUERY_MODEL = "solar-embedding-2-query"
DIMENSION = 1024
API_URL = "https://api.upstage.ai/v1/embeddings"


def pair_vectors(chunks, response):
    items = response["data"]
    if any(type(item["index"]) is not int for item in items):
        raise ValueError("응답 인덱스가 정수가 아닙니다.")

    # 응답의 index를 기준으로 입력 순서를 맞춥니다.
    items = sorted(items, key=lambda item: item["index"])
    if [item["index"] for item in items] != list(range(len(chunks))):
        raise ValueError("입력 조각과 응답 인덱스가 맞지 않습니다.")

    vectors = []
    for chunk, item in zip(chunks, items):
        vector = item["embedding"]
        if not isinstance(vector, list) or len(vector) != DIMENSION:
            raise ValueError("벡터 차원이 예상과 다릅니다.")
        if not all(
            type(value) in (int, float) and math.isfinite(value)
            for value in vector
        ):
            raise ValueError("벡터에 올바르지 않은 숫자가 있습니다.")

        vectors.append({
            "chunk_id": chunk["chunk_id"],
            "embedding": vector,
        })

    return vectors


def main():
    base_dir = PROJECT_ROOT
    folder = base_dir / "data" / "processed" / "attachments"
    input_path = folder / f"{STEM}.search_chunks.json"
    output_path = folder / f"{STEM}.embeddings.json"

    raw = input_path.read_bytes()
    input_hash = hashlib.sha256(raw).hexdigest()
    document = json.loads(raw.decode("utf-8-sig"))
    chunks = document["chunks"]
    texts = [chunk["text"] for chunk in chunks]

    if not 1 <= len(texts) <= 100:
        raise ValueError("이번 코드는 1~100개 조각을 한 번에 처리합니다.")
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("비어 있거나 문자열이 아닌 검색 조각이 있습니다.")
    if len({chunk["chunk_id"] for chunk in chunks}) != len(chunks):
        raise ValueError("중복된 chunk_id가 있습니다.")

    if output_path.exists():
        record = json.loads(output_path.read_text(encoding="utf-8"))
        if (
            record["input_content_hash"] != input_hash
            or record["model"] != MODEL
            or record["dimension"] != DIMENSION
        ):
            raise ValueError("저장된 벡터의 입력 또는 모델 설정이 다릅니다.")
        print("저장된 임베딩을 재사용합니다.")

    else:
        load_dotenv(base_dir / ".env")
        api_key = os.getenv("UPSTAGE_API_KEY")
        if not api_key:
            raise ValueError(".env에 UPSTAGE_API_KEY를 설정하세요.")

        print("임베딩할 검색 조각:", len(texts))
        response = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": MODEL, "input": texts},
            timeout=(10, 180),
        )
        print("HTTP 상태:", response.status_code)
        response.raise_for_status()
        result = response.json()

        record = {
            "schema_version": 1,
            "attachment_id": document["attachment_id"],
            "input_file": input_path.name,
            "input_content_hash": input_hash,
            "model": MODEL,
            "response_model": result.get("model"),
            "query_model": QUERY_MODEL,
            "dimension": DIMENSION,
            "embedded_at": datetime.now(timezone.utc).isoformat(),
            "vectors": pair_vectors(chunks, result),
            "usage": result.get("usage", {}),
        }

        temporary = output_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(output_path)

    print("벡터 개수:", len(record["vectors"]))
    print("벡터 차원:", record["dimension"])
    print("저장:", output_path)


READINESS_FOLDER = "data/processed/attachments/2026-10-01/embedding_ready"
REVIEW_FILE = READINESS_FOLDER + "/review_decisions.json"
REVIEW_SHA256 = "decb511ab42e7dbe2fa7b0f7ade81a2a3e90368de342826371a21f4a0c64bb12"


def reviewed_primary(chunk, decision):
    """원본을 고치지 않고 검토 기록에 고정된 부분만 입력 표현에 반영합니다."""
    import build_ordinance_corpus as body
    text = chunk["text"]
    for patch in decision["primary_text_patches"]:
        body.require(set(patch) == {"old", "new"} and patch["old"] and patch["new"]
                     and text.count(patch["old"]) == 1, "교정 대상 문자열이 유일하지 않습니다")
        text = text.replace(patch["old"], patch["new"], 1)
    return text


def build_embedding_record(document, chunk, decision, decisions):
    import build_attachment_search_chunks as chunks
    import build_ordinance_corpus as body
    # 원래 문맥·본문 계약을 먼저 확인합니다. 본문 인용은 별도 메타데이터로 보관합니다.
    chunks.assemble_chunk(document, chunk)
    body.require(chunk["input_binding_status"] in ("api_uploaded_bytes", "legacy_api_input_hash")
                 and chunk["kind"] != "image_metadata_only", "입력 출처 또는 그림 검토가 해결되지 않았습니다")
    registry = {c["context_id"]: c for c in document["contexts"]}
    contexts = [registry[k] for k in chunk["required_context_ids"]]
    primary = reviewed_primary(chunk, decision)
    body_registry = {r["chunk_id"]: r for r in document["related_body"]["body_references"]}
    body.require(len(set(decision["required_body_chunk_ids"])) == len(decision["required_body_chunk_ids"])
                 and set(decision["required_body_chunk_ids"]) <= set(body_registry), "필수 본문 조건 연결 오류")
    body_contexts = [body_registry[k] for k in decision["required_body_chunk_ids"]]
    extra = []
    indexed = {c["chunk_id"]: (i, c) for i, c in enumerate(document["chunks"])}
    current = indexed[chunk["chunk_id"]][0]
    seen = set()
    for ident in decision["additional_context_chunk_ids"]:
        body.require(ident in indexed and ident not in seen and indexed[ident][0] < current
                     and decisions[ident]["status"] == "ready", "추가 문맥이 같은 표의 검토된 선행 항목이 아닙니다")
        seen.add(ident)
        prior = indexed[ident][1]
        table_keys = ("attachment_id", "parse_file", "parse_content_hash", "page", "element_id", "coordinates", "representation")
        table_refs = lambda c: [{k: r.get(k) for k in table_keys} for r in c["source_refs"]]
        body.require(table_refs(prior) == table_refs(chunk), "추가 문맥의 원문 표가 다릅니다")
        fields = {f["name"]: f["value"] for f in prior["fields"] if "name" in f}
        if "근거법령 원문 표기" in fields:
            context_text = ("[선행 항목의 법령 표기: 현재 사무의 적용법을 확정하지 않음]\n"
                            + fields["항목"] + ": " + fields["근거법령 원문 표기"])
        else:
            referenced = []
            for field in chunk["fields"]:
                ref = field.get("ditto_reference")
                if ref:
                    matches = [f for f in prior["fields"] if f.get("cell_origin", {}).get("row") == ref["row"]
                               and f.get("cell_origin", {}).get("column") == ref["column"] and f["value"] == ref["value"]]
                    body.require(len(matches) == 1, "동일 표시의 선행 셀을 찾을 수 없습니다")
                    referenced.append(" / ".join(" > ".join(p) for p in field["column_paths"]) + ": " + matches[0]["value"])
            body.require(referenced, "추가 문맥의 참조 대상을 확인할 수 없습니다")
            context_text = "[〃의 선행 원문 셀: 이 단위만 참조하며 선행 행의 요율은 가져오지 않음]\n" + "\n".join(referenced)
        extra.append({"source_chunk_id": ident, "text": context_text, "source_refs": prior["source_refs"]})
    parts = [document["ordin_name"], document["title"], primary]
    parts.extend("[필수 첨부 문맥]\n" + c["text"] for c in contexts)
    parts.extend(c["text"] for c in extra)
    parts.extend("[함께 확인할 명시적 본문 조건]\n" + c["quote_text"] for c in body_contexts)
    text = "\n\n".join(parts)
    body.require(text.strip(), "빈 임베딩 입력")
    return {"chunk_id": chunk["chunk_id"], "attachment_id": chunk["attachment_id"],
            "text": text, "text_sha256": body.digest(text.encode("utf-8")), "primary_text": primary,
            "required_contexts": contexts, "additional_contexts": extra,
            "required_body_contexts": body_contexts,
            "related_body": document["related_body"], "related_body_embedding_policy": "explicit_reviewed_conditions_only_other_links_metadata",
            "source": document["source"], "source_refs": chunk["source_refs"],
            "input_binding_status": chunk["input_binding_status"], "correction_id": chunk["correction_id"],
            "review_reason": decision["reason"], "review_basis": decision["review_basis"],
            "embedding_allowed": True, "answer_allowed": False, "legal_applicability": "not_determined"}


def readiness_preview(documents, decisions, records, report):
    import html
    esc = html.escape
    records = {r["chunk_id"]: r for r in records}
    panels = []
    rows = []
    for document in documents:
        approved = sum(c["chunk_id"] in records for c in document["chunks"])
        rows.append(f'<tr><td>{esc(document["ordin_name"])}</td><td>{esc(document["title"])}</td><td>{approved}</td><td>{len(document["chunks"])-approved}</td></tr>')
        items = []
        for chunk in document["chunks"]:
            decision = decisions[chunk["chunk_id"]]
            record = records.get(chunk["chunk_id"])
            status = "임베딩 입력 준비" if record else "보류"
            content = f'<pre>{esc(record["text"])}</pre>' if record else f'<pre>{esc(chunk["text"])}</pre>'
            if record:
                refs = "\n\n".join(r["quote_text"] for r in record["related_body"]["body_references"])
                content += '<details><summary>별도 보관한 본문 참조 · 적용 여부 미판정</summary><pre>' + esc(refs) + '</pre></details>'
            locations = ", ".join(f'p.{r["page"]} 요소 {r["element_id"]}' for r in chunk["source_refs"])
            items.append(f'<details><summary><span class="{decision["status"]}">{status}</span> {esc(chunk["label"][:110])}</summary><p>{esc(decision["reason"])}</p>{content}<small>{esc(chunk["chunk_id"])} · {esc(locations)}</small></details>')
        panels.append('<section><h2>' + esc(document["ordin_name"] + " · " + document["title"]) + '</h2>' + ''.join(items) + '</section>')
    return ('''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>첨부 임베딩 입력 검토</title><style>body{font-family:system-ui,sans-serif;max-width:1180px;margin:auto;padding:26px;background:#f4f7fb;color:#192a3b;line-height:1.7}section{background:white;border:1px solid #d5dfe9;border-radius:12px;margin:24px 0;padding:22px}table{width:100%;border-collapse:collapse}th,td{border:1px solid #c3cfda;padding:8px;text-align:left}summary{cursor:pointer;font-weight:600}details{border-top:1px solid #d5dfe9;padding:12px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit;background:#eef3f8;padding:14px}.ready{color:#096848}.held{color:#97510a}small{overflow-wrap:anywhere}</style><h1>무엇을 임베딩할 준비가 됐는가</h1>'''
            + f'<p>후보 {report["candidate_count"]}개 → 입력 준비 <strong>{report["ready_count"]}개</strong> / 보류 <strong>{report["held_count"]}개</strong></p>'
            + '<p>검토 범위는 원문에서 검색용 표현으로 옮기는 정확성입니다. 새 API 호출·벡터 생성·DB 저장은 하지 않았으며, 답변 허용과 법적 적용은 여전히 미판정입니다.</p>'
            + '<p>단위·비고·조건은 입력과 함께 보존합니다. 관련 본문은 적용 범위가 다를 수 있어 별도로 보관하며 자동 적용하지 않습니다. 과거 원본·Parse·검색 후보는 변경하지 않았습니다.</p>'
            + '<p><a href="../chunks/preview.html">이전 검색 후보</a> · <a href="../review.html">원문 추출·이미지</a></p>'
            + '<table><tr><th>조례</th><th>첨부</th><th>준비</th><th>보류</th></tr>' + ''.join(rows) + '</table>' + ''.join(panels) + '</html>').encode('utf-8')


def prepare_readiness(project):
    import build_attachment_search_chunks as chunks
    import build_ordinance_corpus as body
    import download_attachment as originals
    project = Path(project).resolve(strict=True)
    chunks.run_batch(project, check_only=True)
    manifest_path = chunks.FOLDER + "/chunks.manifest.json"
    manifest_raw = originals.safe_path(project, manifest_path).read_bytes()
    manifest = json.loads(manifest_raw)
    inputs = {**manifest["input_files"], manifest_path: body.digest(manifest_raw)}
    for name, digest in manifest["output_files"].items():
        inputs[chunks.FOLDER + "/" + name] = digest
    candidate_path = chunks.FOLDER + "/candidate_chunks.json"
    candidate_raw = originals.safe_path(project, candidate_path).read_bytes()
    data = json.loads(candidate_raw)
    raw = originals.safe_path(project, REVIEW_FILE).read_bytes()
    body.require(body.digest(raw) == REVIEW_SHA256, "검토한 임베딩 판정 기록이 변경됐습니다")
    review = json.loads(raw)
    body.require(review["schema_version"] == "attachment-embedding-review-v1"
                 and review["candidate_content_hash"] == body.digest(candidate_raw)
                 and review["answer_allowed"] is False and review["legal_applicability"] == "not_determined",
                 "임베딩 판정의 입력·검토 범위 불일치")
    inputs[REVIEW_FILE] = body.digest(raw)
    pairs = [(d, c) for d in data["documents"] for c in d["chunks"]]
    decisions = {d["chunk_id"]: d for d in review["decisions"]}
    body.require(len(decisions) == len(review["decisions"]) == len(pairs)
                 and set(decisions) == {c["chunk_id"] for _, c in pairs}, "판정 누락·중복·알 수 없는 조각")
    records, held, parse_cache = [], [], {}
    for document, chunk in pairs:
        decision = decisions[chunk["chunk_id"]]
        body.require(decision["status"] in ("ready", "held") and decision["reason"].strip()
                     and decision["candidate_chunk_hash"] == body.digest(body.json_bytes(chunk)), "조각 판정 불일치")
        body.require(isinstance(decision["primary_text_patches"], list)
                     and isinstance(decision["additional_context_chunk_ids"], list), "판정 변환 형식 오류")
        if decision["status"] == "ready":
            body.require(decision["review_basis"], "원문 대조 근거가 없습니다")
            import base64
            body.require(len(decision["review_basis"]) == len(chunk["source_refs"]), "검토 근거 위치 누락")
            for basis, ref in zip(decision["review_basis"], chunk["source_refs"]):
                body.require(all(basis[k] == ref[k] for k in ("parse_file", "parse_content_hash", "page", "element_id"))
                             and basis["xml_content_hash"] == document["source"]["content_hash"], "검토 근거의 원문 위치 불일치")
                if basis["parse_file"] not in parse_cache:
                    parse_cache[basis["parse_file"]] = json.loads(originals.safe_path(project, basis["parse_file"]).read_bytes())
                parsed = parse_cache[basis["parse_file"]]
                parsed = parsed.get("response", parsed)
                element = next(e for e in parsed["elements"] if (e["page"], e["id"]) == (basis["page"], basis["element_id"]))
                body.require(body.digest(base64.b64decode(element["base64_encoding"], validate=True)) == basis["crop_content_hash"],
                             "검토 이미지의 바이트 불일치")
            records.append(build_embedding_record(document, chunk, decision, decisions))
        else:
            body.require(not decision["primary_text_patches"] and not decision["additional_context_chunk_ids"], "보류 항목에 입력 변환이 있습니다")
            held.append({"chunk_id": chunk["chunk_id"], "reason_code": decision["reason_code"], "reason": decision["reason"]})
    report = {"candidate_count": len(pairs), "ready_count": len(records), "held_count": len(held),
              "held": held, "model": MODEL, "query_model": QUERY_MODEL, "dimension": DIMENSION,
              "review_scope": "source_representation_for_embedding_only", "answer_allowed": False,
              "embedding_performed": False, "legal_applicability": "not_determined",
              "primary_text_corrected_count": sum(bool(d["primary_text_patches"]) for d in decisions.values()),
              "additional_context_count": sum(bool(r["additional_contexts"]) for r in records),
              "max_input_characters": max((len(r["text"]) for r in records), default=0)}
    payload = {"schema_version": "attachment-embedding-inputs-v1", "model": MODEL, "query_model": QUERY_MODEL,
               "dimension": DIMENSION, "review_content_hash": body.digest(raw), "answer_allowed": False,
               "embedding_performed": False, "records": records}
    outputs = {"embedding_inputs.json": body.json_bytes(payload), "readiness_report.json": body.json_bytes(report),
               "preview.html": readiness_preview(data["documents"], decisions, records, report)}
    outputs["readiness.manifest.json"] = body.json_bytes({"pipeline_version": "attachment-embedding-readiness-v1",
                "input_files": inputs, "output_files": {name: body.digest(content) for name, content in outputs.items()},
                "report": report})
    verify_readiness_inputs(project, outputs)
    return outputs, report


def verify_readiness_inputs(project, outputs):
    import build_ordinance_corpus as body
    import download_attachment as originals
    for relative, digest in json.loads(outputs["readiness.manifest.json"])["input_files"].items():
        body.require(body.digest(originals.safe_path(project, relative).read_bytes()) == digest, "임베딩 준비 중 입력 변경")


def check_readiness_outputs(project, outputs):
    import build_ordinance_corpus as body
    import download_attachment as originals
    for name, raw in outputs.items():
        body.require(originals.safe_path(project, READINESS_FOLDER + "/" + name).read_bytes() == raw,
                     "저장된 임베딩 준비 결과 불일치: " + name)
    verify_readiness_inputs(project, outputs)


def run_readiness(project, check_only=False):
    import build_ordinance_corpus as body
    import download_attachment as originals
    from request_lock import request_lock
    if check_only:
        outputs, report = prepare_readiness(project)
        check_readiness_outputs(project, outputs)
        return report
    with request_lock(originals.safe_path(project, READINESS_FOLDER + "/.readiness.lock")):
        outputs, report = prepare_readiness(project)
        for name, raw in outputs.items():
            path = originals.safe_path(project, READINESS_FOLDER + "/" + name)
            body.require(not path.exists() or path.read_bytes() == raw, "기존 임베딩 준비 결과 충돌: " + name)
        verify_readiness_inputs(project, outputs)
        for name, raw in outputs.items():
            path = originals.safe_path(project, READINESS_FOLDER + "/" + name)
            if not path.exists():
                body.source_import.write_new(path, raw)
        return run_readiness(project, check_only=True)


def load_ready_inputs(project):
    outputs, _ = prepare_readiness(project)
    check_readiness_outputs(project, outputs)
    # 검증 뒤 디스크에서 다시 읽지 않고 검증한 스냅샷을 사용합니다.
    return json.loads(outputs["embedding_inputs.json"])["records"]


BATCH_FOLDER = "data/processed/attachments/2026-10-01/embeddings"
BATCH_SIZE = 100
# 토큰 계산기가 아닌 보수적 로컬 크기 제한입니다. 절대로 자동 절단하지 않습니다.
MAX_INPUT_BYTES = 4000
MAX_BATCH_TEXT_BYTES = 200000


def prepare_embedding_plan(project):
    import build_ordinance_corpus as body
    outputs, _ = prepare_readiness(project)
    check_readiness_outputs(project, outputs)
    input_raw = outputs["embedding_inputs.json"]
    data = json.loads(input_raw)
    body.require((data["model"], data["query_model"], data["dimension"]) == (MODEL, QUERY_MODEL, DIMENSION),
                 "검토한 임베딩 모델 설정 불일치")
    records = data["records"]
    body.require(records and len({r["chunk_id"] for r in records}) == len(records), "빈 입력 또는 조각 ID 중복")
    for record in records:
        body.require(record["embedding_allowed"] is True and record["answer_allowed"] is False
                     and record["text"].strip() and body.digest(record["text"].encode("utf-8")) == record["text_sha256"]
                     and len(record["text"].encode("utf-8")) <= MAX_INPUT_BYTES, "미검토·변경·길이 초과 임베딩 입력")
    inputs = dict(json.loads(outputs["readiness.manifest.json"])["input_files"])
    inputs.update({READINESS_FOLDER + "/" + name: body.digest(raw) for name, raw in outputs.items()})
    batches = []
    current, current_bytes = [], 0
    groups = []
    for record in records:
        length = len(record["text"].encode("utf-8"))
        if current and (len(current) >= BATCH_SIZE or current_bytes + length > MAX_BATCH_TEXT_BYTES):
            groups.append(current)
            current, current_bytes = [], 0
        current.append(record)
        current_bytes += length
    if current:
        groups.append(current)
    for number, group in enumerate(groups, 1):
        payload = {"model": MODEL, "input": [r["text"] for r in group]}
        identity = {"schema_version": "reviewed-embedding-request-v1", "batch_id": f"batch-{number:04d}",
                    "endpoint": API_URL, "model": MODEL, "query_model": QUERY_MODEL, "dimension": DIMENSION,
                    "ready_input_content_hash": body.digest(input_raw),
                    "readiness_manifest_content_hash": body.digest(outputs["readiness.manifest.json"]),
                    "request_body_sha256": body.digest(body.json_bytes(payload)),
                    "input_items": [{"chunk_id": r["chunk_id"], "text_sha256": r["text_sha256"]} for r in group]}
        batches.append({"identity": identity, "payload": payload, "records": group})
    return {"input_files": inputs, "records": records, "batches": batches,
            "ready_input_content_hash": body.digest(input_raw),
            "held_count": json.loads(outputs["readiness_report.json"])["held_count"]}


def verify_embedding_inputs(project, inputs):
    import build_ordinance_corpus as body
    import download_attachment as originals
    for relative, expected in inputs.items():
        body.require(body.digest(originals.safe_path(project, relative).read_bytes()) == expected,
                     "임베딩 처리 중 입력 변경: " + relative)


def validate_embedding_response(batch, response):
    import build_ordinance_corpus as body
    try:
        body.require(response["model"] == MODEL, "임베딩 응답 모델 불일치")
        vectors = pair_vectors(batch["records"], response)
        body.require(all(math.isfinite(math.hypot(*r["embedding"])) and math.hypot(*r["embedding"]) > 0 for r in vectors),
                     "0 또는 크기가 유효하지 않은 임베딩 벡터")
        usage = response["usage"]
        body.require(type(usage["prompt_tokens"]) is int and type(usage["total_tokens"]) is int
                     and 0 <= usage["prompt_tokens"] <= usage["total_tokens"], "임베딩 사용량 형식 오류")
    except (KeyError, TypeError, IndexError, OverflowError):
        raise ValueError("임베딩 응답 필수 필드/형식 오류") from None
    return vectors, usage


def call_embedding_api(payload_bytes, api_key):
    import build_ordinance_corpus as body
    try:
        with requests.post(API_URL, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                           data=payload_bytes, timeout=(10, 180), allow_redirects=False) as response:
            body.require(response.status_code == 200, f"Embedding HTTP {response.status_code}; 자동 재시도하지 않습니다")
            body.require(len(response.content) <= 20 * 1024 * 1024, "임베딩 응답 크기 제한 초과")
            return response.json()
    except (requests.RequestException, requests.exceptions.JSONDecodeError):
        raise ValueError("Embedding 연결/응답 오류. 요청 처리 여부 확인 전 자동 재호출하지 않습니다") from None


def embedding_inventory(project, plan):
    import build_ordinance_corpus as body
    import download_attachment as originals
    inventory = []
    for batch in plan["batches"]:
        identity = batch["identity"]
        prefix = BATCH_FOLDER + "/" + identity["batch_id"]
        attempt_path = originals.safe_path(project, prefix + ".attempt.json")
        response_path = originals.safe_path(project, prefix + ".response.json")
        attempt_raw = attempt_path.read_bytes() if attempt_path.exists() else None
        response_raw = response_path.read_bytes() if response_path.exists() else None
        body.require(response_raw is None or attempt_raw is not None, "요청 기록 없는 임베딩 응답")
        if attempt_raw is not None:
            attempt = json.loads(attempt_raw)
            body.require(attempt["request"] == identity and isinstance(attempt["requested_at"], str), "기존 임베딩 요청의 입력/설정 충돌")
        entry = {"batch": batch, "attempt_raw": attempt_raw, "response_raw": response_raw, "prefix": prefix}
        if response_raw is not None:
            saved = json.loads(response_raw)
            body.require(saved["request"] == identity and saved["attempt_content_hash"] == body.digest(attempt_raw)
                         and isinstance(saved["received_at"], str), "임베딩 응답의 요청 연결 불일치")
            entry["vectors"], entry["usage"] = validate_embedding_response(batch, saved["response"])
            entry["response_model"] = saved["response"]["model"]
        inventory.append(entry)
    return inventory


def embedding_outputs(project, plan, inventory):
    import build_ordinance_corpus as body
    body.require(all(e["response_raw"] is not None for e in inventory), "임베딩 응답이 아직 완료되지 않았습니다")
    inputs, vectors = dict(plan["input_files"]), []
    for entry in inventory:
        inputs[entry["prefix"] + ".attempt.json"] = body.digest(entry["attempt_raw"])
        inputs[entry["prefix"] + ".response.json"] = body.digest(entry["response_raw"])
        for record, vector in zip(entry["batch"]["records"], entry["vectors"]):
            body.require(record["chunk_id"] == vector["chunk_id"], "벡터와 입력 ID 불일치")
            vectors.append({**vector, "text_sha256": record["text_sha256"]})
    report = {"schema_version": "reviewed-attachment-embeddings-v1", "embedded_count": len(vectors),
              "held_count": plan["held_count"],
              "attachment_count": len({r["attachment_id"] for r in plan["records"]}),
              "api_batch_count": len(inventory), "model": MODEL, "query_model": QUERY_MODEL, "dimension": DIMENSION,
              "response_models": sorted({e["response_model"] for e in inventory}),
              "usage": {k: sum(e["usage"][k] for e in inventory) for k in ("prompt_tokens", "total_tokens")},
              "embedding_performed": True, "database_stored": False, "answer_allowed": False,
              "legal_applicability": "not_determined", "storage_format": "json"}
    data = {"schema_version": "reviewed-attachment-embeddings-v1", "model": MODEL, "query_model": QUERY_MODEL,
            "dimension": DIMENSION, "input_file": READINESS_FOLDER + "/embedding_inputs.json",
            "input_content_hash": plan["ready_input_content_hash"], "answer_allowed": False, "vectors": vectors}
    outputs = {"embeddings.json": body.json_bytes(data), "embedding_report.json": body.json_bytes(report)}
    outputs["embeddings.manifest.json"] = body.json_bytes({"pipeline_version": "reviewed-attachment-embeddings-v1",
        "input_files": inputs, "output_files": {name: body.digest(raw) for name, raw in outputs.items()}, "report": report})
    verify_embedding_inputs(project, inputs)
    return outputs, report


def checked_embedding_outputs(project):
    import build_ordinance_corpus as body
    import download_attachment as originals
    plan = prepare_embedding_plan(project)
    outputs, report = embedding_outputs(project, plan, embedding_inventory(project, plan))
    for name, raw in outputs.items():
        body.require(originals.safe_path(project, BATCH_FOLDER + "/" + name).read_bytes() == raw,
                     "저장된 임베딩 결과 불일치: " + name)
    verify_embedding_inputs(project, json.loads(outputs["embeddings.manifest.json"])["input_files"])
    return plan, outputs, report


def check_embeddings(project):
    return checked_embedding_outputs(project)[2]


def run_embeddings(project, env_file=None, check_only=False):
    import build_ordinance_corpus as body
    import download_attachment as originals
    from request_lock import request_lock
    project = Path(project).resolve(strict=True)
    if check_only:
        return {**check_embeddings(project), "called": 0}
    with request_lock(originals.safe_path(project, BATCH_FOLDER + "/.embeddings.lock")):
        plan = prepare_embedding_plan(project)
        inventory = embedding_inventory(project, plan)
        missing = [e for e in inventory if e["response_raw"] is None]
        final_names = ("embeddings.json", "embedding_report.json", "embeddings.manifest.json")
        if missing:
            body.require(not any(originals.safe_path(project, BATCH_FOLDER + "/" + name).exists() for name in final_names),
                         "미완료 응답과 최종 산출물이 함께 있습니다")
            body.require(not any(e["attempt_raw"] is not None for e in missing),
                         "응답 저장이 끝나지 않은 임베딩 요청이 있습니다. 서비스 처리 여부 확인 전 자동 재호출하지 않습니다")
            api_key = os.getenv("UPSTAGE_API_KEY") or dotenv_values(env_file or project / ".env").get("UPSTAGE_API_KEY")
            body.require(bool(api_key), "UPSTAGE_API_KEY 설정이 없습니다")
        called = 0
        for entry in missing:
            batch = entry["batch"]
            verify_embedding_inputs(project, plan["input_files"])
            attempt = {"request": batch["identity"], "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            attempt_raw = body.json_bytes(attempt)
            body.source_import.write_new(originals.safe_path(project, entry["prefix"] + ".attempt.json"), attempt_raw)
            print(f"임베딩 요청: {batch['identity']['batch_id']} / {len(batch['records'])}개 / {MODEL}", flush=True)
            response = call_embedding_api(body.json_bytes(batch["payload"]), api_key)
            received = {"request": batch["identity"], "attempt_content_hash": body.digest(attempt_raw),
                        "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "response": response}
            # 잘못된 응답도 먼저 보존합니다. 재실행이 같은 요청을 다시 과금하지 않게 합니다.
            body.source_import.write_new(originals.safe_path(project, entry["prefix"] + ".response.json"), body.json_bytes(received))
            called += 1
            validate_embedding_response(batch, response)
            verify_embedding_inputs(project, plan["input_files"])
        inventory = embedding_inventory(project, plan)
        outputs, report = embedding_outputs(project, plan, inventory)
        for name, raw in outputs.items():
            path = originals.safe_path(project, BATCH_FOLDER + "/" + name)
            body.require(not path.exists() or path.read_bytes() == raw, "기존 임베딩 산출물 충돌: " + name)
        verify_embedding_inputs(project, json.loads(outputs["embeddings.manifest.json"])["input_files"])
        for name, raw in outputs.items():
            path = originals.safe_path(project, BATCH_FOLDER + "/" + name)
            if not path.exists():
                body.source_import.write_new(path, raw)
        return {**check_embeddings(project), "called": called}


def load_embedded_records(project):
    plan, outputs, _ = checked_embedding_outputs(project)
    vectors = json.loads(outputs["embeddings.json"])["vectors"]
    return [{**record, "embedding": vector["embedding"]} for record, vector in zip(plan["records"], vectors)]


if __name__ == "__main__":
    main()
