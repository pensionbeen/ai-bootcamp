"""Resolve time-of-day public parking tariffs from a complete, source-linked tier table."""

import re


_GRADE = r"([1-9]\d*)급지"
_PERIOD = r"야간|주간|전일"
_WORD = (rf"서울특별시|서울시|공영(?:노상|노외)?주차장|노상(?:주차장)?|노외(?:주차장)?|"
         rf"[1-9]\d*급지|{_PERIOD}|월정기권|1일주차권|1일권|주차요금|이용요금|요금|가격")
_TOKEN = re.compile(rf"({_WORD})(?:에서는|에서|에는|의|에|은|는|이|가|을|를)?(?={_WORD}|$)")
_ENDING = re.compile(r"(?:얼마(?:인가요|예요|에요|입니까|야|인지알려주세요)?|알려주세요|알려줘)[?？.]?$")
_TITLE = re.compile(r"^\s*공영주차장\s*(?:주차)?요금표\s*\(\s*(제\s*\d+\s*조(?:의\d+)?(?:\s*제\s*\d+\s*항)?)\s*관련\s*\)", re.M)
_UNIT = re.compile(r"단위\s*[:：]\s*(천원|만원|원)\s*[-/]\s*(?:1\s*)?(구획|대)(?:당)?\s*\)")
_AMOUNT = re.compile(r"(?:0|[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)")


def _query(question):
    q = "".join(question.split())
    ending = _ENDING.search(q)
    if not ending:
        return None
    tokens = []
    pos = 0
    while pos < ending.start():
        token = _TOKEN.match(q, pos, ending.start())
        if not token:
            return None
        tokens.append(token[1])
        pos = token.end()
    if not any(token.startswith("공영") for token in tokens):
        return None
    grades = {int(t.removesuffix("급지")) for t in tokens if t.endswith("급지")}
    facilities = {t.removeprefix("공영").removesuffix("주차장") for t in tokens if "노상" in t or "노외" in t}
    periods = {t for t in tokens if re.fullmatch(_PERIOD, t)}
    passes = {t.replace("1일권", "1일주차권") for t in tokens if t.endswith("권")}
    if len(periods) != 1 or any(len(x) > 1 for x in (grades, facilities, passes)):
        return None
    return next(iter(grades), None), next(iter(facilities), None), periods.pop(), next(iter(passes), None)


def _key(row):
    return row["ordinance"], row["source_url"], row["effective_date"], row["public_label"]


def _head_context(row, by_id):
    dependencies = [by_id.get(i) for i in row["required_ids"]]
    if any(d is None or _key(d) != _key(row) or d["owner_id"] != row["owner_id"] for d in dependencies):
        return None
    headings = [d for d in dependencies if _TITLE.search(d["quote"])]
    if not headings or len({h["quote"].strip() for h in headings}) != 1:
        return None
    head = headings[0]
    unit_lines = [line for d in dependencies for line in d["quote"].splitlines()
                  if re.search(r"단위\s*[:：]", line)]
    if any(not _UNIT.search(line) for line in unit_lines):
        return None
    units = {u.groups() for line in unit_lines for u in _UNIT.finditer(line)}
    # The single heading we publish must itself substantiate the currency and denominator.
    if len(units) != 1 or not _UNIT.search(head["quote"]):
        return None
    return head, units.pop()


def _columns(lines, facility, period, ticket, scale):
    values = {}
    for line in lines:
        key, separator, number = re.sub(r"\s+", "", line).partition(":")
        if not separator or ">" not in key:
            return None
        parts = key.split(">")
        if len(parts) < 2 or not re.fullmatch(r"(?:노상|노외)주차장", parts[0]):
            return None
        if period not in key or (facility and parts[0] != facility + "주차장"):
            continue
        if len(parts) == 2:
            term = re.fullmatch(rf"(1일주차권|월정기권)\({period}(?:에한함)?\)", parts[1])
        else:
            term = (re.fullmatch(r"(1일주차권|월정기권)", parts[1])
                    if len(parts) == 3 and parts[2] == period else None)
        if not term:
            return None
        label = (parts[0], term[1])
        if ticket and ticket != term[1]:
            continue
        if label in values or not _AMOUNT.fullmatch(number):
            return None
        values[label] = int(number.replace(",", "")) * scale
    return values or None


def _authority(rows, key, heading):
    locator = "".join(_TITLE.search(heading)[1].split())
    annex = key[-1].removeprefix(key[0]).strip()
    if not re.fullmatch(r"별표(?:\s*\d+(?:의\d+)?)?", annex):
        return None
    printed = re.findall(r"\[\s*(별\s*표(?:\s*\d+(?:의\d+)?)?)\s*\]", heading)
    if any("".join(p.split()) != "".join(annex.split()) for p in printed):
        return None
    candidates = [r for r in rows if _key(r)[:3] == key[:3] and "".join(r["locator"].split()) == locator]
    if len(candidates) != 1:
        return None
    row = candidates[0]
    # Do not interpret a fee ceiling, delegation, or exception as a fixed tariff.
    primary, _, exception = "".join(row["quote"].split()).partition("다만")
    if exception and not re.fullmatch(r"[,，]?[가-힣]{2,12}주차구획의주차요금은[가-힣·ㆍ]{0,60}(?:시장|구청장)이"
                                      r"따로정하는경우에는그러하지아니하다\.(?:<개정[^>]+>)?", exception):
        return None
    direct = "공영주차장" in primary and re.search(r"요금은" + re.escape("".join(annex.split())) + r"[과와]같다\.", primary)
    return row if direct else None


