"""Offline atomic JSON persistence tests; writes stay in artifact temp folders."""
import json
import multiprocessing
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import search_attachment_chunks as search

ARTIFACT_ROOT = Path(__file__).resolve().parent


def payload(writer, sequence):
    return {"writer": writer, "sequence": sequence,
            "text": f"한글 원문 writer={writer} sequence={sequence} " * 256}


def process_writer(path, index, ready, count):
    ready.wait(timeout=20)
    for sequence in range(count):
        search.save_json(Path(path), payload(index, sequence))


class AtomicJsonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ARTIFACT_ROOT, prefix="atomic-json-offline-")
        self.folder = Path(temporary.name).resolve()
        self.assertTrue(self.folder.is_relative_to(ARTIFACT_ROOT.resolve()))
        self.addCleanup(temporary.cleanup)
        self.path = self.folder / "result.json"
        self.initial = {"previous": "보존해야 할 결과"}
        search.save_json(self.path, self.initial)
        self.original_bytes = self.path.read_bytes()
        self.network = patch.object(search.requests, "post", side_effect=AssertionError("offline only"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def assert_no_temporary_files(self, extra=()):
        self.assertEqual({path.name for path in self.folder.iterdir()}, {"result.json", *extra})

    def assert_complete_payload(self, value, writers, count):
        self.assertIn(value["writer"], range(writers))
        self.assertIn(value["sequence"], range(count))
        self.assertEqual(value, payload(value["writer"], value["sequence"]))

    def test_concurrent_thread_writers_use_independent_temporary_files(self):
        writers, count = 8, 12
        ready = threading.Barrier(writers)
        # A pre-existing legacy temp file is not owned by any current writer.
        legacy = self.folder / "result.json.tmp"
        legacy.write_bytes(b"another process owns this")
        def write(index):
            ready.wait(timeout=10)
            for sequence in range(count):
                search.save_json(self.path, payload(index, sequence))
        done, observations, errors = threading.Event(), [], []
        def observe():
            while not done.is_set():
                try:
                    value = search.read_json(self.path)
                    if value != self.initial:
                        self.assert_complete_payload(value, writers, count)
                    observations.append(value)
                except PermissionError:
                    # Windows may briefly lock replacement; a later read must
                    # still be a complete old or new document, never fragments.
                    pass
                except Exception as exc:
                    errors.append(exc)
                    return
                done.wait(0.001)
        reader = threading.Thread(target=observe)
        reader.start()
        try:
            with ThreadPoolExecutor(max_workers=writers) as executor:
                list(executor.map(write, range(writers)))
        finally:
            done.set()
            reader.join(timeout=5)
        self.assertFalse(reader.is_alive())
        self.assertFalse(errors, errors)
        self.assertTrue(observations)
        self.assert_complete_payload(search.read_json(self.path), writers, count)
        self.assertEqual(legacy.read_bytes(), b"another process owns this")
        self.assert_no_temporary_files(extra=[legacy.name])

    def test_concurrent_process_writers_leave_one_complete_result(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        writers, count = 4, 8
        processes = [context.Process(target=process_writer,
                                    args=(str(self.path), index, ready, count))
                     for index in range(writers)]
        try:
            for process in processes:
                process.start()
            ready.set()
            for process in processes:
                process.join(timeout=30)
                self.assertFalse(process.is_alive(), "Writer did not finish")
                self.assertEqual(process.exitcode, 0)
        finally:
            # Only children started by this test are eligible for cleanup.
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                process.close()
        self.assert_complete_payload(search.read_json(self.path), writers, count)
        self.assert_no_temporary_files()

    def test_nonfinite_or_nonserializable_values_preserve_previous_bytes(self):
        for invalid in [float("nan"), float("inf"), -float("inf"), object()]:
            with self.subTest(value=type(invalid).__name__):
                with self.assertRaises((ValueError, TypeError)):
                    search.save_json(self.path, {"value": invalid})
                self.assertEqual(self.path.read_bytes(), self.original_bytes)
                self.assert_no_temporary_files()

    def test_replace_failure_preserves_previous_result_and_cleans_owned_temp(self):
        with patch.object(Path, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaisesRegex(OSError, "injected replace failure"):
                search.save_json(self.path, {"next": "new result"})
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assert_no_temporary_files()

    def test_flush_failure_preserves_previous_result_and_cleans_owned_temp(self):
        with patch.object(search.os, "fsync", side_effect=OSError("injected flush failure")):
            with self.assertRaisesRegex(OSError, "injected flush failure"):
                search.save_json(self.path, {"next": "new result"})
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assert_no_temporary_files()

    def test_unicode_and_exact_json_object_survive_atomic_write(self):
        value = {"근거": ["표", "비고\n조건"], "enabled": False, "count": 2.67}
        search.save_json(self.path, value)
        self.assertEqual(json.loads(self.path.read_bytes()), value)
        self.assertIn("근거", self.path.read_text(encoding="utf-8"))
        self.assert_no_temporary_files()


    @unittest.skipUnless(search.os.name == "nt", "Windows sharing retry policy")
    def test_permanent_windows_sharing_failure_has_bounded_retries_and_cleanup(self):
        failure = PermissionError("injected persistent sharing failure")
        failure.winerror = 32
        with patch.object(Path, "replace", side_effect=failure) as replace:
            with patch.object(search.time, "sleep") as sleep:
                with self.assertRaises(PermissionError):
                    search.save_json(self.path, {"next": "new result"})
        self.assertEqual(replace.call_count, 8)
        self.assertEqual(sleep.call_count, 7)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assert_no_temporary_files()

if __name__ == "__main__":
    unittest.main()


