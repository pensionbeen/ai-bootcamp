"""Offline Parse acquisition checks; no credentials, network, or live project writes.

The fixture copies only explicitly listed source records and the three legacy
Parse/support files. Newly acquired batch outputs are deliberately excluded.
"""

import copy
import hashlib
import html
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import build_attachment_evidence as evidence_batch
import parse_attachment as parser


PROJECT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
TEMP_PREFIX = "parse-test-"
DUMMY_KEY = "offline-test-only-placeholder"
EVIDENCE = "data/processed/attachments/2171833_22740725.evidence.json"
MANUAL_RAW = "data/raw/attachments/2171833_22740725_Parse.json"


@unittest.skipUnless((PROJECT / "data/raw/attachments/2171833_22740725_Parse.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/raw/attachments/2171833_22740725_Parse.json")
class ParseAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=TEST_ROOT)).resolve()
        self.addCleanup(self.cleanup_project)
        network = patch.object(requests.sessions.Session, "request",
                               side_effect=AssertionError("Live HTTP is forbidden in Parse tests"))
        network.start()
        self.addCleanup(network.stop)
        key = patch.object(parser.os, "getenv", return_value=DUMMY_KEY)
        self.getenv = key.start()
        self.addCleanup(key.stop)
        dotenv = patch.object(parser, "dotenv_values",
                              side_effect=AssertionError("Tests must not read an environment file"))
        self.dotenv = dotenv.start()
        self.addCleanup(dotenv.stop)

        collection_file = Path(parser.originals.MANIFEST)
        collection = json.loads((PROJECT / collection_file).read_bytes())
        relatives = {collection_file, *map(Path, collection["input_files"])}
        for item in collection["attachments"]:
            relatives.update((Path(item["file_path"]), Path(item["metadata_path"])))
        relatives.update(map(Path, (*parser.LEGACY.values(), EVIDENCE, MANUAL_RAW)))
        for relative in relatives:
            source = (PROJECT / relative).resolve(strict=True)
            target = (self.project / relative).resolve()
            self.assertTrue(source.is_relative_to(PROJECT))
            self.assertTrue(target.is_relative_to(self.project))
            self.assertFalse(relative.as_posix().startswith(parser.FOLDER + "/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        self.collection_hash, self.items, self.parsed = parser.inventory(self.project)
        self.missing = [item for item, result in zip(self.items, self.parsed) if result is None]
        self.legacy_before = self.legacy_snapshot()

    def cleanup_project(self):
        root = self.project.resolve()
        if (root == TEST_ROOT.resolve() or root.parent != TEST_ROOT.resolve()
                or not root.is_relative_to(TEST_ROOT.resolve())
                or not root.name.startswith(TEMP_PREFIX)):
            raise AssertionError(f"Unsafe cleanup target: {root}")
        if root.exists():
            shutil.rmtree(root)

    def legacy_snapshot(self):
        return {
            relative: ((self.project / relative).read_bytes(), (self.project / relative).stat().st_mtime_ns)
            for relative in (*parser.LEGACY.values(), EVIDENCE, MANUAL_RAW)
        }

    def output_snapshot(self):
        folder = self.project / parser.FOLDER
        return {
            path.relative_to(self.project).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in folder.rglob("*.json") if path.is_file()
        } if folder.exists() else {}

    @staticmethod
    def response():
        coordinates = [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1},
                       {"x": 0.9, "y": 0.4}, {"x": 0.1, "y": 0.4}]
        table = '<table><tr><th rowspan="2">대상</th><td>100㎡당 1대</td></tr><tr><td>200㎡당 1대</td></tr></table>'
        return {
            "api": "2.0", "model": "document-parse-test", "usage": {"pages": 2},
            "content": {"html": table + "<p>비고: 예외를 확인한다.</p><figure>도면</figure>"},
            "elements": [
                {"page": 1, "id": 0, "category": "table", "coordinates": copy.deepcopy(coordinates),
                 "content": {"html": table, "text": "대상 100㎡당 1대 200㎡당 1대"}},
                {"page": 1, "id": 1, "category": "paragraph", "coordinates": copy.deepcopy(coordinates),
                 "content": {"html": "<p>비고: 예외를 확인한다.</p>", "text": "비고: 예외를 확인한다."}},
                {"page": 2, "id": 2, "category": "figure", "coordinates": copy.deepcopy(coordinates),
                 "content": {"html": "<figure>도면</figure>", "text": "도면"}},
            ],
        }

    def fake_api(self, raw, filename, key):
        self.assertIsInstance(raw, bytes)
        self.assertEqual(key, DUMMY_KEY)
        item = next(item for item in self.items if Path(item["file_path"]).name == filename)
        self.assertEqual(raw, (self.project / item["file_path"]).read_bytes())
        return self.response()

    def parse_missing(self, **kwargs):
        with patch.object(parser, "call_api", side_effect=self.fake_api) as api:
            summary = parser.parse_all(self.project, **kwargs)
        return summary, api

    def output(self, item):
        return self.project / parser.result_path(item)

    def attempt(self, item):
        return self.project / (parser.result_path(item) + ".attempt.json")

    def attempt_record(self, item):
        hwp = (self.project / item["file_path"]).read_bytes()
        meta = (self.project / item["metadata_path"]).read_bytes()
        return {"attachment_id": item["attachment"]["attachment_id"],
                "input_content_hash": hashlib.sha256(hwp).hexdigest(),
                "metadata_content_hash": hashlib.sha256(meta).hexdigest(),
                "collection_content_hash": self.collection_hash,
                "input_binding_status": "api_uploaded_bytes", "endpoint": parser.ENDPOINT,
                "request_options": copy.deepcopy(parser.OPTIONS), "requested_at": "2026-10-30T00:00:00+00:00"}

    def write_json(self, path, value):
        self.assertTrue(path.resolve().is_relative_to(self.project))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))

    def test_inventory_reuses_two_legacy_results_and_keeps_manual_binding_unverified(self):
        before = self.output_snapshot()
        with patch.object(parser, "call_api") as api:
            summary = parser.check(self.project)
        self.assertEqual(summary, {"total": 14, "parsed": 2, "missing": 12,
                                   "manual_input_binding_pending": 1})
        api.assert_not_called()
        self.getenv.assert_not_called()
        self.dotenv.assert_not_called()
        self.assertEqual(self.legacy_before, self.legacy_snapshot())
        self.assertEqual(before, self.output_snapshot())
        bound = {item["attachment"]["attachment_id"]: result for item, result in zip(self.items, self.parsed) if result}
        self.assertEqual(bound["2171833:22740723"]["input_binding_status"], "legacy_api_input_hash")
        self.assertEqual(bound["2171833:22740725"]["input_binding_status"], "manual_export_unverified")

    def test_twelve_new_results_bind_exact_uploaded_bytes_and_preserve_all_legacy_bytes(self):
        summary, api = self.parse_missing()
        self.assertEqual(summary, {"total": 14, "parsed": 14, "missing": 0,
                                   "manual_input_binding_pending": 1, "called": 12})
        self.assertEqual(api.call_count, 12)
        self.assertEqual(self.legacy_before, self.legacy_snapshot())
        self.dotenv.assert_not_called()
        for item in self.missing:
            raw = self.output(item).read_bytes()
            record = json.loads(raw)
            expected = self.attempt_record(item)
            for key in expected:
                if key != "requested_at":
                    self.assertEqual(record[key], expected[key])
            self.assertEqual(record["response"], self.response())
            parsed = parser.read_parse(self.project, item, self.collection_hash)
            self.assertEqual(parsed["content_hash"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(parsed["input_binding_status"], "api_uploaded_bytes")

    def test_completed_rerun_reads_no_key_calls_no_api_and_rewrites_no_results(self):
        self.parse_missing()
        before = self.output_snapshot()
        self.getenv.reset_mock()
        with patch.object(parser, "call_api") as api:
            summary = parser.parse_all(self.project)
        self.assertEqual(summary["called"], 0)
        api.assert_not_called()
        self.getenv.assert_not_called()
        self.dotenv.assert_not_called()
        self.assertEqual(before, self.output_snapshot())
        self.assertEqual(self.legacy_before, self.legacy_snapshot())

    def test_limit_counts_only_new_api_requests(self):
        summary, api = self.parse_missing(limit=2)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(summary["called"], 2)
        self.assertEqual(summary["parsed"], 4)
        self.assertEqual(summary["missing"], 10)
        self.assertEqual(self.legacy_before, self.legacy_snapshot())

    def test_wrong_legacy_api_input_hash_stops_before_any_new_request(self):
        path = self.project / parser.LEGACY["2171833:22740723"]
        record = json.loads(path.read_bytes())
        record["input_content_hash"] = "0" * 64
        self.write_json(path, record)
        with patch.object(parser, "call_api") as api:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project)
        api.assert_not_called()
        self.assertEqual(self.output_snapshot(), {})

    def test_tampered_manual_parse_fails_its_recorded_hash_before_api(self):
        path = self.project / parser.LEGACY["2171833:22740725"]
        path.write_bytes(path.read_bytes() + b"\n")
        with patch.object(parser, "call_api") as api:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project)
        api.assert_not_called()

    def test_manual_binding_cannot_be_silently_promoted_to_api_verified(self):
        path = self.project / EVIDENCE
        evidence = json.loads(path.read_bytes())
        evidence["input_binding_status"] = "api_uploaded_bytes"
        self.write_json(path, evidence)
        with self.assertRaises(ValueError):
            parser.inventory(self.project)

    def test_each_new_result_binding_field_is_checked_before_reuse(self):
        self.parse_missing(limit=1)
        item = self.missing[0]
        path = self.output(item)
        original = path.read_bytes()
        record = json.loads(original)
        for field in ("attachment_id", "input_content_hash", "metadata_content_hash",
                      "collection_content_hash", "endpoint", "request_options", "input_binding_status"):
            with self.subTest(field=field):
                changed = copy.deepcopy(record)
                changed[field] = {"model": "wrong-model"} if field == "request_options" else "wrong-value"
                self.write_json(path, changed)
                with patch.object(parser, "call_api") as api:
                    with self.assertRaises(ValueError):
                        parser.parse_all(self.project)
                api.assert_not_called()
        path.write_bytes(original)
        self.assertIsNotNone(parser.read_parse(self.project, item, self.collection_hash))

    def test_duplicate_page_element_id_is_preserved_as_failed_response_without_recall(self):
        response = self.response()
        response["elements"][1]["id"] = 0
        with patch.object(parser, "call_api", return_value=response) as api:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project)
        self.assertEqual(api.call_count, 1)
        item = self.missing[0]
        self.assertEqual(json.loads(self.output(item).read_bytes())["response"], response)
        self.assertTrue(self.attempt(item).is_file())
        before = self.output_snapshot()
        with patch.object(parser, "call_api") as retry:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project, retry_unresolved=True)
        retry.assert_not_called()
        self.assertEqual(before, self.output_snapshot())

    def test_same_element_id_on_different_pages_is_allowed_and_empty_page_is_flagged(self):
        response = self.response()
        response["elements"][2]["id"] = 0
        response["usage"]["pages"] = 3
        stats = parser.validate_response(response)
        self.assertEqual(stats["pages_without_elements"], [3])
        self.assertEqual(stats["pages"], 3)
        self.assertEqual(stats["tables"], 1)
        self.assertEqual(stats["figures"], 1)

    def test_invalid_coordinates_are_rejected_instead_of_coerced(self):
        for value in (-0.01, 1.01, float("nan"), float("inf"), True, "0.5", None):
            with self.subTest(x=value):
                response = self.response()
                response["elements"][0]["coordinates"][0]["x"] = value
                with self.assertRaises(ValueError):
                    parser.validate_response(response)
        response = self.response()
        response["elements"][0]["coordinates"].pop()
        with self.assertRaises(ValueError):
            parser.validate_response(response)

    def test_empty_or_non_table_html_is_rejected(self):
        for html in ("", "<p>표가 아니다</p>", "<table></table>", "<table><tr></tr></table>",
                     "<table><tr><td> </td></tr></table>"):
            with self.subTest(html=html):
                response = self.response()
                response["elements"][0]["content"]["html"] = html
                with self.assertRaises(ValueError):
                    parser.validate_response(response)

    def test_unresolved_last_attempt_blocks_earlier_requests_before_reading_a_key(self):
        last = self.missing[-1]
        self.write_json(self.attempt(last), self.attempt_record(last))
        with patch.object(parser, "call_api") as api:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project, limit=1)
        api.assert_not_called()
        self.getenv.assert_not_called()

    def test_failed_call_leaves_attempt_and_requires_explicit_retry(self):
        item = self.missing[0]
        with patch.object(parser, "call_api", side_effect=ValueError("simulated uncertain API outcome")) as api:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project, limit=1)
        self.assertEqual(api.call_count, 1)
        self.assertTrue(self.attempt(item).exists())
        self.assertFalse(self.output(item).exists())
        attempt_before = (self.attempt(item).read_bytes(), self.attempt(item).stat().st_mtime_ns)
        with patch.object(parser, "call_api") as retry:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project, limit=1)
        retry.assert_not_called()
        summary, retry = self.parse_missing(limit=1, retry_unresolved=True)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(summary["parsed"], 3)
        self.assertEqual(attempt_before, (self.attempt(item).read_bytes(), self.attempt(item).stat().st_mtime_ns))

    def test_conflicting_attempt_cannot_be_bypassed_with_retry_flag(self):
        item = self.missing[0]
        for field in ("input_content_hash", "request_options", "collection_content_hash"):
            with self.subTest(field=field):
                record = self.attempt_record(item)
                record[field] = {} if field == "request_options" else "bad-hash"
                self.write_json(self.attempt(item), record)
                with patch.object(parser, "call_api") as api:
                    with self.assertRaises(ValueError):
                        parser.parse_all(self.project, limit=1, retry_unresolved=True)
                api.assert_not_called()

    def test_partial_failure_reuses_success_and_only_retries_remaining_eleven(self):
        calls = 0

        def fail_second(raw, filename, key):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("simulated API failure")
            return self.fake_api(raw, filename, key)

        with patch.object(parser, "call_api", side_effect=fail_second):
            with self.assertRaises(ValueError):
                parser.parse_all(self.project)
        first = self.output(self.missing[0])
        before = (first.read_bytes(), first.stat().st_mtime_ns)
        self.assertEqual(parser.check(self.project)["parsed"], 3)
        summary, retry = self.parse_missing(retry_unresolved=True)
        self.assertEqual(summary["called"], 11)
        self.assertEqual(retry.call_count, 11)
        self.assertEqual(summary["missing"], 0)
        self.assertNotIn(Path(self.missing[0]["file_path"]).name, [call.args[1] for call in retry.call_args_list])
        self.assertEqual(before, (first.read_bytes(), first.stat().st_mtime_ns))

    def test_input_mutation_during_api_preserves_response_but_stops_further_calls(self):
        item = self.missing[0]
        path = self.project / item["file_path"]
        original = path.read_bytes()

        def mutate(raw, filename, key):
            self.assertEqual(raw, original)
            changed = bytearray(original)
            changed[-1] ^= 1
            path.write_bytes(changed)
            return self.response()

        with patch.object(parser, "call_api", side_effect=mutate) as api:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project)
        self.assertEqual(api.call_count, 1)
        record = json.loads(self.output(item).read_bytes())
        self.assertEqual(record["input_content_hash"], hashlib.sha256(original).hexdigest())
        self.assertEqual(record["response"], self.response())
        with patch.object(parser, "call_api") as retry:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project)
        retry.assert_not_called()

    def test_collection_change_during_api_prevents_second_request(self):
        path = self.project / parser.originals.MANIFEST

        def mutate(raw, filename, key):
            path.write_bytes(path.read_bytes() + b"\n")
            return self.response()

        with patch.object(parser, "call_api", side_effect=mutate) as api:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project)
        self.assertEqual(api.call_count, 1)
        self.assertTrue(self.output(self.missing[0]).exists())

    def test_duplicate_legacy_and_new_result_is_not_arbitrarily_selected(self):
        item = next(item for item in self.items if item["attachment"]["attachment_id"] in parser.LEGACY)
        self.write_json(self.output(item), {})
        with patch.object(parser, "call_api") as api:
            with self.assertRaises(ValueError):
                parser.parse_all(self.project)
        api.assert_not_called()

    def test_call_api_uploads_the_supplied_bytes_without_reopening_a_path(self):
        raw = b"opaque test bytes; parser upload binding is independent of filesystem"
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.content = b"{}"
        response.json.return_value = self.response()
        with patch.object(parser.requests, "post", return_value=response) as post:
            result = parser.call_api(raw, "fixture.hwp", DUMMY_KEY)
        self.assertEqual(result, self.response())
        self.assertEqual(post.call_args.args[0], parser.ENDPOINT)
        args = post.call_args.kwargs
        document = args["files"]["document"]
        self.assertEqual(document[0], "fixture.hwp")
        self.assertEqual(document[1].getvalue(), raw)
        self.assertEqual(args["data"], parser.OPTIONS)
        self.assertFalse(args["allow_redirects"])


