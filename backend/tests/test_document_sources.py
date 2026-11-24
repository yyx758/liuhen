"""来源标记与卡片（spec 3.4）。纯函数测试，不需要数据库。"""

import json
import unittest

from backend.app.agent.document_sources import MARKER, build_sources, chars_read_in_turn, strip_markers


def call(call_id, name, **arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def calls(*items):
    return {"role": "assistant", "content": None, "tool_calls": list(items)}


def result(call_id, payload):
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload, ensure_ascii=False)}


def chunk(chunk_id, content, heading_path=("缓存",), seq=0, section=None):
    """section 为 None 时不带 section_seq，模拟加字段之前保存的工具结果。"""
    item = {"chunk_id": chunk_id, "seq": seq, "heading_path": list(heading_path), "content": content}
    if section is not None:
        item["section_seq"] = section
    return item


def read_result(call_id, chunks, *, document_id=1, title="Redis 笔记", chars=None):
    return result(call_id, {
        "document": {"id": document_id, "title": title, "category": None, "updated_at": "2026-11-20T10:00:00+08:00"},
        "scope": "chunk", "chunks": chunks, "links": [], "truncated": False, "next_chunk_id": None,
        "chars_returned": chars if chars is not None else sum(len(c["content"]) for c in chunks),
        "chars_left_this_turn": 0,
    })


def search_result(call_id, hits):
    return result(call_id, {"hits": hits, "note": "摘要"})


def hit(chunk_id, matched):
    return {"document_id": 1, "title": "Redis 笔记", "category": None, "chunk_id": chunk_id,
            "heading_path": ["缓存"], "snippet": "…", "matched_queries": matched,
            "is_heading_only": False, "updated_at": "2026-11-20T10:00:00+08:00"}


def final(text):
    return {"role": "assistant", "content": text}


USER = {"role": "user", "content": "我笔记里怎么说的"}


class StripMarkersTests(unittest.TestCase):
    def test_variants(self) -> None:
        self.assertEqual(strip_markers("用互斥锁 [[来源:12]]。"), "用互斥锁。")
        self.assertEqual(strip_markers("用互斥锁[[来源：12]]，再续期\t[[来源: 3 ]]"), "用互斥锁，再续期")
        self.assertEqual(strip_markers("没有标记 [[项目复盘]]"), "没有标记 [[项目复盘]]")
        self.assertEqual(strip_markers("[[来源:abc]]"), "[[来源:abc]]")

    def test_loose_variants_seen_from_real_model(self) -> None:
        # 真实模型写过的格式：全角或混用方括号、带"片段"前缀（试用记录，2026-11-24）。
        for marker in ("［[来源:片段 16］］", "【来源：16】", "[[来源:片段ID 16]]", "[[来源: ID:16]]", "［［来源：16］］"):
            with self.subTest(marker=marker):
                self.assertEqual(strip_markers(f"异步刷新缓存{marker}。"), "异步刷新缓存。")
                self.assertEqual(MARKER.findall(marker), ["16"])

    def test_ordinary_brackets_are_kept(self) -> None:
        for text in ("参考文献[1]", "【注意】别删", "[[来源]]", "来源:16"):
            with self.subTest(text=text):
                self.assertEqual(strip_markers(text), text)


class CharsReadTests(unittest.TestCase):
    def test_sums_successful_read_results_only(self) -> None:
        messages = [
            USER,
            calls(call("r1", "read_document", document_id=1), call("s1", "search_documents", queries=["x"])),
            read_result("r1", [chunk(1, "a" * 100)]),
            search_result("s1", []),
            calls(call("r2", "read_document", document_id=1), call("r3", "read_document", document_id=2)),
            read_result("r2", [], chars=250),
            result("r3", {"ok": False, "error": "文档不存在"}),
        ]
        self.assertEqual(chars_read_in_turn(messages), 350)
        self.assertEqual(chars_read_in_turn([USER]), 0)

    def test_outline_counts_zero(self) -> None:
        messages = [USER, calls(call("r1", "read_document", document_id=1, scope="outline")),
                    result("r1", {"document": {}, "scope": "outline", "sections": [], "chars_returned": 0})]
        self.assertEqual(chars_read_in_turn(messages), 0)


