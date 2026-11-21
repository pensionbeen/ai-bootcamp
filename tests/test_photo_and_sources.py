"""사진으로 묻기(POST /ui/photo)와 조문 원문 보기(POST /ui/sources) 시험. 가짜 OCR·모델만 씁니다."""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

import photo_question as pq
import source_text
from local_rag_service import create_server

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200
ROOT = Path(__file__).resolve().parents[1]


def b64(data):
    return base64.b64encode(data).decode()


def chat_reply(question):
    return lambda payload, timeout: {"choices": [{"message": {"content": json.dumps({"question": question}, ensure_ascii=False)}}]}


class PhotoUnitTests(unittest.TestCase):
    def test_masks_personal_information(self):
        text = pq.mask("차량번호 12가 3456 / 서울 123나4567 연락처 010-1234-5678 02-123-4567 주민 900101-1234567 a@b.kr")
        for secret in ("3456", "4567", "5678", "1234567", "a@b.kr"):
            self.assertNotIn(secret, text)
        self.assertIn("[가림]", text)

    def test_reads_photo_and_suggests_question_without_personal_info(self):
        ocr = lambda data, mime: "금연구역\n이곳은 금연구역입니다. 위반 시 과태료 10만원\n문의 02-123-4567"
        result = pq.read_photo(b64(JPEG), "image/jpeg", ocr, chat_reply("서울시 금연구역에서 흡연하면 과태료가 얼마인가요?"))
        self.assertTrue(result["ai"])
        self.assertNotIn("4567", result["text"])
        self.assertEqual(result["question"], "서울시 금연구역에서 흡연하면 과태료가 얼마인가요?")

    def test_model_question_is_masked_and_falls_back(self):
        ocr = lambda data, mime: "주정차 위반 과태료 고지서"
        masked = pq.read_photo(b64(PNG), "image/png", ocr, chat_reply("12가3456 차량 주정차 과태료는?"))
        self.assertNotIn("3456", masked["question"])
        fallback = pq.read_photo(b64(PNG), "image/png", ocr, None)
        self.assertFalse(fallback["ai"])
        self.assertIn("주정차 위반 과태료 고지서", fallback["question"])

    def test_rejects_bad_images_and_empty_text(self):
        ocr = lambda data, mime: "글자"
        for value, mime in ((b64(b"GIF89a"), "image/jpeg"), ("@@@", "image/jpeg"), (b64(JPEG), "image/gif"),
                            (b64(b"\xff\xd8\xff" + b"0" * (pq.IMAGE_MAX_BYTES)), "image/jpeg")):
            with self.assertRaises(pq.PhotoError):
                pq.read_photo(value, mime, ocr, None)
        with self.assertRaisesRegex(pq.PhotoError, "no_text"):
            pq.read_photo(b64(JPEG), "image/jpeg", lambda data, mime: " . ", None)


@unittest.skipUnless(source_text.database_path(ROOT).is_file(), "검색 DB가 없어 건너뜁니다")
class SourceTextTests(unittest.TestCase):
    def test_finds_article_paragraph_and_annex_text(self):
        found = source_text.lookup(ROOT, ["서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항",
                                          "서울특별시 청년 기본 조례 제3조 제1호",
                                          "서울특별시 주차장 설치 및 관리 조례 별표 1",
                                          "서울특별시 없는 조례 제1조"])
        self.assertIn("10만원의 과태료", found[0]["text"])
        self.assertIn("19세 이상 39세 이하", found[1]["text"])
        self.assertIn("주차요금", found[2]["text"])
        self.assertFalse(found[3]["found"])
        self.assertTrue(all(len(item.get("text", "")) <= source_text.TEXT_MAX for item in found))


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
    label = "서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항"
    return {"status": "verified_draft", "as_of_date": "2026-10-01",
            "response": {"answer": "10만원입니다.", "citation": [label], "source_url": ["https://www.law.go.kr/a"],
                         "effective_date": ["2026-10-01"]},
            "sources": [{"citation": label, "source_url": "https://www.law.go.kr/a", "effective_date": "2026-10-01"}]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT, prefix="photo-test-")
        self.runtime = Runtime()
        self.looked = []
        def lookup(labels):
            self.looked.append(labels)
            return [{"citation": label, "found": True, "text": "원문 " + label} for label in labels]
        self.server = create_server(Path(self.temp.name), 0, runtime=self.runtime, publicizer=publicizer,
                                    health_provider=lambda: {"as_of_date": "2026-10-01", "identity": "x", "dimension": 1024, "summary": {}},
                                    readiness_provider=lambda: [], workers=2, queue_size=2,
                                    drafter=lambda payload, timeout: {},
                                    photo_ocr=lambda data, mime: "금연구역 과태료 10만원",
                                    photo_chat=chat_reply("서울시 금연구역 과태료는 얼마인가요?"),
                                    source_lookup=lookup)
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

    def test_photo_endpoint(self):
        status, body = self.post("/ui/photo", {"image": b64(JPEG), "mime": "image/jpeg"})
        self.assertEqual((status, body["question"]), (200, "서울시 금연구역 과태료는 얼마인가요?"))
        self.assertEqual(self.post("/ui/photo", {"image": "x", "mime": "image/jpeg"})[0], 400)
        self.server.photo_ocr = lambda data, mime: ""
        self.assertEqual(self.post("/ui/photo", {"image": b64(JPEG), "mime": "image/jpeg"})[0], 422)

    def test_photo_busy(self):
        for _ in range(2):
            self.server.photo_slots.acquire()
        try:
            self.assertEqual(self.post("/ui/photo", {"image": b64(JPEG), "mime": "image/jpeg"})[0], 429)
        finally:
            for _ in range(2):
                self.server.photo_slots.release()

    def test_sources_only_for_that_answers_citations(self):
        _, view = self.post("/ui/ask", {"question": "금연 과태료"})
        status, body = self.post("/ui/sources", {"job_id": view["job_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.looked, [["서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항"]])
        self.assertEqual(body["sources"][0]["text"], "원문 서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항")
        self.assertEqual(self.post("/ui/sources", {"job_id": "nope"})[0], 400)
        self.assertEqual(self.post("/ui/sources", {"job_id": "d" * 64})[0], 404)
        _, slow = self.post("/ui/ask", {"question": "느린 질문", "wait_seconds": 0})
        self.assertEqual(self.post("/ui/sources", {"job_id": slow["job_id"]})[0], 409)


if __name__ == "__main__":
    unittest.main()
