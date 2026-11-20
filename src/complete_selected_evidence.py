"""Complete an existing selection only when the source identifies a unique companion."""

import re

from local_rag_answer import _guard_content_words


_COMPARE = re.compile(r"둘\s*다|함께|동시에|중복|비교|각각")
_PAIR = re.compile(r"([가-힣 ]{1,40}?)(?:과|와|하고|\s및)\s+([가-힣 ]{1,32}?)(?:을|를|은|는)(?=\s)")
_BENEFIT = re.compile(r"\s*(?:(?:주차)?요금\s*)?(?:할인율|감면율|할인|감면|혜택)$")
_RATE = re.compile(r"\d+\s*(?:분의\s*\d+|%|퍼센트)")
_EXCLUSION = re.compile(r"제외|아닌|아니한|않는|않은|미보유|없이|없는|없어|받지\s*못|하지\s*(?:않|아니)")
_MONEY = re.compile(r"요금|수수료|입장료|사용료|이용료|가격|비용|돈")
_VALUE = re.compile(r"^(?P<field>[^:\n]+)[:：][ \t]*\d[\d,]*(?:\.\d+)?[ \t]*(?P<won>원)?[ \t]*$", re.MULTILINE)
_PRICE_FIELD = re.compile(r"요금|금액|가격|입장료|시간당|이용권|주차권|정기권|일권|월권")
_UNIT = re.compile(r"단위\s*[:：]\s*(?:억|백만|만|천|백)?원")
_PRICE_QUERY = re.compile(r"(?:주차|이용|사용)?요금|수수료|입장료|사용료|이용료|금액|가격|비용|돈|몇|얼마[가-힣]*|"
                          r"내야|내나요|내면|내는지|내는|내야겠[가-힣]*|낸다면|하나요|하나|인가요|됩니까")


def _source(row):
    return row["ordinance"], row["source_url"], row["effective_date"]


def _has_name(text, name):
    # Match the whole named subject, including compound qualifiers. In particular
    # '비장애인' cannot satisfy a request about '장애인', or vice versa.
    words = r"\s*".join(re.escape(word) for word in name.split())
    return re.search(r"(?<!\w)" + words + r"(?:은|는|의|을|를|이|가|에게)?(?!\w)", text) is not None


def _comparison_companions(question, rows, picked):
    if not _COMPARE.search(question) or not re.search(r"할인|감면", question):
        return set()
    pair = _PAIR.search(question)
    if not pair or _EXCLUSION.search(question) or re.search(r"서류|절차|신청|제출|접수", question):
        return set()
    names = [_BENEFIT.sub("", group.strip()).strip() for group in pair.groups()]
    if not all(len(name) >= 2 for name in names) or names[0] == names[1]:
        return set()
    anchors = [r for r in picked if re.search(r"(?:감면|할인)율이\s*높은\s*하나만", r["quote"])]
    if len(anchors) != 1:
        return set()
    anchor = anchors[0]
    matches = [set(), set()]
    for row in rows:
        if (_source(row) != _source(anchor) or row["owner_id"] != anchor["owner_id"]
                or "::item::" not in row["unit_id"]):
            continue
        # Match the main benefit clause. A name mentioned only in an exception,
        # cross-reference or later list is insufficient to attach the rate.
        text = re.sub(r"^\s*\d+(?:의\d+)?[.)]\s*", "", row["quote"])
        clause = re.split(r"\n|[.!?;](?=\s|$)|다만", text, maxsplit=1)[0]
        if not (_RATE.search(clause) and re.search(r"할인|감면", clause)) or _EXCLUSION.search(clause):
            continue
        for name, found in zip(names, matches):
            subject = r"\s*".join(re.escape(word) for word in name.split())
            if (_has_name(clause, name) and re.match(r"\s*(?:다음의\s+)?[「『]?" + subject, clause)
                    and not re.search(subject + r"(?:의\s*|을\s*동반한\s*|를\s*동반한\s*)"
                                      r"(?:자녀|보호자|부모|배우자|대리인|가족)", clause)):
                found.add(row["id"])
    if not all(len(found) == 1 for found in matches) or matches[0] == matches[1]:
        return set()
    return matches[0] | matches[1]


def _amount_companions(question, rows, picked):
    if (not re.search(r"얼마|몇\s*원|금액", question) or not _MONEY.search(question)
            or re.search(r"과태료|벌금|범칙금|위약금|제외", question)):
        return set()
    # A unit or caption is not a quoted price. Bind any restored row to the
    # already-selected annex AND its referring rule in the same ordinance edition.
    headers = [r for r in picked if r["locator"].startswith("별표") and r["unit_id"] != r["owner_id"]]
    anchors = [r for r in picked if not r["locator"].startswith("별표")
               and "별표" in r["quote"] and _MONEY.search(r["quote"])]
    if not headers or len({_source(r) for r in anchors}) != 1:
        return set()
    annexes = {(_source(h), h["public_label"], h["owner_id"]) for h in headers}
    if len(annexes) != 1:
        return set()
    scope, label, owner = annexes.pop()
    annex = re.search(r"별\s*표(?:\s*\d+(?:의\d+)?)?$", label)
    if not annex or not all(scope) or not owner:
        return set()
    annex_name = annex.group().replace(" ", "")
    rules = [r for r in anchors if _source(r) == scope
             and annex_name in {m.replace(" ", "") for m in re.findall(r"별\s*표(?:\s*\d+(?:의\d+)?)?", r["quote"])}]
    # A generic fee or city word alone cannot establish the requested facility.
    words = _guard_content_words(question)
    if "서울특별시" in scope[0]:
        words -= {"서울특별시", "서울시"}
    names = {word for word in words if not _PRICE_QUERY.fullmatch(word)}
    if not names or not any(all(_has_name(h["quote"], name) and _has_name(r["quote"], name) for name in names)
                            for h in headers for r in rules):
        return set()
    annex_rows = [r for r in rows if _source(r) == scope and r["public_label"] == label]
    values = [r for r in annex_rows if r["unit_id"] == r["owner_id"]]
    if len(values) != 1 or values[0]["owner_id"] != owner:
        return set()
    row = values[0]
    if not any(m["won"] or _PRICE_FIELD.search(m["field"]) for m in _VALUE.finditer(row["quote"])):
        return set()
    labels = re.findall(r"^\s*(?:구\s*분|항\s*목|종\s*별)\s*[:：]\s*([^\n]+)", row["quote"], re.MULTILINE)
    if not labels or not all(re.fullmatch(r"(?:주차|이용|사용)?요금|수수료|입장료", x.strip())
                             or _has_name(question, x.strip()) for x in labels):
        return set()
    if not any(r["id"] in row["required_ids"] and r["owner_id"] == owner and _UNIT.search(r["quote"])
               for r in annex_rows):
        return set()
    return {row["id"]}


def complete_selection(question, rows, selected_ids):
    """Keep all selected IDs in order; append unambiguous companions in retrieval order."""
    selected = list(selected_ids)
    if not selected:
        return selected
    initial = set(selected)
    picked = [row for row in rows if row["id"] in initial]
    additional = _comparison_companions(question, rows, picked) | _amount_companions(question, rows, picked)
    missing = additional - initial
    return selected + [row["id"] for row in rows if row["id"] in missing]
