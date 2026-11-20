"""POST /kakao/skill(카카오 i 오픈빌더 스킬) 시험. 가짜 모델을 쓰고 외부 API·카카오에는 연결하지 않습니다."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

import kakao_skill as ks
from local_rag_service import create_server


def ready():
    return []


def health():
    return {"as_of_date": "2026-10-01", "identity": "fake-db", "dimension": 1024, "summary": {}}


class Runtime:
    """질문에 "느린"이 들어 있으면 release가 설정될 때까지 기다립니다."""

    def __init__(self):
        self.release = threading.Event()
        self.paid = []

    def __call__(self, project, question, check_only=False, *, attempt_id=None):
        identity = hashlib.sha256((question + (attempt_id or "")).encode()).hexdigest()
        if check_only:
            return {"status": "check_only", "cached": False, "run_identity": identity}
        self.paid.append(question)
        if "느린" in question:
            self.release.wait(10)
        return {"status": "completed", "run_identity": identity, "question": question}


def publicizer(audit, project):
    if "헬멧" in audit.get("question", ""):
        return {"status": "out_of_scope", "message": "범위 밖입니다.", "as_of_date": "2026-10-01",
                "response": {"answer": "확인 불가", "citation": []}}
    return {"status": "verified_draft", "as_of_date": "2026-10-01",
            "response": {"answer": "청년은 19세 이상 39세 이하입니다.",
                         "citation": ["서울특별시 청년 기본 조례 제3조 제1호", "서울특별시 청년 기본 조례 제24조"],
                         "source_url": ["https://www.law.go.kr/a", "https://www.law.go.kr/b"],
                         "effective_date": ["2026-10-01", "2026-10-01"]},
            "sources": [{"citation": "서울특별시 청년 기본 조례 제3조 제1호", "source_url": "https://www.law.go.kr/a",
                         "effective_date": "2026-10-01"},
                        {"citation": "서울특별시 청년 기본 조례 제24조", "source_url": "https://www.law.go.kr/b",
                         "effective_date": "2026-10-01"}]}


def payload(utterance, user="u1", callback=None):
    request = {"utterance": utterance, "user": {"id": user, "type": "botUserKey"}}
    if callback:
        request["callbackUrl"] = callback
    return {"intent": {"id": "fallback", "name": "폴백 블록"}, "userRequest": request, "bot": {"id": "b"}}


class KakaoFormatTests(unittest.TestCase):
    def test_answer_card_respects_kakao_limits(self):
        view = {"status": "completed", "job_id": "x", "answer": "답", "citation": ["서울특별시 청년 기본 조례 제3조"],
                "sources": [{"citation": "서울특별시 청년 기본 조례 제3조", "source_url": "https://www.law.go.kr/a"}]}
        out = ks.render(view, ks.classify(view, 200))
        self.assertEqual(out["version"], "2.0")
        self.assertLessEqual(len(out["template"]["outputs"]), ks.OUTPUTS_MAX)
        card = out["template"]["outputs"][-1]["textCard"]
        self.assertTrue(all(len(b["label"]) <= ks.BUTTON_LABEL_MAX for b in card["buttons"]))
        self.assertLessEqual(len(out["template"]["quickReplies"]), ks.QUICK_REPLIES_MAX)

    def test_long_answer_is_split_not_rewritten(self):
        long = ("가" * 600 + "\n") * 3
        view = {"status": "completed", "job_id": "x", "answer": long, "citation": ["조례 제1조"]}
        outputs = ks.render(view, ks.classify(view, 200))["template"]["outputs"]
        self.assertLessEqual(len(outputs), ks.OUTPUTS_MAX)
        self.assertTrue(all(len(o["simpleText"]["text"]) <= ks.SIMPLE_TEXT_MAX for o in outputs if "simpleText" in o))

    def test_callback_host_check(self):
        hosts = ks.DEFAULT_CALLBACK_HOSTS
        self.assertTrue(ks.callback_allowed("https://bot-api.kakao.com/callback/x", hosts))
        self.assertFalse(ks.callback_allowed("http://bot-api.kakao.com/x", hosts))
        self.assertFalse(ks.callback_allowed("https://evilkakao.com/x", hosts))
        self.assertFalse(ks.callback_allowed("https://127.0.0.1/x", hosts))

    def test_waiting_response_has_no_template(self):
        body = ks.callback_waiting()
        self.assertTrue(body["useCallback"])
        self.assertNotIn("template", body)


class KakaoSkillServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="kakao-test-")
        self.runtime = Runtime()
        self.server = create_server(Path(self.temp.name), 0, runtime=self.runtime, publicizer=publicizer,
                                    health_provider=health, readiness_provider=ready, workers=2, queue_size=2)
        self.server.kakao.sync_wait = 0.5
        self.posted = []
        self.server.kakao.poster = lambda url, body: self.posted.append((url, body))
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.runtime.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def post(self, value=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        data = json.dumps(value, ensure_ascii=False).encode() if value is not None else body
        started = time.monotonic()
        connection.request("POST", ks.KAKAO_SKILL_PATH, body=data, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), time.monotonic() - started
        connection.close()
        return result

    def test_welcome_does_not_call_model(self):
        status, body, _ = self.post(payload("처음으로"))
        self.assertEqual(status, 200)
        self.assertIn("로디", body["template"]["outputs"][0]["simpleText"]["text"])
        self.assertEqual(self.runtime.paid, [])

    def test_fast_answer_is_returned_directly_with_sources(self):
        status, body, elapsed = self.post(payload("청년 나이 알려줘"))
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 5)
        outputs = body["template"]["outputs"]
        self.assertIn("19세", outputs[0]["simpleText"]["text"])
        self.assertEqual([b["webLinkUrl"] for b in outputs[-1]["textCard"]["buttons"]],
                         ["https://www.law.go.kr/a", "https://www.law.go.kr/b"])

    def test_out_of_scope_shows_inquiry_button(self):
        _, body, _ = self.post(payload("헬멧 안 쓰면 과태료?"))
        card = body["template"]["outputs"][0]["textCard"]
        self.assertEqual(card["title"], "확인 불가")
        self.assertEqual(card["buttons"][0]["webLinkUrl"], "https://www.epeople.go.kr/")

    def test_slow_answer_without_callback_uses_check_button(self):
        status, body, elapsed = self.post(payload("느린 질문", user="slow"))
        self.assertLess(elapsed, 5)
        self.assertEqual(body["template"]["quickReplies"][0]["messageText"], "결과 확인")
        _, body, _ = self.post(payload("결과 확인", user="slow"))
        self.assertIn("아직", body["template"]["outputs"][0]["simpleText"]["text"])
        self.runtime.release.set()
        for _ in range(50):
            _, body, _ = self.post(payload("결과 확인", user="slow"))
            if "textCard" in body["template"]["outputs"][-1]:
                break
            time.sleep(0.1)
        self.assertIn("textCard", body["template"]["outputs"][-1])
        self.assertEqual(self.runtime.paid, ["느린 질문"])

    def test_callback_flow_posts_final_answer_once(self):
        callback = "https://bot-api.kakao.com/callback/abc"
        status, body, elapsed = self.post(payload("느린 콜백 질문", callback=callback))
        self.assertTrue(body["useCallback"])
        self.assertLess(elapsed, 5)
        self.runtime.release.set()
        for _ in range(50):
            if self.posted:
                break
            time.sleep(0.1)
        self.assertEqual(len(self.posted), 1)
        url, final = self.posted[0]
        self.assertEqual(url, callback)
        self.assertIn("textCard", final["template"]["outputs"][-1])

    def test_callback_to_other_host_is_not_used(self):
        _, body, _ = self.post(payload("청년 나이", callback="https://attacker.example/x"))
        self.assertNotIn("useCallback", body)
        self.assertEqual(self.posted, [])

    def test_invalid_body_is_rejected(self):
        status, _, _ = self.post(body=b"{not json")
        self.assertEqual(status, 400)

    def test_ask_routes_are_unchanged(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        connection.request("POST", "/ui/ask", body=json.dumps({"question": "청년 나이"}).encode(),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        view = json.loads(response.read())
        connection.close()
        self.assertEqual((response.status, view["status"]), (200, "completed"))


if __name__ == "__main__":
    unittest.main()
