"""Read-only cosine retrieval from the local SQLite RAG database.

Restores mandatory contexts and exact-ID condition candidates. This module does
not call an API, decide legal applicability, or generate a final answer.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

ASOF = "2026-10-01"
DIMENSION = 1024
MAX_REFERENCE_ROWS = 8
MAX_ADDED_REFERENCE_CHUNKS = 16
# Existing link_attachment_article.EXTERNAL_ANNEX_OWNER grammar, applied to
# stored quote text here without running the collection/link-building pipeline.
_EXTERNAL_ANNEX_OWNER = re.compile(
    r"(?:「[^」\n]+」|(?<![가-힣])(?:같은\s*법\s*시행규칙|같은\s*법\s*시행령|"
    r"법\s*시행규칙|법\s*시행령|같은\s*법|같은\s*영|시행규칙|시행령|영|법))"
    r"\s*(?:제\s*[0-9]+\s*조(?:의\s*[0-9]+)?"
    r"(?:\s*제\s*[0-9]+\s*항)?(?:\s*제\s*[0-9]+\s*호)?\s*)?$"
)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def vector_norm(vector):
    require(isinstance(vector, list) and len(vector) == DIMENSION and
            all(type(x) in (int, float) and math.isfinite(x) for x in vector), "invalid_1024_dimensional_vector")
    norm = math.hypot(*vector)
    require(math.isfinite(norm) and norm > 0, "invalid_vector_norm")
    return norm


def load_vector_file(path):
    value = json.loads(Path(path).read_bytes())
    if isinstance(value, dict):
        value = value.get("embedding", value.get("query_vector"))
    vector_norm(value)
    return value


def _compact(text):
    return re.sub(r"\s+", "", text)


def _question_text(text):
    # Keep numeric token boundaries: "별표 1 1급지" is not "별표 11".
    return re.sub(r"\s+", lambda match: " " if match.start() and match.end() < len(text)
                  and text[match.start() - 1].isdigit() and text[match.end()].isdigit() else "", text)


def _term_present(term, text):
    pattern = (r"(?<!\d)" if term[:1].isdigit() else "") + re.escape(term)
    pattern += r"(?!\d)" if term[-1:].isdigit() else ""
    return re.search(pattern, text) is not None


def _scope(record):
    source = record["source"].get("source_record", {})
    source = source.get("source", source)
    values = (source.get("ordin_seq"), source.get("as_of_date"),
              source.get("effective_date"), source.get("content_hash"))
    if (not all(isinstance(value, str) and value for value in values) or
            values[:3] != (record["ordin_seq"], record["as_of_date"], record["effective_date"])):
        return None
    return values


def _scope_value(scope):
    return dict(zip(("ordin_seq", "as_of_date", "effective_date", "content_hash"), scope)) if scope else None


def _attachment_bundle(record):
    source = record["source"].get("source_record", {})
    return source.get("grounding_bundle", source)


def _attachment_label(record):
    # Read the attachment's own heading, never a mention inside a fee row.
    for context in _attachment_bundle(record).get("required_contexts", []):
        if context.get("kind") == "ordered_attachment_text_across_pages":
            first_line = context.get("text", "").splitlines()
            match = re.search(r"\[별\s*표(?:\s*(\d+)(?:\s*의\s*(\d+))?)?\]", first_line[0] if first_line else "")
            if match:
                return "별표" + (" " + str(int(match[1])) if match[1] else "") + ("의" + str(int(match[2])) if match[2] else "")
    # Reviewed legacy rows carry their own explicit annex header; their shared
    # conditions may cite another law's annex and must not supply this label.
    source = record["source"].get("source_record", {})
    for line in source.get("primary_text", "").splitlines()[:3]:
        match = re.match(r"^별\s*표\s*(\d+)(?:\s*의\s*(\d+))?\s*:", line.strip())
        if match:
            return "별표 " + str(int(match[1])) + ("의" + str(int(match[2])) if match[2] else "")
    return None


def _row_terms(record):
    source = record["source"].get("source_record", {})
    terms = set()
    for line in source.get("primary_text", "").splitlines():
        name, separator, value = line.partition(":")
        if separator and _compact(name) in {"공원별", "종별", "구분", "시설명", "시설물", "항목", "행", "점용대상"}:
            value = re.sub(r"^\s*\d+\.\s*", "", value)
            for term in (value, value.partition("(")[0]):
                term = _compact(term)
                # No bare numbers: age/table numbers must not select fee rows.
                if len(term) >= 2 and re.search(r"[가-힣A-Za-z]", term) and term != "공통":
                    terms.add(term)
    return terms


def _verified_body_parent(record, connection):
    """Verify the stored body leaf against its linked original parent, once per ID."""
    raw = record["source"].get("source_record", {})
    parent, span = raw.get("parent_id"), raw.get("source_span", {})
    start, end = span.get("start"), span.get("end")
    paragraph, quote = raw.get("paragraph"), raw.get("quote_text")
    if (record["kind"] != "body" or record["is_deleted"] != 0 or raw.get("is_deleted") is not False or
            raw.get("chunk_id") != record["chunk_id"] or record["source"].get("source_unit_id") != parent or
            not isinstance(parent, str) or "paragraph" not in raw or
            (paragraph is not None and (type(paragraph) is not int or paragraph <= 0)) or
            raw["chunk_id"] != parent + ":" + (str(paragraph) if paragraph is not None else "all") or
            not isinstance(quote, str) or type(start) is not int or type(end) is not int):
        return False
    row = connection.execute(
        "SELECT c.source_json,c.text FROM chunk_context_links l JOIN contexts c ON c.context_id=l.context_id "
        "WHERE l.chunk_id=? AND l.context_id=? AND l.relation='required_parent'",
        (record["chunk_id"], parent)).fetchone()
    if row is None:
        return False
    original = json.loads(row["source_json"]).get("source_record", {})
    text = row["text"]
    return (original.get("article_id") == parent and original.get("article") == raw.get("article") and
            original.get("source_text") == text and original.get("source_text_hash") == hashlib.sha256(text.encode()).hexdigest() and
            0 <= start < end <= len(text) and text[start:end] == quote and
            any(segment.get("kind") == "provision" and "paragraph" in segment and segment.get("paragraph") == paragraph and
                segment.get("start") == start and segment.get("end") == end and segment.get("text") == quote
                for segment in original.get("segments", [])))


def _attachment_body_expansion(by_id, scopes, ranked, added, eligible, connection, state, named_scopes):
    """Follow exact title targets with a verified body→annex edge; never all inverse mentions."""
    seen, body_valid = set(), {}
    # A single bounded step over already selected attachments prevents cycles
    # and does not turn newly added bodies into another round of row selection.
    for cid in sorted(set(ranked) | added):
        row = by_id[cid]
        if (row["kind"] != "attachment" or scopes[cid] is None or
                named_scopes and scopes[cid] not in named_scopes):
            continue
        raw = row["source"].get("source_record", {})
        source_metadata = raw.get("source", {})
        related = raw.get("related_body", {})
        title_refs = related.get("title_references", [])
        if related.get("title_reference_status") != "matched_body_reference" or not title_refs:
            continue
        heading = row["source"].get("catalog_record", {}).get("heading", [])
        title = heading[1] if len(heading) == 2 and isinstance(heading[1], str) else ""
        attachment_id, label = raw.get("attachment_id"), _attachment_label(row)
        primary_refs = raw.get("source_refs", [])
        if (not title or not label or not isinstance(attachment_id, str) or not source_metadata or
                attachment_id.split(":", 1)[0] != source_metadata.get("ordin_seq") or
                not primary_refs or any(ref.get("attachment_id") != attachment_id for ref in primary_refs)):
            continue
        headers = []
        for link in connection.execute(
                "SELECT c.source_json,c.text FROM chunk_context_links l JOIN contexts c ON c.context_id=l.context_id "
                "WHERE l.chunk_id=?", (cid,)):
            context = json.loads(link["source_json"]).get("provenance", {})
            refs = context.get("source_refs", [])
            if (context in _attachment_bundle(row).get("required_contexts", []) and
                    context.get("kind") == "ordered_attachment_text_across_pages" and
                    context.get("text") == link["text"] and _compact(title) in _compact(link["text"]) and
                    refs and all(ref.get("attachment_id") == attachment_id and
                                 any(all(ref.get(key) == primary.get(key) and ref.get(key) for key in
                                         ("parse_file", "parse_content_hash")) for primary in primary_refs)
                                 for ref in refs)):
                headers.append(context)
        for ref in title_refs:
            targets = ref.get("target_chunk_ids", [])
            marker = (attachment_id, ref.get("article"), ref.get("paragraph"))
            if marker in seen:
                continue
            seen.add(marker)
            request = {"reference_id": "attachment-body:" + attachment_id + ":" + str(ref.get("article")) +
                       ":" + str(ref.get("paragraph")), "reference": str(ref.get("article")), "kind": "body",
                       "origin": "explicit_attachment_title_reference", "source_chunk_id": cid,
                       "scope": _scope_value(scopes[cid])}
            state["requested"].append(request)
            span = ref.get("reference_span", {})
            start, end = span.get("start"), span.get("end")
            paragraph = ref.get("paragraph")
            expected = "(" + str(ref.get("article")) + ("제" + str(paragraph) + "항" if paragraph is not None else "") + "관련)"
            valid = (headers and raw.get("chunk_id") == cid and
                     row["source"].get("source_unit_id") == source_metadata["ordin_seq"] + ":attachment:" + attachment_id.split(":", 1)[-1] and
                     ref.get("method") == "explicit_attachment_title_reference" and
                     (paragraph is None or type(paragraph) is int and paragraph > 0) and
                     type(start) is int and type(end) is int and 0 <= start < end <= len(title) and
                     title[start:end] == ref.get("reference_text") and _compact(title[start:end]) == _compact(expected) and
                     isinstance(targets, list) and targets and all(isinstance(target, str) for target in targets))
            selected = set(targets) if valid else set()
            for target_id in sorted(selected):
                target = by_id.get(target_id)
                source = target["source"].get("source_record", {}) if target else {}
                if target_id not in body_valid:
                    body_valid[target_id] = bool(target and _verified_body_parent(target, connection))
                matches = []
                for body_ref in related.get("body_references", []):
                    quote, location = body_ref.get("quote_text"), body_ref.get("reference_span", {})
                    lo, hi = location.get("start"), location.get("end")
                    if (body_ref.get("chunk_id") == target_id and body_ref.get("method") == "explicit_body_reference" and
                            quote == source.get("quote_text") and isinstance(quote, str) and
                            type(lo) is int and type(hi) is int and 0 <= lo < hi <= len(quote) and
                            quote[lo:hi] == body_ref.get("reference_text") and _compact(quote[lo:hi]) == _compact(label) and
                            body_ref.get("article") == source.get("article") and body_ref.get("paragraph") == source.get("paragraph") and
                            not _EXTERNAL_ANNEX_OWNER.search(quote[:lo].rsplit("\n", 1)[-1])):
                        matches.append(body_ref)
                if (target_id not in eligible or scopes.get(target_id) != scopes[cid] or not body_valid[target_id] or
                        source.get("source_url") != source_metadata.get("source_url") or
                        source.get("article") != ref.get("article") or
                        paragraph is not None and source.get("paragraph") != paragraph or not matches):
                    valid = False
            if not valid:
                state["unresolved"].append({**request, "reason": "attachment_body_reference_not_verified",
                                            "scope_status": "local_corpus_unresolved"})
                continue
            if len((added | selected) - set(ranked)) > MAX_ADDED_REFERENCE_CHUNKS:
                state["unresolved"].append({**request, "reason": "reference_budget_exceeded",
                    "scope_status": "local_corpus_unresolved", "budget_kind": "added_reference_chunks",
                    "candidate_count": len((added | selected) - set(ranked)), "candidate_limit": MAX_ADDED_REFERENCE_CHUNKS})
                continue
            added.update(selected)
            state["resolved"].append({**request, "primary_ids": sorted(selected),
                "context_ids": sorted({by_id[target]["source"]["source_record"]["parent_id"] for target in selected}),
                "reason": "verified_attachment_body_target", "candidate_only": True, "applicability_established": False})


def _reference_expansion(by_id, scores, ranked, eligible, question, connection):
    state = {"requested": [], "resolved": [], "unresolved": []}
    if question is None:
        return set(), state, []
    query = _question_text(question)
    scopes = {cid: _scope(row) for cid, row in by_id.items()}
    groups, names = {}, {}
    # Fixed chunk-id order: group members and group order feed the reverse
    # body-reference loop below, so a set's hash order must not choose them.
    for cid in sorted(eligible):
        row, scope = by_id[cid], scopes[cid]
        if scope is None:
            continue
        source = row["source"].get("source_record", {})
        name = source.get("ordin_name")
        if not name:
            headings = row["source"].get("catalog_record", {}).get("heading", [])
            name = headings[0] if headings else row.get("title")
        if isinstance(name, str):
            full = _compact(name)
            short = re.sub(r"^(?:서울특별시|서울시)", "", full)
            for alias in {full, short, short.removesuffix("조례")}:
                if len(alias) >= 2:
                    names.setdefault(alias, set()).add(scope)
        if row["kind"] == "attachment":
            label = _attachment_label(row)
            if label:
                groups.setdefault((scope, label), []).append(cid)

    # Quoted other laws/rules own their following references. A local ordinance
    # name embedded inside a rule's title is not a local scope declaration.
    markers = []
    quoted = list(re.finditer(r"「([^」]+)」", query))
    for match in quoted:
        markers.append((match.start(), match.end(), names.get(match[1], set())))
    for alias, owners in names.items():
        for match in re.finditer(re.escape(alias), query):
            if any(q.start() <= match.start() < q.end() for q in quoted):
                continue
            tail = query[match.end():]
            rule = re.match(r"(?:조례)?(?:시행규칙|시행령|규칙|법률|법)|"
                            r"(?:및|등)[^별제「」,.!?]*?(?:법률|시행규칙|시행령|법)", tail)
            prefix = query[:match.start()]
            municipality = re.search(r"[가-힣]{2,}(?:특별자치도|특별자치시|특별시|광역시|도|시|군|구)(?:의)?$", prefix)
            foreign_owner = municipality and not re.search(r"(?:서울특별시|서울시)(?:의)?$", prefix)
            if rule or foreign_owner:
                markers.append((match.start(), match.end() + (len(rule[0]) if rule else 0), set()))
                continue
            markers.append((match.start(), match.end(), owners))
    # As in the existing source linker, a named external law/rule owns its
    # following annex reference. It need not contain a local ordinance alias.
    legal_words = re.finditer(r"(?<![가-힣A-Za-z0-9])([가-힣A-Za-z0-9ㆍ·]*(?:시행규칙|시행령|법률|조례|규칙|법))"
                              r"(?=$|\s|[은는이가의를을에]|제\s*\d+\s*조|별표)", question)
    for match in legal_words:
        start, end = len(_question_text(question[:match.start()])), len(_question_text(question[:match.end()]))
        if any(marker[1] == end and marker[2] for marker in markers) or match[1] == "조례":
            continue
        markers.append((start, end, set()))
    markers.sort(key=lambda item: (item[1], -item[0]))
    references = list(re.finditer(r"별표(?:(\d+)(?:의(\d+)(?!\d|세|원|명|%))?)?|제(\d+)조(?:의(\d+))?(?:제(\d+)항)?", query))
    # A bare demonstrative annex ("이 별표") after a numbered annex refers back to it, not a new reference.
    references = [match for position, match in enumerate(references)
                  if not (match.group().startswith("별표") and match[1] is None
                          and re.search(r"(?:이|그|해당|같은|위|동|당해)$", query[:match.start()])
                          and any(prior.group().startswith("별표") and prior[1] for prior in references[:position]))]
    pending, external, requests = [], [], []
    for index, match in enumerate(references):
        attachment = match.group().startswith("별표")
        branch, reference_end = match[2], match.end()
        if attachment and branch:
            branch_label = "별표 " + str(int(match[1])) + "의" + str(int(branch))
            if (not any(label == branch_label for _, label in groups) and
                    any(query.startswith(term, match.start(2)) for cid in eligible for term in _row_terms(by_id[cid]))):
                branch, reference_end = None, match.end(1)
        label = ("별표" + (" " + str(int(match[1])) if match[1] else "") + ("의" + str(int(branch)) if branch else "")
                 if attachment else "제" + str(int(match[3])) + "조" + ("의" + str(int(match[4])) if match[4] else ""))
        previous = [marker for marker in markers if marker[1] <= match.start()]
        owners = previous[-1][2] if previous else None
        request = {"reference_id": "question:" + str(index), "reference": label,
                   "kind": "attachment" if attachment else "body", "origin": "question", "scope": None}
        segment = query[reference_end:references[index + 1].start() if index + 1 < len(references) else len(query)]
        if owners is not None and not owners:
            external.append({**request, "classification": "external_question_reference"})
            continue
        if owners is None or len(owners) != 1:
            pending.append((request, "source_scope_unspecified" if owners is None else "ambiguous_ordinance_version"))
            continue
        scope = next(iter(owners))
        # An unnumbered "별표" names one annex only when its own scope has an
        # unnumbered annex; otherwise it points at no specific annex.
        if attachment and match[1] is None and (scope, label) not in groups:
            continue
        request["scope"] = _scope_value(scope)
        requests.append((request, scope, segment, int(match[5]) if match[5] else None))

    # Reverse only an existing exact body reference. Title-only associations and
    # mandatory-condition bundles do not imply that every row applies.
    named_scopes = {scope for _, _, owners in markers for scope in owners}
    question_labels = {item[0]["reference"] for item in requests + pending if item[0]["kind"] == "attachment"}
    for (scope, label), members in groups.items():
        if (external or (named_scopes and scope not in named_scopes) or (question_labels and label not in question_labels)):
            continue
        row_match = any(_term_present(term, query) for cid in members for term in _row_terms(by_id[cid]))
        section_match = any(_compact(re.sub(r"^\s*\d+\.\s*", "", context.get("text", ""))) in query
                            for cid in members for context in _attachment_bundle(by_id[cid]).get("required_contexts", [])
                            if context.get("kind") == "preceding_table_heading" and context.get("text", "").strip())
        if not (row_match or section_match or _compact(label) in query):
            continue
        checked = set()
        for cid in members:
            related = by_id[cid]["source"]["source_record"].get("related_body", {})
            bindings = list(related.get("body_references", []))
            for required in _attachment_bundle(by_id[cid]).get("required_body_contexts", []):
                bindings.append({"chunk_id": required.get("chunk_id"), "quote_text": required.get("quote_text"),
                                 "method": "required_body_context", "source_record": required})
            for ref in bindings:
                origin = ref.get("chunk_id")
                if origin in checked or origin not in ranked or origin not in eligible:
                    continue
                target = by_id[origin]
                source = target["source"].get("source_record", {})
                quote, span = ref.get("quote_text"), ref.get("reference_span", {})
                start, end = span.get("start"), span.get("end")
                if (target["kind"] != "body" or scopes[origin] != scope or
                        not isinstance(quote, str) or quote != source.get("quote_text")):
                    continue
                method = ref.get("method")
                if method == "required_body_context":
                    required = ref["source_record"]
                    if any(required.get(key) != source.get(key) for key in
                           ("ordin_seq", "as_of_date", "effective_date", "content_hash")):
                        continue
                    pattern = re.escape(label).replace(r"\ ", r"\s*") + r"(?!\s*(?:\d|의\s*\d))"
                    mentions = list(re.finditer(pattern, quote))
                    # A mandatory context is a source association, not itself an
                    # attachment reference. Require an unqualified literal one.
                    if (len(mentions) != 1 or _EXTERNAL_ANNEX_OWNER.search(
                            quote[:mentions[0].start()].rsplit("\n", 1)[-1])):
                        request = {"reference_id": "body:" + origin + ":" + by_id[cid]["source"]["source_unit_id"],
                                   "reference": label, "kind": "attachment", "origin": "required_body_context",
                                   "source_chunk_id": origin, "scope": _scope_value(scope)}
                        if not any(item["reference_id"] == request["reference_id"] for item in state["requested"]):
                            state["requested"].append(request)
                            state["unresolved"].append({**request, "reason": "body_reference_not_explicit",
                                                       "scope_status": "local_corpus_unresolved"})
                        continue
                elif (method != "explicit_body_reference" or type(start) is not int or type(end) is not int or
                      not 0 <= start < end <= len(quote) or quote[start:end] != ref.get("reference_text") or
                      _compact(ref.get("reference_text", "")) != _compact(label)):
                    continue
                checked.add(origin)
                request = {"reference_id": "body:" + origin + ":" + by_id[cid]["source"]["source_unit_id"],
                           "reference": label, "kind": "attachment", "origin": method,
                           "source_chunk_id": origin, "scope": _scope_value(scope)}
                state["requested"] = [item for item in state["requested"] if item["reference_id"] != request["reference_id"]]
                state["unresolved"] = [item for item in state["unresolved"] if item["reference_id"] != request["reference_id"]]
                requests.append((request, scope, query, None))

    added = set()
    for request, scope, segment, paragraph in requests:
        state["requested"].append(request)
        label = request["reference"]
        if request["kind"] == "body":
            selected = {cid for cid in eligible if scopes[cid] == scope and by_id[cid]["kind"] == "body"
                        and _compact(by_id[cid]["source"].get("source_record", {}).get("article", "")) == _compact(label)
                        and (paragraph is None or by_id[cid]["source"]["source_record"].get("paragraph") == paragraph)}
            reason, contexts = "explicit_article", []
        else:
            members = groups.get((scope, label), [])
            section_matches = set()
            for cid in members:
                for context in _attachment_bundle(by_id[cid]).get("required_contexts", []):
                    if context.get("kind") == "preceding_table_heading":
                        heading = _compact(re.sub(r"^\s*\d+\.\s*", "", context.get("text", "")))
                        if heading and heading in segment:
                            section_matches.add(cid)
            if section_matches:
                members = [cid for cid in members if cid in section_matches]
            matched = {}
            for cid in members:
                for term in _row_terms(by_id[cid]):
                    if _term_present(term, segment):
                        matched.setdefault(term, set()).add(cid)
            # A value shared by the entire table does not identify a row.
            selected = set().union(*(carriers for carriers in matched.values()
                                     if len(carriers) < len(members) or len(members) == 1)) if matched else set()
            if len(selected) > MAX_REFERENCE_ROWS:
                state["unresolved"].append({**request, "reason": "row_selection_requires_review",
                    "scope_status": "local_corpus_unresolved", "budget_kind": "reference_row_candidates",
                    "candidate_count": len(selected), "candidate_limit": MAX_REFERENCE_ROWS})
                continue
            reason, contexts = "explicit_row_value", []
            if re.search(r"비고|주석|정의", segment):
                notes = {}
                for cid in members:
                    for context in _attachment_bundle(by_id[cid]).get("required_contexts", []):
                        if context.get("kind") in {"table_note", "legacy_full_attachment_conditions"}:
                            # ID and source_refs retain provenance; equal prose
                            # from a different table is never merged here.
                            identity = json.dumps(context, ensure_ascii=False, sort_keys=True)
                            notes.setdefault(identity, set()).add(cid)
                remaining = list(notes.values())
                selected.update(set(ranked) & set(members))
                while any(not selected.intersection(carriers) for carriers in remaining):
                    candidates = set().union(*(carriers for carriers in remaining if not selected.intersection(carriers)))
                    def source_order(cid):
                        refs = by_id[cid]["source"]["source_record"].get("source_refs", [])
                        return min((r.get("page", 0), r.get("element_id", 0), r.get("row_index", 0), cid)
                                   for r in refs) if refs else (0, 0, 0, cid)
                    selected.add(min(candidates, key=source_order))
                if notes:
                    reason = "required_note_context"
            if not selected:
                selected = set(ranked) & set(members)
                reason = "existing_ranked_candidate"
            if selected:
                for cid in sorted(selected):
                    contexts.extend(row[0] for row in connection.execute(
                        "SELECT context_id FROM chunk_context_links WHERE chunk_id=? ORDER BY context_id", (cid,)))
        if not selected:
            state["unresolved"].append({**request, "reason": "row_selection_requires_review" if
                request["kind"] == "attachment" and groups.get((scope, label)) else "local_reference_missing",
                "scope_status": "local_corpus_unresolved"})
            continue
        if len((added | selected) - set(ranked)) > MAX_ADDED_REFERENCE_CHUNKS:
            state["unresolved"].append({**request, "reason": "row_selection_requires_review",
                "scope_status": "local_corpus_unresolved", "budget_kind": "added_reference_chunks",
                "candidate_count": len((added | selected) - set(ranked)),
                "candidate_limit": MAX_ADDED_REFERENCE_CHUNKS})
            continue
        added.update(selected)
        state["resolved"].append({**request, "primary_ids": sorted(selected), "context_ids": sorted(set(contexts)),
                                  "reason": reason, "candidate_only": True, "applicability_established": False})
    for request, reason in pending:
        state["requested"].append(request)
        # A verified body edge with a question-matched row/context may supply
        # ownership; a lone ranked ordinance never does.
        matches = [item for item in state["resolved"] if item["origin"] in {"explicit_body_reference", "required_body_context"}
                   and item["reference"] == request["reference"] and item["reason"] in
                   {"explicit_row_value", "required_note_context"}]
        owners = {json.dumps(item["scope"], sort_keys=True) for item in matches}
        if reason == "source_scope_unspecified" and len(owners) == 1:
            request["scope"] = matches[0]["scope"]
            state["resolved"].append({**request, "scope": matches[0]["scope"],
                "primary_ids": sorted({cid for item in matches for cid in item["primary_ids"]}),
                "context_ids": sorted({cid for item in matches for cid in item["context_ids"]}),
                "reason": "verified_body_reference", "candidate_only": True, "applicability_established": False})
        else:
            state["unresolved"].append({**request, "reason": reason, "scope_status": "local_corpus_unresolved"})
    if not external:
        _attachment_body_expansion(by_id, scopes, ranked, added, eligible, connection, state, named_scopes)
    return added, state, external


def retrieve(database, query_vector, top_k=8, include_deleted=False, *, question=None):
    require(type(top_k) is int and 1 <= top_k <= 100, "top_k_must_be_1_to_100")
    require(type(include_deleted) is bool, "include_deleted_must_be_boolean")
    require(question is None or isinstance(question, str), "question_must_be_string")
    query_norm = vector_norm(query_vector)
    database = Path(database).resolve(strict=True)
    require(database.is_file(), "database_not_file")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        metadata = {row["key"]: json.loads(row["value_json"])
                    for row in connection.execute("SELECT key,value_json FROM metadata")}
        require(metadata.get("as_of_date", ASOF) == ASOF, "database_asof_mismatch")
        if "dimension" in metadata:
            require(metadata["dimension"] == DIMENSION, "database_dimension_mismatch")
        rows = list(connection.execute("SELECT * FROM chunks WHERE as_of_date=?", (ASOF,)))
        require(rows, "database_has_no_asof_chunks")
        by_id, scores, eligible = {}, {}, []
        for row in rows:
            item = dict(row)
            identity = item["chunk_id"]
            require(identity not in by_id, "duplicate_database_chunk")
            require(item["answer_allowed"] == 0, "database_contains_answer_approval")
            require(item["is_deleted"] in (0, 1), "invalid_deleted_flag")
            source = json.loads(item["source_json"])
            require(isinstance(source, dict) and isinstance(source.get("source_unit_id"), str), "missing_source_unit_identity")
            require(hashlib.sha256(item["text"].encode("utf-8")).hexdigest() == item["text_sha256"], "database_text_hash_mismatch")
            vector = json.loads(item["vector_json"])
            norm = vector_norm(vector)
            score = math.fsum((a / query_norm) * (b / norm) for a, b in zip(query_vector, vector))
            scores[identity] = max(-1.0, min(1.0, score))
            by_id[identity] = {**item, "source": source}
            if include_deleted or not item["is_deleted"]:
                eligible.append(identity)
        ranked = sorted(eligible, key=lambda cid: (-scores[cid], cid))[:top_k]
        references, local_reference, external_question_references = _reference_expansion(
            by_id, scores, ranked, set(eligible), question, connection)
        # Explicit companion attachments are restored even if not in top-k.
        expanded = set(ranked) | references
        while True:
            dependencies = {dep for cid in expanded for dep in by_id[cid]["source"].get("dependency_primary_ids", [])}
            require(dependencies <= set(by_id), "missing_dependency_chunk")
            following = expanded | dependencies
            if following == expanded:
                break
            expanded = following

        reviewed = []
        for row in connection.execute("SELECT condition_id,source_unit_id,condition_json FROM reviewed_conditions"):
            entry = json.loads(row["condition_json"])
            require(isinstance(entry, dict) and isinstance(entry.get("condition"), dict), "invalid_saved_condition")
            require(entry["condition"]["condition_id"] == row["condition_id"], "condition_identity_mismatch")
            reviewed.append((row["condition_id"], row["source_unit_id"], entry))
        tasks = {row["reference_id"]: json.loads(row["task_json"])
                 for row in connection.execute("SELECT reference_id,task_json FROM external_material_tasks")}
        evidence, selected_task_ids = [], set()
        ordered = ranked + sorted(expanded - set(ranked))
        for identity in ordered:
            record = by_id[identity]
            source = record["source"]
            contexts, context_ids = [], set()
            links = connection.execute(
                "SELECT l.context_id,l.relation,c.kind,c.source_json,c.text "
                "FROM chunk_context_links l LEFT JOIN contexts c ON c.context_id=l.context_id "
                "WHERE l.chunk_id=? ORDER BY l.context_id,l.relation", (identity,))
            aliases = {identity, source["source_unit_id"]}
            for row in links:
                require(row["source_json"] is not None and row["text"] is not None, "missing_mandatory_context")
                context_source = json.loads(row["source_json"])
                context_ids.add(row["context_id"])
                aliases.add(row["context_id"])
                if isinstance(context_source, dict) and isinstance(context_source.get("source_unit_id"), str):
                    aliases.add(context_source["source_unit_id"])
                # Attachment contexts retain a catalog context ID plus an
                # explicit origin body chunk ID. Resolve only a real DB record;
                # never infer article ownership by splitting an ID or number.
                origin_id = context_source.get("origin_id") if isinstance(context_source, dict) else None
                if isinstance(origin_id, str) and origin_id in by_id:
                    aliases.add(origin_id)
                    aliases.add(by_id[origin_id]["source"]["source_unit_id"])
                contexts.append({"context_id": row["context_id"], "relation": row["relation"],
                                 "kind": row["kind"], "text": row["text"], "source": context_source})
            require(set(source.get("required_context_ids", [])) <= context_ids, "required_context_links_incomplete")
            candidates = []
            for condition_id, source_unit_id, entry in reviewed:
                condition = entry["condition"]
                targets = set(condition.get("current_target_ids", [])) & aliases
                same_source = source_unit_id in aliases
                if not same_source and not targets:
                    continue
                candidates.append({"condition_id": condition_id, "source_unit_id": source_unit_id,
                    "review": entry, "relation": {"same_source_unit": same_source, "matched_explicit_target_ids": sorted(targets)},
                    "classification": "condition_candidate", "applicability_established": False,
                    "scope_requires_review": True, "answer_allowed": False})
            external_ids = set(source.get("external_reference_ids", []))
            require(external_ids <= set(tasks), "missing_external_material_task")
            selected_task_ids.update(external_ids)
            evidence.append({"chunk_id": identity, "retrieval_role": "ranked" if identity in ranked else
                             "explicit_reference" if identity in references else "explicit_dependency",
                "cosine_similarity": scores[identity], "kind": record["kind"], "ordin_seq": record["ordin_seq"],
                "as_of_date": record["as_of_date"], "effective_date": record["effective_date"],
                "title": record["title"], "text": record["text"], "text_sha256": record["text_sha256"],
                "source": source, "is_deleted": bool(record["is_deleted"]), "required_contexts": contexts,
                "condition_candidates": candidates, "external_reference_ids": sorted(external_ids),
                "condition_gate_evaluated": False, "answer_allowed": False})
        return {"schema_version": "local-rag-retrieval-v1", "as_of_date": ASOF,
            "database": str(database), "dimension": DIMENSION, "top_k": top_k,
            "total_vector_count": len(rows), "eligible_vector_count": len(eligible),
            "excluded_deleted_count": 0 if include_deleted else sum(row["is_deleted"] for row in rows),
            "ranked_chunk_ids": ranked, "expanded_chunk_ids": ordered,
            "retrieved_count": len(ranked), "with_dependencies_count": len(ordered), "evidence": evidence,
            "external_tasks": {key: tasks[key] for key in sorted(selected_task_ids)},
            "stored_external_task_count": len(tasks), "condition_gate_evaluated": False,
            "local_reference": local_reference, "external_question_references": external_question_references,
            "reference_candidate_budget": {"rows_per_reference": MAX_REFERENCE_ROWS,
                "added_chunks": MAX_ADDED_REFERENCE_CHUNKS, "meaning": "retrieval_input_budget_only"},
            "answer_allowed": False, "answer_generated": False, "new_api_calls": 0,
            "limitation": "유사도와 같은 출처·명시적 대상 ID 연결은 법적 적용을 확정하지 않습니다. 조건 후보의 관련성·예외는 추가 판단이 필요합니다."}
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--query-vector-file", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--include-deleted", action="store_true")
    parser.add_argument("--question", help="원문 식별자와 검증된 본문 참조에 의한 후보 회수")
    args = parser.parse_args(argv)
    try:
        result = retrieve(args.db, load_vector_file(args.query_vector_file), args.top_k, args.include_deleted,
                          question=args.question)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print("로컬 검색 중단: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
