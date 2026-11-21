"""사진으로 묻기: 시민이 찍은 표지판·고지서·안내문 사진에서 글자를 읽고(Upstage OCR) 질문 하나를 제안합니다. POST /ui/photo

- 사진은 저장하지 않고 메모리에서 OCR로만 보냅니다. 화면이 보내기 전에 크기를 줄여 위치 정보(EXIF)도 빠집니다.
- 읽은 글에서 차량번호·전화번호·주민등록번호·이메일은 [가림]으로 바꾼 뒤에야 모델과 화면에 넘깁니다.
- 제안한 질문은 시민이 확인하고 직접 보내야 질문이 됩니다(자동 전송하지 않음).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from local_rag_client import CHAT_MODEL

OCR_URL = "https://api.upstage.ai/v1/document-digitization"
OCR_MODEL = "ocr"
IMAGE_MAX_BYTES = 3 * 1024 * 1024
OCR_TEXT_MAX = 1500
QUESTION_MAX = 200
OCR_TIMEOUT_SECONDS = 40
QUESTION_TIMEOUT_SECONDS = 40
IMAGE_TYPES = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}

MASKS = (
    re.compile(r"\d{6}\s*-\s*[1-4]\d{6}"),                                   # 주민등록번호
    re.compile(r"(?:0\d{1,2}|1\d{3})\s*[-.)]?\s*\d{3,4}\s*[-.]\s*\d{4}"),     # 전화번호
    re.compile(r"[가-힣]{0,2}\s?\d{2,3}\s?[가-힣]\s?\d{4}"),                   # 차량번호
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),                                   # 이메일
)

SYSTEM_PROMPT = (
    "시민이 찍은 사진(표지판, 과태료 고지서, 안내문 등)에서 읽은 글이 주어집니다. "
    "서울시 조례(주차장, 금연, 청년, 도시공원, 동물보호, 자전거)로 확인해 볼 만한 질문을 시민 입장에서 한 문장으로 만드세요.\n"
    "규칙: 1) 읽은 글에 있는 내용만 씁니다. 2) 이름, 차량번호, 주소, 전화번호 같은 개인정보는 넣지 않습니다. "
    "3) 조문 번호를 지어내지 않습니다. 4) 100자 이내의 자연스러운 질문으로, '서울시'를 넣어 씁니다. "
    "5) 위 분야와 관련이 없으면 사진 내용이 무엇인지 묻는 질문을 만듭니다."
)
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "photo_question",
        "strict": True,
        "schema": {"type": "object", "properties": {"question": {"type": "string"}},
                   "required": ["question"], "additionalProperties": False},
    },
}

Ocr = Callable[[bytes, str], str]            # (image bytes, mime) -> 읽은 글
Chat = Callable[[dict, float], dict]         # (payload, timeout) -> chat completions 본문


class PhotoError(ValueError):
    """화면에 그대로 보여 줄 수 있는 사진 처리 실패."""


def decode_image(value: Any, mime: Any) -> bytes:
    if mime not in IMAGE_TYPES or not isinstance(value, str):
        raise PhotoError("invalid_image")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise PhotoError("invalid_image") from None
    if not data or len(data) > IMAGE_MAX_BYTES or not data.startswith(IMAGE_TYPES[mime]):
        raise PhotoError("invalid_image")
    return data


def mask(text: str) -> str:
    for pattern in MASKS:
        text = pattern.sub("[가림]", text)
    return text


def suggest_question(text: str, chat: Chat | None) -> tuple[str, bool]:
    """(질문, 모델이 만들었는지). 모델을 못 쓰면 읽은 글 첫 줄로 기본 질문을 만듭니다."""
    if chat is not None:
        payload = {"model": CHAT_MODEL, "response_format": RESPONSE_FORMAT,
                   "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": text}]}
        try:
            body = chat(payload, QUESTION_TIMEOUT_SECONDS)
            question = json.loads(body["choices"][0]["message"]["content"])["question"]
            if isinstance(question, str):
                question = mask(question.strip())
                if 4 <= len(question) <= QUESTION_MAX and "http" not in question:
                    return question, True
        except Exception:
            pass
    first = next((line.strip() for line in text.splitlines() if len(line.strip()) >= 2), text.strip())[:60]
    return f"서울시 조례에서 '{first}'에 관한 내용을 알려 주세요.", False


def read_photo(value: Any, mime: Any, ocr: Ocr, chat: Chat | None) -> dict[str, Any]:
    data = decode_image(value, mime)
    try:
        raw = ocr(data, mime)
    except Exception:
        raise PhotoError("ocr_failed") from None
    text = mask(re.sub(r"[ \t]+", " ", raw or "").strip())[:OCR_TEXT_MAX]
    if len(re.sub(r"\W", "", text)) < 2:
        raise PhotoError("no_text")
    question, ai = suggest_question(text, chat)
    return {"text": text, "question": question, "ai": ai}


def upstage_ocr(project) -> Ocr:
    """운영용 OCR: Upstage Document Digitization(model=ocr), 동기 호출."""
    def ocr(data: bytes, mime: str) -> str:
        import requests
        from dotenv import dotenv_values
        key = os.getenv("UPSTAGE_API_KEY") or dotenv_values(Path(project) / ".env").get("UPSTAGE_API_KEY")
        if not key:
            raise RuntimeError("missing_upstage_api_key")
        name = "photo.jpg" if mime == "image/jpeg" else "photo.png"
        response = requests.post(OCR_URL, headers={"Authorization": f"Bearer {key}"},
                                 files={"document": (name, data, mime)}, data={"model": OCR_MODEL},
                                 timeout=(10, OCR_TIMEOUT_SECONDS))
        response.raise_for_status()
        body = response.json()
        if isinstance(body.get("text"), str):
            return body["text"]
        return "\n".join(page.get("text", "") for page in body.get("pages", []) if isinstance(page, dict))
    return ocr


def upstage_chat(project) -> Chat:
    from local_rag_client import Client

    def chat(payload: dict, timeout: float) -> dict:
        body, _meta = Client(project, parallel=True).request("photo_question", payload, timeout=timeout)
        return body
    return chat
