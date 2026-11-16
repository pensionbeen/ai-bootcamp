"""Synthetic, offline checks for supplement provenance and cautious references.

No real project files, API calls, or filesystem fixtures are used.
"""

import copy
import json
import unittest
import xml.etree.ElementTree as ET

import build_supplement_context as supplements


class SupplementContextTests(unittest.TestCase):
    def setUp(self):
        self.chunks = [
            {"article": article, "paragraph": paragraph, "chunk_id": chunk_id}
            for article, paragraph, chunk_id in (
                ("제1조", 1, "2171833:000100:1"),
                ("제2조", 1, "2171833:000200:1"),
                ("제7조", 1, "2171833:000700:1"),
                ("제7조", 2, "2171833:000700:2"),
                ("제25조의3", 2, "2171833:002503:2"),
            )
        ]

    @staticmethod
    def header(day, number):
        return f"부칙 <제{int(number)}호, {int(day[:4])}.{int(day[4:6])}.{int(day[6:])}>"

    def fixture(self, records, current_day="20261001", current_number="10187"):
        root = ET.Element("자치법규")
        basic = ET.SubElement(root, "자치법규기본정보")
        ET.SubElement(basic, "공포일자").text = current_day
        ET.SubElement(basic, "공포번호").text = current_number
        container = ET.SubElement(root, "부칙")
        for day, number, text in records:
            ET.SubElement(container, "부칙공포일자").text = day
            ET.SubElement(container, "부칙공포번호").text = number
            ET.SubElement(container, "부칙내용").text = text
        raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        entry = {
            "ordin_id": "2000351", "ordin_seq": "2171833",
            "ordin_name": "서울특별시 주차장 설치 및 관리 조례",
            "as_of_date": "2026-10-01", "effective_date": "2026-10-01",
            "content_hash": supplements.body.digest(raw),
            "raw_file": "data/raw/2171833.xml",
            "source_url": "https://www.law.go.kr/ordinInfoP.do?ordinSeq=2171833",
            "supplement_block_count": len(records),
        }
        return raw, entry

    def block(self, body_text, current=True):
        day, number = ("20261001", "010187") if current else ("20090318", "04752")
        text = self.header(day, number) + "\n" + body_text
        records = [(day, number, text)]
        if not current:
            records.append(("20261001", "10187", self.header("20261001", "10187") + "\n이 조례는 공포한 날부터 시행한다.\n"))
        raw, entry = self.fixture(records)
        return supplements.parse_blocks(raw, entry)[0]

    def analyze(self, block):
        before = copy.deepcopy(block)
        result = supplements.analyze_block(block, self.chunks)
        self.assertEqual(block, before)
        for occurrence in result["mentions"] + result["date_expressions"]:
            span = occurrence["source_span"]
            self.assertEqual(occurrence["quote_text"], block["source_text"][span["start"]:span["end"]])
        self.assertFalse(result["signals_are_legal_classification"])
        self.assertEqual(result["date_interpretation"], "literal_only_no_effective_date_inferred")
        return result

    @staticmethod
    def article(note):
        return {
            "article_id": "2171833:002503", "article": "제25조의3",
            "source_text": "제25조의3(시험) 본문을 보존한다.\n" + note + "\n",
            "article_notes": [note], "chunk_ids": ["2171833:002503:2"],
        }

    def test_source_whitespace_and_raw_leading_zero_number_are_preserved(self):
        text = "\n\t" + self.header("20261001", "010187") + "  \n\n이 조례는 공포한 날부터 시행한다.  \n\t"
        raw, entry = self.fixture([("20261001", "010187", text)])
        block = supplements.parse_blocks(raw, entry)[0]
        self.assertEqual(block["source_text"], text)
        self.assertEqual(block["source_text_hash"], supplements.body.digest(text.encode("utf-8")))
        self.assertEqual(block["promulgation_number_raw"], "010187")
        self.assertEqual(block["promulgation_number_normalized"], "10187")
        self.assertTrue(block["is_current_amendment"])
        self.assertEqual(block["source_location"]["xml_path"], "./부칙[1]/부칙내용[1]")
        self.assertEqual(block["applicability_status"], "not_determined")
        self.assertFalse(block["answer_allowed"])
        self.assertNotIn("effective_date", block)

    def test_same_day_different_numbers_remain_distinct_and_only_one_is_current(self):
        records = [
            ("20261001", number, self.header("20261001", number) + "\n시행 원문\n")
            for number in ("010186", "010187")
        ]
        raw, entry = self.fixture(records)
        blocks = supplements.parse_blocks(raw, entry)
        self.assertEqual(len({b["supplement_id"] for b in blocks}), 2)
        self.assertEqual([b["is_current_amendment"] for b in blocks], [False, True])
        self.assertEqual([b["source_location"]["content_ordinal"] for b in blocks], [1, 2])

    def test_missing_or_reordered_triplet_fields_are_rejected(self):
        raw, entry = self.fixture([("20261001", "10187", self.header("20261001", "10187") + "\n시행문")])
        for change in ("missing_date", "missing_number", "wrong_order", "extra_tag"):
            with self.subTest(change=change):
                root = ET.fromstring(raw)
                container = root.find("부칙")
                if change == "missing_date":
                    container.remove(container.find("부칙공포일자"))
                elif change == "missing_number":
                    container.remove(container.find("부칙공포번호"))
                elif change == "wrong_order":
                    first = container[0]
                    container.remove(first)
                    container.insert(1, first)
                else:
                    ET.SubElement(container, "예상밖").text = "값"
                with self.assertRaisesRegex(ValueError, "묶음 누락|태그 순서"):
                    supplements.parse_blocks(ET.tostring(root, encoding="utf-8"), entry)

    def test_header_day_and_number_must_match_their_metadata(self):
        for header_day, header_number in (("20260930", "10187"), ("20261001", "10186")):
            with self.subTest(day=header_day, number=header_number):
                raw, entry = self.fixture([("20261001", "10187", self.header(header_day, header_number) + "\n시행문")])
                with self.assertRaisesRegex(ValueError, "제목과 공포정보 불일치"):
                    supplements.parse_blocks(raw, entry)

    def test_duplicate_normalized_identity_and_missing_current_block_are_rejected(self):
        text = self.header("20261001", "10187") + "\n시행문"
        raw, entry = self.fixture([("20261001", "010187", text), ("20261001", "10187", text)])
        with self.assertRaisesRegex(ValueError, "중복 부칙"):
            supplements.parse_blocks(raw, entry)
        raw, entry = self.fixture([("20090318", "04752", self.header("20090318", "04752") + "\n옛 시행문")])
        with self.assertRaisesRegex(ValueError, "현재 개정본의 부칙"):
            supplements.parse_blocks(raw, entry)

    def test_block_count_must_match_the_manifest(self):
        raw, entry = self.fixture([("20261001", "10187", self.header("20261001", "10187") + "\n시행문")])
        entry["supplement_block_count"] = 2
        with self.assertRaisesRegex(ValueError, "블록 수와 manifest"):
            supplements.parse_blocks(raw, entry)

    def test_promulgation_after_cutoff_is_rejected(self):
        raw, entry = self.fixture(
            [("20261002", "10187", self.header("20261002", "10187") + "\n시행문")],
            current_day="20261002",
        )
        with self.assertRaisesRegex(ValueError, "기준일 이후 공포"):
            supplements.parse_blocks(raw, entry)

    def test_current_parking_style_reference_links_only_body_article_seven_paragraph_one(self):
        block = self.block(
            "제1조(시행일) 이 조례는 공포한 날부터 시행한다.\n"
            "제2조(종전 보훈신분증에 관한 경과조치) 이 조례 시행 당시 종전에 발급되어 "
            "2028년 6월 4일까지 사용할 수 있는 보훈신분증은 같은 날까지 "
            "제7조제1항의 개정규정에 따른 신분증으로 본다.\n"
        )
        result = self.analyze(block)
        self.assertEqual(len(result["textual_links"]), 1)
        link = result["textual_links"][0]
        self.assertEqual(link["quote_text"], "제7조제1항")
        self.assertEqual(link["target_chunk_ids"], ["2171833:000700:1"])
        self.assertEqual(link["resolution"], "exact_current_text_reference")
        self.assertEqual(link["applicability_status"], "not_determined")
        self.assertFalse(link["answer_allowed"])
        self.assertEqual([m["resolution"] for m in result["mentions"][:2]], ["supplement_own_heading"] * 2)

    def test_historical_number_matching_current_body_is_not_linked(self):
        result = self.analyze(self.block("제1조(경과조치) 제7조제1항의 개정규정을 적용한다.\n", current=False))
        self.assertEqual(result["textual_links"], [])
        mention = next(m for m in result["mentions"] if m["quote_text"] == "제7조제1항")
        self.assertEqual(mention["resolution"], "historical_reference_requires_version_review")
        self.assertEqual(mention["target_chunk_ids"], [])

    def test_external_law_and_ambiguous_scope_stop_current_body_linking(self):
        examples = (
            "제1조(다른 조례의 개정) 다른 조례의 제7조제1항을 개정한다.",
            "「외부 조례」 제7조제1항을 따른다.",
            "법률 제1234호 부칙 제7조를 따른다.",
            "법 제7조제1항에 따른 기준을 확인한다.",
            "시행령 제7조제1항에 따른 기준을 확인한다.",
            "시행규칙 제7조제1항에 따른 기준을 확인한다.",
            "제7조부터 제8조까지 생략한다.",
            "같은 조의 규정과 제7조제1항을 적용한다.",
            "제7조 및 제8조를 개정한다.",
        )
        for text in examples:
            with self.subTest(text=text):
                result = self.analyze(self.block(text))
                self.assertTrue(result["scope_requires_review"])
                self.assertEqual(result["textual_links"], [])
                mentions = [m for m in result["mentions"] if m["resolution"] != "supplement_own_heading"]
                self.assertTrue(mentions)
                self.assertTrue(all(m["resolution"] == "scope_requires_review" and not m["target_chunk_ids"] for m in mentions))

    def test_own_supplement_headings_are_not_body_references(self):
        result = self.analyze(self.block("  제1조(시행일) 이 조례는 공포한 날부터 시행한다.\n\t제2조 삭제\n"))
        self.assertEqual(result["textual_links"], [])
        self.assertEqual(len(result["mentions"]), 2)
        self.assertTrue(all(m["resolution"] == "supplement_own_heading" for m in result["mentions"]))

    def test_branch_and_paragraph_are_matched_exactly_and_missing_target_is_not_substituted(self):
        result = self.analyze(self.block("제25조의3제2항에 따른 표시를 확인한다. 제7조제3항의 기준도 확인한다."))
        self.assertEqual(len(result["textual_links"]), 1)
        self.assertEqual(result["textual_links"][0]["target_chunk_ids"], ["2171833:002503:2"])
        missing = next(m for m in result["mentions"] if m["quote_text"] == "제7조제3항")
        self.assertEqual(missing["resolution"], "target_not_found")
        self.assertEqual(missing["target_chunk_ids"], [])

    def test_item_reference_is_kept_for_review_without_coarsening_to_paragraph(self):
        result = self.analyze(self.block("제7조제1항제2호에 따른 처분을 확인한다."))
        self.assertEqual(result["textual_links"], [])
        self.assertEqual(result["mentions"][0]["item"], 2)
        self.assertEqual(result["mentions"][0]["resolution"], "scope_requires_review")
        self.assertEqual(result["mentions"][0]["quote_text"], "제7조제1항제2호")

    def test_future_date_inside_valid_block_is_preserved_as_literal_without_expiration(self):
        block = self.block("이 조례는 공포 후 3개월이 경과한 날부터 시행한다.\n종전 신분증은 2028년 6월 4일까지 사용할 수 있다.")
        result = self.analyze(block)
        self.assertIn("2028년 6월 4일", [d["quote_text"] for d in result["date_expressions"]])
        self.assertIn("공포 후 3개월", block["source_text"])
        self.assertEqual(block["applicability_status"], "not_determined")
        self.assertNotIn("effective_date", result)
        self.assertNotIn("expired", result)
        self.assertFalse(block["answer_allowed"])

    def test_note_citation_links_by_both_promulgation_number_and_day(self):
        block = self.block("원문을 보존한다.")
        note = "[조례 제010187호(2026. 10. 1.)에 따른 원문 주석]"
        article = self.article(note)
        links = supplements.link_article_notes([article], [block])
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertEqual(link["supplement_id"], block["supplement_id"])
        self.assertEqual(link["relationship"], "explicit_promulgation_citation")
        span = link["note_source_span"]
        self.assertEqual(article["source_text"][span["start"]:span["end"]], note)
        self.assertFalse(link["answer_allowed"])

    def test_wrong_note_number_or_day_remains_unresolved(self):
        block = self.block("원문을 보존한다.")
        for note in ("[조례 제10186호(2026.10.1.)에 따른 주석]", "[조례 제10187호(2026.9.30.)에 따른 주석]"):
            with self.subTest(note=note):
                links = supplements.link_article_notes([self.article(note)], [block])
                self.assertEqual(len(links), 1)
                self.assertIsNone(links[0]["supplement_id"])
                self.assertEqual(links[0]["relationship"], "cited_supplement_missing")
                self.assertEqual(links[0]["applicability_status"], "not_determined")

    def test_different_stated_end_dates_raise_review_signal_not_legal_conflict_or_expiration(self):
        block = self.block("이 규정은 2028년 6월 4일까지 적용한다.")
        note = "[조례 제10187호(2026.10.1.)에 따라 이 조는 2020년 12월 31일까지 유효]"
        article = self.article(note)
        before = copy.deepcopy((article, block))
        link = supplements.link_article_notes([article], [block])[0]
        self.assertTrue(link["different_stated_end_dates"])
        self.assertEqual(link["note_stated_end_dates"], ["2020-12-31"])
        self.assertEqual(link["block_stated_end_dates"], ["2028-06-04"])
        self.assertEqual(link["comparison_scope"], "whole_block_date_strings_only_not_legal_conflict_determination")
        self.assertEqual(link["applicability_status"], "not_determined")
        self.assertFalse(link["answer_allowed"])
        self.assertNotIn("expired", link)
        self.assertEqual((article, block), before)

    def test_note_citation_with_multiple_matching_blocks_is_rejected(self):
        block = self.block("원문을 보존한다.")
        duplicate = copy.deepcopy(block)
        duplicate["supplement_id"] = "2171833:supplement:9999"
        note = "[조례 제10187호(2026.10.1.)에 따른 주석]"
        with self.assertRaisesRegex(ValueError, "복수 부칙"):
            supplements.link_article_notes([self.article(note)], [block, duplicate])


