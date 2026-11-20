"""Start the local /ask server, wait until /health answers, run checks, then stop the server.

One finite command when you cannot keep a long-lived server open:

  python scripts/serve_and_check.py --question "질문"                 # grader-style self-check
  python scripts/serve_and_check.py --questions-file q.txt --port 8010
  python scripts/serve_and_check.py --question "질문" --kit-check "C:/.../starter-kit/check.py"
  python scripts/serve_and_check.py --health-only                      # no model call, no cost

Each new question can trigger multiple paid Solar calls. Repeating it after completion starts
a fresh answer. The server is stopped when the checks finish or fail.

The starter kit saves the response body but drops its HTTP status and headers. --kit-check
compares its rows with this run's HTTP request log, including the first public response and
X-Rag-Status. Repeated questions are paired in the order the kit submits them.
"""
import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_submission import KIT_FIELDS, TIMEOUT_SECONDS, check_question  # noqa: E402
from request_audit import AUDIT_PATH  # noqa: E402

HEALTH_TIMEOUT = 60
STOP_TIMEOUT = 15
REQUEST_LOG = ROOT / AUDIT_PATH


def request_log_position(path):
    if not path.exists():  # The server creates the log on its first /ask.
        return 0
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT COALESCE(MAX(seq), 0) FROM requests").fetchone()[0]


def kit_request_records(path, after, questions):
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        deadline = time.monotonic() + 1
        while True:
            new = db.execute("SELECT payload FROM requests WHERE seq > ? ORDER BY seq", (after,))
            records = []
            for (payload,) in new:
                record = json.loads(payload)
                if record.get("record_kind") == "request" and record.get("question") in questions:
                    records.append(record)
            # The HTTP handler records the outcome immediately after writing the body.
            if not any(r.get("write_outcome") == "pending" for r in records) or time.monotonic() >= deadline:
                return records
            time.sleep(0.05)


def wait_for_health(url, timeout=None, process=None):
    """Poll GET <url>/health until HTTP 200 JSON. Returns the health body or raises RuntimeError."""
    timeout = HEALTH_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"server_exited_early (code {process.returncode})")
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
                last = f"http_{response.status}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            last = str(error)
        time.sleep(0.5)
    raise RuntimeError(f"health_timeout after {timeout}s: {last}")


def start_server(python, port, host="127.0.0.1", log_path=None):
    """Spawn `run.py serve` as a child process; stdout/stderr go to log_path (or are inherited)."""
    command = [python, "-B", str(ROOT / "run.py"), "serve", "--port", str(port), "--host", host]
    log = open(log_path, "ab") if log_path else None
    kwargs = {"cwd": str(ROOT), "stdout": log, "stderr": subprocess.STDOUT if log else None}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(command, **kwargs)
    process._serve_log = log
    return process


def stop_server(process, timeout=STOP_TIMEOUT):
    """Terminate the child politely, then forcefully; always closes the log handle."""
    try:
        if process.poll() is None:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
            try:
                process.wait(timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout)
    finally:
        log = getattr(process, "_serve_log", None)
        if log:
            log.close()
    return process.returncode


