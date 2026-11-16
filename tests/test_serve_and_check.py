"""serve_and_check: start → /health → checks → stop, using a stand-in server process. No API, no DB."""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import serve_and_check as sac  # noqa: E402

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

    def _job(self, folder, question, status="completed", result=None, name="a", response=None):
        """Write one service job record (the shape Jobs._save writes) and return its path."""
        folder.mkdir(parents=True, exist_ok=True)
        identity = name * 64
        if result is None:
            result = {"status": "verified_draft",
                      "response": {"answer": "답", "citation": []} if response is None else response}
        record = {"job_id": identity, "run_identity": identity, "question": question, "status": status,
                  "cached": False, "result": result}
        path = folder / (identity + ".json")
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return path

    def test_kit_receives_the_full_ask_endpoint(self):
        port = free_port()
        folder = Path(self.tmp.name)
        kit = folder / "check.py"
        (folder / "sample_questions.json").write_text('[{"id":"S1"}]', encoding="utf-8")
        kit.write_text(textwrap.dedent("""
            import json, sys
            from pathlib import Path
            print(sys.argv[1:])
            path = Path.cwd() / "results" / "check.json"
            path.parent.mkdir()
            path.write_text(json.dumps([{"id": "S1", "question": "질문 하나", "format_ok": True, "sec": 1,
                "error": None, "response": {"answer": "답", "citation": ["제1조"],
                "source_url": "https://example.invalid", "effective_date": "2026-01-01"}, "score": 0}]),
                encoding="utf-8")
            print("저장:", path)
        """), encoding="utf-8")
        jobs = folder / "service_jobs"
        self._job(jobs, "질문 하나", response={"answer": "답", "citation": ["제1조"],
                                              "source_url": ["https://example.invalid"]})
        with patch.object(sac, "JOB_FOLDER", jobs):
            code, out = self._run("good", ["--port", str(port), "--kit-check", str(kit)])
        self.assertEqual(code, 0, out)
        self.assertIn(repr(["--url", f"http://127.0.0.1:{port}/ask"]), out)
        self.assertIn("[PASS] 제출 응답 형식·시간 1/1", out)

    def test_kit_zero_exit_with_bad_rows_fails(self):
        folder = Path(self.tmp.name)
        kit = folder / "check.py"
        kit.touch()
        (folder / "sample_questions.json").write_text('[{"id":"S1"}]', encoding="utf-8")
        report = folder / "results" / "check.json"
        report.parent.mkdir()
        jobs = folder / "service_jobs"
        self._job(jobs, "질문")
        result = subprocess.CompletedProcess([], 0, f"저장: {report}\n", "")
        row = {"id": "S1", "question": "질문", "format_ok": True, "sec": 1, "error": None,
               "response": {"answer": "답", "citation": [], "source_url": "", "effective_date": ""}}
        with patch.object(sac.subprocess, "run", return_value=result):
            report.write_text(json.dumps([row]), encoding="utf-8")
            code, out = sac.run_kit_check(sys.executable, kit, "http://127.0.0.1:8010/ask", jobs_folder=jobs)
            self.assertEqual(code, 0, out)
        for updates in ({"error": "timeout"}, {"format_ok": False}, {"sec": 91}, {"id": "S2"},
                        {"response": {**row["response"], "status": "completed"}},
                        {"response": {"answer": "답", "citation": []}}, {"response": {}},
                        {"question": "기록 없는 질문"}):
            with self.subTest(updates=updates), patch.object(sac.subprocess, "run", return_value=result):
                report.write_text(json.dumps([{**row, **updates}]), encoding="utf-8")
                code, out = sac.run_kit_check(sys.executable, kit, "http://127.0.0.1:8010/ask", jobs_folder=jobs)
                self.assertNotEqual(code, 0, out)

    def test_kit_check_reads_the_server_job_record_outcome(self):
        folder = Path(self.tmp.name)
        kit = folder / "check.py"
        kit.touch()
        (folder / "sample_questions.json").write_text('[{"id":"S1"}]', encoding="utf-8")
        report = folder / "results" / "check.json"
        report.parent.mkdir()
        result = subprocess.CompletedProcess([], 0, f"저장: {report}\n", "")
        unknown = {"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""}
        report.write_text(json.dumps([{"id": "S1", "question": " 질문 ", "format_ok": True, "sec": 1,
                                       "error": None, "response": unknown}]), encoding="utf-8")
        cases = ((("running", None), "S1(job_running)"),
                 (("failed", {"status": "processing_error"}), "S1(job_failed)"),
                 (("interrupted", None), "S1(job_interrupted)"),
                 (("completed", {"status": "processing_error"}), "S1(job_processing_error)"),
                 (None, "S1(job_record_missing)"),
                 (("completed", {"status": "insufficient_evidence"}), "S1(job_record_mismatch)"),
                 (("completed", {"status": "insufficient_evidence",
                                 "response": {"answer": "확인 불가", "citation": []}}), None))
        for index, (job, expected) in enumerate(cases):
            jobs = folder / ("jobs%d" % index)
            jobs.mkdir()
            if job is not None:
                self._job(jobs, "질문", *job)
            with self.subTest(job=job), patch.object(sac.subprocess, "run", return_value=result):
                code, out = sac.run_kit_check(sys.executable, kit, "http://127.0.0.1:8010/ask", jobs_folder=jobs)
                if expected is None:
                    self.assertEqual(code, 0, out)
                else:
                    self.assertEqual(code, 1, out)
                    self.assertIn(expected, out)

    def test_kit_row_must_match_the_job_record_response(self):
        folder = Path(self.tmp.name)
        kit = folder / "check.py"
        kit.touch()
        (folder / "sample_questions.json").write_text('[{"id":"S1"}]', encoding="utf-8")
        report = folder / "results" / "check.json"
        report.parent.mkdir()
        result = subprocess.CompletedProcess([], 0, f"저장: {report}\n", "")
        report.write_text(json.dumps([{"id": "S1", "question": "질문", "format_ok": True, "sec": 1, "error": None,
                                       "response": {"answer": "답", "citation": ["제1조"],
                                                    "source_url": "https://example.invalid/1",
                                                    "effective_date": "2026-01-01"}}]), encoding="utf-8")
        cases = (({"answer": "답", "citation": ["제1조"]}, None),
                 ({"answer": "다른 답", "citation": ["제1조"]}, "S1(job_record_mismatch)"),
                 ({"answer": "답", "citation": ["제2조"]}, "S1(job_record_mismatch)"),
                 ({"answer": "답"}, "S1(job_record_mismatch)"))
        for index, (response, expected) in enumerate(cases):
            jobs = folder / ("match%d" % index)
            self._job(jobs, "질문", response=response)
            with self.subTest(response=response), patch.object(sac.subprocess, "run", return_value=result):
                code, out = sac.run_kit_check(sys.executable, kit, "http://127.0.0.1:8010/ask", jobs_folder=jobs)
                if expected is None:
                    self.assertEqual(code, 0, out)
                else:
                    self.assertEqual(code, 1, out)
                    self.assertIn(expected, out)

    def test_latest_job_record_wins_for_the_same_question(self):
        jobs = Path(self.tmp.name) / "service_jobs"
        old = self._job(jobs, "질문", "failed", {"status": "processing_error"}, name="a")
        new = self._job(jobs, " 질문", "completed", None, name="b")
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        (jobs / "not-a-job.json").write_text('{"question": "질문", "status": "failed"}', encoding="utf-8")
        (jobs / ("c" * 64 + ".json")).write_text("{broken", encoding="utf-8")
        records = sac.latest_job_records(jobs)
        self.assertEqual(records["질문"]["status"], "completed")
        os.utime(old, (3_000_000, 3_000_000))
        self.assertEqual(sac.latest_job_records(jobs)["질문"]["status"], "failed")
        self.assertEqual(sac.latest_job_records(Path(self.tmp.name) / "missing"), {})


if __name__ == "__main__":
    unittest.main()
