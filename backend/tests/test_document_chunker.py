"""文档切片（spec 1.8）。纯函数测试，不需要数据库。

大多数用例传入很小的 target / limit（20 / 40），用几十个字就能触发拆分。
每个用例都调用 assert_chunks_valid 检查不变式：原文区间、不丢不重不乱序、长度上限。
"""

import unittest

from backend.app.config.settings import CHUNK_MAX_CHARS, CHUNK_TARGET_CHARS
from backend.app.services.document_chunker import (
    Chunk,
    build_search_text,
    normalize_content,
    split_markdown,
)

TARGET = 20
LIMIT = 40


def split(content: str, *, small: bool = True) -> list[Chunk]:
    content = normalize_content(content)
    if small:
        return split_markdown(content, target=TARGET, limit=LIMIT)
    return split_markdown(content)


def summary(chunks: list[Chunk]) -> list[tuple[int, list[str], str]]:
    return [(c.section_seq, c.heading_path, c.content) for c in chunks]


class ChunkTestCase(unittest.TestCase):
    def assert_chunks_valid(self, content: str, chunks: list[Chunk], limit: int = LIMIT) -> None:
        content = normalize_content(content)
        previous_end = 0
        previous_section = -1
        paths: dict[int, list[str]] = {}
        for index, chunk in enumerate(chunks):
            with self.subTest(seq=index):
                self.assertEqual(chunk.seq, index, "seq 从 0 连续编号")
                self.assertEqual(chunk.content, content[chunk.start_offset:chunk.end_offset], "content 必须是原文区间")
                self.assertLess(chunk.start_offset, chunk.end_offset)
                self.assertLessEqual(previous_end, chunk.start_offset, "区间递增且不重叠")
                self.assertEqual(content[previous_end:chunk.start_offset].strip(), "", "片段之间只能是空白")
                self.assertEqual(chunk.content, chunk.content.strip(), "片段首尾不能是空白")
                self.assertIn(chunk.section_seq, (previous_section, previous_section + 1), "section_seq 连续")
                if chunk.section_seq in paths:
                    self.assertEqual(chunk.heading_path, paths[chunk.section_seq], "同一小节路径相同")
                paths[chunk.section_seq] = chunk.heading_path
                if chunk.is_heading_only:
                    self.assertNotIn("\n", chunk.content, "纯标题片段只有标题行")
                    self.assertTrue(chunk.content.lstrip().startswith("#"))
                else:
                    self.assertLessEqual(len(chunk.content), limit, "除超长标题外不超过 limit")
                previous_end = chunk.end_offset
                previous_section = chunk.section_seq
        if chunks:
            self.assertEqual(chunks[0].section_seq, 0)
        self.assertEqual(content[previous_end:].strip(), "", "最后一个片段之后只能是空白")

    def check(self, content: str, expected: list[tuple[int, list[str], str]]) -> list[Chunk]:
        chunks = split(content)
        self.assert_chunks_valid(content, chunks)
        self.assertEqual(summary(chunks), expected)
        return chunks


class NormalizeTests(ChunkTestCase):
    def test_line_endings_and_trailing_whitespace(self) -> None:
        self.assertEqual(normalize_content("a\r\nb\rc  \n\n"), "a\nb\nc")

    def test_leading_whitespace_is_kept(self) -> None:
        self.assertEqual(normalize_content("  a"), "  a")

    def test_empty_content_has_no_chunks(self) -> None:
        self.assertEqual(split_markdown(""), [])
        self.assertEqual(split_markdown(normalize_content("  \n\t\n")), [])

    def test_crlf_document(self) -> None:
        chunks = split("# A\r\na\r\n")
        self.assertEqual(summary(chunks), [(0, ["A"], "# A\na")])

    def test_default_limits_come_from_settings(self) -> None:
        self.assertEqual((CHUNK_TARGET_CHARS, CHUNK_MAX_CHARS), (800, 1500))
        self.assertEqual(len(split("a" * 1500, small=False)), 1)
        chunks = split("a" * 1600, small=False)
        self.assert_chunks_valid("a" * 1600, chunks, CHUNK_MAX_CHARS)
        self.assertEqual([len(c.content) for c in chunks], [1500, 100])


