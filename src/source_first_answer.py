"""Select exact provisions from retrieval before drafting narrowly resolvable questions."""

import re
from pathlib import Path

import local_rag_answer as rag
from benefit_selection import benefit_evidence
from benefit_procedure import unmatched_benefit_procedure
from scoped_benefit_answer import scoped_benefit_answer
from complete_selected_evidence import _amount_companions
from delegated_fee_answer import delegated_fee_answer
from partial_benefit_answer import partial_benefit
from referenced_publicity_answer import referenced_publicity_answer
from source_candidates import candidate_rows
from simple_source_answer import simple_source_answer
from source_provision_style import concise_provision_answer
from tariff_selection import tariff_evidence


_FEE_QUESTION = re.compile(
    r"(?:(?P<city>서울시|서울특별시) )?(?P<facility>[가-힣]{2,20}?)(?:은|는|의|에서|에서는)? "
    r"(?:(?:돈|요금|이용료)(?:을|를) (?:내야 하나요|내나요)\?\s*낸다면 |"
    r"(?:이용 ?요금|이용료|주차요금|요금)(?:은|는|이|가)? )"
    r"얼마(?:인가요|예요|에요|입니까)\s*[?？.]?"
)
_FIELD = re.compile(r"([^:\n]+):\s*(\d{1,3}(?:,\d{3})+|\d+)\s*$")
_LOCATION = re.compile(r"제\s*\d+\s*조(?:의\d+)?")
_UNIT = re.compile(r"\(단위\s*[:：]\s*원(?:\s*/\s*구획)?\s*\)")


def _provision_text(text):
    text = re.sub(r"^\s*(?:[①-⑳]|\d+(?:의\d+)?[.)])\s*", "", text.strip())
    text = re.sub(r"\s*<개정[\d.,\s]+>\s*$", "", text)
    return text.replace(".다만", ". 다만").strip()


def _fee_schedule(question, rows):
    match = _FEE_QUESTION.fullmatch(" ".join(question.split()))
    if not match:
        return None
    facility = match["facility"]
    headers = [r for r in rows if r["locator"].startswith("별표") and facility in r["quote"]
               and r["unit_id"] != r["owner_id"]]
    rules = [r for r in rows if not r["locator"].startswith("별표") and facility in r["quote"]
             and "별표" in r["quote"] and "무료" in r["quote"]]
    if len(rules) != 1:
        return None
    rule = rules[0]
    scope = (rule["ordinance"], rule["source_url"], rule["effective_date"])
    if match["city"] and "서울특별시" not in scope[0]:
        return None
    same = [r for r in rows if (r["ordinance"], r["source_url"], r["effective_date"]) == scope]
    values = _amount_companions(f"{facility} 요금은 얼마인가요?", same, [rule, *headers])
    if len(values) != 1:
        return None
    value = next(r for r in same if r["id"] in values)
    # Only accept a single explicitly linked unscaled won table. Unknown notes
    # may qualify a fee, so their presence defers the question to normal generation.
    headings = [r for r in headers if r["id"] in value["required_ids"] and r["owner_id"] == value["owner_id"]
                and _UNIT.search(r["quote"])
                and re.sub(r"\s+", "", rule["locator"]) + "관련" in re.sub(r"\s+", "", r["quote"])]
    if len(headings) != 1:
        return None
    heading = headings[0]
    header_lines = [line.strip() for line in heading["quote"].splitlines() if line.strip()]
    title = re.escape(facility) + r"\s*(?:주차|이용|사용)?요금표\s*\(\s*제\s*[\d가-힣\s]+관련\s*\)"
    if (len(header_lines) not in (2, 3) or not re.fullmatch(title, header_lines[-2])
            or not _UNIT.fullmatch(header_lines[-1])
            or (len(header_lines) == 3 and not re.fullmatch(r"\[별\s*표(?:\s*\d+(?:의\d+)?)?\]", header_lines[0]))):
        return None
    by_id = {r["id"]: r for r in same}
    for dep_id in value["required_ids"]:
        dep = by_id.get(dep_id)
        if (not dep or dep["owner_id"] != value["owner_id"] or dep["public_label"] != value["public_label"]
                or dep["quote"].strip() not in (heading["quote"].strip(), header_lines[-1])):
            return None
    # Preserve fee qualifications belonging to the same article, including mandatory
    # exemptions. Leave unnamed or mixed provisions to the regular drafting path.
    article = _LOCATION.match(rule["locator"])
    if not article:
        return None
    related = [r for r in same if (m := _LOCATION.match(r["locator"])) and m[0] == article[0]]
    if any(facility not in r["quote"] or "요금" not in r["quote"] for r in related):
        return None
    fields = value["quote"].strip().splitlines()
    if not fields or not re.fullmatch(r"\s*구\s*분\s*[:：]\s*(?:주차|이용|사용)?요금\s*", fields[0]):
        return None
    amounts = []
    for line in fields[1:]:
        field = _FIELD.fullmatch(line.strip())
        if not field or not re.search(r"시간|분당|일|월|주|년|정기권", field[1]):
            return None
        amounts.append(f"{field[1].strip()} {int(field[2].replace(',', '')):,}원")
    if not amounts:
        return None
    denominator = "1구획당 " if re.search(r"원\s*/\s*구획", heading["quote"]) else ""
    price = f"별표의 {denominator}기준액은 " + ", ".join(amounts) + "입니다."
    ids = [value["id"], heading["id"], *(r["id"] for r in related)]
    answer = " ".join([*(_provision_text(r["quote"]) for r in related), price])
    return {"kind": "fee_schedule", "ids": ids, "used_ids": ids.copy(), "answer": answer, "price": price}


