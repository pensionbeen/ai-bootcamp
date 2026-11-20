"""민원 초안: 끝난 질문 하나(질문·답·근거)와 시민이 적은 상황으로 민원 글 초안을 만듭니다. POST /ui/draft

- 모델(solar-pro4)은 제목·민원 요지·요청 사항 세 문장 묶음만 씁니다.
- 관련 조례 목록과 원문 링크는 모델이 아니라 코드가 끝난 답의 근거에서 붙입니다(답변과 같은 신뢰 장치).
- 모델 글에 근거 목록에 없는 조문 번호, 원문·질문·상황에 없는 숫자, 주소(URL)가 들어 있으면 쓰지 않고
  기본 양식으로 바꿉니다. 모델을 부를 수 없을 때도 기본 양식으로 채우고 그렇다고 알립니다.
- 개인정보(이름·연락처·주소)는 받지도 쓰지도 않고 자리표시로 둡니다. 신청은 시민이 신청 사이트에서 직접 합니다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from local_rag_client import CHAT_MODEL

DETAILS_MAX = 1000
TITLE_MAX = 60
SUMMARY_MAX = 800
REQUEST_MAX = 400
DRAFT_TIMEOUT_SECONDS = 60

SITES = (
    {"name": "서울시 응답소", "url": "https://eungdapso.seoul.go.kr/",
     "description": "서울시 업무(시정 불편 건의·신고·질의)를 서울시에 바로 신청합니다."},
    {"name": "국민신문고", "url": "https://www.epeople.go.kr/",
     "description": "모든 행정기관(구청·중앙부처 포함)에 민원을 신청합니다."},
)
DETAILS_PLACEHOLDER = "[언제, 어디서, 어떤 일이 있었는지 적어 주세요]"

SYSTEM_PROMPT = (
    "당신은 서울 시민이 행정기관에 낼 민원 글의 초안을 돕는 도우미입니다. 다음 규칙을 지키세요.\n"
    "1. 사용자가 준 질문, 작성자가 적은 상황, 확인된 조례 답변에 있는 내용만 씁니다. 새로운 사실을 지어내지 않습니다.\n"
    "2. 조례·법령의 조문 번호, 별표, 법령 이름, 주소(URL)는 쓰지 않습니다. 근거 목록은 따로 붙습니다.\n"
    "3. 금액·날짜·시간·장소는 주어진 글에 있는 것만 씁니다. 모르는 것은 [장소], [날짜]처럼 대괄호로 남깁니다.\n"
    "4. 이름, 연락처, 집 주소 같은 개인정보는 쓰지 않습니다.\n"
    "5. 정중한 민원 문체(~합니다, ~바랍니다)로 씁니다.\n"
    "title은 40자 이내의 민원 제목, summary는 무엇이 문제인지 3~6문장, request는 기관에 바라는 조치 1~3문장입니다."
)

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "complaint_draft",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "request": {"type": "string"},
            },
            "required": ["title", "summary", "request"],
            "additionalProperties": False,
        },
    },
}

# chat(payload, timeout) -> 모델 응답 본문(dict, chat completions 모양)
Chat = Callable[[dict, float], dict]


def citation_lines(view: dict[str, Any]) -> list[dict[str, str | None]]:
    """끝난 답의 근거(sources 우선)를 [{label, url, date}]로 꺼냅니다. 코드가 원문에서 만든 값만 씁니다."""
    citations = [c for c in view.get("citation") or [] if isinstance(c, str)]
    if not citations:
        return []
    items = []
    sources = view.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict) and isinstance(source.get("citation"), str):
                url = source.get("source_url")
                items.append({"label": source["citation"],
                              "url": url if isinstance(url, str) and url.startswith("https://") else None,
                              "date": source.get("effective_date") if isinstance(source.get("effective_date"), str) else None})
    return items or [{"label": c, "url": None, "date": None} for c in citations]


def build_payload(question: str, details: str, view: dict[str, Any]) -> dict:
    items = citation_lines(view)
    answer = view.get("answer") if isinstance(view.get("answer"), str) else ""
    facts = {
        "질문": question,
        "작성자가_적은_상황": details or "(적지 않음)",
        "조례에서_근거를_확인했는지": "예" if items else "아니오",
        "확인된_답변": answer if items else "서울시 조례 6종에서 근거를 확인하지 못했습니다.",
        "근거_조항_이름": [item["label"] for item in items],
    }
    return {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
        ],
        "response_format": RESPONSE_FORMAT,
    }


def parse_reply(body: Any) -> dict[str, str] | None:
    try:
        content = body["choices"][0]["message"]["content"]
        value = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    out = {}
    for field, limit in (("title", TITLE_MAX), ("summary", SUMMARY_MAX), ("request", REQUEST_MAX)):
        text = value.get(field)
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            return None
        out[field] = text.strip()
    return out


ARTICLE = re.compile(r"제\s*\d+\s*조(?:\s*의\s*\d+)?")
NUMBER_WITH_UNIT = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:만\s*원|천\s*원|원|퍼센트|%|세|살|일|시간|분|년|개월|미터|m)")
URL = re.compile(r"https?://|www\.", re.I)
FORBIDDEN_WORDS = ("별표", "시행규칙", "시행령")


def unsupported(draft: dict[str, str], sources: str, labels: list[str]) -> list[str]:
    """모델 글에서 근거로 뒷받침되지 않는 표현을 찾습니다. 비어 있으면 통과입니다."""
    problems = []
    text = " ".join(draft.values())
    squash = lambda value: re.sub(r"\s+", "", value)
    allowed_articles = squash(" ".join(labels) + " " + sources)
    for match in ARTICLE.finditer(text):
        if squash(match.group()) not in allowed_articles:
            problems.append("article:" + match.group())
    source_digits = re.sub(r"[,\s]", "", sources)
    for match in NUMBER_WITH_UNIT.finditer(text):
        digits = re.match(r"\d[\d,]*(?:\.\d+)?", match.group()).group().replace(",", "")
        if digits not in source_digits:
            problems.append("number:" + match.group())
    if URL.search(text):
        problems.append("url")
    problems.extend("word:" + word for word in FORBIDDEN_WORDS if word in text and word not in sources)
    return problems


def template(question: str) -> dict[str, str]:
    short = question if len(question) <= 30 else question[:29] + "…"
    return {
        "title": f"{short} 관련 민원",
        "summary": f"서울특별시 조례와 관련하여 다음 사항을 문의드립니다.\n{question}",
        "request": "관련 규정에 따른 확인과 필요한 조치를 요청드립니다. 처리 결과를 알려 주시기 바랍니다.",
    }


def compose(question: str, details: str, view: dict[str, Any], parts: dict[str, str]) -> str:
    items = citation_lines(view)
    lines = [f"제목: {parts['title']}", "", "[민원 요지]", parts["summary"], "", "[현황]", details or DETAILS_PLACEHOLDER, "",
             "[관련 조례 - Lawddy가 조례 원문에서 확인한 근거]"]
    if items:
        for item in items:
            line = "- " + item["label"]
            if item.get("date"):
                line += f" (시행 {item['date']})"
            if item.get("url"):
                line += f"\n  원문: {item['url']}"
            lines.append(line)
    else:
        lines.append("- 서울특별시 조례 6종(2026-10-01 시행본)에서 이 사항의 근거를 확인하지 못했습니다. 관련 규정을 안내해 주시기 바랍니다.")
    lines += ["", "[요청 사항]", parts["request"], "", "※ 이 초안은 Lawddy가 만든 참고용 초안입니다. 신청 전에 내용을 확인하고 고쳐 주세요."]
    return "\n".join(lines)


def make_draft(question: str, details: str, view: dict[str, Any], chat: Chat | None) -> dict[str, Any]:
    """민원 초안 하나. 모델 실패·검사 실패면 기본 양식으로 채우고 notice로 알립니다."""
    details = (details or "").strip()[:DETAILS_MAX]
    items = citation_lines(view)
    answer = view.get("answer") if isinstance(view.get("answer"), str) else ""
    sources = " ".join([question, details, answer if items else ""])
    parts, ai, notice = None, False, None
    if chat is not None:
        try:
            parts = parse_reply(chat(build_payload(question, details, view), DRAFT_TIMEOUT_SECONDS))
        except Exception:
            parts = None
        if parts is None:
            notice = "AI 초안을 만들지 못해 기본 양식으로 채웠어요. 내용을 직접 다듬어 주세요."
        elif unsupported(parts, sources, [item["label"] for item in items]):
            parts = None
            notice = "AI 초안에 확인되지 않은 내용이 있어 기본 양식으로 바꿨어요. 내용을 직접 다듬어 주세요."
        else:
            ai = True
    else:
        notice = "AI 초안을 쓸 수 없어 기본 양식으로 채웠어요. 내용을 직접 다듬어 주세요."
    parts = parts or template(question)
    return {
        "title": parts["title"],
        "body": compose(question, details, view, parts),
        "ai": ai,
        "notice": notice,
        "has_basis": bool(items),
        "sites": [dict(site) for site in SITES],
    }


def upstage_chat(project) -> Chat:
    """운영용 모델 호출: 기존 요청 클라이언트(영수증·같은 요청 재사용)를 그대로 씁니다."""
    from local_rag_client import Client

    def chat(payload: dict, timeout: float) -> dict:
        client = Client(project, parallel=True)
        body, _meta = client.request("complaint", payload, timeout=timeout)
        return body
    return chat