class SectionTests(ChunkTestCase):
    def test_no_headings(self) -> None:
        chunks = self.check("第一段。\n\n第二段。", [(0, [], "第一段。\n\n第二段。")])
        self.assertFalse(chunks[0].is_heading_only)

    def test_heading_hierarchy(self) -> None:
        self.check("# A\na\n## B\nb\n### C\nc\n## D\nd\n# E\ne", [
            (0, ["A"], "# A\na"),
            (1, ["A", "B"], "## B\nb"),
            (2, ["A", "B", "C"], "### C\nc"),
            (3, ["A", "D"], "## D\nd"),
            (4, ["E"], "# E\ne"),
        ])

    def test_skipped_level_does_not_add_empty_layer(self) -> None:
        chunks = self.check("# A\n### B\nb", [(0, ["A"], "# A"), (1, ["A", "B"], "### B\nb")])
        self.assertEqual([c.is_heading_only for c in chunks], [True, False])

    def test_heading_text_rules(self) -> None:
        cases = {
            "## 标题 ##\n正文": ["标题"],
            "## C# 入门\n正文": ["C# 入门"],
            "## 标题#\n正文": ["标题#"],
            "   # 三格缩进\n正文": ["三格缩进"],
            "## **加粗** `code`\n正文": ["**加粗** `code`"],
            "#\t制表符\n正文": ["制表符"],
        }
        for content, path in cases.items():
            with self.subTest(content=content):
                chunks = split(content)
                self.assert_chunks_valid(content, chunks)
                self.assertEqual(chunks[0].heading_path, path)

    def test_lines_that_are_not_headings(self) -> None:
        content = "正文\n#标题\n#\n# \n####### 七级\n    # 四格缩进"
        self.check(content, [(0, [], content)])

    def test_duplicate_headings_get_different_section_seq(self) -> None:
        self.check("# A\n## 示例\nx\n## 示例\ny", [
            (0, ["A"], "# A"),
            (1, ["A", "示例"], "## 示例\nx"),
            (2, ["A", "示例"], "## 示例\ny"),
        ])

    def test_preamble_is_its_own_section(self) -> None:
        self.check("前言\n# A\na", [(0, [], "前言"), (1, ["A"], "# A\na")])

    def test_short_sections_are_never_merged(self) -> None:
        chunks = self.check("# A\n\n\n## B", [(0, ["A"], "# A"), (1, ["A", "B"], "## B")])
        self.assertTrue(all(c.is_heading_only for c in chunks))

    def test_heading_path_is_a_copy(self) -> None:
        chunks = split("# A\n## B\n# C")
        self.assertEqual([c.heading_path for c in chunks], [["A"], ["A", "B"], ["C"]])


class FrontMatterTests(ChunkTestCase):
    def test_comments_in_front_matter_are_not_headings(self) -> None:
        chunks = self.check("---\ntitle: x\n# 注释\n---\n# A\na", [
            (0, [], "---\ntitle: x\n# 注释\n---"),
            (1, ["A"], "# A\na"),
        ])
        self.assertFalse(chunks[0].is_heading_only)

    def test_front_matter_and_preamble_are_separate_sections(self) -> None:
        self.check("---\nk: v\n---\n前言\n# A", [
            (0, [], "---\nk: v\n---"),
            (1, [], "前言"),
            (2, ["A"], "# A"),
        ])

    def test_dots_close_front_matter(self) -> None:
        self.check("---\n# k: v\n...\n# A", [(0, [], "---\n# k: v\n..."), (1, ["A"], "# A")])

    def test_unclosed_front_matter_is_plain_text(self) -> None:
        self.check("---\n# A\na", [(0, [], "---"), (1, ["A"], "# A\na")])

    def test_front_matter_only_on_first_line(self) -> None:
        self.check("前言\n---\n# 注释\n---", [(0, [], "前言\n---"), (1, ["注释"], "# 注释\n---")])

    def test_fences_are_not_recognized_inside_front_matter(self) -> None:
        self.check("---\nx: ```\n---\n# A", [(0, [], "---\nx: ```\n---"), (1, ["A"], "# A")])


