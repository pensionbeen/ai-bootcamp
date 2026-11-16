"""Offline contract tests for lossless legal-paragraph splitting.

Uses synthetic strings only. Does not read or modify the actual project.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_ordinance_corpus as corpus
from build_ordinance_corpus import split_article


class ArticleSplitTests(unittest.TestCase):
    def split(self, source, article="제1조", title="목적"):
        result = split_article({
            "article": article,
            "title": title,
            "source_text": source,
        })
        self.assert_lossless(source, result)
        return result

    def assert_lossless(self, source, result):
        """Check boundaries against the original, not a normalized copy."""
        self.assertIsInstance(result["segments"], list)
        self.assertIsInstance(result["chunks"], list)
        self.assertIsInstance(result["article_notes"], list)
        self.assertIs(type(result["temporal_review_required"]), bool)
        end = 0
        for segment in result["segments"]:
            with self.subTest(segment_kind=segment["kind"], start=segment["start"]):
                self.assertIn(segment["kind"], {"heading", "provision", "notes"})
                self.assertIs(type(segment["start"]), int)
                self.assertIs(type(segment["end"]), int)
                self.assertEqual(segment["start"], end)
                self.assertGreater(segment["end"], segment["start"])
                self.assertLessEqual(segment["end"], len(source))
                self.assertEqual(segment["text"], source[segment["start"]:segment["end"]])
                end = segment["end"]
        self.assertEqual(end, len(source))
        self.assertEqual("".join(s["text"] for s in result["segments"]), source)
        provisions = [s for s in result["segments"] if s["kind"] == "provision"]
        self.assertEqual(len(provisions), len(result["chunks"]))
        for provision, chunk in zip(provisions, result["chunks"]):
            span = chunk["source_span"]
            self.assertEqual(span, {"start": provision["start"], "end": provision["end"]})
            self.assertEqual(chunk["quote_text"], source[span["start"]:span["end"]])
            self.assertEqual(chunk["text"], chunk["quote_text"].strip())
            self.assertEqual(chunk["paragraph"], provision["paragraph"])
            self.assertIs(type(chunk["is_deleted"]), bool)

    def test_crlf_spaces_tabs_and_final_whitespace_round_trip_exactly(self):
        source = "제1조(목적)  \t① 첫째 문장.  \r\n\t계속되는 문장.\r\n\r\n  ② 둘째 문장. \t\r\n  [전문개정 2025.7.14.]  \r\n\t"
        result = self.split(source)
        self.assertEqual([c["paragraph"] for c in result["chunks"]], [1, 2])
        self.assertIn("\r\n", result["chunks"][0]["quote_text"])
        self.assertTrue(any(s["kind"] == "notes" for s in result["segments"]))

    def test_first_paragraph_can_follow_heading_on_the_same_line(self):
        result = self.split("제1조(목적) ① 시장은 기준을 정한다.\n② 세부 사항은 따로 정한다.")
        self.assertEqual([c["paragraph"] for c in result["chunks"]], [1, 2])
        self.assertEqual(result["chunks"][0]["text"], "① 시장은 기준을 정한다.")

    def test_unnumbered_article_stays_one_chunk_without_invented_paragraph(self):
        body = "이 조례는 시민의 권익을 보호함을 목적으로 한다.\n세부 사항은 다음과 같다."
        result = self.split("제1조(목적) " + body + "\n")
        self.assertEqual(len(result["chunks"]), 1)
        self.assertIsNone(result["chunks"][0]["paragraph"])
        self.assertEqual(result["chunks"][0]["text"], body)

    def test_whole_article_and_numbered_paragraph_deletions_are_preserved(self):
        whole = self.split("제8조 삭제 <2025.7.14.>\n", article="제8조", title="")
        self.assertIsNone(whole["chunks"][0]["paragraph"])
        self.assertTrue(whole["chunks"][0]["is_deleted"])
        numbered = self.split("제1조(목적) ① 삭제 <2025.7.14.>\n② 시장은 기준을 정한다.\n")
        self.assertEqual([c["paragraph"] for c in numbered["chunks"]], [1, 2])
        self.assertEqual([c["is_deleted"] for c in numbered["chunks"]], [True, False])
        self.assertIn("① 삭제 <2025.7.14.>", numbered["chunks"][0]["quote_text"])

    def test_sentence_containing_word_deleted_is_not_a_deleted_provision(self):
        result = self.split("제1조(목적) ① 삭제된 정보를 복원할 수 있다.\n② 시장은 불필요한 항목을 삭제하여야 한다.")
        self.assertEqual([c["is_deleted"] for c in result["chunks"]], [False, False])

    def test_nonsequential_duplicate_or_nonfirst_one_markers_are_rejected(self):
        for body in (
            "② 둘째 항부터 시작한다.",
            "① 첫째 항.\n③ 셋째 항.",
            "① 첫째 항.\n① 중복 항.",
            "① 첫째 항.\n② 둘째 항.\n① 다시 첫째 항.",
        ):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    split_article({"article": "제1조", "title": "목적", "source_text": "제1조(목적) " + body})

    def test_unassigned_intro_before_numbered_paragraph_is_rejected(self):
        with self.assertRaises(ValueError):
            split_article({
                "article": "제1조", "title": "목적",
                "source_text": "제1조(목적) 번호 없는 별도 본문이 있다.\n① 첫째 항이다.\n② 둘째 항이다.",
            })

    def test_items_and_subitems_keep_their_parent_paragraph_condition(self):
        body = "① 다음 각 호의 조건을 모두 충족하여야 한다.\n  1. 첫째 기준\n    가. 첫째 세부 기준\n    나. 둘째 세부 기준\n  2. 둘째 기준\n② 제1항의 신청은 서면으로 한다."
        result = self.split("제1조(목적) " + body)
        self.assertEqual(len(result["chunks"]), 2)
        first = result["chunks"][0]["text"]
        for required in ("다음 각 호의 조건을 모두 충족", "1. 첫째 기준", "가. 첫째 세부 기준", "나. 둘째 세부 기준", "2. 둘째 기준"):
            self.assertIn(required, first)
        self.assertEqual(result["chunks"][1]["text"], "② 제1항의 신청은 서면으로 한다.")

    def test_circled_numbers_inside_sentences_do_not_create_chunks(self):
        source = "제1조(목적) ① 서식의 ② 표시는 안내용이며 ③ 표시는 확인용이다.\n추가 설명에서 ① 표시를 다시 참조한다.\n② 실제 둘째 항이다."
        result = self.split(source)
        self.assertEqual([c["paragraph"] for c in result["chunks"]], [1, 2])
        self.assertIn("서식의 ② 표시", result["chunks"][0]["text"])
        self.assertIn("설명에서 ① 표시", result["chunks"][0]["text"])

    def test_branched_article_heading_is_exact_and_mismatched_title_is_rejected(self):
        source = "제25조의3(환경친화적자동차) ① 전용 구획을 설치한다.\n② 표시 방법은 별도로 정한다."
        result = self.split(source, article="제25조의3", title="환경친화적자동차")
        self.assertEqual(len(result["chunks"]), 2)
        for article, title in (("제25조", "환경친화적자동차"), ("제25조의3", "다른 제목")):
            with self.subTest(article=article, title=title):
                with self.assertRaises(ValueError):
                    split_article({"article": article, "title": title, "source_text": source})

    def test_unknown_terminal_bracket_note_is_preserved_without_interpretation(self):
        source = "제1조(목적) 본문을 보존한다.\n[알 수 없는 원문 메모]\n"
        result = self.split(source)
        self.assertIn("[알 수 없는 원문 메모]", result["article_notes"])
        self.assertFalse(result["temporal_review_required"])
        self.assertEqual(result["chunks"][0]["text"], "본문을 보존한다.")

    def test_multiple_terminal_notes_keep_order_and_internal_brackets_stay_in_body(self):
        source = "제1조(목적) 본문의 [조건]을 지킨다.\n[본문 안의 설명]\n이 문장은 같은 본문에 속한다.\n[전문개정 2025.7.14.]\n[제목개정 2026.1.1.]\n"
        result = self.split(source)
        self.assertEqual(result["article_notes"], ["[전문개정 2025.7.14.]", "[제목개정 2026.1.1.]"])
        self.assertIn("[본문 안의 설명]\n이 문장은", result["chunks"][0]["text"])
        self.assertFalse(result["temporal_review_required"])

    def test_temporal_note_terms_require_review_without_resolving_their_meaning(self):
        for term in ("유효", "시행", "적용", "경과"):
            with self.subTest(term=term):
                result = self.split(f"제1조(목적) 본문을 보존한다.\n[{term} 관련 원문 주석]\n")
                self.assertTrue(result["temporal_review_required"])
                self.assertFalse(result["chunks"][0]["is_deleted"])

    def test_past_date_in_validity_note_does_not_automatically_expire_or_delete_text(self):
        source = "제1조(목적) ① 시험용 원문을 보존한다.\n[이 조는 2020년 12월 31일까지 유효]\n"
        result = self.split(source)
        self.assertEqual(len(result["chunks"]), 1)
        self.assertEqual(result["chunks"][0]["paragraph"], 1)
        self.assertFalse(result["chunks"][0]["is_deleted"])
        self.assertTrue(result["temporal_review_required"])
        self.assertIn("[이 조는 2020년 12월 31일까지 유효]", result["article_notes"])

    def test_circled_paragraphs_one_through_fifty_are_supported(self):
        markers = [chr(code) for start, stop in ((0x2460, 0x2474), (0x3251, 0x3260), (0x32B1, 0x32C0)) for code in range(start, stop)]
        self.assertEqual(len(markers), 50)
        source = "제1조(목적) " + "\n".join(f"{marker} 제{number}항의 본문." for number, marker in enumerate(markers, start=1))
        result = self.split(source)
        self.assertEqual([c["paragraph"] for c in result["chunks"]], list(range(1, 51)))


class CorpusStorageTests(unittest.TestCase):
    """Exercise publication rules with mocked preparation and local fixtures."""

    def setUp(self):
        self.temp_parent = Path(__file__).resolve().parent
        self.temp_prefix = "body-storage-test-"
        self.temporary = tempfile.TemporaryDirectory(
            prefix=self.temp_prefix, dir=self.temp_parent,
        )
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.addCleanup(self.cleanup_fixture)
        self.project = self.root / "fixture-project"
        self.project.mkdir()
        self.input_relative = "data/raw/fixture-input.xml"
        self.input_path = self.project / self.input_relative
        self.input_path.parent.mkdir(parents=True)
        self.input_raw = b"<fixture>original input bytes</fixture>\r\n"
        self.input_path.write_bytes(self.input_raw)
        self.output = self.root / "fixture-output"
        self.files = {
            "fixture.articles.json": b'{"articles":["fixture"]}\n',
            "fixture.chunks.json": b'[{"chunk_id":"fixture:1"}]\n',
            "corpus.manifest.json": b'{"fixture_completion_manifest":true}\n',
        }
        self.report = {
            "as_of_date": "2026-10-01",
            "input_files": {self.input_relative: corpus.digest(self.input_raw)},
            "ordinances": [],
            "article_count": 1,
            "chunk_count": 1,
        }

    def cleanup_fixture(self):
        # Verify the exact owned temporary root before recursive stdlib cleanup.
        candidate = Path(self.temporary.name).resolve()
        if (
            candidate != self.root
            or not candidate.is_relative_to(self.temp_parent)
            or candidate.parent != self.temp_parent
            or not candidate.name.startswith(self.temp_prefix)
            or Path(self.temporary.name).is_symlink()
        ):
            raise AssertionError("Refusing cleanup outside the owned test fixture")
        self.temporary.cleanup()

    def quiet_run(self, check_only=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return corpus.run(self.project, self.output, check_only=check_only)

    def output_snapshot(self):
        if not self.output.exists():
            return {}
        return {
            str(path.relative_to(self.output)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.output.rglob("*") if path.is_file()
        }

    def test_check_only_validates_input_without_creating_output(self):
        with patch.object(corpus, "prepare", return_value=(self.files, self.report)) as prepare, \
                patch.object(corpus.source_import, "write_new") as writer:
            result = self.quiet_run(check_only=True)
        prepare.assert_called_once_with(self.project.resolve())
        writer.assert_not_called()
        self.assertEqual(result, self.report)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.input_path.read_bytes(), self.input_raw)

    def test_identical_rerun_preserves_all_bytes_and_modification_times(self):
        with patch.object(corpus, "prepare", return_value=(self.files, self.report)):
            self.quiet_run()
            before = self.output_snapshot()
            with patch.object(corpus.source_import, "write_new", wraps=corpus.source_import.write_new) as writer:
                self.quiet_run()
            writer.assert_not_called()
        self.assertEqual(len(before), len(self.files))
        self.assertEqual(self.output_snapshot(), before)

    def test_conflict_in_final_file_prevents_all_earlier_new_writes(self):
        self.output.mkdir()
        completion = self.output / "corpus.manifest.json"
        completion.write_bytes(b'{"previous_and_different":true}\n')
        before = self.output_snapshot()
        with patch.object(corpus, "prepare", return_value=(self.files, self.report)), \
                patch.object(corpus.source_import, "write_new") as writer:
            with self.assertRaisesRegex(ValueError, "기존 파일.*corpus.manifest.json"):
                self.quiet_run()
            writer.assert_not_called()
        self.assertEqual(self.output_snapshot(), before)
        self.assertFalse((self.output / "fixture.articles.json").exists())
        self.assertFalse((self.output / "fixture.chunks.json").exists())

    def test_input_changed_after_preparation_prevents_any_output_write(self):
        def preparation_with_external_change(project):
            self.assertEqual(project, self.project.resolve())
            self.input_path.write_bytes(b"changed after the prepared input hash")
            return self.files, self.report

        with patch.object(corpus, "prepare", side_effect=preparation_with_external_change), \
                patch.object(corpus.source_import, "write_new") as writer:
            with self.assertRaisesRegex(ValueError, "저장 직전 입력이 변경"):
                self.quiet_run()
            writer.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_completion_manifest_is_written_after_every_data_file(self):
        write_new = corpus.source_import.write_new
        published = []

        def recording_write(path, raw):
            if path.name == "corpus.manifest.json":
                for name, expected in self.files.items():
                    if name != "corpus.manifest.json":
                        self.assertEqual((self.output / name).read_bytes(), expected)
            else:
                self.assertFalse((self.output / "corpus.manifest.json").exists())
            write_new(path, raw)
            published.append(path.name)

        with patch.object(corpus, "prepare", return_value=(self.files, self.report)), \
                patch.object(corpus.source_import, "write_new", side_effect=recording_write):
            self.quiet_run()
        self.assertEqual(published, list(self.files))
        self.assertEqual(published[-1], "corpus.manifest.json")
        self.assertEqual(
            {name: raw_and_time[0] for name, raw_and_time in self.output_snapshot().items()},
            self.files,
        )

    def test_failed_data_write_does_not_publish_a_completion_manifest(self):
        write_new = corpus.source_import.write_new
        attempted = []

        def interrupted_write(path, raw):
            attempted.append(path.name)
            if path.name == "fixture.chunks.json":
                raise OSError("synthetic fixture write failure")
            write_new(path, raw)

        with patch.object(corpus, "prepare", return_value=(self.files, self.report)), \
                patch.object(corpus.source_import, "write_new", side_effect=interrupted_write):
            with self.assertRaisesRegex(OSError, "synthetic fixture"):
                self.quiet_run()
        self.assertEqual(attempted, ["fixture.articles.json", "fixture.chunks.json"])
        self.assertEqual((self.output / "fixture.articles.json").read_bytes(), self.files["fixture.articles.json"])
        self.assertFalse((self.output / "corpus.manifest.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
