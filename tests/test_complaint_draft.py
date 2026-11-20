"""민원 초안(POST /ui/draft) 시험. 가짜 모델만 쓰고 외부 API에는 연결하지 않습니다."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import complaint_draft as cd
from local_rag_service import create_server

VIEW = {"status": "completed", "job_id": "a" * 64, "answer": "금연구역에서 흡연하면 10만원의 과태료가 부과됩니다.",
        "citation": ["서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항"],
        "sources": [{"citation": "서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항",
                     "source_url": "https://www.law.go.kr/a", "effective_date": "2026-10-01"}]}
UNKNOWN_VIEW = {"status": "completed", "job_id": "b" * 64, "answer": "확인 불가", "citation": [],
                "result": {"status": "out_of_scope"}}


def reply(**fields):
    return {"choices": [{"message": {"content": json.dumps(fields, ensure_ascii=False)}}]}


GOOD = dict(title="공원 금연구역 흡연 단속 요청",
            summary="공원 금연구역에서 흡연하는 사람이 있어 불편을 겪고 있습니다. 조례상 과태료 10만원 대상으로 안내받았습니다.",
            request="현장 점검과 단속을 요청드립니다.")


class DraftUnitTests(unittest.TestCase):
    def test_ai_draft_keeps_model_text_and_code_citations(self):
        draft = cd.make_draft("금연구역 과태료?", "11월 20일 [장소]에서 흡연", VIEW, lambda payload, timeout: reply(**GOOD))
        self.assertTrue(draft["ai"])
        self.assertIsNone(draft["notice"])
        self.assertIn("공원 금연구역 흡연 단속 요청", draft["body"])
        self.assertIn("- 서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항 (시행 2026-10-01)", draft["body"])
        self.assertIn("원문: https://www.law.go.kr/a", draft["body"])
        self.assertEqual([s["url"] for s in draft["sites"]], ["https://eungdapso.seoul.go.kr/", "https://www.epeople.go.kr/"])

    def test_payload_gives_model_only_question_details_answer_and_labels(self):
        payload = cd.build_payload("질문", "상황", VIEW)
        facts = json.loads(payload["messages"][1]["content"])
        self.assertEqual(facts["근거_조항_이름"], VIEW["citation"])
        self.assertNotIn("https://", payload["messages"][1]["content"])
        self.assertEqual(payload["response_format"]["json_schema"]["schema"]["required"], ["title", "summary", "request"])

    def test_invented_article_number_or_amount_falls_back_to_template(self):
        for bad in (dict(GOOD, summary="조례 제12조에 따라 단속해 주세요."),
                    dict(GOOD, summary="과태료 50만원을 부과해 주세요."),
                    dict(GOOD, request="https://example.com 참고 바랍니다."),
                    dict(GOOD, summary="별표 2에 따라 처리해 주세요.")):
            draft = cd.make_draft("금연구역 과태료?", "", VIEW, lambda payload, timeout, bad=bad: reply(**bad))
            self.assertFalse(draft["ai"], bad)
            self.assertIn("확인되지 않은 내용", draft["notice"])
            self.assertIn("[언제, 어디서, 어떤 일이 있었는지 적어 주세요]", draft["body"])

    def test_numbers_from_answer_or_details_are_allowed(self):
        ok = dict(GOOD, summary="11월 20일 7시에 흡연이 있었고, 과태료 10만원 대상으로 압니다.")
        draft = cd.make_draft("과태료?", "11월 20일 저녁 7시", VIEW, lambda payload, timeout: reply(**ok))
        self.assertTrue(draft["ai"])

    def test_model_failure_or_no_model_uses_template_with_notice(self):
        def broken(payload, timeout):
            raise RuntimeError("missing_upstage_api_key")
        for chat in (broken, lambda payload, timeout: {"choices": []}, None):
            draft = cd.make_draft("헬멧 과태료?", "", UNKNOWN_VIEW, chat)
            self.assertFalse(draft["ai"])
            self.assertTrue(draft["notice"])
            self.assertIn("근거를 확인하지 못했습니다", draft["body"])
            self.assertIn("헬멧 과태료? 관련 민원", draft["body"])

    def test_unknown_answer_is_not_given_to_model_as_fact(self):
        facts = json.loads(cd.build_payload("헬멧?", "", UNKNOWN_VIEW)["messages"][1]["content"])
        self.assertEqual(facts["조례에서_근거를_확인했는지"], "아니오")
        self.assertEqual(facts["근거_조항_이름"], [])


class Runtime:
    def __init__(self):
        self.release = threading.Event()

    def __call__(self, project, question, check_only=False, *, attempt_id=None):
        identity = hashlib.sha256((question + (attempt_id or "")).encode()).hexdigest()
        if check_only:
            return {"status": "check_only", "cached": False, "run_identity": identity}
        if "느린" in question:
            self.release.wait(10)
        return {"status": "completed", "run_identity": identity, "question": question}


def publicizer(audit, project):
    return {"status": "verified_draft", "as_of_date": "2026-10-01",
            "response": {"answer": VIEW["answer"], "citation": VIEW["citation"],
                         "source_url": ["https://www.law.go.kr/a"], "effective_date": ["2026-10-01"]},
            "sources": VIEW["sources"]}


class DraftServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="draft-test-")
        self.runtime = Runtime()
        self.calls = []
        def drafter(payload, timeout):
            self.calls.append(payload)
            return reply(**GOOD)
        self.server = create_server(Path(self.temp.name), 0, runtime=self.runtime, publicizer=publicizer,
                                    health_provider=lambda: {"as_of_date": "2026-10-01", "identity": "x", "dimension": 1024, "summary": {}},
                                    readiness_provider=lambda: [], workers=2, queue_size=2, drafter=drafter)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.runtime.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def post(self, path, value):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("POST", path, body=json.dumps(value, ensure_ascii=False).encode(),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_draft_from_finished_question(self):
        _, view = self.post("/ui/ask", {"question": "금연구역 과태료?"})
        status, body = self.post("/ui/draft", {"job_id": view["job_id"], "details": "공원에서 흡연"})
        self.assertEqual(status, 200)
        self.assertTrue(body["draft"]["ai"])
        self.assertIn("제10조 제1항", body["draft"]["body"])
        facts = json.loads(self.calls[0]["messages"][1]["content"])
        self.assertEqual(facts["질문"], "금연구역 과태료?")  # 질문은 서버가 작업 기록에서 꺼냅니다
        self.assertEqual(facts["작성자가_적은_상황"], "공원에서 흡연")

    def test_rejects_bad_unknown_and_unfinished_jobs(self):
        self.assertEqual(self.post("/ui/draft", {"job_id": "nope"})[0], 400)
        self.assertEqual(self.post("/ui/draft", {"job_id": "c" * 64})[0], 404)
        self.assertEqual(self.post("/ui/draft", {"job_id": "c" * 64, "details": "가" * 1001})[0], 400)
        _, job = self.post("/ui/ask", {"question": "느린 질문", "wait_seconds": 0})
        self.assertEqual(self.post("/ui/draft", {"job_id": job["job_id"]})[0], 409)
        self.assertEqual(self.calls, [])

    def test_busy_when_draft_slots_are_taken(self):
        _, view = self.post("/ui/ask", {"question": "금연구역 과태료?"})
        for _ in range(2):
            self.server.draft_slots.acquire()
        try:
            self.assertEqual(self.post("/ui/draft", {"job_id": view["job_id"]})[0], 429)
        finally:
            for _ in range(2):
                self.server.draft_slots.release()


if __name__ == "__main__":
    unittest.main()
