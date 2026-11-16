"""Question embedding -> read-only local retrieval -> one Solar generation and code checks.

All generated text remains an unapproved draft for the fixed 2026-10-01 scope.
"""
import argparse
import importlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

from local_rag_client import Client, QUERY_MODEL, digest, raw, read, require, save

ASOF = "2026-10-01"
DEFAULT_DATABASE = "data/processed/rag/2026-10-01/71ce40874477c9a4bc9d/rag.sqlite3"
OUTPUT_AREA = "data/processed/local_rag/2026-10-01"


def database_identity(database):
    database = Path(database).resolve(strict=True)
    manifest_path = database.with_suffix(".manifest.json")
    manifest = read(manifest_path)
    database_hash = digest(database.read_bytes())
    require(manifest.get("database_sha256") == database_hash, "database_manifest_hash_mismatch")
    require(manifest.get("as_of_date") == ASOF and manifest.get("answer_allowed") is False,
            "database_manifest_scope_mismatch")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key,value_json FROM metadata")}
    finally:
        connection.close()
    require(metadata.get("as_of_date") == ASOF and metadata.get("dimension") == 1024 and
            metadata.get("model") == "solar-embedding-2-passage" and metadata.get("query_model") == QUERY_MODEL,
            "database_embedding_scope_mismatch")
    require(metadata.get("identity") == manifest.get("identity") and metadata.get("answer_allowed") is False,
            "database_metadata_identity_mismatch")
    return {"database": str(database), "database_sha256": database_hash,
            "manifest_sha256": digest(manifest_path.read_bytes()), "identity": manifest["identity"],
            "as_of_date": ASOF, "dimension": 1024, "model": metadata["model"],
            "query_model": QUERY_MODEL, "summary": metadata.get("summary", {})}


def query_vector(body):
    require(isinstance(body, dict) and body.get("model") == QUERY_MODEL, "query_embedding_model_mismatch")
    data = body.get("data")
    require(isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and
            type(data[0].get("index")) is int and data[0]["index"] == 0, "query_embedding_index_mismatch")
    vector = data[0].get("embedding")
    require(isinstance(vector, list) and len(vector) == 1024 and
            all(type(x) in (int, float) and math.isfinite(x) for x in vector), "invalid_query_embedding")
    require(0 < math.hypot(*vector) < float("inf"), "invalid_query_embedding_norm")
    return vector


def code_identity(modules):
    sources = {Path(module.__file__).name: digest(Path(module.__file__).read_bytes()) for module in modules}
    sources[Path(__file__).name] = digest(Path(__file__).read_bytes())
    return {"files": sources, "sha256": digest(raw(sources))}


