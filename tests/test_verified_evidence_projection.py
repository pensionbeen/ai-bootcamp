import copy
import json
import unittest
from unittest.mock import patch

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from local_rag_answer import _attach_named_provisions, _attach_quoted_sibling, _normalize_claim_layout, assess_and_answer
from public_rag_response import _delegated_hours, hours_and_funding_claims, public_result
from test_local_rag_answer import ANSWER, FakeClient, generation, replace_source_quote, retrieval


def provision(uid, paragraph, quote):
    return {"unit_id": uid, "parent_chunk_id": uid, "kind": "body", "source_url": "https://example.test/law",
            "effective_date": "2026-10-01", "ordin_seq": "123", "quote": quote,
            "locator": {"article": "제4조", "paragraph": paragraph}}


class CitationProjectionTests(unittest.TestCase):
    def test_owned_full_paragraph_is_materialized_only_for_its_complete_statement(self):
        name = "서울특별시 청년 기본 조례"
        owner = {**provision("p1", 1, "① 시장은 사업을 운영한다."), "ordin_seq": "2054497",
                 "ordin_name": name, "as_of_date": "2026-10-01"}
        source = " ② 시장은 예산의 범위에서 사업을 지원할 수 있다.\n"
        context = {**owner, "unit_id": "full", "kind": "required_context", "quote": source,
                   "source_segments": [{"kind": "provision", "paragraph": 2, "text": source,
                                        "start": 0, "end": len(source)}]}
        cite = name + " 제4조 제2항"
        text = "시장은 예산의 범위에서 사업을 지원할 수 있습니다. (" + cite + ")"

        def attach(body, catalog):
            claim = {"text": body, "citation_ids": ["p1"]}
            rows = [owner]
            repairs = _attach_named_provisions([claim], rows, catalog)
            return claim, rows, repairs

        for body in (text, "예산 내에서 지원받을 수 있습니다. " + text,
                     "「" + name + "」 제4조 제2항에 따르면, 시장은 예산의 범위에서 사업을 지원할 수 있습니다.",
                     cite + "은 시장이 예산의 범위에서 사업을 지원할 수 있다고 규정하고 있다."):
            claim, rows, repairs = attach(body, {"H001": context})
            self.assertEqual(claim["citation_ids"], ["p1", "full::paragraph::2"])
            self.assertEqual(rows[-1]["quote"], source)
            self.assertEqual(repairs[0]["context_id"], "H001")
            from evaluate_local_rag import _citation_repair_additions
            audit = {"generated_claims": [claim], "citation_repairs": repairs, "context_source_catalog": {"H001": context}}
            by_id = {row["unit_id"]: row for row in rows}
            self.assertIsNotNone(_citation_repair_additions(audit, by_id))
            tampered = {**by_id, rows[-1]["unit_id"]: {**rows[-1], "quote": "다른 원문."}}
            self.assertIsNone(_citation_repair_additions(audit, tampered))
        for body, catalog in ((text.replace("예산의 범위에서 ", ""), {"H001": context}),
                              (text.replace("할 수 있습니다", "하여야 합니다"), {"H001": context}),
                              (text.replace("있습니다.", "있다는 뜻은 아닙니다."), {"H001": context}),
                              ("신청하면 반드시 지원받습니다. " + text, {"H001": context}),
                              ("형편이 어려워도 지원받을 수 없습니다. " + text, {"H001": context}),
                              (text, {"H001": context, "H002": context}),
                              (text, {"H001": {**context, "effective_date": "2020-01-01"}}),
                              (text, {"H001": {**context, "parent_chunk_id": "unrelated"}})):
            self.assertEqual(attach(body, catalog)[0]["citation_ids"], ["p1"])

    def test_exact_second_sentence_needs_its_own_unique_provision(self):
        source = provision("p1", 1, "① 의장은 위원회를 대표한다.")
        next_source = provision("p2", 2, "② 먼저 위촉직을 선임하고, 부의장은 재적 위원 중에서 선출한다.")
        text = "의장은 위원회를 대표한다. 부의장은 재적 위원 중에서 선출한다."
        def apply(rows):
            claim = {"text": text, "citation_ids": ["p1"]}
            repairs = _attach_quoted_sibling([claim], [source, *rows])
            return claim["citation_ids"], repairs
        ids, repairs = apply([next_source])
        self.assertEqual(ids, ["p1", "p2"])
        self.assertEqual(repairs[0]["reference_text"], text.split(". ")[1][:-1])
        self.assertEqual(apply([next_source, {**next_source, "unit_id": "p3"}])[0], ["p1"])
        self.assertEqual(apply([{**next_source, "effective_date": "2020-01-01"}])[0], ["p1"])
        for quote in ("② 긴급한 경우 먼저 위촉직을 선임하고, 부의장은 재적 위원 중에서 선출한다.",
                      "② 부의장은 재적 위원 중에서 선출할 수 있다."):
            self.assertEqual(apply([{**next_source, "quote": quote}])[0], ["p1"])


