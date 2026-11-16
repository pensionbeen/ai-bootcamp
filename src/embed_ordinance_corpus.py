"""Embed the existing body and supplement inputs once, with resumable receipts.

Default execution only describes the plan. --execute calls Upstage sequentially.
Existing attachment vectors and legal applicability flags are never modified.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

from body_embedding_inputs import prepare_body
from supplement_embedding_inputs import prepare_supplements

MODEL = "solar-embedding-2-passage"
QUERY_MODEL = "solar-embedding-2-query"
DIMENSION = 1024
ENDPOINT = "https://api.upstage.ai/v1/embeddings"
ASOF = "2026-10-01"
FOLDER = "data/processed/ordinances/2026-10-01/embeddings"
MAX_BATCH = 100
MAX_TEXT_BYTES = 8000
MAX_BATCH_BYTES = 60000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def safe(root, relative):
    root = Path(root).resolve()
    name = Path(relative)
    require(not name.is_absolute() and ".." not in name.parts, "unsafe_relative_path")
    target = root / name
    require(target.resolve().is_relative_to(root), "output_outside_project")
    require(not any(p.is_symlink() for p in (target, *target.parents) if p.is_relative_to(root)), "symlink_path")
    return target


def save(path, value):
    content = raw(value)
    if path.exists():
        require(path.read_bytes() == content, "immutable_output_conflict:" + path.name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def read(path):
    return json.loads(path.read_bytes())


def verify_inputs(project, inputs):
    for name, expected in inputs.items():
        require(digest(safe(project, name).read_bytes()) == expected, "input_changed:" + name)


def prepare(project):
    """Read already processed sources; do not collect, parse, or call a model."""
    project = Path(project).resolve(strict=True)
    body = prepare_body(project)
    supplements = prepare_supplements(project)
    records = sorted(body["records"] + supplements["records"], key=lambda r: (r["kind"], r["chunk_id"]))
    require(len(records) == len({r["chunk_id"] for r in records}) == 717, "expected_717_unique_inputs")
    require(Counter(r["kind"] for r in records) == {"body": 513, "supplement": 204}, "input_kind_counts")
    inputs = dict(body["input_files"])
    for name, expected in supplements["input_files"].items():
        require(name not in inputs or inputs[name] == expected, "conflicting_source_hash")
        inputs[name] = expected
    for record in records:
        text = record["text"]
        require(isinstance(text, str) and text.strip() and len(text.encode("utf-8")) <= MAX_TEXT_BYTES,
                "empty_or_oversized_embedding_input:" + record["chunk_id"])
        require(record["text_sha256"] == digest(text.encode("utf-8")), "text_hash_mismatch")
        require(record["answer_allowed"] is False and record["embedding_allowed"] is True, "unexpected_input_flags")
    document = {"schema_version": "ordinance-embedding-inputs-v1", "as_of_date": ASOF,
                "model": MODEL, "query_model": QUERY_MODEL, "dimension": DIMENSION,
                "records": records, "input_files": dict(sorted(inputs.items())), "answer_allowed": False}
    identity = digest(raw(document))
    batches, current, size = [], [], 0
    for record in records:
        length = len(record["text"].encode("utf-8"))
        if current and (len(current) >= MAX_BATCH or size + length > MAX_BATCH_BYTES):
            batches.append(current)
            current, size = [], 0
        current.append(record)
        size += length
    if current:
        batches.append(current)
    requests = []
    for number, group in enumerate(batches, 1):
        payload = {"model": MODEL, "input": [r["text"] for r in group]}
        requests.append({"batch_id": f"batch-{number:04d}", "payload": payload,
                         "payload_sha256": digest(raw(payload)),
                         "items": [{"chunk_id": r["chunk_id"], "text_sha256": r["text_sha256"]} for r in group]})
    verify_inputs(project, inputs)
    return {"identity": identity, "relative": FOLDER + "/" + identity[:20], "document": document,
            "batches": requests, "summary": {"body_count": 513, "supplement_count": 204,
              "new_input_count": 717, "existing_attachment_vectors_reused_without_new_calls": 93,
              "batch_count": len(requests), "input_utf8_bytes": sum(len(r["text"].encode("utf-8")) for r in records),
              "model": MODEL, "dimension": DIMENSION, "answer_allowed": False,
              "body_details": body["summary"], "supplement_details": supplements["summary"]}}


def validate_response(batch, body):
    require(isinstance(body, dict) and body.get("model") == MODEL, "unexpected_embedding_model")
    data = body.get("data")
    require(isinstance(data, list) and len(data) == len(batch["items"]), "embedding_response_count")
    ordered = {}
    for item in data:
        index, vector = item.get("index"), item.get("embedding")
        require(type(index) is int and 0 <= index < len(data) and index not in ordered, "embedding_response_index")
        require(isinstance(vector, list) and len(vector) == DIMENSION and
                all(type(x) in (int, float) and math.isfinite(x) for x in vector), "invalid_embedding_vector")
        require(math.isfinite(math.hypot(*vector)) and math.hypot(*vector) > 0, "invalid_embedding_norm")
        ordered[index] = vector
    usage = body.get("usage", {})
    require(all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("prompt_tokens", "total_tokens")), "invalid_usage")
    require(usage["total_tokens"] >= usage["prompt_tokens"], "invalid_usage_total")
    return [{**item, "embedding": ordered[i]} for i, item in enumerate(batch["items"])], usage


def batch_state(folder, batch):
    """Resolve all receipts before issuing another request; unknown outcomes stop."""
    for number in (1, 2):
        prefix = batch["batch_id"] + f".attempt-{number}"
        attempt_path = folder / (prefix + ".json")
        response_path = folder / (prefix + ".response.json")
        failure_path = folder / (prefix + ".failure.json")
        require(not response_path.exists() or attempt_path.exists(), "response_without_attempt")
        if not attempt_path.exists():
            require(not failure_path.exists(), "failure_without_attempt")
            if number == 1:
                require(not any(folder.glob(batch["batch_id"] + ".attempt-2*")), "second_attempt_without_first")
            return {"complete": False, "next_attempt": number}
        attempt = read(attempt_path)
        require(attempt["payload_sha256"] == batch["payload_sha256"] and attempt["items"] == batch["items"],
                "saved_attempt_input_mismatch")
        require(response_path.exists() and not failure_path.exists(), "unresolved_attempt_no_automatic_retry:" + prefix)
        response = read(response_path)
        require(response["attempt_sha256"] == digest(attempt_path.read_bytes()), "response_attempt_binding")
        if response["http_status"] == 200:
            require(not (number == 1 and any(folder.glob(batch["batch_id"] + ".attempt-2*"))), "unexpected_duplicate_attempt")
            vectors, usage = validate_response(batch, response["body"])
            return {"complete": True, "vectors": vectors, "usage": usage,
                    "attempt_file": attempt_path.name, "response_file": response_path.name}
        require(response["http_status"] == 429, "saved_provider_error:" + str(response["http_status"]))
        require(number == 1, "second_rate_limit_response_stop")
    raise ValueError("unreachable_batch_state")


def request_batch(folder, batch, number, api_key):
    import requests
    prefix = batch["batch_id"] + f".attempt-{number}"
    attempt_path = folder / (prefix + ".json")
    attempt = {"payload_sha256": batch["payload_sha256"], "items": batch["items"],
               "requested_at": datetime.now(timezone.utc).isoformat(), "endpoint": ENDPOINT, "model": MODEL}
    save(attempt_path, attempt)
    try:
        response = requests.post(ENDPOINT, headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                                 data=raw(batch["payload"]), timeout=(10, 60), allow_redirects=False)
    except requests.RequestException as exc:
        save(folder / (prefix + ".failure.json"), {"type": type(exc).__name__, "outcome": "unknown_no_automatic_retry"})
        raise ValueError("embedding_network_failure_outcome_unknown") from None
    response_bytes = response.content.replace(api_key.encode("utf-8"), b"[REDACTED]")
    # Preserve provider bytes even when malformed JSON/non-finite numbers make
    # the structured response impossible to serialize or validate.
    with (folder / (prefix + ".response.raw")).open("xb") as stream:
        stream.write(response_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    require(len(response_bytes) <= 20 * 1024 * 1024, "oversized_response_outcome_unknown")
    try:
        body = json.loads(response_bytes)
    except ValueError:
        body = {"invalid_json_text": response_bytes.decode("utf-8", errors="replace")}
    receipt = {"attempt_sha256": digest(attempt_path.read_bytes()), "received_at": datetime.now(timezone.utc).isoformat(),
               "http_status": response.status_code,
               "retry_after": (response.headers.get("Retry-After") or "").replace(api_key, "[REDACTED]"), "body": body}
    save(folder / (prefix + ".response.json"), receipt)
    response.close()
    return receipt


def retry_delay(folder, batch):
    receipt = read(folder / (batch["batch_id"] + ".attempt-1.response.json"))
    try:
        requested = float(receipt.get("retry_after") or 60)
        wait = min(300.0, max(60.0, requested)) if math.isfinite(requested) else 60.0
    except (ValueError, TypeError):
        wait = 60.0
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(receipt["received_at"])).total_seconds()
    return max(0.0, wait - elapsed)


def outputs(plan, states):
    vectors = [vector for state in states for vector in state["vectors"]]
    report = {"schema_version": "ordinance-embeddings-v1", "as_of_date": ASOF, "input_identity": plan["identity"],
              **plan["summary"], "embedded_count": len(vectors), "embedding_performed": True,
              "recorded_successful_api_responses": len(states),
              "usage": {key: sum(state["usage"][key] for state in states) for key in ("prompt_tokens", "total_tokens")},
              "database_stored": False, "answer_generated": False, "legal_applicability": "not_determined"}
    data = {"schema_version": "ordinance-embeddings-v1", "as_of_date": ASOF,
            "model": MODEL, "query_model": QUERY_MODEL, "dimension": DIMENSION,
            "input_identity": plan["identity"], "input_file": "inputs.json", "vectors": vectors, "answer_allowed": False}
    return {"embeddings.json": data, "embedding_report.json": report}


def run(project, execute=False, check_only=False, env_file=None):
    project = Path(project).resolve(strict=True)
    plan = prepare(project)
    if not execute and not check_only:
        return {**plan["summary"], "input_identity": plan["identity"], "output_folder": str(safe(project, plan["relative"])), "called": 0}
    folder = safe(project, plan["relative"])
    if check_only:
        states = [batch_state(folder, batch) for batch in plan["batches"]]
        require(all(state["complete"] for state in states), "embedding_incomplete")
        expected = outputs(plan, states)
        require((folder / "inputs.json").read_bytes() == raw(plan["document"]), "saved_inputs_differ")
        for name, value in expected.items():
            require((folder / name).read_bytes() == raw(value), "saved_embedding_output_differ:" + name)
        manifest = read(folder / "embeddings.manifest.json")
        require(manifest["input_identity"] == plan["identity"] and manifest["input_files"] == plan["document"]["input_files"], "manifest_input_mismatch")
        for name, expected_hash in manifest["output_files"].items():
            require(digest(safe(folder, name).read_bytes()) == expected_hash, "manifest_output_mismatch")
        return {**expected["embedding_report.json"], "called": 0, "output_folder": str(folder)}
    sys.path.insert(0, str(project / "src"))
    from request_lock import request_lock
    from dotenv import dotenv_values
    with request_lock(folder / ".embedding.lock"):
        states = [batch_state(folder, batch) for batch in plan["batches"]]
        missing = [i for i, state in enumerate(states) if not state["complete"]]
        if missing:
            require(not any((folder / name).exists() for name in ("embeddings.json", "embedding_report.json", "embeddings.manifest.json")),
                    "incomplete_batches_with_final_outputs")
        save(folder / "inputs.json", plan["document"])
        key = (os.getenv("UPSTAGE_API_KEY") or dotenv_values(env_file or project / ".env").get("UPSTAGE_API_KEY")) if missing else None
        require(not missing or isinstance(key, str) and bool(key.strip()), "missing_upstage_api_key")
        called, last_call = 0, 0.0
        for index in missing:
            batch = plan["batches"][index]
            number = states[index]["next_attempt"]
            while True:
                verify_inputs(project, plan["document"]["input_files"])
                delay = max(0.0, 12 - (time.monotonic() - last_call)) if last_call else 0.0
                if number == 2:
                    delay = max(delay, retry_delay(folder, batch))
                if delay:
                    print(f"호출 간격 대기: {delay:.0f}초", flush=True)
                    while delay > 0:
                        step = min(delay, 30)
                        time.sleep(step)
                        delay -= step
                print(f"실제 passage 임베딩: {index + 1}/{len(plan['batches'])} 배치, {len(batch['items'])}개", flush=True)
                last_call = time.monotonic()
                receipt = request_batch(folder, batch, number, key)
                called += 1
                if receipt["http_status"] == 429 and number == 1:
                    number = 2
                    continue
                states[index] = batch_state(folder, batch)
                require(states[index]["complete"], "batch_not_complete")
                print(f"저장 완료: {sum(len(s.get('vectors', [])) for s in states)}/717개", flush=True)
                break
        verify_inputs(project, plan["document"]["input_files"])
        expected = outputs(plan, states)
        for name, value in expected.items():
            save(folder / name, value)
        files = [p for p in folder.iterdir() if p.is_file() and p.name not in (".embedding.lock", "embeddings.manifest.json")]
        save(folder / "embeddings.manifest.json", {"schema_version": "ordinance-embeddings-v1", "input_identity": plan["identity"],
             "input_files": plan["document"]["input_files"], "output_files": {p.name: digest(p.read_bytes()) for p in sorted(files)},
             "answer_allowed": False})
        return {**expected["embedding_report.json"], "called": called, "output_folder": str(folder)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--plan-file", type=Path, help="Optional local prepared input JSON; no project data mutation")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.plan_file:
            require(not args.execute and not args.check_only, "plan_file_only_in_prepare_mode")
            save(args.plan_file, prepare(args.project))
        result = run(args.project, execute=args.execute, check_only=args.check_only, env_file=args.env_file)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("임베딩 중단: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
