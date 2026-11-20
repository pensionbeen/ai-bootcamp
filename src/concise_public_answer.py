"""Remove a newly moved source label when all its locators are visible to the reader."""

import re

from citizen_answer_style import _INTRO, _public_label, readable_answer


# Quoted language, versions and competing legal references need their own antecedents.
_SENSITIVE = re.compile(
    r"""["'“”‘’「」『』`<>\[\]()《》〈〉«».!?。！？:;]|(?:조례|법|규칙|규정|고시|부칙|개정|시행|"""
    r"기준일|종전|당시|현행|판본|제정|별표|별지|별도\s*\d|제\s*\d+\s*(?:조|항|호|목)|"
    r"\d{4}\s*(?:년|[-./])|(?:같은|해당|위|앞의|그|이)\s*(?:조|항|호|법|표))"
)
_DEPENDENT = re.compile(r"^(?:이|그|해당|위|앞|같은|다만|또한|따라서|이에|다음|이러한|이때|그때)(?:\s|[은는의를가,])")
_GENERIC_TITLE_WORDS = {"기본", "관한", "의한", "위한", "등에", "따른", "대한", "설치", "관리", "운영", "지원", "활성화", "조성", "방지"}


def concise_answer(answer, citations):
    """Return display text; answer and the caller's public citations stay intact."""
    sources = tuple(citations or ())
    displayed = readable_answer(answer, sources)
    # An existing parenthesis is the author's text. Only remove an attribution
    # that readable_answer just moved, with *all* its locators publicly visible.
    if not isinstance(answer, str) or not sources or displayed == answer or "\n" in answer:
        return displayed
    intro = _INTRO.match(answer)
    if not intro:
        return displayed
    labels = {_public_label(c) if isinstance(c, str) else None for c in sources}
    law = intro["law"].strip("「」")
    positions = {_public_label(law + " " + intro["pos"])}
    heading = ""
    if intro["related"]:
        positions.add(_public_label(law + " " + intro["related"]))
        heading = intro["source"][intro.end("pos"):].split("(", 1)[0].strip()
    suffix = " (" + intro["source"] + ")."
    if positions != labels or not displayed.endswith(suffix):
        return displayed
    # Require the frozen formatter's actual move; a coincidental matching suffix
    # in user-written text does not establish provenance.
    body = displayed[:-len(suffix)]
    if not body or body != readable_answer(answer[intro.end():], [])[:-1]:
        return displayed
    if _DEPENDENT.match(body) or _SENSITIVE.search(body):
        return displayed
    words = re.findall(r"[가-힣]{2,}", law.removeprefix("서울특별시").removesuffix("조례"))
    subject = next((word for word in words if word not in _GENERIC_TITLE_WORDS), "")
    if not subject or subject not in body:
        return displayed
    # A separately published schedule identifies its scope via the actual table
    # title. Retain that title verbatim. Otherwise require Seoul in the claim.
    if not heading and "서울시" not in body and "서울특별시" not in body:
        return displayed
    return body + (f" ({heading})." if heading else ".")
