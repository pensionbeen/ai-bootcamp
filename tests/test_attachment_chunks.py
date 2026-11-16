"""Offline regression checks for attachment candidates, not legal correctness.

Only manifest-listed staging data is copied into a test-owned directory. Tests
never read credentials, contact services, or write to the live project.
"""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import build_attachment_search_chunks as chunks


PROJECT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
TEMP_PREFIX = "chunk-test-"
OUTPUT_NAMES = ("candidate_chunks.json", "chunk_report.json", "preview.html", "chunks.manifest.json")


def read_json(relative):
    return json.loads((PROJECT / relative).read_bytes())


def required_contexts(document, chunk, kind=None):
    return [context for context in document["contexts"]
            if context["context_id"] in chunk["required_context_ids"]
            and (kind is None or context["kind"] == kind)]


class TableGridTests(unittest.TestCase):
    def test_spans_header_paths_and_exact_inner_html_survive(self):
        markup = ('<table>\r\n<tr><th rowspan="2">구분</th><th colspan="2">요금</th></tr>\r\n'
                  '<tr><th>주간</th><th>야간</th></tr>\r\n'
                  '<tr><th>1급지</th><td> 100&nbsp;원<br/>예외 </td><td>200</td></tr></table>')
        result = chunks.parse_table(markup)
        self.assertEqual((result["rows"], result["columns"], result["header_rows"]), (3, 3, 2))
        self.assertEqual(result["grid"], [[0, 1, 1], [0, 2, 3], [4, 5, 6]])
        self.assertEqual(chunks.header_path(result, 0), ["구분"])
        self.assertEqual(chunks.header_path(result, 2), ["요금", "야간"])
        self.assertEqual(result["cells"][5]["html"], " 100&nbsp;원<br/>예외 ")
        self.assertEqual(result["cells"][5]["text"], "100 원 예외")
        self.assertEqual(result["cells"][4]["tag"], "th")

    def test_headerless_continuation_is_not_given_invented_headers(self):
        table = chunks.parse_table('<table><tr><td></td><td>계속</td></tr></table>')
        self.assertEqual(table["header_rows"], 0)
        self.assertEqual(table["cells"][0]["text"], "")
        with self.assertRaises(ValueError):
            chunks.table_candidates({}, {}, {}, '<table><tr><td>계속</td></tr></table>', [])

    def test_ambiguous_or_incomplete_geometry_is_rejected(self):
        invalid = [
            '', '<table></table>', '<table><tr><td>x</tr></table>',
            '<table><tr><td>x</th></tr></table>', '<table><td>x</td></table>',
            '<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>',
            '<table><tr><td><td>x</td></td></tr></table>',
            '<table><tr><td>x</td></tr><tr><td>a</td><td>b</td></tr></table>',
            '<table><tr><td rowspan="2">x</td></tr></table>',
            '<table><tr><td>a</td><td rowspan="2">b</td></tr>'
            '<tr><td colspan="2">overlap</td></tr></table>',
            '<table><tr><td rowspan="1" rowspan="2">x</td></tr></table>',
            '<table><tr><td colspan="0">x</td></tr></table>',
            '<table><tr><td colspan="-1">x</td></tr></table>',
            '<table><tr><td colspan="1.5">x</td></tr></table>',
            '<table><tr><td colspan="1001">x</td></tr></table>',
            '<table><tr><td>x</td></tr></table><table><tr><td>y</td></tr></table>',
        ]
        for markup in invalid:
            with self.subTest(markup=markup), self.assertRaises(ValueError):
                chunks.parse_table(markup)