class DraftLayoutTests(unittest.TestCase):
    def test_only_an_exact_cited_item_can_change_list_numbering(self):
        row = provision("item", 1, "2. 활동 공간을 지원한다.")
        row["locator"]["item"] = "2"
        def normalize(text, ids):
            claim = {"text": text, "citation_ids": ids}
            _normalize_claim_layout([claim], {"item": row})
            return claim["text"]
        self.assertEqual(normalize(row["quote"], ["item"]), "제2호 활동 공간을 지원한다.")
        for text, ids in ((row["quote"], []), ("3. 활동 공간을 지원한다.", ["item"]),
                          ("2. 활동 공간을 지원할 수 있다.", ["item"])):
            self.assertEqual(normalize(text, ids), text)

    def test_whitespace_repair_needs_verified_evidence_and_preserves_the_receipt(self):
        from evaluate_local_rag import source_selection_errors
        for checker in (None, RuntimeError("timeout"), {"provision_id": "NONE", "basis": "no_provision",
                                                        "asked_matter": "청년의 나이"}):
            client = FakeClient(generation(ANSWER + "\n"), provision=[checker] if checker else [])
            result = assess_and_answer("청년의 나이 기준은?", retrieval(), client)
            self.assertEqual(result["audit"][0]["parsed_response"]["claims"][0]["text"], ANSWER + "\n")
            self.assertEqual(result["claim_layout_changes"], [{"claim_index": 0, "reason": "outer_whitespace"}])
            self.assertEqual(result["generated_claims"][0]["text"], ANSWER)
            self.assertEqual(source_selection_errors(result), [])
            if checker:
                self.assertEqual(result["error_code"], "unconfirmed_repaired_draft")
                self.assertNotEqual(result["draft_status"], "withheld_no_provision")
            else:
                self.assertEqual(result["draft_status"], "code_checked_draft")
                bad = copy.deepcopy(result)
                bad["generated_claims"][0]["text"] += " 조건을 삭제한다."
                self.assertIn("source_selection_text_changed", source_selection_errors(bad))


