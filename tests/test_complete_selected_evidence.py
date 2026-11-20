import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from complete_selected_evidence import complete_selection


LAW = "서울특별시 문화시설 조례"
ANNEX = LAW + " 별표 4"
COMPARE = "예술인 할인과 지역 문화카드 할인을 함께 받을 수 있나요?"
PRICE = "공유연습실의 이용료는 얼마인가요?"


def row(key, text, *, owner="article", unit=None, label=LAW + " 제13조", **extra):
    return {"id": key, "quote": text, "owner_id": owner, "unit_id": unit or owner,
            "ordinance": LAW, "source_url": "https://example.invalid/art", "effective_date": "2025-01-01",
            "public_label": label, "locator": label.removeprefix(LAW + " "), "required_ids": [], **extra}


def benefits():
    return [
        row("card", "「지역 문화카드」 소지자는 사용료의 42%를 할인한다.", unit="article::item::a"),
        row("compare", "복수 혜택이 적용되면 감면율이 높은 하나만 적용한다.", unit="article::context::x"),
        row("artist", "예술인은 이용요금의 37%를 감면한다.", unit="article::item::b"),
    ]


def fees():
    return [
        row("table", "구분: 이용요금\n시간당: 7\n월 정기권: 31", owner="table-row", label=ANNEX,
            required_ids=["currency", "title"]),
        row("title", "[별표 4]\n공유연습실 요금표", owner="table-row", unit="title", label=ANNEX),
        row("article", "공유연습실은 별표 4의 범위에서 이용요금을 부과할 수 있다."),
        row("currency", "(단위 : 천원/건)", owner="table-row", unit="currency", label=ANNEX),
    ]


class CompleteSelectionTests(unittest.TestCase):
    def test_comparison_appends_only_two_direct_rows_in_catalog_order(self):
        rows = benefits()
        self.assertEqual(complete_selection(COMPARE, rows, ["compare"]), ["compare", "card", "artist"])
        self.assertEqual(complete_selection(COMPARE, rows, ["artist", "compare"]), ["artist", "compare", "card"])

    def test_unanswered_and_non_comparison_questions_keep_their_selection(self):
        self.assertEqual(complete_selection(COMPARE, benefits(), []), [])
        self.assertEqual(complete_selection(PRICE, fees(), []), [])
        for q in ["예술인에게 할인되나요?", "지역 문화카드와 예술인은 각각 어디서 등록하나요?",
                  "예술인 할인과 지역 문화카드 할인을 함께 신청하는 서류는?",
                  "예술인 할인과 지역 문화카드 할인을 카드가 없어도 함께 받을 수 있나요?"]:
            with self.subTest(q=q):
                self.assertEqual(complete_selection(q, benefits(), ["compare"]), ["compare"])

    def test_one_missing_ambiguous_or_inapplicable_subject_cannot_complete_the_pair(self):
        for wrong in ["「문화카드」 소지자에게 20%를 할인한다.",
                      "「인접 지역 문화카드」 소지자에게 20%를 할인한다.",
                      "지역 문화카드가 없는 사람에게는 요금을 30% 감면한다.",
                      "외지 이용자에게 10%를 할인한다. 다만 지역 문화카드는 예외로 한다.",
                      "지역 문화카드 신청자 명단은 별지에 기재한다."]:
            with self.subTest(wrong=wrong):
                rows = benefits()
                rows[0]["quote"] = wrong
                self.assertEqual(complete_selection(COMPARE, rows, ["compare"]), ["compare"])
        rows = benefits()
        self.assertEqual(complete_selection(COMPARE, rows + [{**rows[0], "id": "also"}], ["compare"]), ["compare"])

    def test_only_one_scoped_rate_for_the_subject_can_be_completed(self):
        for key in ("ordinance", "source_url", "effective_date", "owner_id"):
            with self.subTest(key=key):
                rows = benefits()
                rows[0][key] = "unrelated"
                self.assertEqual(complete_selection(COMPARE, rows, ["compare"]), ["compare"])
        rows = benefits()
        rows[-1]["quote"] = "예술인의 배우자에게는 이용요금을 17% 할인한다."
        self.assertEqual(complete_selection(COMPARE, rows, ["compare"]), ["compare"])

    def test_amount_restores_actual_fee_cells_with_a_linked_currency_unit(self):
        rows = fees()
        self.assertEqual(complete_selection(PRICE, rows, ["title", "article"]), ["title", "article", "table"])
        self.assertEqual(complete_selection(PRICE, rows, ["article"]), ["article"])

    def test_table_request_must_be_for_the_identified_fee_and_facility(self):
        for q in ["공유연습실의 과태료는 얼마인가요?", "공유연습실 매점 이용료는 얼마인가요?",
                  "공유연습실의 반려동물 이용료는 얼마인가요?", "공유연습실의 이용료 규정이 있나요?",
                  "개인연습실의 이용료는 얼마인가요?"]:
            with self.subTest(q=q):
                self.assertEqual(complete_selection(q, fees(), ["title", "article"]), ["title", "article"])

    def test_wrong_unit_category_metric_edition_or_annex_is_insufficient(self):
        for index, key, value in [
            (0, "required_ids", ["title"]), (3, "quote", "(단위 : 명)"), (3, "owner_id", "another"),
            (0, "quote", "구분: 외부강사\n시간당: 15"), (0, "quote", "구분: 이용요금\n이용정원: 8"),
            (2, "quote", "공유연습실은 별표 14의 범위에서 이용요금을 받는다."),
            (0, "owner_id", "another"), (0, "source_url", "wrong"), (0, "effective_date", "1970-01-01"),
            (2, "ordinance", "다른 조례")]:
            with self.subTest(index=index, key=key, value=value):
                rows = fees()
                rows[index][key] = value
                self.assertEqual(complete_selection(PRICE, rows, ["title", "article"]), ["title", "article"])
        rows = fees()
        rows.append({**rows[0], "id": "another", "unit_id": "another", "owner_id": "another"})
        self.assertEqual(complete_selection(PRICE, rows, ["title", "article"]), ["title", "article"])


if __name__ == "__main__":
    unittest.main()
