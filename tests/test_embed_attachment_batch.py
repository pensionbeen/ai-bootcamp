"""Offline batch embedding lifecycle checks using manifest-listed fixtures.

No real keys or service requests are used. A fake provider returns deterministic
vectors in reverse index order so association and retry behavior are observable.
"""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import embed_attachment_chunks as embedding


PROJECT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
TEMP_PREFIX = "embedding-batch-test-"
DUMMY_KEY = "offline-placeholder-not-a-real-key"
FINAL_OUTPUTS = ("embeddings.json", "embedding_report.json", "embeddings.manifest.json")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@unittest.skipUnless((PROJECT / "data/processed/attachments/2026-10-01/embedding_ready/readiness.manifest.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/processed/attachments/2026-10-01/embedding_ready/readiness.manifest.json")
class EmbeddingBatchTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=TEST_ROOT)).resolve()
        self.addCleanup(self.cleanup_project)
        network = patch.object(requests.sessions.Session, "request",
                               side_effect=AssertionError("Live HTTP is forbidden in embedding tests"))
        self.network = network.start()
        self.addCleanup(network.stop)
        dotenv = patch.object(embedding, "load_dotenv", side_effect=AssertionError("No environment file may be read"))
        self.dotenv = dotenv.start()
        self.addCleanup(dotenv.stop)
        dotenv_values = patch.object(embedding, "dotenv_values", side_effect=AssertionError("No environment file may be read"))
        self.dotenv_values = dotenv_values.start()
        self.addCleanup(dotenv_values.stop)
        getenv = patch.object(embedding.os, "getenv", return_value=DUMMY_KEY)
        self.getenv = getenv.start()
        self.addCleanup(getenv.stop)
        api = patch.object(embedding, "call_embedding_api", side_effect=self.fake_api)
        self.api = api.start()
        self.addCleanup(api.stop)

        manifest_path = embedding.READINESS_FOLDER + "/readiness.manifest.json"
        manifest_raw = (PROJECT / manifest_path).read_bytes()
        manifest = json.loads(manifest_raw)
        self.inputs = {**manifest["input_files"], manifest_path: digest(manifest_raw)}
        self.inputs.update({embedding.READINESS_FOLDER + "/" + name: expected
                            for name, expected in manifest["output_files"].items()})
        for relative, expected in self.inputs.items():
            rel = Path(relative)
            source = (PROJECT / rel).resolve(strict=True)
            target = (self.project / rel).resolve()
            self.assertEqual(rel.parts[0], "data")
            self.assertTrue(source.is_relative_to(PROJECT))
            self.assertTrue(target.is_relative_to(self.project))
            self.assertEqual(digest(source.read_bytes()), expected)
            self.assertFalse(relative.startswith(embedding.BATCH_FOLDER + "/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self.ready = json.loads((self.project / embedding.READINESS_FOLDER / "embedding_inputs.json").read_bytes())["records"]
        self.before = self.input_snapshot()

    def cleanup_project(self):
        root, scope = self.project.resolve(), TEST_ROOT.resolve()
        if root.parent != scope or not root.is_relative_to(scope) or not root.name.startswith(TEMP_PREFIX):
            raise AssertionError("Unsafe cleanup target: " + str(root))
        if root.exists():
            shutil.rmtree(root)

    def input_snapshot(self):
        return {relative: ((self.project / relative).read_bytes(), (self.project / relative).stat().st_mtime_ns)
                for relative in self.inputs}

    def output(self, name):
        return self.project / embedding.BATCH_FOLDER / name

    def write_output(self, name, raw):
        path = self.output(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def batch_snapshot(self):
        folder = self.project / embedding.BATCH_FOLDER
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in folder.glob("*.json")} if folder.exists() else {}

    def assert_no_final_outputs(self, except_names=()):
        for name in FINAL_OUTPUTS:
            if name not in except_names:
                self.assertFalse(self.output(name).exists(), name)

    def vector(self, index):
        return [float(index + 1)] + [0.0] * (embedding.DIMENSION - 1)

    def response(self, count):
        return {"object": "list", "model": embedding.MODEL,
                "data": [{"object": "embedding", "index": i, "embedding": self.vector(i)}
                         for i in reversed(range(count))],
                "usage": {"prompt_tokens": count * 3, "total_tokens": count * 3}}

    def fake_api(self, payload_bytes, key):
        self.assertIsInstance(payload_bytes, bytes)
        self.assertEqual(key, DUMMY_KEY)
        payload = json.loads(payload_bytes)
        self.assertEqual(payload["model"], "solar-embedding-2-passage")
        self.assertEqual(payload["input"], [r["text"] for r in self.ready])
        self.assertTrue(1 <= len(payload["input"]) <= 100)
        return self.response(len(payload["input"]))

    def complete(self):
        result = embedding.run_embeddings(self.project)
        self.assertEqual(self.api.call_count, 1)
        return result

    def reject_invalid_response(self, mutate):
        def invalid(payload_bytes, key):
            value = self.fake_api(payload_bytes, key)
            mutate(value)
            return value

        self.api.side_effect = invalid
        with self.assertRaises(ValueError):
            embedding.run_embeddings(self.project)
        self.assertEqual(self.api.call_count, 1)
        self.assertTrue(self.output("batch-0001.attempt.json").exists())
        self.assertTrue(self.output("batch-0001.response.json").exists())
        self.assert_no_final_outputs()
        saved = self.batch_snapshot()
        self.api.reset_mock()
        self.getenv.reset_mock()
        with self.assertRaises(ValueError):
            embedding.run_embeddings(self.project)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assertEqual(self.batch_snapshot(), saved)

    def test_plan_contains_only_the_62_approved_texts_without_key_or_api_access(self):
        plan = embedding.prepare_embedding_plan(self.project)
        self.assertEqual(len(plan["records"]), 62)
        self.assertEqual(plan["records"], self.ready)
        self.assertEqual(len(plan["batches"]), 1)
        batch = plan["batches"][0]
        self.assertEqual(batch["records"], self.ready)
        self.assertEqual(batch["payload"]["input"], [r["text"] for r in self.ready])
        self.assertTrue(batch["identity"])
        self.assertTrue(all(r["embedding_allowed"] and r["answer_allowed"] is False for r in plan["records"]))
        self.assertFalse(any(r["input_binding_status"] == "manual_export_unverified" for r in plan["records"]))
        self.assertEqual(self.input_snapshot(), self.before)
        self.assert_no_final_outputs()
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.dotenv.assert_not_called()
        self.dotenv_values.assert_not_called()

    def test_reversed_provider_indices_bind_to_chunk_and_exact_text_hash(self):
        self.complete()
        output = json.loads(self.output("embeddings.json").read_bytes())
        vectors = output["vectors"]
        self.assertEqual(len(vectors), 62)
        self.assertEqual(len({v["chunk_id"] for v in vectors}), 62)
        for index, (record, vector) in enumerate(zip(self.ready, vectors)):
            self.assertEqual(vector["chunk_id"], record["chunk_id"])
            self.assertEqual(vector["text_sha256"], record["text_sha256"])
            self.assertEqual(vector["embedding"], self.vector(index))
        loaded = embedding.load_embedded_records(self.project)
        self.assertEqual(len(loaded), 62)
        for index, (expected, record) in enumerate(zip(self.ready, loaded)):
            record = dict(record)
            self.assertEqual(record.pop("embedding"), self.vector(index))
            self.assertEqual(record, expected)
        self.assertEqual(self.input_snapshot(), self.before)

    def test_complete_rerun_and_check_do_not_read_key_call_api_or_rewrite_files(self):
        self.complete()
        saved = self.batch_snapshot()
        self.api.reset_mock()
        self.getenv.reset_mock()
        embedding.run_embeddings(self.project)
        embedding.run_embeddings(self.project, check_only=True)
        embedding.check_embeddings(self.project)
        embedding.load_embedded_records(self.project)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.dotenv.assert_not_called()
        self.assertEqual(self.batch_snapshot(), saved)
        self.assertEqual(self.input_snapshot(), self.before)

    def test_62_inputs_are_sent_sequentially_as_20_20_20_2_with_correct_vector_binding(self):
        requested_sizes = []

        def sequential_provider(payload_bytes, key):
            number = len(requested_sizes) + 1
            start = (number - 1) * 20
            expected = self.ready[start:start + 20]
            payload = json.loads(payload_bytes)
            self.assertEqual(key, DUMMY_KEY)
            self.assertEqual(payload["model"], embedding.MODEL)
            self.assertEqual(payload["input"], [r["text"] for r in expected])
            self.assertTrue(self.output(f"batch-{number:04d}.attempt.json").exists())
            self.assertFalse(self.output(f"batch-{number:04d}.response.json").exists())
            if number > 1:
                previous_path = self.output(f"batch-{number - 1:04d}.response.json")
                self.assertTrue(previous_path.exists(), "The next request started before the previous response was saved")
                previous = json.loads(previous_path.read_bytes())
                self.assertEqual(previous["request"]["batch_id"], f"batch-{number - 1:04d}")
                self.assertEqual(sorted(v["index"] for v in previous["response"]["data"]), list(range(20)))
            requested_sizes.append(len(expected))
            # Provider indices restart at zero in every batch. Vector values
            # identify the global input, and reversed order exercises pairing.
            return {"model": embedding.MODEL,
                    "data": [{"index": local, "embedding": self.vector(start + local)}
                             for local in reversed(range(len(expected)))],
                    "usage": {"prompt_tokens": len(expected) * 3, "total_tokens": len(expected) * 3}}

        self.api.side_effect = sequential_provider
        with patch.object(embedding, "BATCH_SIZE", 20):
            report = embedding.run_embeddings(self.project)
            self.assertEqual(requested_sizes, [20, 20, 20, 2])
            self.assertEqual(self.api.call_count, 4)
            self.assertEqual(report["called"], 4)
            self.assertEqual(report["api_batch_count"], 4)
            loaded = embedding.load_embedded_records(self.project)
        self.assertEqual(len(loaded), 62)
        for index, (expected, record) in enumerate(zip(self.ready, loaded)):
            self.assertEqual(record["chunk_id"], expected["chunk_id"])
            self.assertEqual(record["text_sha256"], expected["text_sha256"])
            self.assertEqual(record["embedding"], self.vector(index))
        self.assertEqual(self.input_snapshot(), self.before)

    def test_check_incomplete_collection_never_calls_provider(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            embedding.check_embeddings(self.project)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assert_no_final_outputs()

    def test_uncertain_attempt_blocks_automatic_retry(self):
        self.api.side_effect = ValueError("processing outcome is unknown")
        with self.assertRaises(ValueError):
            embedding.run_embeddings(self.project)
        self.assertTrue(self.output("batch-0001.attempt.json").exists())
        self.assertFalse(self.output("batch-0001.response.json").exists())
        self.assert_no_final_outputs()
        saved = self.batch_snapshot()
        self.api.reset_mock()
        self.getenv.reset_mock()
        with self.assertRaises(ValueError):
            embedding.run_embeddings(self.project)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assertEqual(self.batch_snapshot(), saved)

    def test_duplicate_response_index_is_preserved_and_not_retried(self):
        self.reject_invalid_response(lambda response: response["data"][-1].update(index=response["data"][0]["index"]))

    def test_wrong_response_model_is_preserved_and_not_retried(self):
        self.reject_invalid_response(lambda response: response.update(model="solar-embedding-2-query"))

    def test_wrong_vector_dimension_is_preserved_and_not_retried(self):
        self.reject_invalid_response(lambda response: response["data"][0]["embedding"].pop())

    def test_nan_vector_is_preserved_and_not_retried(self):
        self.reject_invalid_response(lambda response: response["data"][0]["embedding"].__setitem__(1, float("nan")))

    def test_boolean_vector_value_is_preserved_and_not_retried(self):
        self.reject_invalid_response(lambda response: response["data"][0]["embedding"].__setitem__(1, True))

    def test_zero_vector_is_preserved_and_not_retried(self):
        self.reject_invalid_response(lambda response: response["data"][0].update(embedding=[0.0] * embedding.DIMENSION))

    def test_missing_response_index_is_preserved_and_not_retried(self):
        self.reject_invalid_response(lambda response: response["data"].pop())

    def test_source_xml_change_before_run_blocks_key_and_network_access(self):
        relative = next(p for p in self.inputs if p.endswith(".xml"))
        path = self.project / relative
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            embedding.run_embeddings(self.project)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assert_no_final_outputs()

    def test_ready_text_change_even_with_new_text_hash_is_rejected_before_request(self):
        path = self.project / embedding.READINESS_FOLDER / "embedding_inputs.json"
        data = json.loads(path.read_bytes())
        data["records"][0]["text"] += " changed after review"
        data["records"][0]["text_sha256"] = digest(data["records"][0]["text"].encode("utf-8"))
        path.write_bytes(json_bytes(data))
        with self.assertRaises(ValueError):
            embedding.run_embeddings(self.project)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assert_no_final_outputs()

    def test_existing_final_manifest_conflict_blocks_request_and_other_writes(self):
        target = self.write_output("embeddings.manifest.json", b"unrelated existing output")
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            embedding.run_embeddings(self.project)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assertEqual(target.read_bytes(), b"unrelated existing output")
        self.assertFalse(self.output("batch-0001.attempt.json").exists())
        self.assert_no_final_outputs(except_names={"embeddings.manifest.json"})

    def test_input_change_during_api_preserves_response_but_stops_final_outputs(self):
        relative = next(p for p in self.inputs if p.endswith(".xml"))

        def provider_then_input_change(payload_bytes, key):
            response = self.fake_api(payload_bytes, key)
            target = self.project / relative
            target.write_bytes(target.read_bytes() + b"\n")
            return response

        self.api.side_effect = provider_then_input_change
        with self.assertRaises(ValueError):
            embedding.run_embeddings(self.project)
        self.assertEqual(self.api.call_count, 1)
        self.assertTrue(self.output("batch-0001.response.json").exists())
        self.assertTrue(self.output("batch-0001.attempt.json").exists())
        self.assert_no_final_outputs()

    def test_vector_output_tampering_blocks_downstream_loader(self):
        self.complete()
        target = self.output("embeddings.json")
        data = json.loads(target.read_bytes())
        data["vectors"][0]["embedding"][0] += 0.25
        target.write_bytes(json_bytes(data))
        saved = self.batch_snapshot()
        self.api.reset_mock()
        self.getenv.reset_mock()
        with self.assertRaises(ValueError):
            embedding.load_embedded_records(self.project)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assertEqual(self.batch_snapshot(), saved)

    def test_saved_response_with_wrong_request_binding_is_not_reused(self):
        self.complete()
        target = self.output("batch-0001.response.json")
        saved_response = json.loads(target.read_bytes())
        saved_response["request"]["input_items"][0]["text_sha256"] = "0" * 64
        target.write_bytes(json_bytes(saved_response))
        saved = self.batch_snapshot()
        self.api.reset_mock()
        self.getenv.reset_mock()
        with self.assertRaisesRegex(ValueError, "요청 연결 불일치"):
            embedding.run_embeddings(self.project)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assertEqual(self.batch_snapshot(), saved)

    def test_response_without_attempt_is_rejected_without_provider_retry(self):
        self.complete()
        attempt = self.output("batch-0001.attempt.json").resolve(strict=True)
        self.assertTrue(attempt.is_relative_to(self.project))
        self.assertEqual(attempt.name, "batch-0001.attempt.json")
        attempt.unlink()
        saved = self.batch_snapshot()
        self.api.reset_mock()
        self.getenv.reset_mock()
        with self.assertRaisesRegex(ValueError, "요청 기록 없는"):
            embedding.run_embeddings(self.project)
        self.api.assert_not_called()
        self.getenv.assert_not_called()
        self.assertEqual(self.batch_snapshot(), saved)


if __name__ == "__main__":
    unittest.main()
