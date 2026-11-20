"""Read the referenced tariff and period from a single applicable occupancy-fee row."""

from datetime import date
import re


_QUESTION = re.compile(
    r"(?:(?P<city>서울시|서울특별시) )?(?:(?P<place>도시공원|공원|녹지)(?:에|에서| 내에) )?"
    r"(?P<name>[가-힣A-Za-z0-9]{2,30})(?:을|를|의)?(?: (?:설치할 때|설치하면|두면))? "
    r"(?:(?P<period>연간|월간|일간) )?점용료(?:은|는|가|를|의)? "
    r"(?:(?P<amount>얼마(?:인가요|예요|에요|입니까)?|금액을 알려주세요)|"
    r"어떻게 정(?:하나요|해지나요)|어떤 기준으로 정(?:하나요|해지나요)|어떤 규정을 따르나요)\s*[?？.]?"
)
_FIELD = re.compile(r"^([^:\n]+):\s*(.+)$")
_NAME = re.compile(r"([가-힣A-Za-z0-9 ]+?)(?:\s*\(([가-힣A-Za-z0-9 ]+)\))?")
_UNIT = re.compile(r"(?:(연액|월액|일액)|〃\s*\[동일 열 앞 셀:\s*(연액|월액|일액)\])")
_TITLE = re.compile(r"\[(별표\s*\d+(?:의\d+)?)\]\s*([가-힣 ]{1,36}점용료)\s*"
                    r"\(\s*(제\s*\d+\s*조(?:의\d+)?(?:\s*제\s*\d+\s*항)?)\s*관련\s*\)"
                    r"(?:\s*<개정\s*\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.?>)?")
_LAW = r"(?:「[가-힣·ㆍ ]{1,40}(?:법|법률|시행령|시행규칙|조례|규칙)」|[가-힣]{1,24}법(?: 시행령| 시행규칙)?)"
_REFERENCE = re.compile(
    rf"(?P<doc>(?P<law>{_LAW})\s+제\s*\d+\s*조(?:의\s*\d+)?(?:\s*제\s*\d+\s*항)?"
    r"(?:\s*제\s*\d+\s*호)?(?:\s*\[별표\s*\d+(?:의\d+)?\]|\s*관련\s*[가-힣·ㆍ ]{1,28}(?:요율|요금)\s*공고)?)\s*준용"
)
_EARLIER = re.compile(
    r"법 시행령 개정 전\((?P<year>\d{4})\.\s*(?P<month>\d{1,2})\.\s*(?P<day>\d{1,2})\.\) "
    r"(?P<name>[가-힣A-Za-z0-9 ]{2,30}) 점용허가 사례는 최초 허가시점의 점용료 산정기준에 따라 부과한다\.?"
)
_PERIOD = {"연액": "연간", "월액": "월간", "일액": "일간"}


def _named_rows(rows, name):
    matches = []
    for row in rows:
        if row["unit_id"] != row["owner_id"] or not row["locator"].startswith("별표"):
            continue
        fields = []
        for line in row["quote"].strip().splitlines():
            m = _FIELD.fullmatch(line)
            fields.append(("".join(m[1].split()), m[2].strip()) if m else ("", line))
        labels = [v for k, v in fields if k == "점용대상"]
        names = _NAME.fullmatch(labels[-1]) if labels else None
        if not names or name not in {n.strip() for n in names.groups() if n}:
            if any(re.search(r"(?<![가-힣A-Za-z0-9])" + re.escape(name) + r"(?![가-힣A-Za-z0-9])", v) for v in labels):
                return []
            continue
        if [k for k, _ in fields] != ["점용대상"] * len(labels) + ["단위기준", "점용료"]:
            return []
        matches.append((row, {n.strip() for n in names.groups() if n}, fields[-2][1], fields[-1][1]))
    return matches


