from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from source_provision_style import concise_provision_answer


COMMON = {"ordinance": "시 교통 조례", "source_url": "https://example.invalid/source", "effective_date": "2025-02-15"}


def benefit():
    text = ("구청장은 「이동법」 제18조 및 같은 법 시행령 제3조에 따른 소형차와 "
            "「청정법」 제9조에 따른 무공해차에 대하여 주차요금의 100분의 24를 할인할 수 있다. "
            "다만, 철도 환승주차장에서 환승목적으로 주차하는 소형차와 무공해차에 대해서는 주차요금의 "
            "100분의 72.5를 할인하되, 2일 3회 주차의 경우에는 최초 45분까지의 주차요금을 면제한다.")
    row = {**COMMON, "id": "B", "locator": "제8조제2호", "quote": "2. " + text}
    plan = {"kind": "single_benefit", "subject": "소형차", "source_subject": "소형차", "ids": ["B"],
            "used_ids": ["B"], "answer": text}
    return "공영주차장에 소형차를 세우면 할인되나요?", plan, [row]


def fee():
    free = ("법 제31조제2항의 규정에 의하여 설치된 공동주차장의 주차요금은 무료로 한다. "
            "다만, 공동주차장의 관리ㆍ운영을 공공운영단체(이하 \"운영단체\"라 한다) 등에 위탁하는 등 "
            "특별한 사유가 있는 경우에는 주차요금을 별표의 범위 내에서 규칙으로 정하여 부과할 수 있다.")
    exempt = ("법 제31조제3항의 규정에 의하여 공동주차장을 설치하여 도비를 지원받은 경우에는 "
              "그 주차요금을 무료로 하여야 한다.")
    price = "별표의 1구획당 기준액은 30분당 350원, 1일권 1,650원, 월 정기권 36,000원입니다."
    rows = [{**COMMON, "id": alias, "locator": loc, "quote": quote} for alias, loc, quote in [
        ("free", "제19조제1항", "① " + free), ("fund", "제19조제2항", "② " + exempt),
        ("value", "별표 — 행9", "구분: 주차요금\n30분당: 350\n1일권: 1,650\n월 정기권: 36,000"),
        ("unit", "별표 — 1쪽", "[별표]\n공동주차장 요금표(제19조제1항 관련)\n(단위: 원/구획)"),
    ]]
    ids = [r["id"] for r in rows]
    return "공동주차장 요금은 얼마인가요?", {"kind": "fee_schedule", "ids": ids, "used_ids": ids,
                                               "answer": " ".join([free, exempt, price]), "price": price}, rows


class SourceProvisionStyleTests(unittest.TestCase):
    def test_discount_preserves_discretion_transfer_terms_and_only_the_asked_subject(self):
        question, plan, rows = benefit()
        original = deepcopy((rows, plan))
        for mandatory in (False, True):
            if mandatory:
                rows[0]["quote"] = rows[0]["quote"].replace("할인할 수 있다.", "할인한다.")
                plan["answer"] = plan["answer"].replace("할인할 수 있다.", "할인한다.")
            answer = concise_provision_answer(question, plan, rows)
            self.assertLess(len(answer), len(plan["answer"]))
            for term in ("구청장", "공영주차장", "이동법", "시행령", "소형차", "24%", "철도 환승주차장",
                         "환승 목적", "72.5%", "2일 3회", "최초 45분", "면제"):
                self.assertIn(term, answer)
            self.assertIn("24% 할인합니다" if mandatory else "24% 할인할 수 있습니다", answer)
            self.assertNotIn("무공해차", answer)
            if not mandatory:
                self.assertEqual((rows, plan), original)

    def test_changed_eligibility_numeric_scope_or_extra_rule_is_not_shortened(self):
        for old, new in [("무공해차에 대하여", "무공해차 등록자에 한하여"),
                         ("100분의 24", "100분의 24 이상"), ("100분의 72.5", "0분의 7"),
                         ("72.5를 할인하되", "72.5를 할인할 수 있으나"),
                         ("주차하는 소형차와 무공해차", "주차하는 무공해차"),
                         ("면제한다.", "면제한다. 공휴일은 제외한다.")]:
            question, plan, rows = benefit()
            rows[0]["quote"] = rows[0]["quote"].replace(old, new)
            plan["answer"] = plan["answer"].replace(old, new)
            self.assertIsNone(concise_provision_answer(question, plan, rows), new)

    def test_fee_preserves_both_legal_scopes_funding_and_table_amounts(self):
        question, plan, rows = fee()
        original = deepcopy((plan, rows))
        answer = concise_provision_answer(question, plan, rows)
        self.assertLess(len(answer), len(plan["answer"]))
        for term in ("법 제31조제2항", "공동주차장", "원칙적으로 무료", "공공운영단체 등에 위탁하는 등 특별한 사유",
                     "별표 범위", "규칙", "부과할 수", "법 제31조제3항", "도비를 지원받은 경우", "반드시 무료",
                     "1구획당", "30분당 350원", "1일권 1,650원", "월 정기권 36,000원"):
            self.assertIn(term, answer)
        self.assertEqual((plan, rows), original)

    def test_unknown_qualifications_and_missing_units_leave_original_text(self):
        for change in (
            lambda p, r: r[1].update(quote=r[1]["quote"].replace("무료로 하여야 한다", "무료로 할 수 있다")),
            lambda p, r: r[0].update(quote=r[0]["quote"].replace("위탁하는 등", "위탁하는 경우에 한하여")),
            lambda p, r: r[1].update(effective_date="2021-01-01"),
            lambda p, r: p.update(used_ids=["free", "fund", "value"]),
            lambda p, r: p.update(price=p["price"].replace("350원", "2,000원")),
            lambda p, r: r[0].update(quote=r[0]["quote"]+" 다만 재난 시에는 징수하지 않는다."),
        ):
            q, p, rows = fee()
            change(p, rows)
            self.assertIsNone(concise_provision_answer(q, p, rows))
