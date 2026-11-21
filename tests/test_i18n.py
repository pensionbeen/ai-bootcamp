"""다국어: 질문 번역·답 번역(숫자 검증)·조문 번역·카톡 외국어 응답 시험. 가짜 모델만 씁니다."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import i18n_answer as i18n
from local_rag_service import create_server

ROOT = Path(__file__).resolve().parents[1]
LABEL = "서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항"
KO_ANSWER = "서울시가 지정한 금연구역에서 흡연하면 10만원의 과태료가 부과됩니다."


def reply(**fields):
    return {"choices": [{"message": {"content": json.dumps(fields, ensure_ascii=False)}}]}


def fake_chat(answer="You will be fined KRW 100,000 (10만원).", question_ko="서울시 금연구역 흡연 과태료는 얼마인가요?",
              text="(1) A fine of KRW 100,000 (10만원)."):
    calls = []

    def chat(payload, timeout):
        calls.append(payload)
        name = payload["response_format"]["json_schema"]["name"]
        if name.endswith("question_ko"):
            return reply(question_ko=question_ko)
        if name.endswith("answer"):
            return reply(answer=answer)
        return reply(text=text)
    chat.calls = calls
    return chat


class UnitTests(unittest.TestCase):
    def test_detects_language_from_script(self):
        self.assertEqual(i18n.detect_lang("금연구역 과태료"), "ko")
        self.assertEqual(i18n.detect_lang("How much is the fine?"), "en")
        self.assertEqual(i18n.detect_lang("禁煙区域で吸うといくらですか"), "ja")
        self.assertEqual(i18n.detect_lang("在禁烟区吸烟罚款多少"), "zh-CN")
        self.assertEqual(i18n.detect_lang("在禁菸區吸菸罰款多少"), "zh-TW")
        self.assertIsNone(i18n.valid_lang("fr"))
        # 외국인이 한국어 낱말을 섞어 물어도 그 사람 언어로 봅니다.
        self.assertEqual(i18n.detect_lang("how age what '청년'"), "en")
        self.assertEqual(i18n.detect_lang("Can I smoke at 한강공원?"), "en")
        self.assertEqual(i18n.detect_lang("「금연구역」はどういう意味ですか"), "ja")
        self.assertEqual(i18n.detect_lang("这个牌子写的“금연”是什么意思"), "zh-CN")
        # 한국 사람이 영어 낱말을 섞어도 한국어입니다.
        self.assertEqual(i18n.detect_lang("공원에서 drone 날려도 돼?"), "ko")
        self.assertEqual(i18n.detect_lang("PM 킥보드 주차 과태료"), "ko")
        self.assertEqual(i18n.detect_lang("결과 확인"), "ko")

    def test_numbers_must_survive_translation(self):
        self.assertTrue(i18n.numbers_kept(KO_ANSWER, "Fine: KRW 100,000 (10만원)."))
        self.assertTrue(i18n.numbers_kept(KO_ANSWER, "罚款10万韩元。"))
        self.assertTrue(i18n.numbers_kept(KO_ANSWER, "過料は10萬ウォンです。"))
        self.assertFalse(i18n.numbers_kept(KO_ANSWER, "Fine: KRW 10,000."))
        self.assertFalse(i18n.numbers_kept(KO_ANSWER, "You will be fined."))
        # 조문 번호는 지킬 숫자가 아닙니다.
        self.assertEqual(i18n.answer_numbers("제5조에 따라 19세 이상"), {19.0})

    def test_translate_answer_falls_back_to_korean_when_a_number_changes(self):
        ok = i18n.translate_answer(KO_ANSWER, "en", fake_chat())
        self.assertTrue(ok["verified"])
        self.assertIn("100,000", ok["text"])
        bad = i18n.translate_answer(KO_ANSWER, "en", fake_chat(answer="You will be fined KRW 10,000."))
        self.assertFalse(bad["verified"])
        self.assertEqual(bad["text"], KO_ANSWER)

    def test_translate_text_returns_none_on_number_mismatch(self):
        source = "① 금연구역에서 흡연을 한 사람은 10만원의 과태료를 부과한다."
        self.assertIn("100,000", i18n.translate_text(source, "en", fake_chat()))
        self.assertIsNone(i18n.translate_text(source, "en", fake_chat(text="A fine of KRW 50,000.")))


class Runtime:
    def __call__(self, project, question, check_only=False, *, attempt_id=None):
        identity = hashlib.sha256((question + (attempt_id or "")).encode()).hexdigest()
        if check_only:
            return {"status": "check_only", "cached": False, "run_identity": identity}
        return {"status": "completed", "run_identity": identity, "question": question}


def publicizer(audit, project):
    return {"status": "verified_draft", "as_of_date": "2026-10-01",
            "response": {"answer": KO_ANSWER, "citation": [LABEL], "source_url": ["https://www.law.go.kr/a"],
                         "effective_date": ["2026-10-01"]},
            "sources": [{"citation": LABEL, "source_url": "https://www.law.go.kr/a", "effective_date": "2026-10-01"}]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT, prefix="i18n-test-")
        self.chat = fake_chat()
        self.server = create_server(Path(self.temp.name), 0, runtime=Runtime(), publicizer=publicizer,
                                    health_provider=lambda: {"as_of_date": "2026-10-01", "identity": "x", "dimension": 1024, "summary": {}},
                                    readiness_provider=lambda: [], workers=2, queue_size=2,
                                    drafter=lambda payload, timeout: {},
                                    source_lookup=lambda labels: [{"citation": l, "found": True, "text": "10만원의 과태료"} for l in labels],
                                    translate_chat=self.chat)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, value=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        body = None if value is None else json.dumps(value, ensure_ascii=False).encode()
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data

    def ask(self):
        status, body = self.request("POST", "/ui/ask", {"question": "서울시 금연구역 흡연 과태료는 얼마인가요?"})
        self.assertEqual(status, 200)
        return body["job_id"]

    def test_question_is_translated_to_korean(self):
        status, body = self.request("POST", "/ui/translate-question", {"question": "How much is the smoking fine?", "lang": "en"})
        self.assertEqual((status, body["question_ko"]), (200, "서울시 금연구역 흡연 과태료는 얼마인가요?"))
        self.assertEqual(self.request("POST", "/ui/translate-question", {"question": "x", "lang": "ko"})[0], 400)
        self.assertEqual(self.request("POST", "/ui/translate-question", {"question": "", "lang": "en"})[0], 400)

    def test_answer_translation_is_number_checked(self):
        job = self.ask()
        status, body = self.request("POST", "/ui/translate-answer", {"job_id": job, "lang": "ja"})
        self.assertEqual(status, 200)
        self.assertTrue(body["answer"]["verified"])
        self.server.translate_chat = fake_chat(answer="KRW 1,000.")
        status, body = self.request("POST", "/ui/translate-answer", {"job_id": job, "lang": "en"})
        self.assertFalse(body["answer"]["verified"])
        self.assertEqual(body["answer"]["text"], KO_ANSWER)
        self.assertEqual(self.request("POST", "/ui/translate-answer", {"job_id": "b" * 64, "lang": "en"})[0], 404)

    def test_sources_carry_translation_only_for_foreign_languages(self):
        job = self.ask()
        _, korean = self.request("POST", "/ui/sources", {"job_id": job})
        self.assertNotIn("translation", korean["sources"][0])
        _, english = self.request("POST", "/ui/sources", {"job_id": job, "lang": "en"})
        self.assertIn("100,000", english["sources"][0]["translation"])
        self.assertEqual(self.request("POST", "/ui/sources", {"job_id": job, "lang": "fr"})[0], 400)

    def test_photo_sign_text_is_translated_for_foreign_languages(self):
        import base64
        self.server.photo_ocr = lambda data, mime: "금연구역 위반 시 과태료 10만원"
        image = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 200).decode()
        _, korean = self.request("POST", "/ui/photo", {"image": image, "mime": "image/jpeg"})
        self.assertNotIn("text_translation", korean)
        _, english = self.request("POST", "/ui/photo", {"image": image, "mime": "image/jpeg", "lang": "en"})
        self.assertIn("100,000", english["text_translation"])
        self.assertTrue(english["question_display"])

    def test_home_page_opens_with_language_parameter(self):
        self.assertEqual(self.request("GET", "/?lang=en")[0], 200)
        self.assertEqual(self.request("GET", "/?lang=<script>")[0], 404)


class KakaoForeignTests(ServiceTests):
    def kakao(self, utterance):
        value = {"intent": {"id": "f"}, "bot": {"id": "b"},
                 "userRequest": {"utterance": utterance, "user": {"id": "visitor", "type": "botUserKey"}}}
        return self.request("POST", "/kakao/skill", value)

    def test_english_question_gets_english_answer_with_korean_sources(self):
        self.server.kakao.sync_wait = 3
        status, body = self.kakao("How much is the fine for smoking in a no-smoking zone?")
        self.assertEqual(status, 200)
        outputs = body["template"]["outputs"]
        self.assertIn("KRW 100,000", outputs[0]["simpleText"]["text"])
        self.assertEqual(outputs[-1]["textCard"]["buttons"][0]["webLinkUrl"], "https://www.law.go.kr/a")
        searched = [call["messages"][-1]["content"] for call in self.chat.calls]
        self.assertTrue(any("How much is the fine" in text for text in searched))

    def test_changed_number_shows_korean_original(self):
        self.server.kakao.sync_wait = 3
        self.server.kakao.translate_chat = fake_chat(answer="You will be fined KRW 50,000.")
        _, body = self.kakao("How much is the fine for smoking in a no-smoking zone?")
        text = json.dumps(body, ensure_ascii=False)
        self.assertIn(KO_ANSWER, text)
        self.assertNotIn("50,000", text)
        self.assertIn("02-120", text)

    def test_foreign_greeting_needs_no_model(self):
        _, body = self.kakao("hello")
        self.assertIn("Lawddy", json.dumps(body, ensure_ascii=False))
        self.assertEqual(self.chat.calls, [])


if __name__ == "__main__":
    unittest.main()
