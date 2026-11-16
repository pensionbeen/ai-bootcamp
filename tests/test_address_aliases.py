import copy
import re
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_rag_answer import (  # noqa: E402
    AnswerError, citation_aliases, compact_generation_input, compact_model_input, expand_model_input,
    restore_verification_citations)


SOURCE = {"ordin_name": "서울특별시 합성 조례", "effective_date": "2026-07-13", "as_of_date": "2026-10-01"}
OTHER = {"ordin_name": "서울특별시 다른 합성 조례", "effective_date": "2025-01-01", "as_of_date": "2026-10-01"}


def unit(uid, article=None, paragraph=None, item=None, owner="owner:1", source=SOURCE, **extra):
    locator = {}
    if article is not None:
        locator["article"] = article
        locator["paragraph"] = paragraph
        if item is not None:
            locator["item"] = item
    return {**copy.deepcopy(source), "unit_id": uid, "parent_chunk_id": owner, "kind": "body",
            "quote": "원문 " + uid, "locator": locator, "context_only": False, **extra}


def model_input(rows):
    return {"question": "q", "as_of_date": "2026-10-01", "cited_evidence": rows,
            "condition_candidates": [], "condition_review": []}


def own_numbers(row):
    """Every number of the unit's own locator, in article/paragraph/item order."""
    locator = row["locator"]
    numbers = re.findall(r"\d+", locator["article"])
    if locator.get("paragraph") is not None:
        numbers.append(str(locator["paragraph"]))
    if locator.get("item") is not None:
        numbers += re.findall(r"\d+", locator["item"])
    return numbers


