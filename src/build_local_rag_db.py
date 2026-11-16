"""Load existing vectors and source-bound context into a portable local SQLite DB."""
import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

ASOF = "2026-10-01"
EMBEDDINGS_FOLDER = "data/processed/ordinances/2026-10-01/embeddings/1bbd861c1f73dc181dee"
REVIEW_FOLDER = "data/processed/reviews/asof_2026-10-01"
MODEL = "solar-embedding-2-passage"
QUERY_MODEL = "solar-embedding-2-query"
DIMENSION = 1024
SCHEMA_VERSION = "local-ordinance-rag-sqlite-v1"
SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY NOT NULL, value_json TEXT NOT NULL);
CREATE TABLE chunks (
 chunk_id TEXT PRIMARY KEY NOT NULL, kind TEXT NOT NULL, ordin_seq TEXT NOT NULL,
 as_of_date TEXT NOT NULL, effective_date TEXT NOT NULL, title TEXT NOT NULL,
 text TEXT NOT NULL, text_sha256 TEXT NOT NULL, vector_json TEXT NOT NULL,
 source_json TEXT NOT NULL, is_deleted INTEGER NOT NULL CHECK(is_deleted IN (0,1)),
 answer_allowed INTEGER NOT NULL CHECK(answer_allowed=0));
