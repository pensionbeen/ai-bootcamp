"""Short public wording for one fully sourced, two-benefit non-stacking conclusion."""

import re
from itertools import permutations

import complete_selected_evidence as selection
from answer_fact_coverage import _rates
from public_rag_response import citation_label


_NAME = r"[가-힣]+(?: [가-힣]+){0,3}"
_VENUE = r"공영[가-힣]{2,8}"
_BOTH = r"(?:둘 다|동시에|함께|같이)"
_QUESTION = re.compile(
    rf"(?:(?P<front>{_VENUE})에서(?:는)? )?(?P<a>{_NAME}) (?:할인|감면)(?:과|와| 및) "
    rf"(?P<b>{_NAME}) (?:할인|감면)(?:을|를|은|는|이|가|,)? "
    rf"(?:(?P<middle>{_VENUE})에서(?:는)? )?(?:두 (?:할인|감면|혜택)(?:을|를|은|는)? )?"
    rf"(?:{_BOTH} (?:받을 수 있(?:나요|습니까|을까요)|적용(?:되나요|됩니까|할 수 있나요))|"
    r"중복(?: 적용)?(?:이|은)? (?:가능한가요|가능합니까|되나요)|중복(?:으로)? 적용되나요|"
    r"중복(?:으로)? 받을 수 있나요)\?"
)
_SPECIAL = re.compile(r"예외|제외|환승|심야|야간|주말|평일|거주|미소지|미보유|"
                      r"제출|신청|절차|최초|이후|시간|기한|한도|이상|이하|미만|초과|경우에만|전용|동반")
_RULE = re.compile(r"(?:둘|두 가지|두 개) 이상의 (?:[가-힣]+ )?(?:감면|할인)\s*사유에 해당하는 "
                   r"(?:때|경우)에는 (?:그 중 )?(?:감면|할인)율이 높은 하나만 적용한다\.")
_AMEND = re.compile(r"\s*<개정\s+\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.?>\s*$")


def _text(value):
    return re.sub(r"\s+", " ", value).strip()


def _source_prefix(raw):
    locator = raw["locator"]
    name = raw["ordin_name"]
    full = citation_label(raw, {}, detail=True).removeprefix(name).strip()
    parts = full.split()
    options = [r"(?:「?" + re.escape(name) + r"」?\s*)?" + r"\s*".join(map(re.escape, parts))]
    if locator.get("item") is not None:
        options.append(r"같은\s*항\s*" + re.escape(parts[-1]))
    elif locator.get("paragraph") is not None:
        options.append(r"같은\s*조\s*" + re.escape(parts[-1]))
    return r"(?:(?:" + "|".join(options) + r")에\s*따라\s+)?"


def _fact(row, name):
    raw = row["raw"]
    # Work only from the ordinary rate clause. The question and the verified claim
    # must independently exclude a request about later exceptions or proof procedures.
    body = re.sub(r"^\s*\d+(?:의\d+)?[.)]\s*(?:다음의\s+)?", "", raw["quote"])
    clause = _text(re.split(r"\n|[.](?=\s|$)|다만", body, maxsplit=1)[0])
    rates = list(_rates(clause))
    if len(rates) != 1 or _SPECIAL.search(clause) or not selection._has_name(clause, name):
        return None
    start, end, fraction = rates[0]
    percent = fraction * 100
    scaled = percent * 1000
    if scaled.denominator != 1 or not 0 < percent <= 100:
        return None
    whole, rest = divmod(scaled.numerator, 1000)
    display = f"{whole}.{rest:03d}".rstrip("0").rstrip(".")
    action = re.fullmatch(r"(?:을|를)?\s*(할인|감면)(?:한다|할 수 있다)", clause[end:])
    if not action:
        return None
    # Reconstruct the permissible original assertion using the whole source predicate,
    # including who holds the benefit. Nothing from an unverified extra clause is consumed.
    number = re.escape(display)
    expression = rf"(?:{re.escape(clause[start:end])}|{number}\s*(?:%|퍼센트)|100\s*분의\s*{number})"
    full = (re.escape(clause[:start]) + expression + r"(?:을|를)?\s*" + action[1]
            + r"(?:할 수 (?:있고|있으며|있다|있습니다)|하고|하며|한다|합니다)")
    simple = (re.escape(name) + r" (?:할인|감면)(?:율)?(?:은|는|이|가)? " + expression
              + r"(?:이고|이며|이다|입니다|다)?")
    return name, display, _source_prefix(raw) + "(?:" + full + "|" + simple + ")"


