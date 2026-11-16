"""Offline condition-to-retrieval coverage; no legal applicability approval.

The alias rules match search_local_rag.retrieve exactly: actual chunk/source
identities, linked context identities, and DB-verified context origin identities.
No article-number parsing, historical target inference, API, or environment use.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from xml.etree import ElementTree

AS_OF = "2026-10-01"
SCHEMA_VERSION = "condition-retrieval-coverage-v1"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_project_path(project, relative):
    require(isinstance(relative, str) and bool(relative), "invalid_project_source_path")
    path = (project / relative).resolve()
    require(path.is_relative_to(project), "source_path_outside_project")
    return path


def _database(project):
    matches = sorted((project / "data/processed/rag" / AS_OF).glob("*/rag.sqlite3"))
    require(len(matches) == 1, "expected_one_local_rag_database")
    require(matches[0].is_file() and not matches[0].is_symlink(), "invalid_local_rag_database")
    return matches[0]


def _read_database(database):
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        metadata = {r["key"]: json.loads(r["value_json"]) for r in connection.execute("SELECT key,value_json FROM metadata")}
        require(metadata.get("as_of_date", AS_OF) == AS_OF, "database_asof_mismatch")
        chunks = []
        for row in connection.execute("SELECT chunk_id,kind,as_of_date,effective_date,source_json,is_deleted,answer_allowed FROM chunks ORDER BY chunk_id"):
            item = dict(row)
            require(item["as_of_date"] == AS_OF and item["answer_allowed"] == 0, "chunk_scope_or_approval_mismatch")
            item["source"] = json.loads(item.pop("source_json"))
            chunks.append(item)
        contexts = []
        for row in connection.execute("SELECT context_id,kind,source_json,text FROM contexts ORDER BY context_id"):
            item = dict(row)
            item["source"] = json.loads(item.pop("source_json"))
            contexts.append(item)
        links = [dict(row) for row in connection.execute("SELECT chunk_id,context_id,relation FROM chunk_context_links ORDER BY chunk_id,context_id,relation")]
        conditions = []
        for row in connection.execute("SELECT condition_id,source_unit_id,condition_json FROM reviewed_conditions ORDER BY condition_id"):
            item = dict(row)
            item["review"] = json.loads(item.pop("condition_json"))
            require(item["review"]["condition"]["condition_id"] == item["condition_id"], "condition_identity_mismatch")
            conditions.append(item)
        return chunks, contexts, links, conditions
    finally:
        connection.close()


def map_conditions(chunks, contexts, links, conditions):
    """Build complete associations with the current retrieval's exact-ID rules.

    This is a corpus-level reachability report, not similarity recall or a
    statement that any condition applies. Deleted primaries remain distinguished.
    """
    by_id = {row["chunk_id"]: row for row in chunks}
    by_context = {row["context_id"]: row for row in contexts}
    require(len(by_id) == len(chunks) and len(by_context) == len(contexts), "duplicate_corpus_identity")
    by_links = {cid: [] for cid in by_id}
    dangling_links = []
    for link in links:
        if link["chunk_id"] not in by_id:
            dangling_links.append(dict(link))
        else:
            by_links[link["chunk_id"]].append(link)
    catalog, aliases_by_id, context_aliases_by_id = {}, {}, {}
    for cid, row in by_id.items():
        source = row["source"]
        source_unit_id = source.get("source_unit_id")
        require(isinstance(source_unit_id, str), "missing_primary_source_unit_id")
        base = {cid, source_unit_id}
        aliases, context_aliases, missing_contexts = set(base), {}, set()
        linked_context_ids = set()
        for link in by_links[cid]:
            context_id = link["context_id"]
            linked_context_ids.add(context_id)
            context = by_context.get(context_id)
            if context is None:
                missing_contexts.add(context_id)
                continue
            values = {context_id}
            context_source = context["source"]
            if isinstance(context_source, dict) and isinstance(context_source.get("source_unit_id"), str):
                values.add(context_source["source_unit_id"])
            origin_id = context_source.get("origin_id") if isinstance(context_source, dict) else None
            if isinstance(origin_id, str) and origin_id in by_id:
                values.add(origin_id)
                values.add(by_id[origin_id]["source"]["source_unit_id"])
            aliases.update(values)
            context_aliases.setdefault(context_id, set()).update(values)
        missing_contexts.update(set(source.get("required_context_ids", [])) - linked_context_ids)
        dependencies = list(source.get("dependency_primary_ids", []))
        missing_dependencies = sorted(set(dependencies) - set(by_id))
        catalog[cid] = {"kind": row["kind"], "source_unit_id": source_unit_id,
                        "is_deleted": bool(row.get("is_deleted")), "effective_date": row.get("effective_date"),
                        "effective_date_meaning": "selected_ordinance_version" if row["kind"] == "supplement" else "source_ordinance_effective_date",
                        "required_context_ids": sorted(linked_context_ids), "dependency_primary_ids": dependencies,
                        "missing_required_context_ids": sorted(missing_contexts),
                        "missing_dependency_primary_ids": missing_dependencies,
                        "retrieval_structure_complete": not missing_contexts and not missing_dependencies,
                        "answer_allowed": False}
        aliases_by_id[cid] = aliases
        context_aliases_by_id[cid] = context_aliases
    # Structural failures in an explicit companion also invalidate its parent.
    incomplete = {cid for cid, row in catalog.items() if not row["retrieval_structure_complete"]}
    while True:
        expanded = incomplete | {cid for cid, row in catalog.items() if set(row["dependency_primary_ids"]) & incomplete}
        if expanded == incomplete:
            break
        incomplete = expanded
    for cid in incomplete:
        catalog[cid]["retrieval_structure_complete"] = False
    all_aliases = set().union(*aliases_by_id.values()) if by_id else set()
    default_aliases = set().union(*(aliases_by_id[cid] for cid, row in catalog.items()
                                   if not row["is_deleted"] and row["retrieval_structure_complete"])) if by_id else set()
    results = []
    seen_conditions = set()
    for saved in sorted(conditions, key=lambda x: x["condition_id"]):
        condition_id, source_unit_id = saved["condition_id"], saved["source_unit_id"]
        require(condition_id not in seen_conditions, "duplicate_condition_id")
        seen_conditions.add(condition_id)
        review = saved["review"]
        condition = review["condition"]
        require(condition.get("condition_id") == condition_id, "condition_identity_mismatch")
        targets = condition.get("current_target_ids", [])
        require(isinstance(targets, list) and all(isinstance(t, str) for t in targets), "invalid_current_target_ids")
        associations = []
        source_primary_ids, source_context_primary_ids, target_primary_ids = [], [], []
        for cid in sorted(by_id):
            aliases = aliases_by_id[cid]
            same_source = source_unit_id in aliases
            matched_targets = sorted(set(targets) & aliases)
            if not same_source and not matched_targets:
                continue
            base = {cid, catalog[cid]["source_unit_id"]}
            direct_source = source_unit_id in base
            if direct_source:
                source_primary_ids.append(cid)
            elif same_source:
                source_context_primary_ids.append(cid)
            if matched_targets:
                target_primary_ids.append(cid)
            matched_context_ids = sorted(context_id for context_id, values in context_aliases_by_id[cid].items()
                                         if source_unit_id in values or set(targets) & values)
            associations.append({"chunk_id": cid, "same_source_unit": same_source,
                                 "direct_original_source_unit": direct_source,
                                 "source_via_required_context": same_source and not direct_source,
                                 "matched_explicit_target_ids": matched_targets,
                                 "matching_context_ids": matched_context_ids,
                                 "required_context_ids": catalog[cid]["required_context_ids"],
                                 "is_deleted": catalog[cid]["is_deleted"],
                                 "retrieval_structure_complete": catalog[cid]["retrieval_structure_complete"],
                                 "applicability_established": False})
        default_ids = [r["chunk_id"] for r in associations if not r["is_deleted"] and r["retrieval_structure_complete"]]
        source = review.get("source", {})
        deleted_history = (not default_ids and bool(associations) and
                           all(row["is_deleted"] and row["retrieval_structure_complete"] for row in associations) and
                           condition.get("applies_as_of") == "historical_only")
        results.append({"condition_id": condition_id, "source_unit_id": source_unit_id,
                        "source_kind": source.get("kind"), "source_label": source.get("label"),
                        "source_ordinance_effective_date": source.get("effective_date"),
                        "source_supplement_promulgation_date": source.get("supplement_promulgation_date"),
                        "condition_kind": condition.get("kind"), "current_target_ids": list(targets),
                        "empty_current_targets": not targets,
                        "empty_target_meaning": "no_explicit_current_target_not_automatic_application" if not targets else None,
                        "exact_source_primary_ids": source_primary_ids,
                        "source_via_context_primary_ids": source_context_primary_ids,
                        "explicit_target_primary_ids": target_primary_ids,
                        "associated_primary_ids": [r["chunk_id"] for r in associations],
                        "default_retrievable_primary_ids": default_ids,
                        "associated_context_ids": sorted({context_id for r in associations for context_id in r["required_context_ids"]}),
                        "unmatched_source_unit": source_unit_id not in all_aliases,
                        "missing_explicit_target_ids": [target for target in targets if target not in all_aliases],
                        "non_default_target_ids": [target for target in targets if target in all_aliases and target not in default_aliases],
                        "coverage_status": "default_retrievable" if default_ids else
                        ("deleted_source_excluded_by_default" if deleted_history else
                         ("associated_but_not_default_retrievable" if associations else "no_retrieval_association")),
                        "required_facts": condition.get("required_facts", []),
                        "exceptions_and_scope": {"interpretation": condition.get("interpretation"),
                                                 "caution": condition.get("caution"),
                                                 "applies_as_of": condition.get("applies_as_of"),
                                                 "blockers": review.get("blockers", []),
                                                 "requirements": review.get("requirements")},
                        "quote_sha256": sha(condition.get("quote", "").encode("utf-8")),
                        "quote_characters": len(condition.get("quote", "")),
                        "evidence_binding": condition.get("evidence_binding", {}),
                        "associations": associations,
                        "relation_meaning": "candidate_evidence_ownership_or_explicit_target_only",
                        "historical_or_conditional_applicability": "not_established",
                        "applicability_established": False, "answer_allowed": False})
    return catalog, results, dangling_links


def _verify_quote(project, saved, source_cache):
    condition = saved["review"]["condition"]
    binding = condition.get("evidence_binding", {})
    quote = condition.get("quote")
    if not isinstance(quote, str) or not isinstance(binding, dict):
        return {"status": "missing_quote_or_binding"}
    relative = binding.get("project_file")
    try:
        source = safe_project_path(project, relative)
    except ValueError:
        return {"status": "invalid_source_path"}
    if relative not in source_cache:
        if not source.is_file():
            source_cache[relative] = {"status": "source_file_missing"}
        else:
            raw = source.read_bytes()
            try:
                source_cache[relative] = {"sha256": sha(raw), "root": ElementTree.fromstring(raw)}
            except ElementTree.ParseError:
                source_cache[relative] = {"sha256": sha(raw), "status": "invalid_source_xml"}
    original = source_cache[relative]
    if "status" in original:
        return {"status": original["status"]}
    if original["sha256"] != binding.get("file_sha256"):
        return {"status": "source_file_hash_mismatch"}
    if sha(quote.encode("utf-8")) != binding.get("quote_sha256"):
        return {"status": "condition_quote_hash_mismatch"}
    try:
        node = original["root"].find(binding.get("xml_path", ""))
    except (SyntaxError, TypeError):
        return {"status": "invalid_source_xml_path"}
    if node is None:
        return {"status": "source_xml_node_missing"}
    text = "".join(node.itertext())
    start, end = binding.get("start"), binding.get("end")
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text):
        return {"status": "invalid_quote_span"}
    return {"status": "exact_original_source_span" if text[start:end] == quote else "original_source_span_mismatch",
            "source_text_characters": len(text), "start": start, "end": end}


def prepare_coverage(project):
    project = Path(project).resolve(strict=True)
    database = _database(project)
    retrieval_code = project / "src/search_local_rag.py"
    require(retrieval_code.is_file(), "missing_retrieval_implementation")
    database_hash = file_sha(database)
    chunks, contexts, links, saved = _read_database(database)
    require(file_sha(database) == database_hash, "database_changed_during_coverage")
    catalog, conditions, dangling = map_conditions(chunks, contexts, links, saved)
    source_cache = {}
    by_condition = {item["condition_id"]: item for item in saved}
    for item in conditions:
        item["quotation_match"] = _verify_quote(project, by_condition[item["condition_id"]], source_cache)
    source_inputs = {"database": {"project_file": database.relative_to(project).as_posix(), "sha256": database_hash},
                     "retrieval_code_sha256": file_sha(retrieval_code), "coverage_code_sha256": file_sha(Path(__file__)),
                     "original_sources": {name: {"sha256": item.get("sha256"), "status": item.get("status", "read")}
                                          for name, item in sorted(source_cache.items())}}
    uncovered = [r["condition_id"] for r in conditions if not r["default_retrievable_primary_ids"]]
    deleted_history = [r["condition_id"] for r in conditions if r["coverage_status"] == "deleted_source_excluded_by_default"]
    active_uncovered = [identity for identity in uncovered if identity not in set(deleted_history)]
    missing_sources = [r["condition_id"] for r in conditions if r["unmatched_source_unit"]]
    missing_targets = {r["condition_id"]: r["missing_explicit_target_ids"] for r in conditions if r["missing_explicit_target_ids"]}
    bad_quotes = {r["condition_id"]: r["quotation_match"]["status"] for r in conditions
                  if r["quotation_match"]["status"] != "exact_original_source_span"}
    incomplete_primaries = [cid for cid, row in catalog.items() if not row["retrieval_structure_complete"]]
    summary = {"primary_count": len(chunks), "default_rankable_primary_count": sum(not r["is_deleted"] for r in catalog.values()),
               "context_count": len(contexts), "context_link_count": len(links), "condition_count": len(conditions),
               "conditions_with_default_retrieval_association": len(conditions) - len(uncovered),
               "conditions_without_default_retrieval_association": len(uncovered),
               "conditions_excluded_as_deleted_history": len(deleted_history),
               "active_conditions_without_default_retrieval_association": len(active_uncovered),
               "conditions_with_exact_source_primary": sum(bool(r["exact_source_primary_ids"]) for r in conditions),
               "conditions_with_source_via_context": sum(bool(r["source_via_context_primary_ids"]) for r in conditions),
               "conditions_with_explicit_target_association": sum(bool(r["explicit_target_primary_ids"]) for r in conditions),
               "conditions_with_empty_targets": sum(r["empty_current_targets"] for r in conditions),
               "conditions_with_unmatched_original_source_unit": len(missing_sources),
               "conditions_with_missing_explicit_target_ids": len(missing_targets),
               "missing_explicit_target_id_count": sum(len(ids) for ids in missing_targets.values()),
               "conditions_with_exact_original_quote": len(conditions) - len(bad_quotes),
               "conditions_with_quote_binding_issue": len(bad_quotes), "incomplete_primary_count": len(incomplete_primaries),
               "dangling_context_link_count": len(dangling), "new_api_calls": 0, "answer_allowed": False}
    material_gaps = {"conditions_without_default_retrieval_association": active_uncovered,
                     "conditions_with_unmatched_original_source_unit": missing_sources,
                     "missing_explicit_target_ids": missing_targets, "quotation_issues": bad_quotes,
                     "incomplete_primary_ids": incomplete_primaries, "dangling_context_links": dangling}
    detail = {"schema_version": SCHEMA_VERSION, "as_of_date": AS_OF, "source_inputs": source_inputs,
              "summary": summary, "material_gaps": material_gaps,
              "expected_exclusions": {"deleted_historical_condition_ids": deleted_history},
              "primary_catalog": catalog, "conditions": conditions,
              "scope": "corpus_identity_reachability_and_quote_binding_not_similarity_recall_or_legal_applicability",
              "empty_targets_are_not_gaps_by_themselves": True, "applicability_established": False,
              "answer_allowed": False, "new_api_calls": 0}
    identity = sha(encoded({"schema_version": SCHEMA_VERSION, "source_inputs": source_inputs}))
    detail["identity"] = identity
    raw = encoded(detail)
    manifest = {"schema_version": SCHEMA_VERSION, "identity": identity, "as_of_date": AS_OF,
                "source_inputs": source_inputs, "summary": summary,
                "output_files": {"coverage.json": sha(raw)}, "answer_allowed": False, "new_api_calls": 0}
    return detail, {"coverage.json": raw, "coverage.manifest.json": encoded(manifest)}


def run_coverage(project, check_only=False, output_dir=None):
    """Build or verify deterministic, versioned coverage artifacts without APIs.

    ``check_only`` requires existing matching artifacts and never creates paths.
    Unchanged outputs are reused without rewriting their modification times.
    """
    require(type(check_only) is bool, "check_only_must_be_boolean")
    project = Path(project).resolve(strict=True)
    detail, outputs = prepare_coverage(project)
    base = Path(output_dir).resolve() if output_dir is not None else project / "data/processed/local_rag" / AS_OF / "condition_coverage"
    destination = base / detail["identity"][:20]
    written = 0
    for name, raw in outputs.items():
        path = destination / name
        require(not path.is_symlink(), "coverage_output_symlink")
        if path.exists():
            require(path.is_file() and path.read_bytes() == raw, "coverage_output_content_mismatch:" + name)
        elif check_only:
            raise ValueError("condition_coverage_missing:" + name)
        else:
            destination.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            written += 1
    return {"schema_version": SCHEMA_VERSION, "identity": detail["identity"], "check_only": check_only,
            "summary": detail["summary"], "material_gaps": detail["material_gaps"],
            "expected_exclusions": detail["expected_exclusions"],
            "detail_artifact": str(destination / "coverage.json"), "manifest_artifact": str(destination / "coverage.manifest.json"),
            "written_file_count": written, "new_api_calls": 0, "answer_allowed": False,
            "applicability_established": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run_coverage(args.project, args.check_only, args.output_dir), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print("조건 연결 검사 중단: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
