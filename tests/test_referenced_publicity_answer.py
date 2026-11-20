import unittest

from test_complete_selected_evidence import LAW, row
from referenced_publicity_answer import referenced_publicity_answer


ASK = "서울시 문화복지위원회 회의의 공개 원칙과 비공개 사유는 어디에서 확인할 수 있나요?"
RULE = "② 회의는 「문화행정 기본 조례」 제8조의2제3항에 따라 공개하는 것을 원칙으로 한다."


def candidates():
    return [
        row("default", RULE, label=LAW + " 제12조 제2항"),
        row("name", '① 시장은 서울특별시 문화복지위원회(이하 "위원회"라 한다)를 둔다.', label=LAW + " 제12조 제1항"),
        row("operations", "④ 그 밖에 위원회의 운영에 필요한 사항은 위원장이 정한다.", label=LAW + " 제12조 제4항"),
    ]


class ReferencedPublicityTests(unittest.TestCase):
    def test_only_rule_is_cited_and_does_not_invent_external_exceptions(self):
        result = referenced_publicity_answer(ASK, candidates())
        self.assertEqual(result["ids"], ["default"])
        self.assertEqual(result["used_ids"], result["ids"])
        self.assertEqual(result["answer"], "회의는 「문화행정 기본 조례」 제8조의2제3항에 따라 공개하는 것이 원칙입니다. "
                         "이 조항에는 구체적인 비공개 사유가 적혀 있지 않아, 비공개 기준은 해당 규정을 확인해야 합니다.")
        result = referenced_publicity_answer("문화복지위원회의 회의 공개 원칙을 알려주세요.", candidates())
        self.assertEqual(len(result["answer"].split(".")), 2)

    def test_named_committee_and_rule_are_read_from_source(self):
        rows = candidates()
        rows[1]["quote"] = '① 구청장은 생태보전위원회(이하 “위원회”라고 한다)를 설치한다.'
        rows[0]["quote"] = ("② 위원회의 회의의 내용은 정책의 투명성을 높이기 위하여 「환경협의체 운영법」 "
                            "제21조 제4항에 의하여 공개를 원칙으로 한다. <개정 2022.1.4>")
        question = "생태보전위원회 회의를 방청하고 싶어서 문의하니 비공개일 수도 있다고 들었습니다. 어떤 경우에 회의를 비공개로 할 수 있나요?"
        result = referenced_publicity_answer(question, rows)
        self.assertIn("회의 내용은 「환경협의체 운영법」 제21조 제4항", result["answer"])

    def test_defer_when_asked_another_question_or_scope(self):
        for question in (
            ASK.replace("문화복지위원회", "문화복지소위원회"), ASK.replace("서울시", "부산시"),
            ASK + " 위원은 몇 명인가요?", ASK.replace("공개 원칙과", "방청 신청과"),
            ASK.replace("위원회 회의의", "위원회 회의록의"), ASK.replace("원칙과", "원칙과 결정권자와"),
            ASK.replace("위원회 회의의", "위원회와 환경위원회 회의의"),
            "문화복지위원회 회의의 비공개 사유가 개인정보 때문인가요?",
            "문화복지위원회의 회의가 비공개일 때 이의제기 절차를 알려주세요.",
            "문화복지위원회 회의를 방청하려면 어떻게 하나요?",
        ):
            with self.subTest(question=question):
                self.assertIsNone(referenced_publicity_answer(question, candidates()))

    def test_defer_without_unambiguous_same_article_definition(self):
        for change in ({"locator": "제99조 제1항"}, {"effective_date": "2026-05-06"}, {"source_url": ""}):
            rows = candidates()
            rows[1].update(change)
            self.assertIsNone(referenced_publicity_answer(ASK, rows))
        rows = candidates()
        rows.append({**rows[1], "id": "second", "quote": rows[1]["quote"].replace("문화복지위원회", "환경위원회")})
        self.assertIsNone(referenced_publicity_answer(ASK, rows))

    def test_defer_when_external_text_or_real_exception_available(self):
        for extra in (
            {"quote": "③ 위원회는 개인정보가 포함된 안건을 비공개로 한다.", "locator": "제12조 제3항"},
            {"quote": "⑨ 제2항에도 불구하고 회의를 공개하지 아니할 수 있다.", "locator": "제12조 제9항"},
            {"quote": "② 위원장은 공개할 안건을 정한다.", "locator": "제12조 제4항"},
            {"quote": "① 위원회의 회의는 비공개할 수 있다.", "locator": "제23조 제1항"},
            {"ordinance": "문화행정 기본 조례", "locator": "제8조의2 제3항"},
        ):
            rows = candidates()
            rows.append({**rows[0], **extra, "id": "other"})
            self.assertIsNone(referenced_publicity_answer(ASK, rows), extra)

    def test_defer_for_operational_cross_reference_or_conditional_rule(self):
        for quote in (
            RULE.replace("공개하는 것을", "비공개를"), RULE.replace("회의는", "회의록은"),
            RULE.replace("회의는", "안건이 없는 경우 회의는"),
            RULE.replace("회의는", "회의는 일부 사안의 분쟁을 줄이기 위하여"),
            RULE + " 다만 위원장이 필요하다고 인정하면 공개하지 않는다.",
            RULE.replace("「문화행정 기본 조례」", "따로 정한 기준"), RULE.replace(" 제8조의2제3항", ""),
        ):
            rows = candidates()
            rows[0]["quote"] = quote
            self.assertIsNone(referenced_publicity_answer(ASK, rows), quote)


if __name__ == "__main__":
    unittest.main()
