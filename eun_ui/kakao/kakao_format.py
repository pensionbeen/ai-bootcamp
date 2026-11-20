"""백엔드(/ui/ask, /jobs) 응답을 카카오 챗봇 스킬 응답(JSON 2.0)으로 바꿉니다.

화면 쪽 eun_ui/api.mjs와 같은 기준으로 결과를 나눕니다(docs/ui-guide.md 3장).
- answer : 근거 조항이 있는 답
- unknown: 정상 완료된 "확인 불가"(근거 없음 no_basis / 확정 못함 not_confirmed)
- poll   : 아직 처리 중(/jobs로 다시 확인)
- retry  : 시간 초과·대기열 초과·처리 오류. 근거가 없다는 뜻이 아님
- error  : 요청 자체가 잘못됨

카카오 제한(카카오 비즈니스 가이드 '스킬 응답 JSON 포맷'):
outputs 1~3개, quickReplies 10개 이하, simpleText 1000자, textCard 제목 50자·설명 400자,
버튼은 세로 3개까지, 버튼 글자 14자.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

UNKNOWN = "확인 불가"
JOB_PATH = re.compile(r"^/jobs/[0-9a-f]{64}$")

SIMPLE_TEXT_MAX = 1000
CARD_TITLE_MAX = 50
CARD_DESCRIPTION_MAX = 400
BUTTON_LABEL_MAX = 14
BUTTONS_MAX = 3
OUTPUTS_MAX = 3
QUICK_REPLIES_MAX = 10

# 처음 화면·답 아래에 붙는 빠른 답장. 화면의 examples.mjs에서 분야별로 하나씩 골랐습니다.
# label은 버튼에 보이는 글자, message는 누르면 사용자가 보낸 것처럼 전송되는 질문입니다.
EXAMPLES = [
    ("청년 나이 기준", "서울시 청년 기본 조례에서 말하는 청년은 몇 살부터 몇 살까지인가요?"),
    ("금연구역 과태료", "서울시 금연구역에서 담배를 피우면 과태료가 얼마인가요?"),
    ("공원 흡연", "서울시 도시공원에서 담배를 피워도 되나요?"),
    ("어린이공원 음주", "서울시 어린이공원에서 술을 마시면 과태료가 있나요?"),
    ("반려견 외출 규칙", "서울시에서 반려견을 데리고 외출할 때 소유자가 지켜야 할 사항은 무엇인가요?"),
    ("공영주차장 요금", "서울시 공영주차장 1급지 노상주차장 요금은 5분에 얼마인가요?"),
    ("자전거주차장 요금", "서울시 자전거주차장은 돈을 내야 하나요? 낸다면 얼마인가요?"),
]

HOME_WORDS = {"처음으로", "시작", "시작하기", "도움말", "메뉴", "안녕", "안녕하세요"}
CHECK_WORDS = {"결과 확인", "결과확인", "답변 확인", "답 확인"}

WELCOME = (
    "안녕하세요, 서울시 조례 도우미예요.\n\n"
    "주차, 금연, 청년, 공원, 반려동물, 자전거에 관한 서울시 조례를 찾아 쉬운 말로 답하고 "
    "근거 조항을 함께 보여 드려요. 근거가 없으면 지어내지 않고 \"확인 불가\"라고 답해요.\n\n"
    "궁금한 점을 편하게 보내 주세요. 아래 예시를 눌러도 돼요."
)
NOTE = "서울특별시 조례 6종, 2026-10-01 시행본 기준 안내이며 법률 자문이 아니에요."


# ---------- 결과 분류 (eun_ui/api.mjs와 같은 기준) ----------

def job_to_view(job: dict[str, Any]) -> dict[str, Any]:
    """GET /jobs/<id> 응답을 /ui/ask 응답과 같은 모양으로 바꿉니다."""
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    response = result.get("response")
    status = job.get("status")
    view: dict[str, Any] = {"answer": UNKNOWN, "citation": [], "job_id": job.get("job_id")}
    if status == "completed" and result.get("status") == "processing_error":
        status = "processing_error"
    if status == "completed" and isinstance(response, dict) and isinstance(response.get("answer"), str):
        view["answer"] = response["answer"]
        view["citation"] = response.get("citation") if isinstance(response.get("citation"), list) else []
        view["source_url"] = response.get("source_url")
        view["effective_date"] = response.get("effective_date")
    elif status in ("queued", "running"):
        status = "deadline_exceeded"
    view["status"] = status
    for key in ("sources", "as_of_date"):
        if key in result:
            view[key] = result[key]
    for key in ("poll_url", "message"):
        if job.get(key):
            view[key] = job[key]
    if job.get("result"):
        view["result"] = job["result"]
    return view


def _pick(value: Any, index: int, count: int) -> Any:
    if isinstance(value, list):
        return value[index] if index < len(value) else None
    if isinstance(value, str) and (index == 0 or count == 1):
        return value
    return None


def citation_items(view: dict[str, Any]) -> list[dict[str, Any]]:
    citations = [c for c in view.get("citation") or [] if isinstance(c, str)]
    if not citations:
        return []
    sources = view.get("sources")
    if isinstance(sources, list) and sources:
        items = [
            {"label": s["citation"], "url": s.get("source_url"), "date": s.get("effective_date")}
            for s in sources
            if isinstance(s, dict) and isinstance(s.get("citation"), str)
        ]
        if items:
            return items
    return [
        {"label": c, "url": _pick(view.get("source_url"), i, len(citations)),
         "date": _pick(view.get("effective_date"), i, len(citations))}
        for i, c in enumerate(citations)
    ]


def safe_url(url: Any) -> str | None:
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    return url if urlsplit(url).hostname else None


def inquiry_url(view: dict[str, Any]) -> str | None:
    result = view.get("result") if isinstance(view.get("result"), dict) else {}
    return safe_url(result.get("inquiry_url"))


def split_citation(label: str) -> tuple[str, str]:
    """'서울특별시 청년 기본 조례 제3조 제1호' -> ('서울특별시 청년 기본 조례', '제3조 제1호')"""
    match = re.match(r"^(.*?(?:조례|조례안))\s+((?:제\d|별표|부칙).*)$", label)
    return (match[1], match[2]) if match else (label, "")


def classify(view: dict[str, Any], http_status: int) -> dict[str, str]:
    status = view.get("status") if isinstance(view.get("status"), str) else ""
    if http_status >= 400 and not view.get("job_id"):
        return {"kind": "error", "text": "질문을 처리하지 못했어요. 질문을 조금 줄이거나 바꿔서 다시 보내 주세요."}
    if status == "completed":
        if citation_items(view):
            return {"kind": "answer", "text": ""}
        result = view.get("result") if isinstance(view.get("result"), dict) else {}
        if result.get("status") in ("out_of_scope", "insufficient_evidence"):
            return {"kind": "unknown", "reason": "no_basis",
                    "text": "서울시 조례 6종에서 이 질문의 근거 조항을 찾지 못했어요."}
        return {"kind": "unknown", "reason": "not_confirmed",
                "text": "근거 후보는 찾았지만 답을 확정하지 못했어요."}
    if status == "deadline_exceeded":
        if isinstance(view.get("poll_url"), str) and JOB_PATH.match(view["poll_url"]):
            return {"kind": "poll", "text": "답을 만드는 데 시간이 더 걸리고 있어요."}
        return {"kind": "retry", "text": "답이 늦어지고 있어요. 잠시 후 같은 질문을 다시 보내 주세요."}
    if status == "queue_full":
        return {"kind": "retry", "text": "지금 질문이 많아 잠시 기다려야 해요. 조금 뒤에 다시 보내 주세요."}
    if status == "interrupted":
        return {"kind": "retry", "text": "처리가 중간에 멈췄어요. 잠시 후 같은 질문을 다시 보내 주세요."}
    if status in ("failed", "processing_error"):
        return {"kind": "retry", "text": "처리 중 오류가 났어요. 근거가 없다는 뜻은 아니에요. 잠시 후 다시 보내 주세요."}
    return {"kind": "retry", "text": "답을 받지 못했어요. 잠시 후 다시 보내 주세요."}


# ---------- 카카오 응답 조각 ----------

def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def simple_text(text: str) -> dict[str, Any]:
    return {"simpleText": {"text": _cut(text, SIMPLE_TEXT_MAX)}}


def _text_chunks(text: str, limit: int = SIMPLE_TEXT_MAX, parts: int = 2) -> list[str]:
    """답 문장은 고치지 않고 그대로 보여 줍니다. 1000자를 넘으면 문단 경계에서 나눕니다."""
    chunks: list[str] = []
    rest = text
    while rest and len(chunks) < parts:
        if len(rest) <= limit or len(chunks) == parts - 1:
            chunks.append(_cut(rest, limit))
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return chunks


def web_button(label: str, url: str) -> dict[str, Any]:
    return {"action": "webLink", "label": _cut(label, BUTTON_LABEL_MAX), "webLinkUrl": url}


def message_reply(label: str, message: str) -> dict[str, Any]:
    return {"action": "message", "label": label, "messageText": message}


def home_replies(extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    replies = list(extra or [])
    replies += [message_reply(label, message) for label, message in EXAMPLES]
    return replies[:QUICK_REPLIES_MAX]


def skill_response(outputs: list[dict[str, Any]], quick_replies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    template: dict[str, Any] = {"outputs": outputs[:OUTPUTS_MAX]}
    if quick_replies:
        template["quickReplies"] = quick_replies[:QUICK_REPLIES_MAX]
    return {"version": "2.0", "template": template}


def callback_waiting(text: str = "조례에서 근거를 찾고 있어요. 보통 20초쯤 걸려요.") -> dict[str, Any]:
    """5초 안에 돌려주는 콜백 대기 응답. template은 넣지 않습니다(카카오 콜백 가이드)."""
    return {"version": "2.0", "useCallback": True, "data": {"text": text}}


def welcome() -> dict[str, Any]:
    return skill_response([simple_text(WELCOME)], home_replies())


def pending(question: str) -> dict[str, Any]:
    """콜백 없이 5초를 넘길 때: 결과 확인 버튼으로 다시 받아 가게 합니다."""
    return skill_response(
        [simple_text("조례에서 근거를 찾고 있어요. 보통 20초쯤 걸려요.\n잠시 뒤 아래 '결과 확인'을 눌러 주세요.")],
        [message_reply("결과 확인", "결과 확인")],
    )


def still_pending() -> dict[str, Any]:
    return skill_response(
        [simple_text("아직 답을 만들고 있어요. 조금 뒤에 다시 '결과 확인'을 눌러 주세요.")],
        [message_reply("결과 확인", "결과 확인")],
    )


def nothing_pending() -> dict[str, Any]:
    return skill_response(
        [simple_text("확인할 질문이 없어요. 궁금한 점을 보내 주세요.")],
        home_replies(),
    )


def _sources_card(items: list[dict[str, Any]], as_of: str | None) -> dict[str, Any]:
    lines = []
    for item in items:
        law, part = split_citation(item["label"])
        name = f"{part} ({law.replace('서울특별시 ', '')})" if part else law
        date = f" · 시행 {item['date']}" if isinstance(item.get("date"), str) and item["date"] else ""
        lines.append(f"• {name}{date}")
    description = "\n".join(lines)
    if as_of:
        description += f"\n\n기준일 {as_of} · {NOTE}"
    buttons = []
    seen: set[str] = set()
    for item in items:
        url = safe_url(item.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        _, part = split_citation(item["label"])
        label = f"{part} 원문" if part and len(f"{part} 원문") <= BUTTON_LABEL_MAX else "조례 원문 보기"
        buttons.append(web_button(label, url))
        if len(buttons) == BUTTONS_MAX:
            break
    card: dict[str, Any] = {
        "title": "근거 조항",
        "description": _cut(description, CARD_DESCRIPTION_MAX),
    }
    if buttons:
        card["buttons"] = buttons
        card["buttonLayout"] = "vertical"
    return {"textCard": card}


def render(view: dict[str, Any] | None, info: dict[str, str], question: str = "") -> dict[str, Any]:
    """분류된 결과 하나를 카카오 응답으로 만듭니다."""
    view = view or {}
    as_of = view.get("as_of_date") if isinstance(view.get("as_of_date"), str) else None
    kind = info["kind"]

    if kind == "answer":
        answer = view.get("answer") if isinstance(view.get("answer"), str) else UNKNOWN
        outputs = [simple_text(chunk) for chunk in _text_chunks(answer)]
        outputs.append(_sources_card(citation_items(view), as_of))
        return skill_response(outputs, home_replies([message_reply("처음으로", "처음으로")])[:6])

    if kind == "unknown":
        lines = [info["text"]]
        result = view.get("result") if isinstance(view.get("result"), dict) else {}
        if isinstance(result.get("message"), str) and result["message"]:
            lines.append(result["message"])
        inquiry = inquiry_url(view)
        if info.get("reason") == "no_basis":
            lines.append("조례 밖의 내용(법률·시행규칙·개별 사업 등)일 수 있어요."
                         + (" 국민신문고에 문의하면 담당 기관이 답해 줘요." if inquiry else " 관할 구청이나 담당 부서에 문의해 주세요."))
        else:
            lines.append("질문을 조금 더 구체적으로 바꿔 보거나, 관할 구청이나 담당 부서에 문의해 주세요.")
        card: dict[str, Any] = {"title": UNKNOWN, "description": _cut("\n\n".join(lines), CARD_DESCRIPTION_MAX)}
        if inquiry:
            card["buttons"] = [web_button("국민신문고 문의", inquiry)]
            card["buttonLayout"] = "vertical"
        return skill_response([{"textCard": card}], home_replies([message_reply("처음으로", "처음으로")]))

    # retry / error / poll(제한 시간 안에 못 끝남)
    replies = []
    if kind == "retry" and question:
        replies.append(message_reply("다시 보내기", question))
    replies.append(message_reply("처음으로", "처음으로"))
    return skill_response([simple_text(info["text"])], replies)


def parse_payload(payload: Any) -> tuple[str, str, str | None]:
    """카카오 스킬 요청에서 (발화, 사용자 ID, 콜백 URL)을 꺼냅니다."""
    request = payload.get("userRequest") if isinstance(payload, dict) else None
    if not isinstance(request, dict):
        return "", "", None
    utterance = request.get("utterance") if isinstance(request.get("utterance"), str) else ""
    user = request.get("user") if isinstance(request.get("user"), dict) else {}
    user_id = user.get("id") if isinstance(user.get("id"), str) else ""
    callback = request.get("callbackUrl") if isinstance(request.get("callbackUrl"), str) else None
    return utterance.strip(), user_id[:70], callback
