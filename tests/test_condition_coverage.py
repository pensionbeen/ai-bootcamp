"""Focused offline coverage behavior, using a small real SQLite fixture."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from condition_coverage import AS_OF, _verify_quote, encoded, map_conditions, run_coverage, sha


def corpus():
    chunks = [{"chunk_id": "law:article:all", "kind": "body", "as_of_date": AS_OF, "effective_date": "2026-07-13",
               "is_deleted": 0, "answer_allowed": 0, "source": {"source_unit_id": "law:article",
               "required_context_ids": ["law:article"], "dependency_primary_ids": []}}]
    contexts = [{"context_id": "law:article", "kind": "article", "text": "규칙 30명 이상", "source": {}}]
    links = [{"chunk_id": "law:article:all", "context_id": "law:article", "relation": "required_parent"}]
    condition = {"condition_id": "law:condition:1", "kind": "application", "quote": "규칙 30명 이상",
                 "current_target_ids": [], "required_facts": ["인원"], "applies_as_of": "conditional",
                 "interpretation": "30명 이상인 경우에 한한다.", "caution": "개별 적용 미확정", "evidence_binding": {}}
    conditions = [{"condition_id": condition["condition_id"], "source_unit_id": "law:article",
                   "review": {"condition": condition, "source": {"kind": "article", "label": "제1조",
                   "effective_date": "2026-07-13"}, "blockers": []}}]
    return chunks, contexts, links, conditions


def project_fixture(project):
    chunks, contexts, links, conditions = corpus()
    raw = "<root><body><text>규칙 30명 이상</text></body></root>".encode("utf-8")
    path = project / "data/raw/test.xml"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    quote = conditions[0]["review"]["condition"]["quote"]
    conditions[0]["review"]["condition"]["evidence_binding"] = {
        "project_file": "data/raw/test.xml", "file_sha256": sha(raw), "quote_sha256": sha(quote.encode()),
        "xml_path": "./body/text", "start": 0, "end": len(quote)}
    code = project / "src/search_local_rag.py"
    code.parent.mkdir()
    code.write_text("# fixture retrieval implementation\n", encoding="utf-8")
    database = project / "data/processed/rag" / AS_OF / "fixture/rag.sqlite3"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value_json TEXT);
        CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,kind TEXT,as_of_date TEXT,effective_date TEXT,
                            source_json TEXT,is_deleted INTEGER,answer_allowed INTEGER);
        CREATE TABLE contexts(context_id TEXT PRIMARY KEY,kind TEXT,source_json TEXT,text TEXT);
        CREATE TABLE chunk_context_links(chunk_id TEXT,context_id TEXT,relation TEXT);
        CREATE TABLE reviewed_conditions(condition_id TEXT PRIMARY KEY,source_unit_id TEXT,condition_json TEXT);
    """)
    connection.execute("INSERT INTO metadata VALUES (?,?)", ("as_of_date", json.dumps(AS_OF)))
    for row in chunks:
        connection.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)", (row["chunk_id"], row["kind"], AS_OF,
            row["effective_date"], json.dumps(row["source"]), row["is_deleted"], 0))
    for row in contexts:
        connection.execute("INSERT INTO contexts VALUES (?,?,?,?)", (row["context_id"], row["kind"],
            json.dumps(row["source"]), row["text"]))
    connection.executemany("INSERT INTO chunk_context_links VALUES (?,?,?)",
        [(row["chunk_id"], row["context_id"], row["relation"]) for row in links])
    for row in conditions:
        connection.execute("INSERT INTO reviewed_conditions VALUES (?,?,?)", (row["condition_id"],
            row["source_unit_id"], json.dumps(row["review"])))
    connection.commit()
    connection.close()
    return conditions[0]


