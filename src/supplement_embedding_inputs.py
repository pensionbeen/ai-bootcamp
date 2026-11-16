"""Prepare preserved supplement blocks for retrieval-only passage embeddings.

This adapter reads the six existing, manifest-bound supplement outputs. It does
not rebuild the corpus, parse XML, collect sources, call an API, or write files.
An old promulgation date never makes a transitional rule automatically obsolete.
"""
import copy
from datetime import date
import hashlib
import json
from pathlib import Path


ASOF = "2026-10-01"
FOLDER = "data/processed/ordinances/2026-10-01/supplements"
MANIFEST = FOLDER + "/supplement.manifest.json"
EXPECTED_COUNTS = {
    "2054497": 25,
    "2147709": 45,
    "2171767": 29,
    "2171803": 15,
    "2171829": 35,
    "2171833": 55,
}
MAX_TEXT_UTF8_BYTES = 8000


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _safe(project, relative):
    relative_path = Path(relative)
    _require(not relative_path.is_absolute() and ".." not in relative_path.parts,
             "unsafe_supplement_input_path")
    path = project / relative_path
    _require(path.resolve(strict=True).is_relative_to(project), "supplement_input_outside_project")
    _require(not any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(project)),
             "symlink_supplement_input")
    return path


def _cutoff_date(value, label):
    _require(isinstance(value, str), label + "_not_string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(label + "_invalid") from None
    _require(parsed.isoformat() == value and parsed <= date.fromisoformat(ASOF),
             label + "_after_cutoff_or_noncanonical")


def prepare_supplements(project):
    """Return 204 unchanged retrieval texts with full source records and hashes."""
    project = Path(project).resolve(strict=True)
    manifest_bytes = _safe(project, MANIFEST).read_bytes()
    manifest = json.loads(manifest_bytes)
    _require(manifest.get("schema_version") == 1 and
             manifest.get("pipeline_version") == "ordinance-supplements-v1",
             "unsupported_supplement_manifest")
    _require(manifest.get("answer_allowed") is False and manifest.get("production_ready") is False,
             "unexpected_supplement_manifest_approval")
    expected_names = {seq + ".supplements.json" for seq in EXPECTED_COUNTS}
    _require({name for name in manifest["output_files"] if name.endswith(".supplements.json")} == expected_names,
             "supplement_file_scope_mismatch")
    inputs = {MANIFEST: _sha(manifest_bytes)}
    records = []
    seen = set()
    historical_count = 0
    mentions_count = 0
    largest = 0
    for seq, expected_count in sorted(EXPECTED_COUNTS.items()):
        filename = seq + ".supplements.json"
        relative = FOLDER + "/" + filename
        source_bytes = _safe(project, relative).read_bytes()
        source_hash = _sha(source_bytes)
        _require(source_hash == manifest["output_files"][filename],
                 "supplement_output_hash_mismatch:" + filename)
        inputs[relative] = source_hash
        document = json.loads(source_bytes)
        _require(document.get("schema_version") == 1 and
                 document.get("pipeline_version") == "ordinance-supplements-v1",
                 "unsupported_supplement_document:" + filename)
        _require(document.get("ordin_seq") == seq and document.get("as_of_date") == ASOF,
                 "supplement_document_scope_mismatch:" + filename)
        blocks = document["blocks"]
        _require(isinstance(blocks, list) and len(blocks) == expected_count,
                 "supplement_block_count_mismatch:" + filename)
        for ordinal, block in enumerate(blocks, 1):
            supplement_id = block["supplement_id"]
            _require(supplement_id == f"{seq}:supplement:{ordinal:04d}" and supplement_id not in seen,
                     "supplement_id_mismatch_or_duplicate")
            seen.add(supplement_id)
            _require(block["ordin_seq"] == seq and block["as_of_date"] == ASOF and
                     block["ordin_name"] == document["ordin_name"], "supplement_block_scope_mismatch")
            _cutoff_date(block["selected_version_effective_date"], "selected_version_effective_date")
            _cutoff_date(block["supplement_promulgation_date"], "supplement_promulgation_date")
            _require(type(block["is_current_amendment"]) is bool, "invalid_current_amendment_flag")
            _require(block["answer_allowed"] is False and block["applicability_status"] == "not_determined",
                     "unexpected_supplement_applicability_approval")
            raw_file = block["source_location"]["raw_file"]
            _require(raw_file == "data/raw/" + seq + ".xml" and
                     block["ordinance_xml_hash"] == manifest["input_files"].get(raw_file),
                     "supplement_raw_source_binding_mismatch")
            source_text = block["source_text"]
            _require(isinstance(source_text, str) and bool(source_text.strip()) and
                     _sha(source_text.encode("utf-8")) == block["source_text_hash"],
                     "supplement_source_text_hash_mismatch")
            text = block["retrieval_text"]
            # This verifies the stored representation, without constructing or
            # normalizing a replacement and without stripping its whitespace.
            _require(text == block["ordin_name"] + "\n" + source_text,
                     "supplement_retrieval_text_does_not_preserve_source")
            text_bytes = text.encode("utf-8")
            _require(0 < len(text_bytes) <= MAX_TEXT_UTF8_BYTES, "supplement_embedding_text_size_limit")
            analysis = block["analysis"]
            _require(analysis["signals_are_legal_classification"] is False and
                     type(analysis["scope_requires_review"]) is bool, "invalid_supplement_scope_metadata")
            for mention in analysis["mentions"]:
                span = mention["source_span"]
                start, end = span["start"], span["end"]
                _require(type(start) is int and type(end) is int and 0 <= start < end <= len(source_text),
                         "invalid_supplement_mention_span")
                _require(source_text[start:end] == mention["quote_text"], "supplement_mention_quote_mismatch")
                _require(mention["applicability_status"] == "not_determined",
                         "unexpected_supplement_mention_approval")
            records.append({
                "chunk_id": supplement_id,
                "kind": "supplement",
                "text": text,
                "text_sha256": _sha(text_bytes),
                "source_record": copy.deepcopy(block),
                "source_file": relative,
                "source_record_sha256": _sha(_canonical(block)),
                "required_context_ids": [supplement_id],
                "answer_allowed": False,
                "embedding_allowed": True,
                "embedding_scope": "retrieval_representation_only",
            })
            historical_count += not block["is_current_amendment"]
            mentions_count += len(analysis["mentions"])
            largest = max(largest, len(text_bytes))
    _require(len(records) == len(seen) == 204, "supplement_total_count_mismatch")
    return {
        "records": records,
        "input_files": dict(sorted(inputs.items())),
        "summary": {
            "schema_version": "supplement-embedding-inputs-v1",
            "as_of_date": ASOF,
            "record_count": len(records),
            "ordinance_count": len(EXPECTED_COUNTS),
            "ordinance_block_counts": dict(EXPECTED_COUNTS),
            "historical_block_count": historical_count,
            "current_amendment_block_count": len(records) - historical_count,
            "preserved_mention_count": mentions_count,
            "max_text_utf8_bytes": largest,
            "source_text_whitespace_preserved": True,
            "retrieval_text_reused_exactly": True,
            "historical_blocks_discarded": 0,
            "legal_applicability": "not_determined",
            "answer_allowed": False,
            "new_api_calls": 0,
            "embedding_performed": False,
            "scope_note": "Historical and current amendment flags describe source context; neither establishes applicability or obsolescence.",
        },
    }