def _context(row, rows, unit, ditto, place):
    by_id = {r["id"]: r for r in rows}
    if len(by_id) != len(rows):
        return None
    annex = row["public_label"].removeprefix(row["ordinance"]).strip()
    key = ("owner_id", "ordinance", "source_url", "effective_date", "public_label")
    if not all(row[k] for k in key) or not re.fullmatch(r"별표\s*\d+(?:의\d+)?", annex):
        return None
    heading = marker = None
    for required in row["required_ids"]:
        dep = by_id.get(required)
        if not dep or any(dep[k] != row[k] for k in key) or dep["unit_id"] == row["unit_id"]:
            return None
        text = dep["quote"].strip()
        title = _TITLE.fullmatch(text)
        if title and "".join(title[1].split()) == "".join(annex.split()):
            if heading and heading["quote"].strip() != text:
                return None
            if place and place.removeprefix("도시") not in title[2]:
                return None
            heading = heading or dep
        elif ditto and text in (
                f"단위기준 〃은 같은 열의 앞 셀 '{unit}'을 가리킨다.",
                f"[〃의 선행 원문 셀: 이 단위만 참조하며 선행 행의 요율은 가져오지 않음]\n단위기준: {unit}"):
            marker = dep
        else:
            return None
    if not heading or (ditto and not marker):
        return None
    return [heading["id"]] + ([marker["id"]] if marker else [])


def delegated_fee_answer(question, rows):
    """Return a source-bound tariff reference, preserving recognized permit-date exceptions."""
    query = _QUESTION.fullmatch(" ".join(question.split()))
    if not query:
        return None
    # Remove only a case particle consumed by the name pattern before a Korean verb.
    name = query["name"]
    targets = _named_rows(rows, name)
    if name.endswith(("을", "를", "의")):
        other = _named_rows(rows, name[:-1])
        if targets and other:
            return None
        if other:
            name, targets = name[:-1], other
    if len(targets) != 1:
        return None
    row, aliases, unit_text, source = targets[0]
    if (name in {"기타", "시설", "설비", "점용대상", "해당시설"}
            or (query["city"] and not row["ordinance"].startswith("서울특별시 "))
            or (query["place"] == "도시공원" and "도시공원" not in row["ordinance"])):
        return None
    # An additional provision about this same facility can qualify the table.
    # Keep that question for the general reader instead of silently dropping it.
    if any(r is not row and any(alias in r["quote"] for alias in aliases)
           for r in rows if r["source_url"] == row["source_url"] and r["effective_date"] == row["effective_date"]):
        return None
    unit_match = _UNIT.fullmatch(unit_text)
    if not unit_match:
        return None
    literal, preceding = unit_match.groups()
    period = _PERIOD[literal or preceding]
    if query["period"] and query["period"] != period:
        return None
    support = _context(row, rows, literal or preceding, bool(preceding), query["place"])
    reference, separator, note = source.partition("※")
    parsed = _REFERENCE.fullmatch(reference.strip())
    if (not support or not parsed or parsed["law"].strip("「」") == row["ordinance"]
            or re.search(r"다만|제외|경우|한하여|조건부", reference)):
        return None
    answer = f"{name}의 {period} 점용료는 {parsed['doc']}의 기준을 준용해 정합니다."
    if separator:
        older = _EARLIER.fullmatch(note.strip())
        if not older or older["name"] not in aliases or "시행령" not in parsed["law"]:
            return None
        try:
            amendment = date(int(older["year"]), int(older["month"]), int(older["day"]))
        except ValueError:
            return None
        answer += (f" 다만 {amendment.year}년 {amendment.month}월 {amendment.day}일 개정 전에 점용허가를 "
                   "받은 경우에는 최초 허가시점의 점용료 산정기준을 적용합니다.")
    ids = [row["id"], *support]
    return {"kind": "delegated_fee", "ids": ids, "used_ids": list(ids), "answer": answer,
            "missing_materials": [parsed["doc"]] if query["amount"] else []}
