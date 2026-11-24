"""Agent 文档工具（spec 3.2、3.3）：阅读范围、续读、阅读量，以及三个工具经 ToolRegistry 的调用。使用隔离数据库。"""

import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.app.agent.tool_registry import ToolRegistry
from backend.app.db.isolated import create_user, isolated_engine
from backend.app.repositories import document_repository
from backend.app.schemas.document import DocumentCreate
from backend.app.services import document_reader, document_service

LONG_PARAGRAPH = "长文段落" * 225  # 900 字，每段单独成片

FIXTURE = (
    "前言段落。\n\n"
    "# 缓存\n缓存概述，见 [[项目复盘]] 和 [[不存在的笔记]]，图 ![架构图](/documents/images/abc)。\n\n"
    "## 示例\n示例一正文。\n\n"
    "### 细节\n细节正文。\n\n"
    "## 示例\n示例二正文。\n\n"
    "# 长文\n" + "\n\n".join(f"{i}{LONG_PARAGRAPH}" for i in range(10)) + "\n\n"
    "# 结尾\n结尾正文。"
)


class ReaderTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = isolated_engine()
        cls.engine = cls.database.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.database.__exit__(None, None, None)

    def setUp(self) -> None:
        self.user = create_user(self.engine, f"u{uuid4().hex[:8]}")
        self.other = create_user(self.engine, f"o{uuid4().hex[:8]}")
        self.linked = self.add("项目复盘", "复盘正文")
        self.doc = self.add("Redis 笔记", FIXTURE)
        with Session(self.engine) as session:
            self.chunks = document_repository.get_chunks(session, self.doc, user_id=self.user)
            session.expunge_all()

    def add(self, title, content, *, user=None, category=None):
        with Session(self.engine) as session:
            return document_service.create_document(
                session, DocumentCreate(title=title, content=content, category=category), user_id=user or self.user
            ).id

    def ids(self, *path):
        """某个小节（按 heading_path）全部片段的 ID；同名小节取第一个。"""
        section = next(c.section_seq for c in self.chunks if c.heading_path == list(path))
        return [c.id for c in self.chunks if c.section_seq == section]

    def second_example(self):
        sections = sorted({c.section_seq for c in self.chunks if c.heading_path == ["缓存", "示例"]})
        return [c.id for c in self.chunks if c.section_seq == sections[1]]

    def read(self, document_id=None, *, chunk_ids=None, scope="chunk", continue_from=None, before=0, user=None):
        with Session(self.engine) as session:
            return document_reader.read(
                session, document_id or self.doc, user_id=user or self.user, chunk_ids=chunk_ids,
                scope=scope, continue_from=continue_from, chars_read_before=before,
            )

    @staticmethod
    def returned(result):
        return [c["chunk_id"] for c in result["chunks"]]