def run_kit_check(python, kit_check, url, request_log=None):
    """Run the unmodified kit and verify its report against the actual HTTP responses."""
    kit_check = Path(kit_check).resolve(strict=True)
    request_log = REQUEST_LOG if request_log is None else Path(request_log)
    samples = json.loads((kit_check.parent / "sample_questions.json").read_text(encoding="utf-8"))
    command = [python, "-B", "-X", "utf8", str(kit_check), "--url", url]
    after = request_log_position(request_log)
    completed = subprocess.run(command, cwd=kit_check.parent, capture_output=True, text=True,
                               encoding="utf-8", timeout=TIMEOUT_SECONDS * len(samples) + 60)
    output = completed.stdout + completed.stderr
    if completed.returncode:
        return completed.returncode, output
    # check.py can exit 0 with HTTP errors or malformed answers in its report.
    _, marker, filename = completed.stdout.rstrip().rpartition("저장: ")
    report = Path(filename).resolve()
    if not marker or report.parent != kit_check.parent / "results" or not report.is_file():
        return 1, output + "\n[FAIL] 킷 결과 파일을 찾을 수 없습니다."
    rows = json.loads(report.read_text(encoding="utf-8"))
    if not samples or [(r.get("id"), r.get("question")) for r in rows] != [(s["id"], s["question"]) for s in samples]:
        return 1, output + "\n[FAIL] 키트 결과의 문항 ID·질문·순서가 연습 문항과 다릅니다."
    questions = [row["question"] for row in rows]
    records = kit_request_records(request_log, after, set(questions))
    if [r.get("question") for r in records] != questions:
        return 1, output + "\n[FAIL] 이번 실행의 /ask 요청 횟수 또는 순서가 키트의 질문 목록과 다릅니다."
    failed = []
    for row, record in zip(rows, records, strict=True):
        response = row["response"]
        if (row["error"] is not None or not row["format_ok"] or row["sec"] > TIMEOUT_SECONDS
                or not isinstance(response, dict) or set(response) != set(KIT_FIELDS)
                or not isinstance(response.get("answer"), str)
                or not response["answer"].strip() or not isinstance(response.get("citation"), list)
                or not all(isinstance(cite, str) for cite in response["citation"])):
            failed.append(f"{row['id']}(response)")
            continue
        if record.get("write_outcome") != "written" or record.get("http_status") != 200:
            failed.append(f"{row['id']}(http_delivery)")
        elif record.get("rag_status") != "completed":
            failed.append(f"{row['id']}({record.get('rag_status')})")
        elif record.get("response") != response:
            failed.append(f"{row['id']}(http_response_mismatch)")
    if failed:
        return 1, output + "\n[FAIL] 제출 응답 검사 실패 문항 " + ", ".join(failed)
    return 0, output + f"\n[PASS] 제출 응답 형식·시간 {len(rows)}/{len(samples)}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default=sys.executable, help="interpreter for the server (default: this one)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--question", action="append", default=[], help="repeatable; each fresh answer may use multiple paid calls")
    parser.add_argument("--questions-file", help="UTF-8 file, one question per line")
    parser.add_argument("--host-header", help="simulate a foreign Host header (needs --host 0.0.0.0)")
    parser.add_argument("--kit-check", help="path to starter-kit check.py; runs `check.py --url <url>` after self-checks")
    parser.add_argument("--health-only", action="store_true", help="start, confirm /health, stop; no /ask calls")
    parser.add_argument("--log", default=str(ROOT / "serve_and_check.log"),
                        help="server stdout/stderr log (appended)")
    parser.add_argument("--timeout", type=int, default=90, help="per-question timeout (grader uses 90)")
    args = parser.parse_args(argv)

    questions = list(args.question)
    if args.questions_file:
        questions += [line.strip() for line in Path(args.questions_file).read_text(encoding="utf-8").splitlines()
                      if line.strip()]
    if args.health_only and (questions or args.kit_check):
        parser.error("--health-only cannot be combined with questions or --kit-check")
    if not questions and not args.health_only and not args.kit_check:
        parser.error("give --question/--questions-file, --kit-check, or --health-only")

    url = f"http://127.0.0.1:{args.port}"
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    print(f"서버 시작: {args.python} run.py serve --port {args.port} --host {args.host} (로그 {args.log})", flush=True)
    process = start_server(args.python, args.port, args.host, args.log)
    failures = 0
    try:
        started = time.monotonic()
        health = wait_for_health(url, process=process)
        print(f"/health OK ({time.monotonic() - started:.1f}s) db={str(health.get('database', {}).get('identity', ''))[:12]} "
              f"sync_wait={health.get('sync_wait_seconds')}", flush=True)
        for question in questions:
            verdict = check_question(url, question, timeout=args.timeout, host_header=args.host_header)
            mark = "PASS" if verdict["ok"] else "FAIL"
            print(f"[{mark}] {verdict['elapsed']}s http={verdict['status_code']} status={verdict['status']} "
                  f"reason={verdict['reason']} :: {question[:60]}", flush=True)
            failures += 0 if verdict["ok"] else 1
        if args.kit_check and not failures:
            endpoint = url + "/ask"
            code, output = run_kit_check(args.python, args.kit_check, endpoint)
            print(f"--- starter-kit check.py --url {endpoint} (exit {code}) ---\n{output.strip()}\n---", flush=True)
            failures += 0 if code == 0 else 1
    except Exception as error:  # report, then still stop the server
        print(f"[FAIL] {type(error).__name__}: {error}", flush=True)
        failures += 1
    finally:
        code = stop_server(process)
        print(f"서버 종료 (exit {code})", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