class DelegatedOperatingHoursTests(unittest.TestCase):
    def setUp(self):
        self.rule = provision("p1", 1, "① 체육관의 설치 및 운영에 필요한 사항은 시장이 따로 정할 수 있다.")
        self.other = provision("p2", 2, "② 시장은 체육시설을 설치할 수 있다.")
        self.rows = [self.rule, self.other]
        check = {"basis": "delegates_elsewhere", "provision_id": "C2", "asked_matter": "운영 시간"}
        self.result = {"decision": "supported", "draft_status": "code_checked_draft",
                       "audit": [{"stage": "provision_check", "parsed_response": dict(check), "response": {}}],
                       "provision_check": check, "citation_aliases": {"C2": "p1"}, "source_citations": self.rows}

    def test_verified_full_rule_preserves_its_discretion_for_a_new_facility(self):
        for question in ("서울시 체육관은 몇 시까지 운영하나요?", "체육관의 문 여는 시간이 정해져 있나요?"):
            text, row = _delegated_hours(question, self.result, self.rows)
            self.assertIn("구체적인 운영 시간", text)
            self.assertIn("시장이 따로 정할 수 있습니다", text)
            self.assertEqual(row, [self.rule])

    def test_multiple_issues_concrete_schedule_and_conditional_delegation_keep_existing_answer(self):
        question = "체육관은 몇 시까지 운영하나요?"
        for mixed in ("체육관은 몇 시까지 운영하나요? 입장료도 알려 주세요.", "다른 시설은 몇 시까지 운영하나요?",
                      "체육관의 운영시간은 몇 시이고 신청도 가능한가요?"):
            self.assertIsNone(_delegated_hours(mixed, self.result, self.rows))
        for quote in ("② 매일 오전에 개방한다.", "② 개방시간은 별표에 따른다."):
            self.assertIsNone(_delegated_hours(question, {**self.result, "source_citations": [
                self.rule, {**self.other, "quote": quote}]}, self.rows))
        conditional = {**self.rule, "quote": self.rule["quote"] + " 다만, 공휴일은 제외한다."}
        self.assertIsNone(_delegated_hours(question, self.result, [conditional, self.other]))
        bad = copy.deepcopy(self.result)
        bad["provision_check"]["provision_id"] = "C3"
        self.assertIsNone(_delegated_hours(question, bad, self.rows))

    def test_schedule_and_funding_are_two_separately_sourced_proposals(self):
        grant = provision("grant", 4, "④ 군수가 아닌 자가 체육관을 설치ㆍ운영할 때 예산의 범위 안에서 비용을 융자할 수 있다.")
        originals = [self.rule, grant]
        aliases = {"H": "p1", "F": "grant"}
        for question in ("체육관은 몇 시에 문을 닫나요? 체육관 비용을 융자할 수 있나요?",
                         "체육관 설치 비용을 융자할 수 있나요? 운영시간은 누가 정하나요?"):
            proposed = hours_and_funding_claims(question, originals, originals, aliases)
            self.assertEqual([c["citation_ids"] for c in proposed], [["H"], ["F"]])
            self.assertIn("구체적인 운영 시간은 이 조항에 나와 있지 않습니다", proposed[0]["text"])
            self.assertIn("정할 수 있습니다", proposed[0]["text"])
            self.assertEqual(proposed[1]["text"], "군수가 아닌 자가 체육관을 설치ㆍ운영할 때 예산의 범위 안에서 비용을 융자할 수 있습니다.")
        different_date = {**grant, "effective_date": "2024-01-02"}
        for candidates, sources in (([self.rule], originals), ([self.rule, different_date], [self.rule, different_date]),
                                    (originals, originals + [provision("clock", 5, "⑤ 공휴일에는 오후에 개방한다.")])):
            self.assertIsNone(hours_and_funding_claims(question, candidates, sources, aliases))

    def test_optional_proposals_cannot_push_a_valid_request_over_its_input_limit(self):
        from local_rag_answer import MAX_INPUT_CHARACTERS
        q = "청년의 나이 기준은?"
        regular = FakeClient(generation(ANSWER))
        before = assess_and_answer(q, retrieval(), regular)
        proposal = [{"text": "가" * MAX_INPUT_CHARACTERS, "citation_ids": ["C001"]}]
        fallback = FakeClient(generation(ANSWER))
        with patch("local_rag_answer.hours_and_funding_claims", return_value=proposal):
            after = assess_and_answer(q, retrieval(), fallback)
        self.assertEqual(before["draft_status"], "code_checked_draft")
        self.assertEqual(regular.calls, fallback.calls)
        self.assertEqual(after["draft_response"], before["draft_response"])
        self.assertEqual(after["generation_input_sha256"], before["generation_input_sha256"])
        self.assertNotIn("proposed_claims", after)

    def test_expense_for_another_location_or_only_an_extension_preserves_the_general_path(self):
        grant = provision("grant", 4, "④ 시장은 자치구에 대하여 예산의 범위에서 체육관 설치에 필요한 경비를 지원할 수 있다.")
        sources = [self.rule, grant]
        for q in ("체육관 운영시간을 늘리면 인건비를 지원하나요?",
                  "서울시 체육관은 몇 시에 문을 닫나요? 부산시 체육관 설치비를 지원하나요?",
                  "체육관은 몇 시에 문을 닫나요? 도서관 시설의 증설 비용도 지원하나요?",
                  "체육관은 몇 시에 문을 닫나요? 그 체육관 맞은편 도서관 설치비도 지원하나요?",
                  "체육관의 운영시간과 동물병원 진료비 지원이 각각 궁금합니다."):
            self.assertIsNone(hours_and_funding_claims(q, sources, sources, {"H": "p1", "F": "grant"}), q)

    def test_an_elided_facility_in_the_ordinance_cannot_authorize_an_unrelated_repair_centre(self):
        hours = provision("h", 1, "① 농기계보관소·수리센터의 운영방법 등은 군수가 따로 정한다.")
        funding = provision("g", 2, "② 민간단체가 농기계수리센터를 설치하면 예산의 범위에서 그 비용을 지원할 수 있다.")
        sources = [hours, funding]
        aliases = {"H": "h", "F": "g"}
        query = "농기계 수리센터는 몇 시까지 운영하나요? 민간단체가 센터를 설치하면 비용을 지원하나요?"
        self.assertTrue(hours_and_funding_claims(query, sources, sources, aliases))
        self.assertIsNone(hours_and_funding_claims(query.replace("농기계", "자동차"), sources, sources, aliases))

        # A list's full facility name can also occur in a separate paragraph
        # of the same owner's article. Check the actual recorded text span.
        funding = {**funding, "quote": funding["quote"].replace("농기계수리센터를", "농기계보관소·수리센터를")}
        full_text = "③ 농기계수리센터는 주민이 이용할 수 있다."
        context = {**hours, "kind": "required_context", "unit_id": "h::context::article", "source_unit_id": "article",
                   "quote": full_text, "source_segments": [
                       {"kind": "provision", "start": 0, "end": len(full_text), "text": full_text}]}
        sources = [hours, funding]
        self.assertTrue(hours_and_funding_claims(query, sources, sources + [context], aliases))
        for invalid in ({"parent_chunk_id": "other"}, {"effective_date": "2020-01-01"}, {"quote": "다른 원문"},
                        {"source_segments": [{**context["source_segments"][0], "end": 1}]}):
            self.assertIsNone(hours_and_funding_claims(query, sources, sources + [{**context, **invalid}], aliases))

    def test_visit_preamble_does_not_require_the_facilitys_actual_clock_time(self):
        query = "서울시 체육관에 저녁에 방문하려고 해요. 조례상 몇 시에 문을 닫나요?"
        hidden = {**self.result, "decision": "needs_clarification", "missing_information": ["방문할 시설"]}
        answer, row = _delegated_hours(query, hidden, [self.rule])
        self.assertEqual(row, [self.rule])
        self.assertIn("따로 정할 수 있습니다", answer)
        self.assertIn("시설 안내", answer)
        for text in ("② 운영은 18:30까지로 한다.", "② 공휴일은 휴장한다."):
            case = {**hidden, "source_citations": [self.rule, {**self.other,"quote":text}]}
            self.assertIsNone(_delegated_hours(query, case, [self.rule]))
        for modified in (query + " 입장료도 알려주세요.", query.replace("저녁에", "공휴일에"),
                         query.replace("체육관", "부산의 체육관")):
            self.assertIsNone(_delegated_hours(modified, hidden, [self.rule]))
        unverified = {**hidden, "audit":[{**hidden["audit"][0],"deadline":{"outcome":"timeout"}}]}
        self.assertIsNone(_delegated_hours(query, unverified, [self.rule]))

    def test_installation_and_hours_keep_both_owned_operative_provisions(self):
        grant = {**self.other, "quote": "② 시장 또는 구청장은 체육활동을 위하여 체육관을 설치 운영할 수 있다."}
        result = {**self.result, "source_citations": [self.rule, grant]}
        question = "체육관의 설치 주체와 운영시간을 알려 주세요."
        answer, rows = _delegated_hours(question, result, [self.rule, grant])
        self.assertEqual(rows, [self.rule, grant])
        self.assertIn("구체적인 운영 시간", answer)
        self.assertIn("시장 또는 구청장은", answer)
        self.assertIn("설치 운영할 수 있습니다.", answer)
        self.assertIsNone(_delegated_hours(question, result, [self.rule]))
        self.assertIsNone(_delegated_hours(question+" 비용도 알려 주세요.", result, [self.rule, grant]))
        self.assertIsNone(_delegated_hours("체육관은 군수도 설치할 수 있나요? 운영시간도 조례에 나오나요?",
                                          result, [self.rule, grant]))
        conditional = {**grant, "quote": grant["quote"]+" 다만, 위탁시설을 제외한다."}
        self.assertIsNone(_delegated_hours(question, {**result,"source_citations":[self.rule,conditional]},
                                          [self.rule, conditional]))

    def test_empty_draft_requires_independent_confirmation_of_identified_delegation(self):
        question = "체육관은 몇 시까지 운영하나요?"
        data = retrieval()
        record = data['evidence'][0]['source']['source_record']
        record.pop('item')
        record['paragraph'] = 1
        replace_source_quote(data, self.rule['quote'])

        class Checker(FakeClient):
            def request(client, kind, payload, timeout=None):
                if kind == "provision_check" and not client.provision:
                    unit = json.loads(payload["messages"][1]["content"])["citations"][0]
                    client.provision.append({"asked_matter": "운영 시간", "provision_id": unit["id"],
                                             "basis": "delegates_elsewhere"})
                return super().request(kind, payload, timeout)

        draft = generation(decision="insufficient_evidence", claims=[])
        for checker, expected in (([], "code_checked_draft"), ([RuntimeError("timeout")], "code_check_failed"),
                                  ([{"asked_matter": "운영 시간", "provision_id": "NONE",
                                     "basis": "no_provision"}], "code_check_failed")):
            client = Checker(draft, provision=checker)
            result = assess_and_answer(question, data, client)
            view = public_result({"status": "completed", "question": question, "result": result}, catalog={})
            self.assertEqual(view["status"], expected, result.get("error_code"))
            self.assertEqual(result["audit"][0]["parsed_response"]["decision"], "insufficient_evidence")
            if expected == "code_checked_draft":
                self.assertIn("따로 정할 수 있습니다", view["response"]["answer"])
                self.assertEqual(len(view["response"]["citation"]), 1)
            else:
                self.assertEqual(result["error_code"], "unconfirmed_delegation")