class ReadScopeTests(ReaderTestCase):
    def test_fixture_shape(self) -> None:
        self.assertGreater(len(self.ids("长文")), 6)
        self.assertEqual(len(self.ids("缓存", "示例")), 1)
        self.assertEqual(len(self.second_example()), 1)

    def test_chunk(self) -> None:
        [first_example] = self.ids("缓存", "示例")
        result = self.read(chunk_ids=[first_example])
        self.assertEqual(self.returned(result), [first_example])
        self.assertEqual(result["scope"], "chunk")
        [chunk] = result["chunks"]
        self.assertEqual(chunk["heading_path"], ["缓存", "示例"])
        self.assertIn("示例一正文", chunk["content"])
        # 同名兄弟小节靠 section_seq 区分（来源卡片按它合并）。
        [second] = self.read(chunk_ids=self.second_example())["chunks"]
        self.assertEqual(second["heading_path"], chunk["heading_path"])
        self.assertNotEqual(second["section_seq"], chunk["section_seq"])

    def test_neighbors_cross_sections(self) -> None:
        [detail] = self.ids("缓存", "示例", "细节")
        result = self.read(chunk_ids=[detail], scope="neighbors")
        self.assertEqual(self.returned(result), self.ids("缓存", "示例") + [detail] + self.second_example())

    def test_section_excludes_children(self) -> None:
        [example] = self.ids("缓存", "示例")
        self.assertEqual(self.returned(self.read(chunk_ids=[example], scope="section")), [example])

    def test_subtree_stops_at_same_name_sibling(self) -> None:
        [example] = self.ids("缓存", "示例")
        result = self.read(chunk_ids=[example], scope="subtree")
        self.assertEqual(self.returned(result), [example] + self.ids("缓存", "示例", "细节"))

    def test_subtree_of_top_level_section(self) -> None:
        [cache] = self.ids("缓存")
        result = self.read(chunk_ids=[cache], scope="subtree")
        self.assertEqual(
            self.returned(result),
            [cache] + self.ids("缓存", "示例") + self.ids("缓存", "示例", "细节") + self.second_example(),
        )

    def test_parent_reads_parent_subtree(self) -> None:
        [detail] = self.ids("缓存", "示例", "细节")
        result = self.read(chunk_ids=[detail], scope="parent")
        self.assertEqual(self.returned(result), self.ids("缓存", "示例") + [detail])

    def test_parent_of_top_level_returns_outline(self) -> None:
        [cache] = self.ids("缓存")
        result = self.read(chunk_ids=[cache], scope="parent")
        self.assertEqual(result["note"], "已是顶层小节，返回目录")
        self.assertNotIn("chunks", result)
        self.assertEqual([s["heading_path"] for s in result["sections"]][:2], [[], ["缓存"]])
        self.assertEqual(result["chars_returned"], 0)

    def test_outline(self) -> None:
        result = self.read(scope="outline", before=100)
        self.assertEqual(result["scope"], "outline")
        self.assertNotIn("chunks", result)
        self.assertNotIn("links", result)
        sections = result["sections"]
        self.assertEqual(
            [s["heading_path"] for s in sections],
            [[], ["缓存"], ["缓存", "示例"], ["缓存", "示例", "细节"], ["缓存", "示例"], ["长文"], ["结尾"]],
        )
        self.assertEqual(sections[5]["first_chunk_id"], self.ids("长文")[0])
        self.assertEqual(sections[5]["chunk_count"], len(self.ids("长文")))
        self.assertEqual((result["chars_returned"], result["chars_left_this_turn"]), (0, 24_000 - 100))

    def test_multiple_starts_are_merged_in_order(self) -> None:
        [example] = self.ids("缓存", "示例")
        [ending] = self.ids("结尾")
        [detail] = self.ids("缓存", "示例", "细节")
        result = self.read(chunk_ids=[ending, example, detail], scope="subtree")
        self.assertEqual(self.returned(result), [example, detail, ending])

    def test_without_chunk_ids_reads_from_start(self) -> None:
        result = self.read(scope="chunk")
        self.assertEqual(self.returned(result), [self.chunks[0].id])
        self.assertIn("前言段落", result["chunks"][0]["content"])

    def test_document_meta(self) -> None:
        result = self.read(scope="outline")
        self.assertEqual((result["document"]["id"], result["document"]["title"]), (self.doc, "Redis 笔记"))


