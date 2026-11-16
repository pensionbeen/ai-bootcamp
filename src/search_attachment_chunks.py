"""별표 한 개의 저장된 임베딩으로 질문 검색을 연습합니다."""

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


API_URL = "https://api.upstage.ai/v1/embeddings"
PASSAGE_MODEL = "solar-embedding-2-passage"
QUERY_MODEL = "solar-embedding-2-query"
DIMENSION = 1024


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path, value):
    # Serialize before touching disk so invalid values leave the old file intact.
    serialized = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Every writer owns a unique file on the destination filesystem.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(8):
            try:
                temporary.replace(path)
                break
            except PermissionError as exc:
                # Windows can briefly deny simultaneous replacement or an open
                # reader. Retry only those sharing/access errors, then fail.
                if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 7:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def vector_norm(vector):
    if not isinstance(vector, list) or len(vector) != DIMENSION:
        raise ValueError("벡터 차원이 1024가 아닙니다.")
    if not all(type(x) in (int, float) and math.isfinite(x) for x in vector):
        raise ValueError("벡터에 올바르지 않은 숫자가 있습니다.")
    norm = math.sqrt(sum(x * x for x in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("비교할 수 없는 벡터입니다.")
    return norm


def load_documents(project_dir, stem):
    folder = project_dir / "data" / "processed" / "attachments"
    search_path = folder / f"{stem}.search_chunks.json"
    raw = search_path.read_bytes()
    document = json.loads(raw.decode("utf-8-sig"))
    embeddings = read_json(folder / f"{stem}.embeddings.json")
    if hashlib.sha256(raw).hexdigest() != embeddings["input_content_hash"]:
        raise ValueError("검색 텍스트가 임베딩 생성 당시와 다릅니다.")
    if (
        embeddings["model"] != PASSAGE_MODEL
        or embeddings["query_model"] != QUERY_MODEL
        or embeddings["dimension"] != DIMENSION
    ):
        raise ValueError("문서와 질문 임베딩의 모델 설정이 다릅니다.")
    if document["attachment_id"] != embeddings["attachment_id"]:
        raise ValueError("검색 자료와 벡터의 첨부 ID가 다릅니다.")

    chunks = {c["chunk_id"]: c for c in document["chunks"]}
    vectors = {v["chunk_id"]: v["embedding"] for v in embeddings["vectors"]}
    if len(chunks) != len(document["chunks"]) or len(vectors) != len(embeddings["vectors"]):
        raise ValueError("chunk_id가 중복됐습니다.")
    if not chunks or chunks.keys() != vectors.keys():
        raise ValueError("검색 조각과 벡터의 chunk_id가 맞지 않습니다.")
    context_id = document["attachment_context"]["attachment_id"]
    if context_id != document["attachment_id"] or any(
        c["context_attachment_id"] != context_id for c in chunks.values()
    ):
        raise ValueError("검색 조각과 비고 묶음의 첨부 ID가 다릅니다.")
    for vector in vectors.values():
        vector_norm(vector)
    return document, embeddings, chunks, vectors


def embed_question(project_dir, question, cache_path):
    if cache_path.exists():
        cached = read_json(cache_path)
        if cached["question"] != question or cached["model"] != QUERY_MODEL:
            raise ValueError("저장된 질문 임베딩의 입력 또는 모델이 다릅니다.")
        vector_norm(cached["embedding"])
        print("저장된 질문 임베딩을 재사용합니다.")
        return cached

    load_dotenv(project_dir / ".env")
    api_key = os.getenv("UPSTAGE_API_KEY")
    if not api_key:
        raise ValueError(".env에 UPSTAGE_API_KEY를 설정하세요.")
    response = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": QUERY_MODEL, "input": [question]},
        timeout=(10, 120),
    )
    print("질문 임베딩 HTTP 상태:", response.status_code)
    response.raise_for_status()
    result = response.json()
    data = result["data"]
    if len(data) != 1 or type(data[0]["index"]) is not int or data[0]["index"] != 0:
        raise ValueError("질문 임베딩 응답이 입력과 맞지 않습니다.")
    vector_norm(data[0]["embedding"])
    cached = {
        "question": question,
        "model": QUERY_MODEL,
        "response_model": result.get("model"),
        "embedding": data[0]["embedding"],
        "usage": result.get("usage", {}),
        "embedded_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(cache_path, cached)
    return cached


def rank_chunks(chunks, vectors, query_vector, top_k):
    query_norm = vector_norm(query_vector)
    hits = []
    for chunk_id, vector in vectors.items():
        norm = vector_norm(vector)
        score = sum(a * b for a, b in zip(query_vector, vector)) / (query_norm * norm)
        hits.append({"score": score, **chunks[chunk_id]})
    hits.sort(key=lambda hit: (-hit["score"], hit["chunk_id"]))
    return [{"rank": rank, **hit} for rank, hit in enumerate(hits[:top_k], 1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--stem", default="2171833_22740725")
    parser.add_argument("--question", required=True)
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()
    question = args.question.strip()
    if not question:
        raise ValueError("질문을 입력하세요.")

    document, embeddings, chunks, vectors = load_documents(args.project_dir, args.stem)
    if not 1 <= args.top_k <= len(chunks):
        raise ValueError("top-k는 1 이상 전체 조각 수 이하여야 합니다.")
    cache_key = hashlib.sha256((QUERY_MODEL + "\0" + question).encode("utf-8")).hexdigest()[:16]
    query = embed_question(args.project_dir, question, args.output_dir / f"query-{cache_key}.json")
    hits = rank_chunks(chunks, vectors, query["embedding"], args.top_k)
    output = {
        "question": question,
        "retrieval_status": "candidates_only",
        "similarity_metric": "cosine",
        "query_model": QUERY_MODEL,
        "passage_model": PASSAGE_MODEL,
        "search_chunks_content_hash": embeddings["input_content_hash"],
        "review_status": document["review_status"],
        "source": document["source"],
        "hits": hits,
        "attachment_context": document["attachment_context"],
    }
    output_path = args.output_dir / f"{args.stem}.{cache_key}.search.json"
    save_json(output_path, output)

    print("질문:", question)
    for hit in hits:
        print(f'{hit["rank"]}위 | 유사도 {hit["score"]:.4f} | {hit["row_label"]}')
        print("  조각 ID:", hit["chunk_id"])
        print("  원문 위치:", json.dumps(hit["source_refs"], ensure_ascii=False))
    print("함께 보존한 문맥:", len(output["attachment_context"]["blocks"]))
    print("시행일:", output["source"]["effective_date"])
    print("출처:", output["source"]["source_url"])
    print("저장:", output_path)


SEARCH_FOLDER = "data/processed/attachments/2026-10-01/search_validation"
CASES_FILE = SEARCH_FOLDER + "/evaluation_cases.json"
CASES_SHA256 = "9822843366e32126a363a46448470f39bd62a15a2e1bbc7b604f1b9e21a0accf"
INITIAL_TOP_K = 5


def load_reviewed_corpus(project):
    import embed_attachment_chunks as embedding
    import build_ordinance_corpus as body
    plan, outputs, report = embedding.checked_embedding_outputs(project)
    vectors = json.loads(outputs["embeddings.json"])["vectors"]
    records = [{**record, "embedding": vector["embedding"]} for record, vector in zip(plan["records"], vectors)]
    inputs = dict(json.loads(outputs["embeddings.manifest.json"])["input_files"])
    inputs.update({embedding.BATCH_FOLDER + "/" + name: body.digest(raw) for name, raw in outputs.items()})
    documents = {}
    for record in records:
        name, title = record["text"].split("\n\n", 2)[:2]
        documents[record["attachment_id"]] = {"attachment_id": record["attachment_id"], "ordin_name": name,
                                             "title": title, "source": record["source"]}
    return {"records": records, "input_files": inputs,
            "corpus_hash": body.digest(outputs["embeddings.manifest.json"]),
            "scope": {"as_of_date": "2026-10-01", "searchable_chunk_count": len(records),
                      "held_candidate_count": report["held_count"], "body_corpus_embedded": False,
                      "attachments": list(documents.values())}}


def call_reviewed_api(endpoint, payload_bytes, api_key):
    """HTTP 오류에 비밀값이나 응답 원문을 포함하지 않습니다. 재시도는 하지 않습니다."""
    import build_ordinance_corpus as body
    try:
        with requests.post(endpoint, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                           data=payload_bytes, timeout=(10, 180), allow_redirects=False) as response:
            body.require(response.status_code == 200, f"검색 검증 API HTTP {response.status_code}; 자동 재시도하지 않습니다")
            body.require(len(response.content) <= 20 * 1024 * 1024, "검색 검증 API 응답 크기 초과")
            return response.json()
    except (requests.RequestException, requests.exceptions.JSONDecodeError):
        raise ValueError("검색 검증 API 연결/응답 오류. 처리 여부 확인 전 자동 재호출하지 않습니다") from None


def cached_reviewed_call(project, corpus, kind, payload, env_file=None, read_only=False):
    import build_ordinance_corpus as body
    import download_attachment as originals
    import embed_attachment_chunks as embedding
    import evidence_gate as gate
    from dotenv import dotenv_values
    body.require(kind in ("query", "gate"), "지원하지 않는 검색 검증 요청")
    endpoint = API_URL if kind == "query" else gate.CHAT_URL
    raw_payload = body.json_bytes(payload)
    identity = {"endpoint": endpoint, "request_body_sha256": body.digest(raw_payload),
                "corpus_hash": corpus["corpus_hash"], "kind": kind}
    key = body.digest(body.json_bytes(identity))
    prefix = SEARCH_FOLDER + "/cache/" + kind + "-" + key
    attempt_path = originals.safe_path(project, prefix + ".attempt.json")
    response_path = originals.safe_path(project, prefix + ".response.json")
    called = 0
    if response_path.exists():
        attempt_raw = attempt_path.read_bytes()
        body.require(json.loads(attempt_raw)["request"] == identity, "검색 요청 캐시 불일치")
    else:
        body.require(not read_only, "저장된 검색 API 응답이 없습니다")
        body.require(not attempt_path.exists(), "응답 저장이 끝나지 않은 검색 요청입니다. 처리 확인 전 자동 재호출하지 않습니다")
        embedding.verify_embedding_inputs(project, corpus["input_files"])
        api_key = os.getenv("UPSTAGE_API_KEY") or dotenv_values(env_file or Path(project) / ".env").get("UPSTAGE_API_KEY")
        body.require(bool(api_key), "UPSTAGE_API_KEY 설정이 없습니다")
        attempt_raw = body.json_bytes({"request": identity, "requested_at": datetime.now(timezone.utc).isoformat()})
        body.source_import.write_new(attempt_path, attempt_raw)
        raw_response = call_reviewed_api(endpoint, raw_payload, api_key)
        if kind == "gate":
            # 제공자가 반환할 수 있는 내부 추론 필드는 읽거나 저장하지 않습니다.
            response_object = raw_response if isinstance(raw_response, dict) else {}
            choices = response_object.get("choices")
            raw_response = {"model": response_object.get("model"), "usage": response_object.get("usage"),
                            "choices": [{"finish_reason": c.get("finish_reason"),
                                         "message": {"content": c.get("message", {}).get("content"),
                                                     "refusal": c.get("message", {}).get("refusal")}}
                                        for c in (choices if isinstance(choices, list) else [])
                                        if isinstance(c, dict) and isinstance(c.get("message"), dict)]}
        receipt = {"request": identity, "attempt_content_hash": body.digest(attempt_raw),
                   "received_at": datetime.now(timezone.utc).isoformat(), "response": raw_response}
        body.source_import.write_new(response_path, body.json_bytes(receipt))
        called = 1
    response_raw = response_path.read_bytes()
    receipt = json.loads(response_raw)
    body.require(receipt["request"] == identity and receipt["attempt_content_hash"] == body.digest(attempt_raw),
                 "검색 응답의 입력 연결 불일치")
    embedding.verify_embedding_inputs(project, corpus["input_files"])
    files = {prefix + ".attempt.json": body.digest(attempt_raw), prefix + ".response.json": body.digest(response_raw)}
    return receipt["response"], files, called


def reviewed_queries(project, corpus, questions, env_file=None, read_only=False):
    import build_ordinance_corpus as body
    import embed_attachment_chunks as embedding
    body.require(isinstance(questions, list) and questions and all(isinstance(q, str) and q.strip()
                 and len(q.encode("utf-8")) <= 4000 for q in questions), "질문이 비어 있거나 길이/개수 제한을 초과했습니다")
    groups, group, size = [], [], 0
    for question in questions:
        length = len(question.encode("utf-8"))
        if group and (len(group) == 100 or size + length > 200000):
            groups.append(group)
            group, size = [], 0
        group.append(question)
        size += length
    if group:
        groups.append(group)
    vectors, files, called = [], {}, 0
    for group in groups:
        response, batch_files, used = cached_reviewed_call(project, corpus, "query", {"model": QUERY_MODEL, "input": group}, env_file, read_only)
        body.require(response["model"] == QUERY_MODEL, "질문 임베딩 응답 모델 불일치")
        paired = embedding.pair_vectors([{"chunk_id": str(i)} for i in range(len(group))], response)
        for item in paired:
            vector_norm(item["embedding"])
        vectors.extend(p["embedding"] for p in paired)
        files.update(batch_files)
        called += used
    return vectors, files, called


def run_reviewed_question(project, corpus, question, query_vector, env_file=None, read_only=False,
                          record_validation_errors=False):
    import evidence_gate as gate
    import build_ordinance_corpus as body
    import re
    records = {r["chunk_id"]: r for r in corpus["records"]}
    ranked = rank_chunks(records, {k: r["embedding"] for k, r in records.items()}, query_vector, len(records))
    rounds, inputs, called = [], {}, 0
    for limit in dict.fromkeys((min(INITIAL_TOP_K, len(ranked)), len(ranked))):
        selected = [records[h["chunk_id"]] for h in ranked[:limit]]
        payload, units = gate.reviewed_gate_payload(question, selected, corpus["scope"])
        response, files, used = cached_reviewed_call(project, corpus, "gate", payload, env_file, read_only)
        inputs.update(files)
        called += used
        response_model = response.get("model", "") if isinstance(response, dict) else ""
        body.require(isinstance(response_model, str) and re.fullmatch(re.escape(gate.MODEL) + r"(?:-\d{6})?", response_model),
                     "근거 판정 응답 모델 불일치")
        stage = {"searched_chunk_count": limit, "chunk_ids": [h["chunk_id"] for h in ranked[:limit]],
                 "initial_model_result": None, "initial_validation_error": None,
                 "model_result_before_policy": None, "policy_hold": None, "verification": None,
                 "result": None, "response_model": response_model, "usage": response.get("usage"),
                 "request_files": files}
        rounds.append(stage)
        try:
            initial = gate.decode_response(response)
            stage["initial_model_result"] = initial
            try:
                result = gate.resolve_reviewed_result(response, units)
            except gate.GateError as exc:
                stage["initial_validation_error"] = str(exc)
                if str(exc) not in ("missing_parent_condition", "incomplete_parent_condition", "unexpected_missing_information"):
                    raise
                result = None
            if result is None or result["decision"] == "supported":
                review_payload, review_units = gate.reviewed_verification_payload(question, selected, corpus["scope"], initial)
                reviewed, review_files, review_used = cached_reviewed_call(project, corpus, "gate", review_payload, env_file, read_only)
                inputs.update(review_files)
                called += review_used
                review_model = reviewed.get("model", "") if isinstance(reviewed, dict) else ""
                body.require(isinstance(review_model, str) and re.fullmatch(re.escape(gate.MODEL) + r"(?:-\d{6})?", review_model),
                             "근거 재검토 응답 모델 불일치")
                stage["verification"] = {"response_model": review_model, "usage": reviewed.get("usage"),
                                         "request_files": review_files, "model_result": None, "result": None}
                stage["verification"]["model_result"] = gate.decode_response(reviewed)
                result = gate.resolve_reviewed_result(reviewed, review_units)
                stage["verification"]["result"] = result
            stage["model_result_before_policy"] = result
            result, policy_hold = gate.apply_reviewed_policy(question, result, corpus["records"])
            stage["policy_hold"] = policy_hold
            if len(rounds) > 1 and result["decision"] == "out_of_scope":
                raise gate.GateError("scope_changed_during_expansion")
        except gate.GateError as exc:
            if not record_validation_errors:
                raise
            # Evaluation records the failed contract; normal searches still raise.
            # An invalid response is neither an absence of evidence nor a scope decision.
            result = {"decision": "validation_error", "error_code": str(exc), "evidence": [],
                      "missing_information": [], "reason": "모델 응답 검증 실패: " + str(exc),
                      "answer_allowed": False, "legal_applicability": "not_determined"}
        stage["result"] = result
        if stage["policy_hold"] is not None or result["decision"] not in ("insufficient_evidence", "needs_clarification"):
            break
    hits = [{k: h[k] for k in ("rank", "score", "chunk_id", "attachment_id", "text_sha256", "source_refs")}
            for h in ranked[:INITIAL_TOP_K]]
    messages = {"supported": "질문을 뒷받침하는 근거를 선택했습니다. 최종 답변 생성·적용 확정 전입니다.",
                "insufficient_evidence": "현재 검색 가능한 자료에서 질문의 모든 조건을 확인할 근거가 부족합니다.",
                "needs_clarification": "질문에 답하는 데 필요한 대상·조건 등 추가 사실관계를 확인해야 합니다.",
                "out_of_scope": "요청한 관할·주제 또는 시행 판본이 이 프로젝트의 범위를 벗어납니다.",
                "validation_error": "모델 응답이 검증 규칙을 통과하지 못해 판정을 보류했습니다. 자료의 유무는 확정하지 않았습니다."}
    return {"question": question, "corpus_hash": corpus["corpus_hash"], "scope": corpus["scope"],
            "retrieval_status": "candidates_then_semantic_evidence_check", "similarity_is_probability": False,
            "hits": hits, "rounds": rounds, **result, "evidence_sufficient": result["decision"] == "supported",
            "answer_allowed": False, "answer_generated": False, "query_model": QUERY_MODEL,
            "user_message": messages[result["decision"]], "reason_is_model_diagnostic_not_answer": True,
            "gate_model": gate.MODEL, "source_review": "representation_reviewed_legal_applicability_pending"}, inputs, called


def search_reviewed(project, question, env_file=None, check_only=False):
    import build_ordinance_corpus as body
    import download_attachment as originals
    import embed_attachment_chunks as embedding
    from request_lock import request_lock
    from contextlib import nullcontext
    project = Path(project).resolve(strict=True)
    with (nullcontext() if check_only else request_lock(originals.safe_path(project, SEARCH_FOLDER + "/.search.lock"))):
        corpus = load_reviewed_corpus(project)
        vectors, inputs, called = reviewed_queries(project, corpus, [question], env_file, check_only)
        result, gate_inputs, used = run_reviewed_question(project, corpus, question, vectors[0], env_file, check_only)
        inputs = {**corpus["input_files"], **inputs, **gate_inputs}
        output = {"input_files": inputs, "result": result}
        raw = body.json_bytes(output)
        key = body.digest((corpus["corpus_hash"] + "\0" + question).encode("utf-8"))
        path = originals.safe_path(project, SEARCH_FOLDER + "/queries/" + key + ".json")
        embedding.verify_embedding_inputs(project, inputs)
        body.require(not path.exists() or path.read_bytes() == raw, "기존 질문 검색 결과 충돌")
        if check_only:
            body.require(path.exists(), "저장된 질문 결과가 없습니다")
        elif not path.exists():
            body.source_import.write_new(path, raw)
        embedding.verify_embedding_inputs(project, inputs)
        return {"result": result, "called": called + used, "output_file": str(path)}


def evaluation_preview(results, report):
    import html
    esc = html.escape
    labels = {"supported": "근거 있음", "insufficient_evidence": "자료에서 확인 불가",
              "needs_clarification": "추가 조건 필요", "out_of_scope": "프로젝트 범위 밖",
              "validation_error": "응답 검증 실패 · 판정 보류"}
    panels = []
    for row in results:
        result = row["result"]
        hits = "".join(f'<li>{esc(h["chunk_id"])} · {h["score"]:.4f}</li>' for h in result["hits"])
        evidence = "".join('<details><summary>' + esc(e["unit_id"]) + '</summary><pre>' + esc(e["quote"]) + '</pre></details>' for e in result["evidence"])
        review_trace = []
        for stage in result["rounds"]:
            if stage["verification"] is not None:
                review_trace.append({"검토후보수": stage["searched_chunk_count"],
                                     "최초모델응답": stage["initial_model_result"],
                                     "형식검사차단": stage["initial_validation_error"],
                                     "재검토모델응답": stage["verification"]["model_result"],
                                     "코드보류규칙": stage["policy_hold"],
                                     "최종판정": stage["result"]["decision"], "최종이유": stage["result"]["reason"]})
        trace = ('<details><summary>최초 판정과 재검토 기록 · 최초 오류도 보존</summary><pre>'
                 + esc(json.dumps(review_trace, ensure_ascii=False, indent=2)) + '</pre></details>') if review_trace else ''
        panels.append('<section><h2>' + esc(row["question"]) + '</h2><p>' + ("통과" if row["passed"] else "실패")
                      + ' · 기대: ' + labels[row["expected_decision"]] + ' · 실제: ' + labels[result["decision"]]
                      + '</p><p>' + esc(result["user_message"]) + '</p><details><summary>모델의 판정 진단 · 검증된 사실 답변이 아님</summary><p>'
                      + esc(result["reason"]) + '</p></details><p>검토한 후보: '
                      + " → ".join(str(r["searched_chunk_count"]) for r in result["rounds"]) + '개</p>'
                      + '<details><summary>유사도 상위 후보 · 정답 확률이 아님</summary><ol>' + hits + '</ol></details>' + trace + evidence + '</section>')
    return ('''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>질문 검색과 근거 판단 평가</title><style>body{font-family:system-ui,sans-serif;max-width:1150px;margin:auto;padding:26px;line-height:1.7;color:#172635;background:#f4f7fb}section{background:white;border:1px solid #ced9e5;border-radius:12px;padding:20px;margin:20px 0}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf3f8;padding:14px;font-family:inherit}li{overflow-wrap:anywhere}h2{font-size:1.15rem}</style><h1>검색 결과가 질문의 근거인가</h1>'''
        + f'<p>평가 {report["case_count"]}개 / 통과 {report["passed_count"]}개 / 응답 검증 오류 {report["validation_error_count"]}개 / 잘못된 긍정 판정 {report["false_supported_count"]}개</p>'
        + '<p>질문 임베딩 → 상위 5개 검색 → 근거 판단 → 근거 있음은 가정·조건 재검토 → 부족하거나 모호하면 검색 가능한 62개 전체 재검토. 유사도만으로 답하지 않습니다.</p>'
        + '<p>미리 고정한 소규모 시험이며 전체 정확도 보장이 아닙니다. 보류 31개·대부분의 본문·부칙은 검색 대상에 포함되지 않습니다. 최종 답변은 생성하지 않았고 answer_allowed=false를 유지합니다.</p>'
        + ''.join(panels) + '</html>').encode('utf-8')


def run_search_evaluation(project, env_file=None, check_only=False):
    import evidence_gate as gate
    import build_ordinance_corpus as body
    import download_attachment as originals
    import embed_attachment_chunks as embedding
    from request_lock import request_lock
    from contextlib import nullcontext
    project = Path(project).resolve(strict=True)
    with (nullcontext() if check_only else request_lock(originals.safe_path(project, SEARCH_FOLDER + "/.search.lock"))):
        corpus = load_reviewed_corpus(project)
        raw_cases = originals.safe_path(project, CASES_FILE).read_bytes()
        body.require(body.digest(raw_cases) == CASES_SHA256, "고정한 평가 질문이 변경됐습니다")
        cases = json.loads(raw_cases)["cases"]
        body.require(len({c["case_id"] for c in cases}) == len(cases), "평가 ID 중복")
        # 기대 판정·기대 근거는 API 입력으로 넘기지 않습니다.
        query_vectors, query_inputs, called = reviewed_queries(project, corpus, [c["question"] for c in cases], env_file, check_only)
        inputs = {**corpus["input_files"], CASES_FILE: body.digest(raw_cases), **query_inputs}
        results = []
        for case, vector in zip(cases, query_vectors):
            if not check_only:
                print("질문 검증:", case["case_id"], flush=True)
            result, gate_inputs, used = run_reviewed_question(project, corpus, case["question"], vector, env_file, check_only,
                                                             record_validation_errors=True)
            inputs.update(gate_inputs)
            called += used
            cited = {c["unit_id"] for c in result["evidence"] if c["kind"] == "primary"
                     and (result["decision"] != "supported" or c["role"] == "direct_support")}
            passed = result["decision"] == case["expected_decision"] and set(case["required_chunk_ids"]) <= cited
            results.append({**case, "passed": passed, "result": result})
        report = {"schema_version": "reviewed-search-evaluation-v1", "corpus_hash": corpus["corpus_hash"],
                  "case_count": len(cases), "passed_count": sum(r["passed"] for r in results),
                  "validation_error_count": sum(r["result"]["decision"] == "validation_error" for r in results),
                  "false_supported_count": sum(r["result"]["decision"] == "supported" and r["expected_decision"] != "supported" for r in results),
                  "failed_case_ids": [r["case_id"] for r in results if not r["passed"]],
                  "query_model": QUERY_MODEL, "gate_model": gate.MODEL, "searchable_chunk_count": len(corpus["records"]),
                  "held_candidate_count": corpus["scope"]["held_candidate_count"],
                  "answer_allowed": False, "answer_generated": False, "database_stored": False,
                  "verification_count": sum(r["verification"] is not None for row in results for r in row["result"]["rounds"]),
                  "initial_validation_failures": sum(r["initial_validation_error"] is not None for row in results for r in row["result"]["rounds"]),
                  "initial_false_supported_count": sum(row["expected_decision"] != "supported" and any(
                      (r["initial_model_result"] or {}).get("decision") == "supported" for r in row["result"]["rounds"]) for row in results),
                  "policy_hold_count": sum(r["policy_hold"] is not None for row in results for r in row["result"]["rounds"]),
                  "gate_request_count": sum("/gate-" in name and name.endswith(".response.json") for name in inputs),
                  "query_request_count": sum("/query-" in name and name.endswith(".response.json") for name in inputs),
                  "evaluation_limit": "fixed_small_test_set_not_general_accuracy_guarantee"}
        outputs = {"evaluation_results.json": body.json_bytes({"report": report, "cases": results}),
                   "evaluation_report.json": body.json_bytes(report), "preview.html": evaluation_preview(results, report)}
        outputs["evaluation.manifest.json"] = body.json_bytes({"pipeline_version": "reviewed-search-evaluation-v1",
            "input_files": inputs, "output_files": {name: body.digest(raw) for name, raw in outputs.items()}, "report": report})
        embedding.verify_embedding_inputs(project, inputs)
        for name, raw in outputs.items():
            path = originals.safe_path(project, SEARCH_FOLDER + "/" + name)
            body.require(not path.exists() or path.read_bytes() == raw, "기존 검색 평가 결과 충돌: " + name)
            if check_only:
                body.require(path.exists(), "저장된 검색 평가 결과가 없습니다: " + name)
        if not check_only:
            for name, raw in outputs.items():
                path = originals.safe_path(project, SEARCH_FOLDER + "/" + name)
                if not path.exists():
                    body.source_import.write_new(path, raw)
        embedding.verify_embedding_inputs(project, inputs)
        return {**report, "called": called}


if __name__ == "__main__":
    main()