class BatchLayoutTests(unittest.TestCase):
    @staticmethod
    def document_fixture(kind="별표", binding="legacy_api_input_hash"):
        response = ParseAttachmentTests.response()
        response["elements"] = response["elements"][:2]
        for element in response["elements"]:
            element["content"]["text"] = ""
        response["usage"]["pages"] = 1
        stats = parser.validate_response(response)
        parsed = {"file": "data/processed/fixture.json", "content_hash": "a" * 64,
                  "input_binding_status": binding, "response": response, **stats}
        item = {"ordin_name": "시험 조례", "ordin_seq": "1", "source": {"ordin_seq": "1"},
                "source_location": {"raw_file": "data/raw/1.xml", "xml_path": "./별표단위[1]"},
                "attachment": {"attachment_id": "1:2", "attachment_type": kind,
                               "attachment_no": "0001", "label": kind + " 1", "title": "시험 자료",
                               "raw_text": "대상 100㎡당 1대 200㎡당 1대 비고: 예외를 확인한다."}}
        links = {"body_references": [], "title_reference_status": "no_title_reference",
                 "review_notes": [], "answer_allowed": False}
        return item, parsed, links

    def test_safe_html_removes_active_content_external_images_and_keeps_merged_cells(self):
        markup = ('<script>alert("private")</script><style>.secret{display:block}</style>'
                  '<table onclick="bad()"><tr><th rowspan="2" style="color:red">요금 &amp; 기준</th>'
                  '<td colspan="3" onmouseover="bad()">100㎡당 1대<br/>예외</td></tr></table>'
                  '<img src="https://example.invalid/tracker" onerror="bad()">'
                  '<a href="javascript:bad()">원문 안내</a>')
        layout = evidence_batch.LayoutHTML(markup)
        safe = "".join(layout.parts)
        for unwanted in ("<script", "<style", "<img", "https://", "javascript:", "onclick",
                         "onmouseover", "onerror", "alert", "private", "secret"):
            self.assertNotIn(unwanted, safe)
        self.assertIn('<th rowspan="2">', safe)
        self.assertIn('<td colspan="3">', safe)
        self.assertIn("요금 &amp; 기준", safe)
        self.assertEqual(layout.rows, 1)
        self.assertEqual(layout.cells, 2)
        self.assertEqual(layout.spans, [{"attribute": "rowspan", "value": 2},
                                         {"attribute": "colspan", "value": 3}])
        self.assertIn("100㎡당 1대 예외", layout.text)

    def test_legacy_empty_text_uses_html_projection_and_preserves_original_structure(self):
        item, parsed, links = self.document_fixture()
        original = copy.deepcopy(parsed)
        doc = evidence_batch.batch_document(item, parsed, links)
        self.assertEqual(parsed, original)
        table = doc["elements"][0]
        self.assertEqual(table["text_method"], "html_text_projection")
        self.assertIn("100㎡당 1대", table["text"])
        self.assertEqual(table["html"], original["response"]["elements"][0]["content"]["html"])
        self.assertEqual(table["coordinates"], original["response"]["elements"][0]["coordinates"])
        self.assertEqual(table["source_element_index"], 0)
        self.assertEqual(table["table_structure"]["html_rows"], 2)
        self.assertEqual(table["table_structure"]["html_cells"], 3)
        self.assertEqual(table["table_structure"]["merged_cell_attributes"], [{"attribute": "rowspan", "value": 2}])
        self.assertEqual(table["table_structure"]["multipage_merge"], "not_performed")
        self.assertIn("table_crop_unavailable", doc["review_flags"])

    def test_manual_binding_and_drawing_remain_pending_even_when_no_figure_was_detected(self):
        item, parsed, links = self.document_fixture(kind="별도", binding="manual_export_unverified")
        self.assertEqual(parsed["figures"], 0)
        doc = evidence_batch.batch_document(item, parsed, links)
        self.assertEqual(doc["input_binding_status"], "manual_export_unverified")
        self.assertIn("manual_input_binding_unverified", doc["review_flags"])
        self.assertIn("drawing_visual_review_required", doc["review_flags"])
        self.assertIn("content_review_pending", doc["review_flags"])
        self.assertEqual(doc["source_review"], "pending")
        self.assertFalse(doc["answer_allowed"])
        self.assertFalse(doc["is_embedding_input"])
        self.assertTrue(all(not element["is_search_chunk"] for element in doc["elements"]))

    def test_numeric_multiset_comparison_is_never_an_accuracy_or_semantic_verdict(self):
        difference = evidence_batch.compare_xml_text("100 100 200", "100 200")
        self.assertEqual(difference["numbers_only_or_more_in_xml"], {"100": 1})
        self.assertEqual(difference["numbers_only_or_more_in_parse"], {})
        swapped = evidence_batch.compare_xml_text("가 100원 나 200원", "가 200원 나 100원")
        self.assertEqual(swapped["numbers_only_or_more_in_xml"], {})
        self.assertEqual(swapped["numbers_only_or_more_in_parse"], {})
        self.assertFalse(swapped["normalized_text_equal"])
        exact = evidence_batch.compare_xml_text("100 원", "100원")
        self.assertTrue(exact["normalized_text_equal"])
        for result in (difference, swapped, exact):
            self.assertFalse(result["is_accuracy_score"])
            self.assertEqual(result["semantic_correctness"], "not_determined")