class ReadErrorTests(ReaderTestCase):
    def test_other_users_document_is_not_found(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self.read(scope="outline", user=self.other)
        self.assertEqual(caught.exception.status_code, 404)
        with self.assertRaises(HTTPException):
            self.read(10**9)

    def test_chunk_of_another_document_is_rejected(self) -> None:
        with Session(self.engine) as session:
            [foreign] = document_repository.get_chunks(session, self.linked, user_id=self.user)
        with self.assertRaisesRegex(ValueError, "片段不存在或已失效"):
            self.read(chunk_ids=[foreign.id])

    def test_stale_chunk_id_after_edit_is_rejected(self) -> None:
        old = self.chunks[0].id
        from backend.app.schemas.document import DocumentUpdate

        with Session(self.engine) as session:
            document_service.update_document(session, self.doc, DocumentUpdate(content="新内容"), 1, user_id=self.user)
        with self.assertRaisesRegex(ValueError, "片段不存在或已失效"):
            self.read(chunk_ids=[old])


class ReadBudgetTests(ReaderTestCase):
    def test_single_call_is_truncated_at_6000_chars(self) -> None:
        long_ids = self.ids("长文")
        result = self.read(chunk_ids=[long_ids[0]], scope="section")
        returned = self.returned(result)
        self.assertTrue(result["truncated"])
        self.assertEqual(returned, long_ids[:len(returned)])
        self.assertEqual(result["next_chunk_id"], long_ids[len(returned)])
        lengths = {c.id: len(c.content) for c in self.chunks}
        self.assertEqual(result["chars_returned"], sum(lengths[i] for i in returned))
        self.assertLessEqual(result["chars_returned"], 6_000)
        self.assertGreater(result["chars_returned"] + lengths[result["next_chunk_id"]], 6_000)
        self.assertEqual(result["chars_left_this_turn"], 24_000 - result["chars_returned"])

    def test_truncation_lists_skipped_chunks_explicitly(self) -> None:
        # 测评试跑时模型漏看了 truncated / next_chunk_id，跳过 3 段却说"全读"：
        # 截断时把没返回的片段 ID 明确列出来，并提示续读或在回答里说明。
        long_ids = self.ids("长文")
        result = self.read(chunk_ids=long_ids, scope="chunk")
        returned = self.returned(result)
        skipped = long_ids[len(returned):]
        self.assertEqual(result["skipped_chunk_ids"], skipped)
        self.assertIn(f"continue_from={skipped[0]}", result["note"])
        self.assertIn("没有读", result["note"])
        complete = self.read(chunk_ids=long_ids[:2], scope="chunk")
        self.assertEqual(complete["skipped_chunk_ids"], [])
        self.assertNotIn("note", complete)

    def test_continue_from_does_not_repeat(self) -> None:
        long_ids = self.ids("长文")
        first = self.read(chunk_ids=[long_ids[0]], scope="section")
        second = self.read(chunk_ids=[long_ids[0]], scope="section", continue_from=first["next_chunk_id"])
        self.assertEqual(self.returned(first) + self.returned(second), long_ids)
        self.assertFalse(second["truncated"])
        self.assertIsNone(second["next_chunk_id"])

    def test_continue_from_outside_range_is_rejected(self) -> None:
        [ending] = self.ids("结尾")
        with self.assertRaisesRegex(ValueError, "continue_from"):
            self.read(chunk_ids=[self.ids("长文")[0]], scope="section", continue_from=ending)

    def test_turn_budget_limits_returned_chunks(self) -> None:
        long_ids = self.ids("长文")
        lengths = {c.id: len(c.content) for c in self.chunks}
        before = 24_000 - lengths[long_ids[0]] - lengths[long_ids[1]]
        result = self.read(chunk_ids=[long_ids[0]], scope="section", before=before)
        self.assertEqual(self.returned(result), long_ids[:2])
        self.assertEqual(result["chars_left_this_turn"], 0)

    def test_first_chunk_is_always_returned_within_budget(self) -> None:
        long_ids = self.ids("长文")
        result = self.read(chunk_ids=long_ids[:2], scope="chunk", before=24_000 - 10)
        self.assertEqual(self.returned(result), long_ids[:1])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["chars_left_this_turn"], 0)

    def test_exhausted_budget_reads_nothing(self) -> None:
        result = self.read(chunk_ids=self.ids("缓存"), before=24_000)
        self.assertEqual(result["chunks"], [])
        self.assertEqual(result["chars_returned"], 0)
        self.assertIn("本轮阅读量已用完", result["note"])
        self.assertEqual(result["skipped_chunk_ids"], self.ids("缓存"))

    def test_outline_does_not_need_budget(self) -> None:
        result = self.read(scope="outline", before=30_000)
        self.assertGreater(len(result["sections"]), 0)


class ReadContentTests(ReaderTestCase):
    def test_images_become_text_and_links_are_resolved(self) -> None:
        result = self.read(chunk_ids=self.ids("缓存"))
        content = result["chunks"][0]["content"]
        self.assertIn("[图片：架构图]", content)
        self.assertNotIn("/documents/images/abc", content)
        self.assertIn("[[项目复盘]]", content)
        self.assertEqual(
            [(link["title"], link["document_ids"]) for link in result["links"]],
            [("项目复盘", [self.linked]), ("不存在的笔记", [])],
        )

    def test_empty_document(self) -> None:
        empty = self.add("空白", "")
        result = self.read(empty)
        self.assertEqual((result["chunks"], result["truncated"], result["chars_returned"]), ([], False, 0))