@unittest.skipUnless((PROJECT / "data/processed/attachments/2026-10-01/chunks/candidate_chunks.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/processed/attachments/2026-10-01/chunks/candidate_chunks.json")
class CandidateMeaningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = read_json(chunks.FOLDER + "/candidate_chunks.json")
        cls.report = read_json(chunks.FOLDER + "/chunk_report.json")
        cls.docs = {d["attachment_id"]: d for d in cls.data["documents"]}

    def test_counts_are_document_dependent_and_no_candidate_is_approved(self):
        all_chunks = [c for doc in self.docs.values() for c in doc["chunks"]]
        self.assertEqual(len(self.docs), 14)
        self.assertEqual(len(all_chunks), 93)
        self.assertEqual(len({c["chunk_id"] for c in all_chunks}), 93)
        self.assertEqual(self.report["correction_count"], 3)
        self.assertEqual(self.report["manual_binding_held_chunks"], 13)
        self.assertEqual(self.report["image_metadata_only_chunks"], 3)
        self.assertFalse(self.report["embedding_performed"])
        self.assertEqual({len(d["chunks"]) for d in self.docs.values()}, {1, 2, 3, 8, 13, 34})
        for chunk in all_chunks:
            self.assertEqual(chunk["source_review"], "pending")
            self.assertIs(chunk["answer_allowed"], False)
            self.assertIs(chunk["embedding_allowed"], False)

    def test_family_one_space_does_not_inherit_two_space_clearance(self):
        doc = self.docs["2171833:22740735"]
        one = next(c for c in doc["chunks"] if c["label"] == "확장형(1면)")
        fields = {f["name"]: f["value"] for f in one["fields"]}
        self.assertIn("2.6m", " ".join(fields.values()))
        self.assertNotIn("0.35", one["text"])
        self.assertIn("도면에 미표기", fields.values())
        # The warning mentions 0.35 to explain why it must not become a value.
        context = required_contexts(doc, one, "reviewed_record_context")[0]
        self.assertIn("확장형(2면)", context["text"])
        self.assertIn("0.35", context["text"])
        self.assertEqual(one["source_refs"][0]["representation"], "image_and_xml_transcription")

    def test_family_two_space_diagram_dimension_and_minimum_are_separate(self):
        two = next(c for c in self.docs["2171833:22740735"]["chunks"] if c["label"] == "확장형(2면)")
        values = [f["value"] for f in two["fields"]]
        self.assertIn("각 면 2.8m", values)
        self.assertTrue(any("2.6m이상" in value for value in values))
        self.assertTrue(any("0.7m" in value and "0.35m" in value for value in values))
        self.assertNotEqual(next(v for v in values if "2.8m" in v), next(v for v in values if "2.6m이상" in v))

    def test_camping_age_fee_pairs_and_blank_notes_are_not_guessed(self):
        doc = self.docs["2147709:22415979"]
        camping = [c for c in doc["chunks"] if c["source_refs"][0]["element_id"] == 5]
        seoul = [c for c in camping if c["label"].startswith("서울대공원")]
        self.assertEqual(len(seoul), 3)
        expected = {"어른 1회당": "2,000~4,000", "청소년 1회당": "1,500~3,000", "어린이 1회당": "1,000~2,000"}
        actual = {}
        for chunk in seoul:
            fields = chunk["fields"]
            actual[fields[2]["value"]] = fields[3]["value"]
            self.assertEqual(fields[-1]["value"], "")
            self.assertNotIn("ditto_reference", fields[-1])
            self.assertNotIn("30%", chunks.assemble_chunk(doc, chunk))
            self.assertTrue(all(f["cell_origin_basis"] == "corrected_html" for f in fields))
            self.assertEqual(chunk["source_refs"][0]["representation"], "corrected_html")
        self.assertEqual(actual, expected)

    def test_common_camping_discount_stops_at_merged_cell_boundary(self):
        camping = [c for c in self.docs["2147709:22415979"]["chunks"]
                   if c["source_refs"][0]["element_id"] == 5]
        for chunk in camping:
            row = chunk["source_refs"][0]["row_index"]
            self.assertEqual("30%할인" in chunk["text"], row in (1, 2, 3))
            if row in (1, 2, 3):
                note = chunk["fields"][-1]
                self.assertEqual(note["cell_origin"]["row"], 1)
                self.assertEqual(note["cell_origin"]["rowspan"], 3)

    def test_delegation_thirteen_records_preserve_the_single_authority_exception(self):
        doc = self.docs["2147709:22416027"]
        self.assertEqual([c["label"] for c in doc["chunks"]],
                         ["1가", "1나", "1다", "2가", "2나", "2다", "2라", "2마", "2바", "2사", "2아", "2자", "2차"])
        target = next(c for c in doc["chunks"] if c["label"] == "1다")
        fields = {f["name"]: f["value"] for f in target["fields"]}
        self.assertEqual(fields["수임기관"], "구청장")
        self.assertEqual(fields["근거법령 원문 표기"], "같은 법률 제16조")
        for chunk in doc["chunks"]:
            if chunk is not target:
                self.assertIn("서울식물원장", next(f["value"] for f in chunk["fields"] if f["name"] == "수임기관"))

    def test_delegation_group_one_proviso_does_not_leak_to_group_two(self):
        doc = self.docs["2147709:22416027"]
        for chunk in doc["chunks"]:
            contexts = required_contexts(doc, chunk, "reviewed_record_context")
            self.assertEqual(len(contexts), 1)
            self.assertEqual("시장이 입안·설치" in contexts[0]["text"], chunk["label"].startswith("1"))

    def test_legacy_thirteen_rows_preserve_original_text_and_manual_hold(self):
        doc = self.docs["2171833:22740725"]
        old = read_json("data/processed/attachments/2171833_22740725.search_chunks.json")
        self.assertEqual(len(doc["chunks"]), 13)
        self.assertEqual([c["text"] for c in doc["chunks"]], [c["text"] for c in old["chunks"]])
        self.assertEqual(doc["contexts"][0]["text"], "\n".join(b["text"] for b in old["attachment_context"]["blocks"]))
        self.assertEqual(len(old["attachment_context"]["blocks"]), 20)
        for chunk in doc["chunks"]:
            self.assertEqual(chunk["input_binding_status"], "manual_export_unverified")
            self.assertEqual(chunk["hold_reason"], "manual_export_input_binding_unverified")
            self.assertFalse(chunk["embedding_allowed"])

    def test_legacy_page_continuation_and_note_eleven_parent_are_preserved(self):
        doc = self.docs["2171833:22740725"]
        row = next(c for c in doc["chunks"] if c["label"].startswith("10."))
        self.assertEqual({(r["page"], r["element_id"]) for r in row["source_refs"]}, {(1, 2), (2, 3)})
        for value in ("400m", "250m", "200m"):
            self.assertIn(value, row["text"])
        for chunk, rate in zip(doc["chunks"][-2:], ("40", "70")):
            self.assertIn("상위 조건 — 비고 11:", chunk["text"])
            self.assertIn("승용차공동이용", chunk["text"])
            self.assertIn("100분의 " + rate, chunk["text"])
            self.assertEqual({r["element_id"] for r in chunk["source_refs"]}, {22, 23})

    def test_required_context_list_or_registry_removal_is_rejected(self):
        for mode in ("list", "registry"):
            with self.subTest(mode=mode):
                doc = copy.deepcopy(self.docs["2147709:22415979"])
                chunk = doc["chunks"][0]
                self.assertGreater(len(chunk["required_context_ids"]), 1)
                if mode == "list":
                    chunk["required_context_ids"].pop()
                else:
                    missing = chunk["required_context_ids"][-1]
                    doc["contexts"] = [c for c in doc["contexts"] if c["context_id"] != missing]
                with self.assertRaises(ValueError):
                    chunks.assemble_chunk(doc, chunk)

    def test_related_body_loss_duplicate_context_or_foreign_chunk_is_rejected(self):
        for mode in ("body", "duplicate", "foreign"):
            with self.subTest(mode=mode):
                doc = copy.deepcopy(self.docs["2171833:22740723"])
                chunk = doc["chunks"][0]
                self.assertTrue(chunk["related_body_chunk_ids"])
                if mode == "body":
                    chunk["related_body_chunk_ids"].pop()
                elif mode == "duplicate":
                    doc["contexts"].append(copy.deepcopy(doc["contexts"][0]))
                else:
                    chunk = self.docs["2171833:22740729"]["chunks"][0]
                with self.assertRaises(ValueError):
                    chunks.assemble_chunk(doc, chunk)

    def test_parking_note_crossing_page_two_and_three_stays_together(self):
        doc = self.docs["2171833:22740727"]
        context = next(c for c in doc["contexts"] if c["kind"] == "ordered_attachment_text_across_pages")
        self.assertIn("부지\n의 소유권", context["text"])
        refs = {(r["page"], r["element_id"]) for r in context["source_refs"]}
        self.assertTrue({(2, 24), (3, 25)}.issubset(refs))
        for chunk in doc["chunks"]:
            self.assertIn(context["context_id"], chunk["required_context_ids"])
            self.assertIn(context["text"], chunks.assemble_chunk(doc, chunk))

    def test_unreviewed_figures_supply_metadata_not_provider_descriptions(self):
        for ident in ("2171833:22740737", "2171833:22740739", "2171833:22740781"):
            doc = self.docs[ident]
            chunk, = doc["chunks"]
            self.assertEqual(chunk["kind"], "image_metadata_only")
            self.assertEqual(chunk["text"], doc["title"])
            self.assertEqual(chunk["hold_reason"], "unverified_visual_content_excluded_from_text")
            layout = read_json(chunks.parse.FOLDER + "/" + ident.replace(":", "_") + ".layout_evidence.json")
            excluded = {(e["page"], e["element_id"]) for e in layout["elements"] if e["category"] in ("figure", "chart")}
            refs = {(r["page"], r["element_id"]) for c in doc["contexts"] for r in c["source_refs"]}
            self.assertTrue(excluded.isdisjoint(refs))

    def test_footer_definitions_are_context_for_both_rows_not_an_extra_rate(self):
        doc = self.docs["2147709:22415977"]
        self.assertEqual(len(doc["chunks"]), 2)
        for chunk in doc["chunks"]:
            notes = required_contexts(doc, chunk, "table_note")
            self.assertEqual(len(notes), 1)
            for term in ("어린이", "청소년", "어른", "단체"):
                self.assertIn(term, "".join(notes[0]["text"].split()))
            self.assertIn(notes[0]["text"], chunks.assemble_chunk(doc, chunk))

    def test_four_facility_headings_and_merged_header_paths_remain_local(self):
        doc = self.docs["2147709:22415979"]
        expected = {3: "1. 운동시설", 5: "2. 휴양시설", 7: "3. 교양시설", 9: "4. 기타"}
        for chunk in doc["chunks"]:
            contexts = required_contexts(doc, chunk, "preceding_table_heading")
            self.assertEqual([c["text"] for c in contexts], [expected[chunk["source_refs"][0]["element_id"]]])
        merged = [f for c in doc["chunks"] for f in c["fields"] if len(f["column_paths"]) == 2]
        self.assertTrue(merged)
        self.assertTrue(any("체육경기" in str(f["column_paths"]) and "기타행사" in str(f["column_paths"]) for f in merged))
        parking = self.docs["2171833:22740723"]["chunks"][0]
        self.assertIn(["노 외 주 차 장", "월 정 기 권", "야 간"], parking["fields"][-1]["column_paths"])

    def test_ditto_marker_is_retained_with_explicit_reference(self):
        doc = self.docs["2147709:22416021"]
        ditto = [f for c in doc["chunks"] for f in c["fields"] if "ditto_reference" in f]
        self.assertTrue(ditto)
        for field in ditto:
            self.assertEqual(field["value"], "〃")
            self.assertEqual(field["ditto_reference"], {"value": "연액", "row": 1, "column": 2})
            self.assertEqual(field["cell_origin_basis"], "parse_html")

    def test_rectangular_original_table_does_not_prove_correct_associations(self):
        ident = "2147709:22416027"
        layout = read_json(chunks.parse.FOLDER + "/" + ident.replace(":", "_") + ".layout_evidence.json")
        original = next(e for e in layout["elements"] if e["category"] == "table")
        table = chunks.parse_table(original["html"])
        self.assertEqual(table["rows"] - table["header_rows"], 1)
        self.assertEqual(len(self.docs[ident]["chunks"]), 13)
        self.assertTrue(all(c["correction_id"] == "delegation_table_vertical_alignment_lost" for c in self.docs[ident]["chunks"]))
        self.assertFalse(self.docs[ident]["answer_allowed"])


@unittest.skipUnless((PROJECT / "data/processed/attachments/2026-10-01/chunks/chunks.manifest.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/processed/attachments/2026-10-01/chunks/chunks.manifest.json")
class CandidateStorageTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=TEST_ROOT)).resolve()
        self.addCleanup(self.cleanup_project)
        for target, name in ((requests.sessions.Session, "request"), (chunks.parse, "call_api"),
                             (chunks.parse, "dotenv_values"), (chunks.parse.os, "getenv")):
            guard = patch.object(target, name, side_effect=AssertionError("Network/credential access forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.manifest = read_json(chunks.FOLDER + "/chunks.manifest.json")
        for relative, expected in self.manifest["input_files"].items():
            rel = Path(relative)
            source = (PROJECT / rel).resolve(strict=True)
            target = (self.project / rel).resolve()
            self.assertEqual(rel.parts[0], "data")
            self.assertTrue(source.is_relative_to(PROJECT))
            self.assertTrue(target.is_relative_to(self.project))
            self.assertNotEqual(source.name, ".env")
            self.assertEqual(chunks.body.digest(source.read_bytes()), expected)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self.baseline = {name: (PROJECT / chunks.FOLDER / name).read_bytes() for name in OUTPUT_NAMES}

    def cleanup_project(self):
        root = self.project.resolve()
        scope = TEST_ROOT.resolve()
        if root.parent != scope or not root.is_relative_to(scope) or not root.name.startswith(TEMP_PREFIX):
            raise AssertionError("Unsafe cleanup target: " + str(root))
        if root.exists():
            shutil.rmtree(root)

    def output(self, name):
        return self.project / chunks.FOLDER / name

    def assert_no_outputs(self, except_names=()):
        for name in OUTPUT_NAMES:
            if name not in except_names:
                self.assertFalse(self.output(name).exists(), name)

    def input_snapshot(self):
        return {relative: ((self.project / relative).read_bytes(), (self.project / relative).stat().st_mtime_ns)
                for relative in self.manifest["input_files"]}

    def test_prepare_reproduces_outputs_without_changing_source_bytes_or_dates(self):
        before = self.input_snapshot()
        outputs, report = chunks.prepare_batch(self.project)
        self.assertEqual(outputs, self.baseline)
        self.assertEqual(report["chunk_count"], 93)
        self.assertEqual(list(outputs)[-1], "chunks.manifest.json")
        self.assertEqual(self.input_snapshot(), before)
        self.assert_no_outputs()

    def test_corrections_pin_rejects_modified_value_before_any_output(self):
        correction = self.project / chunks.CORRECTIONS_FILE
        raw = correction.read_bytes()
        self.assertIn(b"2.6m", raw)
        correction.write_bytes(raw.replace(b"2.6m", b"9.9m", 1))
        with self.assertRaisesRegex(ValueError, "교정 데이터가 변경"):
            chunks.run_batch(self.project)
        self.assert_no_outputs()

    def test_parse_tampering_is_detected_before_output(self):
        relative = next(p for p in self.manifest["input_files"] if p.endswith(".document_parse.json"))
        path = self.project / relative
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            chunks.run_batch(self.project)
        self.assert_no_outputs()

    def test_legacy_candidate_tampering_does_not_get_reused(self):
        relative = "data/processed/attachments/2171833_22740725.search_chunks.json"
        path = self.project / relative
        value = json.loads(path.read_bytes())
        value["chunks"][0]["text"] += " altered"
        path.write_bytes(chunks.body.json_bytes(value))
        with self.assertRaisesRegex(ValueError, "기존 검색 조각과 근거"):
            chunks.run_batch(self.project)
        self.assert_no_outputs()

    def test_late_manifest_conflict_stops_all_preceding_output_writes(self):
        target = self.output("chunks.manifest.json")
        target.write_bytes(b"existing unrelated output")
        with patch.object(chunks.body.source_import, "write_new") as writer:
            with self.assertRaisesRegex(ValueError, "기존 검색 후보 충돌"):
                chunks.run_batch(self.project)
            writer.assert_not_called()
        self.assertEqual(target.read_bytes(), b"existing unrelated output")
        self.assert_no_outputs(except_names={"chunks.manifest.json"})

    def test_input_change_after_prepare_stops_all_output_writes(self):
        original_prepare = chunks.prepare_batch

        def prepare_then_change(project):
            result = original_prepare(project)
            path = Path(project) / chunks.CORRECTIONS_FILE
            path.write_bytes(path.read_bytes() + b" ")
            return result

        with patch.object(chunks, "prepare_batch", side_effect=prepare_then_change), \
                patch.object(chunks.body.source_import, "write_new") as writer:
            with self.assertRaisesRegex(ValueError, "준비 중 입력 변경"):
                chunks.run_batch(self.project)
            writer.assert_not_called()
        self.assert_no_outputs()

    def test_manifest_is_last_and_identical_rerun_preserves_all_bytes_and_mtimes(self):
        before = self.input_snapshot()
        with patch.object(chunks.body.source_import, "write_new", wraps=chunks.body.source_import.write_new) as writer:
            result = chunks.run_batch(self.project)
            self.assertEqual(result["chunk_count"], 93)
            self.assertEqual([call.args[0].name for call in writer.call_args_list], list(OUTPUT_NAMES))
            writer.reset_mock()
            saved = {n: (self.output(n).read_bytes(), self.output(n).stat().st_mtime_ns) for n in OUTPUT_NAMES}
            self.assertEqual(chunks.run_batch(self.project), result)
            self.assertEqual(chunks.run_batch(self.project, check_only=True), result)
            writer.assert_not_called()
        self.assertEqual({n: (self.output(n).read_bytes(), self.output(n).stat().st_mtime_ns) for n in OUTPUT_NAMES}, saved)
        self.assertEqual(self.input_snapshot(), before)

    def test_check_only_detects_saved_candidate_tampering_without_rewriting(self):
        for name, raw in self.baseline.items():
            self.output(name).write_bytes(raw)
        target = self.output("candidate_chunks.json")
        changed = json.loads(target.read_bytes())
        changed["documents"][0]["chunks"][0]["required_context_ids"].pop()
        target.write_bytes(chunks.body.json_bytes(changed))
        before = {n: (self.output(n).read_bytes(), self.output(n).stat().st_mtime_ns) for n in OUTPUT_NAMES}
        with patch.object(chunks.body.source_import, "write_new") as writer:
            with self.assertRaisesRegex(ValueError, "검색 후보 산출물 불일치"):
                chunks.run_batch(self.project, check_only=True)
            writer.assert_not_called()
        self.assertEqual({n: (self.output(n).read_bytes(), self.output(n).stat().st_mtime_ns) for n in OUTPUT_NAMES}, before)


if __name__ == "__main__":
    unittest.main()
