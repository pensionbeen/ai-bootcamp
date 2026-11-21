"""조문 원문 보기: 끝난 답의 근거 조항마다 검색 DB에 들어 있는 조례 원문을 꺼냅니다. POST /ui/sources

- 새 자료를 모으지 않고, 서비스가 이미 쓰는 검색 DB(조례 원문)를 읽기 전용으로 엽니다.
- 그 질문의 답에 실제로 인용된 조항만 꺼냅니다(임의 조문 조회 아님).
- 화면은 답에 나온 숫자·금액을 원문에서 형광펜으로 표시해, 답이 원문 그대로임을 보여 줍니다.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

TEXT_MAX = 2500
NOTE_MARKERS = ("[함께 확인할 조문 말미 주석]", "[필수 첨부 문맥]")
UNIT = re.compile(r"\s+(?:제\d+(?:의\d+)?(?:조|항|호|목)|[가-하]목)$")


def database_path(project) -> Path:
    from ask_local_rag import DEFAULT_DATABASE
    return Path(project) / DEFAULT_DATABASE


def _clean(text: str) -> str:
    for marker in NOTE_MARKERS:
        if marker in text:
            text = text[: text.index(marker)]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= TEXT_MAX else text[: TEXT_MAX - 1] + "…"


def _body_rows(db: sqlite3.Connection, label: str) -> list[tuple[str, str]]:
    """label('…조례 제10조 제1항')로 시작하는 본문 조각. 없으면 끝 단위(호·항)를 떼고 다시 찾습니다."""
    current = label.strip()
    while current:
        rows = db.execute(
            "SELECT title, text FROM chunks WHERE kind = 'body' AND is_deleted = 0 "
            "AND (title = ? OR title LIKE ? ESCAPE '\\') ORDER BY chunk_id LIMIT 6",
            (current, current.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + " %"),
        ).fetchall()
        if rows:
            return rows
        shorter = UNIT.sub("", current)
        if shorter == current:
            return []
        current = shorter
    return []


def _attachment_rows(db: sqlite3.Connection, label: str) -> list[tuple[str, str]]:
    match = re.match(r"^(.*?(?:조례|조례안))\s+별표\s*(\d+(?:의\d+)?)?", label)
    if not match:
        return []
    law, number = match.groups()
    rows = db.execute("SELECT title, text FROM chunks WHERE kind = 'attachment' AND is_deleted = 0 AND title = ? "
                      "ORDER BY chunk_id", (law,)).fetchall()
    if number:
        pattern = re.compile(r"별표\s*" + re.escape(number) + r"(?![\d의])")
        rows = [row for row in rows if pattern.search(row[1])]
    return rows[:6]


def lookup(project, labels: list[str]) -> list[dict[str, Any]]:
    path = database_path(project)
    if not path.is_file():
        return [{"citation": label, "found": False} for label in labels]
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = []
        for label in labels:
            rows = _attachment_rows(db, label) if "별표" in label else _body_rows(db, label)
            if not rows:
                out.append({"citation": label, "found": False})
                continue
            parts, seen = [], set()
            for _title, text in rows:
                cleaned = _clean(text)
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    parts.append(cleaned)
            text = _clean("\n\n".join(parts))
            out.append({"citation": label, "found": True, "text": text})
        return out
    finally:
        db.close()
