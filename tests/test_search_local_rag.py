"""SQLite → retrieval → exact citation integration for explicit reference closure."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import local_rag_answer as answer
import search_local_rag as search

ASOF = "2026-10-01"
NAME = "서울특별시 가상공원 조례"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def vector(score=1.0):
    return [score, (1 - score * score) ** 0.5] + [0.0] * 1022


class ReferenceClosureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1], prefix="reference-closure-")
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "rag.sqlite3"
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

    def source(self):
        return {"ordin_seq": "10", "ordin_name": NAME, "as_of_date": ASOF,
                "effective_date": "2026-07-13", "content_hash": "a" * 64,
                "source_url": "https://www.law.go.kr/ordinInfoP.do?ordinSeq=10"}

    def add(self, cid, kind, raw, unit, score, contexts, title=None):
        meta = raw.get("source", raw)
        text = raw.get("quote_text", raw.get("primary_text"))
        wrapper = {"source_unit_id": unit, "source_record": raw, "dependency_primary_ids": [],
                   "external_reference_ids": [], "required_context_ids": [c[0] for c in contexts]}
        if title:
            wrapper["catalog_record"] = {"heading": [NAME, title]}
        self.connection.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, kind, meta["ordin_seq"], meta["as_of_date"], meta["effective_date"], NAME,
             text, digest(text), json.dumps(vector(score)), json.dumps(wrapper), 0, 0))
        for context_id, kind, source, context_text, relation in contexts:
            self.connection.execute("INSERT OR IGNORE INTO contexts VALUES(?,?,?,?)",
                                    (context_id, kind, json.dumps(source), context_text))
            self.connection.execute("INSERT INTO chunk_context_links VALUES(?,?,?)",
                                    (cid, context_id, relation))
        return wrapper

    def body(self, article=15, paragraph=1, score=0.2):
        parent = f"10:{article:04}00"
        cid = parent + ":" + (str(paragraph) if paragraph is not None else "all")
        heading = f"제{article}조(이용료)"
        quote = " ① 별표 2의 범위에서 비용을 고려하여 규칙으로 정한다.\n" if paragraph else " 별표 2의 범위에서 규칙으로 정한다.\n"
        full = heading + quote
        span = {"start": len(heading), "end": len(full)}
        raw = {**self.source(), "chunk_id": cid, "parent_id": parent, "article": f"제{article}조",
               "paragraph": paragraph, "quote_text": quote, "source_span": span,
               "is_deleted": False, "title": "이용료", "source_review": "pending"}
        context = {"article_id": parent, "article": raw["article"], "title": raw["title"],
                   "source_text": full, "source_text_hash": digest(full),
                   "segments": [{"kind": "heading", "start": 0, "end": len(heading), "text": heading},
                                {"kind": "provision", "paragraph": paragraph, **span, "text": quote}]}
        self.add(cid, "body", raw, parent, score,
                 [(parent, "article", {"source_record": context}, full, "required_parent")])
        return raw

    def attachment(self, body, index=1, score=1.0):
        aid, cid = "10:20", f"10:20:row:{index}"
        paragraph = f"제{body['paragraph']}항" if body['paragraph'] is not None else ""
        title = f"시설 이용료({body['article']}{paragraph} 관련)"
        reftext = title[title.index("("):]
        header = "[별표 2]\n" + title
        refs = [{"attachment_id": aid, "page": 1, "element_id": 2, "row_index": index,
                 "parse_file": "original-parse.json", "parse_content_hash": "b" * 64}]
        context = {"context_id": "heading-20", "kind": "ordered_attachment_text_across_pages",
                   "text": header, "source_refs": refs}
        lo = body["quote_text"].index("별표 2")
        raw = {"source": self.source(), "chunk_id": cid, "attachment_id": aid,
               "primary_text": "종 별: 야영장\n금 액(원): 1000", "source_refs": refs,
               "required_contexts": [context], "required_body_contexts": [],
               "related_body": {"title_reference_status": "matched_body_reference",
                   "title_references": [{"method": "explicit_attachment_title_reference",
                       "article": body["article"], "paragraph": body["paragraph"],
                       "reference_text": reftext, "reference_span": {"start": title.index("("), "end": len(title)},
                       "target_chunk_ids": [body["chunk_id"]]}],
                   "body_references": [{"method": "explicit_body_reference", "chunk_id": body["chunk_id"],
                       "article": body["article"], "paragraph": body["paragraph"], "quote_text": body["quote_text"],
                       "reference_text": "별표 2", "reference_span": {"start": lo, "end": lo + 4}}]}}
        context_id = f"{cid}:heading"
        saved = {"unit_id": context_id, "origin_id": context["context_id"], "kind": "required_contexts",
                 "provenance": context, "text": header}
        self.add(cid, "attachment", raw, "10:attachment:20", score,
                 [(context_id, "required_contexts", saved, header, "required_contexts")], title)
        return raw

    def update_raw(self, cid, raw):
        wrapper = json.loads(self.connection.execute("SELECT source_json FROM chunks WHERE chunk_id=?", (cid,)).fetchone()[0])
        wrapper["source_record"] = raw
        self.connection.execute("UPDATE chunks SET source_json=? WHERE chunk_id=?", (json.dumps(wrapper), cid))

    def retrieve(self, top_k=1, question="야영장 이용료를 정하는 방법은?"):
        self.connection.commit()
        return search.retrieve(self.db, vector(), top_k=top_k, question=question)

    def exact_citations(self, result):
        data, units, _ = answer.prepare_evidence(result)
        ids = [r["unit_id"] for r in data["primary_units"]]
        return answer._citations(ids, units, data["primary_units"])

    def test_annex_restores_outside_top_k_body_parent_and_citable_quote(self):
        body = self.body()
        annex = self.attachment(body)
        result = self.retrieve()
        self.assertEqual(result["ranked_chunk_ids"], [annex["chunk_id"]])
        self.assertEqual(set(result["expanded_chunk_ids"]), {annex["chunk_id"], body["chunk_id"]})
        restored = next(e for e in result["evidence"] if e["chunk_id"] == body["chunk_id"])
        self.assertEqual(restored["retrieval_role"], "explicit_reference")
        self.assertEqual(restored["source"]["source_record"], body)
        self.assertTrue(any(c["context_id"] == body["parent_id"] for c in restored["required_contexts"]))
        citation = next(c for c in self.exact_citations(result) if c["unit_id"] == body["chunk_id"])
        self.assertEqual(citation["quote"], body["quote_text"])
        self.assertEqual(citation["locator"]["source_span"], body["source_span"])
        self.assertEqual(citation["parent_chunk_id"], body["chunk_id"])
        self.assertEqual(citation["source_url"], body["source_url"])
        self.assertFalse(result["answer_allowed"])
        self.assertFalse(result["local_reference"]["resolved"][-1]["applicability_established"])

    def test_body_to_annex_still_transfers_exact_header_and_citation(self):
        body = self.body(score=1.0)
        annex = self.attachment(body, score=0.2)
        result = self.retrieve(question="별표 2 야영장 이용료는?")
        self.assertEqual(result["ranked_chunk_ids"], [body["chunk_id"]])
        self.assertIn(annex["chunk_id"], result["expanded_chunk_ids"])
        citations = self.exact_citations(result)
        self.assertEqual(next(c["quote"] for c in citations if c["unit_id"] == annex["chunk_id"]), annex["primary_text"])
        self.assertTrue(any("[별표 2]" in c["quote"] for c in citations if c["kind"] == "required_context"))
        self.assertEqual(len(result["expanded_chunk_ids"]), 2)

    def test_missing_target_and_tampered_reference_do_not_expand(self):
        body = self.body()
        original = self.attachment(body)
        for mutation in ("missing_target", "body_quote", "body_span", "wrong_annex", "title_span", "title_target"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(original)
                title = changed["related_body"]["title_references"][0]
                ref = changed["related_body"]["body_references"][0]
                if mutation == "missing_target": title["target_chunk_ids"] = ["10:999900:1"]
                elif mutation == "body_quote": ref["quote_text"] += "변조"
                elif mutation == "body_span": ref["reference_span"]["start"] += 1
                elif mutation == "wrong_annex": ref["reference_text"] = "별표 1"
                elif mutation == "title_span": title["reference_span"]["end"] -= 1
                else: title["paragraph"] = 2
                self.update_raw(original["chunk_id"], changed)
                result = self.retrieve()
                self.assertEqual(result["expanded_chunk_ids"], [original["chunk_id"]])
                self.assertTrue(result["local_reference"]["unresolved"])

    def test_owner_version_url_or_deleted_target_is_rejected(self):
        original = self.body()
        annex = self.attachment(original)
        for key, value in (("ordin_seq", "11"), ("as_of_date", "2026-09-01"),
                           ("effective_date", "2025-01-01"), ("content_hash", "c"*64),
                           ("source_url", "https://other.invalid"), ("is_deleted", True)):
            with self.subTest(key=key):
                raw = {**original, key: value}
                self.update_raw(original["chunk_id"], raw)
                self.assertEqual(self.retrieve()["expanded_chunk_ids"], [annex["chunk_id"]])
        self.update_raw(original["chunk_id"], original)
        self.connection.execute("UPDATE chunks SET is_deleted=1 WHERE chunk_id=?", (original["chunk_id"],))
        self.assertEqual(self.retrieve()["expanded_chunk_ids"], [annex["chunk_id"]])

    def test_body_parent_span_and_heading_provenance_are_verified(self):
        body = self.body(paragraph=None)
        annex = self.attachment(body)
        self.assertIn(body["chunk_id"], self.retrieve()["expanded_chunk_ids"])
        missing = dict(body)
        del missing["paragraph"]
        self.update_raw(body["chunk_id"], missing)
        self.assertEqual(self.retrieve()["expanded_chunk_ids"], [annex["chunk_id"]])
        self.update_raw(body["chunk_id"], body)
        stored = self.connection.execute("SELECT source_json FROM contexts WHERE context_id=?",
                                         (body["parent_id"],)).fetchone()[0]
        changed = json.loads(stored)
        del changed["source_record"]["segments"][1]["paragraph"]
        self.connection.execute("UPDATE contexts SET source_json=? WHERE context_id=?",
                                (json.dumps(changed), body["parent_id"]))
        self.assertEqual(self.retrieve()["expanded_chunk_ids"], [annex["chunk_id"]])
        self.connection.execute("UPDATE contexts SET source_json=? WHERE context_id=?",
                                (stored, body["parent_id"]))
        self.connection.execute("UPDATE contexts SET text=text||'변조' WHERE context_id=?", (body["parent_id"],))
        self.assertEqual(self.retrieve()["expanded_chunk_ids"], [annex["chunk_id"]])

    def test_attachment_header_wrong_owner_does_not_authorize_body(self):
        body = self.body()
        annex = self.attachment(body)
        changed = copy.deepcopy(annex)
        changed["source_refs"][0]["attachment_id"] = "10:99"
        self.update_raw(annex["chunk_id"], changed)
        self.assertEqual(self.retrieve()["expanded_chunk_ids"], [annex["chunk_id"]])
        # Even internally consistent Parse refs cannot give another ordinance's
        # attachment the current ordinance's source/target identity.
        changed = copy.deepcopy(annex)
        changed["attachment_id"] = "11:20"
        changed["source_refs"][0]["attachment_id"] = "11:20"
        changed["required_contexts"][0]["source_refs"][0]["attachment_id"] = "11:20"
        self.update_raw(annex["chunk_id"], changed)
        saved = {"unit_id": annex["chunk_id"] + ":heading", "origin_id": "heading-20", "kind": "required_contexts",
                 "provenance": changed["required_contexts"][0], "text": changed["required_contexts"][0]["text"]}
        self.connection.execute("UPDATE contexts SET source_json=? WHERE context_id=?",
                                (json.dumps(saved), annex["chunk_id"] + ":heading"))
        self.assertEqual(self.retrieve()["expanded_chunk_ids"], [annex["chunk_id"]])

    def test_duplicate_rows_and_other_backlinks_do_not_expand_unrelated_bodies(self):
        body = self.body()
        other = self.body(article=8, score=0.1)
        a1 = self.attachment(body)
        a2 = self.attachment(body, index=2, score=0.9)
        for annex in (a1, a2):
            ref = copy.deepcopy(annex["related_body"]["body_references"][0])
            ref.update(chunk_id=other["chunk_id"], article=other["article"], quote_text=other["quote_text"])
            annex["related_body"]["body_references"].extend([ref, copy.deepcopy(annex["related_body"]["body_references"][0])])
            annex["related_body"]["title_references"][0]["target_chunk_ids"] *= 2
            self.update_raw(annex["chunk_id"], annex)
        result = self.retrieve(top_k=2)
        self.assertEqual(set(result["expanded_chunk_ids"]), {a1["chunk_id"], a2["chunk_id"], body["chunk_id"]})
        self.assertEqual(len(result["local_reference"]["resolved"]), 1)
        self.assertNotIn(other["chunk_id"], result["expanded_chunk_ids"])

    def test_title_only_or_unstated_targets_are_not_inferred(self):
        body = self.body()
        annex = self.attachment(body)
        for key in ("body_references", "title_references"):
            changed = copy.deepcopy(annex)
            changed["related_body"][key] = []
            self.update_raw(annex["chunk_id"], changed)
            self.assertEqual(self.retrieve()["expanded_chunk_ids"], [annex["chunk_id"]])

    def test_reference_budget_is_atomic_and_external_scope_is_preserved(self):
        body = self.body()
        annex = self.attachment(body)
        with patch.object(search, "MAX_ADDED_REFERENCE_CHUNKS", 0):
            result = self.retrieve()
        self.assertEqual(result["expanded_chunk_ids"], [annex["chunk_id"]])
        self.assertEqual(result["local_reference"]["unresolved"][-1]["budget_kind"], "added_reference_chunks")
        result = self.retrieve(question="「다른 법 시행규칙」 별표 2 야영장 이용료는?")
        self.assertEqual(result["expanded_chunk_ids"], [annex["chunk_id"]])
        self.assertTrue(result["external_question_references"])
        self.assertFalse(result["answer_generated"])


if __name__ == "__main__":
    unittest.main()

