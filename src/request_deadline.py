"""Windows에서도 제한 시간을 넘긴 요청 작업을 종료하는 프로세스 감독기."""

import json
import math
import multiprocessing
import time
import uuid
from pathlib import Path


class RequestDeadlineExceeded(TimeoutError):
    pass


class RequestWorkerError(RuntimeError):
    pass


def _worker(function, kwargs, result_path):
    path = Path(result_path)
    try:
        envelope = {"ok": True, "value": function(**kwargs)}
    except Exception as exc:
        # 예외 문구에 인증값/요청 내용이 섞일 수 있어 종류만 보존합니다.
        envelope = {"ok": False, "error_type": type(exc).__name__}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(envelope, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def run_with_deadline(function, kwargs, work_dir, seconds=75.0):
    if not math.isfinite(seconds) or not 0 < seconds <= 75:
        raise ValueError("요청 제한 시간은 0초 초과 75초 이하여야 합니다.")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    result_path = work_dir / f"{uuid.uuid4().hex}.json"
    temporary_path = result_path.with_suffix(".tmp")
    process = multiprocessing.get_context("spawn").Process(
        target=_worker, args=(function, kwargs, str(result_path)), daemon=True,
    )
    started = time.monotonic()
    try:
        process.start()
        process.join(max(0.0, seconds - (time.monotonic() - started)))
        if process.is_alive() or time.monotonic() - started >= seconds:
            raise RequestDeadlineExceeded("request_deadline_exceeded")
        if process.exitcode != 0 or not result_path.is_file():
            raise RequestWorkerError("request_worker_failed")
        envelope = json.loads(result_path.read_text(encoding="utf-8"))
        if type(envelope) is not dict or type(envelope.get("ok")) is not bool:
            raise RequestWorkerError("request_worker_invalid_result")
        if not envelope["ok"]:
            raise RequestWorkerError("request_worker_failed")
        if "value" not in envelope:
            raise RequestWorkerError("request_worker_invalid_result")
        return envelope["value"]
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
                process.join(2.0)
            if process.is_alive():
                process.kill()
                process.join(2.0)
            if process.is_alive():
                raise RequestWorkerError("request_worker_termination_failed")
            process.close()
        # 이 요청의 고유한 파일 두 개만 정리합니다. 다른 요청의 캐시는 건드리지 않습니다.
        for path in (temporary_path, result_path):
            if path.exists():
                path.unlink()
