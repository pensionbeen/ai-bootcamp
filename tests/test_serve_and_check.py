"""serve_and_check: start → /health → checks → stop, using a stand-in server process. No API, no DB."""
import contextlib
import io
import json
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import serve_and_check as sac  # noqa: E402
from local_rag_service import create_server
from request_audit import RequestAudit
from test_local_rag_service import FakeRuntime, full_publicizer, health, ready

FAKE_SERVER = textwrap.dedent("""
    import json, sys, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port, mode = int(sys.argv[1]), sys.argv[2]
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, obj, outcome=None):
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", "application/json")
            if outcome: self.send_header("X-Rag-Status", outcome)
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            self._send(200, {"status": "ok", "database": {"identity": "fake"}, "sync_wait_seconds": 80})
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0)); self.rfile.read(n)
            if mode == "good":
                self._send(200, {"answer": "답", "citation": ["제1조"],
                                 "source_url": "https://example.invalid", "effective_date": "2026-01-01"}, "completed")
            elif mode == "detail":
                self._send(200, {"answer": "답", "citation": ["제1조"], "status": "completed",
                                 "source_url": "https://example.invalid", "effective_date": "2026-01-01"})
            else:
                self._send(200, {"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""},
                           "deadline_exceeded")
    HTTPServer(("127.0.0.1", port), H).serve_forever()
""")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServeAndCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script = Path(self.tmp.name) / "fake_server.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.log = Path(self.tmp.name) / "serve.log"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, mode, argv):
        def fake_start(python, port, host="127.0.0.1", log_path=None):
            process = subprocess.Popen([sys.executable, str(self.script), str(port), mode],
                                       creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            process._serve_log = None
            return process
        with patch.object(sac, "start_server", fake_start), contextlib.redirect_stdout(io.StringIO()) as out:
            code = sac.main(argv + ["--log", str(self.log)])
        return code, out.getvalue()

    def test_good_answer_passes_and_server_is_stopped(self):
        port = free_port()
        code, out = self._run("good", ["--port", str(port), "--question", "q1"])
        self.assertEqual(code, 0)
        self.assertIn("[PASS]", out)
        self.assertIn("서버 종료", out)
        with self.assertRaises(OSError):  # port is free again → server really stopped
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_deadline_exceeded_fails_but_still_stops_server(self):
        port = free_port()
        code, out = self._run("timeout", ["--port", str(port), "--question", "q1"])
        self.assertEqual(code, 1)
        self.assertIn("reason=deadline_exceeded", out)
        self.assertIn("서버 종료", out)

    def test_detailed_body_on_ask_fails_as_unexpected_fields(self):
        port = free_port()
        code, out = self._run("detail", ["--port", str(port), "--question", "q1"])
        self.assertEqual(code, 1)
        self.assertIn("reason=unexpected_fields: status", out)
        self.assertIn("status=None", out)

    def test_health_only_makes_no_ask_call(self):
        port = free_port()
        code, out = self._run("timeout", ["--port", str(port), "--health-only"])
        self.assertEqual(code, 0)
        self.assertIn("/health OK", out)
        self.assertNotIn("[PASS]", out)
        self.assertNotIn("[FAIL]", out)

    def test_server_that_never_answers_health_is_reported_and_killed(self):
        port = free_port()
        def dead_start(python, port, host="127.0.0.1", log_path=None):
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                       creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            process._serve_log = None
            return process
        with patch.object(sac, "start_server", dead_start), patch.object(sac, "HEALTH_TIMEOUT", 2), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = sac.main(["--port", str(port), "--health-only", "--log", str(self.log)])
        self.assertEqual(code, 1)
        self.assertIn("health_timeout", out.getvalue())
        self.assertIn("서버 종료", out.getvalue())


class KitRequestChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.kit = self.project / "check.py"
        self.kit.touch()
        self.report = self.project / "results" / "check.json"
        self.report.parent.mkdir()
        self.log = self.project / sac.AUDIT_PATH
        self.audit = RequestAudit(self.project)
        self.addCleanup(self.audit.close)
        self.response = {"answer": "답", "citation": ["제1조"], "source_url": "https://example.invalid",
                         "effective_date": "2026-01-01"}
        self.row = {"id": "S1", "question": "질문", "format_ok": True, "error": None, "sec": 1,
                    "response": self.response}

    def record(self, row, **changes):
        seq = self.audit.begin({}, "127.0.0.1")
        fields = {"question": row["question"], "response": row["response"], "http_status": 200,
                  "rag_status": "completed", "write_outcome": "written", "elapsed_ms": 1}
        self.audit.update(seq, **{**fields, **changes})
        return seq

    def check(self, rows, record_requests, samples=None):
        if samples is None:
            samples = [{"id": row["id"], "question": row["question"]} for row in rows]
        (self.project / "sample_questions.json").write_text(json.dumps(samples), encoding="utf-8")

        def run(command, **kwargs):
            record_requests()
            self.report.write_text(json.dumps(rows), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, f"저장: {self.report}\n", "")

        with patch.object(sac.subprocess, "run", side_effect=run):
            return sac.run_kit_check(sys.executable, self.kit, "http://127.0.0.1:8010/ask", self.log)

    def test_kit_receives_full_endpoint_and_reports_the_http_body(self):
        # The real service formats percentages on the outgoing copy. Its saved job keeps the original.
        runtime = FakeRuntime()
        runtime.release.set()

        def publicizer(audit, project):
            result = full_publicizer(audit, project)
            result["response"]["answer"] = "주차요금의 100분의 50을 감면할 수 있습니다."
            return result

        server = create_server(self.project, port=0, runtime=runtime, publicizer=publicizer,
                               health_provider=health, readiness_provider=ready)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        (self.project / "sample_questions.json").write_text('[{"id": "S1", "question": "할인"}]', encoding="utf-8")
        self.kit.write_text(textwrap.dedent("""
            import json, sys
            from pathlib import Path
            from urllib.request import Request, urlopen

            print(sys.argv[1:])
            request = Request(sys.argv[2], json.dumps({"question": "할인"}).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as reply:
                response = json.load(reply)
            assert response["answer"] == "주차요금의 50%를 감면할 수 있습니다."
            row = {"id": "S1", "question": "할인", "sec": 1, "format_ok": True, "error": None, "response": response}
            path = Path.cwd() / "results" / "check.json"
            path.write_text(json.dumps([row]), encoding="utf-8")
            print("저장:", path)
        """), encoding="utf-8")
        port = server.server_port
        with patch.object(sac, "REQUEST_LOG", self.log), patch.object(sac, "start_server") as start, \
                patch.object(sac, "stop_server"), contextlib.redirect_stdout(io.StringIO()) as output:
            start.return_value.poll.return_value = None
            code = sac.main(["--port", str(port), "--kit-check", str(self.kit), "--log", str(self.project / "serve.log")])
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn(repr(["--url", f"http://127.0.0.1:{port}/ask"]), output.getvalue())

    def test_old_or_missing_requests_cannot_validate_a_report(self):
        self.record(self.row)
        code, _ = self.check([self.row], lambda: None)
        self.assertEqual(code, 1)

    def test_repeated_question_uses_each_http_response_in_order(self):
        other = {**self.row, "id": "S2", "response": {**self.response, "answer": "두 번째 답"}}
        self.record(other)
        def during_run():
            self.record(self.row)
            self.record({**self.row, "question": "다른 요청"})
            self.record(other)
        self.assertEqual(self.check([self.row, other], during_run)[0], 0)
        self.assertEqual(self.check([other, self.row], during_run)[0], 1)
        # An extra request for this question makes attribution ambiguous.
        self.assertEqual(self.check([self.row], during_run)[0], 1)

    def test_unknown_requires_completed_http_delivery(self):
        unknown = {**self.row, "response": {"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""}}
        self.assertEqual(self.check([unknown], lambda: self.record(unknown))[0], 0)
        for fields in ({"rag_status": "processing_error"}, {"rag_status": "deadline_exceeded"},
                       {"http_status": 500}, {"write_outcome": "disconnected"}):
            with self.subTest(fields=fields):
                code, _ = self.check([unknown], lambda: self.record(unknown, **fields))
                self.assertEqual(code, 1)

    def test_kit_report_format_time_and_case_identity_are_checked(self):
        samples = [{"id": "S1", "question": "질문"}]
        for fields in ({"response": {}}, {"error": "timeout"}, {"format_ok": False}, {"sec": 91},
                       {"id": "S2"}, {"question": "다른 질문"}, {"response": {**self.response, "extra": 1}}):
            with self.subTest(fields=fields):
                code, _ = self.check([{**self.row, **fields}], lambda: self.record(self.row), samples)
                self.assertEqual(code, 1)

    def test_all_four_fields_match_the_wire_response(self):
        for key, changed in (("answer", "다른 답"), ("citation", ["제2조"]),
                             ("source_url", "https://other.invalid"), ("effective_date", "2025-01-01")):
            row = {**self.row, "response": {**self.response, key: changed}}
            with self.subTest(key=key):
                code, output = self.check([row], lambda: self.record(self.row))
                self.assertEqual(code, 1)
                self.assertIn("http_response_mismatch", output)

    def test_waits_for_pending_http_log_to_finish(self):
        seq = []
        def during_run():
            seq.append(self.record(self.row, write_outcome="pending"))
        def finish_request(_seconds):
            self.audit.update(seq[0], write_outcome="written")
        with patch.object(sac.time, "sleep", side_effect=finish_request):
            self.assertEqual(self.check([self.row], during_run)[0], 0)


if __name__ == "__main__":
    unittest.main()