def _verified_wording(text, facts, rule, rule_clause, venue):
    a, b = (re.escape(f[0]) + r" (?:할인|감면)" for f in facts)
    pair = rf"(?:(?:{a}(?:과|와| 및) {b}|{b}(?:과|와| 및) {a})(?:을|를|은|는|이|가)? |두 (?:할인|감면|혜택)(?:을|를|은|는)? )?"
    where = re.escape(venue) + r"에서(?:는)? " if venue else ""
    where = "(?:" + where + ")?" if where else ""
    no_stack = (where + pair + where + r"(?:"
                + _BOTH + r" 받을 수(?:는)? (?:없다|없습니다|없고|없으며)|"
                r"중복(?: 적용| 할인)?(?:은|이)? (?:불가(?:하다|합니다|하며)|불가능(?:하다|합니다|하며)|"
                r"되지 (?:않는다|않습니다|않고))|중복(?:으로)? 받을 수 (?:없다|없습니다|없고))")
    source_rule = re.escape(rule_clause.removesuffix("적용한다."))
    short_rule = r"(?:(?:두 (?:감면|할인)(?:율)?|둘) 중 )?(?:감면율|할인율)이 (?:더 )?높은 (?:하나|한 가지)만 "
    higher = _source_prefix(rule) + "(?:" + source_rule + "|" + short_rule + ")" + r"적용(?:하므로|한다|합니다|됩니다|하고|된다)"
    # Consume the entire verified claim. Reordering these four supported facts is fine,
    # but an additional eligibility condition, place, time, exception or grant is not.
    separator = r"(?:,\s*|\.\s+|\s+)(?:다만,?\s+)?"
    return any(re.fullmatch(separator.join(parts) + r"\.", _text(text))
               for parts in permutations((facts[0][2], facts[1][2], higher, no_stack)))


def _verified_comparison(question, public, result):
    query = _QUESTION.fullmatch(question)
    if (not query or query["a"] == query["b"] or query["front"] and query["middle"]
            or _SPECIAL.search(query["a"] + " " + query["b"])):
        return None
    claims, checks, draft = result["generated_claims"], result["code_checks"], result["draft_response"]
    if (result["decision"] != "supported" or result["draft_status"] != "code_checked_draft"
            or result["citation_contract"] != "exact_source_subunits_v1" or checks["status"] != "passed"
            or checks["issues"] or checks["checked_claims"] != claims or len(claims) != 1):
        return None
    claim = claims[0]
    if claim["text"] != public["answer"] or claim["text"] != draft["answer"]:
        return None
    ids = claim["citation_ids"]
    if len(ids) != 3 or len(set(ids)) != 3:
        return None
    sources = [r for r in draft["citation"] if r["unit_id"] in ids]
    if (len(sources) != 3 or {r["unit_id"] for r in sources} != set(ids)
            or any(r.get("context_only") or not r["locator"].get("article") for r in sources)):
        return None
    labels = list(dict.fromkeys(citation_label(r, {}, detail=False) for r in sources))
    if (labels != public["citation"] or any(r["source_url"] != public["source_url"]
                                            or r["effective_date"] != public["effective_date"] for r in sources)):
        return None
    rows = [{"id": r["unit_id"], "unit_id": r["unit_id"], "owner_id": r["parent_chunk_id"],
             "ordinance": r["ordin_name"], "source_url": r["source_url"], "effective_date": r["effective_date"],
             "quote": r["quote"], "raw": r} for r in sources]
    scope = {(selection._source(r), r["owner_id"], r["raw"]["locator"]["article"]) for r in rows}
    if (len(scope) != 1 or not all([*selection._source(rows[0]), rows[0]["owner_id"]])
            or any(r["unit_id"] != r["owner_id"] and not r["unit_id"].startswith(r["owner_id"] + "::") for r in rows)):
        return None
    venues = set(re.findall(r"(?<!\w)(" + _VENUE + r")에서", claim["text"]))
    requested = query["front"] or query["middle"]
    if len(venues) > 1 or requested and requested not in venues:
        return None
    venue = next(iter(venues), "")
    if venue and venue.removeprefix("공영") not in rows[0]["ordinance"]:
        return None
    # The shared extractor handles the two verified legal names; the independently
    # parsed question above establishes whether non-stacking was actually asked.
    comparison = f"{query['a']} 할인과 {query['b']} 할인을 둘 다 받을 수 있나요?"
    direct = selection._comparison_companions(comparison, rows, rows)
    if len(direct) != 2:
        return None
    items = [r for r in rows if r["id"] in direct]
    if (any(r["raw"]["kind"] != "body" or r["raw"]["locator"].get("item") is None for r in items)
            or len({r["raw"]["locator"].get("paragraph") for r in items}) != 1):
        return None
    rule = next(r["raw"] for r in rows if r["id"] not in direct)
    clause = _text(re.sub(r"^\s*[①-⑳]\s*", "", _AMEND.sub("", rule["quote"])))
    if (not _RULE.fullmatch(clause) or rule["locator"].get("item") is not None
            or rule["kind"] not in {"body", "required_context"}):
        return None
    facts = []
    for name in (query["a"], query["b"]):
        found = [fact for row in items if (fact := _fact(row, name))]
        if len(found) != 1:
            return None
        facts.append(found[0])
    if facts[0][1] == facts[1][1] or not _verified_wording(claim["text"], facts, rule, clause, venue):
        return None
    a, b = facts
    location = venue + "에서는 " if venue else ""
    brief = (f"{location}두 감면 요건을 모두 충족해도 중복 할인은 받을 수 없습니다. "
             f"{a[0]} 할인({a[1]}%)과 {b[0]} 할인({b[1]}%) 중 할인율이 높은 하나만 적용됩니다.")
    return brief if len(brief) < len(public["answer"]) else None


def compact_discount_comparison(question, public_response, audit_result):
    """Return only replacement text. Existing public sources and the underlying audit stay attached."""
    try:
        return _verified_comparison(question, public_response, audit_result)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