def run_question(project, question, db_path=None, check_only=False, env_file=None, client_options=None):
    # `client_options` (service only) are transport options passed to Client
    # (parallel, deadline_seconds, gate); never part of the question identity.
    # Question start for the provision-check deadline: before database
    # identity, embedding and retrieval (service queueing/preflight excluded).
    started = time.monotonic()
    client, folder = None, None
    try:
        project = Path(project).resolve(strict=True)
        require(isinstance(question, str) and bool(question.strip()), "question_required")
        require(len(question.encode("utf-8")) <= 16000, "question_too_large")
        question = question.strip()
        database = Path(db_path) if db_path else project / DEFAULT_DATABASE
        if not database.is_absolute():
            database = project / database
        db = database_identity(database)
        retrieval_module = importlib.import_module("search_local_rag")
        answer_module = importlib.import_module("local_rag_answer")
        client_module = importlib.import_module("local_rag_client")
        public_module = importlib.import_module("public_rag_response")
        # Not called here, but they decide what the answer publishes (numeric checks,
        # releasable check issues): a change to either must not reuse a saved audit.
        numeric_module = importlib.import_module("rag_numeric_amounts")
        release_module = importlib.import_module("release_policy")
        code = code_identity([retrieval_module, answer_module, client_module, public_module,
                              numeric_module, release_module])
        identity = {"question": question, "database": db, "code": code,
                    "schema_version": "local-rag-single-question-v1", "as_of_date": ASOF,
                    "answer_allowed": False}
        run_hash = digest(raw(identity))
        folder = (project / OUTPUT_AREA / db["database_sha256"][:20] /
                  code["sha256"][:20] / digest(question.encode("utf-8"))[:20]).resolve()
        require(folder.is_relative_to(project), "output_outside_project")
        audit_path = folder / "audit.json"
        manifest_path = folder / "audit.manifest.json"
        paths = {"output_folder": str(folder), "audit": str(audit_path), "manifest": str(manifest_path)}
        if audit_path.exists() or manifest_path.exists():
            require(audit_path.exists() and manifest_path.exists(), "incomplete_saved_question")
            manifest = read(manifest_path)
            require(manifest.get("run_identity") == run_hash and
                    manifest.get("audit_sha256") == digest(audit_path.read_bytes()), "saved_audit_hash_mismatch")
            audit = read(audit_path)
            require(audit.get("answer_allowed") is False and audit.get("run_identity") == run_hash,
                    "invalid_saved_audit")
            return {**audit, "called": 0, "cached": True, "check_only": check_only, "output_paths": paths}
        if check_only:
            return {"status": "check_only", "database": db, "code": code,
                    "question": question, "run_identity": run_hash, "called": 0,
                    "cache_complete": False, "answer_allowed": False,
                    "final_answer_generated": False, "output_paths": paths}
        client = Client(project, env_file=env_file, **(client_options or {}))
        # Serialize the whole question as well as individual provider calls.
        from request_lock import request_lock
        with request_lock(folder / ".question.lock"):
            # Another process may have completed this question while we waited.
            if audit_path.exists() or manifest_path.exists():
                return run_question(project, question, db_path=database, check_only=True, env_file=env_file)
            save(folder / "input.json", {**identity, "run_identity": run_hash})
            body, embedding_meta = client.request("embedding", {"model": QUERY_MODEL, "input": [question]})
            vector = query_vector(body)
            retrieval = retrieval_module.retrieve(database, vector, top_k=8, include_deleted=False, question=question)
            retrieval["expansion_attempted"] = False
            save(folder / "retrieval-top8.json", retrieval)
            result = answer_module.assess_and_answer(question, retrieval, client, started=started)
            require(isinstance(result, dict), "answer_module_invalid_result")
            # A draft stopped only by a skipped HTTP 429 wait wrote nothing at the provider;
            # saving it would pin processing_error for this identity for good.
            require(not (result.get("decision") == "processing_error"
                         and "draft" in getattr(client, "rate_limit_skipped", ())),
                    "draft_rate_limited_retry_later")
            result = {**result, "answer_allowed": False, "final_answer_generated": False,
                      "output_label": "검증 대상 초안 — 최종 법률 답변으로 승인되지 않음"}
            require(result.get("request_more_evidence") is not True, "runtime_requested_forbidden_regeneration")
            audit = {"schema_version": "local-rag-single-question-v1", "status": "completed",
                     "question": question, "as_of_date": ASOF, "database": db, "code": code,
                     "run_identity": run_hash, "result": result, "called": client.called,
                     "calls": client.calls, "embedding_call": embedding_meta,
                     "retrieval_top_k": retrieval["top_k"], "answer_allowed": False,
                     "final_answer_generated": False, "output_paths": paths}
            save(audit_path, audit)
            save(manifest_path, {"run_identity": run_hash, "audit_sha256": digest(audit_path.read_bytes()),
                                 "answer_allowed": False})
            return audit
    except Exception as exc:
        # Provider exception messages are excluded to prevent credential leakage.
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        return {"status": "processing_error", "error_type": type(exc).__name__, "error": message,
                "called": client.called if client else 0, "answer_allowed": False,
                "final_answer_generated": False, "legal_absence_established": False,
                "output_folder": str(folder) if folder else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--question", required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--env", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    audit = run_question(args.project, args.question, args.db, args.check_only, args.env)
    print(json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if audit["status"] == "processing_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
