"""Offline readiness checks: reviewed expressions are not generated embeddings.

Fixtures contain only manifest-listed staging data. Network and credential
access are forbidden, and every write is confined to a test-owned directory.
"""

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import requests

import build_attachment_search_chunks as candidates
import embed_attachment_chunks as readiness


PROJECT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
TEMP_PREFIX = "readiness-test-"
OUTPUT_NAMES = ("embedding_inputs.json", "readiness_report.json", "preview.html", "readiness.manifest.json")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def no_external_access():
    guards = ExitStack()
    for target, name in ((requests.sessions.Session, "request"), (readiness, "load_dotenv"),
                         (readiness.os, "getenv"), (candidates.parse, "call_api"),
                         (candidates.parse, "dotenv_values")):
        guards.enter_context(patch.object(target, name, side_effect=AssertionError("Network/credential access forbidden")))
    return guards


@unittest.skipUnless((PROJECT / "data/processed/attachments/2026-10-01/chunks/candidate_chunks.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/processed/attachments/2026-10-01/chunks/candidate_chunks.json")
class ReadinessConstructionTests(unittest.TestCase):
    def setUp(self):
        data = json.loads((PROJECT / candidates.FOLDER / "candidate_chunks.json").read_bytes())
        self.docs = {d["attachment_id"]: d for d in data["documents"]}
        self.decisions = {c["chunk_id"]: {"chunk_id": c["chunk_id"], "status": "ready",
                          "reason": "offline construction test", "review_basis": ["synthetic test decision"],
                          "primary_text_patches": [], "additional_context_chunk_ids": [], "required_body_chunk_ids": []}
                          for d in data["documents"] for c in d["chunks"]}

    def build(self, doc, chunk):
        return readiness.build_embedding_record(doc, chunk, self.decisions[chunk["chunk_id"]], self.decisions)

    def test_primary_patch_requires_one_exact_nonempty_match(self):
        for old, new in (("same", "changed"), ("absent", "changed"), ("", "changed"), ("same same", "")):
            with self.subTest(old=old, new=new), self.assertRaises(ValueError):
                readiness.reviewed_primary({"text": "same same"}, {"primary_text_patches": [{"old": old, "new": new}]})

    def test_manual_or_image_candidate_cannot_be_approved_by_decision_alone(self):
        for ident in ("2171833:22740725", "2171833:22740737"):
            with self.subTest(attachment=ident):
                doc = self.docs[ident]
                with self.assertRaises(ValueError):
                    self.build(doc, doc["chunks"][0])

    def test_missing_required_context_prevents_input_construction(self):
        doc = self.docs["2171833:22740735"]
        chunk = doc["chunks"][0]
        chunk["required_context_ids"].pop()
        with self.assertRaises(ValueError):
            self.build(doc, chunk)

    def test_same_table_prior_record_may_have_a_different_correction_index(self):
        doc = self.docs["2147709:22416027"]
        prior, current = doc["chunks"][0], doc["chunks"][2]
        self.assertNotEqual(prior["source_refs"][0]["correction_record_index"],
                            current["source_refs"][0]["correction_record_index"])
        self.decisions[current["chunk_id"]]["additional_context_chunk_ids"] = [prior["chunk_id"]]
        record = self.build(doc, current)
        extra, = record["additional_contexts"]
        self.assertEqual(extra["source_chunk_id"], prior["chunk_id"])
        self.assertEqual(extra["source_refs"], prior["source_refs"])
        self.assertNotIn("수임기관:", extra["text"])
        self.assertIn("현재 사무의 적용법을 확정하지 않음", extra["text"])

    def test_future_duplicate_held_or_other_table_antecedents_are_rejected(self):
        for mode in ("future", "duplicate", "held", "other_table"):
            with self.subTest(mode=mode):
                doc = copy.deepcopy(self.docs["2147709:22416027"])
                prior, current = doc["chunks"][0], doc["chunks"][2]
                previous = [prior["chunk_id"]]
                self.decisions[prior["chunk_id"]]["status"] = "ready"
                if mode == "future":
                    previous = [doc["chunks"][3]["chunk_id"]]
                elif mode == "duplicate":
                    previous *= 2
                elif mode == "held":
                    self.decisions[prior["chunk_id"]]["status"] = "held"
                else:
                    prior["source_refs"][0]["element_id"] += 100
                self.decisions[current["chunk_id"]]["additional_context_chunk_ids"] = previous
                with self.assertRaises(ValueError):
                    self.build(doc, current)


@unittest.skipUnless((PROJECT / "data/raw/ordinances.manifest.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/raw/ordinances.manifest.json")
class ReadinessMeaningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with no_external_access():
            cls.outputs, cls.report = readiness.prepare_readiness(PROJECT)
        cls.data = json.loads(cls.outputs["embedding_inputs.json"])
        cls.review = json.loads((PROJECT / readiness.REVIEW_FILE).read_bytes())
        source = json.loads((PROJECT / candidates.FOLDER / "candidate_chunks.json").read_bytes())
        cls.documents = {d["attachment_id"]: d for d in source["documents"]}
        cls.chunks = {c["chunk_id"]: c for d in source["documents"] for c in d["chunks"]}
        cls.records = {r["chunk_id"]: r for r in cls.data["records"]}
        cls.decisions = {d["chunk_id"]: d for d in cls.review["decisions"]}

    def test_review_covers_each_of_the_93_candidates_once(self):
        self.assertEqual(len(self.chunks), 93)
        self.assertEqual(len(self.review["decisions"]), 93)
        self.assertEqual(set(self.decisions), set(self.chunks))
        self.assertEqual(set(self.records), {d["chunk_id"] for d in self.review["decisions"] if d["status"] == "ready"})
        self.assertEqual(len(self.records), len(self.data["records"]))
        self.assertEqual(self.report["ready_count"], len(self.records))
        self.assertEqual(self.report["held_count"], len(self.chunks) - len(self.records))
        for decision in self.review["decisions"]:
            self.assertIn(decision["status"], ("ready", "held"))
            self.assertTrue(decision["reason"].strip())

    def test_manual_13_and_visual_metadata_3_are_excluded(self):
        manual = [c for c in self.chunks.values() if c["input_binding_status"] == "manual_export_unverified"]
        visual = [c for c in self.chunks.values() if c["kind"] == "image_metadata_only"]
        self.assertEqual(len(manual), 13)
        self.assertEqual(len(visual), 3)
        for chunk in manual + visual:
            self.assertNotIn(chunk["chunk_id"], self.records)
            self.assertEqual(self.decisions[chunk["chunk_id"]]["status"], "held")

    def test_readiness_has_no_vectors_or_answer_permission(self):
        self.assertEqual(self.data["model"], "solar-embedding-2-passage")
        self.assertEqual(self.data["query_model"], "solar-embedding-2-query")
        self.assertEqual(self.data["dimension"], 1024)
        self.assertIs(self.data["answer_allowed"], False)
        self.assertIs(self.data["embedding_performed"], False)
        self.assertNotIn("vectors", self.data)
        for record in self.records.values():
            self.assertIs(record["embedding_allowed"], True)
            self.assertIs(record["answer_allowed"], False)
            self.assertNotIn("embedding", record)
            self.assertEqual(record["text_sha256"], digest(record["text"].encode("utf-8")))
            self.assertIn(record["primary_text"], record["text"])

    def test_required_contexts_and_original_provenance_are_preserved(self):
        for record in self.records.values():
            chunk = self.chunks[record["chunk_id"]]
            doc = self.documents[chunk["attachment_id"]]
            contexts = {c["context_id"]: c for c in doc["contexts"]}
            expected = [contexts[ident] for ident in chunk["required_context_ids"]]
            self.assertEqual(record["required_contexts"], expected)
            self.assertEqual(record["source_refs"], chunk["source_refs"])
            self.assertEqual(record["input_binding_status"], chunk["input_binding_status"])
            self.assertEqual(record["related_body"], doc["related_body"])
            self.assertIn(doc["ordin_name"], record["text"])
            self.assertIn(doc["title"], record["text"])
            for context in expected:
                self.assertIn(context["text"], record["text"])

    def test_camping_pairs_keep_blank_discount_and_exclude_sports_body_quote(self):
        expected = [(7, "어른", "2,000~4,000"), (8, "청소년", "1,500~3,000"), (9, "어린이", "1,000~2,000")]
        for row, age, fee in expected:
            record = self.records[f"2147709:22415979:candidate:p1-e5-r{row}"]
            self.assertIn(age + " 1회당", record["primary_text"])
            self.assertIn(fee, record["primary_text"])
            self.assertIn("2. 휴양시설", record["text"])
            self.assertNotIn("30%할인", record["text"])
            self.assertNotIn("100분의 50", record["text"])
            refs = record["related_body"]["body_references"]
            sports = next(r for r in refs if r["chunk_id"] == "2147709:002000:3")
            self.assertIn("운동시설 이용료", sports["quote_text"])
            self.assertNotIn(sports["quote_text"], record["text"])

    def test_forest_typo_is_corrected_only_in_reviewed_primary_expression(self):
        ident = "2147709:22415979:candidate:p2-e7-r4"
        record = self.records[ident]
        self.assertIn("어 르", self.chunks[ident]["text"])
        self.assertNotIn("어 르", record["primary_text"])
        self.assertIn("어른1회당", "".join(record["primary_text"].split()))
        for value in ("8,000~12,000", "5,000~8,000", "3,000~5,000", "평일 단체 이용시 30% 할인"):
            self.assertIn(value, record["primary_text"])
        expected = self.chunks[ident]["text"]
        patches = self.decisions[ident]["primary_text_patches"]
        self.assertTrue(patches)
        for change in patches:
            self.assertEqual(expected.count(change["old"]), 1)
            expected = expected.replace(change["old"], change["new"])
        self.assertEqual(record["primary_text"], expected)

    def test_family_values_stay_separate_from_explanatory_warnings(self):
        prefix = "2171833:22740735:candidate:family_parking_dimension_misattribution-"
        one = self.records[prefix + "1"]
        two = self.records[prefix + "2"]
        self.assertNotIn("0.35", one["primary_text"])
        self.assertIn("도면에 미표기", one["primary_text"])
        self.assertIn("확장형(2면)", one["text"])
        self.assertIn("각 면 2.8m", two["primary_text"])
        self.assertIn("2.6m이상", two["primary_text"])
        self.assertIn("전체 0.7m(각각 0.35m)", two["primary_text"])

    def test_only_explicitly_reviewed_body_conditions_enter_embedding_text(self):
        with_body = [r for r in self.records.values() if r["required_body_contexts"]]
        self.assertEqual({r["chunk_id"] for r in with_body},
                         {"2171833:22740729:candidate:p1-e2-r1", "2171833:22740729:candidate:p1-e2-r2"})
        for record in self.records.values():
            ids = self.decisions[record["chunk_id"]]["required_body_chunk_ids"]
            self.assertEqual([r["chunk_id"] for r in record["required_body_contexts"]], ids)
            for context in record["required_body_contexts"]:
                self.assertEqual(context["chunk_id"], "2171833:002000:3")
                self.assertIn("중ㆍ대형 승합자동차", context["quote_text"])
                self.assertIn(context["quote_text"], record["text"])

    def test_ditto_context_references_unit_without_importing_other_rows_rate(self):
        prefix = "2147709:22416021:candidate:p1-e1-r"
        for row in (3, 4, 5, 8):
            record = self.records[prefix + str(row)]
            context, = record["additional_contexts"]
            self.assertEqual(context["source_chunk_id"], prefix + "1")
            self.assertIn("단위기준: 연액", context["text"])
            self.assertIn("선행 행의 요율은 가져오지 않음", context["text"])
            self.assertNotIn("/1,000", context["text"])
            self.assertNotIn("해당재산가액", context["text"])

    def test_delegation_antecedents_preserve_order_without_replacing_same_law(self):
        prefix = "2147709:22416027:candidate:delegation_table_vertical_alignment_lost-"
        expected = {2: [0, 1], **{i: list(range(3, i)) for i in range(4, 13)}}
        for index, previous in expected.items():
            record = self.records[prefix + str(index)]
            self.assertIn("같은 법률", record["primary_text"])
            expected_ids = [prefix + str(i) for i in previous]
            self.assertEqual(self.decisions[record["chunk_id"]]["additional_context_chunk_ids"], expected_ids)
            contexts = record["additional_contexts"]
            self.assertEqual([c["source_chunk_id"] for c in contexts], expected_ids)
            for context, ident in zip(contexts, expected_ids):
                source = self.chunks[ident]
                law = next(f["value"] for f in source["fields"] if f["name"] == "근거법령 원문 표기")
                self.assertIn(law, context["text"])
                self.assertIn(source["label"], context["text"])
                self.assertIn(context["text"], record["text"])


@unittest.skipUnless((PROJECT / "data/processed/attachments/2026-10-01/embedding_ready/readiness.manifest.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/processed/attachments/2026-10-01/embedding_ready/readiness.manifest.json")
class ReadinessStorageTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=TEST_ROOT)).resolve()
        self.addCleanup(self.cleanup_project)
        self.guards = no_external_access()
        self.addCleanup(self.guards.close)
        self.manifest = json.loads((PROJECT / readiness.READINESS_FOLDER / "readiness.manifest.json").read_bytes())
        for relative, expected in self.manifest["input_files"].items():
            rel = Path(relative)
            source = (PROJECT / rel).resolve(strict=True)
            target = (self.project / rel).resolve()
            self.assertEqual(rel.parts[0], "data")
            self.assertTrue(source.is_relative_to(PROJECT))
            self.assertTrue(target.is_relative_to(self.project))
            self.assertEqual(digest(source.read_bytes()), expected)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self.baseline = {name: (PROJECT / readiness.READINESS_FOLDER / name).read_bytes() for name in OUTPUT_NAMES}

    def cleanup_project(self):
        root, scope = self.project.resolve(), TEST_ROOT.resolve()
        if root.parent != scope or not root.is_relative_to(scope) or not root.name.startswith(TEMP_PREFIX):
            raise AssertionError("Unsafe cleanup target: " + str(root))
        if root.exists():
            shutil.rmtree(root)

    def output(self, name):
        return self.project / readiness.READINESS_FOLDER / name

    def assert_no_outputs(self, except_names=()):
        for name in OUTPUT_NAMES:
            if name not in except_names:
                self.assertFalse(self.output(name).exists(), name)

    def input_snapshot(self):
        return {rel: ((self.project / rel).read_bytes(), (self.project / rel).stat().st_mtime_ns)
                for rel in self.manifest["input_files"]}

    def save_baseline(self):
        for name, raw in self.baseline.items():
            self.output(name).write_bytes(raw)

    def mutate_xml_after_upstream_validation(self, operation):
        relative = next(p for p in self.manifest["input_files"] if p.endswith(".xml"))
        target = self.project / relative
        original_raw = target.read_bytes()
        upstream = candidates.run_batch
        mutations = []

        def validate_then_change(project, check_only=False):
            self.assertEqual(Path(project).resolve(), self.project)
            self.assertIs(check_only, True)
            result = upstream(project, check_only=check_only)
            target.write_bytes(original_raw + b"\n")
            mutations.append(relative)
            return result

        with patch.object(candidates, "run_batch", side_effect=validate_then_change):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(mutations, [relative])
        self.assertEqual(target.read_bytes(), original_raw + b"\n")

    def test_prepare_is_reproducible_and_leaves_all_source_bytes_and_mtimes_intact(self):
        before = self.input_snapshot()
        outputs, report = readiness.prepare_readiness(self.project)
        self.assertEqual(outputs, self.baseline)
        self.assertEqual(list(outputs)[-1], "readiness.manifest.json")
        self.assertEqual(self.input_snapshot(), before)
        self.assert_no_outputs()

    def test_review_tampering_is_rejected_before_any_output(self):
        path = self.project / readiness.REVIEW_FILE
        changed = json.loads(path.read_bytes())
        changed["decisions"][0]["reason"] += " altered"
        path.write_bytes(json_bytes(changed))
        with self.assertRaises(ValueError):
            readiness.run_readiness(self.project)
        self.assert_no_outputs()

    def test_source_candidate_tampering_is_rejected_before_any_output(self):
        path = self.project / candidates.FOLDER / "candidate_chunks.json"
        changed = json.loads(path.read_bytes())
        changed["documents"][0]["chunks"][0]["text"] += " altered"
        path.write_bytes(json_bytes(changed))
        with self.assertRaises(ValueError):
            readiness.run_readiness(self.project)
        self.assert_no_outputs()

    def test_identical_rerun_and_check_preserve_output_and_input_mtimes(self):
        before = self.input_snapshot()
        report = readiness.run_readiness(self.project)
        saved = {n: (self.output(n).read_bytes(), self.output(n).stat().st_mtime_ns) for n in OUTPUT_NAMES}
        self.assertEqual(readiness.run_readiness(self.project), report)
        self.assertEqual(readiness.run_readiness(self.project, check_only=True), report)
        self.assertEqual({n: (self.output(n).read_bytes(), self.output(n).stat().st_mtime_ns) for n in OUTPUT_NAMES}, saved)
        self.assertEqual(self.input_snapshot(), before)
        self.assertEqual(readiness.load_ready_inputs(self.project), json.loads(saved["embedding_inputs.json"][0])["records"])

    def test_late_manifest_conflict_stops_preceding_output_writes(self):
        target = self.output("readiness.manifest.json")
        target.write_bytes(b"unrelated existing manifest")
        with self.assertRaises(ValueError):
            readiness.run_readiness(self.project)
        self.assertEqual(target.read_bytes(), b"unrelated existing manifest")
        self.assert_no_outputs(except_names={"readiness.manifest.json"})

    def test_input_change_after_prepare_stops_all_output_writes(self):
        original = readiness.prepare_readiness

        def prepare_then_change(project):
            outputs, report = original(project)
            path = Path(project) / readiness.REVIEW_FILE
            path.write_bytes(path.read_bytes() + b" ")
            return outputs, report

        with patch.object(readiness, "prepare_readiness", side_effect=prepare_then_change):
            with self.assertRaises(ValueError):
                readiness.run_readiness(self.project)
        self.assert_no_outputs()

    def test_prepare_rejects_xml_change_immediately_after_upstream_validation(self):
        self.mutate_xml_after_upstream_validation(lambda: readiness.prepare_readiness(self.project))
        self.assert_no_outputs()

    def test_loader_rejects_xml_change_immediately_after_upstream_validation(self):
        self.save_baseline()
        before = {n: (self.output(n).read_bytes(), self.output(n).stat().st_mtime_ns) for n in OUTPUT_NAMES}
        self.mutate_xml_after_upstream_validation(lambda: readiness.load_ready_inputs(self.project))
        self.assertEqual({n: (self.output(n).read_bytes(), self.output(n).stat().st_mtime_ns) for n in OUTPUT_NAMES}, before)

    def test_output_tampering_blocks_loader_even_if_text_hash_is_recomputed(self):
        self.save_baseline()
        target = self.output("embedding_inputs.json")
        data = json.loads(target.read_bytes())
        data["records"][0]["text"] += " unauthorized condition"
        data["records"][0]["text_sha256"] = digest(data["records"][0]["text"].encode("utf-8"))
        target.write_bytes(json_bytes(data))
        changed = target.read_bytes()
        with self.assertRaises(ValueError):
            readiness.load_ready_inputs(self.project)
        self.assertEqual(target.read_bytes(), changed)


if __name__ == "__main__":
    unittest.main()
