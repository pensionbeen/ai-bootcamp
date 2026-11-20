"""Render a selected provision only when its full operative wording fits a known structure."""

import re
from fractions import Fraction

from benefit_selection import _TERM


_LOC = r"제\s*\d+\s*조(?:의\d+)?(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?"
_NAME = r"[가-힣]{2,24}"
_VALUE = r"(?:\d+(?:\.\d+)?\s*분의\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*%)"
_TARGET = re.compile(
    rf"(?:「(?P<law>[가-힣 ]+)」\s*{_LOC}(?:\s*및 같은 법 (?P<rule>시행규칙|시행령)\s*{_LOC})?에 따른 )?"
    rf"(?P<name>{_NAME})"
)
_CAR_BENEFIT = re.compile(
    rf"(?P<actor>시장|구청장)은 (?P<targets>.+?)에 대하여 (?P<fee>{_NAME}요금)의 "
    rf"(?P<ordinary>{_VALUE})[을를] 할인(?P<grant>할 수 있다|한다)\.\s*다만,?\s*"
    rf"(?P<place>{_NAME}(?: {_NAME}){{0,2}})에서 (?P<purpose>{_NAME})\s*목적으로 주차하는 "
    rf"(?P<special>{_NAME}(?:[와과] {_NAME})*)에 대해서는 (?P=fee)의 (?P<extra>{_VALUE})[을를] 할인하되,? "
    r"(?P<frequency>\d+일 \d+회) 주차의 경우에는 최초 (?P<duration>\d+(?:시간|분))까지의 "
    r"(?P=fee)[을를] 면제한다\."
)
_FREE = re.compile(
    rf"법 (?P<loc>{_LOC})의 규정에 의하여 설치된 (?P<site>{_NAME})의 (?P<fee>{_NAME}요금)은 무료로 한다\. "
    rf"다만,? (?P=site)의 관리[·ㆍ]운영을 (?P<agent>{_NAME})(?:\(이하 [\"“]{_NAME}[\"”]라 한다\))? 등에 "
    r"위탁하는 등 특별한 사유가 있는 경우에는 (?P=fee)[을를] 별표의 범위 내에서 규칙으로 정하여 부과할 수 있다\."
)
_EXEMPT = re.compile(
    rf"법 (?P<loc>{_LOC})의 규정에 의하여 (?P<site>{_NAME})[을를] 설치하여 (?P<fund>[가-힣]{{1,12}}비)[을를] "
    rf"지원받은 경우에는 그 (?P<fee>{_NAME}요금)[을를] 무료로 하여야 한다\."
)


def _body(quote):
    text = re.sub(r"^\s*(?:[①-⑳]|\d+(?:의\d+)?[.)])\s*", "", quote.strip())
    text = re.sub(r"\s*<개정[\d.,\s]+>\s*$", "", text)
    return " ".join(text.split()).replace(".다만", ". 다만")


def _percent(raw):
    parts = raw.replace(" ", "").removesuffix("%").split("분의")
    if len(parts) == 2 and Fraction(parts[0]) == 0:
        return None
    rate = Fraction(parts[0]) if len(parts) == 1 else 100 * Fraction(parts[1]) / Fraction(parts[0])
    scaled = rate * 1000
    if not 0 < rate <= 100 or scaled.denominator != 1:
        return None
    whole, decimals = divmod(scaled.numerator, 1000)
    return f"{whole}.{decimals:03d}".rstrip("0").rstrip(".") + "%"


def _benefit(question, plan, selected):
    if len(selected) != 1:
        return None
    original = _body(selected[0]["quote"])
    match = _CAR_BENEFIT.fullmatch(original)
    if not match or original != plan["answer"]:
        return None
    subjects = [_TARGET.fullmatch(s) for s in re.split(r"[와과] (?=(?:「|[가-힣]))", match["targets"])]
    if not all(subjects):
        return None
    # A modifier such as '등록증을 제시한', or a mismatch between the ordinary and
    # transfer recipients, leaves the provision verbatim.
    names = [s["name"] for s in subjects]
    if len(set(names)) != len(names) or set(names) != set(re.split(r"[와과] ", match["special"])):
        return None
    target = plan.get("source_subject")
    ask = plan.get("subject")
    if target not in names or not ask or ask not in question or _TERM.get(ask, ask) != target:
        return None
    recipient = subjects[names.index(target)]
    label = target if ask == target else f"{target}({ask})"
    if recipient["law"]:
        law = "「" + recipient["law"] + "」"
        if recipient["rule"]:
            law += " 및 같은 법 " + recipient["rule"]
        label = f"{law}에 따른 {label}"
    ordinary, extra = _percent(match["ordinary"]), _percent(match["extra"])
    if not ordinary or not extra:
        return None
    fee = match["fee"]
    if "공영주차장" in question:
        fee = "공영주차장 " + ("요금" if fee == "주차요금" else fee)
    grant = "할인할 수 있습니다" if match["grant"] == "할 수 있다" else "할인합니다"
    return (f"{match['actor']}은 {label}의 {fee}을 {ordinary} {grant}. "
            f"{match['place']}에서 {match['purpose']} 목적으로 주차하면 {extra} 할인하고, "
            f"{match['frequency']} 주차 시 최초 {match['duration']}까지 요금을 면제합니다.")


def _fees(question, plan, selected):
    rules = [_body(r["quote"]) for r in selected if not r["locator"].startswith("별표")]
    if len(selected) != 4 or len(rules) != 2:
        return None
    ordinary = next((m for text in rules if (m := _FREE.fullmatch(text))), None)
    funded = next((m for text in rules if (m := _EXEMPT.fullmatch(text))), None)
    if (not ordinary or not funded or ordinary["site"] not in question or ordinary["site"] != funded["site"]
            or ordinary["fee"] != funded["fee"]):
        return None
    # Preserve the fee schedule sentence assembled directly from the selected data
    # row and its unit. The prose renderer edits only the two regulatory paragraphs.
    original = " ".join(rules) + " "
    price = plan.get("price")
    if (not isinstance(price, str) or plan["answer"] != original + price
            or not price.startswith("별표의 ") or not price.endswith("원입니다.")):
        return None
    price = re.sub(r"(\d+)회주차시\s*\((\d+)(시간|분)당\)", r"\1회 주차 시 \2\3당", price)
    price = re.sub(r"(\d+)일주차권", r"\1일 주차권", price)
    return (f"법 {ordinary['loc']}에 따라 설치한 {ordinary['site']}의 {ordinary['fee']}은 원칙적으로 무료입니다. "
            f"관리·운영을 {ordinary['agent']} 등에 위탁하는 등 특별한 사유가 있으면 별표 범위에서 규칙으로 정해 "
            f"요금을 부과할 수 있습니다. 법 {funded['loc']}에 따라 설치해 {funded['fund']}를 지원받은 경우에는 "
            f"반드시 무료입니다. {price}")


def concise_provision_answer(question, plan, rows):
    """Return shorter wording or None. The caller retains its original source IDs."""
    if not plan or plan["kind"] not in {"single_benefit", "fee_schedule"} or not plan.get("answer"):
        return None
    ids = plan.get("used_ids", [])
    selected = [r for r in rows if r["id"] in ids]
    if (not ids or len(selected) != len(ids) or set(ids) != set(plan["ids"])
            or len({(r["ordinance"], r["source_url"], r["effective_date"]) for r in selected}) != 1):
        return None
    answer = (_benefit(question, plan, selected) if plan["kind"] == "single_benefit"
              else _fees(question, plan, selected))
    return answer if answer and len(answer) < len(plan["answer"]) else None
