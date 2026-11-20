"""Answer the scope of an explicit allowance grant when its amount or term is missing."""

import re


_PAYMENT = r"[가-힣]{1,16}(?:수당|지원금|장려금|보조금|축하금)"
_QUESTION = re.compile(
    rf"(?:(?P<region>[가-힣]{{2,8}}(?:특별자치도|특별자치시|광역시|특별시|시|도|군|구))(?:의)?\s+)?"
    rf"(?P<name>{_PAYMENT})(?:은|는|을|를|의|이|가)?\s*(?P<request>.+)"
)
_FIELDS = re.compile(r"(?P<monthly>월지급액|월수령액|월얼마|(?:한달|매달|매월)(?:에|마다)?얼마)"
                     r"|(?P<amount>지급금액|지급액|수령액|금액|얼마(?!나))"
                     r"|(?P<term>지급기간|수령기간|몇개월|몇달)")
# Recognize a whole request. An extra condition, a second scheme, or another
# action (repayment, application, etc.) needs the normal interpretation path.
_REQUEST_WORD = re.compile(
    r"(?:어떻게|되나요|되나|되는지|인가요|인가|나요|나옴|나와요|나오나요|나오는지|나오나|"
    r"받을수있(?:는지도|는지|나요|을까요|습니까)|받나요|받아요|받습니까|지급되나요|"
    r"궁금(?:해요|합니다)|알려(?:주세요|줘요|줘)|그리고|동안|까지|하고|"
    r"은|는|이|가|을|를|과|와|도|에|씩|[?,.!？·ㆍ])+"
)
_PLACE_SUFFIX = re.compile(r"(?:특별자치|광역|특별)?([시도군구])$")
_AUTHORITY = r"(?P<actor>시장|도지사|군수|구청장|교육감)(?:은|는)"
_REFERENCE = re.compile(r"^\s*(?:[①-⑳]|\d+(?:의\d+)?[.)])\s*")
_SOURCE_DATE = re.compile(r"\s*<(?:개정|신설)[^<>]*>\s*$")
_AMOUNT_OR_TERM = re.compile(r"(?:\d|[일이삼사오육칠팔구십백천만]+)\s*(?:만\s*원|천\s*원|원|개\s*월)"
                             r"|(?:지급|지원)\s*(?:금액|액|기간|한도|상한)|월\s*지급액")
_PROVISO = re.compile(r"다만|제외|한하여|한해|(?:하는|되는|아닌|해당하는)\s*(?:때|경우)|조건|단서")
_DOCUMENT = r"(?:사업\s*공고|시행\s*규칙|운영\s*지침|지급\s*지침)"
_ARTICLE = re.compile(r"제\s*\d+\s*조(?:\s*의\s*\d+)?")
_SIBLING_DETAIL = re.compile(
    r"(?:\d+|[한두세네]|[일이삼사오육칠팔구십백천]+)\s*(?:개월|분기|년|달|월|주|일)"
    r"|별표|별지|시행\s*규칙|고시|공고|지침|한한다|한정|받을\s*수\s*없"
)
_ADMINISTRATIVE_PURPOSE = re.compile(
    r"(?P<purpose>[^,.;]{2,70}?기\s*(?:위해(?:서)?|위하여))\s+[^,.;]*?"
    r"(?:방안|대책|계획)을\s*(?:강구|마련|수립)하여야\s*하며"
)
_MEETING_EXPENSES = re.compile(
    r"(?:또한,?\s*)?[^.]*?(?:회의|위원회)[^.]*?(?:참석|출석)\s*위원에게(?:는)?\s*"
    r"[^.]*?(?:수당과\s*여비|여비와\s*수당)[^.]*?지급할\s*수\s*있다[.]?"
)


def _asked(question):
    match = _QUESTION.fullmatch(" ".join(question.split()))
    if not match:
        return None
    request = re.sub(r"[ㄱ-ㅎ]{1,4}[?.!？]*$", "", re.sub(r"\s+", "", match["request"]))
    found = {m.lastgroup for m in _FIELDS.finditer(request)}
    # Preserve gaps between recognized fields so adjacent fragments cannot
    # combine into an accepted word after a match is removed.
    start = 0
    for span in _FIELDS.finditer(request):
        fragment = request[start:span.start()]
        if fragment and not _REQUEST_WORD.fullmatch(fragment):
            return None
        start = span.end()
    if not found or (request[start:] and not _REQUEST_WORD.fullmatch(request[start:])):
        return None
    return match["name"], match["region"], found


