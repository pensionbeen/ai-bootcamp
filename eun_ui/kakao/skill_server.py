"""카카오 챗봇 스킬 서버: 카카오톡 채널 ↔ 서울 조례 도우미 백엔드를 이어 줍니다.

    카카오톡 → 카카오 i 오픈빌더 → (POST /kakao/skill) 이 서버 → (POST /ui/ask) 백엔드

카카오는 스킬 응답을 5초 안에 받아야 하는데, 조례 답변은 보통 20초쯤 걸립니다. 그래서 두 가지 방식을 씁니다.
1) 콜백(권장): 블록에 Callback 설정을 켜면 요청에 callbackUrl이 옵니다. 바로 {"useCallback": true}로
   "찾는 중" 메시지를 보내고, 답이 나오면 callbackUrl로 최종 답을 POST합니다(callbackUrl은 5분 동안 1회 사용).
2) 콜백이 없을 때: 3.5초 안에 답이 나오면 바로 보내고, 아니면 "결과 확인" 버튼을 보냅니다.
   사용자가 누르면 그때까지 나온 답을 보여 줍니다.

외부 패키지 없이 Python 3.10+ 표준 라이브러리만 씁니다. 기존 백엔드 코드는 고치지 않습니다.

실행:
    python eun_ui/kakao/skill_server.py            # 기본: 0.0.0.0:8010, 백엔드 http://127.0.0.1:8000
환경 변수:
    RAG_BASE_URL            백엔드 주소 (기본 http://127.0.0.1:8000)
    KAKAO_SKILL_HOST/PORT   이 서버가 열 주소 (기본 0.0.0.0 / 8010)
    KAKAO_CALLBACK_HOSTS    콜백을 보낼 수 있는 호스트 끝부분, 쉼표로 구분 (기본 kakao.com,kakaoenterprise.com)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kakao_format as kf  # noqa: E402

LOG = logging.getLogger("kakao_skill")

SKILL_PATH = "/kakao/skill"
MAX_BODY = 64 * 1024
SYNC_WAIT_SECONDS = 3.5          # 카카오 5초 제한 안에 응답하도록 여유를 둡니다.
CALLBACK_DEADLINE_SECONDS = 270  # callbackUrl 유효 시간 5분보다 짧게 끝냅니다.
FALLBACK_DEADLINE_SECONDS = 600  # 결과 확인 방식에서 기다리는 최대 시간(화면과 같은 10분)
POLL_SECONDS = 3
ASK_TIMEOUT_SECONDS = 95         # 백엔드 /ui/ask는 최대 80초 안에 답합니다.
PENDING_TTL_SECONDS = 15 * 60


# ---------- 백엔드 호출 ----------

class Backend:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _call(self, method: str, path: str, body: dict | None, timeout: float) -> tuple[dict, int]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urlrequest.Request(self.base_url + path, data=data, method=method, headers={
            # 일부 방화벽(Cloudflare 등)은 기본 "Python-urllib" 요청을 403으로 막습니다.
            "User-Agent": "Mozilla/5.0 (RawddyKakaoRelay/1.0)",
            "Accept": "application/json",
        })
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                status, raw = resp.status, resp.read()
        except urlerror.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if not isinstance(parsed, dict):
            LOG.warning("backend %s %s -> HTTP %s, JSON 아님: %r", method, path, status, raw[:200])
            return {}, status if status >= 400 else 500
        if status >= 400:
            LOG.warning("backend %s %s -> HTTP %s: %s", method, path, status, str(parsed)[:200])
        return parsed, status

    def ask(self, question: str, timeout: float) -> tuple[dict, int]:
        return self._call("POST", "/ui/ask", {"question": question}, timeout)

    def job(self, poll_url: str, timeout: float = 10) -> tuple[dict, int]:
        return self._call("GET", poll_url, None, timeout)


def resolve(backend: Backend, question: str, deadline: float,
            sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic
            ) -> tuple[dict | None, dict]:
    """질문 하나를 끝까지 처리해 (view, info)를 돌려줍니다. 화면의 askQuestion과 같은 흐름입니다."""
    try:
        view, status = backend.ask(question, timeout=max(5.0, min(ASK_TIMEOUT_SECONDS, deadline - now())))
    except (urlerror.URLError, TimeoutError, OSError):
        return None, {"kind": "retry", "text": "답을 만드는 서버에 연결하지 못했어요. 잠시 후 다시 보내 주세요."}
    info = kf.classify(view, status)
    while info["kind"] == "poll":
        if now() + POLL_SECONDS > deadline:
            return view, {"kind": "retry", "text": "답이 너무 늦어지고 있어요. 잠시 후 같은 질문을 다시 보내 주세요."}
        sleep(POLL_SECONDS)
        poll_url = view["poll_url"]
        try:
            body, status = backend.job(poll_url)
        except (urlerror.URLError, TimeoutError, OSError):
            continue  # 잠깐 끊긴 것은 제한 시간까지 계속 확인합니다.
        if body.get("job_id"):
            view = kf.job_to_view(body)
            if view.get("status") == "deadline_exceeded" and not view.get("poll_url"):
                view["poll_url"] = poll_url
        else:
            view = body
            status = status if status >= 400 else 500
        info = kf.classify(view, status)
    return view, info


# ---------- 콜백 ----------

def callback_allowed(url: str | None, suffixes: tuple[str, ...]) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == s or host.endswith("." + s) for s in suffixes)


def post_callback(url: str, body: dict, timeout: float = 10) -> None:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            LOG.info("callback sent: HTTP %s %s", resp.status, resp.read(300).decode("utf-8", "replace"))
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        LOG.warning("callback failed: %s", exc)


# ---------- 콜백 없이 기다리는 질문 ----------

@dataclass
class Pending:
    question: str
    created: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    outcome: tuple[dict | None, dict] | None = None


class PendingStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, Pending] = {}

    def put(self, user_id: str, item: Pending) -> None:
        with self._lock:
            cutoff = time.monotonic() - PENDING_TTL_SECONDS
            self._items = {k: v for k, v in self._items.items() if v.created > cutoff}
            self._items[user_id] = item

    def get(self, user_id: str) -> Pending | None:
        with self._lock:
            return self._items.get(user_id)

    def pop_if(self, user_id: str, item: Pending) -> None:
        with self._lock:
            if self._items.get(user_id) is item:
                del self._items[user_id]


# ---------- HTTP ----------

class SkillServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, backend: Backend, callback_hosts: tuple[str, ...]):
        super().__init__(address, SkillHandler)
        self.backend = backend
        self.callback_hosts = callback_hosts
        self.pending = PendingStore()


class SkillHandler(BaseHTTPRequestHandler):
    server: SkillServer
    server_version = "SeoulOrdinanceKakaoSkill/1.0"

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.client_address[0], fmt % args)

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        self._send(404, {"status": "not_found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != SKILL_PATH:
            return self._send(404, {"status": "not_found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return self._send(413, {"status": "request_too_large"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"status": "invalid_json"})
        try:
            self._send(200, self.handle_skill(payload))
        except Exception:  # 카카오에는 항상 올바른 응답 형식을 돌려줍니다.
            LOG.exception("skill handling failed")
            self._send(200, kf.render(None, {"kind": "retry", "text": "처리 중 오류가 났어요. 잠시 후 다시 보내 주세요."}))

    def handle_skill(self, payload: Any) -> dict:
        utterance, user_id, callback_url = kf.parse_payload(payload)
        if not utterance or utterance in kf.HOME_WORDS:
            return kf.welcome()
        store = self.server.pending
        if utterance in kf.CHECK_WORDS:
            item = store.get(user_id)
            if item is None:
                return kf.nothing_pending()
            if not item.done.is_set():
                return kf.still_pending()
            store.pop_if(user_id, item)
            view, info = item.outcome
            return kf.render(view, info, item.question)

        question = utterance
        backend = self.server.backend

        if callback_allowed(callback_url, self.server.callback_hosts):
            deadline = time.monotonic() + CALLBACK_DEADLINE_SECONDS

            def work():
                view, info = resolve(backend, question, deadline)
                post_callback(callback_url, kf.render(view, info, question))

            threading.Thread(target=work, daemon=True).start()
            return kf.callback_waiting()
        if callback_url:
            LOG.warning("callbackUrl host not allowed, using 결과 확인 mode: %s", urlsplit(callback_url).hostname)

        item = Pending(question=question)
        deadline = item.created + FALLBACK_DEADLINE_SECONDS

        def work_pending():
            item.outcome = resolve(backend, question, deadline)
            item.done.set()

        store.put(user_id, item)
        threading.Thread(target=work_pending, daemon=True).start()
        if item.done.wait(SYNC_WAIT_SECONDS):
            store.pop_if(user_id, item)
            view, info = item.outcome
            return kf.render(view, info, question)
        return kf.pending(question)


def make_server(host: str, port: int, backend_url: str, callback_hosts: tuple[str, ...]) -> SkillServer:
    return SkillServer((host, port), Backend(backend_url), callback_hosts)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("KAKAO_SKILL_HOST", "0.0.0.0")
    port = int(os.environ.get("KAKAO_SKILL_PORT", "8010"))
    backend_url = os.environ.get("RAG_BASE_URL", "http://127.0.0.1:8000")
    hosts = tuple(h.strip().lower() for h in os.environ.get("KAKAO_CALLBACK_HOSTS", "kakao.com,kakaoenterprise.com").split(",") if h.strip())
    server = make_server(host, port, backend_url, hosts)
    LOG.info("kakao skill server on http://%s:%s%s -> backend %s", host, port, SKILL_PATH, backend_url)
    server.serve_forever()


if __name__ == "__main__":
    main()
