"""Small offline reference-recovery tests; no source corpus or provider calls."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ask_local_rag
import search_local_rag as search

ASOF = "2026-10-01"
NAME = "서울특별시 가상공원 조례"


def vector(score=1.0):
    return [score, (1.0 - score ** 2) ** 0.5] + [0.0] * 1022


class ReferenceFixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="reference-test-")
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / "rag.sqlite3"
        self.connection = sqlite3.connect(self.db)
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value_json TEXT);
            CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,kind TEXT,ordin_seq TEXT,as_of_date TEXT,
                effective_date TEXT,title TEXT,text TEXT,text_sha256 TEXT,vector_json TEXT,
                source_json TEXT,is_deleted INTEGER,answer_allowed INTEGER);
            CREATE TABLE contexts(context_id TEXT PRIMARY KEY,kind TEXT,source_json TEXT,text TEXT);
            CREATE TABLE chunk_context_links(chunk_id TEXT,context_id TEXT,relation TEXT);
            CREATE TABLE reviewed_conditions(condition_id TEXT,source_unit_id TEXT,condition_json TEXT);
            CREATE TABLE external_material_tasks(reference_id TEXT PRIMARY KEY,task_json TEXT);
        """)
        self.connection.executemany("INSERT INTO metadata VALUES(?,?)",
                                    [("as_of_date", json.dumps(ASOF)), ("dimension", "1024")])

    def source(self, seq="10", date="2026-07-13", content="a" * 64):
        return {"ordin_seq": seq, "as_of_date": ASOF, "effective_date": date, "content_hash": content,
                "source_url": "https://www.law.go.kr/ordinInfoP.do?ordinSeq=" + seq}

    def add(self, cid, kind, source, record, score, contexts=(), external=()):
        text = record.get("quote_text", record.get("primary_text", ""))
        wrapper = {"source_unit_id": cid + ":unit", "source_record": record,
                   "required_context_ids": [item[0] for item in contexts],
                   "dependency_primary_ids": [], "external_reference_ids": list(external),
                   "catalog_record": {"heading": [NAME]}}
        self.connection.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, kind, source["ordin_seq"], ASOF, source["effective_date"], NAME, text,
             hashlib.sha256(text.encode()).hexdigest(), json.dumps(vector(score)), json.dumps(wrapper), 0, 0))
        for context_id, context in contexts:
            saved = {"origin_id": context["context_id"], "provenance": context, "text": context["text"]}
            self.connection.execute("INSERT OR IGNORE INTO contexts VALUES(?,?,?,?)",
                                    (context_id, context["kind"], json.dumps(saved), context["text"]))
            self.connection.execute("INSERT INTO chunk_context_links VALUES(?,?,?)",
                                    (cid, context_id, context["kind"]))
        return wrapper

    def body(self, cid="body", article="제15조", paragraph=1, quote="별표 2의 범위에서 정한다.", source=None):
        source = source or self.source()
        record = {**source, "ordin_name": NAME, "article": article, "paragraph": paragraph, "quote_text": quote}
        self.add(cid, "body", source, record, 1.0)
        return record

    def attachment(self, cid, label="별표 2", row="잔디광장", index=1, note=None, body=None,
                   source=None, score=0.2, section="1. 운동시설"):
        source = source or self.source()
        refs = [{"attachment_id": label, "page": 1, "element_id": 2, "row_index": index,
                 "parse_file": "existing-parse.json", "parse_content_hash": "p" * 64}]
        contexts = [{"context_id": label + ":heading", "kind": "ordered_attachment_text_across_pages",
                     "text": "[" + label + "]\n시설 이용료", "source_refs": refs},
                    {"context_id": label + ":section:" + section, "kind": "preceding_table_heading",
                     "text": section, "source_refs": refs}]
        if note:
            contexts.append(note)
        record = {"source": source, "attachment_id": source["ordin_seq"] + ":" + label,
                  "primary_text": "종 별: " + row + "\n금 액(원): 13~18", "required_contexts": contexts,
                  "source_refs": refs, "related_body": {"body_references": []}}
        if body:
            quote = body["quote_text"]
            start = quote.index(label)
            record["related_body"]["body_references"].append({"chunk_id": body["chunk_id"],
                "quote_text": quote, "reference_text": label, "reference_span": {"start": start, "end": start + len(label)},
                "method": "explicit_body_reference"})
        dbcontexts = [(source["content_hash"] + ":" + context["context_id"], context) for context in contexts]
        self.add(cid, "attachment", source, record, score, dbcontexts)
        return record

    def retrieve(self, question=None, **kwargs):
        self.connection.commit()
        return search.retrieve(self.db, vector(), top_k=1, question=question, **kwargs)

    def test_omitted_question_preserves_existing_ranked_retrieval(self):
        body = self.body()
        self.attachment("row", body={**body, "chunk_id": "body"})
        result = self.retrieve()
        self.assertEqual(result["expanded_chunk_ids"], ["body"])
        self.assertEqual(result["local_reference"], {"requested": [], "resolved": [], "unresolved": []})

    def test_named_note_uses_one_provenance_carrier_and_preserves_full_note(self):
        self.body(quote="다른 검색 상위 조문")
        note = {"context_id": "note-one", "kind": "table_note", "text": "청소년은 13세 이상 18세 이하",
                "source_refs": [{"parse_file": "existing-parse.json", "page": 1, "element_id": 2}]}
        self.attachment("first-row", label="별표 1", row="동물원", note=note)
        self.attachment("second-row", label="별표 1", row="테마가든", index=2, note=note, score=0.9)
        result = self.retrieve("가상공원 별표 1 비고에서 청소년 정의를 알려 주세요")
        self.assertEqual(result["expanded_chunk_ids"], ["body", "first-row"])
        restored = result["evidence"][1]
        self.assertIn(note["text"], [item["text"] for item in restored["required_contexts"]])
        resolved = result["local_reference"]["resolved"][0]
        self.assertEqual(resolved["reason"], "required_note_context")
        self.assertFalse(resolved["applicability_established"])

    def test_body_reference_recovers_question_named_facility_without_ordinance_guess(self):
        body = {**self.body(), "chunk_id": "body"}
        self.attachment("lawn", row="잔디광장", body=body)
        self.attachment("field", row="일반운동장", index=2, body=body)
        result = self.retrieve("별표 2 잔디광장 이용료는 얼마인가요?")
        self.assertEqual(result["expanded_chunk_ids"], ["body", "lawn"])
        self.assertEqual(result["local_reference"]["resolved"][0]["source_chunk_id"], "body")
        requested = {item["reference_id"]: item for item in result["local_reference"]["requested"]}
        self.assertTrue(all(item["scope"] == requested[item["reference_id"]]["scope"]
                            for item in result["local_reference"]["resolved"]))

    def nested_attachment(self, body):
        record = self.attachment("nested-lawn", row="잔디광장")
        contexts = record.pop("required_contexts")
        contexts[0]["text"] = "■ " + NAME + " [별표 2]\n시설 이용료"
        record.pop("related_body")
        record["grounding_bundle"] = {"required_contexts": contexts, "required_body_contexts": [body],
                                      "requested_chunk_id": "nested-lawn", "primary_records": []}
        wrapper = json.loads(self.connection.execute("SELECT source_json FROM chunks WHERE chunk_id='nested-lawn'").fetchone()[0])
        wrapper["source_record"] = record
        wrapper["catalog_record"]["heading"] = []
        self.connection.execute("UPDATE chunks SET source_json=? WHERE chunk_id='nested-lawn'", (json.dumps(wrapper),))

    def test_nested_grounding_bundle_restores_named_and_exact_body_references(self):
        body = {**self.body(), "chunk_id": "body"}
        self.nested_attachment(body)
        for question in ("가상공원 조례 별표 2 잔디광장 이용료", "별표 2 잔디광장 이용료"):
            with self.subTest(question=question):
                result = self.retrieve(question)
                self.assertEqual(result["expanded_chunk_ids"], ["body", "nested-lawn"])
                self.assertFalse(result["local_reference"]["unresolved"])
                requested = {item["reference_id"]: item for item in result["local_reference"]["requested"]}
                self.assertTrue(all(item["scope"] == requested[item["reference_id"]]["scope"]
                                    for item in result["local_reference"]["resolved"]))

    def test_nested_mandatory_source_without_local_reference_is_unresolved(self):
        body = {**self.body(quote="「외부 법률」 별표 2를 확인한다."), "chunk_id": "body"}
        self.nested_attachment(body)
        result = self.retrieve("잔디광장 이용료")
        self.assertEqual(result["expanded_chunk_ids"], ["body"])
        self.assertEqual(result["local_reference"]["unresolved"][0]["reason"], "body_reference_not_explicit")
        self.assertFalse(result["local_reference"]["resolved"])

    def test_nested_mandatory_unquoted_external_owner_does_not_create_edge(self):
        body = {**self.body(quote="법 시행령 별표 2의 잔디광장 기준을 따른다."), "chunk_id": "body"}
        self.nested_attachment(body)
        result = self.retrieve("잔디광장 이용료")
        self.assertEqual(result["expanded_chunk_ids"], ["body"])
        self.assertEqual(result["local_reference"]["unresolved"][0]["reason"], "body_reference_not_explicit")
        self.assertFalse(result["local_reference"]["resolved"])

    def test_bad_quote_or_span_or_other_ordinance_or_version_never_creates_edge(self):
        body = {**self.body(), "chunk_id": "body"}
        original = self.attachment("lawn", row="잔디광장", body=body)
        for change in ("quote", "span", "ordinance", "version", "content", "title_only"):
            record = copy.deepcopy(original)
            if change == "quote":
                record["related_body"]["body_references"][0]["quote_text"] += "다름"
            elif change == "span":
                record["related_body"]["body_references"][0]["reference_span"]["start"] = 1
            elif change == "ordinance":
                record["source"]["ordin_seq"] = "20"
            elif change == "version":
                record["source"]["effective_date"] = "2025-01-01"
            elif change == "content":
                record["source"]["content_hash"] = "b" * 64
            else:
                record["related_body"]["body_references"][0]["method"] = "explicit_attachment_title_reference"
            saved = json.loads(self.connection.execute("SELECT source_json FROM chunks WHERE chunk_id='lawn'").fetchone()[0])
            saved["source_record"] = record
            self.connection.execute("UPDATE chunks SET source_json=? WHERE chunk_id='lawn'", (json.dumps(saved),))
            with self.subTest(change=change):
                self.assertEqual(self.retrieve("잔디광장 이용료")["expanded_chunk_ids"], ["body"])

    def test_same_title_multiple_versions_is_unresolved_even_if_one_ranks_first(self):
        self.body(quote="검색 상위 조문")
        self.attachment("current", label="별표 1")
        self.attachment("old", label="별표 1", source=self.source(date="2025-01-01", content="b" * 64))
        result = self.retrieve("가상공원 조례 별표 1 비고")
        self.assertEqual(result["expanded_chunk_ids"], ["body"])
        self.assertEqual(result["local_reference"]["unresolved"][0]["reason"], "ambiguous_ordinance_version")

    def test_other_municipality_or_quoted_and_unquoted_rule_cannot_fall_back_to_local_body(self):
        body = {**self.body(), "chunk_id": "body"}
        self.attachment("lawn", row="잔디광장", body=body)
        for question in ("부산시 가상공원 조례 별표 2 잔디광장 요금",
                         "「서울특별시 가상공원 조례 시행규칙」 별표 2 잔디광장 요금",
                         "가상공원 조례 시행규칙에 따라 위임된 사항 중 별표 2 잔디광장 요금",
                         "가상공원 및 녹지 등에 관한 법률에서 정한 별표 2 잔디광장 요금",
                         "주차장법 시행령 별표 2 잔디광장 요금",
                         "가상공원 조례 제15조와 주차장법 시행령 별표 2 잔디광장 요금"):
            with self.subTest(question=question):
                result = self.retrieve(question)
                self.assertEqual(result["expanded_chunk_ids"], ["body"])
                self.assertFalse(any(item["kind"] == "attachment" for item in result["local_reference"]["resolved"]))
                self.assertTrue(result["external_question_references"])
                self.assertFalse(result["external_tasks"])

    def test_explicit_article_paragraph_and_local_missing_reference_are_distinct(self):
        self.body(cid="first", article="제20조", paragraph=1, quote="입장료 면제")
        self.body(cid="third", article="제20조", paragraph=3, quote="운동시설 감면")
        result = self.retrieve("가상공원 조례 제20조 제3항과 별표 99를 비교해 주세요")
        self.assertIn("third", result["expanded_chunk_ids"])
        self.assertEqual(result["local_reference"]["resolved"][0]["primary_ids"], ["third"])
        self.assertEqual(result["local_reference"]["unresolved"][0]["reason"], "local_reference_missing")
        self.assertFalse(result["external_tasks"])

    def test_same_note_prose_with_different_provenance_is_not_merged(self):
        self.body(quote="다른 검색 상위 조문")
        for index in (1, 2):
            note = {"context_id": "note-" + str(index), "kind": "table_note", "text": "청소년은 13세 이상",
                    "source_refs": [{"page": index, "element_id": 2}]}
            self.attachment("row-" + str(index), label="별표 1", index=index, note=note)
        result = self.retrieve("가상공원 별표 1 비고의 정의")
        self.assertEqual(set(result["expanded_chunk_ids"]), {"body", "row-1", "row-2"})

    def test_age_or_amount_or_common_row_value_cannot_expand_entire_table(self):
        self.body(quote="다른 검색 상위 조문")
        for index in range(12):
            self.attachment("row-" + str(index), label="별표 1", row="어린이", index=index)
        result = self.retrieve("가상공원 별표 1에서 어린이 13세와 18원 요금을 확인해 주세요")
        self.assertEqual(result["expanded_chunk_ids"], ["body"])
        self.assertEqual(result["local_reference"]["unresolved"][0]["reason"], "row_selection_requires_review")

    def test_table_possessive_age_and_row_numeric_boundaries_are_not_identifiers(self):
        self.body(quote="다른 검색 상위 조문")
        self.attachment("first-grade", label="별표 1", row="1급지")
        self.attachment("second-grade", label="별표 1", row="2급지", index=2)
        for question in ("가상공원 별표 1의 13세 요금", "가상공원 별표 1에서 11급지 요금"):
            with self.subTest(question=question):
                result = self.retrieve(question)
                self.assertEqual(result["expanded_chunk_ids"], ["body"])
                pending = result["local_reference"]["unresolved"][0]
                self.assertEqual((pending["reference"], pending["reason"]),
                                 ("별표 1", "row_selection_requires_review"))
        for question in ("가상공원 별표 1의 1급지 요금", "가상공원 별표 1 1급지 요금"):
            result = self.retrieve(question)
            self.assertEqual(result["expanded_chunk_ids"], ["body", "first-grade"])
            self.assertFalse(result["local_reference"]["unresolved"])

    def test_unnumbered_and_legacy_own_headers_do_not_invent_numbers_or_use_external_notes(self):
        record = {"source": {"source_record": {"required_contexts": [{
            "kind": "ordered_attachment_text_across_pages", "text": "[별 표]\n주차요금표"}]}}}
        self.assertEqual(search._attachment_label(record), "별표")
        record["source"]["source_record"] = {"primary_text": NAME + "\n별표 2: 설치 기준\n행: 1. 위락시설",
            "grounding_bundle": {"required_contexts": [{"kind": "legacy_full_attachment_conditions",
                "text": "「건축법 시행령」 별표 1의 시설물 기준"}]}}
        self.assertEqual(search._attachment_label(record), "별표 2")

    def test_row_budget_excess_is_unresolved_instead_of_silent_truncation(self):
        self.body(quote="다른 검색 상위 조문")
        for index, row in enumerate(("공원가", "공원나", "공원다", "공원라")):
            self.attachment("row-" + str(index), label="별표 1", row=row, index=index)
        with patch.object(search, "MAX_REFERENCE_ROWS", 2):
            result = self.retrieve("가상공원 별표 1 공원가 공원나 공원다 요금")
        self.assertEqual(result["expanded_chunk_ids"], ["body"])
        self.assertFalse(result["local_reference"]["resolved"])
        pending = result["local_reference"]["unresolved"][0]
        self.assertEqual((pending["budget_kind"], pending["candidate_count"], pending["candidate_limit"]),
                         ("reference_row_candidates", 3, 2))

    def test_note_budget_never_marks_partial_provenance_coverage_resolved(self):
        self.body(quote="다른 검색 상위 조문")
        for index in range(3):
            note = {"context_id": "note-" + str(index), "kind": "table_note", "text": "필수 비고",
                    "source_refs": [{"page": index + 1, "element_id": 2}]}
            self.attachment("row-" + str(index), label="별표 1", index=index, note=note)
        with patch.object(search, "MAX_ADDED_REFERENCE_CHUNKS", 2):
            result = self.retrieve("가상공원 별표 1 비고")
        self.assertEqual(result["expanded_chunk_ids"], ["body"])
        self.assertFalse(result["local_reference"]["resolved"])
        self.assertEqual(result["local_reference"]["unresolved"][0]["budget_kind"], "added_reference_chunks")

    def anaphora_requests(self, question):
        result = self.retrieve(question)
        return result, [item for item in result["local_reference"]["requested"] if item["origin"] == "question"]

    def test_demonstrative_bare_annex_after_numbered_annex_is_not_a_new_reference(self):
        body = {**self.body(), "chunk_id": "body"}
        self.attachment("lawn", row="잔디광장", body=body)
        result, requested = self.anaphora_requests(
            "가상공원 조례 별표 2(시설 이용료)에 적힌 잔디광장 이용료는 얼마인가요? "
            "그리고 별표 2 제목 옆의 개정 날짜가 곧 이 별표의 시행일인지 구분해 알려 주세요.")
        self.assertEqual([(item["reference_id"], item["reference"]) for item in requested],
                         [("question:0", "별표 2"), ("question:1", "별표 2")])
        self.assertNotIn("local_reference_missing",
                         [item["reason"] for item in result["local_reference"]["unresolved"]])

    def test_bare_annex_with_unnumbered_annex_in_scope_stays_requested_outside_demonstrative_exception(self):
        # The demonstrative exception must not swallow bare annex mentions: when
        # the scope has an annex labelled exactly "별표", the bare mention names it.
        body = {**self.body(), "chunk_id": "body"}
        self.attachment("lawn", row="잔디광장", body=body)
        self.attachment("bare-annex", label="별표", row="야외무대")
        for question in ("가상공원 조례에서 이 별표의 요금은 얼마인가요?",
                         "가상공원 조례 별표 2 잔디광장 이용료와 그리고 별표의 비고는 무엇인가요?",
                         "가상공원 조례 이 별표의 시행일과 별표 2 잔디광장 이용료는 무엇인가요?"):
            with self.subTest(question=question):
                _, requested = self.anaphora_requests(question)
                self.assertIn("별표", [item["reference"] for item in requested])

    def test_bare_annex_without_unnumbered_annex_in_scope_is_not_a_reference(self):
        # Only numbered annexes exist in this scope, so a bare "별표" names no
        # specific annex; other references keep their handling and ids.
        body = {**self.body(), "chunk_id": "body"}
        self.attachment("lawn", row="잔디광장", body=body)
        cases = (("가상공원 조례 별표에서 잔디광장 이용료는 얼마인가요?", []),
                 ("가상공원 조례 제15조와 별표 2 잔디광장 이용료, 그리고 별표의 비고는 무엇인가요?",
                  [("question:0", "제15조"), ("question:1", "별표 2")]),
                 ("가상공원 조례 별표의 비고와 제15조, 별표 2 잔디광장 이용료는 무엇인가요?",
                  [("question:1", "제15조"), ("question:2", "별표 2")]))
        for question, expected in cases:
            with self.subTest(question=question):
                result, requested = self.anaphora_requests(question)
                reference = result["local_reference"]
                self.assertEqual([(item["reference_id"], item["reference"]) for item in requested], expected)
                self.assertNotIn("별표", [item["reference"] for item in reference["requested"] + reference["unresolved"]])
                self.assertNotIn("local_reference_missing", [item["reason"] for item in reference["unresolved"]])

    def test_reverse_body_reference_does_not_depend_on_eligible_iteration_order(self):
        # One annex group whose members bind the same ranked body chunk in two
        # ways: an explicit body reference and a mandatory body context only.
        # The recorded origin must not follow the hash order of the eligible set.
        body = {**self.body(), "chunk_id": "body"}
        self.attachment("row-explicit", row="잔디광장", body=body)
        self.attachment("row-required", row="일반운동장", index=2)
        wrapper = json.loads(self.connection.execute(
            "SELECT source_json FROM chunks WHERE chunk_id='row-required'").fetchone()[0])
        wrapper["source_record"]["required_body_contexts"] = [body]
        self.connection.execute("UPDATE chunks SET source_json=? WHERE chunk_id='row-required'", (json.dumps(wrapper),))

        class OrderedEligible(set):
            # Same membership as the real set; only the iteration order differs.
            def __init__(self, items, order):
                super().__init__(items)
                self.order = order

            def __iter__(self):
                return iter(self.order)

        original = search._reference_expansion
        results = []
        for order in (["body", "row-explicit", "row-required"], ["body", "row-required", "row-explicit"],
                      ["row-required", "row-explicit", "body"]):
            def expansion(by_id, scores, ranked, eligible, question, connection, order=order):
                self.assertEqual(set(eligible), set(order))
                return original(by_id, scores, ranked, OrderedEligible(eligible, order), question, connection)
            with self.subTest(order=order), patch.object(search, "_reference_expansion", expansion):
                results.append(self.retrieve("별표 2 잔디광장 이용료는 얼마인가요?"))
        origins = [[item["origin"] for item in result["local_reference"]["requested"]
                    if item["reference_id"].startswith("body:")] for result in results]
        self.assertEqual(origins[0], ["explicit_body_reference"])
        for result in results[1:]:
            self.assertEqual(result, results[0])


