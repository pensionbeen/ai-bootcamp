"""Prepare existing body representations without collecting or parsing documents."""
import copy
import hashlib
import json
from pathlib import Path

ASOF = "2026-10-01"
FOLDER = "data/processed/ordinances/2026-10-01"
ORDINANCE_IDS = ("2054497", "2147709", "2171767", "2171803", "2171829", "2171833")
MAX_INPUT_BYTES = 8000


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _record_sha(value):
    return _sha((json.dumps(value, ensure_ascii=False, sort_keys=True,
                            indent=2, allow_nan=False) + "\n").encode("utf-8"))


def _read(project, relative, inputs, expected=None):
    path = project / relative
    _require(path.resolve(strict=True).is_relative_to(project), "body_input_outside_project:" + relative)
    _require(not any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(project)),
             "body_input_symlink:" + relative)
    raw = path.read_bytes()
    actual = _sha(raw)
    _require(expected is None or actual == expected, "body_input_hash_mismatch:" + relative)
    inputs[relative] = actual
    return raw


def prepare_body(project):
    """Return 513 deterministic records, with deleted provisions visibly marked.

    Only consumed chunk/parent JSON files and their original XML/metadata hashes
    are checked. The existing body pipeline is not re-run. Embedding permission
    covers the representation for retrieval; answer approval remains false.
    """
    project = Path(project).resolve(strict=True)
    inputs = {}
    manifest = json.loads(_read(project, FOLDER + "/corpus.manifest.json", inputs))
    _require(manifest["as_of_date"] == ASOF and manifest["chunk_count"] == 513
             and manifest["article_count"] == 184, "body_manifest_scope_mismatch")
    records = []
    article_ids = set()
    for ordin_seq in ORDINANCE_IDS:
        raw_file = "data/raw/" + ordin_seq + ".xml"
        metadata_file = "data/raw/" + ordin_seq + ".metadata.json"
        for relative in (raw_file, metadata_file):
            _read(project, relative, inputs, manifest["input_files"][relative])
        chunks_name = ordin_seq + ".chunks.json"
        parents_name = ordin_seq + ".articles.json"
        source_file = FOLDER + "/" + chunks_name
        parent_file = FOLDER + "/" + parents_name
        chunks = json.loads(_read(project, source_file, inputs, manifest["output_files"][chunks_name]))
        parent_document = json.loads(_read(project, parent_file, inputs, manifest["output_files"][parents_name]))
        parents = {row["article_id"]: row for row in parent_document["articles"]}
        _require(len(parents) == len(parent_document["articles"]), "duplicate_body_parent")
        _require(not article_ids.intersection(parents), "duplicate_body_parent_across_files")
        article_ids.update(parents)
        for source in chunks:
            _require(source["ordin_seq"] == ordin_seq and source["as_of_date"] == ASOF
                     and source["effective_date"] <= ASOF, "body_source_scope_mismatch")
            _require(source["content_hash"] == inputs[raw_file]
                     and source["metadata_content_hash"] == inputs[metadata_file], "body_source_binding_mismatch")
            _require(source["answer_allowed"] is False, "unexpected_body_answer_approval")
            _require(type(source["is_deleted"]) is bool, "invalid_body_deleted_flag")
            text = source["embedding_text"]
            _require(isinstance(text, str) and bool(text.strip()), "empty_body_embedding_text")
            _require(len(text.encode("utf-8")) <= MAX_INPUT_BYTES, "body_embedding_text_too_long")
            parent = parents.get(source["parent_id"])
            _require(parent is not None and source["chunk_id"] in parent["chunk_ids"], "body_parent_link_mismatch")
            _require(source["parent_id"] in source["required_context_ids"]
                     and set(source["required_context_ids"]) <= set(parents), "body_required_parent_missing")
            _require(parent["source_location"]["raw_file"] == raw_file, "body_parent_source_mismatch")
            records.append({
                "chunk_id": source["chunk_id"], "kind": "body", "text": text,
                "text_sha256": _sha(text.encode("utf-8")), "source_record": copy.deepcopy(source),
                "source_file": source_file, "source_record_sha256": _record_sha(source),
                "source_record_hash_encoding": "json_utf8_sorted_keys_indent2_trailing_lf",
                "required_context_ids": copy.deepcopy(source["required_context_ids"]),
                "parent_context": {"source_file": parent_file, "article_id": source["parent_id"],
                                   "source_text_sha256": parent["source_text_hash"],
                                   "source_location": copy.deepcopy(parent["source_location"])},
                "is_deleted": source["is_deleted"], "answer_allowed": False,
                "embedding_allowed": True, "embedding_scope": "retrieval_representation_only",
                "applicability_status": "not_determined",
                "retrieval_use": "deleted_provision_reference_only" if source["is_deleted"]
                                 else "candidate_requires_parent_context",
            })
    records.sort(key=lambda record: record["chunk_id"])
    _require(len(records) == len({record["chunk_id"] for record in records}) == 513,
             "body_record_count_or_identity_mismatch")
    _require(len(article_ids) == 184, "body_parent_count_mismatch")
    deleted_count = sum(record["is_deleted"] for record in records)
    _require(deleted_count == 6, "body_deleted_count_mismatch")
    return {"records": records, "input_files": dict(sorted(inputs.items())),
            "summary": {"as_of_date": ASOF, "body_record_count": len(records),
                        "body_article_count": len(article_ids), "deleted_record_count": deleted_count,
                        "active_record_count": len(records) - deleted_count,
                        "max_input_utf8_bytes": max(len(record["text"].encode("utf-8")) for record in records),
                        "total_input_utf8_bytes": sum(len(record["text"].encode("utf-8")) for record in records),
                        "source_file_count": len(inputs), "embedding_scope": "retrieval_representation_only",
                        "answer_allowed": False, "new_embeddings_created": 0}}
