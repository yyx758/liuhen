"""行内元素识别与互链解析（spec 1.8.8）。纯函数测试，不需要数据库。"""

import unittest

from backend.app.services.document_markdown import (
    DocLink,
    extract_doc_links,
    find_inline_spans,
    images_to_text,
    to_search_text,
)


def spans_of(text: str) -> list[tuple[str, str, str, str | None]]:
    """把识别结果压缩成 (kind, 原文片段, text, target)，便于断言。"""
    return [(s.kind, text[s.start:s.end], s.text, s.target) for s in find_inline_spans(text)]


class FindInlineSpansTests(unittest.TestCase):
    def test_image(self) -> None:
        text = "看 ![说明](a.png) 完"
        spans = find_inline_spans(text)
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual((span.kind, span.start, span.end), ("image", 2, 14))
        self.assertEqual((span.text, span.target), ("说明", "a.png"))

    def test_image_with_title_keeps_only_url_as_target(self) -> None:
        self.assertEqual(spans_of('![a](b "c")'), [("image", '![a](b "c")', "a", "b")])

    def test_image_with_empty_alt(self) -> None:
        self.assertEqual(spans_of("![](u)"), [("image", "![](u)", "", "u")])

    def test_link(self) -> None:
        self.assertEqual(spans_of("[文字](http://x)"), [("link", "[文字](http://x)", "文字", "http://x")])

    def test_wikilink_variants(self) -> None:
        self.assertEqual(spans_of("[[A]]"), [("wikilink", "[[A]]", "A", "A")])
        self.assertEqual(spans_of("[[A|显示]]"), [("wikilink", "[[A|显示]]", "显示", "A")])
        self.assertEqual(spans_of("[[A#B]]"), [("wikilink", "[[A#B]]", "A#B", "A#B")])
        self.assertEqual(spans_of("[[A#B|C]]"), [("wikilink", "[[A#B|C]]", "C", "A#B")])

    def test_obsidian_image_by_extension(self) -> None:
        self.assertEqual(spans_of("![[图.PNG]]"), [("image", "![[图.PNG]]", "图.PNG", "图.PNG")])
        for name in ("a.png", "a.jpg", "a.jpeg", "a.gif", "a.webp"):
            with self.subTest(name=name):
                self.assertEqual(spans_of(f"![[{name}]]")[0][0], "image")

    def test_embedded_note_is_wikilink_including_bang(self) -> None:
        self.assertEqual(spans_of("![[笔记]]"), [("wikilink", "![[笔记]]", "笔记", "笔记")])

    def test_inline_code_hides_other_syntax(self) -> None:
        self.assertEqual(
            spans_of("`[[x]]` 和 `![a](b)`"),
            [("code", "`[[x]]`", "[[x]]", None), ("code", "`![a](b)`", "![a](b)", None)],
        )

    def test_double_backtick_code(self) -> None:
        self.assertEqual(spans_of("``a`b``"), [("code", "``a`b``", "a`b", None)])

    def test_elements_do_not_span_lines(self) -> None:
        self.assertEqual(find_inline_spans("![a](\nb)"), [])
        self.assertEqual(find_inline_spans("[[a\nb]]"), [])
        self.assertEqual(find_inline_spans("`a\nb`"), [])

    def test_unclosed_syntax_is_plain_text(self) -> None:
        self.assertEqual(find_inline_spans("[[abc"), [])
        self.assertEqual(find_inline_spans("`abc"), [])
        self.assertEqual(find_inline_spans("![a](b"), [])

    def test_multiple_spans_in_order(self) -> None:
        self.assertEqual(
            [kind for kind, *_ in spans_of("[[A]] 和 [b](c.md) 还有 ![i](j)")],
            ["wikilink", "link", "image"],
        )