class BuildSourcesTests(unittest.TestCase):
    def build(self, *reads, answer, extra=()):
        messages = [USER]
        for index, (chunks, kwargs) in enumerate(reads):
            messages += [calls(call(f"r{index}", "read_document", document_id=1)),
                         read_result(f"r{index}", chunks, **kwargs)]
        messages += list(extra)
        messages.append(final(answer))
        return [s.model_dump() for s in build_sources(messages)]

    def test_card_from_read_chunk(self) -> None:
        sources = self.build(([chunk(7, "热点 key 过期时用互斥锁重建缓存。")], {}), answer="用互斥锁 [[来源:7]]。")
        self.assertEqual(sources, [{
            "document_id": 1, "title": "Redis 笔记", "heading_path": ["缓存"], "quote": "热点 key 过期时用互斥锁重建缓存。",
        }])

    def test_loose_marker_still_produces_card(self) -> None:
        sources = self.build(([chunk(7, "热点 key 过期时用互斥锁重建缓存。")], {}), answer="用互斥锁［[来源:片段 7］］。")
        self.assertEqual([s["document_id"] for s in sources], [1])

    def test_unread_and_search_only_chunks_are_dropped(self) -> None:
        extra = [calls(call("s1", "search_documents", queries=["锁"])), search_result("s1", [hit(9, ["锁"])])]
        with self.assertLogs("backend.app.agent.document_sources", "WARNING") as logs:
            sources = self.build(([chunk(7, "正文正文正文")], {}), answer="甲 [[来源:9]] 乙 [[来源:404]]", extra=extra)
        self.assertEqual(sources, [])
        self.assertEqual(len(logs.records), 2)

    def test_failed_read_and_previous_turn_reads_do_not_count(self) -> None:
        previous_turn = [USER, calls(call("old", "read_document", document_id=1)),
                         read_result("old", [chunk(5, "上一轮读过的内容")]), final("上一轮回答")]
        messages = [
            USER,
            calls(call("r1", "read_document", document_id=1)),
            result("r1", {"ok": False, "error": "片段不存在"}),
            final("见 [[来源:5]]"),
        ]
        self.assertEqual(build_sources(messages), [])
        # 调用方只传本轮消息；即使误传历史，也只以最后一个用户消息之后的部分为准。
        self.assertEqual(build_sources(previous_turn[:-1] + messages), [])

    def test_merged_by_section_in_marker_order_and_capped(self) -> None:
        chunks = [chunk(1, "第一节第一段", ("一",)), chunk(2, "第一节第二段", ("一",))]
        chunks += [chunk(10 + i, f"第{i}节正文", (f"节{i}",)) for i in range(6)]
        answer = "a [[来源:12]] b [[来源:2]] c [[来源:1]] " + " ".join(f"[[来源:{10 + i}]]" for i in range(6))
        sources = self.build((chunks, {}), answer=answer)
        self.assertEqual([s["heading_path"] for s in sources], [["节2"], ["一"], ["节0"], ["节1"], ["节3"]])
        self.assertEqual(sources[1]["quote"], "第一节第二段")

    def test_same_name_sibling_sections_get_separate_cards(self) -> None:
        # 真实模型试用时发现：两个同名的"缓存问题 > 示例"被并成一张卡片，第二个来源丢失。
        chunks = [
            chunk(14, "第一个示例：互斥锁重建缓存。", ("缓存问题", "示例"), section=3),
            chunk(15, "第一个示例的第二段内容。", ("缓存问题", "示例"), section=3),
            chunk(16, "第二个示例：逻辑过期异步刷新。", ("缓存问题", "示例"), section=5),
        ]
        sources = self.build((chunks, {}), answer="甲 [[来源:14]] 乙 [[来源:16]] 丙 [[来源:15]]")
        self.assertEqual([s["quote"] for s in sources], ["第一个示例：互斥锁重建缓存。", "第二个示例：逻辑过期异步刷新。"])

    def test_same_heading_in_different_documents_are_separate(self) -> None:
        sources = self.build(
            ([chunk(1, "甲文档正文")], {"document_id": 1, "title": "甲"}),
            ([chunk(2, "乙文档正文")], {"document_id": 2, "title": "乙"}),
            answer="[[来源:1]][[来源:2]]",
        )
        self.assertEqual([(s["document_id"], s["title"]) for s in sources], [(1, "甲"), (2, "乙")])

    def test_no_final_reply_gives_no_sources(self) -> None:
        self.assertEqual(build_sources([USER, calls(call("r1", "read_document", document_id=1))]), [])
        self.assertEqual(build_sources([]), [])


