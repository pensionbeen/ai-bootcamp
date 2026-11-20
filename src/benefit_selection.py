"""Resolve benefit evidence from retrieved legal units before asking for a draft."""

import re

from answer_fact_coverage import _rates
from complete_selected_evidence import _EXCLUSION, _comparison_companions, _has_name, _source
from grounded_discount_comparison import _AMEND, _NAME, _QUESTION, _RULE, _SPECIAL


_BENEFIT = r"(?:할인|감면)"
_FACILITY = r"공영[가-힣]{2,8}"
_FEE = r"[가-힣]*?(?:요금|입장료|이용료|사용료|수수료)"
_SINGLE_END = _BENEFIT + r"(?:되나요|됩니까|돼요|받을 수 있나요|받을 수 있을까요)?\?"
_PARKING = re.compile(
    rf"(?P<name>{_NAME})(?:을|를) (?:(?P<city>서울시|서울특별시) )?(?P<site>{_FACILITY})(?:에|에서) "
    rf"(?:세우면|주차하면|이용하면) (?P<fee>{_FEE})(?:이|가|은|는)? " + _SINGLE_END
)
_SINGLE = re.compile(
    rf"(?:(?:(?P<city>서울시|서울특별시) )?(?P<site>{_FACILITY})에서(?:는)? )?"
    rf"(?P<name>{_NAME})(?:은|는|도)? (?P<fee>{_FEE})(?:이|가|은|는)? " + _SINGLE_END
)
_PAIR_SHORT = re.compile(
    rf"(?:(?P<front>{_FACILITY})에서(?:는)? )?(?P<a>{_NAME})(?:과|와|하고) "
    rf"(?P<b>{_NAME}) (?:할인|감면)(?:을|를|이|가|은|는)? (?:(?P<middle>{_FACILITY})에서(?:는)? )?"
    r"(?:중복(?:으로)? (?:적용되나요|적용돼요|받을 수 있나요)|중복되나요|"
    r"중복 적용이 가능한가요|(?:함께|동시에|같이|둘 다) (?:받을 수 있나요|받을 수 있을까요|적용되나요))\?"
)
_COMBINATION = re.compile(r"중복|병용|높은 하나|낮은 하나|(?:함께|동시에)\s*(?:적용|할인|감면)")
_ARTICLE = re.compile(r"^제\s*\d+\s*조(?:의\s*\d+)?(?=\s|$)")
_DENIED = re.compile(r"없는|아닌|아니한|않는|미보유|비대상|불가|제외|그 가족|그 보호자")
_SUBJECT_TAIL = r"(?=\s|[은는이가와과을를」]|에게|$)"
_TERM = {"경차": "경형자동차"}


def _items(rows, site, city=None):
    return [r for r in rows if all(_source(r)) and r["owner_id"]
            and r["unit_id"].startswith(r["owner_id"] + "::item::")
            and (not site or site.removeprefix("공영") in r["ordinance"])
            and (not city or "서울특별시" in r["ordinance"])]


def _ordinary_clause(row):
    text = re.sub(r"^\s*\d+(?:의\d+)?[.)]\s*", "", row["quote"])
    return re.split(r"\n|다만|[.](?=\s|$)", text, maxsplit=1)[0].strip().removesuffix(".")


def _rate(clause):
    rates = list(_rates(clause))
    if len(rates) != 1:
        return None
    _, end, fraction = rates[0]
    scaled = fraction * 100000
    if not 0 < fraction <= 1 or scaled.denominator != 1:
        return None
    if not re.fullmatch(r"(?:을|를)?\s*(?:할인|감면)(?:한다|할 수 있다)", clause[end:]):
        return None
    whole, decimal = divmod(scaled.numerator, 1000)
    return f"{whole}.{decimal:03}".rstrip("0").rstrip(".")


