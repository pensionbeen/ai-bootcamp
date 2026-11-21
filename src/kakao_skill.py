"""카카오톡 채널 챗봇(카카오 i 오픈빌더 스킬) 연동: POST /kakao/skill.

카카오톡 → 오픈빌더 → POST /kakao/skill(이 서버) → 기존 질문 처리 큐(Jobs) → 카카오 응답 JSON 2.0

- 질문 처리는 /ui/ask와 같은 Jobs.submit / Jobs.wait를 그대로 씁니다. 결과는 sync_view 모양으로 받아
  화면(src/web/app/api.mjs)과 같은 기준으로 나눕니다:
  answer(근거 있는 답) / unknown(정상 완료된 확인 불가) / retry(시간 초과·대기열 초과·처리 오류) / error
- 카카오는 스킬 응답을 5초 안에 받아야 합니다(카카오 비즈니스 가이드, skill timeout 5초).
  1) 블록에 Callback을 켜면 요청에 userRequest.callbackUrl이 옵니다. 바로 useCallback 응답을 보내고,
     답이 나오면 callbackUrl로 최종 응답을 POST합니다(callbackUrl은 5분 동안 1회 사용).
  2) callbackUrl이 없으면 3.5초 안에 끝난 답은 바로 보내고, 아니면 "결과 확인" 빠른 답장을 보냅니다.
  3) 콜백 전송이 실패하면(카카오가 SUCCESS가 아닌 status를 주거나 연결 실패) 결과를 보관해 "결과 확인"으로 다시 받게 합니다.
- 같은 질문은 기다리는 작업 하나를 함께 쓰고, 동시에 기다리는 질문 수에 상한(max_inflight)을 둡니다.
- 카카오 응답 제한: outputs 1~3개, quickReplies 10개, simpleText 1000자, textCard 설명 400자, 버튼 세로 3개·14자.
- 외부 패키지 없이 표준 라이브러리만 씁니다.
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
    "안녕하세요, 서울시 조례 및 민원 도우미 로디(Lawddy)예요.\n\n"
    "주차, 금연, 청년, 공원, 반려동물, 자전거에 관한 서울시 조례를 찾아 쉬운 말로 답하고 "
    "근거 조항을 함께 보여 드려요. 근거가 없으면 지어내지 않고 \"확인 불가\"라고 답해요.\n\n"
    "궁금한 점을 편하게 보내 주세요. 아래 예시를 눌러도 돼요."
)
NOTE = "서울특별시 조례 6종, 2026-10-01 시행본 기준 안내이며 법률 자문이 아니에요."


# ---------- 결과 분류 (src/web/app/api.mjs와 같은 기준) ----------

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


LONG_ANSWER_NOTE = "\n\n(답이 길어 뒷부분을 줄였어요. 아래 근거 조항 원문에서 전체 내용을 확인해 주세요.)"
def _break_at(rest: str, lo: int, hi: int) -> int:
    """rest[lo:hi] 안에서 자를 위치: 줄바꿈 → 문장 끝 → 공백 → hi 순서로 고릅니다."""
    lo = max(1, min(lo, hi))
    nl = rest.rfind("\n", lo, hi + 1)
    if nl >= lo:
        return nl
    ends = [m.end() for m in re.finditer(r"[.!?。](?=\s)", rest[:hi]) if m.end() >= lo]
    if ends:
        return ends[-1]
    space = rest.rfind(" ", lo, hi + 1)
    if space >= lo:
        return space
    return hi


def _text_chunks(text: str, limit: int = SIMPLE_TEXT_MAX, parts: int = 2) -> list[str]:
    """답 문장은 고치지 않고 parts개 말풍선(각 limit자)에 나눠 담습니다.
    전체가 들어가는 길이면 글자를 하나도 버리지 않도록 자를 위치를 고릅니다(남은 글이 남은 말풍선에 들어가는 범위 안에서
    줄바꿈·문장 끝·공백을 우선). 다 들어가지 않을 때만 마지막 말풍선 끝을 줄이고 그렇다고 알립니다."""
    rest = text.strip()
    chunks: list[str] = []
    while rest:
        left = parts - len(chunks)
        if len(rest) <= limit:
            chunks.append(rest)
            return chunks
        if left == 1:
            room = limit - len(LONG_ANSWER_NOTE)
            cut = _break_at(rest, room // 2, room - 1)
            chunks.append(rest[:cut].rstrip() + "…" + LONG_ANSWER_NOTE)
            return chunks
        # 이 말풍선을 lo자 이상 채워야 나머지가 남은 말풍선에 들어갑니다(공백을 버리는 만큼 여유가 생김).
        lo = len(rest) - (left - 1) * limit
        if lo > limit:  # 어떻게 잘라도 다 들어가지 않음: 보기 좋은 위치에서 자르고 마지막에서 줄입니다.
            lo = limit // 2
        cut = _break_at(rest, lo, limit)
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return chunks or [text[:limit]]


def web_button(label: str, url: str) -> dict[str, Any]:
    return {"action": "webLink", "label": _cut(label, BUTTON_LABEL_MAX), "webLinkUrl": url}


def phone_button(label: str, number: str) -> dict[str, Any]:
    return {"action": "phone", "label": _cut(label, BUTTON_LABEL_MAX), "phoneNumber": number}


# 민원 신청(국민신문고) 연결은 보류(2026-11-21 결정). 근거가 없을 때는 120다산콜재단 전화로 안내합니다.
HELP_NUMBER = "02-120"


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


def callback_waiting(text: str = "조례에서 근거를 찾고 있어요. 보통 20초쯤 걸려요.\n답이 오지 않으면 '결과 확인'을 보내 주세요.") -> dict[str, Any]:
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
        if info.get("reason") == "no_basis":
            lines.append("조례 밖의 내용(국가 법률·시행규칙·개별 사업 등)일 수 있어요. 처벌 여부는 여기서 판단하지 않아요."
                         " 120다산콜재단(02-120)에 물어보면 담당 기관을 안내받을 수 있어요.")
        else:
            lines.append("질문을 조금 더 구체적으로 바꿔 보거나, 120다산콜재단(02-120)에 물어봐 주세요.")
        card: dict[str, Any] = {"title": UNKNOWN, "description": _cut("\n\n".join(lines), CARD_DESCRIPTION_MAX),
                                "buttons": [phone_button("120에 전화하기", HELP_NUMBER)], "buttonLayout": "vertical"}
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


# ---------- 다국어 (외국인 방문객): 질문 언어를 알아서 그 언어로 답합니다 ----------

FOREIGN_HOME_WORDS = {"hi", "hello", "help", "start", "menu", "你好", "您好", "开始", "開始", "こんにちは", "はじめる", "ヘルプ"}
FOREIGN_CHECK_WORDS = {"check result", "result", "查看结果", "查看結果", "結果確認", "結果を確認"}

T = {
    "en": {
        "welcome": "Hi, I'm Lawddy, Seoul's ordinance & civil-complaint helper.\n\nAsk in English about Seoul's local rules on smoking, parks, bicycles, parking, pets and youth. I answer with the exact ordinance article, and I say so when the ordinance has no answer.",
        "examples": [("Smoking fine", "What is the fine for smoking in a no-smoking area in Seoul?"),
                     ("Drinking in parks", "Can I drink alcohol in a park in Seoul?"),
                     ("Bike parking", "Do I have to pay to park a bicycle in Seoul?")],
        "home": "Start over", "check": "Check result",
        "basis": "Legal basis (근거 조항)", "unknown": "Not confirmed (확인 불가)",
        "no_basis": "We could not find a basis for this in Seoul's 6 ordinances (parking, smoking, youth, parks, animals, bicycles). It may be covered by national law or another rule, so Lawddy does not say whether it is punishable.",
        "call": "Call 120 (press 9)",
        "help": "For help in your language, call 02-120 and press 9 (English, Chinese, Japanese, Vietnamese, Mongolian; weekdays 9:00-18:00).",
        "unverified": "(We could not safely translate this answer, so it is shown in Korean. For help in your language, call 120 Dasan Call: 02-120, then press 9.)",
        "note": "Based on Seoul ordinances in force on 2026-10-01. Reference only, not legal advice.",
        "waiting": "Looking up the ordinance. This usually takes about 20 seconds. If no answer arrives, send 'Check result'.",
        "pending": "Looking up the ordinance. This usually takes about 20 seconds.\nPlease tap 'Check result' in a moment.",
        "still": "Still working on it. Please tap 'Check result' again shortly.",
        "nothing": "There is no question to check. Please send your question.",
        "busy": "Many questions right now. Please try again in a moment.",
        "retry": "Sorry, the answer could not be prepared. Please send the question again.",
        "resend": "Send again",
    },
    "zh-CN": {
        "welcome": "你好，我是首尔条例与民愿助手 Lawddy。\n\n可以用中文询问首尔关于吸烟、公园、自行车、停车、宠物、青年的规定。我会附上条例原文条款回答；条例里没有的，我会如实告诉你。",
        "examples": [("吸烟罚款", "在首尔禁烟区吸烟罚款多少？"), ("公园饮酒", "在首尔的公园里可以喝酒吗？"), ("自行车停车", "在首尔停放自行车要收费吗？")],
        "home": "重新开始", "check": "查看结果",
        "basis": "依据条款 (근거 조항)", "unknown": "无法确认 (확인 불가)",
        "no_basis": "在首尔6部条例（停车、禁烟、青年、公园、动物、自行车）中找不到相关依据，可能属于国家法律或其他规定，因此不判断是否会受处罚。",
        "call": "拨打120（按9）",
        "help": "如需中文帮助，请拨打 02-120 后按 9（工作日 9:00-18:00，提供英语、中文、日语、越南语、蒙古语）。",
        "unverified": "（为避免译错数字，以下显示韩文原文。如需母语帮助，请拨打120茶山热线：02-120后按9。）",
        "note": "依据2026-10-01施行的首尔条例，仅供参考，不构成法律意见。",
        "waiting": "正在查找条例，通常需要约20秒。如未收到回复，请发送“查看结果”。",
        "pending": "正在查找条例，通常需要约20秒。\n请稍后点击“查看结果”。",
        "still": "仍在处理中，请稍后再次点击“查看结果”。",
        "nothing": "没有需要查看的问题，请直接发送您的问题。",
        "busy": "当前提问较多，请稍后再试。",
        "retry": "抱歉，暂时无法生成回答，请重新发送问题。",
        "resend": "重新发送",
    },
    "zh-TW": {
        "welcome": "你好，我是首爾條例與陳情助手 Lawddy。\n\n可以用中文詢問首爾關於吸菸、公園、自行車、停車、寵物、青年的規定。我會附上條例原文條款回答；條例裡沒有的，我會如實告訴你。",
        "examples": [("吸菸罰款", "在首爾禁菸區吸菸罰款多少？"), ("公園飲酒", "在首爾的公園裡可以喝酒嗎？"), ("自行車停車", "在首爾停放自行車要收費嗎？")],
        "home": "重新開始", "check": "查看結果",
        "basis": "依據條款 (근거 조항)", "unknown": "無法確認 (확인 불가)",
        "no_basis": "在首爾6部條例（停車、禁菸、青年、公園、動物、自行車）中找不到相關依據，可能屬於國家法律或其他規定，因此不判斷是否會受處罰。",
        "call": "撥打120（按9）",
        "help": "如需中文協助，請撥打 02-120 後按 9（平日 9:00-18:00，提供英語、中文、日語、越南語、蒙古語）。",
        "unverified": "（為避免譯錯數字，以下顯示韓文原文。如需母語協助，請撥打120茶山熱線：02-120後按9。）",
        "note": "依據2026-10-01施行的首爾條例，僅供參考，不構成法律意見。",
        "waiting": "正在查找條例，通常需要約20秒。如未收到回覆，請傳送「查看結果」。",
        "pending": "正在查找條例，通常需要約20秒。\n請稍後點選「查看結果」。",
        "still": "仍在處理中，請稍後再次點選「查看結果」。",
        "nothing": "沒有需要查看的問題，請直接傳送您的問題。",
        "busy": "目前提問較多，請稍後再試。",
        "retry": "抱歉，暫時無法產生回答，請重新傳送問題。",
        "resend": "重新傳送",
    },
    "ja": {
        "welcome": "こんにちは。ソウル市の条例・民願ヘルパー Lawddy です。\n\n喫煙、公園、自転車、駐車、ペット、青年に関するソウルのルールを日本語で質問できます。条例の条文を根拠に答え、条例に定めがない場合はそうお伝えします。",
        "examples": [("喫煙の過料", "ソウルの禁煙区域で喫煙すると過料はいくらですか？"), ("公園での飲酒", "ソウルの公園でお酒を飲んでもいいですか？"), ("自転車の駐輪", "ソウルで自転車を駐輪するのにお金はかかりますか？")],
        "home": "最初から", "check": "結果確認",
        "basis": "根拠条項 (근거 조항)", "unknown": "確認できません (확인 불가)",
        "no_basis": "ソウル市の条例6種（駐車場、禁煙、青年、公園、動物、自転車）にこの根拠は見つかりませんでした。国の法律など別のルールの対象かもしれないため、処罰の有無は判断しません。",
        "call": "120に電話（9番）",
        "help": "日本語での相談は 02-120 に電話して 9 を押してください（平日 9:00-18:00、英語・中国語・日本語・ベトナム語・モンゴル語）。",
        "unverified": "（数字の誤訳を避けるため、韓国語の原文を表示します。母語での相談は120タサンコール：02-120のあと9。）",
        "note": "2026-10-01施行のソウル市条例に基づく参考情報で、法律相談ではありません。",
        "waiting": "条例を調べています。通常20秒ほどかかります。回答が届かない場合は「結果確認」と送ってください。",
        "pending": "条例を調べています。通常20秒ほどかかります。\n少ししてから「結果確認」を押してください。",
        "still": "まだ処理中です。少ししてからもう一度「結果確認」を押してください。",
        "nothing": "確認する質問がありません。質問を送ってください。",
        "busy": "現在質問が多いため、少ししてからもう一度お試しください。",
        "retry": "申し訳ありません。回答を用意できませんでした。もう一度質問を送ってください。",
        "resend": "もう一度送る",
    },
}


def _t(lang: str, key: str):
    return T.get(lang, T["en"])[key]


def foreign_replies(lang: str, extra: list | None = None) -> list[dict[str, Any]]:
    replies = list(extra or [])
    replies.append(message_reply(_t(lang, "home"), "hello" if lang == "en" else {"zh-CN": "你好", "zh-TW": "你好", "ja": "こんにちは"}.get(lang, "hello")))
    replies += [message_reply(label, message) for label, message in _t(lang, "examples")]
    return replies[:QUICK_REPLIES_MAX]


def foreign_simple(lang: str, key: str, check: bool = False) -> dict[str, Any]:
    replies = [message_reply(_t(lang, "check"), _t(lang, "check"))] if check else foreign_replies(lang)
    return skill_response([simple_text(_t(lang, key))], replies)


def foreign_welcome(lang: str) -> dict[str, Any]:
    return skill_response([simple_text(_t(lang, "welcome"))], foreign_replies(lang)[1:])


def render_foreign(view: dict[str, Any] | None, info: dict[str, str], question: str, lang: str,
                   translated: dict[str, Any] | None) -> dict[str, Any]:
    """외국어 사용자용 응답. 답은 번역(숫자 검증 통과분)을, 근거 조항 이름은 한국어 원문 그대로 씁니다."""
    view = view or {}
    kind = info["kind"]
    if kind == "answer":
        if translated and translated.get("verified"):
            text = translated["text"]
        else:
            text = (view.get("answer") if isinstance(view.get("answer"), str) else UNKNOWN) + "\n\n" + _t(lang, "unverified")
        outputs = [simple_text(chunk) for chunk in _text_chunks(text)]
        card = _sources_card(citation_items(view), None)["textCard"]
        card["title"] = _t(lang, "basis")
        card["description"] = _cut(card["description"] + "\n\n" + _t(lang, "note"), CARD_DESCRIPTION_MAX)
        outputs.append({"textCard": card})
        return skill_response(outputs, foreign_replies(lang))
    if kind == "unknown":
        card = {"title": _t(lang, "unknown"),
                "description": _cut(_t(lang, "no_basis") + "\n\n" + _t(lang, "help"), CARD_DESCRIPTION_MAX),
                "buttons": [phone_button(_t(lang, "call"), HELP_NUMBER)], "buttonLayout": "vertical"}
        return skill_response([{"textCard": card}], foreign_replies(lang))
    replies = [message_reply(_t(lang, "resend"), question)] if question else []
    return skill_response([simple_text(_t(lang, "retry"))], foreign_replies(lang, replies))


# ---------- 5초 제한 처리: 콜백 / 결과 확인 ----------

import json  # noqa: E402
import logging  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Callable  # noqa: E402
from urllib import error as urlerror  # noqa: E402
from urllib import request as urlrequest  # noqa: E402

LOG = logging.getLogger("kakao_skill")

KAKAO_SKILL_PATH = "/kakao/skill"
SYNC_WAIT_SECONDS = 3.5          # 카카오 5초 제한 안에 응답하도록 여유를 둡니다.
ANSWER_DEADLINE_SECONDS = 270    # callbackUrl 유효 시간 5분보다 짧게 끝냅니다. 결과 확인 방식도 같은 시간을 씁니다.
RESULT_TTL_SECONDS = 15 * 60     # "결과 확인"으로 다시 받을 수 있게 결과를 보관하는 시간
DEFAULT_MAX_INFLIGHT = 4         # 동시에 기다리는 서로 다른 질문 수(= 대기 스레드 수) 상한
MAX_CALLBACKS_PER_QUESTION = 50  # 같은 질문에 붙는 콜백 수 상한
DEFAULT_CALLBACK_HOSTS = ("kakao.com", "kakaoenterprise.com")
BUSY_TEXT = "지금 질문이 많아 잠시 기다려야 해요. 조금 뒤에 다시 보내 주세요."

# resolver(question, deadline_monotonic) -> (view, http_status). view는 sync_view 모양입니다.
Resolver = Callable[[str, float], "tuple[dict, int]"]
# poster(callback_url, body) -> 카카오가 전송을 받아들였으면 True
Poster = Callable[[str, dict], bool]


def callback_allowed(url: str | None, suffixes: tuple[str, ...]) -> bool:
    """콜백은 카카오 도메인의 https 주소로만 보냅니다(다른 주소로 요청을 보내게 하는 공격 방지)."""
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == s or host.endswith("." + s) for s in suffixes)


def callback_accepted(status: int, raw: bytes) -> bool:
    """콜백 API 응답이 HTTP 2xx이고 본문 status가 SUCCESS일 때만 성공입니다(FAIL·ERROR·본문 없음은 실패)."""
    if not 200 <= status < 300:
        return False
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(body, dict) and body.get("status") == "SUCCESS"


def post_callback(url: str, body: dict, timeout: float = 10) -> bool:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read(4096)
    except urlerror.HTTPError as exc:
        status, raw = exc.code, exc.read(4096)
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        LOG.warning("kakao callback failed: %s", type(exc).__name__)
        return False
    ok = callback_accepted(status, raw)
    if not ok:
        LOG.warning("kakao callback rejected: HTTP %s %s", status, raw[:200].decode("utf-8", "replace"))
    return ok


def _outcome(resolver: Resolver, question: str, deadline: float) -> tuple[dict | None, dict]:
    try:
        view, status = resolver(question, deadline)
    except Exception:
        return None, {"kind": "retry", "text": "처리 중 오류가 났어요. 근거가 없다는 뜻은 아니에요. 잠시 후 다시 보내 주세요."}
    info = classify(view, status)
    if info["kind"] == "poll":  # 제한 시간 안에 끝나지 않음
        info = {"kind": "retry", "text": "답이 너무 늦어지고 있어요. 잠시 후 같은 질문을 다시 보내 주세요."}
    return view, info


@dataclass
class Inflight:
    """기다리는 질문 하나. 같은 질문을 보낸 사용자들이 함께 씁니다."""
    question: str
    created: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    outcome: tuple | None = None
    callbacks: list = field(default_factory=list)  # [(user_id, callback_url)]
    lang: str = "ko"
    body: dict | None = None  # 보낼 응답(질문 언어로 만든 것)


@dataclass
class UserResult:
    item: Inflight
    created: float = field(default_factory=time.monotonic)


class KakaoSkill:
    """카카오 스킬 요청 하나를 받아 카카오 응답 JSON을 돌려줍니다. 응답은 항상 5초 안에 끝납니다.

    대기 스레드는 서로 다른 질문마다 하나이고, 동시에 max_inflight개까지만 둡니다."""

    def __init__(self, resolver: Resolver, callback_hosts: tuple[str, ...] = DEFAULT_CALLBACK_HOSTS,
                 sync_wait: float = SYNC_WAIT_SECONDS, poster: Poster = post_callback,
                 max_inflight: int = DEFAULT_MAX_INFLIGHT, deadline_seconds: float = ANSWER_DEADLINE_SECONDS,
                 translate_chat=None):
        self.resolver = resolver
        # 외국어 질문 번역용 모델 호출. 없으면 모든 질문을 한국어로 처리합니다.
        self.translate_chat = translate_chat
        self.callback_hosts = callback_hosts
        self.sync_wait = sync_wait
        self.poster = poster
        self.max_inflight = max(1, int(max_inflight))
        self.deadline_seconds = deadline_seconds
        self._lock = threading.Lock()
        self._inflight: dict[str, Inflight] = {}
        self._users: dict[str, UserResult] = {}

    # ---- 보관 ----

    def _expire(self) -> None:
        cutoff = time.monotonic() - RESULT_TTL_SECONDS
        self._users = {k: v for k, v in self._users.items() if v.created > cutoff}

    def _drop_user(self, user_id: str, item: Inflight) -> None:
        with self._lock:
            record = self._users.get(user_id)
            if record is not None and record.item is item:
                del self._users[user_id]

    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    # ---- 작업 ----

    def _answer(self, item: Inflight) -> None:
        """질문 하나를 끝까지 처리해 item.outcome과 item.body(질문 언어의 응답)를 채웁니다."""
        deadline = item.created + self.deadline_seconds
        if item.lang == "ko":
            item.outcome = _outcome(self.resolver, item.question, deadline)
            item.body = render(*item.outcome, item.question)
            return
        import i18n_answer
        try:
            korean = i18n_answer.question_to_korean(item.question, item.lang, self.translate_chat)
        except Exception:
            item.outcome = (None, {"kind": "retry", "text": ""})
            item.body = render_foreign(None, item.outcome[1], item.question, item.lang, None)
            return
        view, info = _outcome(self.resolver, korean, deadline)
        item.outcome = (view, info)
        translated = None
        if info["kind"] == "answer" and isinstance((view or {}).get("answer"), str):
            translated = i18n_answer.translate_answer(view["answer"], item.lang, self.translate_chat)
        item.body = render_foreign(view, info, item.question, item.lang, translated)

    def _work(self, item: Inflight) -> None:
        try:
            self._answer(item)
        except Exception:
            item.outcome = None
        finally:
            if item.outcome is None or item.body is None:
                item.outcome = (None, {"kind": "retry", "text": "처리 중 오류가 났어요. 잠시 후 다시 보내 주세요."})
                item.body = (render(*item.outcome, item.question) if item.lang == "ko"
                             else render_foreign(None, item.outcome[1], item.question, item.lang, None))
            with self._lock:
                if self._inflight.get(item.question) is item:
                    del self._inflight[item.question]
                callbacks = list(item.callbacks)
            item.done.set()
        body = item.body
        for user_id, url in callbacks:
            try:
                ok = self.poster(url, body)
            except Exception:
                ok = False
            if ok:
                self._drop_user(user_id, item)
            else:
                # 전송 실패: 결과를 남겨 두었다가 사용자가 "결과 확인"을 보내면 보여 줍니다.
                LOG.warning("kakao callback not delivered; result kept for 결과 확인")

    def handle(self, payload: Any) -> dict:
        utterance, user_id, callback_url = parse_payload(payload)
        if not utterance or utterance in HOME_WORDS:
            return welcome()
        lang = "ko"
        if self.translate_chat is not None:
            import i18n_answer
            lang = i18n_answer.detect_lang(utterance)
        lowered = utterance.lower().strip(" !.?")
        if lang != "ko" and lowered in FOREIGN_HOME_WORDS:
            return foreign_welcome(lang)
        if utterance in CHECK_WORDS or lowered in FOREIGN_CHECK_WORDS:
            check_lang = "ko" if utterance in CHECK_WORDS else lang
            with self._lock:
                self._expire()
                record = self._users.get(user_id)
            if record is None:
                return nothing_pending() if check_lang == "ko" else foreign_simple(check_lang, "nothing")
            if not record.item.done.is_set():
                return still_pending() if record.item.lang == "ko" else foreign_simple(record.item.lang, "still", check=True)
            self._drop_user(user_id, record.item)
            return record.item.body

        question = utterance
        use_callback = callback_allowed(callback_url, self.callback_hosts)
        if callback_url and not use_callback:
            LOG.warning("kakao callbackUrl host not allowed; using 결과 확인 mode: %s", urlsplit(callback_url).hostname)
        start = False
        with self._lock:
            self._expire()
            item = self._inflight.get(question)
            if item is None:
                if len(self._inflight) >= self.max_inflight:
                    if lang != "ko":
                        return foreign_simple(lang, "busy")
                    return render(None, {"kind": "retry", "text": BUSY_TEXT}, question)
                item = Inflight(question=question, lang=lang)
                self._inflight[question] = item
                start = True
            if use_callback and len(item.callbacks) < MAX_CALLBACKS_PER_QUESTION and not item.done.is_set():
                item.callbacks.append((user_id, callback_url))
            else:
                use_callback = False
            if user_id:
                self._users[user_id] = UserResult(item)
        if start:
            threading.Thread(target=self._work, args=(item,), daemon=True, name="kakao-wait").start()
        if use_callback:
            return callback_waiting() if item.lang == "ko" else callback_waiting(_t(item.lang, "waiting"))
        if item.done.wait(self.sync_wait):
            self._drop_user(user_id, item)
            return item.body
        return pending(question) if item.lang == "ko" else foreign_simple(item.lang, "pending", check=True)