class QuoteTests(unittest.TestCase):
    def quote(self, content, matched=None):
        messages = [USER]
        if matched is not None:
            messages += [calls(call("s1", "search_documents", queries=matched)), search_result("s1", [hit(7, matched)])]
        messages += [calls(call("r1", "read_document", document_id=1)), read_result("r1", [chunk(7, content)]),
                     final("答 [[来源:7]]")]
        [source] = build_sources(messages)
        return source.quote

    def test_markdown_symbols_split_segments(self) -> None:
        self.assertEqual(self.quote("## 缓存击穿\n> **热点** key 过期"), "key 过期")

    def test_body_preferred_over_heading_even_if_heading_matches(self) -> None:
        self.assertEqual(self.quote("## 缓存击穿\n热点过期时用互斥锁", ["击穿"]), "热点过期时用互斥锁")
        self.assertEqual(self.quote("## 缓存击穿"), "缓存击穿")
        self.assertEqual(self.quote("# 标\n- 列表项内容\n1. 第一步"), "列表项内容")
        self.assertEqual(self.quote("| -------- |\n第一步操作"), "第一步操作")

    def test_matched_keyword_segment_preferred(self) -> None:
        self.assertEqual(self.quote("## 缓存击穿\n普通段落内容\n用**互斥锁方案**重建", ["互斥锁"]), "互斥锁方案")
        self.assertEqual(self.quote("开头段落内容\n看门狗续期机制", ["看门狗"]), "看门狗续期机制")
        self.assertEqual(self.quote("开头段落内容\nREDIS 集群", ["redis"]), "REDIS 集群")

    def test_short_segments_are_skipped(self) -> None:
        self.assertEqual(self.quote("# 锁\n短\n足够长的段落"), "足够长的段落")

    def test_long_segment_is_cut_to_30_chars(self) -> None:
        text = "甲" * 50 + "看门狗" + "乙" * 50
        self.assertEqual(self.quote(text), "甲" * 30)
        centered = self.quote(text, ["看门狗"])
        self.assertEqual(len(centered), 30)
        self.assertIn("看门狗", centered)
        self.assertIn(centered, text)
        self.assertEqual(self.quote("看门狗" + "乙" * 50, ["看门狗"]), "看门狗" + "乙" * 27)

    def test_image_placeholders_and_link_targets_are_not_quoted(self) -> None:
        self.assertEqual(self.quote("[图片：架构图示意]\n见[文档说明](./doc.md)"), "文档说明")
        self.assertEqual(self.quote("[[项目复盘0812|那次复盘]]"), "那次复盘")

    def test_no_plain_text_gives_empty_quote(self) -> None:
        self.assertEqual(self.quote("[图片：架构图]"), "")
        self.assertEqual(self.quote("# 锁\n**短**"), "")


if __name__ == "__main__":
    unittest.main()
