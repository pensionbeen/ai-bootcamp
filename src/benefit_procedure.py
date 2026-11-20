"""Recognize a filing question that cannot identify which of several fee schemes applies."""
import re

from scoped_benefit_answer import _INTRO
from source_provision_style import _body


# Account for the whole question. An unfamiliar beneficiary, place, document,
# named service, amount or additional request leaves the normal answer path in charge.
_PROCEDURE_WORD = re.compile(
    r"(?:감면|면제|할인|혜택|요금|이용료|신청|서류|구비서류|제출서류|증빙(?:서류|자료)?|증명|자료|"
    r"준비물|증명서|신청서|제출처|접수처|접수|곳|방법|절차|때|시|것)(?:에는|에서|으로|에|로|은|는|을|를|도|과|와|이|가|의)?|"
    r"(?:감면|할인|면제)?(?:을|를)?받(?:으려면|고|을|으려는데)|"
    r"(?:신청|접수|제출|제시|준비|확인)?(?:하려면|하려고|하려는데|하는데|하는|하고|할|해야|해야하나요|하나요|합니까|합니다|하죠|하면)|"
    r"깎아|주(?:는|나요)|내(?:야|면|나요)|챙겨|가면|되(?:나요|죠|려면)|"
    r"어디(?:에|로|서|에서|인가요)?|누구에게|어느|어떤|무슨|무엇(?:을|이)?|뭘|뭐(?:가|를|예요)?|"
    r"필요(?:한|하나요|한가요|합니까|해요)|알려(?:주세요|주실)|주세요|궁금(?:합니다|해요)|싶(?:어요|습니다|은데)|그리고|좀"
)
_PROCEDURE = re.compile(r"제출|접수|제시|지참|첨부|신청|구비|준비물|서류|증빙|절차|"
                        r"(?:감면|할인|면제)에 관하여[^.\n]{0,80}준용|"
                        r"(?:신분증|등록증|증명서).{0,25}(?:필수|필요|확인|의무|갖추|내야|보여)")
_GRANT = re.compile(r"(?<![가-힣])([가-힣]*(?:요금|수수료|입장료|사용료|이용료|점용료|대여료))[을를] (?:감면|면제|할인)(?:한다|할 수 있다)")


def unmatched_benefit_procedure(question, originals, conditions):
    if (not re.search(r"감면|면제|할인|깎", question) or not re.search(r"서류|증빙|증명서|제출|접수|준비물|신청서|제시", question)
            or not re.fullmatch(r"[가-힣\s,.?!？]+", question)
            or not all(_PROCEDURE_WORD.fullmatch(word) for word in re.findall(r"[가-힣]+", question))):
        return []
    grants = {}
    waivers = []
    for row in originals:
        if row["kind"] != "body":
            continue
        lines = row["quote"].strip().splitlines()
        if not lines:
            continue
        header = _body(lines[0])
        if fee := _GRANT.search(header):
            grants[(row["ordin_seq"], fee[1])] = row["unit_id"]
        rule = _INTRO.fullmatch(header)
        if not rule or not rule["systems"]:
            continue
        # Each remaining procedural clause must be a listed recipient's proof,
        # never another general obligation or a later override.
        proofs = [line.strip() for line in lines[1:] if _PROCEDURE.search(line)]
        if all(re.match(r"^(?:\d+(?:의\d+)?|[가-하])\.\s*", line)
               and re.search(r"제시(?:한 |하는 |하고 | 또는 ).*?(?:경우|사람|차량)", line)
               and not re.search(r"제출|접수|지참|첨부|신청|(?:다만|불구하고)[^.]*제시", line)
               for line in proofs):
            waivers.append((row, rule["fee"]))
    if len(waivers) != 1:
        return []
    owner, fee = waivers[0]
    others = [uid for (law, subject), uid in grants.items() if law != owner["ordin_seq"] and subject != fee]
    if not others:
        return []
    # The only documentary rule we can explain applies to this fee. Inspect
    # full supplied contexts and conditions too: a shared procedure takes precedence.
    scoped_text = " ".join(owner["quote"].split())
    texts = [(" ".join(r["quote"].split()), r["ordin_seq"] == owner["ordin_seq"]) for r in originals]
    texts.extend((" ".join(c["condition"]["quote"].split()), c["source"].get("source_url") == owner["source_url"])
                 for c in conditions)
    for text, same_source in texts:
        if same_source and text in scoped_text:
            continue
        if _PROCEDURE.search(text.replace(scoped_text, "") if same_source else text):
            return []
    return [owner["unit_id"], *others]