CREATE TABLE contexts (
 context_id TEXT PRIMARY KEY NOT NULL, kind TEXT NOT NULL, source_json TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE chunk_context_links (
 chunk_id TEXT NOT NULL, context_id TEXT NOT NULL, relation TEXT NOT NULL,
 PRIMARY KEY(chunk_id,context_id,relation),
 FOREIGN KEY(chunk_id) REFERENCES chunks(chunk_id), FOREIGN KEY(context_id) REFERENCES contexts(context_id));
CREATE TABLE reviewed_conditions (
 condition_id TEXT PRIMARY KEY NOT NULL, source_unit_id TEXT NOT NULL, condition_json TEXT NOT NULL);
CREATE TABLE external_material_tasks (reference_id TEXT PRIMARY KEY NOT NULL, task_json TEXT NOT NULL);
CREATE INDEX chunks_scope ON chunks(as_of_date,kind,is_deleted);
CREATE INDEX conditions_source ON reviewed_conditions(source_unit_id);
"""


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _module(path):
    spec = importlib.util.spec_from_file_location("_local_attachment_catalog", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _vector(value):
    require(isinstance(value, list) and len(value) == DIMENSION, "invalid_vector_dimension")
    require(all(type(x) in (float, int) and math.isfinite(x) for x in value), "invalid_vector_number")
    require(sum(x*x for x in value) > 0, "zero_vector")


def prepare(project):
    project = Path(project).resolve(strict=True)
    inputs, loaded = {}, {}

    def load(relative, expected=None):
        path = project / relative
        require(path.resolve(strict=True).is_relative_to(project), "input_outside_project:" + relative)
        require(not any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(project)),
                "input_symlink:" + relative)
        if relative not in loaded:
            content = path.read_bytes()
            inputs[relative] = sha(content)
            loaded[relative] = json.loads(content)
        require(expected is None or inputs[relative] == expected, "input_hash_mismatch:" + relative)
        return loaded[relative]

    manifest = load(EMBEDDINGS_FOLDER + "/embeddings.manifest.json")
    new_inputs = load(EMBEDDINGS_FOLDER + "/inputs.json", manifest["output_files"]["inputs.json"])
    vectors = load(EMBEDDINGS_FOLDER + "/embeddings.json", manifest["output_files"]["embeddings.json"])
    require(vectors["input_identity"] == manifest["input_identity"], "embedding_input_identity_mismatch")
    for document in (new_inputs, vectors):
        require((document["as_of_date"], document["model"], document["query_model"], document["dimension"])
                == (ASOF, MODEL, QUERY_MODEL, DIMENSION), "embedding_scope_mismatch")
        require(document["answer_allowed"] is False, "unexpected_embedding_approval")
    source_records = {row["chunk_id"]: row for row in new_inputs["records"]}
    by_vector = {row["chunk_id"]: row for row in vectors["vectors"]}
    require(len(source_records) == len(new_inputs["records"]) == len(by_vector) == len(vectors["vectors"]) == 717
            and set(source_records) == set(by_vector), "new_vector_identity_mismatch")

    engine_path = project / "src/search_combined_attachments.py"
    inputs["src/search_combined_attachments.py"] = sha(engine_path.read_bytes())
    engine = _module(engine_path)
    snapshot = load(engine.SOURCE_SNAPSHOT, engine.SOURCE_SNAPSHOT_SHA256)
    attachment_file = engine.REL + "/combined_embeddings.json"
    external_file = engine.REL + "/external_materials_required.json"
    attachment_data = load(attachment_file, snapshot["project_inputs"][attachment_file])
    external_data = load(external_file, snapshot["project_inputs"][external_file])
    require((attachment_data["as_of_date"], attachment_data["model"], attachment_data["query_model"],
             attachment_data["dimension"]) == (ASOF, MODEL, QUERY_MODEL, DIMENSION), "attachment_scope_mismatch")
    tasks = {row["reference_id"]: row for row in external_data["tasks"]}
    require(len(tasks) == len(external_data["tasks"]) == 23, "external_task_identity_mismatch")
    require(all(row["target_content_verified"] is False for row in tasks.values()), "external_task_resolved_unexpectedly")
    catalog = engine.build_catalog(attachment_data["records"], tasks)

    chunks, contexts, links = [], {}, set()

    def context(context_id, kind, source, text):
        require(isinstance(text, str) and bool(text.strip()), "empty_context:" + context_id)
        row = {"context_id": context_id, "kind": kind, "source_json": packed(source), "text": text}
        require(context_id not in contexts or contexts[context_id] == row, "context_identity_conflict:" + context_id)
        contexts[context_id] = row

    def chunk(chunk_id, kind, source, text, text_sha, vector, source_wrapper, title, deleted=False):
        require(source["as_of_date"] == ASOF, "chunk_asof_mismatch")
        effective = source.get("effective_date", source.get("selected_version_effective_date"))
        require(isinstance(effective, str) and effective <= ASOF, "chunk_effective_date_mismatch")
        require(sha(text.encode("utf-8")) == text_sha, "chunk_text_hash_mismatch:" + chunk_id)
        _vector(vector)
        chunks.append({"chunk_id": chunk_id, "kind": kind, "ordin_seq": source["ordin_seq"],
                       "as_of_date": ASOF, "effective_date": effective, "title": title,
                       "text": text, "text_sha256": text_sha, "vector_json": packed(vector),
                       "source_json": packed(source_wrapper), "is_deleted": int(deleted), "answer_allowed": 0})

    parent_cache = {}
    for chunk_id, record in sorted(source_records.items()):
        source = record["source_record"]
        require(sha(raw(source)) == record["source_record_sha256"], "source_record_hash_mismatch:" + chunk_id)
        require(record["answer_allowed"] is False and record["embedding_allowed"] is True, "invalid_input_permissions")
        require(by_vector[chunk_id]["text_sha256"] == record["text_sha256"], "vector_text_binding_mismatch")
        kind = record["kind"]
        if kind == "body":
            unit_id = source["parent_id"]
            parent_file = record["parent_context"]["source_file"]
            if parent_file not in parent_cache:
                document = load(parent_file, manifest["input_files"][parent_file])
                parent_cache[parent_file] = {row["article_id"]: row for row in document["articles"]}
                for parent_id, parent in parent_cache[parent_file].items():
                    require(sha(parent["source_text"].encode("utf-8")) == parent["source_text_hash"], "parent_text_hash_mismatch")
                    context(parent_id, "article", {"source_file": parent_file, "source_record": parent}, parent["source_text"])
            require(unit_id in parent_cache[parent_file], "missing_parent_context")
            for context_id in record["required_context_ids"]:
                require(context_id in parent_cache[parent_file], "unknown_parent_reference")
                links.add((chunk_id, context_id, "required_parent"))
            title = source["label"] + " " + source["title"]
        elif kind == "supplement":
            unit_id = source["supplement_id"]
            require(unit_id == chunk_id, "supplement_identity_mismatch")
            require(sha(source["source_text"].encode("utf-8")) == source["source_text_hash"], "supplement_text_hash_mismatch")
            context(unit_id, "supplement", {"source_file": record["source_file"], "source_record": source}, source["source_text"])
            require(record["required_context_ids"] == [unit_id], "supplement_context_mismatch")
            links.add((chunk_id, unit_id, "required_supplement"))
            title = source["ordin_name"] + " " + source["title"]
        else:
            raise ValueError("unknown_new_chunk_kind")
        wrapper = {"source_unit_id": unit_id, "source_record": source, "input_record": record,
                   "required_context_ids": record["required_context_ids"], "dependency_primary_ids": [],
                   "external_reference_ids": [], "answer_allowed": False}
        chunk(chunk_id, kind, source, record["text"], record["text_sha256"], by_vector[chunk_id]["embedding"],
              wrapper, title, record.get("is_deleted", False))

    for context_id, record in sorted(catalog["contexts"].items()):
        context(context_id, record["kind"], record, record["text"])
    for record in sorted(attachment_data["records"], key=lambda row: row["chunk_id"]):
        chunk_id = record["chunk_id"]
        original = {key: copy.deepcopy(value) for key, value in record.items() if key != "embedding"}
        catalog_record = catalog["records"][chunk_id]
        attachment_id = record.get("attachment_id") or ":".join(chunk_id.split(":")[:2])
        require(chunk_id.startswith(attachment_id + ":"), "attachment_source_identity_mismatch")
        ordin_seq, file_id = attachment_id.split(":")
        require(record["source"]["ordin_seq"] == ordin_seq, "attachment_source_ordinance_mismatch")
        unit_id = ordin_seq + ":attachment:" + file_id
        wrapper = {"source_unit_id": unit_id, "source_record": original, "catalog_record": catalog_record,
                   "required_context_ids": catalog_record["required_context_ids"],
                   "dependency_primary_ids": catalog_record["dependency_primary_ids"],
                   "external_reference_ids": record["external_reference_ids"], "answer_allowed": False}
        chunk(chunk_id, "attachment", record["source"], record["text"], record["text_sha256"],
              record["embedding"], wrapper, record["text"].splitlines()[0])
        for context_id in catalog_record["required_context_ids"]:
            links.add((chunk_id, context_id, catalog["contexts"][context_id]["kind"]))

    review_manifest = load(REVIEW_FOLDER + "/review.manifest.json")
    reviewed = load(REVIEW_FOLDER + "/reviewed_conditions.json", review_manifest["output_files"]["reviewed_conditions.json"])
    requirements = load(REVIEW_FOLDER + "/requirements.json", review_manifest["output_files"]["requirements.json"])
    by_requirement = {row["unit_id"]: row for row in requirements["requirements"]}
    conditions = []
    for unit in reviewed["units"]:
        require(unit["answer_allowed"] is False and unit["source"]["as_of_date"] == ASOF, "review_scope_mismatch")
        for condition in unit["conditions"]:
            wrapper = {"condition": condition, "source": unit["source"], "review_outcome": unit["outcome"],
                       "review_summary": unit["summary"], "blockers": unit["blockers"],
                       "requirements": by_requirement.get(unit["unit_id"]), "answer_allowed": False,
                       "applicability_established": False}
            conditions.append({"condition_id": condition["condition_id"], "source_unit_id": unit["unit_id"],
                               "condition_json": packed(wrapper)})
    require(len(conditions) == len({row["condition_id"] for row in conditions}) == 434, "condition_count_mismatch")
    require(len(chunks) == len({row["chunk_id"] for row in chunks}) == 810, "chunk_count_mismatch")
    require(sum(row["is_deleted"] for row in chunks) == 6, "deleted_count_mismatch")
    require(sum(row["kind"] == "body" for row in chunks) == 513
            and sum(row["kind"] == "supplement" for row in chunks) == 204
            and sum(row["kind"] == "attachment" for row in chunks) == 93, "kind_count_mismatch")
    ids = {row["chunk_id"] for row in chunks}
    require(all(cid in ids and ctx in contexts for cid, ctx, _ in links), "dangling_context_link")
    require(all(set(json.loads(row["source_json"])["dependency_primary_ids"]) <= ids for row in chunks), "dangling_primary_dependency")
    identity = sha(raw({"schema_version": SCHEMA_VERSION, "schema": SCHEMA, "input_files": inputs,
                        "loader_sha256": sha(Path(__file__).read_bytes())}))
    summary = {"chunk_count": 810, "body_count": 513, "supplement_count": 204, "attachment_count": 93,
               "deleted_count": 6, "context_count": len(contexts), "context_link_count": len(links),
               "condition_count": 434, "external_task_count": 23, "dimension": DIMENSION,
               "answer_allowed": False, "conditions_are_stored_without_inferred_applicability": True}
    return {"identity": identity, "input_files": dict(sorted(inputs.items())), "summary": summary,
            "chunks": sorted(chunks, key=lambda row: row["chunk_id"]),
            "contexts": [contexts[key] for key in sorted(contexts)],
            "chunk_context_links": [{"chunk_id": cid, "context_id": ctx, "relation": relation} for cid, ctx, relation in sorted(links)],
            "reviewed_conditions": sorted(conditions, key=lambda row: row["condition_id"]),
            "external_material_tasks": [{"reference_id": key, "task_json": packed(tasks[key])} for key in sorted(tasks)]}


def _validate_db(connection, plan):
    require(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "database_integrity_failed")
    require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "database_reference_failed")
    for table in ("chunks", "contexts", "chunk_context_links", "reviewed_conditions", "external_material_tasks"):
        require(connection.execute("SELECT count(*) FROM " + table).fetchone()[0] == len(plan[table]), "database_count_mismatch:" + table)
    require(connection.execute("SELECT sum(is_deleted),sum(answer_allowed) FROM chunks").fetchone() == (6, 0), "database_flags_mismatch")


def build(project, output=None):
    project = Path(project).resolve(strict=True)
    plan = prepare(project)
    output = Path(output).resolve() if output else project / "data/processed/rag" / ASOF / plan["identity"][:20] / "rag.sqlite3"
    manifest_path = output.with_suffix(".manifest.json")
    if output.exists() or manifest_path.exists():
        require(output.is_file() and manifest_path.is_file(), "incomplete_database_output")
        saved = json.loads(manifest_path.read_bytes())
        require(saved["identity"] == plan["identity"] and saved["input_files"] == plan["input_files"], "database_identity_conflict")
        require(sha(output.read_bytes()) == saved["database_sha256"], "database_hash_mismatch")
        return {**saved, "database_file": str(output), "manifest_file": str(manifest_path), "reused": True}
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".rag-", suffix=".sqlite3", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(SCHEMA)
            with connection:
                metadata = {"schema_version": SCHEMA_VERSION, "identity": plan["identity"], "as_of_date": ASOF,
                            "model": MODEL, "query_model": QUERY_MODEL, "dimension": DIMENSION,
                            "answer_allowed": False, "summary": plan["summary"], "input_files": plan["input_files"]}
                connection.executemany("INSERT INTO metadata(key,value_json) VALUES (?,?)", [(key, packed(value)) for key, value in sorted(metadata.items())])
                for table in ("chunks", "contexts", "chunk_context_links", "reviewed_conditions", "external_material_tasks"):
                    columns = list(plan[table][0])
                    statement = "INSERT INTO " + table + "(" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")"
                    connection.executemany(statement, [[row[key] for key in columns] for row in plan[table]])
                _validate_db(connection, plan)
        finally:
            connection.close()
        manifest = {"schema_version": SCHEMA_VERSION, "identity": plan["identity"], "as_of_date": ASOF,
                    "input_files": plan["input_files"], "summary": plan["summary"],
                    "database_sha256": sha(temporary.read_bytes()), "answer_allowed": False}
        require(not output.exists() and not manifest_path.exists(), "database_output_race")
        os.replace(temporary, output)
        with manifest_path.open("xb") as handle:
            handle.write(raw(manifest))
        return {**manifest, "database_file": str(output), "manifest_file": str(manifest_path), "reused": False}
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = build(args.project, args.output)
    print(json.dumps({key: result[key] for key in ("database_file", "manifest_file", "summary", "reused")}, ensure_ascii=False, indent=2))