class CodeFenceTests(ChunkTestCase):
    def test_hash_inside_code_is_not_a_heading(self) -> None:
        chunks = self.check("# A\n```\n# 注释\n```\n# B", [
            (0, ["A"], "# A\n```\n# 注释\n```"),
            (1, ["B"], "# B"),
        ])
        self.assertFalse(chunks[0].is_heading_only)

    def test_tilde_fence(self) -> None:
        self.check("~~~\n# x\n~~~\n# B", [(0, [], "~~~\n# x\n~~~"), (1, ["B"], "# B")])

    def test_backtick_fence_is_not_closed_by_tilde(self) -> None:
        self.check("```\n# x\n~~~\n# y\n```\n# B", [(0, [], "```\n# x\n~~~\n# y\n```"), (1, ["B"], "# B")])

    def test_shorter_fence_does_not_close_longer_one(self) -> None:
        self.check("````\n```\n# x\n```\n````\n# B", [(0, [], "````\n```\n# x\n```\n````"), (1, ["B"], "# B")])

    def test_longer_fence_closes_shorter_one(self) -> None:
        self.check("```\n# x\n`````\n# B", [(0, [], "```\n# x\n`````"), (1, ["B"], "# B")])

    def test_fence_with_info_string_does_not_close(self) -> None:
        self.check("```\n```python\n# x\n```\n# B", [(0, [], "```\n```python\n# x\n```"), (1, ["B"], "# B")])

    def test_closing_fence_indented_four_spaces_does_not_close(self) -> None:
        self.check("```\n    ```\n# x\n```\n# B", [(0, [], "```\n    ```\n# x\n```"), (1, ["B"], "# B")])

    def test_closing_fence_may_have_trailing_spaces(self) -> None:
        self.check("```\na\n```   \n# B", [(0, [], "```\na\n```"), (1, ["B"], "# B")])

    def test_opening_fence_may_be_indented_three_spaces(self) -> None:
        self.check("前言\n   ```\n# x\n   ```\n# B", [(0, [], "前言\n   ```\n# x\n   ```"), (1, ["B"], "# B")])

    def test_opening_fence_indented_four_spaces_is_not_a_fence(self) -> None:
        self.check("正文\n    ```\n# x", [(0, [], "正文\n    ```"), (1, ["x"], "# x")])

    def test_backtick_in_info_string_is_not_a_fence(self) -> None:
        self.check("``` a`b\n# x", [(0, [], "``` a`b"), (1, ["x"], "# x")])

    def test_unclosed_fence_runs_to_end(self) -> None:
        content = "# A\n```\n# x\n# y"
        self.check(content, [(0, ["A"], content)])


class PackingTests(ChunkTestCase):
    def test_heading_is_a_prefix_not_a_separate_chunk(self) -> None:
        body1, body2 = "a" * 30, "b" * 30
        self.check(f"# T\n\n{body1}\n\n{body2}", [(0, ["T"], f"# T\n\n{body1}"), (0, ["T"], body2)])

    def test_greedy_packing_keeps_original_blank_lines(self) -> None:
        content = "# T\naaaaaaaa\n\nbbbbbbbb\n\n\ncccccccc\n\ndddddddd\n\neeeeeeee"
        chunks = self.check(content, [
            (0, ["T"], "# T\naaaaaaaa"),
            (0, ["T"], "bbbbbbbb\n\n\ncccccccc"),
            (0, ["T"], "dddddddd\n\neeeeeeee"),
        ])
        self.assertFalse(any(c.is_heading_only for c in chunks))

    def test_only_first_chunk_of_section_has_heading(self) -> None:
        chunks = split("# T\n\n" + "\n\n".join(ch * 15 for ch in "abcdef"))
        self.assertTrue(chunks[0].content.startswith("# T"))
        self.assertTrue(all(not c.content.startswith("#") for c in chunks[1:]))
        self.assertEqual({c.section_seq for c in chunks}, {0})


