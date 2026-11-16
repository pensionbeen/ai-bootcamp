"""실행기의 도움말·누락 검사·실패 중단 동작. 외부 API 사용 없음."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run as runner


class RunnerTests(unittest.TestCase):
    def test_no_arguments_only_show_help(self):
        with patch.object(runner, "check_saved_outputs") as check, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.main([]), 0)
        check.assert_not_called()
        self.assertIn("check", output.getvalue())

    def test_missing_output_is_error_and_does_not_create_it(self):
        parent = Path(__file__).resolve().parent
        temporary = tempfile.TemporaryDirectory(dir=parent, prefix="runner-test-")
        folder = Path(temporary.name).resolve()
        try:
            module = SimpleNamespace(prepare=lambda project: ({"result.json": b"{}"}, {}))
            with self.assertRaisesRegex(ValueError, "산출물이 없습니다"):
                runner.check_saved_outputs(module, folder)
            self.assertEqual(list(folder.iterdir()), [])
        finally:
            self.assertTrue(folder.is_relative_to(parent) and folder.name.startswith("runner-test-"))
            temporary.cleanup()

    def test_missing_tests_do_not_report_success(self):
        with patch.object(runner.unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(["test"]), 1)

    def test_body_failure_stops_before_supplement_build(self):
        import build_ordinance_corpus as body
        import build_supplement_context as supplements
        with patch.object(body, "run", side_effect=ValueError("입력 오류")), patch.object(supplements, "run") as second, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(["build"]), 1)
        second.assert_not_called()
