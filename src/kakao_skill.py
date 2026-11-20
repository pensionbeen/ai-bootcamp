"""카카오톡 채널 챗봇(카카오 i 오픈빌더 스킬) 연동: POST /kakao/skill.

카카오톡 → 오픈빌더 → POST /kakao/skill(이 서버) → 기존 질문 처리 큐(Jobs) → 카카오 응답 JSON 2.0

- 질문 처리는 /ui/ask와 같은 Jobs.submit / Jobs.wait를 그대로 씁니다. 결과는 sync_view 모양으로 받아
  화면(src/web, eun_ui/api.mjs)과 같은 기준으로 나눕니다:
  answer(근거 있는 답) / unknown(정상 완료된 확인 불가) / retry(시간 초과·대기열 초과·처리 오류) / error
- 카카오는 스킬 응답을 5초 안에 받아야 합니다(카카오 비즈니스 가이드, skill timeout 5초).
  1) 블록에 Callback을 켜면 요청에 userRequest.callbackUrl이 옵니다. 바로 useCallback 응답을 보내고,
     답이 나오면 callbackUrl로 최종 응답을 POST합니다(callbackUrl은 5분 동안 1회 사용).
  2) callbackUrl이 없으면 3.5초 안에 끝난 답은 바로 보내고, 아니면 "결과 확인" 빠른 답장을 보냅니다.
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
    "안녕하세요, 서울시 조례 도우미 로디(Rawddy)예요.\n\n"
    "주차, 금연, 청년, 공원, 반려동물, 자전거에 관한 서울시 조례를 찾아 쉬운 말로 답하고 "
    "근거 조항을 함께 보여 드려요. 근거가 없으면 지어내지 않고 \"확인 불가\"라고 답해요.\n\n"
    "궁금한 점을 편하게 보내 주세요. 아래 예시를 눌러도 돼요."
)
NOTE = "서울특별시 조례 6종, 2026-10-01 시행본 기준 안내이며 법률 자문이 아니에요."


# ---------- 결과 분류 (eun_ui/api.mjs와 같은 기준) ----------

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
CALLBACK_DEADLINE_SECONDS = 270  # callbackUrl 유효 시간 5분보다 짧게 끝냅니다.
FALLBACK_DEADLINE_SECONDS = 600  # 결과 확인 방식에서 기다리는 최대 시간(화면과 같은 10분)
PENDING_TTL_SECONDS = 15 * 60
DEFAULT_CALLBACK_HOSTS = ("kakao.com", "kakaoenterprise.com")

# resolver(question, deadline_monotonic) -> (view, http_status). view는 sync_view 모양입니다.
Resolver = Callable[[str, float], "tuple[dict, int]"]


def callback_allowed(url: str | None, suffixes: tuple[str, ...]) -> bool:
    """콜백은 카카오 도메인의 https 주소로만 보냅니다(다른 주소로 요청을 보내게 하는 공격 방지)."""
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == s or host.endswith("." + s) for s in suffixes)


def post_callback(url: str, body: dict, timeout: float = 10) -> None:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            LOG.info("kakao callback sent: HTTP %s", resp.status)
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        LOG.warning("kakao callback failed: %s", type(exc).__name__)


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
class Pending:
    question: str
    created: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    outcome: tuple | None = None


class KakaoSkill:
    """카카오 스킬 요청 하나를 받아 카카오 응답 JSON을 돌려줍니다. 응답은 항상 5초 안에 끝납니다."""

    def __init__(self, resolver: Resolver, callback_hosts: tuple[str, ...] = DEFAULT_CALLBACK_HOSTS,
                 sync_wait: float = SYNC_WAIT_SECONDS, poster: Callable[[str, dict], None] = post_callback):
        self.resolver = resolver
        self.callback_hosts = callback_hosts
        self.sync_wait = sync_wait
        self.poster = poster
        self._lock = threading.Lock()
        self._pending: dict[str, Pending] = {}

    def _put(self, user_id: str, item: Pending) -> None:
        with self._lock:
            cutoff = time.monotonic() - PENDING_TTL_SECONDS
            self._pending = {k: v for k, v in self._pending.items() if v.created > cutoff}
            self._pending[user_id] = item

    def _get(self, user_id: str) -> Pending | None:
        with self._lock:
            return self._pending.get(user_id)

    def _drop(self, user_id: str, item: Pending) -> None:
        with self._lock:
            if self._pending.get(user_id) is item:
                del self._pending[user_id]

    def handle(self, payload: Any) -> dict:
        utterance, user_id, callback_url = parse_payload(payload)
        if not utterance or utterance in HOME_WORDS:
            return welcome()
        if utterance in CHECK_WORDS:
            item = self._get(user_id)
            if item is None:
                return nothing_pending()
            if not item.done.is_set():
                return still_pending()
            self._drop(user_id, item)
            view, info = item.outcome
            return render(view, info, item.question)

        question = utterance
        if callback_allowed(callback_url, self.callback_hosts):
            deadline = time.monotonic() + CALLBACK_DEADLINE_SECONDS

            def work_callback():
                view, info = _outcome(self.resolver, question, deadline)
                self.poster(callback_url, render(view, info, question))

            threading.Thread(target=work_callback, daemon=True, name="kakao-callback").start()
            return callback_waiting()
        if callback_url:
            LOG.warning("kakao callbackUrl host not allowed; using 결과 확인 mode: %s", urlsplit(callback_url).hostname)

        item = Pending(question=question)
        deadline = item.created + FALLBACK_DEADLINE_SECONDS

        def work_pending():
            item.outcome = _outcome(self.resolver, question, deadline)
            item.done.set()

        self._put(user_id, item)
        threading.Thread(target=work_pending, daemon=True, name="kakao-pending").start()
        if item.done.wait(self.sync_wait):
            self._drop(user_id, item)
            view, info = item.outcome
            return render(view, info, question)
        return pending(question)
