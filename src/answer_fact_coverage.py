"""Offline review of two narrowly identifiable source facts in a rendered answer."""

import re
from decimal import Decimal
from fractions import Fraction

import complete_selected_evidence as selection
from rag_numeric_amounts import won_literals


_PERCENT = re.compile(r"(?<![\d.])(?P<n>\d+(?:\.\d+)?)\s*(?:분의\s*(?P<part>\d+(?:\.\d+)?)|[%％]|퍼센트)")
_SENTENCE = re.compile(r"[,;!?。\n]|\.(?=\s|$)")
_DENIED = re.compile(r"\s*(?:이|가|은|는|를)?\s*(?:아닌|아니|아닙|아님|(?:할인|감면|적용)(?:되지|하지)\s*않)")
_SCALES = {"원": 1, "백원": 100, "천원": 1000, "만원": 10000, "백만원": 1000000, "억원": 100000000}


def _rates(text):
    for m in _PERCENT.finditer(text):
        if m["part"] is None:
            value = Fraction(m["n"]) / 100
        elif Fraction(m["n"]):
            value = Fraction(m["part"]) / Fraction(m["n"])
        else:
            continue
        yield m.start(), m.end(), value


def _phrase(text):
    return r"\s*".join(re.escape(word) for word in text.split())


def _mentions(answer, patterns, values):
    """Associate the nearest unobstructed subject or fee label with one displayed value."""
    spans = sorted((m.start(), m.end(), label) for label, pattern in patterns.items()
                   for m in pattern.finditer(answer))
    pairs = set()
    last_end = 0
    for start, end, value in values:
        if _DENIED.match(answer[end:]):
            last_end = end
            continue
        before = [s for s in spans if last_end <= s[1] <= start]
        if before:
            _, finish, label = max(before, key=lambda span: span[1])
            gap = answer[finish:start]
            if len(gap) <= 70 and "각각" not in gap and not _SENTENCE.search(gap):
                pairs.add((label, value))
        after = [s for s in spans if s[0] >= end]
        if after:
            begin, _, label = after[0]
            if re.fullmatch(r"\s*(?:인|에\s*해당하는|[/／(])\s*", answer[end:begin]):
                pairs.add((label, value))
        last_end = end
    return pairs


def _discount_gaps(question, rows, picked, answer, used):
    companions = selection._comparison_companions(question, rows, picked)
    if not companions:
        return []
    names = [selection._BENEFIT.sub("", name.strip()).strip() for name in selection._PAIR.search(question).groups()]
    expected = []
    for row in rows:
        if row["id"] not in companions:
            continue
        text = re.sub(r"^\s*\d+(?:의\d+)?[.)]\s*", "", row["quote"])
        clause = re.split(r"\n|[.!?;](?=\s|$)|다만", text, maxsplit=1)[0]
        rates = {value for _, _, value in _rates(clause)}
        subjects = [name for name in names if selection._has_name(clause, name)]
        if len(rates) != 1 or len(subjects) != 1:
            return []
        expected.append((subjects[0], rates.pop(), row["id"]))
    patterns = {name: re.compile(r"(?<!\w)" + _phrase(name)
                                 + r"(?=(?:은|는|의|을|를|이|가|에게|과|와|할인|감면|\W|$))") for name in names}
    present = _mentions(answer, patterns, _rates(answer))
    # "A와 B는 각각 X%, Y%" assigns two values in order without repeating each name.
    for match in re.finditer(r"각각\s*", answer):
        previous = re.split(r"[.;!?。\n]", answer[:match.start()])[-1]
        ordered = sorted((m.start(), name) for name, pattern in patterns.items() for m in pattern.finditer(previous))
        after = re.split(r"[;!?。\n]|\.(?=\s|$)", answer[match.end():], maxsplit=1)[0]
        rates = list(_rates(after))
        if (len(ordered) == len(rates) == 2 and ordered[0][1] != ordered[1][1]
                and not after[:rates[0][0]].strip()
                and re.fullmatch(r"\s*(?:,|와|과|및|·)\s*", after[rates[0][1]:rates[1][0]])
                and not _DENIED.match(after[rates[1][1]:])):
            present.update((subject[1], rate[2]) for subject, rate in zip(ordered, rates))
    gaps = []
    for name, rate, row_id in expected:
        if (name, rate) not in present:
            gaps.append(f"대상별 할인율 확인 필요 · {name} ({row_id})")
        elif row_id not in used:
            gaps.append(f"할인율 사용 근거 누락 · {name} ({row_id})")
    return gaps


def _period(field):
    normalized = re.sub(r"\s+", "", field)
    # An explicit period in parentheses can be shown without the table's longer column heading.
    alternatives = [_phrase(field)]
    interval = re.search(r"(\d*)(시간|분|일|개월|주)당", normalized)
    if interval:
        count, unit = interval.groups()
        prefix = r"(?:1\s*)?" if count in ("", "1") else re.escape(count) + r"\s*"
        alternatives.append(r"(?<!\d)" + prefix + unit + r"\s*당")
    if re.fullmatch(r"(?:1일|일일|하루)(?:주차|이용)?권", normalized):
        alternatives.append(r"(?:1\s*일|일일|하루)\s*(?:주차|이용)?\s*권|(?<![가-힣])일권")
    if re.fullmatch(r"월(?:정기|주차|이용)?권", normalized):
        alternatives.append(r"월\s*(?:정기|주차|이용)?\s*권|한\s*달")
    return re.compile("|".join(alternatives))


def _fee_gaps(question, rows, picked, answer, used):
    context_ids = {i for row in picked for i in row["required_ids"]}
    companions = selection._amount_companions(question, rows, picked + [r for r in rows if r["id"] in context_ids])
    if not companions:
        return []
    row = next(r for r in rows if r["id"] in companions)
    units = {re.split(r"[:：]", m.group())[-1].strip()
             for r in rows if r["id"] in row["required_ids"] and r["owner_id"] == row["owner_id"]
             and selection._source(r) == selection._source(row) and r["public_label"] == row["public_label"]
             for m in selection._UNIT.finditer(r["quote"])}
    if len(units) != 1:
        return []
    scale = _SCALES[units.pop()]
    expected = {}
    for m in selection._VALUE.finditer(row["quote"]):
        field = m["field"].strip()
        if not (m["won"] or selection._PRICE_FIELD.search(field)):
            continue
        number = re.split(r"[:：]", m[0])[-1].strip().removesuffix("원").strip().replace(",", "")
        if field in expected:
            return []
        expected[field] = Decimal(number) * (1 if m["won"] else scale)
    answer = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", answer)
    answer = re.sub(r"(?<=\d)\s+(?=(?:시간|분|일|개월|주)당)", "", answer)
    values = [(m["start"], m["end"], Decimal(m["value"])) for m in won_literals(answer) if m["value"] is not None]
    pairs = _mentions(answer, {field: _period(field) for field in expected}, values)
    gaps = [f"구분별 금액·단위 확인 필요 · {field} ({row['id']})"
            for field, value in expected.items() if (field, value) not in pairs]
    if row["id"] not in used:
        gaps.append(f"요금표 데이터 행 사용 근거 누락 · {row['id']}")
    return gaps


def missing_material_evidence(question, rows, selected_ids, answer, used_ids):
    """Return review reasons. An empty list includes cases outside these two checks."""
    selected = set(selected_ids)
    picked = [row for row in rows if row["id"] in selected]
    used = set(used_ids)
    return _discount_gaps(question, rows, picked, answer, used) + _fee_gaps(question, rows, picked, answer, used)
