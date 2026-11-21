"""번역 품질 자체 평가 (Lawddy 다국어).

사용법 (lawddy-all 폴더, 서버가 8000번에서 켜져 있을 때):
    .venv/Scripts/python.exe translation_eval.py

하는 일
  1. 예시 질문 12개(한국어)를 서버에 물어 한국어 답을 받습니다(이미 물어본 건 캐시라 빠름).
  2. 각 답을 영어·중국어(간체/번체)·일본어로 번역합니다(실제 서비스와 같은 /ui/translate-answer).
  3. 번역 하나마다 세 가지를 봅니다.
     - 숫자 보존: 원문의 금액·나이·기간이 번역에 그대로 있는가 (코드 검사, 서비스의 verified)
     - 언어 정확: 고른 언어로 나왔는가, 한국어를 그대로 돌려주지 않았는가 (코드 검사)
     - 의미 보존: 원문과 뜻이 같은가 (Solar가 1~5점으로 채점, 4점 이상 = 통과)
  4. 셋 다 통과한 비율을 "번역 통과율"로 계산해 translation_eval_result.md / .csv 에 저장합니다.

주의: 의미 점수는 같은 Solar 모델이 채점하므로 참고용입니다. csv의 human 칸에 사람이 직접 O/X를 적으면
      사람 평가 통과율도 같이 낼 수 있습니다(외국어 가능한 팀원 1명이 10분이면 됩니다).
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
import i18n_answer  # noqa: E402

BASE = os.environ.get("LAWDDY_BASE", "http://127.0.0.1:8000")
LANGS = ["en", "zh-CN", "zh-TW", "ja"]
QUESTIONS = [
    "1급지 노상주차장 요금은 5분에 얼마예요?",
    "경차는 공영주차장 요금이 할인되나요?",
    "장애인 할인과 다둥이 할인, 둘 다 받을 수 있나요?",
    "금연구역에서 담배 피우면 과태료가 얼마예요?",
    "공원에서 담배 피워도 되나요?",
    "청년은 몇 살부터 몇 살까지예요?",
    "군대 다녀오면 청년 나이 기준이 달라지나요?",
    "어린이공원에서 술 마셔도 되나요?",
    "반려견 산책할 때 꼭 지켜야 할 게 뭐예요?",
    "맹견은 어린이공원에 들어갈 수 있나요?",
    "자전거주차장은 돈을 내야 하나요?",
    "헬멧 안 쓰면 과태료가 있나요?",
]
SESSION = requests.Session()
SESSION.headers["Content-Type"] = "application/json"


def ask(question: str) -> tuple[str | None, str | None]:
    body = SESSION.post(BASE + "/ui/ask", data=json.dumps({"question": question}), timeout=60).json()
    for _ in range(80):
        if body.get("status") == "completed":
            answer = body.get("answer")
            return body.get("job_id"), answer if isinstance(answer, str) else None
        if body.get("status") in ("failed", "cancelled"):
            return None, None
        time.sleep(3)
        body = SESSION.get(BASE + body["poll_url"], timeout=60).json()
    return None, None


def translate(job_id: str, lang: str) -> dict:
    body = SESSION.post(BASE + "/ui/translate-answer", data=json.dumps({"job_id": job_id, "lang": lang}), timeout=120).json()
    return body.get("answer") or {}


def right_language(text: str, lang: str) -> bool:
    if i18n_answer.still_korean(text):
        return False
    found = i18n_answer.detect_lang(text)
    if lang.startswith("zh"):
        return found in ("zh-CN", "zh-TW")
    return found == lang


def judge(chat, korean: str, translated: str, lang: str) -> tuple[int, str]:
    payload = {
        "model": i18n_answer.CHAT_MODEL,
        "messages": [
            {"role": "system", "content": (
                "You are a strict bilingual reviewer. Compare a Korean original with its "
                f"{i18n_answer.LANGS[lang]} translation. Score meaning preservation 1-5: 5 = same meaning, "
                "nothing added or lost; 4 = same meaning, minor wording issues; 3 = small meaning change; "
                "2 = important information lost or wrong; 1 = wrong. Return JSON {\"score\": n, \"issue\": \"...\"} "
                "with issue in Korean, empty if none.")},
            {"role": "user", "content": f"[Korean original]\n{korean}\n\n[Translation]\n{translated}"},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "translation_review", "strict": True,
            "schema": {"type": "object", "properties": {"score": {"type": "integer"}, "issue": {"type": "string"}},
                       "required": ["score", "issue"], "additionalProperties": False}}},
    }
    try:
        reply = json.loads(chat(payload, 60)["choices"][0]["message"]["content"])
        return max(1, min(5, int(reply["score"]))), str(reply.get("issue", ""))
    except Exception as error:  # 채점 실패는 따로 표시
        return 0, f"채점 실패: {error}"


def main() -> None:
    try:
        SESSION.get(BASE + "/health", timeout=5).raise_for_status()
    except Exception:
        sys.exit("서버가 꺼져 있어요. 먼저 run.py serve --port 8000 으로 켜 주세요.")
    chat = i18n_answer.upstage_chat(ROOT)

    print("1/3 한국어 답 받는 중…")
    answers = []
    for question in QUESTIONS:
        job_id, answer = ask(question)
        print(("  ✓ " if answer else "  · (답 없음) ") + question)
        if job_id and answer:
            answers.append((question, job_id, answer))

    print(f"2/3 번역 중… ({len(answers)}개 답 × {len(LANGS)}개 언어)")
    jobs = [(q, j, a, lang) for q, j, a in answers for lang in LANGS]
    with ThreadPoolExecutor(4) as pool:
        translations = list(pool.map(lambda item: translate(item[1], item[3]), jobs))

    print("3/3 의미 채점 중…")
    def review(index: int) -> dict:
        question, _job, korean, lang = jobs[index]
        result = translations[index]
        text = result.get("text") or ""
        numbers = result.get("verified") is True
        language = numbers and right_language(text, lang)
        if language:
            score, issue = judge(chat, korean, text, lang)
        else:
            score, issue = 0, ("숫자가 달라져 한국어 원문 표시" if not numbers else "고른 언어로 번역되지 않음")
        return {"question": question, "lang": lang, "numbers_kept": numbers, "right_language": language,
                "meaning_score": score, "pass": numbers and language and score >= 4,
                "issue": issue, "korean": korean, "translation": text, "human": ""}
    with ThreadPoolExecutor(4) as pool:
        rows = list(pool.map(review, range(len(jobs))))

    total = len(rows) or 1
    def rate(key):
        return 100 * sum(1 for row in rows if row[key]) / total
    scores = [row["meaning_score"] for row in rows if row["meaning_score"]]
    lines = [
        "# Lawddy 번역 품질 자체 평가", "",
        f"- 표본: 예시 질문 {len(answers)}개의 답 × {len(LANGS)}개 언어 = {len(rows)}건",
        f"- 숫자 보존율: {rate('numbers_kept'):.1f}%",
        f"- 언어 정확률: {rate('right_language'):.1f}%",
        f"- 의미 점수 평균: {sum(scores) / max(1, len(scores)):.2f} / 5 (Solar 채점, 참고용)",
        f"- **번역 통과율 (세 가지 모두 통과): {rate('pass'):.1f}%**", "",
        "| 언어 | 건수 | 통과율 |", "|---|---|---|",
    ]
    for lang in LANGS:
        part = [row for row in rows if row["lang"] == lang]
        lines.append(f"| {lang} | {len(part)} | {100 * sum(r['pass'] for r in part) / max(1, len(part)):.1f}% |")
    lines += ["", "## 통과 못 한 번역", ""]
    for row in rows:
        if not row["pass"]:
            lines.append(f"- [{row['lang']}] {row['question']} — 점수 {row['meaning_score']}: {row['issue']}")
    (ROOT / "translation_eval_result.md").write_text("\n".join(lines), encoding="utf-8")
    with open(ROOT / "translation_eval_result.csv", "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()) if rows else ["question"])
        writer.writeheader()
        writer.writerows(rows)
    print("\n".join(lines[:8]))
    print("\n결과 파일: translation_eval_result.md, translation_eval_result.csv")


if __name__ == "__main__":
    main()