class OversizedBlockTests(ChunkTestCase):
    def test_first_chunk_budget_includes_heading(self) -> None:
        heading = "# 标题标题"
        first_body = LIMIT - len(heading) - 1
        self.check(f"{heading}\n" + "a" * 60, [
            (0, ["标题标题"], f"{heading}\n" + "a" * first_body),
            (0, ["标题标题"], "a" * (60 - first_body)),
        ])

    def test_long_list_is_cut_at_line_breaks(self) -> None:
        lines = [ch * 15 for ch in "abcd"]
        self.check("# H\n" + "\n".join(lines), [
            (0, ["H"], f"# H\n{lines[0]}\n{lines[1]}"),
            (0, ["H"], f"{lines[2]}\n{lines[3]}"),
        ])

    def test_long_paragraph_is_cut_after_sentence_end(self) -> None:
        sentences = [ch * 14 + "。" for ch in "甲乙丙丁"]
        self.check("".join(sentences), [
            (0, [], sentences[0] + sentences[1]),
            (0, [], sentences[2] + sentences[3]),
        ])

    def test_line_break_is_preferred_over_later_sentence_end(self) -> None:
        content = "a" * 10 + "\n" + "b" * 20 + "。" + "c" * 20
        # 前 40 字里同时有换行（第 10 字）和句末（第 32 字），按"换行 → 句末 → 硬切"的顺序先在换行处切。
        self.check(content, [(0, [], "a" * 10), (0, [], "b" * 20 + "。"), (0, [], "c" * 20)])

    def test_hard_cut_without_line_break_or_sentence_end(self) -> None:
        self.check("a" * 100, [(0, [], "a" * 40), (0, [], "a" * 40), (0, [], "a" * 20)])

    def test_long_code_block_is_cut_at_lines(self) -> None:
        lines = [str(i) * 10 for i in range(6)]
        content = "```\n" + "\n".join(lines) + "\n```"
        self.check(content, [
            (0, [], "```\n" + "\n".join(lines[:3])),
            (0, [], "\n".join(lines[3:]) + "\n```"),
        ])

    def test_code_line_longer_than_limit_is_hard_cut(self) -> None:
        content = "```\n" + "y" * 50 + "\n```"
        chunks = split(content)
        self.assert_chunks_valid(content, chunks)
        self.assertGreaterEqual(len(chunks), 2)

    def test_heading_longer_than_limit_is_its_own_chunk(self) -> None:
        heading = "# " + "长" * 50
        chunks = self.check(f"{heading}\n正文", [(0, ["长" * 50], heading), (0, ["长" * 50], "正文")])
        self.assertEqual([c.is_heading_only for c in chunks], [True, False])


class InlineSpanTests(ChunkTestCase):
    """依赖 document_markdown.find_inline_spans（D30）。"""

    def test_hard_cut_never_splits_a_link(self) -> None:
        link = "[链接文字](http://x.y/z)"
        self.check("a" * 30 + link + "b" * 20, [(0, [], "a" * 30), (0, [], link + "b" * 20)])

    def test_sentence_end_inside_image_alt_is_not_a_cut_point(self) -> None:
        image = "![图一。图二](img)"
        self.check("甲" * 25 + image + "乙" * 20, [
            (0, [], "甲" * 25 + image + "乙" * 2),
            (0, [], "乙" * 18),
        ])

    def test_hard_cut_never_splits_a_wikilink(self) -> None:
        wikilink = "[[很长的笔记标题]]"
        self.check("a" * 35 + wikilink, [(0, [], "a" * 35), (0, [], wikilink)])

    def test_image_only_block_stays_with_previous_block(self) -> None:
        intro, image, rest = "a" * 15 + "如下图：", "![图](u)", "c" * 30
        self.check(f"{intro}\n\n{image}\n\n{rest}", [(0, [], f"{intro}\n\n{image}"), (0, [], rest)])

    def test_image_only_block_still_respects_limit(self) -> None:
        intro, image = "a" * 35, "![图](u)"
        content = f"{intro}\n\n{image}"
        self.check(content, [(0, [], intro), (0, [], image)])


class BuildSearchTextTests(unittest.TestCase):
    def chunk(self, heading_path: list[str], content: str) -> Chunk:
        return Chunk(seq=0, section_seq=0, heading_path=heading_path, start_offset=0,
                     end_offset=len(content), is_heading_only=False, content=content)

    def test_title_path_and_readable_content(self) -> None:
        chunk = self.chunk(["分布式锁", "看门狗"], "默认 ![图](u) 见 [[A]]")
        self.assertEqual(build_search_text("Redis 笔记", chunk), "Redis 笔记\n分布式锁 > 看门狗\n默认 [图片：图] 见 A")

    def test_empty_heading_path(self) -> None:
        self.assertEqual(build_search_text("T", self.chunk([], "正文")), "T\n\n正文")


if __name__ == "__main__":
    unittest.main()