def _content(row):
    return _REFERENCE.sub("", _SOURCE_DATE.sub("", row["quote"].strip())).strip()


def _scope(row):
    return row.get("ordinance"), row.get("source_url"), row.get("effective_date")


def _source_document(text, name, fields):
    # Only claim a named follow-up document when the same programme explicitly
    # assigns the actual dimensions the citizen asks about to that document.
    match = re.fullmatch(rf"{name}(?:의)?\s+(?P<fields>.+?)(?:은|는)\s+"
                         rf"(?P<document>{_DOCUMENT})(?:에서|으로|로)\s+정한다[.。]?", text)
    if not match:
        return None
    tokens = re.split(r"\s*(?:·|ㆍ|및|와|과)\s*", match["fields"])
    dimensions = {re.sub(r"\s+", "", t) for t in tokens}
    if not dimensions <= {"지급액", "지급금액", "지급기간", "월지급액"}:
        return None
    if fields & {"monthly", "amount"} and not dimensions & {"지급액", "지급금액", "월지급액"}:
        return None
    if "term" in fields and "지급기간" not in dimensions:
        return None
    return match["document"]


def partial_benefit(question, rows):
    parsed = _asked(question)
    if not parsed:
        return None
    name, region, fields = parsed
    grant_pattern = re.compile(
        rf"{_AUTHORITY}\s+(?:(?P<preamble>[^.]*?(?:하여야|해야)\s+하며),\s*)?"
        rf"예산의\s*범위(?:에서|\s*안에서)\s+{name}(?:을|를)\s+지급할\s+수\s+있다[.。]?"
    )
    grants, documents = [], []
    for row in rows:
        text = _content(row)
        if name not in text:
            continue
        if not row.get("id") or not all(_scope(row)) or not row.get("locator", "").lstrip().startswith("제"):
            return None
        sentence, _, extra = text.partition(".")
        grant = grant_pattern.fullmatch(sentence.strip())
        if grant and not _PROVISO.search(text):
            # A separate committee remuneration sentence concerns different
            # recipients. Other trailing text may qualify this payment.
            if extra.strip() and (name in extra or not _MEETING_EXPENSES.fullmatch(extra.strip())):
                return None
            purpose = ""
            if grant["preamble"]:
                recognized = _ADMINISTRATIVE_PURPOSE.fullmatch(grant["preamble"])
                if not recognized:
                    return None
                purpose = recognized["purpose"] + " "
            grants.append((row, grant["actor"], purpose))
        else:
            document = _source_document(text, name, fields)
            if not document:
                return None
            documents.append((row, document))
    if len(grants) != 1 or len(documents) > 1:
        return None
    grant, actor, purpose = grants[0]
    scope = _scope(grant)
    if region and _PLACE_SUFFIX.sub(r"\1", region) != _PLACE_SUFFIX.sub(r"\1", scope[0].split()[0]):
        return None
    if any(_scope(r) != scope for r, _ in documents):
        return None
    used = [grant["id"], *(r["id"] for r, _ in documents)]
    # A dependent tariff/other provision may give the actual amount or term,
    # including without repeating the proper name. Defer instead of declaring a gap.
    article = _ARTICLE.match(grant["locator"])
    if not article or len(set(used)) != len(used):
        return None
    for row in rows:
        if _scope(row) != scope or row["id"] in used:
            continue
        text = _content(row)
        if row["locator"].startswith("별표") or _AMOUNT_OR_TERM.search(text):
            return None
        sibling = _ARTICLE.match(row["locator"])
        if sibling and sibling[0] == article[0] and (_SIBLING_DETAIL.search(text) or _PROVISO.search(text)):
            return None
    requested = []
    if fields & {"monthly", "amount"}:
        requested.append("월 지급액" if "monthly" in fields else "지급액")
    if "term" in fields:
        requested.append("지급 기간")
    detail = "과 ".join(requested)
    particle = "은" if (ord(actor[-1]) - 0xac00) % 28 else "는"
    missing = f"{name} {detail}을 확인할 수 있는 지급 기준"
    answer = f"{actor}{particle} {purpose}예산의 범위에서 {name}을 지급할 수 있습니다. "
    if documents:
        missing = f"{name} {documents[0][1]}"
        answer += f"{detail}은 {documents[0][1]}에서 확인해야 합니다."
    else:
        answer += f"이 지급 조항에는 {detail}이 명시되어 있지 않습니다."
    return {"kind": "partial_allowance", "ids": used, "used_ids": list(used),
            "answer": answer, "missing_materials": [missing]}