def tariff_evidence(question, rows):
    """Return directly applicable IDs and amounts, else defer to the general answer path."""
    request = _query(question)
    if request is None:
        return None
    grade, facility, period, ticket = request
    by_id = {r["id"]: r for r in rows}
    if len(by_id) != len(rows):
        return None
    data, unknown_annexes = [], set()
    for row in rows:
        if row["unit_id"] != row["owner_id"] or not row["locator"].startswith("별표"):
            continue
        lines = row["quote"].strip().splitlines()
        if not lines:
            continue
        label = re.fullmatch(r"구\s*분\s*[:：]\s*" + _GRADE, lines[0].strip())
        if label:
            data.append((int(label[1]), row, lines[1:]))
        else:
            unknown_annexes.add(_key(row))
    if not data or len({_key(r) for _, r, _ in data}) != 1:
        return None
    key = _key(data[0][1])
    if not all(key) or key in unknown_annexes:
        return None
    grades = {}
    heads = {}
    for tier, row, lines in data:
        context = _head_context(row, by_id)
        if context is None:
            return None
        head, (currency, _) = context
        heads[head["quote"].strip()] = head
        if grade and tier != grade:
            continue
        values = _columns(lines, facility, period, ticket, {"원": 1, "천원": 1000, "만원": 10000}[currency])
        if values is None:
            return None
        if tier in grades:
            old = grades[tier][0]
            if old["unit_id"] == row["unit_id"] and old["quote"] == row["quote"]:
                continue
            return None
        grades[tier] = row, values
    if len(heads) != 1 or not grades:
        return None
    heading, head = next(iter(heads.items()))
    declared = {int(n) for n in re.findall(r"^\s*(?:[가-힣][.)]\s*)?" + _GRADE + r"\s*[:：]", heading, re.M)}
    if (len(declared) < 2 or not set(grades) <= declared or (grade is None and set(grades) != declared)):
        return None
    anchor = _authority(rows, key, heading)
    labels = next(iter(grades.values()))[1].keys()
    if not anchor or any(values.keys() != labels for _, values in grades.values()):
        return None
    # A table contingency may refer one product to another. Only quote columns the
    # table directly prices. Defer a specifically requested contingent product.
    related_ticket = re.search(r"(?m)^\s*(?:\d+\.\s*)?(노상주차장의 월 정기권은 [^.\n]*"
                               r"노외주차장 월 정기권 요금을 따른다)\.", heading)
    if facility == "노상" and ticket != "1일주차권" and related_ticket:
        return None
    # Leave an unfamiliar price-changing condition to the general answering path.
    for line in heading.splitlines():
        if ((period in line or "정기권" in line or "주차권" in line)
                and re.search(r"할증|할인|면제|무료|제외|배로|%|분의|천원|\d\s*원", line)):
            return None
    scale_name = _UNIT.search(heading)[2]
    vehicle = "승용차 " if "1구획은 승용자동차를 기준" in heading else ""
    lines = [f"공영주차장 {period} 요금표의 {vehicle}1{scale_name}당 기준액입니다."]
    cells = []
    for kind, term in labels:
        amounts = []
        for tier in sorted(grades):
            row, values = grades[tier]
            price = values[kind, term]
            amounts.append(f"{tier}급지 {price:,}원")
            cells.append({"tier": tier, "facility": kind, "ticket": term, "period": period,
                          "won": price, "id": row["id"]})
        lines.append(f"{kind} {term.replace('월정기권', '월 정기권')}은 " + ", ".join(amounts) + "입니다.")
    adjustment = re.search(r"^※\s*(개별주차장 요금은 [^.\n]+산정한다)\.", heading, re.M)
    if adjustment:
        lines.append(adjustment[1].removesuffix("한다") + "합니다.")
    if not facility and ticket != "1일주차권" and related_ticket:
        lines.append(related_ticket[1].removesuffix("따른다") + "따릅니다.")
    ids = [grades[t][0]["id"] for t in sorted(grades)]
    # Use one equivalent attached heading, belonging to a row that was actually selected.
    head = _head_context(grades[min(grades)][0], by_id)[0]
    support = [head["id"], anchor["id"]]
    return {"kind": "tiered_tariff", "data_ids": ids, "ids": ids + support, "used_ids": ids + support, "cells": cells,
            "answer": " ".join(lines)}