class TemporalReviewRecordTests(unittest.TestCase):
    def fixture(self):
        chunks_file = "data/processed/ordinances/2026-10-01/123.chunks.json"
        xml = "<root><text>과거 규정의 변경 기록</text></root>".encode()
        files = {"data/raw/history.xml": xml,
                 chunks_file: json.dumps([{"chunk_id": "123:002000:4", "parent_id": "123:002000"}]).encode()}
        record = {"schema_version": 1, "case_id": "fixture", "case_status": "resolved_by_official_amendment_text",
                  "as_of_date": "2026-10-01", "selected_ordin_seq": "123", "selected_article_id": "123:002000",
                  "context_chunk_ids": ["123:002000:4"], "source_review": "pending", "answer_allowed": False,
                  "answer_generator_connected": False, "input_files": {n: supplements.body.digest(v) for n,v in files.items()},
                  "evidence_quotes": [{"evidence_id": "q1", "file": "data/raw/history.xml", "xml_path": "text",
                                       "source_span": {"start": 0, "end": 5}, "quote": "과거 규정"}],
                  "findings": [{"claim": "검토 기록", "evidence_ids": ["q1"]}]}
        return record, files

    def test_review_preserves_unpublished_state_and_validates_exact_quotes(self):
        record, files = self.fixture()
        self.assertEqual(supplements.validate_review_record(record, files.__getitem__), "fixture")
        self.assertFalse(record["answer_allowed"])

    def test_changed_evidence_and_modified_quote_are_rejected(self):
        record, files = self.fixture()
        changed = {**files, "data/raw/history.xml": files["data/raw/history.xml"] + b" "}
        with self.assertRaisesRegex(ValueError, "해시 불일치"):
            supplements.validate_review_record(record, changed.__getitem__)
        record["evidence_quotes"][0]["quote"] = "다른 문구"
        with self.assertRaisesRegex(ValueError, "인용문 불일치"):
            supplements.validate_review_record(record, files.__getitem__)

    def test_wrong_current_article_and_missing_finding_evidence_are_rejected(self):
        record, files = self.fixture()
        record["selected_article_id"] = "123:009900"
        with self.assertRaisesRegex(ValueError, "현재 조문 연결"):
            supplements.validate_review_record(record, files.__getitem__)
        record, files = self.fixture()
        record["findings"][0]["evidence_ids"] = ["missing"]
        with self.assertRaisesRegex(ValueError, "근거 인용 연결"):
            supplements.validate_review_record(record, files.__getitem__)

    def test_case_review_cannot_enable_final_answers(self):
        record, files = self.fixture()
        record["answer_allowed"] = True
        with self.assertRaisesRegex(ValueError, "전체 승인 상태"):
            supplements.validate_review_record(record, files.__getitem__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