class ExtractDocLinksTests(unittest.TestCase):
    def test_wikilink_with_heading_and_alias(self) -> None:
        self.assertEqual(extract_doc_links("[[A#B|C]]"), [DocLink(text="C", title="A", heading="B")])

    def test_wikilink_strips_whitespace(self) -> None:
        self.assertEqual(extract_doc_links("[[ A # B ]]"), [DocLink(text=" A # B ", title="A", heading="B")])

    def test_empty_heading_becomes_none(self) -> None:
        self.assertEqual(extract_doc_links("[[A#]]"), [DocLink(text="A#", title="A", heading=None)])

    def test_embedded_note_is_a_link(self) -> None:
        self.assertEqual(extract_doc_links("![[笔记]]"), [DocLink(text="笔记", title="笔记", heading=None)])

    def test_relative_md_link_is_url_decoded(self) -> None:
        self.assertEqual(
            extract_doc_links("[文字](../笔记%20一.md#小%E8%8A%82)"),
            [DocLink(text="文字", title="笔记 一", heading="小节")],
        )

    def test_md_extension_is_case_insensitive(self) -> None:
        self.assertEqual(extract_doc_links("[x](笔记.MD)"), [DocLink(text="x", title="笔记", heading=None)])

    def test_non_document_links_are_skipped(self) -> None:
        for text in (
            "[x](https://a.com/b.md)",
            "[x](http://a.com/b.md)",
            "[x](mailto:a@b.md)",
            "[x](/abs/b.md)",
            "[x](b.txt)",
            "[x](b.md.bak)",
            "[[#小节]]",
            "![a](b.md)",
            "![[图.png]]",
            "`[[A]]`",
        ):
            with self.subTest(text=text):
                self.assertEqual(extract_doc_links(text), [])

    def test_links_inside_fenced_code_are_ignored(self) -> None:
        self.assertEqual(
            extract_doc_links("```\n[[A]]\n```\n[[B]]"),
            [DocLink(text="B", title="B", heading=None)],
        )

    def test_duplicates_keep_first_by_title_and_heading(self) -> None:
        self.assertEqual(
            extract_doc_links("[[A]] [[A|别名]] [[A#B]] [x](A.md#B)"),
            [DocLink(text="A", title="A", heading=None), DocLink(text="A#B", title="A", heading="B")],
        )


class ToSearchTextTests(unittest.TestCase):
    def test_images_become_readable_placeholders(self) -> None:
        self.assertEqual(to_search_text("看 ![监控截图](u) 和 ![](v)"), "看 [图片：监控截图] 和 [图片]")

    def test_links_keep_only_readable_text(self) -> None:
        self.assertEqual(
            to_search_text("[文字](http://x) [[A|显示]] [[B]] ![[图.png]]"),
            "文字 显示（A） B [图片：图.png]",
        )

    def test_inline_code_is_kept_verbatim(self) -> None:
        self.assertEqual(to_search_text("用 `[[x]]` 和 `redis.set()`"), "用 `[[x]]` 和 `redis.set()`")

    def test_fenced_code_is_kept_verbatim(self) -> None:
        self.assertEqual(
            to_search_text("```\n![a](b)\n```\n![c](d)"),
            "```\n![a](b)\n```\n[图片：c]",
        )

    def test_plain_text_unchanged(self) -> None:
        text = "普通文字，没有任何行内元素。\n第二行"
        self.assertEqual(to_search_text(text), text)


class ImagesToTextTests(unittest.TestCase):
    """给模型读的正文（spec 3.2）：只替换图片，链接原样保留，模型能看到链接目标。"""

    def test_images_become_placeholders(self) -> None:
        self.assertEqual(
            images_to_text("看 ![监控截图](/documents/images/abc) 和 ![](v) ![[图.png]]"),
            "看 [图片：监控截图] 和 [图片] [图片：图.png]",
        )

    def test_links_are_kept_verbatim(self) -> None:
        text = "[文字](http://x) [[A|显示]] [[B#小节]] [复盘](./复盘.md)"
        self.assertEqual(images_to_text(text), text)

    def test_code_is_kept_verbatim(self) -> None:
        self.assertEqual(
            images_to_text("`![a](b)`\n```\n![c](d)\n```\n![e](f)"),
            "`![a](b)`\n```\n![c](d)\n```\n[图片：e]",
        )


if __name__ == "__main__":
    unittest.main()
