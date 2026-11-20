import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from local_rag_answer import _calculation, _check_claims, _citations, _numbers, AnswerError
from rag_numeric_amounts import parse_won, won_literals


class WonArithmeticTests(unittest.TestCase):
    def test_converted_operand_is_bound_to_the_verified_displayed_equation(self):
        expr = "90,000 ÷ 3"
        question = "9만원을 3명에게 나누면?"
        valid = expr + " = 30,000원입니다."
        for claim, expected in ((valid, "passed"), (valid+" 90,000명입니다.", "failed"),
                                ("인원은 90,000명입니다.", "failed"),
                                ("3 + "+valid, "failed"), (expr+" = 31,000원입니다.", "failed")):
            check = self.checked(claim, question, expression=expr, result="30000")
            self.assertEqual(check["status"], expected)

    def test_unscaled_price_difference_uses_own_table_unit(self):
        rows = [{"unit_id": "row", "parent_chunk_id": "fees", "kind": "attachment",
                 "quote": "전일권: 170,000\n야간권: 130,000", "locator": {}}]
        claim = {"text": "차액은 40,000원입니다.", "citation_ids": ["row"]}
        calculation = {"expression": "170000 - 130000", "result": "40000", "citation_ids": ["row"]}
        for owner, expected in (("fees", "passed"), ("different", "failed")):
            heading = {"unit_id": "heading", "kind": "required_context", "parent_chunk_id": owner,
                       "locator": {}, "quote": "(단위 : 원)"}
            check = _check_claims([claim], [calculation], claim["text"], rows, "차액은?", [heading])
            self.assertEqual(check["status"], expected)

    def test_native_table_prices_only_authorize_owned_addition_or_difference(self):
        rows = [{"unit_id": "row", "parent_chunk_id": "fees", "kind": "attachment", "locator": {},
                 "quote": "노 외 주 차 장 > 월 정 기 권 > 전 일: 170,000\n야간권: 85,000\n대상자: 3"}]
        heading = [{"unit_id": "heading", "kind": "required_context", "parent_chunk_id": "fees",
                    "quote": "(단위: 원)", "locator": {}}]
        for expression, result, question, expected in (
            ("170000-85000", "85000", "차액은?", "passed"),
            ("170000+85000", "255000", "합계는?", "passed"),
            ("170000/85000", "2", "비율은?", "failed"),
            ("170000//85000", "2", "비율은?", "failed"),
            ("170000*85000", "14450000000", "곱은?", "failed"),
            ("170000-3", "169997", "170000명 중 3명을 빼면?", "failed"),
            ("170000-130000", "40000", "170000명 중 130000명을 빼면?", "failed"),
            ("(170000-85000)+3", "85003", "합계는?", "failed")):
            text = "금액은 " + result + "원입니다."
            claim = {"text": text, "citation_ids": ["row"]}
            calculation = {"expression": expression, "result": result, "citation_ids": ["row"]}
            checked = _check_claims([claim], [calculation], text, rows, question, heading)
            self.assertEqual(checked["status"], expected, (expression, checked))

    def test_native_money_parent_does_not_type_people_or_nested_counters(self):
        for label in ("전일권 인원", "요금 대상자", "월정기권 > 인원", "사용료 > 수량", "월정기권 > 전일 > 인원"):
            rows = [{"unit_id": "row", "kind": "attachment", "parent_chunk_id": "fees", "locator": {},
                     "quote": "노 외 주 차 장 > 월 정 기 권 > 전 일: 170,000\n" + label + ": 3"}]
            heading = [{"unit_id": "heading", "kind": "required_context", "parent_chunk_id": "fees",
                        "quote": "(단위: 원)", "locator": {}}]
            text = "차액은 169997원입니다."
            checked = _check_claims([{"text": text, "citation_ids": ["row"]}],
                                   [{"expression": "170000-3", "result": "169997", "citation_ids": ["row"]}],
                                   text, rows, "차액은?", heading)
            self.assertEqual(checked["status"], "failed", (label, checked))

    def test_native_cross_row_arithmetic_requires_same_physical_table_and_each_owned_unit(self):
        source = {"ordin_seq": "ordin", "source_url": "https://example.test/ordin",
                  "effective_date": "2026-10-01", "as_of_date": "2026-10-01"}
        capture = {"attachment_id": "attachment", "parse_file": "table.json", "parse_content_hash": "digest",
                   "page": 1, "element_id": 3, "representation": "parse_html"}
        rows, contexts = [], []
        for index, value in enumerate((170000, 85000)):
            uid = "row" + str(index)
            rows.append({"unit_id": uid, "parent_chunk_id": uid, "kind": "attachment", "source": dict(source),
                         "quote": "주차장 > 월정기권 > 전일: " + str(value),
                         "locator": {"attachment_id": "attachment", "source_refs": [{**capture, "row_index": index}]}})
            contexts.append({"unit_id": "heading" + str(index), "kind": "required_context", "parent_chunk_id": uid,
                             "source": dict(source), "quote": "(단위: 원)",
                             "locator": {"source_refs": [{**capture, "element_id": 2}]}})

        def check(test_rows, test_contexts, expression="170000-85000", result="85000"):
            # Exercise the real pipeline's canonical flattened citation format,
            # including the original required contexts passed into the checker.
            units = {row["unit_id"]: copy.deepcopy(row) for row in test_rows + test_contexts}
            primary = []
            for row in test_rows:
                units[row["unit_id"]]["required_context_ids"] = [context["unit_id"] for context in test_contexts
                                                                if context["parent_chunk_id"] == row["unit_id"]]
                primary.append({"unit_id": row["unit_id"], "dependency_primary_ids": []})
            originals = _citations([row["unit_id"] for row in test_rows], units, primary)
            canonical_rows = [row for row in originals if row["kind"] == "attachment"]
            self.assertTrue(all("source" not in row and row["source_url"] for row in originals))
            text = "금액은 " + result + "원입니다."
            return _check_claims([{"text": text, "citation_ids": ["row0", "row1"]}],
                                 [{"expression": expression, "result": result, "citation_ids": ["row0", "row1"]}],
                                 text, canonical_rows, "계산 결과는?", originals)

        # Sum produces a new number: it cannot pass through direct presence.
        self.assertEqual(check(rows, contexts, "170000+85000", "255000")["status"], "passed")
        self.assertEqual(check(rows, contexts, "170000/85000", "2")["status"], "failed")
        # Use a distinct second amount to exercise an actual derived difference.
        difference_rows = copy.deepcopy(rows)
        difference_rows[1]["quote"] = "주차장 > 월정기권 > 전일: 130000"
        self.assertEqual(check(difference_rows, contexts, "170000-130000", "40000")["status"], "passed")
        for mutation in ("table", "page", "capture", "version", "missing_unit", "wrong_unit_capture", "wrong_owner"):
            changed, headings = copy.deepcopy(difference_rows), copy.deepcopy(contexts)
            if mutation == "table": changed[1]["locator"]["source_refs"][0]["element_id"] = 4
            elif mutation == "page": changed[1]["locator"]["source_refs"][0]["page"] = 2
            elif mutation == "capture": changed[1]["locator"]["source_refs"][0]["parse_content_hash"] = "other"
            elif mutation == "version": changed[1]["source"]["effective_date"] = "2025-10-01"
            elif mutation == "missing_unit": headings.pop()
            elif mutation == "wrong_unit_capture": headings[1]["locator"]["source_refs"][0]["parse_content_hash"] = "other"
            elif mutation == "wrong_owner": headings[1]["parent_chunk_id"] = "row0"
            self.assertEqual(check(changed, headings, "170000-130000", "40000")["status"], "failed", mutation)

    def checked(self, text, question="금액은?", quote="계산 기준입니다.", expression=None, result=None,
                extra=None, calculation_ids=None):
        citations = [{"unit_id": "a", "quote": quote, "locator": {}}] + (extra or [])
        calculations = [] if expression is None else [{"expression": expression, "result": result,
                                                     "citation_ids": calculation_ids or ["a"]}]
        return _check_claims([{"text": text, "citation_ids": ["a"]}], calculations,
                             text, citations, question)

    def assert_failed_calculation(self, question, quote, expression, result, extra=None):
        """A failed calculation is audit-only when unstated and holds once its result is stated."""
        checked = self.checked("계산했습니다.", question, quote, expression, result, extra=extra)
        self.assertNotIn("unverified_derived_calculation", checked["issues"], checked)
        self.assertEqual([(item["expression"], item["result"]) for item in checked["unverified_calculations_not_stated"]],
                         [(expression, result)], checked)
        checked = self.checked("계산 결과는 " + result + "입니다.", question, quote, expression, result, extra=extra)
        self.assertIn("unverified_derived_calculation", checked["issues"], checked)
        self.assertEqual(checked["status"], "failed", checked)
        self.assertEqual(checked["unverified_calculations_not_stated"], [], checked)

    def test_strict_amount_parser(self):
        for text, value in (("120만", 1200000), ("36만5천", 365000), ("8천만", 80000000),
                            ("1억2천만3천", 120003000), ("12,345", 12345),
                            ("1.25만", 12500), ("0", 0), ("2백3십5", 235)):
            with self.subTest(text=text):
                self.assertEqual(float(parse_won(text)), value)
        for text in ("1만2억", "1만2만", "10천", "2천3천", "12,34", "1.2만5천", "만", "1조"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_won(text)
        self.assertEqual(won_literals("금액은120만원입니다.")[0]["value"], "1.2E+6")

    def test_division_preserves_ast_and_operand_restrictions(self):
        allowed = _numbers("120 3 2 1 0.5")
        for expression in ("120÷3", "ceil(120÷3)", "floor(120÷3)×2", "120÷-3"):
            self.assertEqual(_calculation(expression, allowed), _calculation(expression.replace("÷", "/"), allowed))
        for expression in ("120÷÷3", "120÷/3", "120/÷3", "120÷(3**2)", "abs(120÷3)",
                           "120÷7", "120÷0", "120÷3 +", "[120]÷3"):
            with self.subTest(expression=expression), self.assertRaises((AnswerError, SyntaxError, ArithmeticError)):
                _calculation(expression, allowed)

    def test_owned_amounts_supply_only_calculation_local_operands(self):
        for expression in ("1200000÷3", "120만원÷3", "1200000/3"):
            checked = self.checked("금액은 40만원입니다.", question="120만원을 3으로 나누면?",
                                   expression=expression, result="400000")
            self.assertEqual(checked["status"], "passed", checked)
            self.assertTrue(checked["calculation_operand_provenance"])
        for question, quote, expression, result in (
            ("3으로 나누면?", "120만원", "120000÷3", "40000"),
            ("3으로 나누면?", "120만원", "120÷3", "40"),
            ("3으로 나누면?", "120만원", "1200000÷4", "300000"),
            ("3으로 나누면?", "120만원", "1200000÷3", "300000")):
            with self.subTest(expression=expression, result=result):
                self.assert_failed_calculation(question, quote, expression, result)
        checked = self.checked("금액은 40만원입니다.", "3으로 나누면?", "기준입니다.",
                               "1200000÷3", "400000", extra=[{"unit_id": "b", "quote": "120만원"}])
        self.assertIn("unverified_derived_calculation", checked["issues"])

    def test_compounds_percentages_and_rounding(self):
        checked = self.checked("금액은 36만5천원입니다.", "73만원을 2로 나누면?",
                               expression="730000÷2", result="365000")
        self.assertEqual(checked["status"], "passed", checked)
        checked = self.checked("금액은 2천만원입니다.", "8천만원의 25%는?",
                               expression="80000000×0.25", result="20000000")
        self.assertEqual(checked["status"], "passed", checked)
        checked = self.checked("금액은 12만2천원입니다.", "36만5천원을 3으로 나누고 1000원 단위로 올림하면?",
                               expression="ceil(365000÷3÷1000)×1000", result="122000")
        self.assertEqual(checked["status"], "passed", checked)
        checked = self.checked("금액은 12만2천원입니다.", "36만5천원을 3으로 나누면?",
                               expression="ceil(365000÷3÷1000)×1000", result="122000")
        self.assertIn("unverified_derived_calculation", checked["issues"])

    def test_display_amount_never_supplies_arbitrary_numbers_or_wrong_units(self):
        for text in ("인원은 120명입니다.", "인원은 1200000명입니다.", "금액은 120원입니다.",
                     "금액은 12만원입니다.", "금액은 1200000만원입니다."):
            checked = self.checked(text, quote="금액은 120만원입니다.")
            self.assertEqual(checked["status"], "failed", checked)
        for text in ("금액은 120만원입니다.", "금액은 1,200,000원입니다."):
            self.assertEqual(self.checked(text, quote="120만원")["status"], "passed")
        for text in ("금액은 40만원이고 40명입니다.", "금액은 4000만원입니다.", "금액은 40원입니다.",
                     "인원은 400000명입니다.", "금액은 400000이고 400000세입니다."):
            checked = self.checked(text, "120만원을 3으로 나누면?", expression="1200000÷3", result="400000")
            self.assertEqual(checked["status"], "failed", checked)
        checked = self.checked("금액은 40만원입니다.", "120만원을 3으로 나누면?")
        self.assertEqual(checked["status"], "failed", checked)
        for expression, result in (("1200000+3", "1200003"), ("1200000×1200000", "1440000000000")):
            with self.subTest(expression=expression):
                self.assert_failed_calculation("120만원과 3이 있습니다.", "계산 기준입니다.", expression, result)

    def test_result_currency_requires_currency_arithmetic_and_own_citation(self):
        checked = self.checked("금액은 40원입니다.", "120명을 3으로 나누면?",
                               expression="120÷3", result="40")
        self.assertEqual(checked["status"], "failed", checked)
        checked = self.checked("금액은 40만원입니다.", "3으로 나누면?", "기준입니다.",
                               "1200000÷3", "400000", extra=[{"unit_id": "b", "quote": "120만원"}],
                               calculation_ids=["b"])
        self.assertEqual(checked["status"], "failed", checked)
        # A won ratio has no won dimension.
        checked = self.checked("금액은 3원입니다.", "120만원과 40만원의 비율은?",
                               expression="1200000÷400000", result="3")
        self.assertEqual(checked["status"], "failed", checked)

    def test_punctuation_before_a_word_does_not_create_a_won_amount(self):
        for text in ("무료이고, 원칙적으로 요금이 없다.", "납부했다. 원문은 별표에 있다."):
            self.assertEqual(won_literals(text), [])
        for amount in ("420원", "21,000원", "3만 2천원"):
            text = f"요금은 {amount}이고, 원칙적으로 현장에서 납부한다."
            self.assertEqual([s["literal"] for s in won_literals(text)], [amount])
        for bad in (",300원", ".7원", "3,50원", "3.1.5원", ",~3,000원"):
            self.assertTrue(any(s["value"] is None for s in won_literals(bad)), bad)

    def test_malformed_currency_is_never_partially_converted(self):
        for literal in ("12,34원", "1만2만원", "2천3천원", "1.2만5천원", "1조원", "1만원원"):
            checked = self.checked("금액은 " + literal + "입니다.", quote=literal)
            self.assertIn("answer_contains_invalid_won_literal", checked["issues"], checked)
            with self.subTest(literal=literal):
                self.assert_failed_calculation("2로 나누면?", literal, literal + "÷2", "0")
        checked = self.checked("금액은 40만원입니다.", "-120만원을 3으로 나누면?",
                               expression="1200000÷3", result="400000")
        self.assertIn("unverified_derived_calculation", checked["issues"], checked)
        checked = self.checked("금액은 80만원입니다.", "120만원에서 40만원을 빼면?",
                               expression="120만원-40만원", result="800000")
        self.assertEqual(checked["status"], "passed", checked)

    def test_spaced_compounds_and_amount_ranges_do_not_leak_partial_amounts(self):
        for source, amount in (("120만 5천원", "1205000원"), ("36만 5천 원", "365000원")):
            self.assertEqual(self.checked("금액은 " + amount + "입니다.", quote=source)["status"], "passed")
            for text in ("금액은 5천원입니다.", "인원은 120명입니다."):
                self.assertEqual(self.checked(text, quote=source)["status"], "failed")
        checked = self.checked("요금은 13,000~19,500원입니다.", quote="요금 13,000~19,500원")
        self.assertEqual(checked["status"], "passed", checked)
        for amount in ("13,000원", "19,500원"):
            self.assertEqual(self.checked("요금은 " + amount + "입니다.", quote="13,000~19,500원")["status"], "passed")
        for source in ("13만~19만원", "13만~19원", "19,500~13,000원", "-120만원"):
            self.assertEqual(self.checked("금액은 19만원입니다.", quote=source)["status"], "failed")

    def test_table_unit_requires_a_monetary_row_or_column(self):
        for quote in ("금액(단위: 만원)\n지원액 120\n대상자 3명",
                      "금액(단위: 만원)\n| 구분 | 지원액 | 인원 |\n|---|---|---|\n| 일반 | 120 | 3 |"):
            self.assertEqual(self.checked("지원액은 120만원입니다.", quote=quote)["status"], "passed")
            self.assertEqual(self.checked("지원액은 3만원입니다.", quote=quote)["status"], "failed")
        quote = "요금(단위: 원)\n| 연도 | 사용료 | 인원 |\n|---|---|---|\n| 2026 | 13,000~19,500 | 3 |"
        self.assertEqual(self.checked("요금은 13,000~19,500원입니다.", quote=quote)["status"], "passed")
        for amount in ("2026만원", "3만원"):
            self.assertEqual(self.checked("요금은 " + amount + "입니다.", quote=quote)["status"], "failed")
        # Existing plain-number presence is deliberately preserved; it is not
        # semantic proof that a table's person/year column denotes money.
        self.assertEqual(self.checked("요금은 3원입니다.", quote=quote)["status"], "passed")
        for quote in ("(단위: 만원)\n|대상|금액|\n|청년|120|\n\n인원현황\n|구분|인원|\n|학생|3|",
                      "(단위: 만원)\n|대상|금액|인원|\n||120|3|"):
            self.assertEqual(self.checked("지원액은 3만원입니다.", quote=quote)["status"], "failed")

    def test_plain_won_native_table_and_range_preserve_presence_contract(self):
        for quote, text in (
            ("사용기준: 최초 1시간 → 금 액(원): 11,000~18,000", "사용료는 11,000~18,000원입니다."),
            ("구분: 주차요금\n1회주차시 (1시간당): 210\n1일주차권: 1,100\n월 정기권: 12,000", "월 정기권은 12,000원입니다."),
            ("구 분: 2급지\n노 외 주 차 장 > 월 정 기 권 > 전 일: 170,000", "월 정기권은 170,000원입니다.")):
            self.assertEqual(self.checked(text, quote=quote)["status"], "passed")

    def test_scalar_currency_collision_never_changes_result_into_an_untyped_number(self):
        checked = self.checked("인원은 400000명입니다.", "120만원을 3명에게 나누면?", "별도 수수료는 3원이다.",
                               expression="1200000÷3", result="400000")
        self.assertEqual(checked["status"], "failed", checked)
        self.assertIn("unverified_derived_calculation", checked["issues"], checked)

    def test_grouped_integer_arithmetic_has_no_tuple_or_function_repair(self):
        checked = self.checked("금액은 800,000원입니다.", "8천만원에 10/1000을 곱하면?",
                               expression="80,000,000 × 10/1,000", result="800,000")
        self.assertEqual(checked["status"], "passed", checked)
        for expression in ("(1, 002)", "1,20", "foo(2, 000)", "(1,002)", "ceil(2,000)",
                           "1234,567", "1,234,56", "1,000e3", "0,123", "1,000.5", "0,000"):
            with self.subTest(expression=expression), self.assertRaises((AnswerError, SyntaxError, ValueError)):
                _calculation(expression, _numbers("1002 20 2000 1234567 123456 1000000 123 0"))
        self.assertEqual(_calculation("1,000+0", _numbers("1000 0")), "1E+3")
        with self.assertRaises(AnswerError):
            _calculation("1,000+0", _numbers("1000"))
        for expression, reason in (("80,000,000 × 10/1,000", "calculation_result_mismatch"),
                                   ("8,000,000 × 10/1,000", "calculation_has_unsourced_operand")):
            with self.subTest(expression=expression):
                # The declared 80,000 is not in the answer, so the failed calculation is
                # audit-only; the answer's unsupported 800,000원 still holds it.
                checked = self.checked("금액은 800,000원입니다.", "8천만원에 10/1000을 곱하면?",
                                       expression=expression, result="80,000")
                self.assertEqual(checked["status"], "failed", checked)
                self.assertIn("answer_contains_unsupported_number", checked["issues"], checked)
                self.assertNotIn("unverified_derived_calculation", checked["issues"], checked)
                self.assertEqual([item["reason"] for item in checked["unverified_calculations_not_stated"]],
                                 [reason], checked)
                # Stating the declared result makes the answer rely on the failed calculation.
                checked = self.checked("금액은 80,000원입니다.", "8천만원에 10/1000을 곱하면?",
                                       expression=expression, result="80,000")
                self.assertIn("unverified_derived_calculation", checked["issues"], checked)
                self.assertEqual(checked["unverified_calculations_not_stated"], [], checked)


class UnstatedFailedCalculationTests(unittest.TestCase):
    """A failed calculation holds the answer only when the answer states its unsupported result."""

    def checked(self, text, quote, expression, result, question="어떻게 되나요?"):
        citations = [{"unit_id": "a", "quote": quote, "locator": {}}]
        calculations = [{"expression": expression, "result": result, "citation_ids": ["a"]}]
        return _check_claims([{"text": text, "citation_ids": ["a"]}], calculations, text, citations, question)

    def assert_audit_only(self, checked, expression, result):
        self.assertEqual(checked["status"], "passed", checked)
        self.assertEqual(checked["issues"], [], checked)
        audit = checked["unverified_calculations_not_stated"]
        self.assertEqual([(item["expression"], item["result"]) for item in audit], [(expression, result)], checked)
        self.assertTrue(all(isinstance(item["reason"], str) and item["reason"] for item in audit), checked)

    def test_unit_bearing_result_not_stated_is_audit_only(self):
        # Same-unit arithmetic now verifies (W2), so a mixed-unit calculation keeps
        # the audit-only property: it fails, and its result is not in the answer.
        quote = "주차 후 1시간까지는 요금을 면제하고, 1시간을 초과하는 시간은 100분의 50을 할인한다."
        text = "1시간까지는 면제되고, 초과하는 시간은 100분의 50을 할인합니다."
        checked = self.checked(text, quote, "3시간 - 1분", "2시간", question="3시간 주차하면 요금은?")
        self.assert_audit_only(checked, "3시간 - 1분", "2시간")
        self.assertEqual(checked["unverified_calculations_not_stated"][0]["reason"], "ValueError")
        # The same-unit form verifies and is not an audit-only failure.
        checked = self.checked(text, quote, "3시간 - 1시간", "2시간", question="3시간 주차하면 요금은?")
        self.assertEqual(checked["status"], "passed", checked)
        self.assertEqual(checked["unverified_calculations_not_stated"], [], checked)

    def test_unsourced_operand_result_not_stated_is_audit_only(self):
        quote = "1회 이용료는 150원으로 한다."
        text = "1회 이용료는 150원입니다."
        checked = self.checked(text, quote, "150 × 48", "7,200")
        self.assert_audit_only(checked, "150 × 48", "7,200")
        self.assertEqual(checked["unverified_calculations_not_stated"][0]["reason"],
                         "calculation_has_unsourced_operand")

    def test_symbolic_result_whose_number_is_not_stated_is_audit_only(self):
        quote = "이용 취소 시 이용료의 100분의 30을 공제한 나머지를 반환한다."
        text = "이용료의 100분의 30을 공제한 나머지를 반환합니다."
        checked = self.checked(text, quote, "이용료 × (1 - 30/100)", "이용료의 70%")
        self.assert_audit_only(checked, "이용료 × (1 - 30/100)", "이용료의 70%")

    def test_result_without_numbers_never_holds_but_other_unsupported_numbers_still_do(self):
        quote = "이용 취소 시 이용료의 100분의 30을 공제한 나머지를 반환한다."
        text = "이용료의 100분의 40을 공제합니다."
        checked = self.checked(text, quote, "이용료 × (1 - 30/100)", "공제 후 나머지")
        self.assertEqual(checked["status"], "failed", checked)
        self.assertEqual(checked["issues"], ["answer_contains_unsupported_number"], checked)
        self.assertEqual(checked["unsupported_numbers"], ["40"], checked)
        self.assertEqual(len(checked["unverified_calculations_not_stated"]), 1, checked)

    def test_stated_result_outside_sources_still_holds(self):
        quote = "위원회는 위원 9명으로 구성하고, 재적위원 과반수의 찬성으로 의결한다."
        text = "재적위원 9명 중 5명 이상이 찬성하면 의결됩니다."
        checked = self.checked(text, quote, "9 ÷ 2 = 4.5 → 과반수 = 5", "5")
        self.assertEqual(checked["status"], "failed", checked)
        self.assertIn("unverified_derived_calculation", checked["issues"], checked)
        self.assertIn("answer_contains_unsupported_number", checked["issues"], checked)
        self.assertEqual(checked["unsupported_numbers"], ["5"], checked)
        self.assertEqual(checked["unverified_calculations_not_stated"], [], checked)

    def test_result_stated_in_either_won_form_still_holds(self):
        quote = "사용료는 120만원으로 한다."
        for text, result in (("1인당 30만원입니다.", "300000"), ("1인당 300,000원입니다.", "300000"),
                             ("1인당 300,000원입니다.", "30만원"), ("1인당 30만원입니다.", "30만원")):
            with self.subTest(text=text, result=result):
                checked = self.checked(text, quote, "1200000÷4", result, question="3명이 나누면?")
                self.assertEqual(checked["status"], "failed", checked)
                self.assertIn("unverified_derived_calculation", checked["issues"], checked)
                self.assertEqual(checked["unverified_calculations_not_stated"], [], checked)

    def test_stated_result_that_equals_a_source_number_is_not_relied_on(self):
        # The answer's 5 is backed by the cited text itself, so it does not depend on the
        # failed calculation. This is presence checking, not semantic proof.
        quote = "위원회는 위원 5명 이상 9명 이하로 구성한다."
        text = "위원회는 위원 5명 이상 9명 이하로 구성합니다."
        checked = self.checked(text, quote, "9 ÷ 2 = 4.5 → 과반수 = 5", "5")
        self.assert_audit_only(checked, "9 ÷ 2 = 4.5 → 과반수 = 5", "5")

    def test_failed_calculation_never_supports_the_answer(self):
        quote = "1회 이용료는 150원으로 한다."
        text = "총 이용료는 7,200원입니다."
        checked = self.checked(text, quote, "150 × 48", "7,200")
        self.assertEqual(checked["status"], "failed", checked)
        self.assertEqual(checked["unsupported_numbers"], ["7200"], checked)
        self.assertIn("unverified_derived_calculation", checked["issues"], checked)

    def test_non_text_result_stays_conservative(self):
        checked = self.checked("계산했습니다.", "기준입니다.", "9 ÷ 2 = 4.5", 5)
        self.assertIn("unverified_derived_calculation", checked["issues"], checked)
        self.assertEqual(checked["unverified_calculations_not_stated"], [], checked)

    def test_verified_calculations_are_unchanged(self):
        quote = "위원은 9명, 간사는 2명으로 한다."
        checked = self.checked("합계는 11명입니다.", quote, "9 + 2", "11")
        self.assertEqual(checked["status"], "passed", checked)
        self.assertEqual(checked["unverified_calculations_not_stated"], [], checked)


class QuantityNormalizationTests(unittest.TestCase):
    """W2: header units, 만/천 quantities, percent/fraction notation and written units."""

    def test_field_label_unit_types_only_whole_plain_values(self):
        from rag_numeric_amounts import won_literals
        text = "종 별: 전기료\n금 액(원): 3,000~5,000\n금 액(원) > 체육경기 | 금 액(원) > 기타행사: 25,000~37,500\n금액(천원): 12"
        values = [(item["literal"], item["value"], item.get("conversion")) for item in won_literals(text)]
        self.assertEqual(values, [("3,000", "3E+3", "explicit_column_header_unit"),
                                  ("5,000", "5E+3", "explicit_column_header_unit"),
                                  ("25,000", "2.5E+4", "explicit_column_header_unit"),
                                  ("37,500", "3.75E+4", "explicit_column_header_unit"),
                                  ("12", "1.2E+4", "explicit_column_header_unit")])
        for text in ("인원(명): 30", "금 액(원): 3,000원 이상", "금액(원) > 요금(천원): 5", "비 고: 금액(원) 30% 가산"):
            with self.subTest(text=text):
                self.assertFalse([item for item in won_literals(text) if item.get("conversion") == "explicit_column_header_unit"])

    def test_scaled_quantities_skip_won_literals_and_bad_groups(self):
        from rag_numeric_amounts import scaled_quantities
        self.assertEqual([(item["literal"], item["value"]) for item in scaled_quantities("부지 40만㎡, 2천 명, 3만원, 5만만")],
                         [("40만", "4E+5"), ("2천", "2E+3")])

    def test_expression_quantities_and_units(self):
        from rag_numeric_amounts import normalize_expression, expression_unit, declared_quantity
        self.assertEqual(normalize_expression("40만㎡ × 0.6퍼센트"), "400000 * 0.006")
        self.assertEqual(normalize_expression("12,500 × 50%"), "12500 * 0.5")
        self.assertEqual(normalize_expression("이용료 100분의 30"), "이용료 (30/100)")
        self.assertEqual(normalize_expression("3시간 - 1시간"), "3 - 1")
        self.assertEqual(expression_unit("3시간 - 1시간"), "시간")
        self.assertEqual(expression_unit("2,000㎡ × 3"), "㎡")
        self.assertEqual(expression_unit("6시간 ÷ 2시간"), None)
        self.assertIsNone(expression_unit("25,000 ÷ 2"))
        for expression in ("3시간 - 1분", "3시간 × 2시간", "4시간 ÷ 5분", "2 ÷ 3시간", "이용료 × (1 - 30/100)"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                expression_unit(expression)
        self.assertEqual(declared_quantity("2,400"), ("2.4E+3", None))
        self.assertEqual(declared_quantity("2시간"), ("2", "시간"))
        self.assertEqual(declared_quantity("8,000원"), ("8E+3", "원"))
        for result in ("다음 해 3월 31일", "이용료의 70%", "2시간 30분", "약 5"):
            with self.subTest(result=result), self.assertRaises(ValueError):
                declared_quantity(result)


class ClosedOperandSourceTests(unittest.TestCase):
    """W2: operands from cited text, the question, or earlier verified results only."""

    def checked(self, claims, calculations, rows, question="얼마인가요?"):
        answer = "\n\n".join(text for text, _ in claims)
        return _check_claims([{"text": text, "citation_ids": ids} for text, ids in claims], calculations, answer,
                             rows, question)

    def test_chained_header_unit_calculation_supports_stated_amounts(self):
        # Q06 shape: 2-hour price range, 50% discount past 2 hours, 4 hours asked.
        rows = [{"unit_id": "r", "locator": {}, "quote": "사용기준: 1면당, 2시간당\n금 액(원): 25,000~37,500\n"
                 "비 고: ◦2시간 초과시 초과시간당 이용료는 시간당 이용료의 50% 할인"}]
        calcs = [{"expression": e, "result": r, "citation_ids": ["r"]} for e, r in (
            ("25,000 ÷ 2", "12,500"), ("12,500 × 0.5", "6,250"), ("6,250 × 2", "12,500"), ("25,000 + 12,500", "37,500"))]
        text = "시간당 12,500원, 할인 후 시간당 6,250원이므로 4시간 총 이용료는 37,500원입니다."
        checked = self.checked([(text, ["r"])], calcs, rows, "실내 테니스장을 4시간, 2시간 넘긴 시간 할인 포함하면?")
        self.assertEqual(checked["status"], "passed", checked)
        chained = [item for item in checked["calculation_operand_provenance"] if "verified_calculation_operands" in item]
        self.assertTrue(chained, checked)
        # An unsourced number in the chain still fails, and stating its result holds.
        bad = calcs + [{"expression": "37,500 × 3", "result": "112,500", "citation_ids": ["r"]}]
        checked = self.checked([(text + " 세 면이면 112,500원입니다.", ["r"])], bad, rows, "4시간, 2시간 넘긴 시간 할인 포함하면?")
        self.assertIn("unverified_derived_calculation", checked["issues"], checked)
        self.assertEqual(checked["unsupported_numbers"], ["112500"], checked)

    def test_chain_uses_only_earlier_results_within_own_citations(self):
        rows = [{"unit_id": "a", "locator": {}, "quote": "기본 요금은 10,000원이다."},
                {"unit_id": "b", "locator": {}, "quote": "할인율은 30%이다."}]
        later_first = [{"expression": "5,000 × 2", "result": "10,000", "citation_ids": ["a"]},
                       {"expression": "10,000 ÷ 2", "result": "5,000", "citation_ids": ["a"]}]
        checked = self.checked([("요금은 10,000원입니다.", ["a"])], later_first, rows, "2명이 나누면 얼마인가요?")
        self.assertEqual([item["expression"] for item in checked["unverified_calculations_not_stated"]], ["5,000 × 2"])
        other_citation = [{"expression": "10,000 × 0.3", "result": "3,000", "citation_ids": ["a", "b"]},
                          {"expression": "3,000 × 2", "result": "6,000", "citation_ids": ["a"]}]
        checked = self.checked([("할인액 3,000원의 두 배는 6,000원입니다.", ["a", "b"])], other_citation, rows,
                               "할인액의 2배는 얼마인가요?")
        self.assertIn("unverified_derived_calculation", checked["issues"], checked)
        self.assertEqual(checked["unsupported_numbers"], ["6000"], checked)

    def test_question_quantities_and_percent_words_are_operands(self):
        # Q03 shape: 40만㎡ site from the question, 0.6퍼센트 from the cited text.
        rows = [{"unit_id": "p", "locator": {"article": "제17조", "paragraph": 4},
                 "quote": "④ 노외주차장의 규모는 해당 사업부지면적의 0.6퍼센트 이상의 면적으로 한다."}]
        calcs = [{"expression": "400,000 × 0.006", "result": "2,400", "citation_ids": ["p"]}]
        text = "사업부지면적이 40만㎡이면 0.6퍼센트는 2,400㎡이므로 계획한 2,000㎡는 기준에 미달합니다."
        checked = self.checked([(text, ["p"])], calcs, rows, "부지면적 40만㎡ 사업에 노외주차장 2,000㎡면 충족하나요?")
        self.assertEqual(checked["status"], "passed", checked)
        checked = self.checked([(text, ["p"])], calcs, rows, "노외주차장 2,000㎡면 충족하나요?")
        self.assertIn("2400", checked["unsupported_numbers"], checked)
        self.assertIn("unverified_derived_calculation", checked["issues"], checked)

    def test_fraction_and_unit_results(self):
        rows = [{"unit_id": "f", "locator": {}, "quote": "1시간을 초과하는 시간은 주차요금의 100분의 50을 할인한다."}]
        calcs = [{"expression": "3시간 - 1시간", "result": "2시간", "citation_ids": ["f"]},
                 {"expression": "1,000원 × 100분의 50", "result": "500원", "citation_ids": ["f"]}]
        text = "3시간 중 할인되는 시간은 2시간이고, 1,000원 요금이면 500원이 할인됩니다."
        checked = self.checked([(text, ["f"])], calcs, rows, "3시간 주차하고 요금이 1,000원이면?")
        self.assertEqual(checked["status"], "passed", checked)
        for expression, result in (("3시간 - 1시간", "2분"), ("1,000원 × 3시간", "3,000원"), ("3시간 - 1시간", "2원")):
            with self.subTest(expression=expression, result=result):
                checked = self.checked([("결과는 " + result + "입니다.", ["f"])],
                                       [{"expression": expression, "result": result, "citation_ids": ["f"]}], rows,
                                       "3시간 주차하고 요금이 1,000원이면?")
                self.assertIn("unverified_derived_calculation", checked["issues"], checked)

    def test_header_unit_row_sum_supports_won_total(self):
        # M12 shape: two rows whose values carry the "(원)" field label.
        rows = [{"unit_id": "tent", "locator": {}, "quote": "종 별: 물품대여료\n금 액(원): 5,000~20,000"},
                {"unit_id": "power", "locator": {}, "quote": "종 별: 전기료\n금 액(원): 3,000~5,000"}]
        calcs = [{"expression": "5,000 + 3,000", "result": "8,000", "citation_ids": ["tent", "power"]},
                 {"expression": "20,000 + 5,000", "result": "25,000", "citation_ids": ["tent", "power"]}]
        text = "최소 8,000원(텐트 5,000원 + 전기 3,000원), 최대 25,000원(텐트 20,000원 + 전기 5,000원)입니다."
        checked = self.checked([(text, ["tent", "power"])], calcs, rows)
        self.assertEqual(checked["status"], "passed", checked)
        checked = self.checked([("합계는 9,000원입니다.", ["tent", "power"])], calcs, rows)
        self.assertEqual(checked["unsupported_numbers"], ["9000"], checked)


if __name__ == "__main__":
    unittest.main()
