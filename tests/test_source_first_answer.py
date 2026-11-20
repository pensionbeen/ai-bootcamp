import unittest
from unittest.mock import patch

import local_rag_answer as rag
from source_first_answer import resolve
from test_complete_selected_evidence import LAW, PRICE, fees, row
from test_local_rag_answer import ANSWER, FakeClient, retrieval


def facility_sources():
    rows = fees()
    rows[1]["quote"] = "[별표 4]\n공유연습실 요금표(제13조 관련)\n(단위 : 원)"
    rows[2]["quote"] = "공유연습실 이용요금은 무료로 한다. 다만, 위탁 시 별표 4의 범위에서 이용요금을 부과할 수 있다."
    rows[3]["quote"] = "(단위 : 원)"
    return rows


class FeeScheduleTests(unittest.TestCase):
    def test_rule_uses_linked_tariff_and_exact_amounts(self):
        rows = facility_sources()
        rows[0]["quote"] = "구분: 이용요금\n시간당: 3,140\n월 정기권: 9,300"
        plan = resolve(PRICE, rows)
        self.assertEqual(plan["used_ids"], ["table", "title", "article"])
        self.assertIn("3,140원", plan["answer"])
        self.assertIn("9,300원", plan["answer"])
        self.assertIn("부과할 수 있다", plan["answer"])

    def test_unresolved_notes_wrong_authority_or_units_defer(self):
        for index, new in [(3, "(단위 : 천원)"), (3, "주말은 요금의 두 배를 부과한다."),
                           (1, "[별표 4]\n공유연습실 요금표(제12조 관련)\n(단위 : 원)"),
                           (1, "[별표 4]\n공유연습실 요금표(제13조 관련)\n(단위 : 원) 주말 할증"),
                           (0, "구분: 이용요금\n시간당: 협의")]:
            with self.subTest(new=new):
                rows = facility_sources()
                rows[index]["quote"] = new
                self.assertIsNone(resolve(PRICE, rows))
        rows = facility_sources()
        rows.append(row("condition", "특별한 경우에는 시장의 결정을 따른다.", label=LAW + " 제13조 제2항"))
        self.assertIsNone(resolve(PRICE, rows))
        self.assertIsNone(resolve("주말에 공유연습실 이용료는 얼마인가요?", facility_sources()))


class SourceFirstDispatchTests(unittest.TestCase):
    def test_generated_claim_still_passes_standard_provision_veto(self):
        client = FakeClient(provision=[{"asked_matter": "조례상 연령", "provision_id": "NONE", "basis": "no_provision"}])
        def prepared(question, citations, originals, crosswalk, *args):
            alias = next(a for a, uid in crosswalk.items() if uid == citations[0]["unit_id"])
            return ({"decision": "supported", "missing_information": [], "missing_materials": [], "calculations": [],
                     "claims": [{"text": ANSWER, "citation_ids": [alias]}]}, {"kind": "single_benefit"})
        with patch("source_first_answer.prepare_draft", side_effect=prepared):
            result = rag.assess_and_answer("청년의 정의는?", retrieval(), client)
        self.assertEqual([c[0] for c in client.calls], ["provision_check"])
        self.assertEqual(result["code_checks"]["status"], "passed")
        self.assertEqual(result["draft_status"], "withheld_no_provision")


if __name__ == "__main__":
    unittest.main()
