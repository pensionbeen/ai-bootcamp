"""Conservative display edits on an already generated, cited Korean answer."""

import re


_LAW = r"서울특별시[ 가-힣ㆍ·]+?조례"
_ARTICLE = r"제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호(?:의\d+)?)?"
_POSITION = rf"(?:{_ARTICLE}|별표(?:[ \t]*\d+)?|별도[ \t]*\d+)"
_LABEL = re.compile(rf"(?P<law>{_LAW})[ \t]+(?P<pos>{_POSITION})")
_INTRO = re.compile(
    rf"(?P<source>(?P<law>「{_LAW}」|{_LAW})[ \t]+(?P<pos>{_POSITION})"
    rf"(?:[ \t]+[가-힣][가-힣 ]{{0,36}}표\((?P<related>{_ARTICLE})[ \t]*관련\))?)"
    r"에[ \t]*따르면,?[ \t]+"
)
_RELATIVE = re.compile(r"(?:같은|앞의|해당|동|위|이)\s*(?:조례|법|규칙|조|항|호|목)")
_SENTENCE_END = re.compile(r"(?<=[다요죠])[.](?=\s|$)")
_QUOTES = {'"': '"', "'": "'", "‘": "’", "“": "”", "「": "」", "『": "』", "`": "`"}
_PERCENT = re.compile(
    r"(?<![\d.,/\-+−])100[ \t]*분의[ \t]*(?P<n>\d+(?:\.\d+)?)"
    r"(?![\d%]|[.,]\d)(?:(?P<particle>으로|로|을|를|은|는|과|와|[이가](?=\s))"
    r"|(?=[ \t]+(?:이상|이하|미만|초과|범위|한도|할인|감면|상당))|(?=입니다|이다))"
)
_PARTICLE = {"으로": "로", "을": "를", "은": "는", "과": "와", "이": "가"}
_TECHNICAL = re.compile(r"[=+×÷*/~∼～]|(?:\d|\s)[−–-]\s*\d")
_VERBATIM = re.compile(r"^[ \t]*>|^[ \t]*(?:원문|조문\s*원문|직접\s*인용)\s*[:：]", re.MULTILINE)


def _quoted_spans(text):
    """Keep literal quotations, including nested or unterminated ones, unchanged."""
    end_marks = []
    start = 0
    for i, char in enumerate(text):
        if end_marks and char == end_marks[-1]:
            end_marks.pop()
            if not end_marks:
                yield start, i + 1
        elif char in _QUOTES:
            if not end_marks:
                start = i
            end_marks.append(_QUOTES[char])
    if end_marks:
        yield start, len(text)


def _percent(match):
    suffix = match["particle"] or ""
    return match["n"] + "%" + _PARTICLE.get(suffix, suffix)


def _public_label(text):
    clean = text.replace("「", "").replace("」", "")
    if not _LABEL.fullmatch(clean):
        return None
    return re.sub(r"\s+", "", clean)


def _put_conclusion_first(paragraph, labels):
    intro = _INTRO.match(paragraph)
    if not intro or _public_label(intro["law"] + " " + intro["pos"]) not in labels:
        return paragraph
    if intro["related"] and (not intro["pos"].startswith(("별표", "별도"))
                             or _public_label(intro["law"] + " " + intro["related"]) not in labels):
        return paragraph
    body = paragraph[intro.end():]
    # A heading may introduce multiple statements. Move it only when its scope
    # is one complete sentence, retaining the whole legal locator verbatim.
    ends = list(_SENTENCE_END.finditer(body))
    if len(ends) != 1 or ends[0].end() != len(body) or _INTRO.match(body):
        return paragraph
    return body[:-1] + " (" + intro["source"] + ")."


def readable_answer(answer, citations):
    """Return display text without mutating the public sources or underlying audit."""
    if not isinstance(answer, str) or not answer.strip() or answer.strip() == "확인 불가":
        return answer
    spans = list(_quoted_spans(answer))
    labels = {_public_label(c) for c in citations or () if isinstance(c, str)} - {None}
    # Do not move the antecedent of a relative legal reference.
    allow_move = bool(labels) and not _RELATIVE.search(answer)
    # Keep blank lines intact, including CRLFs. Whitespace within a paragraph
    # (for example a table or list) has structural meaning.
    parts = re.split(r"(\r?\n[ \t]*\r?\n)", answer)
    offset = 0
    for i, part in enumerate(parts):
        end = offset + len(part)
        if i % 2 == 0 and part and not _VERBATIM.search(part):
            text = part
            if "분의" in part and not _TECHNICAL.search(part):
                def replace(match):
                    at = offset + match.start()
                    if any(start <= at < stop for start, stop in spans):
                        return match.group()
                    return _percent(match)
                text = _PERCENT.sub(replace, text)
            if allow_move and "\n" not in part and not any(start < offset < stop for start, stop in spans):
                text = _put_conclusion_first(text, labels)
            parts[i] = text
        offset = end
    return "".join(parts)
