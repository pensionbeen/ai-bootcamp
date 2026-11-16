"""Offline reviewed-evidence adapters and role/condition boundaries."""

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import dotenv

import evidence_gate as gate
import embed_attachment_chunks as embedding
import search_attachment_chunks as search


PROJECT = Path(__file__).resolve().parents[1]


def model_response(decision, evidence, missing=()):
    value = {"decision": decision, "reason": "오프라인 판정 검증", "evidence": evidence,
             "missing_information": list(missing)}
    return {"model": gate.MODEL, "choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(value, ensure_ascii=False)}}]}


def complete_citations(units, primary_id):
    return ([{"unit_id": primary_id, "role": "direct_support"}]
            + [{"unit_id": uid, "role": "applicability"}
               for uid in units[primary_id]["required_context_ids"]])


@unittest.skipUnless((PROJECT / "data/processed/attachments/2026-10-01/embedding_ready/embedding_inputs.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/processed/attachments/2026-10-01/embedding_ready/embedding_inputs.json")
class ReviewedUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = PROJECT / "data/processed/attachments/2026-10-01/embedding_ready/embedding_inputs.json"
        cls.records = json.loads(path.read_bytes())["records"]
        cls.by_id = {r["chunk_id"]: r for r in cls.records}

    def setUp(self):
        network = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("No live API in unit tests"))
        network.start()
        self.addCleanup(network.stop)
        dotenv = patch.object(gate, "load_dotenv", side_effect=AssertionError("No credentials in unit tests"))
        dotenv.start()
        self.addCleanup(dotenv.stop)

    def test_all_approved_records_have_owned_complete_condition_graphs(self):
        units = gate.build_reviewed_units(self.records)
        self.assertEqual(len([u for u in units.values() if u["kind"] == "primary"]), 62)
        expected_count = sum(1 + len(r["required_contexts"]) + len(r["additional_contexts"])
                             + len(r["required_body_contexts"]) for r in self.records)
        self.assertEqual(len(units), expected_count)
        for record in self.records:
            primary = units[record["chunk_id"]]
            self.assertEqual(primary["text"], record["primary_text"])
            self.assertEqual(primary["attachment_id"], record["attachment_id"])
            self.assertEqual(primary["source_refs"], record["source_refs"])
            self.assertEqual(primary["source"], record["source"])
            for uid in primary["required_context_ids"]:
                child = units[uid]
                self.assertEqual(child["parent_chunk_id"], record["chunk_id"])
                self.assertEqual(child["attachment_id"], record["attachment_id"])
                self.assertNotIn("direct_support", child["allowed_roles"])
                self.assertFalse(child["answer_allowed"])

    def test_shared_attachment_contexts_keep_distinct_parent_ownership(self):
        records = [self.by_id[f"2171833:22740729:candidate:p1-e2-r{i}"] for i in (1, 2)]
        units = gate.build_reviewed_units(records)
        first, second = (units[r["chunk_id"]] for r in records)
        self.assertTrue(set(first["required_context_ids"]).isdisjoint(second["required_context_ids"]))
        self.assertEqual(units[first["required_context_ids"][0]]["origin_id"],
                         units[second["required_context_ids"][0]]["origin_id"])

    def test_camping_omits_sports_discount_body_even_when_metadata_contains_it(self):
        record = self.by_id["2147709:22415979:candidate:p1-e5-r7"]
        self.assertTrue(any(r["chunk_id"] == "2147709:002000:3" for r in record["related_body"]["body_references"]))
        units = gate.build_reviewed_units([record])
        self.assertNotIn("required_body_context", {u["kind"] for u in units.values()})
        text = "\n".join(u["text"] for u in units.values())
        self.assertNotIn("100분의 50", text)
        self.assertNotIn("30%할인", text)
        self.assertIn("2. 휴양시설", text)

    def test_explicit_bus_parking_body_condition_is_required_and_keeps_body_source(self):
        record = self.by_id["2171833:22740729:candidate:p1-e2-r1"]
        units = gate.build_reviewed_units([record])
        body, = [u for u in units.values() if u["kind"] == "required_body_context"]
        self.assertEqual(body["origin_id"], "2171833:002000:3")
        self.assertIn("중ㆍ대형 승합자동차", body["text"])
        self.assertIn(body["unit_id"], units[record["chunk_id"]]["required_context_ids"])
        self.assertEqual(body["source_refs"][0]["source_kind"], "ordinance_body")
        self.assertEqual(body["source_refs"][0]["body_chunk_id"], "2171833:002000:3")

    def test_unapproved_manual_or_modified_text_records_are_rejected(self):
        for mutation in ("not_approved", "manual", "primary_text", "text_hash"):
            record = copy.deepcopy(self.records[0])
            if mutation == "not_approved":
                record["embedding_allowed"] = False
            elif mutation == "manual":
                record["input_binding_status"] = "manual_export_unverified"
            elif mutation == "primary_text":
                record["primary_text"] = "different rule"
            else:
                record["text_sha256"] = "0" * 64
            with self.subTest(mutation=mutation), self.assertRaises(gate.GateError):
                gate.build_reviewed_units([record])

    def test_duplicate_primary_or_foreign_attachment_context_is_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.build_reviewed_units([self.records[0], self.records[0]])
        record = copy.deepcopy(self.records[0])
        record["required_contexts"][0]["source_refs"][0]["attachment_id"] = "foreign:attachment"
        with self.assertRaises(gate.GateError):
            gate.build_reviewed_units([record])

    def test_adapter_does_not_mutate_input_and_omits_vectors_and_scores(self):
        records = copy.deepcopy(self.records[:1])
        records[0].update(embedding=[0.1] * 1024, score=0.99, rank=1)
        before = copy.deepcopy(records)
        payload, units = gate.reviewed_gate_payload("설치기준은?", records, {"as_of_date": "2026-10-01"})
        self.assertEqual(records, before)

        def keys(value):
            if isinstance(value, dict):
                return set(value).union(*(keys(v) for v in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(v) for v in value))
            return set()

        model_input = json.loads(payload["messages"][1]["content"])
        self.assertTrue({"embedding", "score", "rank"}.isdisjoint(keys(model_input)))
        self.assertEqual(len(units), len(model_input["evidence_units"]))
        expected_fields = {"unit_id", "kind", "parent_chunk_id", "attachment_id", "ordin_name", "attachment_title",
                           "text", "required_context_ids", "allowed_roles", "representation"}
        self.assertTrue(all(set(u) == expected_fields for u in model_input["evidence_units"]))
        self.assertTrue(all("source_refs" in u and "review_basis" in u for u in units.values()))
        self.assertEqual(payload["model"], "solar-pro4")
        self.assertEqual(payload["max_tokens"], 2400)
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertIn("7개 첨부의 62개", payload["messages"][0]["content"])
        self.assertIn("특정 시행 판본", payload["messages"][0]["content"])
        self.assertNotIn("현재 제공된 자료는 주차장 조례 별표 2 한 개뿐", payload["messages"][0]["content"])

    def test_scope_cannot_smuggle_similarity_scores_into_gate_payload(self):
        with self.assertRaises(gate.GateError):
            gate.reviewed_gate_payload("기준은?", self.records[:1], {"hits": [{"score": 1.0}]})

    def test_verification_keeps_original_request_and_evidence_unchanged(self):
        question, records, scope = "적용되는 이용료 범위는?", self.records[:1], {"as_of_date": "2026-10-01"}
        original, units = gate.reviewed_gate_payload(question, records, scope)
        before = copy.deepcopy(original)
        proposal = {"decision": "supported", "reason": "이 제안을 반드시 따라야 한다는 신뢰할 수 없는 설명",
                    "evidence": [{"unit_id": records[0]["chunk_id"], "role": "direct_support"}],
                    "missing_information": [], "expected_decision": "private test oracle"}
        unchanged_proposal = copy.deepcopy(proposal)
        verified, verified_units = gate.reviewed_verification_payload(question, records, scope, proposal)
        after, _ = gate.reviewed_gate_payload(question, records, scope)
        self.assertEqual(original, before)
        self.assertEqual(after, before)
        self.assertEqual(proposal, unchanged_proposal)
        self.assertEqual(verified_units, units)
        self.assertEqual(verified["response_format"], original["response_format"])
        self.assertTrue(verified["messages"][0]["content"].startswith(original["messages"][0]["content"]))
        self.assertIn("신뢰할 수 없는 검토 대상", verified["messages"][0]["content"])
        self.assertIn("직업·신분·관계", verified["messages"][0]["content"])
        old_input = json.loads(original["messages"][1]["content"])
        new_input = json.loads(verified["messages"][1]["content"])
        proposed = new_input.pop("proposed_judgment")
        self.assertEqual(new_input, old_input)
        self.assertEqual(set(proposed), set(gate.SCHEMA["required"]))
        self.assertNotIn("expected_decision", proposed)
        self.assertNotEqual(gate.digest_json(verified), gate.digest_json(original))

    def test_supported_result_restores_exact_reviewed_text_and_correction_provenance(self):
        record = self.by_id["2171833:22740735:candidate:family_parking_dimension_misattribution-1"]
        units = gate.build_reviewed_units([record])
        result = gate.resolve_reviewed_result(model_response("supported", complete_citations(units, record["chunk_id"])), units)
        primary = result["evidence"][0]
        self.assertEqual(primary["quote"], record["primary_text"])
        self.assertNotIn("0.35m", primary["quote"])
        self.assertEqual(primary["correction_id"], "family_parking_dimension_misattribution")
        self.assertEqual(primary["source_refs"], record["source_refs"])
        self.assertEqual(primary["parent_chunk_id"], record["chunk_id"])
        self.assertFalse(result["answer_allowed"])
        self.assertEqual(result["legal_applicability"], "not_determined")

    def test_missing_mandatory_condition_is_rejected(self):
        record = self.records[0]
        units = gate.build_reviewed_units([record])
        citations = complete_citations(units, record["chunk_id"])
        citations.pop()
        with self.assertRaisesRegex(gate.GateError, "missing_parent_condition"):
            gate.resolve_reviewed_result(model_response("supported", citations), units)

    def test_context_cannot_be_direct_support_even_with_its_primary_present(self):
        record = self.records[0]
        units = gate.build_reviewed_units([record])
        citations = complete_citations(units, record["chunk_id"])
        citations[-1]["role"] = "direct_support"
        with self.assertRaisesRegex(gate.GateError, "invalid_reviewed_evidence_role"):
            gate.resolve_reviewed_result(model_response("supported", citations), units)

    def test_reference_only_clarification_is_not_a_supported_rule(self):
        record = self.by_id["2147709:22416027:candidate:delegation_table_vertical_alignment_lost-2"]
        units = gate.build_reviewed_units([record])
        reference = next(u for u in units.values() if u["kind"] == "additional_context")
        citations = [{"unit_id": reference["unit_id"], "role": "applicability"}]
        with self.assertRaisesRegex(gate.GateError, "reviewed_decision_without_primary"):
            gate.resolve_reviewed_result(model_response("needs_clarification", citations, ["어떤 시설인가요?"]), units)

    def test_other_rows_context_cannot_be_borrowed_without_its_owner(self):
        records = [self.by_id[f"2171833:22740729:candidate:p1-e2-r{i}"] for i in (1, 2)]
        units = gate.build_reviewed_units(records)
        citations = complete_citations(units, records[0]["chunk_id"])
        citations.append({"unit_id": units[records[1]["chunk_id"]]["required_context_ids"][0], "role": "exception"})
        with self.assertRaisesRegex(gate.GateError, "reviewed_context_without_own_primary"):
            gate.resolve_reviewed_result(model_response("supported", citations), units)

    def test_duplicate_evidence_and_model_written_quotes_are_rejected(self):
        record = self.records[0]
        units = gate.build_reviewed_units([record])
        for mutation in ("duplicate", "quote"):
            citations = complete_citations(units, record["chunk_id"])
            if mutation == "duplicate":
                citations.append(copy.deepcopy(citations[0]))
            else:
                citations[0]["quote"] = "model invented a quote"
            with self.subTest(mutation=mutation), self.assertRaises(gate.GateError):
                gate.resolve_reviewed_result(model_response("supported", citations), units)

    def test_negative_decisions_keep_empty_citations_and_answer_block(self):
        units = gate.build_reviewed_units(self.records[:1])
        for decision in ("insufficient_evidence", "out_of_scope"):
            result = gate.resolve_reviewed_result(model_response(decision, []), units)
            self.assertEqual(result["decision"], decision)
            self.assertEqual(result["evidence"], [])
            self.assertFalse(result["answer_allowed"])

    def supported_for(self, *identifiers):
        units = gate.build_reviewed_units([self.by_id[ident] for ident in identifiers])
        citations = [citation for ident in identifiers for citation in complete_citations(units, ident)]
        return gate.resolve_reviewed_result(model_response("supported", citations), units)

    def test_policy_does_not_turn_representation_review_into_legal_compliance(self):
        ident = "2171833:22740735:candidate:family_parking_dimension_misattribution-1"
        original = self.supported_for(ident)
        before = copy.deepcopy(original)
        questions = [
            "가족배려주차장 확장형 1면의 중앙여유공간을 0.35m로 시공하면 설치 의무를 충족하는지 확정해 주세요.",
            "이 방식으로 설치하는 것이 적법한지 판단해 주세요.",
            "이번 시설의 설치 의무 충족 여부를 판단해 주세요.",
        ]
        for question in questions:
            with self.subTest(question=question):
                result, info = gate.apply_reviewed_policy(question, original, self.records)
                self.assertEqual(result["decision"], "insufficient_evidence")
                self.assertEqual(result["evidence"], [])
                self.assertEqual(result["missing_information"], [])
                self.assertEqual(info["guard"], "legal_applicability_not_determined")
                self.assertTrue(info["skipped_expansion"])
                self.assertFalse(result["answer_allowed"])
        self.assertEqual(original, before)

    def test_policy_does_not_assign_unknown_person_labels_to_tariff_categories(self):
        ident = "2147709:22415979:candidate:p1-e5-r7"
        original = self.supported_for(ident)
        for label in ("학생", "친구", "동행인", "성인"):
            question = f"서울대공원 캠핑장에 어른 1명과 {label} 1명이 갑니다. 두 사람의 기본 이용료 합계 범위는?"
            with self.subTest(label=label):
                result, info = gate.apply_reviewed_policy(question, original, self.records)
                self.assertEqual(result["decision"], "needs_clarification")
                self.assertEqual(result["evidence"], original["evidence"])
                self.assertEqual(info["unknown_labels"], [label])
                self.assertEqual(set(info["categories"]), {"어른", "청소년", "어린이"})
                self.assertEqual(info["facility_groups"][0]["attachment_id"], "2147709:22415979")
                self.assertTrue(info["skipped_expansion"])
                self.assertTrue(any(label in text for text in result["missing_information"]))
                self.assertFalse(result["answer_allowed"])

    def test_policy_accepts_explicit_known_categories_and_ignores_aggregate_count_labels(self):
        original = self.supported_for("2147709:22415979:candidate:p1-e5-r7",
                                      "2147709:22415979:candidate:p1-e5-r8")
        question = "서울대공원 캠핑장에 어른 1명과 청소년 1명, 총 2명이 갑니다. 기본 이용료 합계 범위는?"
        result, info = gate.apply_reviewed_policy(question, original, self.records)
        self.assertIsNone(info)
        self.assertIs(result, original)

    def test_policy_leaves_general_fee_and_quoted_dimension_queries_alone(self):
        questions = [("2147709:22415979:candidate:p1-e5-r7", "서울대공원 캠핑장 어른 1회 기본 이용료 범위를 인용해 주세요."),
                     ("2171833:22740735:candidate:family_parking_dimension_misattribution-1", "확장형 1면 도면의 가로와 세로 치수는 얼마인가요?")]
        for ident, question in questions:
            original = self.supported_for(ident)
            with self.subTest(question=question):
                result, info = gate.apply_reviewed_policy(question, original, self.records)
                self.assertIsNone(info)
                self.assertIs(result, original)

    def test_policy_reads_all_tariff_lines_in_the_forest_primary(self):
        ident = "2147709:22415979:candidate:p2-e7-r4"
        self.assertEqual(self.by_id[ident]["primary_text"].count("사용기준:"), 3)
        original = self.supported_for(ident)
        result, info = gate.apply_reviewed_policy("청소년 체험의 숲에 어른 1명과 청소년 1명이 갑니다. 기본 이용료 합계는?",
                                                  original, self.records)
        self.assertIsNone(info)
        self.assertIs(result, original)

    def test_policy_does_not_treat_a_single_place_unit_as_person_categories(self):
        ident = "2147709:22415979:candidate:p1-e5-r1"
        original = self.supported_for(ident)
        self.assertIn("야영장소1회당", self.by_id[ident]["primary_text"])
        result, info = gate.apply_reviewed_policy("친구 2명의 야영장소 이용료 합계는?", original, self.records)
        self.assertIsNone(info)
        self.assertIs(result, original)

    def test_policy_does_not_borrow_categories_from_a_different_facility(self):
        ident = "2147709:22415979:candidate:p1-e5-r7"
        original = self.supported_for(ident)
        extra = copy.deepcopy(self.by_id[ident])
        extra["chunk_id"] += ":other-facility"
        extra["primary_text"] = extra["primary_text"].replace("서울대공원 캠핑장", "다른 시설").replace("어른 1회당", "친구 1회당")
        result, info = gate.apply_reviewed_policy("어른 1명과 친구 1명의 이용료 합계는?", original, [*self.records, extra])
        self.assertEqual(result["decision"], "needs_clarification")
        self.assertEqual(info["unknown_labels"], ["친구"])
        self.assertNotIn("친구", info["categories"])

    def test_policy_categories_are_dynamic_and_not_a_fixed_age_vocabulary(self):
        ident = "2147709:22415979:candidate:p1-e5-r7"
        # A synthetic reviewed table uses an additional, explicitly named fare
        # category. The guard must read that table, not a hard-coded age list.
        extra = copy.deepcopy(self.by_id[ident])
        extra["chunk_id"] += ":synthetic-category"
        extra["primary_text"] = extra["primary_text"].replace("어른 1회당", "방문자 1회당")
        original = self.supported_for(ident)
        result, info = gate.apply_reviewed_policy("어른 1명과 방문자 1명의 이용료 합계는?", original, [*self.records, extra])
        self.assertIsNone(info)
        self.assertIs(result, original)

    def test_policy_leaves_non_supported_decisions_unchanged(self):
        ident = "2147709:22415979:candidate:p1-e5-r7"
        units = gate.build_reviewed_units([self.by_id[ident]])
        for decision in ("insufficient_evidence", "needs_clarification", "out_of_scope"):
            citations = complete_citations(units, ident) if decision == "needs_clarification" else []
            missing = ["필요한 연령 구분을 알려주세요."] if decision == "needs_clarification" else []
            original = gate.resolve_reviewed_result(model_response(decision, citations, missing), units)
            with self.subTest(decision=decision):
                result, info = gate.apply_reviewed_policy("설치 의무 충족 여부를 확정해 주세요.", original, self.records)
                self.assertIsNone(info)
                self.assertIs(result, original)

@unittest.skipUnless((PROJECT / "data/processed/attachments/2026-10-01/embeddings/embeddings.manifest.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/processed/attachments/2026-10-01/embeddings/embeddings.manifest.json")
class ReviewedSearchIntegrationTests(unittest.TestCase):
    TARGET_ID = "2147709:22415979:candidate:p1-e5-r7"
    POSITIVE_QUESTION = "서울대공원 캠핑장 어른 1회 이용료 범위를 인용해 주세요."
    TEMP_PREFIX = "reviewed-search-test-"

    def setUp(self):
        self.test_root = Path(__file__).resolve().parent
        self.project = Path(tempfile.mkdtemp(prefix=self.TEMP_PREFIX, dir=self.test_root)).resolve()
        self.addCleanup(self.cleanup_project)
        network = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Live HTTP is forbidden"))
        network.start()
        self.addCleanup(network.stop)
        values = patch.object(dotenv, "dotenv_values", side_effect=AssertionError("No environment file may be read"))
        values.start()
        self.addCleanup(values.stop)
        old_dotenv = patch.object(gate, "load_dotenv", side_effect=AssertionError("No environment file may be read"))
        old_dotenv.start()
        self.addCleanup(old_dotenv.stop)
        key = patch.object(search.os, "getenv", return_value="offline-search-test-only")
        self.getenv = key.start()
        self.addCleanup(key.stop)
        provider = patch.object(search, "call_reviewed_api", side_effect=self.fake_api)
        self.api = provider.start()
        self.addCleanup(provider.stop)

        manifest_path = embedding.BATCH_FOLDER + "/embeddings.manifest.json"
        manifest_raw = (PROJECT / manifest_path).read_bytes()
        manifest = json.loads(manifest_raw)
        self.inputs = {**manifest["input_files"], manifest_path: self.digest(manifest_raw)}
        self.inputs.update({embedding.BATCH_FOLDER + "/" + name: expected
                            for name, expected in manifest["output_files"].items()})
        self.inputs[search.CASES_FILE] = search.CASES_SHA256
        for relative, expected in self.inputs.items():
            rel = Path(relative)
            source = (PROJECT / rel).resolve(strict=True)
            target = (self.project / rel).resolve()
            self.assertEqual(rel.parts[0], "data")
            self.assertTrue(source.is_relative_to(PROJECT))
            self.assertTrue(target.is_relative_to(self.project))
            self.assertEqual(self.digest(source.read_bytes()), expected)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self.before = self.input_snapshot()
        vectors = json.loads((self.project / embedding.BATCH_FOLDER / "embeddings.json").read_bytes())["vectors"]
        self.query_vector = next(v["embedding"] for v in vectors if v["chunk_id"] == self.TARGET_ID)
        self.calls = []
        self.gate_mode = "supported"
        self.query_mutation = None
        self.gate_mutation = None
        self.gate_scenario = None

    @staticmethod
    def digest(raw):
        return hashlib.sha256(raw).hexdigest()

    def cleanup_project(self):
        root, scope = self.project.resolve(), self.test_root.resolve()
        if root.parent != scope or not root.is_relative_to(scope) or not root.name.startswith(self.TEMP_PREFIX):
            raise AssertionError("Unsafe cleanup target: " + str(root))
        if root.exists():
            shutil.rmtree(root)

    def input_snapshot(self):
        return {rel: ((self.project / rel).read_bytes(), (self.project / rel).stat().st_mtime_ns)
                for rel in self.inputs}

    def search_snapshot(self):
        folder = self.project / search.SEARCH_FOLDER
        return {p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in folder.rglob("*.json")} if folder.exists() else {}

    def assert_no_question_outputs(self):
        folder = self.project / search.SEARCH_FOLDER / "queries"
        self.assertFalse(folder.exists() and any(folder.glob("*.json")))

    @staticmethod
    def object_keys(value):
        if isinstance(value, dict):
            return set(value).union(*(ReviewedSearchIntegrationTests.object_keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(ReviewedSearchIntegrationTests.object_keys(v) for v in value))
        return set()

    def fake_api(self, endpoint, payload_bytes, key):
        self.assertEqual(key, "offline-search-test-only")
        self.assertIsInstance(payload_bytes, bytes)
        payload = json.loads(payload_bytes)
        forbidden = {"expected_decision", "required_chunk_ids", "rationale", "score"}
        self.assertTrue(forbidden.isdisjoint(self.object_keys(payload)))
        if endpoint == search.API_URL:
            self.assertEqual(payload["model"], search.QUERY_MODEL)
            self.assertIn(len(payload["input"]), (1, 12))
            self.calls.append(("query", payload))
            response = {"model": search.QUERY_MODEL,
                        "data": [{"index": i, "embedding": list(self.query_vector)}
                                 for i in reversed(range(len(payload["input"])))],
                        "usage": {"prompt_tokens": 10, "total_tokens": 10}}
            if self.query_mutation:
                self.query_mutation(response)
            return response
        self.assertEqual(endpoint, gate.CHAT_URL)
        self.assertEqual(payload["model"], gate.MODEL)
        model_input = json.loads(payload["messages"][1]["content"])
        self.assertTrue(forbidden.isdisjoint(self.object_keys(model_input)))
        self.assertTrue({"embedding", "review_basis", "source_refs", "embedding_text_sha256"}.isdisjoint(
            self.object_keys(model_input["evidence_units"])))
        self.calls.append(("gate", model_input))
        if self.gate_scenario is not None:
            response = self.gate_scenario(model_input)
        elif self.gate_mode == "supported":
            units = {u["unit_id"]: u for u in model_input["evidence_units"]}
            self.assertIn(self.TARGET_ID, units)
            response = model_response("supported", complete_citations(units, self.TARGET_ID))
        else:
            response = model_response("insufficient_evidence", [])
        response["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        if self.gate_mutation:
            value = json.loads(response["choices"][0]["message"]["content"])
            self.gate_mutation(value)
            response["choices"][0]["message"]["content"] = json.dumps(value, ensure_ascii=False)
        return response

    def test_exact_primary_with_all_mandatory_citations_is_supported(self):
        output = search.search_reviewed(self.project, self.POSITIVE_QUESTION)
        result = output["result"]
        self.assertEqual(output["called"], 3)
        self.assertEqual(result["decision"], "supported")
        self.assertEqual(result["rounds"][0]["searched_chunk_count"], 5)
        self.assertEqual(result["hits"][0]["chunk_id"], self.TARGET_ID)
        primary = next(e for e in result["evidence"] if e["role"] == "direct_support")
        self.assertEqual(primary["unit_id"], self.TARGET_ID)
        self.assertIn("2,000~4,000", primary["quote"])
        self.assertFalse(result["answer_allowed"])
        self.assertFalse(result["answer_generated"])
        self.assertFalse(result["similarity_is_probability"])
        self.assertEqual(self.input_snapshot(), self.before)

    def test_negative_question_expands_5_to_62_and_still_has_no_evidence(self):
        self.gate_mode = "insufficient_evidence"
        output = search.search_reviewed(self.project, "서울대공원 캠핑장 취소수수료가 7,500원인가요?")
        result = output["result"]
        self.assertEqual(output["called"], 3)
        self.assertEqual([r["searched_chunk_count"] for r in result["rounds"]], [5, 62])
        self.assertEqual(len(result["hits"]), 5)
        self.assertEqual(result["decision"], "insufficient_evidence")
        self.assertEqual(result["evidence"], [])
        self.assertFalse(result["evidence_sufficient"])
        self.assertFalse(result["answer_allowed"])

    def test_cached_search_and_check_make_no_api_or_key_access_and_no_rewrites(self):
        first = search.search_reviewed(self.project, self.POSITIVE_QUESTION)
        before = self.search_snapshot()
        self.api.reset_mock()
        self.getenv.reset_mock()
        repeated = search.search_reviewed(self.project, self.POSITIVE_QUESTION)
        checked = search.search_reviewed(self.project, self.POSITIVE_QUESTION, check_only=True)
        self.assertEqual(repeated["called"], 0)
        self.assertEqual(checked["called"], 0)
        self.assertEqual(repeated["result"], first["result"])
        self.assertEqual(checked["result"], first["result"])
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assertEqual(self.search_snapshot(), before)
        self.assertEqual(self.input_snapshot(), self.before)

    def test_incomplete_attempt_is_not_retried_automatically(self):
        self.api.side_effect = ValueError("processing outcome unknown")
        with self.assertRaises(ValueError):
            search.search_reviewed(self.project, self.POSITIVE_QUESTION)
        saved = self.search_snapshot()
        self.assertEqual(len([name for name in saved if name.endswith(".attempt.json")]), 1)
        self.assertFalse(any(name.endswith(".response.json") for name in saved))
        self.api.reset_mock()
        self.getenv.reset_mock()
        with self.assertRaisesRegex(ValueError, "자동 재호출하지 않습니다"):
            search.search_reviewed(self.project, self.POSITIVE_QUESTION)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assertEqual(self.search_snapshot(), saved)
        self.assert_no_question_outputs()

    def test_modified_source_blocks_search_before_key_or_provider_access(self):
        relative = next(p for p in self.inputs if p.endswith(".xml"))
        target = self.project / relative
        target.write_bytes(target.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            search.search_reviewed(self.project, self.POSITIVE_QUESTION)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assert_no_question_outputs()

    def test_wrong_query_model_or_dimension_is_an_error_not_empty_evidence(self):
        mutations = {"model": lambda r: r.update(model=search.PASSAGE_MODEL),
                     "dimension": lambda r: r["data"][0]["embedding"].pop()}
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                self.query_mutation = mutate
                question = self.POSITIVE_QUESTION + " 오류시험 " + name
                self.api.reset_mock()
                with self.assertRaises(ValueError):
                    search.search_reviewed(self.project, question)
                self.assertEqual(self.api.call_count, 1)
                saved = self.search_snapshot()
                self.api.reset_mock()
                self.getenv.reset_mock()
                with self.assertRaises(ValueError):
                    search.search_reviewed(self.project, question)
                self.api.assert_not_called()
                self.getenv.assert_not_called()
                self.assertEqual(self.search_snapshot(), saved)
        self.assertFalse(any(kind == "gate" for kind, _ in self.calls))
        self.assert_no_question_outputs()

    def test_unknown_forbidden_or_incomplete_gate_evidence_is_an_error(self):
        mutations = {"unknown": lambda v: v["evidence"][0].update(unit_id="unknown:unit"),
                     "forbidden_role": lambda v: v["evidence"][-1].update(role="direct_support"),
                     "missing_context": lambda v: v["evidence"].pop()}
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                self.gate_mutation = mutate
                question = self.POSITIVE_QUESTION + " 오류시험 " + name
                self.api.reset_mock()
                with self.assertRaises(gate.GateError):
                    search.search_reviewed(self.project, question)
                self.assertEqual(self.api.call_count, 3 if name == "missing_context" else 2)
                saved = self.search_snapshot()
                self.api.reset_mock()
                self.getenv.reset_mock()
                with self.assertRaises(gate.GateError):
                    search.search_reviewed(self.project, question)
                self.api.assert_not_called()
                self.getenv.assert_not_called()
                self.assertEqual(self.search_snapshot(), saved)
        self.assert_no_question_outputs()

    def test_evaluation_reports_wrong_supported_results_without_leaking_expected_answers(self):
        # Deliberately claim the adult camping rule supports every question.
        # Structural validity must not be confused with evaluation correctness.
        cases = json.loads((self.project / search.CASES_FILE).read_bytes())["cases"]
        report = search.run_search_evaluation(self.project)
        self.assertEqual(report["case_count"], 12)
        # Two general policy guards correct the known legal-applicability and
        # unclassified-person cases despite the mock model's repeated support.
        policy_corrected = {"E06", "E10"}
        passed = {"E01", *policy_corrected}
        self.assertEqual(report["passed_count"], len(passed))
        self.assertEqual(report["false_supported_count"], sum(c["expected_decision"] != "supported" for c in cases) - len(policy_corrected))
        self.assertEqual(set(report["failed_case_ids"]), {c["case_id"] for c in cases if c["case_id"] not in passed})
        self.assertEqual([len(p["input"]) for kind, p in self.calls if kind == "query"], [12])
        self.assertEqual(len([p for kind, p in self.calls if kind == "gate"]), 24)
        self.assertFalse(report["answer_allowed"])
        results = json.loads((self.project / search.SEARCH_FOLDER / "evaluation_results.json").read_bytes())
        self.assertEqual({row["case_id"] for row in results["cases"] if row["passed"]}, passed)
        self.assertEqual(self.input_snapshot(), self.before)

    def test_evaluation_records_repeated_invalid_insufficient_response_and_replays_cache(self):
        cases = json.loads((self.project / search.CASES_FILE).read_bytes())["cases"]
        failing_case = next(case for case in cases if case["case_id"] == "E12")
        events = []

        def invalid_secondary_scenario(model_input):
            units = {unit["unit_id"]: unit for unit in model_input["evidence_units"]}
            if model_input["question"] != failing_case["question"]:
                return model_response("supported", complete_citations(units, self.TARGET_ID))
            count = sum(unit["kind"] == "primary" for unit in units.values())
            reviewing = "proposed_judgment" in model_input
            events.append((count, reviewing))
            if count == 5:
                return model_response("insufficient_evidence", [])
            # Preserve the actual failure shape: the model reports insufficient
            # evidence but still emits a citation and a missing-information list.
            return model_response("insufficient_evidence",
                                  [{"unit_id": self.TARGET_ID, "role": "direct_support"}],
                                  ["재검토에서도 요청 판본 확인이 필요합니다." if reviewing
                                   else "요청한 시행 판본 확인이 필요합니다."])

        self.gate_scenario = invalid_secondary_scenario
        report = search.run_search_evaluation(self.project)
        self.assertEqual(report["case_count"], 12)
        self.assertEqual(report["validation_error_count"], 1)
        self.assertIn("E12", report["failed_case_ids"])
        self.assertEqual(events, [(5, False), (62, False), (62, True)])
        self.assertFalse(report["answer_allowed"])
        self.assertFalse(report["answer_generated"])
        folder = self.project / search.SEARCH_FOLDER
        results = json.loads((folder / "evaluation_results.json").read_bytes())
        self.assertEqual(len(results["cases"]), 12)
        self.assertEqual({row["case_id"] for row in results["cases"]}, {case["case_id"] for case in cases})
        failed = next(row for row in results["cases"] if row["case_id"] == "E12")
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["expected_decision"], failing_case["expected_decision"])
        result = failed["result"]
        self.assertEqual(result["decision"], "validation_error")
        self.assertEqual(result["error_code"], "unexpected_missing_information")
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["missing_information"], [])
        self.assertFalse(result["evidence_sufficient"])
        self.assertFalse(result["answer_allowed"])
        self.assertIn("자료의 유무는 확정하지 않았습니다", result["user_message"])
        self.assertEqual([stage["searched_chunk_count"] for stage in result["rounds"]], [5, 62])
        stage = result["rounds"][-1]
        self.assertEqual(stage["initial_validation_error"], "unexpected_missing_information")
        self.assertIsNone(stage["verification"]["result"])
        manifest = json.loads((folder / "evaluation.manifest.json").read_bytes())
        for model_result, files in ((stage["initial_model_result"], stage["request_files"]),
                                    (stage["verification"]["model_result"], stage["verification"]["request_files"])):
            self.assertEqual(model_result["decision"], "insufficient_evidence")
            self.assertEqual(model_result["evidence"], [{"unit_id": self.TARGET_ID, "role": "direct_support"}])
            self.assertTrue(model_result["missing_information"])
            self.assertTrue(any(name.endswith(".attempt.json") for name in files))
            responses = [name for name in files if name.endswith(".response.json")]
            self.assertEqual(len(responses), 1)
            for name, expected in files.items():
                raw = (self.project / name).read_bytes()
                self.assertEqual(self.digest(raw), expected)
                self.assertEqual(manifest["input_files"][name], expected)
            receipt = json.loads((self.project / responses[0]).read_bytes())
            raw_model_result = json.loads(receipt["response"]["choices"][0]["message"]["content"])
            self.assertEqual(raw_model_result, model_result)
        for name, expected in manifest["output_files"].items():
            self.assertEqual(self.digest((folder / name).read_bytes()), expected)
        before = self.search_snapshot()
        preview_before = ((folder / "preview.html").read_bytes(), (folder / "preview.html").stat().st_mtime_ns)
        self.api.reset_mock()
        self.getenv.reset_mock()
        checked = search.run_search_evaluation(self.project, check_only=True)
        self.assertEqual(checked, {**report, "called": 0})
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assertEqual(self.search_snapshot(), before)
        self.assertEqual(((folder / "preview.html").read_bytes(), (folder / "preview.html").stat().st_mtime_ns), preview_before)
        self.assertEqual(self.input_snapshot(), self.before)

    def test_verification_rejects_assumed_age_and_expands_clarification_to_all_62(self):
        events = []

        def review_scenario(model_input):
            units = {u["unit_id"]: u for u in model_input["evidence_units"]}
            count = sum(u["kind"] == "primary" for u in units.values())
            verification = "proposed_judgment" in model_input
            events.append((count, verification))
            citations = complete_citations(units, self.TARGET_ID)
            if verification:
                self.assertEqual(model_input["proposed_judgment"]["decision"], "supported")
                return model_response("needs_clarification", citations, ["동행인의 연령 구간은 무엇인가요?"])
            return model_response("supported", citations)

        self.gate_scenario = review_scenario
        question = "서울대공원 캠핑장에 어른과 학생이 한 명씩 갑니다. 두 사람의 기본 이용료 합계 범위는?"
        output = search.search_reviewed(self.project, question)
        result = output["result"]
        self.assertEqual(events, [(5, False), (5, True), (62, False), (62, True)])
        self.assertEqual(output["called"], 5)
        self.assertEqual([r["searched_chunk_count"] for r in result["rounds"]], [5, 62])
        self.assertEqual(result["decision"], "needs_clarification")
        self.assertEqual(result["missing_information"], ["동행인의 연령 구간은 무엇인가요?"])
        self.assertFalse(result["evidence_sufficient"])
        self.assertFalse(result["answer_allowed"])

    def test_policy_stops_repeated_support_for_unknown_person_category_without_expansion(self):
        output = search.search_reviewed(self.project,
            "서울대공원 캠핑장에 어른 1명과 학생 1명이 갑니다. 기본 이용료 합계 범위는?")
        result = output["result"]
        self.assertEqual(output["called"], 3)
        self.assertEqual([r["searched_chunk_count"] for r in result["rounds"]], [5])
        self.assertEqual(result["decision"], "needs_clarification")
        self.assertTrue(any("학생" in item for item in result["missing_information"]))
        round_info = result["rounds"][0]
        self.assertEqual(round_info["initial_model_result"]["decision"], "supported")
        self.assertEqual(round_info["verification"]["result"]["decision"], "supported")
        self.assertEqual(round_info["model_result_before_policy"]["decision"], "supported")
        self.assertEqual(round_info["policy_hold"]["unknown_labels"], ["학생"])
        self.assertTrue(round_info["policy_hold"]["skipped_expansion"])
        self.assertFalse(result["answer_allowed"])

    def test_out_of_scope_with_invalid_missing_information_is_reverified_strictly(self):
        events = []

        def repair_scenario(model_input):
            reviewing = "proposed_judgment" in model_input
            events.append(reviewing)
            if reviewing:
                self.assertEqual(model_input["proposed_judgment"]["decision"], "out_of_scope")
                self.assertTrue(model_input["proposed_judgment"]["missing_information"])
                return model_response("out_of_scope", [])
            return model_response("out_of_scope", [], ["어떤 개정 판본인지 알려주세요."])

        self.gate_scenario = repair_scenario
        output = search.search_reviewed(self.project, "2027년 1월 1일 시행 개정 조례의 서울대공원 캠핑장 어른 이용료는?")
        result = output["result"]
        self.assertEqual(events, [False, True])
        self.assertEqual(output["called"], 3)
        self.assertEqual(result["decision"], "out_of_scope")
        self.assertEqual(result["missing_information"], [])
        self.assertEqual(result["evidence"], [])
        round_info = result["rounds"][0]
        self.assertEqual(round_info["initial_validation_error"], "unexpected_missing_information")
        self.assertTrue(round_info["initial_model_result"]["missing_information"])
        self.assertIsNotNone(round_info["verification"])
        self.assertFalse(result["answer_allowed"])

if __name__ == "__main__":
    unittest.main()