@unittest.skipUnless((PROJECT / "data/processed/attachments/2026-10-01/evidence.manifest.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/processed/attachments/2026-10-01/evidence.manifest.json")
class BatchEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=TEST_ROOT)).resolve()
        self.addCleanup(self.cleanup_project)
        for target, name in ((requests.sessions.Session, "request"), (parser, "call_api"),
                             (parser, "dotenv_values"), (parser.os, "getenv")):
            blocker = patch.object(target, name, side_effect=AssertionError("Batch evidence tests are fully offline"))
            blocker.start()
            self.addCleanup(blocker.stop)
        manifest_source = PROJECT / evidence_batch.BATCH_MANIFEST
        self.expected_manifest_raw = manifest_source.read_bytes()
        self.expected_manifest = json.loads(self.expected_manifest_raw)
        for relative in self.expected_manifest["input_files"]:
            self.copy_fixture(relative)
        self.output_folder = self.project / parser.FOLDER

    def cleanup_project(self):
        root = self.project.resolve()
        if (root == TEST_ROOT.resolve() or root.parent != TEST_ROOT.resolve()
                or not root.is_relative_to(TEST_ROOT.resolve())
                or not root.name.startswith(TEMP_PREFIX)):
            raise AssertionError(f"Unsafe cleanup target: {root}")
        if root.exists():
            shutil.rmtree(root)

    def copy_fixture(self, relative):
        relative = Path(relative)
        self.assertEqual(relative.parts[0], "data")
        self.assertIn(relative.suffix, (".json", ".xml", ".hwp", ".html"))
        source = (PROJECT / relative).resolve(strict=True)
        target = (self.project / relative).resolve()
        self.assertTrue(source.is_relative_to(PROJECT))
        self.assertTrue(target.is_relative_to(self.project))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def copy_derived(self):
        for name in self.expected_manifest["output_files"]:
            self.copy_fixture(Path(parser.FOLDER) / name)
        self.copy_fixture(evidence_batch.BATCH_MANIFEST)

    def derived_paths(self):
        return [self.output_folder / name for name in (*self.expected_manifest["output_files"], "evidence.manifest.json")]

    def derived_snapshot(self):
        return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.derived_paths() if path.exists()}

    def test_prepare_binds_fourteen_layouts_and_seventeen_outputs_to_exact_input_files(self):
        outputs, report = evidence_batch.prepare_batch(self.project)
        self.assertEqual(len(outputs), 17)
        self.assertEqual(sum(name.endswith(".layout_evidence.json") for name in outputs), 14)
        self.assertEqual(report["attachment_count"], 14)
        self.assertEqual(report["content_review_pending"], 14)
        self.assertEqual(report["manual_input_binding_pending"], 1)
        self.assertFalse(report["answer_allowed"])
        manifest = json.loads(outputs["evidence.manifest.json"])
        for relative, expected_hash in manifest["input_files"].items():
            self.assertEqual(hashlib.sha256((self.project / relative).read_bytes()).hexdigest(), expected_hash)
        for name, expected_hash in manifest["output_files"].items():
            self.assertEqual(hashlib.sha256(outputs[name]).hexdigest(), expected_hash)
        self.assertEqual(outputs["evidence.manifest.json"], self.expected_manifest_raw)
        self.assertFalse(self.derived_snapshot())

    def test_body_reference_spans_match_exact_saved_chunks_and_never_claim_applicability(self):
        outputs, _ = evidence_batch.prepare_batch(self.project)
        chunks = {}
        body_folder = self.project / "data/processed/ordinances/2026-10-01"
        for path in body_folder.glob("*.chunks.json"):
            value = json.loads(path.read_bytes())
            chunks.update({chunk["chunk_id"]: chunk for chunk in value})
        count = 0
        for name, raw in outputs.items():
            if not name.endswith(".layout_evidence.json"):
                continue
            document = json.loads(raw)
            links = document["related_body"]
            self.assertFalse(links["answer_allowed"])
            self.assertFalse(document["answer_allowed"])
            for ref in links["body_references"]:
                chunk = chunks[ref["chunk_id"]]
                self.assertEqual(ref["quote_text"], chunk["quote_text"])
                self.assertEqual(ref["article"], chunk["article"])
                self.assertEqual(ref["paragraph"], chunk["paragraph"])
                span = ref["reference_span"]
                self.assertEqual(ref["quote_text"][span["start"]:span["end"]], ref["reference_text"])
                self.assertEqual(ref["method"], "explicit_body_reference")
                count += 1
            for ref in links["title_references"]:
                span = ref["reference_span"]
                self.assertEqual(document["attachment"]["title"][span["start"]:span["end"]], ref["reference_text"])
        self.assertGreater(count, 0)

    def test_saved_body_tamper_is_rejected_before_any_evidence_write(self):
        path = self.project / "data/processed/ordinances/2026-10-01/2171833.chunks.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            evidence_batch.run_batch(self.project)
        self.assertEqual(self.derived_snapshot(), {})

    def test_first_run_writes_manifest_last_and_rerun_or_check_rewrites_nothing(self):
        original = evidence_batch.body_corpus.source_import.write_new
        written = []

        def record_write(path, raw):
            written.append(Path(path).name)
            return original(path, raw)

        with patch.object(evidence_batch.body_corpus.source_import, "write_new", side_effect=record_write):
            evidence_batch.run_batch(self.project)
        self.assertEqual(len(written), 17)
        self.assertEqual(written[-1], "evidence.manifest.json")
        before = self.derived_snapshot()
        with patch.object(evidence_batch.body_corpus.source_import, "write_new") as write:
            evidence_batch.run_batch(self.project)
            evidence_batch.run_batch(self.project, check_only=True)
        write.assert_not_called()
        self.assertEqual(before, self.derived_snapshot())

    def test_late_output_conflict_blocks_all_earlier_new_writes_and_check_rejects_tamper(self):
        self.output_folder.mkdir(parents=True, exist_ok=True)
        conflict = self.output_folder / "review.html"
        conflict.write_bytes(b"<html>unrelated output</html>")
        before = self.derived_snapshot()
        with patch.object(evidence_batch.body_corpus.source_import, "write_new") as write:
            with self.assertRaises(ValueError):
                evidence_batch.run_batch(self.project)
        write.assert_not_called()
        self.assertEqual(before, self.derived_snapshot())
        self.copy_derived()
        evidence_batch.run_batch(self.project, check_only=True)
        conflict.write_bytes(conflict.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            evidence_batch.run_batch(self.project, check_only=True)

    def test_input_changed_after_prepare_is_detected_before_any_output_write(self):
        original_prepare = evidence_batch.prepare_batch

        def change_after_prepare(project):
            outputs, report = original_prepare(project)
            changed = self.project / parser.LEGACY["2171833:22740723"]
            changed.write_bytes(changed.read_bytes() + b"\n")
            return outputs, report

        with patch.object(evidence_batch, "prepare_batch", side_effect=change_after_prepare):
            with patch.object(evidence_batch.body_corpus.source_import, "write_new") as write:
                with self.assertRaises(ValueError):
                    evidence_batch.run_batch(self.project)
        write.assert_not_called()
        self.assertEqual(self.derived_snapshot(), {})

    def test_review_quote_parse_hash_and_image_hash_tampering_are_rejected(self):
        path = self.output_folder / "review_findings.json"
        original = path.read_bytes()
        review = json.loads(original)
        _, items, parsed = parser.inventory(self.project)
        changes = {
            "extracted_quote": "이 문장은 실제 Parse 요소에 존재하지 않습니다.",
            "parse_content_hash": "0" * 64,
            "crop_content_hash": "0" * 64,
        }
        for index in range(len(review["findings"])):
            for field, value in changes.items():
                with self.subTest(finding=index, field=field):
                    changed = copy.deepcopy(review)
                    changed["findings"][index][field] = value
                    path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        evidence_batch.load_review_findings(self.project, items, parsed, {})
        path.write_bytes(original)
        inputs = {}
        found = evidence_batch.load_review_findings(self.project, items, parsed, inputs)
        self.assertEqual(sum(map(len, found.values())), len(review["findings"]))
        self.assertEqual(inputs[(Path(parser.FOLDER) / "review_findings.json").as_posix()],
                         hashlib.sha256(original).hexdigest())
        self.assertEqual(self.derived_snapshot(), {})

    def test_review_answer_permission_or_resolved_status_cannot_be_silently_enabled(self):
        path = self.output_folder / "review_findings.json"
        original = path.read_bytes()
        review = json.loads(original)
        _, items, parsed = parser.inventory(self.project)
        variants = []
        changed = copy.deepcopy(review)
        changed["answer_allowed"] = True
        variants.append(("review_answer_allowed", changed))
        for index in range(len(review["findings"])):
            changed = copy.deepcopy(review)
            changed["findings"][index]["answer_allowed"] = True
            variants.append((f"finding_{index}_answer_allowed", changed))
            changed = copy.deepcopy(review)
            changed["findings"][index]["status"] = "resolved"
            variants.append((f"finding_{index}_resolved", changed))
        for name, changed in variants:
            with self.subTest(change=name):
                path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(ValueError):
                    evidence_batch.load_review_findings(self.project, items, parsed, {})
        path.write_bytes(original)
        self.assertEqual(self.derived_snapshot(), {})

    def test_known_visual_findings_are_shown_without_correcting_or_rewriting_source(self):
        before = {
            relative: (hashlib.sha256((self.project / relative).read_bytes()).hexdigest(),
                       (self.project / relative).stat().st_mtime_ns)
            for relative in self.expected_manifest["input_files"]
        }
        review = json.loads((self.output_folder / "review_findings.json").read_bytes())
        self.assertTrue({"family_parking_dimension_misattribution", "camping_fee_cell_line_boundaries_lost"}
                        .issubset({finding["finding_id"] for finding in review["findings"]}))
        outputs, report = evidence_batch.prepare_batch(self.project)
        self.assertEqual(report["known_blocking_findings"], len(review["findings"]))
        self.assertFalse(report["answer_allowed"])
        preview = outputs["review.html"].decode("utf-8")
        for finding in review["findings"]:
            filename = finding["attachment_id"].replace(":", "_") + ".layout_evidence.json"
            doc = json.loads(outputs[filename])
            self.assertIn(finding, doc["review_findings"])
            self.assertIn("known_extraction_issue_requires_resolution", doc["review_flags"])
            self.assertEqual(doc["source_review"], "pending")
            self.assertFalse(doc["answer_allowed"])
            self.assertFalse(doc["is_embedding_input"])
            element = next(element for element in doc["elements"]
                           if element["page"] == finding["page"] and element["element_id"] == finding["element_id"])
            self.assertIn(finding["extracted_quote"], element[finding["content_field"]])
            self.assertIn(html.escape(finding["title"]), preview)
            self.assertIn(html.escape(finding["extracted_quote"]), preview)
            self.assertIn(html.escape(finding["required_action"]), preview)
        after = {
            relative: (hashlib.sha256((self.project / relative).read_bytes()).hexdigest(),
                       (self.project / relative).stat().st_mtime_ns)
            for relative in self.expected_manifest["input_files"]
        }
        self.assertEqual(before, after)
        self.assertEqual(self.derived_snapshot(), {})


if __name__ == "__main__":
    unittest.main()
