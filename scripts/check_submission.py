"""Grader-style self-check: POST <url>/ask with {"question": ...} only and validate the reply shape.

Mimics the starter-kit grader as described by the team: JSON body with only `question`,
`Content-Type: application/json; charset=utf-8`, 90-second timeout, and expects HTTP 200 with a
JSON object holding exactly the starter-kit keys `answer` (non-empty string), `citation` (list of
strings), `source_url` and `effective_date` (strings). The kit requires a scalar source URL and
YYYY-MM-DD date for a published answer ("" is allowed only with "확인 불가" and an empty citation).
The outcome is read from the `X-Rag-Status` response header, not the body: a reply whose header is
deadline_exceeded/failed/interrupted/processing_error/queue_full is a FAIL even though its shape is valid,
because the grader would only see "확인 불가".

Usage:
  python3 scripts/check_submission.py --url http://127.0.0.1:8000 --question "질문" [--question ...]
  python3 scripts/check_submission.py --questions-file questions.txt [--host-header grader.internal:8000]
Exit code 1 if any question FAILs.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from submission_http import STATUS_HEADER, TIMEOUT_SECONDS, submission_request

BAD_STATUSES = ("deadline_exceeded", "failed", "interrupted", "processing_error", "queue_full")
KIT_FIELDS = ("answer", "citation", "source_url", "effective_date")


def kit_source_format(value):
    if value["answer"].startswith("확인 불가") and value["citation"] == []:
        return True
    url = value.get("source_url")
    date = value.get("effective_date")
    return (isinstance(url, str) and url.startswith("http")
            and isinstance(date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) is not None)


def check_question(url, question, *, timeout=TIMEOUT_SECONDS, host_header=None):
    """Send one grader-style request and return a verdict dict (never raises for HTTP/network errors)."""
    request = submission_request(url, question, host_header=host_header)
    started = time.monotonic()
    verdict = {"question": question, "status_code": None, "status": None, "elapsed": None, "ok": False, "reason": ""}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code, raw, outcome = response.status, response.read(), response.headers.get(STATUS_HEADER)
    except urllib.error.HTTPError as error:
        code, raw = error.code, error.read()
        outcome = error.headers.get(STATUS_HEADER) if error.headers is not None else None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        verdict.update(elapsed=round(time.monotonic() - started, 3), reason=f"request_error: {error}")
        return verdict
    verdict.update(elapsed=round(time.monotonic() - started, 3), status_code=code)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        value = None
    if isinstance(outcome, str) and outcome:
        verdict["status"] = outcome
    if code != 200:
        verdict["reason"] = f"http_{code}" + (f" ({verdict['status']})" if verdict["status"] else "")
    elif not isinstance(value, dict):
        verdict["reason"] = "body_is_not_json_object"
    elif any(field not in value for field in KIT_FIELDS):
        verdict["reason"] = "missing_fields: " + ",".join(field for field in KIT_FIELDS if field not in value)
    elif set(value) != set(KIT_FIELDS):
        verdict["reason"] = "unexpected_fields: " + ",".join(sorted(set(value) - set(KIT_FIELDS)))
    elif not isinstance(value.get("answer"), str) or not value["answer"].strip():
        verdict["reason"] = "missing_or_empty_answer"
    elif not isinstance(value.get("citation"), list) or not all(isinstance(c, str) for c in value["citation"]):
        verdict["reason"] = "citation_is_not_list_of_str"
    elif not isinstance(value["source_url"], str) or not isinstance(value["effective_date"], str):
        verdict["reason"] = "source_url_or_effective_date_is_not_str"
    elif verdict["status"] in BAD_STATUSES:
        verdict["reason"] = verdict["status"]
    elif not kit_source_format(value):
        verdict["reason"] = "invalid_source_url_or_effective_date"
    else:
        verdict.update(ok=True, reason="ok")
        verdict["answer"], verdict["citation"] = value["answer"], value["citation"]
    return verdict


def _questions(args):
    questions = list(args.question or [])
    if args.questions_file:
        with open(args.questions_file, encoding="utf-8") as handle:
            questions += [line.strip() for line in handle if line.strip()]
    return questions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--question", action="append", help="repeatable")
    parser.add_argument("--questions-file", help="UTF-8, one question per line")
    parser.add_argument("--host-header", help="override Host header to simulate a foreign host")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    questions = _questions(args)
    if not questions:
        parser.error("give --question or --questions-file")
    failed = 0
    for index, question in enumerate(questions, 1):
        v = check_question(args.url, question, timeout=args.timeout, host_header=args.host_header)
        failed += not v["ok"]
        print(f"[{index}] elapsed={v['elapsed']}s http={v['status_code']} status={v['status']}")
        print(f"    {'PASS' if v['ok'] else 'FAIL'}: {v['reason']} | {question[:80]}")
        if v["ok"]:
            print(f"    answer={v['answer'][:120]!r} citation={v['citation']}")
    print(f"{len(questions) - failed}/{len(questions)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
