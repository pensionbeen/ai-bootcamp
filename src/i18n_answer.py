"""다국어: 외국인이 자기 언어로 묻고 자기 언어로 답을 받게 합니다.

흐름: 외국어 질문 → (모델) 한국어 질문 → 기존 검색·검증(그대로) → 한국어 답 → (모델) 답 번역
- 검색·검증·인용은 한국어 원문 그대로입니다. 번역은 보여 주는 글에만 씁니다.
- 번역한 답이 원문 답의 숫자·금액을 하나라도 빠뜨리거나 바꾸면 번역을 쓰지 않고 한국어 답과 안내를 보여 줍니다.
- 근거 조항 이름과 조례 원문은 한국어 그대로 두고, 원문 아래에 번역을 따로 붙입니다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from local_rag_client import CHAT_MODEL

LANGS = {
    "ko": "Korean",
    "en": "English",
    "zh-CN": "Simplified Chinese",
    "zh-TW": "Traditional Chinese (Taiwan)",
    "ja": "Japanese",
}
FOREIGN = tuple(code for code in LANGS if code != "ko")
TEXT_MAX = 4000
TIMEOUT_SECONDS = 40

Chat = Callable[[dict, float], dict]


class TranslateError(ValueError):
    pass


def valid_lang(value: Any) -> str | None:
    return value if isinstance(value, str) and value in LANGS else None


def detect_lang(text: str) -> str:
    """카카오처럼 언어를 고를 수 없는 곳에서 씁니다.

    외국인은 표지판에서 본 한국어 낱말을 섞어 묻기도 합니다(예: how age what '청년').
    그래서 한글이 조금 있다고 한국어로 보지 않고 양을 비교합니다:
    영어와 한국어는 낱말 수로, 중국어·일본어는 글자 수로 견줍니다. 같으면 한국어입니다.
    """
    hangul = len(re.findall(r"[가-힣]", text))
    kana = len(re.findall(r"[぀-ヿ]", text))
    han = re.findall(r"[一-鿿]", text)
    latin = len(re.findall(r"[A-Za-z]+", text))
    hangul_words = len(re.findall(r"[가-힣]+", text))
    if kana and kana + len(han) >= hangul:
        return "ja"
    if han and len(han) >= hangul and len(han) >= latin:
        # 간체와 모양이 다른 번체 글자만 넣습니다(禁·多처럼 양쪽이 같은 글자는 넣지 않음).
        traditional = set("們這個來會時說對開關點體學國灣與為麼裡後車場園煙菸號區費罰騎狗貓錢嗎")
        return "zh-TW" if any(ch in traditional for ch in han) else "zh-CN"
    if latin > hangul_words:
        return "en"
    return "ko"


def _json_reply(chat: Chat, system: str, user: str, field: str) -> str:
    payload = {
        "model": CHAT_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "translation_" + field, "strict": True,
            "schema": {"type": "object", "properties": {field: {"type": "string"}},
                       "required": [field], "additionalProperties": False}}},
    }
    body = chat(payload, TIMEOUT_SECONDS)
    try:
        value = json.loads(body["choices"][0]["message"]["content"])[field]
    except (KeyError, IndexError, TypeError, ValueError):
        raise TranslateError("bad_model_reply") from None
    if not isinstance(value, str) or not value.strip():
        raise TranslateError("bad_model_reply")
    return value.strip()


def still_korean(text: str) -> bool:
    """번역 결과가 사실상 한국어 그대로인지(모델이 원문을 되돌려 준 경우).

    "KRW 100,000 (10만원)"처럼 괄호 속 한국어는 괜찮으므로 글자 비율로 봅니다.
    """
    hangul = len(re.findall(r"[가-힣]", text))
    other = len(re.findall(r"[A-Za-z぀-ヿ一-鿿]", text))
    return hangul > 0 and hangul >= (hangul + other) * 0.35


def _source_block(text: str, lang: str) -> str:
    return f"Translate the Korean text between <<< and >>> into {LANGS[lang]}. Write the result in {LANGS[lang]} only.\n<<<\n{text}\n>>>"


def question_to_korean(question: str, lang: str, chat: Chat) -> str:
    """외국어 질문을 검색에 쓸 한국어 질문 하나로 바꿉니다."""
    system = (
        "You translate a visitor's question about local rules in Seoul into natural Korean for searching "
        "Seoul Metropolitan Government ordinances (parking, smoking, youth, city parks, animal protection, bicycles). "
        "Keep the meaning exactly; do not add facts or answer the question. If the question does not mention Seoul, "
        "write it as a question about 서울시. Return JSON {\"question_ko\": \"...\"}."
    )
    korean = _json_reply(chat, system, f"[{LANGS[lang]}] {question[:TEXT_MAX]}", "question_ko")
    if not re.search(r"[가-힣]", korean) or len(korean) > 500:
        raise TranslateError("not_korean")
    return korean


# ---- 숫자 검증: 번역이 금액·나이·시간을 바꾸지 않았는지 ----

_KO_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(만|천)?\s*(원|세|살|분|시간|일|개월|년|퍼센트|%|급지)?")


def _values(text: str) -> set[float]:
    """글 속 숫자 값(만·万·천·千 단위 포함)을 모읍니다."""
    found = set()
    for number, scale in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(만|万|萬|천|千)?", text):
        try:
            value = float(number.replace(",", ""))
        except ValueError:
            continue
        if scale in ("만", "万", "萬"):
            value *= 10000
        elif scale in ("천", "千"):
            value *= 1000
        found.add(value)
    return found


_HISTORY = re.compile(r"[<〈]\s*(?:개정|신설|본조신설|전문개정|삭제|타법개정)[^>〉]*[>〉]")
_DATE = re.compile(r"\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.?")


def strip_history(text: str) -> str:
    """조문 끝의 개정 이력(<개정 2011.9.29, 2015.10.8>)을 뺍니다. 번역·숫자 대조 모두 본문만 봅니다."""
    return re.sub(r"[ \t]+\n", "\n", _HISTORY.sub("", text)).strip()


def answer_numbers(answer: str) -> set[float]:
    """원문 답에서 지켜야 할 숫자 값. 조문 번호(제5조)·개정 이력·날짜는 빼고, 단위가 있거나 세 자리 이상인 숫자만."""
    values = set()
    answer = _DATE.sub(" ", strip_history(answer))
    for match in _KO_NUMBER.finditer(answer):
        start = match.start()
        if start > 0 and answer[start - 1] in "제":
            continue
        number, scale, unit = match.groups()
        digits = number.replace(",", "")
        if not (unit or scale or len(digits.split(".")[0]) >= 3):
            continue
        value = float(digits)
        if scale == "만":
            value *= 10000
        elif scale == "천":
            value *= 1000
        values.add(value)
    return values


def numbers_kept(korean: str, translated: str) -> bool:
    return answer_numbers(korean) <= _values(translated)


def translate_answer(answer: str, lang: str, chat: Chat) -> dict[str, Any]:
    """{text, verified}. 숫자 검증에 실패하면 verified=False이고 text는 한국어 원문입니다."""
    system = (
        f"Translate this answer about a Seoul ordinance from Korean into {LANGS[lang]} for a foreign visitor. "
        "Rules: keep every number, amount, age and time exactly; after each money amount add the original Korean in "
        "parentheses, e.g. \"KRW 100,000 (10만원)\". Keep ordinance and article names in Korean inside quotes. "
        "Do not add advice or facts that are not in the original. Return JSON {\"answer\": \"...\"}."
    )
    translated = None
    for _ in range(2):
        try:
            candidate = _json_reply(chat, system, _source_block(answer[:TEXT_MAX], lang), "answer")
        except TranslateError:
            system += "\nReturn valid JSON containing a nonempty translation."
            continue
        except Exception:
            continue
        if not still_korean(candidate):
            translated = candidate
            break
        system += f"\nThe previous reply repeated the Korean source. Translate it into {LANGS[lang]}."
    if translated is None:
        return {"text": answer, "verified": False, "reason": "translation_failed"}
    if not numbers_kept(answer, translated):
        return {"text": answer, "verified": False, "reason": "numbers_changed"}
    return {"text": translated, "verified": True}


TEXT_KINDS = {
    "ordinance": "this Korean ordinance text",
    "sign": "this text read by OCR from a sign or notice photographed in Seoul (it may contain OCR noise; "
            "translate what is readable and skip broken fragments)",
    "question": "this Korean question about Seoul rules",
}


def translate_text(text: str, lang: str, chat: Chat, kind: str = "ordinance") -> str | None:
    """참고용 번역(조례 원문·표지판 글·질문). 숫자가 바뀌거나 실패하면 None."""
    if kind == "ordinance":
        text = strip_history(text)
    system = (
        f"You are a translator. Translate {TEXT_KINDS[kind]} into {LANGS[lang]} as a faithful reference translation. "
        f"The output must be written in {LANGS[lang]}, never left in Korean. "
        "Keep numbers, article numbers (제N조 → Article N) and list markers. Do not summarize or add anything. "
        "Return JSON {\"text\": \"...\"}."
    )
    # 모델이 한국어를 그대로 돌려주는 일이 있어, 그 언어로 안 나왔으면 한 번 더 시도합니다.
    for _ in range(2):
        try:
            translated = _json_reply(chat, system, _source_block(text[:TEXT_MAX], lang), "text")
        except TranslateError:
            system += "\nReturn valid JSON containing a nonempty translation."
            continue
        except Exception:
            continue
        if still_korean(translated) or not numbers_kept(text, translated):
            system += f"\nTranslate the source again into {LANGS[lang]}, preserving all original values."
            continue
        return translated
    return None


def translate_question(question: str, lang: str, chat: Chat) -> str | None:
    """사진에서 만든 한국어 질문을 사용자 언어로 보여 주기 위한 번역.

    검색은 원래 한국어 질문으로 하므로(손대지 않았을 때) 보여 주기용입니다.
    숫자 대조에서 떨어지거나 응답이 비면 한 번 더 시도합니다.
    """
    return translate_text(question, lang, chat, "question")


def upstage_chat(project) -> Chat:
    from local_rag_client import Client

    def chat(payload: dict, timeout: float) -> dict:
        body, _meta = Client(project, parallel=True).request("translate", payload, timeout=timeout)
        return body
    return chat
