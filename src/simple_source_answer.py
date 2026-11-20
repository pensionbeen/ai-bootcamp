"""Answer a simple admission or fine question when its conditions match exact source rows."""

import re

from complete_selected_evidence import _source
from source_provision_style import _body


_AGE = r"(?:만\s*)?(?P<age>\d{1,3})(?:세|살)"
_SITE = r"(?P<site>[가-힣]+?(?: [가-힣]+?){0,3}?)"
_EXEMPT_QUESTION = (r"면제(?:받을 수 (?:있나요|있을까요|있습니까)| 대상(?:인가요|일까요)|"
                    r"(?:가 )?(?:되나요|될까요)|인가요)[?.]?")
_ENTRANCE = r"입장료(?:를|가|는)? "
_AGE_QUERY = re.compile(
    r"(?:(?:저는|저희) )?" + _AGE + r"(?:인데요?|인데|인 (?:아버지|어머니|부모님|자녀|아이)(?:가|께서|이)) "
    + _SITE + r"(?:에 (?:가시는데|가는데|가면)|의)? " + _ENTRANCE + _EXEMPT_QUESTION
)
_FACILITY_FIRST = re.compile(
    _SITE + r"에서 (?:(?:저는|제가) )?" + _AGE + r"(?:인데|라면|이면|도|인 사람은) " + _ENTRANCE + _EXEMPT_QUESTION
)
_FEE_FIRST = re.compile(
    _SITE + r"(?:의)? 입장료(?:는|가)? " + _AGE + r"(?:이면|라면|도|인 사람은) " + _EXEMPT_QUESTION
)
_ADMISSION = re.compile(r"(?:시장|구청장|군수|도지사)(?:은|는) 다음 각 호의 사람에 대하여 입장료를 "
                        r"면제(?P<grant>할 수 있다|한다)\.")
_LIMIT = r"(?:만\s*)?(\d{1,3})\s*세\s*(미만|이상|이하|초과)"
_AGE_ITEM = re.compile(rf"(?P<limits>{_LIMIT}(?: 또는 {_LIMIT})?)(?:인)? 사람"
                       r"(?:과 「[^」]+」에 따른 [가-힣]+(?:\([^()]+\))?)?[.]?")
_FACILITY_CELL = re.compile(r"(?:[가-힣]+별|종별|시설명|시설|명칭)")
_PLACE = r"[가-힣]{2,16}(?:구역|지역|지구)"
_PENALTY_QUERY = re.compile(
    rf"(?:(?P<city>[가-힣]{{2,12}}?)(?:가 지정한 |에서 | ))?(?P<place>{_PLACE})(?:에서|으로 지정한 장소에서) "
    r"(?P<act>담배를 피우|술을 마시|[가-힣]{2,12}(?:[을를] )?하)(?:면|는 경우) "
    r"(?:내야 하는 )?과태료(?:가|는|의)?(?: 액수(?:가|는)?)? "
    r"(?:(?:얼마(?:인가요|일까요|예요|입니까)|궁금해요)|얼마(?:이고|고),? (?P<appeal>이의제기 절차)는 어느 법을 따르나요)[?.]?"
)
_PENALTY = re.compile(
    r"(?:시장|구청장|군수|도지사)(?:은|는) (?:「[^」]+」 )?제\d+조(?:의\d+)?(?:제\d+항)?(?:제\d+호)?[을를] 위반하여 "
    rf"(?P<place>{_PLACE})에서 (?P<act>[가-힣]+)[을를] 한 사람(?:은|에게는|에게) "
    r"(?P<amount>\d+(?:,\d{3})*(?:만|천)?\s*원)의 과태료를 부과(?:[·ㆍ]징수)?(?P<grant>한다|할 수 있다)\."
)
_APPEAL = re.compile(r"제(?P<para>\d+)항에 따른 과태료의 부과[ㆍ·]징수 및 이의제기 절차는 「(?P<law>[^」]+)」에 따른다\.")
_ARTICLE_PARA = re.compile(r"(제\d+조(?:의\d+)?) 제(\d+)항")


def _plan(kind, rows, answer):
    ids = [r["id"] for r in rows]
    return {"kind": kind, "ids": ids, "used_ids": ids.copy(), "answer": answer}


def _facility_scopes(site, rows):
    """Verify a complete named facility path against a real admission-fee table row."""
    by_id = {r["id"]: r for r in rows}
    scopes = set()
    for row in rows:
        if not row["locator"].startswith("별표") or row["unit_id"] != row["owner_id"]:
            continue
        sites = []
        for line in row["quote"].splitlines():
            key, sep, value = line.partition(":")
            if sep and _FACILITY_CELL.fullmatch("".join(key.split())):
                sites.append(re.sub(r"\s*\([가-힣 ]+포함\)\s*$", "", value).strip())
        if " ".join(sites) != site:
            continue
        headers = [by_id.get(dep) for dep in row["required_ids"]]
        if (not all(_source(row)) or not headers or any(not h or _source(h) != _source(row)
                or h["owner_id"] != row["owner_id"] or h["public_label"] != row["public_label"] for h in headers)):
            return set()
        if any(re.search(r"면제|제외|제출|제시|예외", h["quote"]) for h in [row, *headers]):
            return set()
        if any(re.search(r"^[가-힣 ]*입장료\s*\(\s*제\s*\d+조(?:의\d+)?(?:\s*제\s*\d+항)?\s*관련\s*\)\s*$",
                         h["quote"], re.M) for h in headers):
            scopes.add(_source(row))
    return scopes


