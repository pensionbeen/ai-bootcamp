"""Answer a single benefit from its grant, one named eligibility branch and its exceptions."""

import re

from benefit_selection import _PARKING, _SINGLE
from complete_selected_evidence import _source
from source_provision_style import _VALUE, _body, _percent


_FEE = r"[가-힣]*(?:요금|입장료|이용료|사용료|수수료)"
_INTRO = re.compile(
    rf"(?P<authority>시장|도지사|군수|구청장)(?:은|는) (?:제\d+조(?:의\d+)?(?:제\d+항)? 본문에도 불구하고 )?"
    rf"(?:(?P<scope>(?P<operator>[가-힣]*(?:시장|도지사|군수|구청장))이 "
    rf"(?:직접 운영|설치[·ㆍ]운영)하는 (?P<facility>[가-힣]+))에서 )?"
    rf"다음 각 호에 해당할 경우에는 (?P<fee>{_FEE})[을를] 감면할 수 있다\."
    rf"(?: 이 경우 (?P<systems>[가-힣]+(?: 또는 [가-힣]+)*?로) 각 호의 자격이 확인되는 "
    rf"[가-힣]+ 이용자는 증빙서류를 제시하지 않고 (?P=fee)[을를] 감면받을 수 있다\.)?"
)
_GRANT = re.compile(
    rf"다음의? [가-힣]+(?: (?:및|또는) [가-힣]+)*(?: 등)?이 (?:사용|이용|운행)하는 "
    rf"(?P<object>[가-힣]+)에 대하여는 (?P<fee>{_FEE})의 (?P<rate>{_VALUE})[을를] 할인한다\."
    rf"(?: 다만,? (?P<place>[가-힣]+(?: [가-힣]+)*)의 경우에는 (?P<count>\d+일 \d+회)당 "
    rf"최초 (?P<time>\d+(?:시간|분))까지의 (?P=fee)[은는] 면제하고 이후 (?P=fee)의 "
    rf"(?P<later>{_VALUE})[을를] 할인한다\.)?"
)
_DIRECT = re.compile(rf"(?P<condition>.+? 제시한 경우) (?P<fee>{_FEE})의 (?P<rate>{_VALUE})[을를] 할인한다\.")
_ENTRY = re.compile(r"([가나다라마바사아자차카타파하])\.\s*(.+)")
_QUALIFIED = re.compile(r"(?:「[^」]+」[^.]*?에 따른 )?(?P<subject>[가-힣]+?)(?:으?로서) "
                        r"[가-힣]+[을를] 제시한 (?:경우|사람)")
_ABOARD = re.compile(r"(?:「[^」]+」[^.]*?에 따른 )?[가-힣]+표지[을를] 부착하고 "
                     r"(?P<subject>[가-힣]+?)(?:이|가) 탑승하여 [가-힣]+[을를] 제시한 경우")
_UNASKED_CALCULATION = re.compile(r"[가-힣]+의 할인 등에 따라 \d+원 미만의 단수가 발생할 경우 그 단수는 계산하지 아니한다\.")
_UNASKED_COMBINATION = re.compile(r"둘 이상의 [가-힣]+ 감면사유에 해당하는 때에는 그 중 감면율이 높은 하나만 적용한다\.")
_CHECKED_PROOF = re.compile(
    rf"(?:(?P<city>서울시|서울특별시) )?(?:(?P<site>공영[가-힣]+)에서 )?"
    rf"(?P<name>[가-힣]+?) (?P<fee>{_FEE}) (?:감면|할인)(?:을|를)? "
    r"(?:받으려는데|받을 때|신청할 때) (?P<system>[가-힣]+?)(?:으로|로) 자격이 "
    r"(?:확인돼도|확인되면|확인된 경우에도) (?P<document>[가-힣]*(?:서류|등록증|증명서|신분증))[을를] "
    r"(?:(?:꼭|따로|별도로) )*(?:보여 ?줘야|보여야|제시해야|제출해야) (?:하나요|합니까|해요)\?"
)


def _beneficiary(entry):
    for pattern in (_QUALIFIED, _ABOARD):
        if match := pattern.fullmatch(entry):
            return match["subject"]
    return None


def _branch(row, asked):
    lines = [line.strip() for line in row["quote"].splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) == 1:
        direct = _DIRECT.fullmatch(_body(lines[0]))
        if not direct:
            return None
        clauses = [direct["condition"]]
        grant = direct.groupdict()
    else:
        group = _GRANT.fullmatch(_body(lines[0]))
        entries = [_ENTRY.fullmatch(line) for line in lines[1:]]
        if not group or not all(entries):
            return None
        if [e[1] for e in entries] != list("가나다라마바사아자차카타파하"[:len(entries)]):
            return None
        clauses = [e[2] for e in entries]
        grant = group.groupdict()
    for clause in clauses:
        name = _beneficiary(clause)
        if not name or not re.fullmatch(re.escape(name) + r"[이가은는도]?", asked):
            continue
        if sum(name in c for c in clauses) == 1 and not any("다만" in c for c in clauses):
            return name, clause, grant
    return None


