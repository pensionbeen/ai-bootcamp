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

    def _answer_texts(self, answer):
        view = {"status": "completed", "job_id": "x", "answer": answer, "citation": ["조례 제1조"]}
        outputs = ks.render(view, ks.classify(view, 200))["template"]["outputs"]
        self.assertLessEqual(len(outputs), ks.OUTPUTS_MAX)
        self.assertIn("textCard", outputs[-1])  # 근거 카드는 항상 마지막에 남습니다
        texts = [o["simpleText"]["text"] for o in outputs if "simpleText" in o]
        self.assertTrue(all(len(t) <= ks.SIMPLE_TEXT_MAX for t in texts))
        return texts

    def test_answer_that_fits_two_bubbles_keeps_every_character(self):
        # 리뷰 사례: 600자 문단 + 긴 문단 + 마지막 예외 조건, 합계 1777자 안팎
        sentence = "조례 제5조에 따라 공영주차장 요금은 별표 1의 기준을 따릅니다. "
        answer = (sentence * 20)[:600] + "\n" + (sentence * 40)[:1150] + "\n다만, 장애인 차량은 요금의 50퍼센트를 감면합니다."
        self.assertLessEqual(len(answer), 2 * ks.SIMPLE_TEXT_MAX)
        texts = self._answer_texts(answer)
        squash = lambda t: "".join(t.split())
        self.assertEqual(squash("".join(texts)), squash(answer))
        self.assertTrue(texts[-1].endswith("50퍼센트를 감면합니다."))

    def test_answer_without_spaces_or_breaks_still_fits(self):
        answer = "가" * 590 + "\n" + "나" * 1180 + "\n예외 조건"
        texts = self._answer_texts(answer)
        self.assertEqual("".join(texts).replace("\n", ""), answer.replace("\n", ""))

    def test_answer_too_long_says_it_was_shortened(self):
        texts = self._answer_texts("가나다 " * 900)
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[-1].endswith(ks.LONG_ANSWER_NOTE))

    def test_callback_response_body_must_say_success(self):
        self.assertTrue(ks.callback_accepted(200, b'{"taskId":"t","status":"SUCCESS"}'))
        self.assertFalse(ks.callback_accepted(200, b'{"taskId":"t","status":"FAIL","message":"expired"}'))
        self.assertFalse(ks.callback_accepted(200, b'{"status":"ERROR"}'))
        self.assertFalse(ks.callback_accepted(200, b"ok"))
        self.assertFalse(ks.callback_accepted(500, b'{"status":"SUCCESS"}'))

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
        self.callback_ok = True
        self.server.kakao.poster = lambda url, body: (self.posted.append((url, body)), self.callback_ok)[1]
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
        # 국민신문고 연결은 보류: 120다산콜재단 전화 버튼으로 안내합니다.
        self.assertEqual(card["buttons"][0], {"action": "phone", "label": "120에 전화하기", "phoneNumber": "02-120"})
        self.assertNotIn("epeople", json.dumps(body))

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

    def _wait_posted(self, count=1):
        for _ in range(80):
            if len(self.posted) >= count:
                return
            time.sleep(0.05)

    def test_failed_callback_keeps_result_for_check_button(self):
        self.callback_ok = False
        callback = "https://bot-api.kakao.com/callback/fail"
        _, body, _ = self.post(payload("느린 실패 콜백", user="cb", callback=callback))
        self.assertTrue(body["useCallback"])
        self.runtime.release.set()
        self._wait_posted()
        self.assertEqual(len(self.posted), 1)
        for _ in range(40):  # 전송 실패 처리까지 끝나기를 잠깐 기다립니다
            _, body, _ = self.post(payload("결과 확인", user="cb"))
            if "textCard" in body["template"]["outputs"][-1]:
                break
            time.sleep(0.05)
        self.assertIn("textCard", body["template"]["outputs"][-1])
        _, body, _ = self.post(payload("결과 확인", user="cb"))
        self.assertIn("확인할 질문이 없어요", body["template"]["outputs"][0]["simpleText"]["text"])

    def test_delivered_callback_clears_pending_result(self):
        callback = "https://bot-api.kakao.com/callback/ok"
        self.post(payload("느린 성공 콜백", user="ok", callback=callback))
        self.runtime.release.set()
        self._wait_posted()
        time.sleep(0.1)
        _, body, _ = self.post(payload("결과 확인", user="ok"))
        self.assertIn("확인할 질문이 없어요", body["template"]["outputs"][0]["simpleText"]["text"])

    def test_same_question_shares_one_waiting_thread_and_one_answer_per_callback(self):
        callback = "https://bot-api.kakao.com/callback/"
        results = []
        def send(i):
            results.append(self.post(payload("느린 같은 질문", user=f"u{i}", callback=callback + str(i))))
        threads = [threading.Thread(target=send, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 20)
        self.assertTrue(all(r[1].get("useCallback") for r in results))
        self.assertEqual(self.server.kakao.inflight_count(), 1)
        self.assertEqual(sum(t.name == "kakao-wait" for t in threading.enumerate()), 1)
        self.runtime.release.set()
        self._wait_posted(20)
        self.assertEqual(sorted(url for url, _ in self.posted), sorted(callback + str(i) for i in range(20)))
        self.assertEqual(self.runtime.paid, ["느린 같은 질문"])


class KakaoSkillLimitTests(unittest.TestCase):
    """작업자 1개·대기열 0일 때 대기 스레드가 질문 수만큼 늘지 않는지 확인합니다(리뷰 사례)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="kakao-test-")
        self.runtime = Runtime()
        self.server = create_server(Path(self.temp.name), 0, runtime=self.runtime, publicizer=publicizer,
                                    health_provider=health, readiness_provider=ready, workers=1, queue_size=0)
        self.server.kakao.sync_wait = 0.2
        self.server.kakao.poster = lambda url, body: True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.runtime.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def post(self, value):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        connection.request("POST", ks.KAKAO_SKILL_PATH, body=json.dumps(value, ensure_ascii=False).encode(),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_thirty_same_questions_make_one_waiting_thread(self):
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.post(payload("느린 질문", user=f"u{i}"))))
                   for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 30)
        self.assertTrue(all(status == 200 for status, _ in results))
        self.assertLessEqual(sum(t.name == "kakao-wait" for t in threading.enumerate()), 1)
        self.assertEqual(self.server.kakao.inflight_count(), 1)

    def test_other_questions_over_the_limit_get_busy_reply_without_new_thread(self):
        self.post(payload("느린 첫 질문", user="a"))
        status, body = self.post(payload("느린 두번째 질문", user="b"))
        self.assertEqual(status, 200)
        self.assertEqual(body["template"]["outputs"][0]["simpleText"]["text"], ks.BUSY_TEXT)
        self.assertLessEqual(sum(t.name == "kakao-wait" for t in threading.enumerate()), 1)


if __name__ == "__main__":
    unittest.main()
