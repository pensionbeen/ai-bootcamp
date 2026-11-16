"""첨부 배치 참조 연결 검사. 저장 원문/합성 문장만 사용하며 네트워크 호출 없음."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_ordinance_corpus as body
import download_attachment as originals
import link_attachment_article as linker
from project_paths import PROJECT_ROOT


def item(key, number, title="자료(제1조제1항 관련)", kind="별표", branch="00"):
    return {"ordin_seq": "123", "attachment": {
        "attachment_id": f"123:{key}", "attachment_key": str(key),
        "attachment_type": kind, "attachment_no": number,
        "attachment_branch_no": branch, "title": title,
    }}


def chunk(text, article="제1조", paragraph=1, suffix="000100:1", deleted=False):
    return {"chunk_id": f"123:{suffix}", "article": article,
            "paragraph": paragraph, "quote_text": text, "is_deleted": deleted}


def connect(items, chunks):
    documents = {"123": {"articles": [{"article": c["article"]} for c in chunks]}}
    return linker._batch_reference_links(items, documents, {"123": chunks})


class ExplicitReferenceRules(unittest.TestCase):
    def test_grouped_tables_with_earlier_law_reference_are_both_local(self):
        items = [item(1, "0001"), item(2, "0002")]
        text = "  ① 영 제46조에 해당하는 공원의 요금은 각각 별표 1 및 별표 2의 범위에서 정한다.\n"
        result = connect(items, [chunk(text)])
        for key, expected in [("123:1", "별표 1"), ("123:2", "별표 2")]:
            refs = result[key]["body_references"]
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]["quote_text"], text)
            span = refs[0]["reference_span"]
            self.assertEqual(text[span["start"]:span["end"]], expected)
            self.assertEqual(result[key]["title_reference_status"], "matched_body_reference")

    def test_explicit_external_owners_are_not_linked_to_local_table(self):
        for text in (
            "① 「자동차관리법 시행규칙」 별표 1에 따른다.",
            "① 「다른 법률 시행규칙」 제2조제1항 별표 1의 기준에 따른다.",
            "① 영 제23조 별표 1의 기준에 따른다.",
            "① 기준은 영 제51조제1항 별표 1과 같다.",
            "① 법 시행규칙 별표 1에 따른다.",
            "① 같은 법 별표 1에 따른다.",
        ):
            with self.subTest(text=text):
                result = connect([item(1, "0001")], [chunk(text)])
                self.assertEqual(result["123:1"]["body_references"], [])
                self.assertEqual(result["123:1"]["title_reference_status"], "title_body_reference_not_confirmed")

    def test_quoted_local_table_and_spacing_remain_explicit_references(self):
        text = "① 요금은 「별표1」과 같다."
        result = connect([item(1, "0001")], [chunk(text)])
        self.assertEqual(result["123:1"]["body_references"][0]["reference_text"], "별표1")

    def test_drawings_and_form_suffix_do_not_collide_with_table_numbers(self):
        rows = [item(1, "0004"), item(2, "0004", kind="별도")]
        result = connect(rows, [chunk("① 표시는 별도 제4호서식에 따른다.")])
        self.assertEqual(result["123:1"]["body_references"], [])
        self.assertEqual(result["123:2"]["body_references"][0]["reference_text"], "별도 제4호서식")

    def test_two_drawings_in_one_paragraph_keep_both_distinct_targets(self):
        rows = [item(5, "0005", kind="별도"), item(6, "0006", kind="별도")]
        result = connect(rows, [chunk("① 바닥에는 별도 5의 표시를 하고 입구에는 별도 6의 안내표지판을 설치한다.")])
        self.assertEqual([result[f"123:{n}"]["body_references"][0]["reference_text"] for n in [5, 6]], ["별도 5", "별도 6"])

    def test_ordinary_separately_word_is_not_a_drawing_reference(self):
        result = connect([item(1, "0001", kind="별도")], [chunk("① 별도의 점검과 별도 이용시간은 별도로 정한다.")])
        self.assertEqual(result["123:1"]["body_references"], [])

    def test_unnumbered_table_requires_unique_table_and_matching_title(self):
        bare = item(1, "0000")
        result = connect([bare], [chunk("① 요금은 별표의 범위에서 정한다.")])
        self.assertEqual(result["123:1"]["body_references"][0]["reference_text"], "별표")
        for rows, text in (
            ([bare, item(2, "0002")], "① 요금은 별표의 범위에서 정한다."),
            ([item(1, "0000", "자료(제2조제1항 관련)")], "① 요금은 별표의 범위에서 정한다."),
            ([bare], "① 요금은 별표 0의 범위에서 정한다."),
            ([bare], "① 요금은 법 시행규칙 별표의 범위에서 정한다."),
        ):
            with self.subTest(rows=rows, text=text):
                self.assertEqual(connect(rows, [chunk(text)])["123:1"]["body_references"], [])

    def test_malformed_title_is_not_repaired_but_body_links_are_preserved(self):
        rows = [item(3, "0003", "자료(제21제2항 관련)")]
        chunks = [chunk(f"{marker} 기준은 별표 3에 따른다.", "제21조", n, f"002100:{n}")
                  for n, marker in [(1, "①"), (2, "②"), (3, "③")]]
        result = connect(rows, chunks)["123:3"]
        self.assertEqual(len(result["body_references"]), 3)
        self.assertEqual(result["title_reference_status"], "unparsed_title_reference")
        self.assertEqual(result["title_references"], [])

    def test_article_without_paragraph_does_not_invent_paragraph_one(self):
        result = connect([item(3, "0003", "점용료(제17조 관련)")],
                         [chunk(" 점용자는 별표 3의 요금을 납부한다.\n", "제17조", None, "001700:all")])["123:3"]
        self.assertIsNone(result["body_references"][0]["paragraph"])
        self.assertEqual(result["title_references"][0]["target_chunk_ids"], ["123:001700:all"])

    def test_branch_numbers_are_exact_and_ranges_are_not_partially_resolved(self):
        rows = [item(1, "0001"), item(2, "0001", branch="02")]
        result = connect(rows, [chunk("① 기준은 별표 1의2에 따른다.")])
        self.assertEqual(result["123:1"]["body_references"], [])
        self.assertEqual(result["123:2"]["body_references"][0]["reference_text"], "별표 1의2")
        for text in ["① 별표 10을 따른다.", "① 별표 1부터 별표 3까지 적용한다."]:
            self.assertEqual(connect(rows, [chunk(text)])["123:1"]["body_references"], [])

    def test_deleted_provision_is_not_used_as_active_reference(self):
        self.assertEqual(connect([item(1, "0001")], [chunk("① 별표 1을 적용한다.", deleted=True)])["123:1"]["body_references"], [])


@unittest.skipUnless((PROJECT_ROOT / "data/raw/ordinances.manifest.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/raw/ordinances.manifest.json")
class SavedSixOrdinances(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(originals.requests, "get", side_effect=AssertionError("network forbidden")), \
                patch.object(linker, "load_corpus", side_effect=AssertionError("legacy demo not used")):
            _, cls.items = originals.catalog(PROJECT_ROOT)
            cls.links, cls.input_hashes = linker.load_batch_links(PROJECT_ROOT, cls.items)

    def test_all_23_references_match_independently_audited_xml_locations(self):
        expected = {
            "2171833:22740723": {"000600:1", "001300:3", "001800:3"},
            "2171833:22740725": {"002000:1", "002100:4"},
            "2171833:22740727": {"002100:1", "002100:2", "002100:3"},
            "2171833:22740729": {"002000:3"},
            "2171833:22740735": {"002502:5"},
            "2171833:22740737": {"002503:2"},
            "2171833:22740739": {"002505:4"},
            "2171833:22740781": {"002505:4"},
            "2171767:22728501": {"000900:1"},
            "2147709:22415977": {"001500:1", "002000:2"},
            "2147709:22415979": {"000800:2", "001500:1", "002000:3"},
            "2147709:22416021": {"001700:all"},
            "2147709:22416025": {"003200:1"},
            "2147709:22416027": {"003300:1", "003300:2"},
        }
        self.assertEqual(set(self.links), set(expected))
        self.assertEqual(sum(len(r["body_references"]) for r in self.links.values()), 23)
        for ident, suffixes in expected.items():
            with self.subTest(attachment=ident):
                seq = ident.split(":")[0]
                self.assertEqual({r["chunk_id"] for r in self.links[ident]["body_references"]},
                                 {f"{seq}:{suffix}" for suffix in suffixes})
                self.assertFalse(self.links[ident]["answer_allowed"])

    def test_title_review_and_all_recorded_spans_match_exact_original_strings(self):
        by_id = {i["attachment"]["attachment_id"]: i["attachment"] for i in self.items}
        matched = 0
        for ident, record in self.links.items():
            matched += record["title_reference_status"] == "matched_body_reference"
            for ref in record["body_references"]:
                span = ref["reference_span"]
                self.assertEqual(ref["quote_text"][span["start"]:span["end"]], ref["reference_text"])
            for ref in record["title_references"]:
                span = ref["reference_span"]
                self.assertEqual(by_id[ident]["title"][span["start"]:span["end"]], ref["reference_text"])
        self.assertEqual(matched, 13)
        self.assertEqual(self.links["2171833:22740727"]["title_reference_status"], "unparsed_title_reference")
        self.assertGreaterEqual(len(self.input_hashes), 30)
        for relative, digest in self.input_hashes.items():
            self.assertEqual(hashlib.sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest(), digest)

    def test_caller_cannot_replace_attachment_title_or_source(self):
        changed = copy.deepcopy(self.items)
        changed[0]["attachment"]["title"] = "임의의 제목(제1조 관련)"
        with self.assertRaisesRegex(ValueError, "XML 목록과 다릅니다"):
            linker.load_batch_links(PROJECT_ROOT, changed)

    def test_changed_saved_body_is_rejected_before_linking(self):
        parent = Path(__file__).resolve().parent
        temporary = tempfile.TemporaryDirectory(dir=parent, prefix="attachment-links-test-")
        root = Path(temporary.name).resolve()
        try:
            expected = b"[]\n"
            name = "123.chunks.json"
            folder = root / "data/processed/ordinances/2026-10-01"
            folder.mkdir(parents=True)
            (folder / name).write_bytes(b"[1]\n")
            report = {"as_of_date": "2026-10-01", "input_files": {}}
            with patch.object(originals, "catalog", return_value=({}, [])), \
                    patch.object(body, "prepare", return_value=({name: expected}, report)), \
                    patch.object(linker, "_batch_reference_links") as link:
                with self.assertRaisesRegex(ValueError, "저장된 본문이 XML 검증 결과와 다릅니다"):
                    linker.load_batch_links(root, [])
                link.assert_not_called()
        finally:
            self.assertTrue(root.parent == parent and root.name.startswith("attachment-links-test-") and not root.is_symlink())
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