class QuestionForwardingTests(unittest.TestCase):
    def test_question_is_forwarded_to_the_single_retrieval(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="reference-ask-test-") as folder:
            retrieve = Mock(return_value={"top_k": 8})
            modules = {
                "search_local_rag": types.SimpleNamespace(__file__=search.__file__, retrieve=retrieve),
                "local_rag_answer": types.SimpleNamespace(__file__=ask_local_rag.__file__, assess_and_answer=Mock(
                    return_value={"request_more_evidence": False})),
                "local_rag_client": sys.modules["local_rag_client"],
                "public_rag_response": types.SimpleNamespace(__file__=search.__file__),
                "rag_numeric_amounts": types.SimpleNamespace(__file__=search.__file__),
                "release_policy": types.SimpleNamespace(__file__=search.__file__),
                **{name: types.SimpleNamespace(__file__=search.__file__) for name in ask_local_rag.SOURCE_SELECTION_MODULES},
            }
            client = types.SimpleNamespace(called=0, calls=[], request=Mock(return_value=(
                {"model": "solar-embedding-2-query", "data": [{"index": 0, "embedding": vector()}]}, {})))
            identity = {"database_sha256": "a" * 64, "identity": "fixture", "as_of_date": ASOF}
            with patch.object(ask_local_rag, "database_identity", return_value=identity), \
                    patch.object(ask_local_rag.importlib, "import_module", side_effect=modules.__getitem__), \
                    patch.object(ask_local_rag, "Client", return_value=client):
                result = ask_local_rag.run_question(Path(folder), "가상공원 별표 1 비고")
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(retrieve.call_count, 1)
            self.assertEqual(retrieve.call_args.kwargs["top_k"], 8)
            self.assertEqual(modules["local_rag_answer"].assess_and_answer.call_count, 1)
            self.assertEqual(retrieve.call_args.kwargs["question"], "가상공원 별표 1 비고")
            self.assertFalse(result["answer_allowed"])
            self.assertFalse(result["final_answer_generated"])


if __name__ == "__main__":
    unittest.main()