def scoped_benefit_answer(question, rows):
    """Keep the whole operative branch, never project one branch onto another recipient."""
    text = " ".join(question.split()).rstrip("?？") + "?"
    text = re.sub(r"(요금|입장료|이용료|사용료|수수료)[을를] (?=할인|감면)", r"\1 ", text)
    proof = _CHECKED_PROOF.fullmatch(text)
    query = proof or _SINGLE.fullmatch(text) or _PARKING.fullmatch(text)
    if not query:
        return None
    by_id = {r["id"]: r for r in rows}
    candidates = []
    for row in rows:
        if not row["owner_id"] or not row["unit_id"].startswith(row["owner_id"] + "::item::"):
            continue
        branch = _branch(row, query["name"])
        if not branch:
            continue
        name, clause, grant = branch
        if grant["fee"] != query["fee"] or not all(_source(row)):
            return None
        intro_rows = [by_id.get(i) for i in row["required_ids"]]
        intros = [r for r in intro_rows if r and r["unit_id"] == row["owner_id"] + "::intro"
                  and r["owner_id"] == row["owner_id"] and r["public_label"] == row["public_label"]
                  and _source(r) == _source(row)]
        if len(intros) != 1:
            return None
        rule = _INTRO.fullmatch(_body(intros[0]["quote"]))
        rate = _percent(grant["rate"])
        later = _percent(grant["later"]) if grant.get("place") else None
        if not rule or rule["fee"] != grant["fee"] or not rate or grant.get("place") and not later:
            return None
        # A facility in the ordinance title or a neighbouring benefit does not
        # establish who operates the facility covered by this grant.
        if rule["operator"] and not rule["operator"].endswith(rule["authority"]):
            return None
        if query["city"] and (not row["ordinance"].startswith("서울특별시 ") or
                              re.match(r"서울특별시 [가-힣]+구\s", row["ordinance"])):
            return None
        if proof and not rule["scope"]:
            # Answer the evidence requirement conditionally; no claim that
            # the venue or the individual meets the grant's own eligibility.
            if query["site"] and query["site"].removeprefix("공영") not in row["ordinance"].split():
                return None
        elif ((query["site"] and query["site"] != rule["facility"])
              or (query["city"] and rule["operator"] != "서울특별시장")):
            return None
        condition = re.sub(r"제시한 (?:경우|사람)$", "제시하면", clause)
        # Additional dependencies may qualify the entire grant. Rounding and
        # non-stacking affect a calculated price or a second benefit, neither of
        # which the single-recipient question parser accepts.
        deps = (set(row["required_ids"]) | set(intros[0]["required_ids"])) - {intros[0]["id"], row["id"]}
        if any(i not in by_id or _source(by_id[i]) != _source(row)
               or not any(p.fullmatch(_body(by_id[i]["quote"])) for p in (_UNASKED_CALCULATION, _UNASKED_COMBINATION))
               for i in deps):
            return None
        fee_particle = "를" if grant["fee"].endswith("료") else "을"
        object_phrase = f"해당 {grant['object']}의 " if grant.get("object") else ""
        scope = rule["scope"] + "에서는 " if rule["scope"] else ""
        answer = f"{scope}{condition} {object_phrase}{grant['fee']}{fee_particle} {rate} 할인받을 수 있습니다."
        if rule["systems"]:
            answer += f" {rule['systems']} 자격이 확인되면 증빙서류를 제시하지 않아도 됩니다."
        if grant.get("place"):
            answer += (f" {grant['place']}에서는 {grant['count']}당 최초 {grant['time']}까지 면제하고 "
                       f"이후에는 {later} 할인합니다.")
        if proof:
            if not rule["systems"] or (proof["document"] != "증빙서류" and proof["document"] not in clause):
                return None
            systems = re.sub(r"(?:으로|로)$", "", rule["systems"]).split(" 또는 ")
            if proof["system"] != "전산" and proof["system"] not in systems:
                return None
            answer = (f"이 조례의 {grant['fee']} 감면 대상이라면, {rule['systems']} 자격이 확인될 때 "
                      "증빙서류를 따로 제시하지 않아도 됩니다.")
        ids = [intros[0]["id"], row["id"]]
        # Another operative item mentioning the same recipient and fee could
        # describe a different tier even if its syntax is unfamiliar.
        item_number = re.search(r"(제\d+호(?:의\d+)?)$", row["locator"])
        if not item_number:
            return None
        number = re.escape(item_number[1]) + r"(?!의\d)"
        item_ref = re.compile(r"(?<![항조\d])" + number)
        benefit_ref = re.compile(number + r"(?:에도불구하고|(?:의|에따른)(?:감면|할인))")
        for other in rows:
            if other["id"] == row["id"] or other["owner_id"] != row["owner_id"]:
                continue
            wording = "".join(other["quote"].split())
            if item_ref.search(wording) or benefit_ref.search(wording) or "".join(row["locator"].split()) in wording:
                return None
        if any(other["id"] != row["id"] and "::item::" in other["unit_id"]
               and name in other["quote"] and query["fee"] in other["quote"]
               and re.search(r"감면|할인|면제", other["quote"]) for other in rows):
            return None
        candidates.append({"kind": "scoped_benefit", "ids": ids, "used_ids": ids.copy(), "answer": answer})
    return candidates[0] if len(candidates) == 1 else None