class AddressAliasTests(unittest.TestCase):
    def test_item_alias_carries_its_own_item_number_not_its_position(self):
        # (a) An intro and earlier items shift positions: the unit for 제17호 is
        # the 4th row, yet its alias must say 17, and no alias says its position.
        rows = [unit("p1::intro", "제5조", 1), unit("p1::item::1", "제5조", 1, "1"),
                unit("p1::item::3", "제5조", 1, "3"), unit("p1::item::17", "제5조", 1, "17")]
        aliases = citation_aliases(rows)
        self.assertEqual(aliases["p1::item::17"], "Ca5.p1.i17")
        self.assertEqual(aliases["p1::item::3"], "Ca5.p1.i3")
        self.assertEqual(aliases["p1::intro"], "Ca5.p1")
        for row in rows:
            self.assertEqual(re.findall(r"\d+", aliases[row["unit_id"]]), own_numbers(row))

    def test_same_item_number_under_different_paragraphs_or_articles_is_unique(self):
        # (b)
        rows = [unit("a", "제5조", 1, "3"), unit("b", "제5조", 2, "3"), unit("c", "제6조", 1, "3"),
                unit("d", "제5조", None, "3")]
        aliases = citation_aliases(rows)
        self.assertEqual([aliases[key] for key in "abcd"], ["Ca5.p1.i3", "Ca5.p2.i3", "Ca6.p1.i3", "Ca5.i3"])
        self.assertEqual(len(set(aliases.values())), 4)

    def test_branch_numbers_of_article_and_item_stay_distinct(self):
        # (c) 제12조 / 제12조의3, 제7호 / 제7호의3 / 제73호 never share an alias.
        rows = [unit("a", "제12조", 2, "7"), unit("b", "제12조의3", 2, "7"), unit("c", "제12조의3", 2, "7의3"),
                unit("d", "제12조의3", 2, "73"), unit("e", "제123조", 2, "7")]
        aliases = citation_aliases(rows)
        self.assertEqual([aliases[key] for key in "abcde"],
                         ["Ca12.p2.i7", "Ca12-3.p2.i7", "Ca12-3.p2.i7-3", "Ca12-3.p2.i73", "Ca123.p2.i7"])

    def test_units_without_address_or_with_shared_address_keep_sequence_aliases(self):
        # (d) Attachment rows, context-only parents and colliding addresses fall
        # back to dense sequence aliases; an address alias never looks like one.
        rows = [unit("table", attachment=True), unit("parent", "제5조", 1, context_only=True),
                unit("intro", "제5조", 1), unit("dup1", "제9조", 1, "2", kind="required_context"),
                unit("dup2", "제9조", 1, "2"), unit("bad", "부칙", None), unit("item", "제5조", 1, "1")]
        aliases = citation_aliases(rows)
        self.assertEqual(aliases, {"table": "C001", "parent": "C002", "intro": "Ca5.p1", "dup1": "C003",
                                   "dup2": "C004", "bad": "C005", "item": "Ca5.p1.i1"})
        sequence = re.compile(r"C\d+")
        for uid, alias in aliases.items():
            is_address = alias.startswith("Ca")
            self.assertNotEqual(bool(sequence.fullmatch(alias)), is_address, uid)
        # Article-only address (제5조 without paragraph or item) is not a C+digits string.
        self.assertEqual(citation_aliases([unit("art", "제5조", None)])["art"], "Ca5")

    def test_two_documents_get_a_document_letter_and_stay_deterministic(self):
        # (e)
        rows = [unit("a", "제5조", 1, "3"), unit("b", "제5조", 1, "3", source=OTHER), unit("t", attachment=True),
                unit("c", "제7조", None, source=OTHER)]
        aliases = citation_aliases(rows)
        self.assertEqual(aliases, {"a": "CA.a5.p1.i3", "b": "CB.a5.p1.i3", "t": "C001", "c": "CB.a7"})
        self.assertEqual(citation_aliases(copy.deepcopy(rows)), aliases)
        # Same ordinance name with another effective date is a separate document.
        dated = [unit("a", "제5조", 1), unit("b", "제5조", 1, source={**SOURCE, "effective_date": "2020-01-01"})]
        self.assertEqual(citation_aliases(dated), {"a": "CA.a5.p1", "b": "CB.a5.p1"})

    def test_compact_roundtrip_and_verification_restore_use_address_aliases(self):
        # (f)
        rows = [unit("owner:1", "제5조", 1, context_only=True), unit("owner:1::intro", "제5조", 1),
                unit("owner:1::item::17", "제5조", 1, "17"), unit("table", attachment=True)]
        data = model_input(rows)
        compact, crosswalk = compact_model_input(data)
        self.assertEqual(crosswalk, {"C001": "owner:1", "Ca5.p1": "owner:1::intro",
                                     "Ca5.p1.i17": "owner:1::item::17", "C002": "table"})
        self.assertEqual([row["unit_id"] for row in compact["cited_evidence"]["units"]],
                         ["C001", "Ca5.p1", "Ca5.p1.i17", "C002"])
        self.assertEqual(expand_model_input(compact, crosswalk), data)
        generated = {"claims": [{"text": "x", "citation_ids": ["Ca5.p1.i17", "Ca5.p1"]}], "calculations": [
            {"expression": "1+1", "result": "2", "citation_ids": ["Ca5.p1.i17"]}]}
        allowed = ["Ca5.p1", "Ca5.p1.i17", "C002"]
        restored = restore_verification_citations(generated, crosswalk, allowed)
        self.assertEqual(restored["claims"][0]["citation_ids"], ["owner:1::item::17", "owner:1::intro"])
        self.assertEqual(restored["calculations"][0]["citation_ids"], ["owner:1::item::17"])
        for wrong in ("C003", "Ca5.p1.i16", "C001"):
            with self.assertRaises(AnswerError) as caught:
                restore_verification_citations({"claims": [{"text": "x", "citation_ids": [wrong]}],
                                                "calculations": []}, crosswalk, allowed)
            self.assertEqual(caught.exception.args[0], "unknown_or_context_citation_alias")

    def test_generation_view_lists_address_aliases_in_original_order(self):
        rows = [unit("owner:1::intro", "제3조", None), unit("owner:1::item::2", "제3조", None, "2")]
        data = {**model_input(rows), "context_sources": [], "external_tasks": {}, "completeness_context": {
            "condition_checks": [], "mandatory_contexts": [], "texts": {}}}
        view, crosswalk = compact_generation_input(data)
        self.assertEqual(view["cited_evidence"]["citation_order"], ["Ca3", "Ca3.i2"])
        self.assertEqual(set(crosswalk), {"Ca3", "Ca3.i2", "O001"})


if __name__ == "__main__":
    unittest.main()
