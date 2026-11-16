import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import answer_attachment as answer
from request_lock import RequestInProgress, request_lock


ROOT = Path(__file__).resolve().parent


def attempt_lock(lock_path, result_path):
    try:
        with request_lock(lock_path):
            value = "acquired"
    except RequestInProgress:
        value = "busy"
    Path(result_path).write_text(value, encoding="utf-8")


class RequestLockTests(unittest.TestCase):
    def test_actual_process_cannot_take_held_lock_and_can_take_released_lock(self):
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="lock-test-") as temporary:
            folder = Path(temporary).resolve()
            self.assertTrue(folder.is_relative_to(ROOT))
            lock_path, result_path = folder / "same.lock", folder / "result.txt"
            with request_lock(lock_path):
                process = multiprocessing.get_context("spawn").Process(
                    target=attempt_lock, args=(lock_path, result_path), daemon=True)
                try:
                    process.start()
                    process.join(5)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(2)
                    if not process.is_alive():
                        process.close()
                self.assertEqual(result_path.read_text(encoding="utf-8"), "busy")
            attempt_lock(lock_path, result_path)
            self.assertEqual(result_path.read_text(encoding="utf-8"), "acquired")

    def test_same_input_busy_does_not_delete_active_request_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="busy-cli-") as temporary:
            folder = Path(temporary).resolve()
            self.assertTrue(folder.is_relative_to(ROOT))
            key = answer.digest_json({"question": "test"})[:16]
            response = folder / f"{key}.draft.response.json"
            audit = folder / f"{key}.audit.json"
            response.write_text("old response", encoding="utf-8")
            audit.write_text("old audit", encoding="utf-8")
            argv = ["answer_attachment.py", "--question", "test", "--demo", "--output-dir", str(folder)]
            with request_lock(folder / ".locks" / f"{key}.lock"), patch("sys.argv", argv), \
                    patch("builtins.print"), patch.object(answer, "run_with_deadline") as run:
                with self.assertRaises(SystemExit) as caught:
                    answer.main()
                self.assertEqual(caught.exception.code, 2)
                run.assert_not_called()
            self.assertEqual(response.read_text(encoding="utf-8"), "old response")
            self.assertEqual(audit.read_text(encoding="utf-8"), "old audit")

    def test_timeout_is_error_and_removes_only_same_input_draft(self):
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="timeout-cli-") as temporary:
            folder = Path(temporary).resolve()
            self.assertTrue(folder.is_relative_to(ROOT))
            key = answer.digest_json({"question": "test"})[:16]
            response = folder / f"{key}.draft.response.json"
            response.write_text("old response", encoding="utf-8")
            other = folder / "other.draft.response.json"
            other.write_text("other response", encoding="utf-8")
            argv = ["answer_attachment.py", "--question", "test", "--demo", "--output-dir", str(folder)]
            with patch("sys.argv", argv), patch("builtins.print"), patch.object(
                    answer, "run_with_deadline", side_effect=answer.RequestDeadlineExceeded):
                with self.assertRaises(SystemExit) as caught:
                    answer.main()
                self.assertEqual(caught.exception.code, 1)
            self.assertFalse(response.exists())
            self.assertEqual(other.read_text(encoding="utf-8"), "other response")
            audit = answer.read_json(folder / f"{key}.audit.json")
            self.assertEqual(audit["status"], "error")
            self.assertEqual(audit["error_code"], "request_deadline_exceeded")
            self.assertIsNone(audit["response"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