def _admission(match, rows):
    scopes = _facility_scopes(match["site"], rows)
    if len(scopes) != 1:
        return None
    scope = scopes.pop()
    grants = [(r, m) for r in rows if _source(r) == scope and (m := _ADMISSION.fullmatch(_body(r["quote"])))]
    if len(grants) != 1:
        return None
    intro, grant = grants[0]
    article = intro["locator"].split()[0]
    if any(_source(r) == scope and not r["unit_id"].startswith(intro["owner_id"] + "::item::")
           and r["id"] != intro["id"] and ("입장료" in r["quote"] or r["locator"].split()[:1] == [article])
           and "면제" in r["quote"] and
           re.search(r"다만|제외|제시|증명|제출|한정|한하여|불구하고", r["quote"]) for r in rows):
        return None
    age = int(match["age"])
    matches = []
    for row in rows:
        if (_source(row) != scope or row["owner_id"] != intro["owner_id"] or intro["id"] not in row["required_ids"]
                or not row["unit_id"].startswith(intro["owner_id"] + "::item::")
                or row["public_label"] != intro["public_label"]):
            continue
        text = _body(row["quote"])
        item = _AGE_ITEM.fullmatch(text)
        if not item:
            if re.search(r"\d+\s*세", text):
                return None
            continue
        # A qualifier attached after the age branch could narrow that branch too.
        if re.search(r"다만|제외|한정|한하여|경우에만|\d+\s*세", text[item.end("limits"):]):
            return None
        for threshold, relation in re.findall(_LIMIT, item["limits"]):
            n = int(threshold)
            applies = {"이상": age >= n, "초과": age > n, "이하": age <= n, "미만": age < n}
            if applies[relation]:
                matches.append((row, f"{n}세 {relation}"))
    if len(matches) != 1:
        return None
    row, condition = matches[0]
    grant_phrase = "면제받을 수 있습니다." if grant["grant"] == "할 수 있다" else "면제받습니다."
    return _plan("age_admission", [intro, row], f"네. {condition}인 사람은 입장료를 {grant_phrase}")


def _fine(query, rows):
    verb = query["act"]
    common = {"담배를 피우": "흡연", "술을 마시": "음주"}
    action = common.get(verb, re.sub(r"(?:[을를] )?하$", "", verb))
    relevant = []
    for row in rows:
        if not _ARTICLE_PARA.fullmatch(row["locator"]):
            continue
        rule = _PENALTY.fullmatch(_body(row["quote"]))
        if rule and (rule["place"], rule["act"]) == (query["place"], action):
            relevant.append((row, rule))
    if len(relevant) != 1:
        return None
    row, rule = relevant[0]
    city = query["city"]
    city = {"서울": "서울특별시", "서울시": "서울특별시"}.get(city, city)
    if not all(_source(row)) or (city and city != row["ordinance"].split()[0]):
        return None
    if any(_source(other) == _source(row) and other["id"] != row["id"] and
           all(w in other["quote"] for w in (query["place"], action, "과태료")) for other in rows):
        return None
    selected = [row]
    grant = "부과됩니다." if rule["grant"] == "한다" else "부과될 수 있습니다."
    answer = f"{rule['place']}에서 {action}하면 {rule['amount']}의 과태료가 {grant}"
    if query["appeal"]:
        article, para = _ARTICLE_PARA.fullmatch(row["locator"]).groups()
        appeals = []
        for other in rows:
            loc = _ARTICLE_PARA.fullmatch(other["locator"])
            if loc and loc[1] == article and _source(other) == _source(row):
                process = _APPEAL.fullmatch(_body(other["quote"]))
                if process and process["para"] == para:
                    appeals.append((other, process["law"]))
        if len(appeals) != 1:
            return None
        other, law = appeals[0]
        selected.append(other)
        answer += f" 이의제기 절차는 「{law}」에 따릅니다."
    return _plan("area_fine", selected, answer)


def simple_source_answer(question, rows):
    """Return a compact, row-bound response for one unambiguous question."""
    text = " ".join(question.split())
    age = _AGE_QUERY.fullmatch(text) or _FACILITY_FIRST.fullmatch(text) or _FEE_FIRST.fullmatch(text)
    if age:
        return _admission(age, rows)
    fine = _PENALTY_QUERY.fullmatch(text)
    return _fine(fine, rows) if fine else None