class ConditionCoverageTest(unittest.TestCase):
    def test_empty_targets_preserve_source_and_never_approve_applicability(self):
        _, rows, _ = map_conditions(*corpus())
        row = rows[0]
        self.assertTrue(row["empty_current_targets"])
        self.assertEqual(row["current_target_ids"], [])
        self.assertEqual(row["exact_source_primary_ids"], ["law:article:all"])
        self.assertEqual(row["required_facts"], ["인원"])
        self.assertIn("30명 이상", row["exceptions_and_scope"]["interpretation"])
        self.assertFalse(row["applicability_established"])
        self.assertFalse(row["answer_allowed"])

    def test_explicit_missing_targets_preserved_without_article_number_inference(self):
        data = corpus()
        target_ids = ["other-law:article", "law:missing", "law:article"]
        data[3][0]["review"]["condition"]["current_target_ids"] = target_ids
        _, rows, _ = map_conditions(*data)
        self.assertEqual(rows[0]["current_target_ids"], target_ids)
        self.assertEqual(rows[0]["missing_explicit_target_ids"], target_ids[:2])
        self.assertEqual(rows[0]["explicit_target_primary_ids"], ["law:article:all"])

    def test_required_context_origin_restores_real_body_source(self):
        chunks, contexts, links, conditions = corpus()
        attachment = {"chunk_id": "law:attachment:row", "kind": "attachment", "is_deleted": 0,
                      "source": {"source_unit_id": "law:attachment", "required_context_ids": ["ctx-body"],
                                 "dependency_primary_ids": []}}
        chunks.append(attachment)
        contexts.append({"context_id": "ctx-body", "kind": "required_body_contexts", "text": "본문 규칙",
                         "source": {"origin_id": "law:article:all"}})
        links.append({"chunk_id": attachment["chunk_id"], "context_id": "ctx-body", "relation": "required_body_contexts"})
        _, rows, _ = map_conditions(chunks, contexts, links, conditions)
        row = rows[0]
        self.assertEqual(row["source_via_context_primary_ids"], ["law:attachment:row"])
        association = next(item for item in row["associations"] if item["chunk_id"] == "law:attachment:row")
        self.assertEqual(association["matching_context_ids"], ["ctx-body"])
        self.assertFalse(association["applicability_established"])
        contexts[-1]["source"]["origin_id"] = "law:article:nonexistent"
        _, rows, _ = map_conditions(chunks, contexts, links, conditions)
        self.assertEqual(rows[0]["source_via_context_primary_ids"], [])

    def test_deleted_history_is_expected_default_exclusion(self):
        data = corpus()
        data[0][0]["is_deleted"] = 1
        data[3][0]["review"]["condition"]["applies_as_of"] = "historical_only"
        _, rows, _ = map_conditions(*data)
        self.assertEqual(rows[0]["coverage_status"], "deleted_source_excluded_by_default")
        self.assertEqual(rows[0]["default_retrievable_primary_ids"], [])
        self.assertEqual(rows[0]["associated_primary_ids"], ["law:article:all"])

    def test_missing_mandatory_context_is_reported_not_counted_ready(self):
        data = corpus()
        data[0][0]["source"]["required_context_ids"].append("missing-context")
        catalog, rows, _ = map_conditions(*data)
        self.assertEqual(catalog["law:article:all"]["missing_required_context_ids"], ["missing-context"])
        self.assertEqual(rows[0]["default_retrievable_primary_ids"], [])

    def test_missing_original_source_is_explicit_even_with_a_target_match(self):
        data = corpus()
        data[3][0]["source_unit_id"] = "missing:source"
        data[3][0]["review"]["condition"]["current_target_ids"] = ["law:article"]
        _, rows, _ = map_conditions(*data)
        self.assertTrue(rows[0]["unmatched_source_unit"])
        self.assertEqual(rows[0]["explicit_target_primary_ids"], ["law:article:all"])

    def test_quote_binding_checks_original_file_span_and_quote_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            saved = project_fixture(project)
            self.assertEqual(_verify_quote(project, saved, {})["status"], "exact_original_source_span")
            altered = copy.deepcopy(saved)
            altered["review"]["condition"]["quote"] = "규칙 31명 이상"
            self.assertEqual(_verify_quote(project, altered, {})["status"], "condition_quote_hash_mismatch")
            altered = copy.deepcopy(saved)
            altered["review"]["condition"]["evidence_binding"]["start"] = 1
            self.assertEqual(_verify_quote(project, altered, {})["status"], "original_source_span_mismatch")

    def test_check_only_is_read_only_and_unchanged_outputs_are_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            project_fixture(project)
            output = project / "outputs"
            with self.assertRaisesRegex(ValueError, "condition_coverage_missing"):
                run_coverage(project, True, output)
            self.assertFalse(output.exists())
            first = run_coverage(project, False, output)
            detail = Path(first["detail_artifact"])
            before = detail.stat().st_mtime_ns
            self.assertEqual(first["written_file_count"], 2)
            self.assertEqual(run_coverage(project, False, output)["written_file_count"], 0)
            checked = run_coverage(project, True, output)
            self.assertEqual(checked["written_file_count"], 0)
            self.assertEqual(detail.stat().st_mtime_ns, before)
            data = json.loads(detail.read_bytes())
            self.assertEqual(data["summary"]["condition_count"], 1)
            self.assertEqual(data["summary"]["conditions_with_exact_original_quote"], 1)
            self.assertEqual(len(data["source_inputs"]["database"]["sha256"]), 64)
            self.assertEqual(data["new_api_calls"], 0)
            detail.write_bytes(encoded({"tampered": True}))
            with self.assertRaisesRegex(ValueError, "coverage_output_content_mismatch"):
                run_coverage(project, True, output)


if __name__ == "__main__":
    unittest.main()
