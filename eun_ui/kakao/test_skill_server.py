"""카카오 스킬 서버 시험. 실행: python -m unittest eun_ui/kakao/test_skill_server.py

가짜 백엔드와 가짜 카카오 콜백 서버를 띄워 실제 HTTP로 주고받습니다. 모델·네트워크를 쓰지 않습니다.
"""

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlrequest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kakao_format as kf  # noqa: E402
import skill_server as ss  # noqa: E402

JOB = "c" * 64
ANSWER = {
    "answer": "청년은 19세 이상 39세 이하입니다.",
    "citation": ["서울특별시 청년 기본 조례 제3조 제1호", "서울특별시 청년 기본 조례 제24조"],
    "source_url": "https://www.law.go.kr/ordinInfoP.do?ordinSeq=2054497",
    "effective_date": "2026-10-01", "status": "completed", "job_id": JOB, "as_of_date": "2026-10-01",
    "sources": [
        {"citation": "서울특별시 청년 기본 조례 제3조 제1호", "source_url": "https://www.law.go.kr/a", "effective_date": "2026-10-01"},
        {"citation": "서울특별시 청년 기본 조례 제24조", "source_url": "https://www.law.go.kr/b", "effective_date": "2026-10-01"},
    ],
    "result": {"status": "code_checked_draft"},
}
UNKNOWN = {"answer": "확인 불가", "citation": [], "status": "completed", "job_id": JOB,
           "result": {"status": "out_of_scope", "message": "범위 밖입니다.", "inquiry_url": "https://www.epeople.go.kr/"}}


class FakeBackend(BaseHTTPRequestHandler):
    delay = 0.0
    polls = 0

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        q = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["question"]
        time.sleep(FakeBackend.delay)
        if "늦게" in q:
            return self._json(200, {"answer": "확인 불가", "citation": [], "status": "deadline_exceeded", "job_id": JOB, "poll_url": "/jobs/" + JOB})
        self._json(200, UNKNOWN if "헬멧" in q else ANSWER)

    def do_GET(self):
        FakeBackend.polls += 1
        if FakeBackend.polls < 2:
            return self._json(202, {"job_id": JOB, "status": "running", "poll_url": "/jobs/" + JOB})
        self._json(200, {"job_id": JOB, "status": "completed",
                         "result": {"status": "code_checked_draft", "response": {"answer": "늦은 답", "citation": ["서울특별시 청년 기본 조례 제3조"]}}})