def _pair(match, rows):
    a, b = match["a"], match["b"]
    site = match["front"] or match["middle"]
    if a == b or (match["front"] and match["middle"]) or _SPECIAL.search(a + " " + b):
        return None
    items = _items(rows, site)
    canonical = f"{a} 할인과 {b} 할인을 둘 다 받을 수 있나요?"
    results = []
    for rule in rows:
        if not _RULE.fullmatch(re.sub(r"^\s*[①-⑳]\s*", "", _AMEND.sub("", rule["quote"])).strip()):
            continue
        scope = [r for r in items if _source(r) == _source(rule) and r["owner_id"] == rule["owner_id"]]
        if any(_COMBINATION.search(r["quote"]) and r["quote"].strip() != rule["quote"].strip()
               for r in rows if _source(r) == _source(rule) and r["owner_id"] == rule["owner_id"]):
            continue
        # The non-stacking provision must be explicitly linked to both rate items.
        linked = [r for r in scope if rule["id"] in r["required_ids"]]
        companions = _comparison_companions(canonical, scope, [rule])
        chosen = [r for r in linked if r["id"] in companions]
        if len(chosen) != 2 or len({r["public_label"] for r in chosen}) != 1:
            continue
        article = _ARTICLE.match(rule["locator"])
        if not article or any(not (loc := _ARTICLE.match(r["locator"])) or loc[0] != article[0] for r in chosen):
            continue
        ordered = []
        for name, other in ((a, b), (b, a)):
            target = [r for r in chosen if _has_name(_ordinary_clause(r), name)]
            if len(target) != 1 or _has_name(target[0]["quote"], other) or _COMBINATION.search(target[0]["quote"]):
                return None
            row = target[0]
            clause = _ordinary_clause(row)
            rate = _rate(clause)
            if not rate or _SPECIAL.search(clause) or len({value for _, _, value in _rates(row["quote"])}) != 1:
                return None
            ordered.append((row, rate))
        fees = [re.findall(_FEE, _ordinary_clause(row)) for row, _ in ordered]
        if len(fees[0]) != 1 or fees[0] != fees[1]:
            return None
        ids = [r["id"] for r, _ in ordered] + [rule["id"]]
        location = site + "에서는" if site else rule["ordinance"] + "에 따르면"
        answer = (f"{location} 두 감면 요건을 모두 충족해도 중복 할인은 받을 수 없습니다. "
                  f"{a} 할인({ordered[0][1]}%)과 {b} 할인({ordered[1][1]}%) 중 할인율이 높은 하나만 적용됩니다.")
        results.append({"kind": "discount_pair", "ids": ids, "used_ids": ids.copy(), "answer": answer})
    return results[0] if len(results) == 1 else None


def _single(match, rows):
    name = _TERM.get(match["name"], match["name"])
    if _SPECIAL.search(name) or _EXCLUSION.search(name):
        return None
    # A colloquial subject may follow a statutory definition. It must still head
    # the operative recipient phrase, rather than occur in an exception or example.
    subject = re.compile(r"^(?:시장은\s+|구청장은\s+)?(?:다음의\s+)?"
                         r"(?:「[^」]+」[^.。]{0,150}?에\s*따른\s+)?[「『]?"
                         + re.escape(name) + _SUBJECT_TAIL)
    chosen = []
    for row in _items(rows, match["site"], match["city"]):
        clause = _ordinary_clause(row)
        found = subject.match(clause)
        if not found or match["fee"] not in clause or _rate(clause) is None or _DENIED.search(clause):
            continue
        if re.match(r"(?:의\s*|을\s*동반한\s*|를\s*동반한\s*)(?:자녀|보호자|부모|배우자|대리인|가족)",
                    clause[found.end():]):
            continue
        chosen.append(row)
    if len(chosen) == 1:
        row = chosen[0]
        if re.search(r"\n\s*[가-힣]\.\s*", row["quote"]):
            # Nested recipient lists can require documentation and exceptions in the
            # parent introduction; a single item cannot certify the entire input.
            return None
        # Give the writer the whole item, including its exceptions and source conditions.
        return {"kind": "single_benefit", "ids": [row["id"]], "required_ids": [row["id"]],
                "subject": match["name"], "source_subject": name, "used_ids": [], "answer": None}
    return None


def benefit_evidence(question, rows):
    """Select only when the question and retrieved provisions resolve the intended benefit."""
    question = " ".join(question.split()).rstrip("?") + "?"
    match = _QUESTION.fullmatch(question) or _PAIR_SHORT.fullmatch(question)
    if match:
        return _pair(match, rows)
    match = _PARKING.fullmatch(question) or _SINGLE.fullmatch(question)
    return _single(match, rows) if match else None
