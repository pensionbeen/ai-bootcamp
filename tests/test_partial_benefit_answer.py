import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from partial_benefit_answer import partial_benefit


def row(uid, text, **changes):
    return {"id": uid, "unit_id": f"source::{uid}", "locator": "제20조 제2항",
            "ordinance": "가람군 생활지원 조례", "source_url": "https://example.invalid/budget",
            "effective_date": "2024-01-01", "quote": text, **changes}


GRANT = row("grant", "② 군수는 예산의 범위에서 육아지원금을 지급할 수 있다.")
REQUEST = "가람군 육아지원금은 매달 얼마 나오나요? 몇 개월 받을 수 있나요?"


class PartialBenefitTest(unittest.TestCase):
    def test_confirms_grant_without_guessing_amount_or_term(self):
        other = row("other", "시장은 예산의 범위에서 취업장려금을 지급할 수 있다.")
        plan = partial_benefit(REQUEST, [other, GRANT])
        self.assertEqual(plan["ids"], ["grant"])
        self.assertEqual(plan["used_ids"], ["grant"])
        self.assertEqual(plan["answer"], "군수는 예산의 범위에서 육아지원금을 지급할 수 있습니다. "
                         "이 지급 조항에는 월 지급액과 지급 기간이 명시되어 있지 않습니다.")
        self.assertEqual(plan["missing_materials"], ["육아지원금 월 지급액과 지급 기간을 확인할 수 있는 지급 기준"])

    def test_preserves_source_purpose_and_distinguishes_other_recipients(self):
        grant = row("grant", "③ 시장은 시민의 경제활동을 보장하기 위해 취업을 촉진하는 방안을 "
                    "강구하여야 하며, 예산의 범위에서 취업장려금을 지급할 수 있다. 또한, 필요시 "
                    "자문회의를 운영하며 참석위원에게는 예산의 범위에서 수당과 여비를 지급할 수 있다. "
                    "<개정 2025.1.1>", ordinance="서울특별시 생활지원 조례")
        procedure = row("procedure", "활동비 지급 대상 선정 기준·방법·절차를 마련한다.")
        plan = partial_benefit("서울시 취업장려금 한 달에 얼마 나옴? 몇 개월 받을 수 있는지도 ㅎㅎ",
                               [grant, procedure])
        self.assertEqual(plan["ids"], ["grant"])
        self.assertIn("시민의 경제활동을 보장하기 위해", plan["answer"])
        self.assertNotIn("위원", plan["answer"])

    def test_scopes_an_explicit_document_to_the_requested_fields(self):
        document = row("doc", "④ 육아지원금의 지급액과 지급 기간은 사업 공고로 정한다.", locator="제20조 제4항")
        plan = partial_benefit(REQUEST, [document, GRANT])
        self.assertEqual(plan["used_ids"], ["grant", "doc"])
        self.assertEqual(plan["missing_materials"], ["육아지원금 사업 공고"])
        self.assertIn("사업 공고에서 확인", plan["answer"])
        amount_only = row("doc", "육아지원금의 지급액은 운영 지침에서 정한다.")
        self.assertIsNone(partial_benefit(REQUEST, [GRANT, amount_only]))
        self.assertIn("지급액은 운영 지침", partial_benefit("육아지원금은 얼마인가요?", [GRANT, amount_only])["answer"])
        term_only = partial_benefit("육아지원금의 지급 기간이 어떻게 되나요?", [GRANT])
        self.assertNotIn("지급액", term_only["answer"])

    def test_scope_is_unambiguous_and_only_missing_amount_or_term(self):
        for q in [
            "새롬시 육아지원금은 월 얼마 나오나요?", "육아지원금과 취업장려금은 매달 얼마인가요?",
            "육아지원금 신청 기한이 몇 개월인가요?", "육아지원금의 환수 금액은 얼마인가요?",
            "육아지원금 요율은 얼마인가요?", "육아지원금 미신고 과태료가 얼마인가요?",
            "육아지원금의 지급액이랑 지원 자격을 알려줘", "올해 육아지원금이 인상되면 얼마인가요?",
            "취업장려금은 월 얼마인가요?", "지원금은 얼마인가요?", "육아지원금은 얼마나 받나요?"
        ]:
            with self.subTest(q=q):
                self.assertIsNone(partial_benefit(q, [GRANT]))

    def test_competing_terms_concrete_amounts_and_unlinked_tables_defer(self):
        changes = [
            row("duplicate", GRANT["quote"], ordinance="새롬시 출생 조례"),
            row("specific", "육아지원금은 월 50만원까지 지원한다."),
            row("other article", "지급 기간은 따로 정한다."),
            row("specific", "월 30만 원까지 지급한다."),
            row("table", "구분: 기본형\n단가: 3,500", locator="별표 3"),
            row("doc", "육아지원금의 지급액과 지급 기간은 사업 공고로 정한다.", effective_date="2023-01-01"),
        ]
        for extra in changes:
            with self.subTest(quote=extra["quote"]):
                self.assertIsNone(partial_benefit(REQUEST, [GRANT, extra]))
        for grant in [
            row("qualified", GRANT["quote"] + " 다만, 기수급자는 제외한다."),
            row("qualified", GRANT["quote"] + " 지원대상 및 지급액은 별표와 같다."),
            row("qualified", "군수는 주민에게 등록하도록 해야 하며, 예산의 범위에서 육아지원금을 지급할 수 있다."),
            row("term", "① 군수는 예산의 범위에서 육아지원금을 매달 지급할 수 있다."),
            row("qualified", "군수는 예산의 범위에서 등록자에게 육아지원금을 지급할 수 있다."),
            row("from_other_scheme", "군수는 예산의 범위에서 다문화육아지원금을 지급할 수 있다."),
            row("missing_url", GRANT["quote"], source_url=""),
        ]:
            with self.subTest(quote=grant["quote"]):
                self.assertIsNone(partial_benefit(REQUEST, [grant]))

    def test_unnamed_sibling_can_supply_or_limit_the_requested_benefit(self):
        for qualifier in [
            "② 1년 동안 지급한다.",
            "② 지급은 12주까지 가능하다.",
            "② 상기 지급은 90일간으로 제한한다.",
            "② 2027년 1월 1일부터 6월 30일까지 지급한다.",
            "② 지원은 매년 3월부터 6월까지만 한다.",
            "② 별표에서 정하는 바에 따라 지급한다.",
            "② 지급 대상과 지급 방법은 시행규칙에 따른다.",
            "② 다만, 이미 지원을 받은 수급자는 지급 대상에서 제외한다.",
            "② 다만, 군내에 주민등록을 한 경우에 한한다.",
        ]:
            sibling = row("sibling", qualifier, locator="제20조 제3항")
            with self.subTest(qualifier=qualifier):
                self.assertIsNone(partial_benefit(REQUEST, [GRANT, sibling]))
        committee = row("committee", "위촉직 위원의 임기는 2년으로 한다.", locator="제30조 제1항")
        self.assertEqual(partial_benefit(REQUEST, [GRANT, committee])["ids"], ["grant"])


if __name__ == "__main__":
    unittest.main()
