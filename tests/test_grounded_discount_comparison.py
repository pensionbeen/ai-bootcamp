import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from grounded_discount_comparison import compact_discount_comparison
from public_rag_response import citation_label


def fixture(facility="문화회관", a="예술인", b="지역 문화카드", rates=(37, 21), reverse=False):
    ordinance = f"서울특별시 {facility} 이용 조례"
    owner, venue = "example:owner", "공영" + facility
    meta = {"ordin_name": ordinance, "parent_chunk_id": owner, "source_url": "https://example.invalid/ordinance",
            "effective_date": "2025-04-01", "context_only": False}
    stems = [f"{a} 및 동료가 사용하는 시설에 대하여는 이용요금의 ", f"「{b}」 소지자에 대하여 이용요금의 "]
    rows = [{**meta, "kind": "body", "unit_id": f"{owner}::item::{j + 1}",
             "locator": {"article": "제14조", "paragraph": 2, "item": j + 1},
             "quote": f"{j + 1}. " + stems[j] + f"100분의 {rates[j]}를 할인한다."} for j in range(2)]
    ordinary = "둘 이상의 이용요금 감면사유에 해당하는 때에는 그 중 감면율이 높은 하나만 적용"
    rows.append({**meta, "kind": "required_context", "unit_id": owner + "::context::applicability",
                 "locator": {"article": "제14조", "paragraph": 3}, "quote": "③ " + ordinary + "한다."})
    assertions = [citation_label(row, {}) + "에 따라 " + stem + f"100분의 {rate}를 할인할 수 있습니다."
                  for row, stem, rate in zip(rows, stems, rates)]
    rule = citation_label(rows[-1], {}) + "에 따라 " + ordinary + "합니다."
    anti = f"{venue}에서는 두 감면을 동시에 받을 수 없습니다."
    text = " ".join([assertions[0], assertions[1], rule, anti] if not reverse
                    else [anti, assertions[1], assertions[0], rule])
    claims = [{"text": text, "citation_ids": [row["unit_id"] for row in rows]}]
    audit = {"decision": "supported", "draft_status": "code_checked_draft", "citation_contract": "exact_source_subunits_v1",
             "code_checks": {"status": "passed", "issues": [], "checked_claims": copy.deepcopy(claims)},
             "generated_claims": claims, "draft_response": {"answer": text, "citation": rows}}
    public = {"answer": text, "citation": list(dict.fromkeys(citation_label(row, {}, detail=False) for row in rows)),
              "source_url": meta["source_url"], "effective_date": meta["effective_date"]}
    return public, audit


def replace_claim(public, result, transform):
    changed = transform(public["answer"])
    public["answer"] = result["draft_response"]["answer"] = changed
    result["code_checks"]["checked_claims"][0]["text"] = result["generated_claims"][0]["text"] = changed


class GroundedDiscountComparisonTests(unittest.TestCase):
    def test_multiple_wordings_and_domains_keep_individually_verified_rates_and_facility(self):
        for facility, a, b, rates in [
            ("문화회관", "예술인", "지역 문화카드", (37, 21)), ("체육센터", "청소년", "생활 체육카드", (19.5, 54)),
        ]:
            v = "공영" + facility
            for reverse in (False, True):
                public, audit = fixture(facility, a, b, rates, reverse)
                untouched = copy.deepcopy((public, audit))
                for question in [
                    f"{a} 할인과 {b} 할인을 {v}에서 둘 다 받을 수 있나요?",
                    f"{a} 할인과 {b} 할인은 중복 적용이 가능한가요?",
                    f"{v}에서는 {a} 감면과 {b} 감면이 함께 적용되나요?",
                    f"{b} 할인과 {a} 할인, 두 할인을 동시에 받을 수 있나요?",
                    f"{b} 할인과 {a} 할인을 함께 받을 수 있습니까?",
                    f"{b} 할인과 {a} 할인은 중복 적용되나요?",
                ]:
                    with self.subTest(q=question, reverse=reverse):
                        brief = compact_discount_comparison(question, public, audit)
                        self.assertIsNotNone(brief)
                        for fact in (v, "모두 충족해도", a + f" 할인({rates[0]}%)", b + f" 할인({rates[1]}%)",
                                     "중복 할인은 받을 수 없습니다", "높은 하나만"):
                            self.assertIn(fact, brief)
                        self.assertLess(len(brief), len(public["answer"]))
                        self.assertEqual((public, audit), untouched)

    def test_conditional_duplicate_extra_and_wrong_venue_questions_keep_original(self):
        public, audit = fixture()
        for question in (
            "예술인 할인과 지역 문화카드 할인을 야간에 함께 받을 수 있나요?",
            "예술인 할인과 지역 문화카드 할인을 공영문화회관에서 둘 다 받을 수 있나요? 준비물은요?",
            "환승 예술인 할인과 지역 문화카드 할인을 함께 받을 수 있나요?",
            "예술인 할인과 예술인 할인을 함께 받을 수 있나요?",
            "예술인 할인과 지역 문화카드 할인을 공영체육센터에서 둘 다 받을 수 있나요?",
            "두 할인을 동시에 받을 수 있나요?",
            "예술인 할인과 지역 문화카드 할인 중 누가 얼마를 받나요?",
        ):
            with self.subTest(q=question):
                self.assertIsNone(compact_discount_comparison(question, public, audit))

    def test_additional_facts_mismatched_percent_conditions_or_missing_rule_in_original_keep_original(self):
        question = "예술인 할인과 지역 문화카드 할인은 중복 적용이 가능한가요?"
        for change in (
            lambda text: text.replace("100분의 37", "100분의 21"),
            lambda text: text.replace("높은 하나만", "낮은 하나만"),
            lambda text: text.replace("동시에 받을 수 없습니다", "동시에 받을 수 있습니다"),
            lambda text: text + " 단, 교통 환승 시에는 두 감면 모두 가능합니다.",
            lambda text: text.replace("사용하는 시설에", "등록증을 제출한 뒤 사용하는 시설에"),
        ):
            public, audit = fixture()
            replace_claim(public, audit, change)
            self.assertIsNone(compact_discount_comparison(question, public, audit))

    def test_source_and_audit_conflicts_force_original(self):
        question = "예술인 할인과 지역 문화카드 할인은 중복 적용이 가능한가요?"
        for change in (
            lambda p, r: r["code_checks"].update(status="failed"),
            lambda p, r: r["code_checks"]["checked_claims"].clear(),
            lambda p, r: r["generated_claims"].append(copy.deepcopy(r["generated_claims"][0])),
            lambda p, r: r["generated_claims"][0]["citation_ids"].pop(),
            lambda p, r: p["citation"].pop(),
            lambda p, r: p.update(effective_date="2023-01-02"),
            lambda p, r: r["draft_response"]["citation"][0].update(parent_chunk_id="unrelated"),
            lambda p, r: r["draft_response"]["citation"][1].update(effective_date="2023-01-02"),
            lambda p, r: r["draft_response"]["citation"][0].update(context_only=True),
            lambda p, r: r["draft_response"]["citation"][-1].update(quote=r["draft_response"]["citation"][-1]["quote"]
                                                                    + " 다만 정기회원은 중복 적용한다."),
            lambda p, r: r["draft_response"]["citation"][1].update(quote="2. 지역 문화카드가 없는 사람은 21%를 할인한다."),
        ):
            public, audit = fixture()
            change(public, audit)
            self.assertIsNone(compact_discount_comparison(question, public, audit))


if __name__ == "__main__":
    unittest.main()