def resolve(question, rows):
    if not rows:
        return None
    plan = (benefit_evidence(question, rows) or scoped_benefit_answer(question, rows)
            or tariff_evidence(question, rows) or _fee_schedule(question, rows)
            or partial_benefit(question, rows) or simple_source_answer(question, rows)
            or delegated_fee_answer(question, rows) or referenced_publicity_answer(question, rows))
    if plan and plan["kind"] == "single_benefit" and plan["answer"] is None:
        # One standalone operative item contains the grant and any exceptions. Keep
        # its wording intact instead of asking a writer to guess which numbers matter.
        row = next(r for r in rows if r["id"] == plan["ids"][0])
        plan = {**plan, "answer": _provision_text(row["quote"]), "used_ids": [row["id"]]}
    if plan and plan["kind"] in ("single_benefit", "fee_schedule"):
        concise = concise_provision_answer(question, plan, rows)
        if concise:
            plan = {**plan, "answer": concise}
    return plan


def public_preview(plan, rows):
    if not plan or not plan["answer"]:
        return None
    used = set(plan["used_ids"])
    cited = [r for r in rows if r["id"] in used]
    if not cited or len(used) != len(cited):
        return None
    from concise_public_answer import concise_answer
    from public_rag_response import scope_boundary_notice
    labels = list(dict.fromkeys(r["public_label"] for r in cited))
    answer = plan["answer"]
    if plan.get("missing_materials"):
        answer += " " + scope_boundary_notice(plan["missing_materials"], [], source_kind=plan["kind"])["text"]
    return {"answer": concise_answer(answer, labels), "citation": labels,
            "source_url": cited[0]["source_url"], "effective_date": cited[0]["effective_date"]}


def prepare_draft(question, citations, originals, crosswalk, project, expanded, units, primaries, conditions):
    if not re.search(r"할인|감면|면제|깎|과태료|점용료|야간|주간|전일|요금|이용료|돈을 내|수당|지원금|장려금|보조금|축하금|공개", question):
        return None
    try:
        rows, _ = candidate_rows(citations, originals, expanded, units, primaries, conditions,
                                 project or Path(__file__).resolve().parents[1])
        conflicting = unmatched_benefit_procedure(question, originals, conditions)
        if conflicting:
            draft = {"decision": "insufficient_evidence", "missing_information": [], "missing_materials": [],
                     "claims": [], "calculations": []}
            return draft, {"kind": "unidentified_benefit_procedure", "candidate_count": len(rows),
                           "competing_provisions": conflicting, "used_unit_ids": [], "draft_model_called": False}
        plan = resolve(question, rows)
    except (OSError, ValueError, KeyError):
        # Optional source routing cannot authorize an answer on incomplete or
        # unrecognized provenance. The normal pipeline still verifies its own input.
        return None
    if not plan or not plan["answer"]:
        return None
    used = set(plan["used_ids"])
    selected = [r["unit_id"] for r in rows if r["id"] in used]
    aliases = {uid: alias for alias, uid in crosswalk.items()}
    if not used or len(selected) != len(used) or not all(i in aliases for i in selected):
        return None
    claim = {"text": plan["answer"], "citation_ids": selected}
    checks = rag._check_claims([claim], [], claim["text"], citations, question, originals)
    if checks["issues"] or rag._draft_presentation_issues(claim["text"]):
        return None
    missing = plan.get("missing_materials", [])
    if missing and rag._self_referential_missing_materials(missing):
        return None
    generated = {"decision": "external_material_required" if missing else "supported",
                 "missing_information": [], "missing_materials": missing, "calculations": [],
                 "claims": [{"text": claim["text"], "citation_ids": [aliases[i] for i in selected]}]}
    trace = {"kind": plan["kind"], "candidate_count": len(rows), "selected_ids": plan["ids"],
             "used_unit_ids": selected, "draft_model_called": False}
    return generated, trace