class FakeCallback(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        FakeCallback.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        raw = b'{"taskId":"t","status":"SUCCESS"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def skill_payload(utterance, user="u1", callback=None):
    request = {"utterance": utterance, "user": {"id": user, "type": "botUserKey"}}
    if callback:
        request["callbackUrl"] = callback
    return {"userRequest": request}


class FormatTest(unittest.TestCase):
    def test_answer_has_text_and_source_card_within_limits(self):
        out = kf.render(ANSWER, kf.classify(ANSWER, 200))
        self.assertEqual(out["version"], "2.0")
        outputs = out["template"]["outputs"]
        self.assertEqual(outputs[0]["simpleText"]["text"], ANSWER["answer"])
        card = outputs[-1]["textCard"]
        self.assertIn("제3조 제1호", card["description"])
        self.assertEqual([b["webLinkUrl"] for b in card["buttons"]], ["https://www.law.go.kr/a", "https://www.law.go.kr/b"])
        self.assertTrue(all(len(b["label"]) <= kf.BUTTON_LABEL_MAX for b in card["buttons"]))
        self.assertLessEqual(len(out["template"]["quickReplies"]), kf.QUICK_REPLIES_MAX)

    def test_long_answer_is_split_not_rewritten(self):
        long = ("가" * 600 + "\n") * 3
        view = dict(ANSWER, answer=long)
        outputs = kf.render(view, kf.classify(view, 200))["template"]["outputs"]
        self.assertLessEqual(len(outputs), kf.OUTPUTS_MAX)
        self.assertTrue(all(len(o["simpleText"]["text"]) <= kf.SIMPLE_TEXT_MAX for o in outputs if "simpleText" in o))

    def test_unknown_shows_inquiry_button_only_when_given(self):
        card = kf.render(UNKNOWN, kf.classify(UNKNOWN, 200))["template"]["outputs"][0]["textCard"]
        self.assertEqual(card["title"], "확인 불가")
        self.assertEqual(card["buttons"][0]["webLinkUrl"], "https://www.epeople.go.kr/")
        no_link = dict(UNKNOWN, result={"status": "out_of_scope"})
        self.assertNotIn("buttons", kf.render(no_link, kf.classify(no_link, 200))["template"]["outputs"][0]["textCard"])

    def test_retry_is_not_unknown(self):
        view = {"status": "queue_full", "answer": "확인 불가", "citation": []}
        out = kf.render(view, kf.classify(view, 200), "질문")
        self.assertIn("simpleText", out["template"]["outputs"][0])
        self.assertEqual(out["template"]["quickReplies"][0]["messageText"], "질문")

    def test_callback_waiting_has_no_template(self):
        body = kf.callback_waiting()
        self.assertTrue(body["useCallback"])
        self.assertNotIn("template", body)

    def test_callback_host_check(self):
        hosts = ("kakao.com",)
        self.assertTrue(ss.callback_allowed("https://bot-api.kakao.com/callback/x", hosts))
        self.assertFalse(ss.callback_allowed("http://bot-api.kakao.com/x", hosts))
        self.assertFalse(ss.callback_allowed("https://evilkakao.com/x", hosts))
        self.assertFalse(ss.callback_allowed(None, hosts))


class ServerTest(unittest.TestCase):
    def setUp(self):
        FakeBackend.delay = 0.0
        FakeBackend.polls = 0
        FakeCallback.received = []
        self.backend = serve(FakeBackend)
        self.callback = serve(FakeCallback)
        # 시험에서는 가짜 콜백 서버(127.0.0.1)를 허용하고, https 검사는 함수 시험에서 따로 봅니다.
        self.skill = ss.make_server("127.0.0.1", 0, f"http://127.0.0.1:{self.backend.server_port}", ("kakao.com",))
        threading.Thread(target=self.skill.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.skill.server_port}{ss.SKILL_PATH}"

    def tearDown(self):
        for server in (self.skill, self.backend, self.callback):
            server.shutdown()
            server.server_close()

    def post(self, payload):
        req = urlrequest.Request(self.url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
        started = time.monotonic()
        with urlrequest.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()), time.monotonic() - started

    def test_welcome(self):
        body, _ = self.post(skill_payload("처음으로"))
        self.assertIn("서울시 조례 도우미", body["template"]["outputs"][0]["simpleText"]["text"])

    def test_fast_answer_returns_directly(self):
        body, elapsed = self.post(skill_payload("청년 나이 알려줘"))
        self.assertLess(elapsed, 5)
        self.assertIn("textCard", body["template"]["outputs"][-1])

    def test_slow_answer_without_callback_uses_check_button(self):
        FakeBackend.delay = 4.5
        body, elapsed = self.post(skill_payload("청년 나이 알려줘", user="slow"))
        self.assertLess(elapsed, 5)
        self.assertEqual(body["template"]["quickReplies"][0]["messageText"], "결과 확인")
        body, _ = self.post(skill_payload("결과 확인", user="slow"))
        self.assertIn("아직", body["template"]["outputs"][0]["simpleText"]["text"])
        time.sleep(1.5)
        body, _ = self.post(skill_payload("결과 확인", user="slow"))
        self.assertIn("textCard", body["template"]["outputs"][-1])
        body, _ = self.post(skill_payload("결과 확인", user="slow"))
        self.assertIn("없어요", body["template"]["outputs"][0]["simpleText"]["text"])

    def test_callback_flow_sends_final_answer(self):
        original = ss.callback_allowed
        ss.callback_allowed = lambda url, hosts: bool(url)
        try:
            cb = f"http://127.0.0.1:{self.callback.server_port}/cb"
            body, elapsed = self.post(skill_payload("헬멧 안 쓰면?", callback=cb))
            self.assertTrue(body["useCallback"])
            self.assertLess(elapsed, 5)
            for _ in range(50):
                if FakeCallback.received:
                    break
                time.sleep(0.1)
            card = FakeCallback.received[0]["template"]["outputs"][0]["textCard"]
            self.assertEqual(card["title"], "확인 불가")
        finally:
            ss.callback_allowed = original

    def test_poll_until_done(self):
        backend = ss.Backend(f"http://127.0.0.1:{self.backend.server_port}")
        view, info = ss.resolve(backend, "늦게 답하는 질문", time.monotonic() + 60, sleep=lambda s: None)
        self.assertEqual(info["kind"], "answer")
        self.assertEqual(view["answer"], "늦은 답")


if __name__ == "__main__":
    unittest.main()
