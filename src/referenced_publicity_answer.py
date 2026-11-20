"""Answer meeting-publicity questions from an explicit rule and its cited provision."""

import re

from complete_selected_evidence import _source
from source_provision_style import _body


_ARTICLE = re.compile(r"(제\s*\d+\s*조(?:의\s*\d+)?)(?:\s*제\s*\d+\s*항)?")
_ADDRESS = r"제\s*\d+\s*조(?:의\s*\d+)?(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?"
_RULE = re.compile(
    r"(?:위원회의 )?(?P<object>회의(?:의 내용)?)(?:은|는) "
    r"(?:(?P<purpose>[가-힣ㆍ· ,]+?기 (?:위하여|위해)) )?"
    rf"「(?P<law>[^「」.\n]+)」\s*(?P<address>{_ADDRESS})에 (?:따라|의하여) "
    r"공개(?:를|하는 것을) 원칙으로 한다\."
)
_DEFINITION = re.compile(r"(?<!\w)([가-힣]+위원회)\s*\(이하\s*[\"“]위원회[\"”](?:라|라고) 한다\)"
                         r"(?:를|을)\s*(?:설치|두|둔)")
_QUALIFIER = re.compile(r"경우|다만|제외|한정|한하여|한해|한해서|대하여만|일부|비공개|에만|않|아니|"
                        r"(?:^| )(?:시|때)(?:에|에는|만)? |(?:면|거나|하되|여야|어야|아야) ")
_OTHER_PUBLICITY = re.compile(r"비공개|공개\s*(?:하지|여부|제한|제외|범위|대상)|공개.{0,30}(?:제외|제한)|"
                              r"공개.{0,30}위원장|공개.{0,15}(?:않|아니)|방청.{0,20}(?:거부|제한)|"
                              r"비밀(?:로|에 부)|공개하지|공개할 수 있다")
_PARTICLE = r"(?:에서는|에서|에는|에도|에|은|는|이|가|을|를|의|과|와|도|로|으로|만)?"
_ASK_WORD = re.compile(
    r"(?:위원회|조례|회의|공개|비공개|원칙|기준|사유|예외|조건|경우|때|내용|규정|여부|어디|무엇|어느|언제|원칙적)"
    + _PARTICLE +
    r"|어떤|어떻게|및|또는|할|하는|할지|하는지|정하는지|되는지|가능한|확인할|확인|정해져|규정되어|알려|수|"
    r"있는지|있는|있나요|있을까요|있습니까|없나요|무엇인가요|무엇입니까|뭔가요|인가요|인가|"
    r"어디인가요|어디서|어디에서|확인하나요|따르나요|되나요|있어요|알려주세요|주세요|궁금합니다|궁금해요|"
    r"원칙인가요|원칙입니까|원칙인지|원칙상|공개인가요"
)
_BACKGROUND_WORD = re.compile(
    r"(?:위원회|회의|안건|경우|방청|참석|공개|비공개)" + _PARTICLE +
    r"|방청하고|참석하고|싶어|싶어서|문의했더니|문의하니|따라|비공개일|비공개될|공개될|"
    r"수도|수|있다고|들었습니다|합니다|들었어요"
)


def _question_matches(question, ordinance, committee):
    # Bound the whole request. A separate request for dates, authority, minutes or
    # permission to attend needs evidence beyond this public-default provision.
    text = " ".join(question.split())
    region = ordinance.split()[0]
    short = re.sub(r"(?:특별자치시|특별시|광역시)$", "시", region)
    for name in (ordinance, ordinance.replace(region, short, 1)):
        text = text.replace(name, "조례")
    region_pattern = "(?:" + "|".join(map(re.escape, (region, short))) + ") "
    named = re.compile(r"(?<!\w)(?:" + region_pattern + r")?" + re.escape(committee) +
                       r"(?=(?:에서는|에서|에는|은|는|의|에|가|를)?(?!\w))")
    if not named.search(text):
        return False
    text = named.sub("위원회", text)
    parts = re.split(r"(?<=[.?!])\s+", text)
    if len(parts) > 2 or "회의" not in text:
        return False
    if len(parts) == 2:
        background = parts[0]
        if (not background.endswith(".") or "회의" not in background or
                not all(_BACKGROUND_WORD.fullmatch(w) for w in background[:-1].split())):
            return False
    ask = parts[-1].rstrip("?.")
    if not all(_ASK_WORD.fullmatch(w) for w in ask.split()):
        return False
    if "비공개" in ask:
        return bool(re.search(r"기준|사유|조건|예외|어떤 (?:경우|때)|언제", ask))
    return "공개" in ask and "원칙" in ask


def _applicable(row, rows, question, rule, article):
    scope = _source(row)
    if not all(scope) or rule["law"] == scope[0]:
        return False
    if rule["purpose"] and _QUALIFIER.search(rule["purpose"]):
        return False
    definitions = set()
    for other in rows:
        if other is row:
            continue
        # If the external provision was retrieved, use the full evidence path.
        if other["ordinance"] == rule["law"]:
            return False
        if other["ordinance"] != scope[0]:
            continue
        loc = _ARTICLE.fullmatch(other["locator"])
        same_article = loc and loc[1] == article
        quote = _body(other["quote"])
        paragraph = re.search(r"(제\s*\d+\s*항)$", row["locator"])
        refers_to_default = paragraph and paragraph[1] in quote
        if (_OTHER_PUBLICITY.search(quote) or
                (same_article and re.search(r"공개|방청|비밀회의", quote)) or
                (same_article and refers_to_default and re.search(r"불구하고|다만|제외", quote))):
            return False
        if same_article and _source(other) == scope:
            definitions.update(_DEFINITION.findall(quote))
    return len(definitions) == 1 and _question_matches(question, scope[0], next(iter(definitions)))


def referenced_publicity_answer(question, rows):
    rules = []
    for row in rows:
        loc = _ARTICLE.fullmatch(row["locator"])
        rule = _RULE.fullmatch(_body(row["quote"]))
        if loc and rule:
            rules.append((row, rule, loc[1]))
    if len(rules) != 1:
        return None
    row, rule, article = rules[0]
    if not _applicable(row, rows, question, rule, article):
        return None
    subject = "회의 내용은" if rule["object"] == "회의의 내용" else "회의는"
    reference = f"「{rule['law']}」 {rule['address']}"
    answer = f"{subject} {reference}에 따라 공개하는 것이 원칙입니다."
    if "비공개" in question:
        answer += " 이 조항에는 구체적인 비공개 사유가 적혀 있지 않아, 비공개 기준은 해당 규정을 확인해야 합니다."
    return {"kind": "referenced_publicity", "ids": [row["id"]], "used_ids": [row["id"]], "answer": answer}
