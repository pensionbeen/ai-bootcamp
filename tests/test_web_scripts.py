"""브라우저 화면 스크립트(src/web)의 Node 시험을 run.py test에서 함께 돌립니다. node가 없으면 건너뜁니다."""
from pathlib import Path
import shutil
import subprocess
import unittest

TESTS = Path(__file__).resolve().parent
NODE_TESTS = ("test_web_app_api.mjs", "test_ask_validation.mjs", "test_web_i18n.mjs")


@unittest.skipIf(shutil.which("node") is None, "node가 없어 화면 스크립트 시험을 건너뜁니다")
class WebScriptTests(unittest.TestCase):
    def test_node_suites_pass(self):
        result = subprocess.run(["node", "--test", *(str(TESTS / name) for name in NODE_TESTS)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