class DocumentToolTests(ReaderTestCase):
    """经 ToolRegistry.execute_tool 调用：参数校验、结果格式、user_id 来自调用方。"""

    def call(self, name, arguments, *, user=None, chars_read=None):
        with Session(self.engine) as session:
            if chars_read is not None:
                session.info["document_chars_read"] = chars_read
            output = ToolRegistry().execute_tool(
                name, json.dumps(arguments, ensure_ascii=False), session, user_id=user or self.user
            )
        return json.loads(output)

    def test_registry_lists_document_tools(self) -> None:
        names = {tool["function"]["name"] for tool in ToolRegistry().get_llm_tools()}
        self.assertTrue({"search_documents", "read_document", "list_documents"} <= names)
        for name in ("search_documents", "read_document", "list_documents"):
            self.assertFalse(ToolRegistry().is_terminal(name))

    def test_user_id_argument_is_rejected(self) -> None:
        for name, arguments in (
            ("search_documents", {"queries": ["缓存"], "user_id": self.other}),
            ("read_document", {"document_id": self.doc, "user_id": self.other}),
            ("list_documents", {"user_id": self.other}),
        ):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.call(name, arguments)

    def test_search_documents(self) -> None:
        self.add("别人的笔记", "示例二正文", user=self.other)
        result = self.call("search_documents", {"queries": ["示例二", " 示例二 "]})
        [hit] = result["hits"]
        self.assertEqual(
            (hit["document_id"], hit["title"], hit["chunk_id"], hit["heading_path"], hit["matched_queries"]),
            (self.doc, "Redis 笔记", self.second_example()[0], ["缓存", "示例"], ["示例二"]),
        )
        self.assertIn("示例二正文", hit["snippet"])
        self.assertIn("read_document", result["note"])

    def test_search_without_hits_suggests_rephrasing(self) -> None:
        result = self.call("search_documents", {"queries": ["没有这个词"]})
        self.assertEqual(result["hits"], [])
        self.assertIn("换同义词", result["note"])

    def test_search_category_and_limit(self) -> None:
        self.add("学习笔记", "示例三", category="学习")
        result = self.call("search_documents", {"queries": ["示例"], "category": "学习", "limit": 1})
        self.assertEqual([h["title"] for h in result["hits"]], ["学习笔记"])

    def test_invalid_queries(self) -> None:
        for arguments in ({"queries": []}, {"queries": ["  "]}, {"queries": ["a" * 31]},
                          {"queries": [str(i) for i in range(6)]}, {"queries": ["x"], "limit": 16}):
            with self.subTest(arguments=arguments), self.assertRaises((ValueError, ValidationError)):
                self.call("search_documents", arguments)

    def test_read_document_uses_chars_read_from_session(self) -> None:
        long_ids = self.ids("长文")
        result = self.call(
            "read_document",
            {"document_id": self.doc, "chunk_ids": [long_ids[0]], "scope": "section"},
            chars_read=24_000,
        )
        self.assertEqual(result["chunks"], [])
        fresh = self.call("read_document", {"document_id": self.doc, "chunk_ids": [long_ids[0]], "scope": "section"})
        self.assertEqual(fresh["chars_left_this_turn"], 24_000 - fresh["chars_returned"])
        self.assertEqual(fresh["document"]["title"], "Redis 笔记")

    def test_read_document_rejects_other_users_document(self) -> None:
        with self.assertRaises(HTTPException):
            self.call("read_document", {"document_id": self.doc, "scope": "outline"}, user=self.other)
        with self.assertRaises(ValidationError):
            self.call("read_document", {"document_id": self.doc, "scope": "all"})

    def test_list_documents_filters(self) -> None:
        self.add("100%_完成", "x", category="学习")
        self.add("1000 完成", "x", category="学习")
        self.add("别人的", "x", user=self.other)
        result = self.call("list_documents", {"title_contains": "%_"})
        self.assertEqual([d["title"] for d in result["documents"]], ["100%_完成"])
        self.assertEqual(result["total_shown"], 1)
        studied = self.call("list_documents", {"category": "学习"})
        self.assertEqual([d["title"] for d in studied["documents"]], ["1000 完成", "100%_完成"])
        everything = self.call("list_documents", {})
        self.assertEqual(
            set(everything["documents"][0]), {"id", "title", "category", "content_chars", "updated_at"}
        )
        self.assertEqual(everything["total_shown"], 4)

    def test_list_documents_shows_at_most_50(self) -> None:
        with patch("backend.app.agent.document_tools.LIST_MAX", 2):
            result = self.call("list_documents", {})
        self.assertEqual(len(result["documents"]), 2)

    def test_list_documents_can_include_outlines(self) -> None:
        # 一次拿到多篇的目录，不用逐篇调用 read_document(outline)（试跑 45 号场景时为此用掉了 5 次调用）。
        plain = self.call("list_documents", {"title_contains": "Redis"})
        self.assertNotIn("sections", plain["documents"][0])
        result = self.call("list_documents", {"title_contains": "Redis", "include_outline": True})
        [document] = result["documents"]
        outline = self.read(scope="outline")["sections"]
        self.assertEqual(document["sections"], outline)
        # 目录不含正文，也不计入阅读量。
        self.assertNotIn("content", json.dumps(document["sections"]))


if __name__ == "__main__":
    unittest.main()
