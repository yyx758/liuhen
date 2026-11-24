"""文档服务（spec 2.3、2.5）：校验、幂等、上限、版本、片段重建、审计、详情、列表。使用隔离数据库。"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config.clock import fixed_now
from backend.app.db.isolated import create_user, isolated_engine
from backend.app.models.document import DocumentChunkORM
from backend.app.models.operation_event import OperationEventORM
from backend.app.schemas.document import DocumentCreate, DocumentUpdate
from backend.app.services import document_service

T0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


class DocumentServiceTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = isolated_engine()
        cls.engine = cls.database.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.database.__exit__(None, None, None)

    def setUp(self) -> None:
        # 每个用例用新用户，数据互不干扰。
        self.user = create_user(self.engine, f"u{uuid4().hex[:8]}")
        self.other = create_user(self.engine, f"o{uuid4().hex[:8]}")

    def create(self, title="笔记", content="", *, category=None, user=None, key=None, at=T0):
        with fixed_now(at), Session(self.engine) as session:
            return document_service.create_document(
                session, DocumentCreate(title=title, category=category, content=content), key,
                user_id=user or self.user,
            )

    def update(self, document_id, version, *, user=None, at=T0, **changes):
        with fixed_now(at), Session(self.engine) as session:
            return document_service.update_document(
                session, document_id, DocumentUpdate(**changes), version, user_id=user or self.user
            )

    def delete(self, document_id, version, *, user=None):
        with Session(self.engine) as session:
            document_service.delete_document(session, document_id, version, user_id=user or self.user)

    def get(self, document_id, *, user=None):
        with Session(self.engine) as session:
            return document_service.get_document(session, document_id, user_id=user or self.user)

    def chunks(self, document_id):
        with Session(self.engine) as session:
            return session.scalars(
                select(DocumentChunkORM).where(DocumentChunkORM.document_id == document_id)
                .order_by(DocumentChunkORM.seq)
            ).all()

    def events(self, document_id):
        with Session(self.engine) as session:
            return session.scalars(
                select(OperationEventORM).where(OperationEventORM.document_id == document_id)
                .order_by(OperationEventORM.id)
            ).all()

    def assert_http(self, status: int, func, *args, **kwargs) -> HTTPException:
        with self.assertRaises(HTTPException) as raised:
            func(*args, **kwargs)
        self.assertEqual(raised.exception.status_code, status, raised.exception.detail)
        return raised.exception


class SchemaTests(unittest.TestCase):
    def test_create_normalizes_fields(self) -> None:
        data = DocumentCreate(title=" 标题 ", category="  ", content="a\r\nb  \n\n")
        self.assertEqual((data.title, data.category, data.content), ("标题", None, "a\nb"))

    def test_create_rejects_invalid_fields(self) -> None:
        for fields in ({"title": "   "}, {"title": "a" * 101}, {"title": "t", "category": "c" * 31},
                       {"title": "t", "content": "x" * 50_001}, {"title": "t", "user_id": 1}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                DocumentCreate(**fields)

    def test_update_distinguishes_missing_from_null(self) -> None:
        self.assertEqual(DocumentUpdate(category=None).model_fields_set, {"category"})
        for fields in ({}, {"title": None}, {"content": None}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                DocumentUpdate(**fields)


class CreateTests(DocumentServiceTestCase):
    def test_create_builds_chunks_and_audit_without_content(self) -> None:
        summary = self.create("Redis", "# 锁\n正文内容", category="学习")
        self.assertEqual((summary.title, summary.category, summary.version), ("Redis", "学习", 1))
        self.assertEqual(summary.content_chars, len("# 锁\n正文内容"))
        self.assertEqual(summary.created_at.utcoffset().total_seconds(), 8 * 3600)
        self.assertEqual([c.heading_path for c in self.chunks(summary.id)], [["锁"]])

        [event] = self.events(summary.id)
        self.assertEqual((event.event_type, event.task_id, event.user_id), ("document_created", None, self.user))
        snapshot = event.details["document"]
        self.assertEqual(snapshot["title"], "Redis")
        self.assertEqual(snapshot["content_chars"], summary.content_chars)
        self.assertEqual(len(snapshot["content_hash"]), 64)
        self.assertNotIn("content", snapshot)

    def test_empty_content_is_allowed(self) -> None:
        summary = self.create("空白笔记")
        self.assertEqual((summary.content_chars, summary.excerpt), (0, ""))
        self.assertEqual(self.chunks(summary.id), [])

    def test_idempotent_replay_returns_same_document(self) -> None:
        key = str(uuid4())
        first = self.create("A", "正文", key=key)
        again = self.create("A", "正文", key=key)
        self.assertEqual(again, first)
        self.assertEqual(len(self.events(first.id)), 1)

    def test_same_key_with_different_payload_is_rejected(self) -> None:
        key = str(uuid4())
        self.create("A", "正文", key=key)
        self.assert_http(409, self.create, "A", "改过的正文", key=key)

    def test_key_of_another_user_is_rejected_without_replay(self) -> None:
        key = str(uuid4())
        self.create("A 的私密笔记", "正文", key=key)
        error = self.assert_http(409, self.create, "A 的私密笔记", "正文", key=key, user=self.other)
        self.assertNotIn("私密", error.detail)

    def test_document_limit(self) -> None:
        with patch.object(document_service, "DOCUMENT_MAX_PER_USER", 2):
            self.create("一")
            self.create("二")
            self.assert_http(409, self.create, "三")
            self.create("别人的不受影响", user=self.other)


class UpdateTests(DocumentServiceTestCase):
    def test_stale_version_and_no_change_are_conflicts(self) -> None:
        summary = self.create("A", "正文")
        self.assert_http(409, self.update, summary.id, 2, title="B")
        self.assert_http(409, self.update, summary.id, 1, title="A", content="正文")

    def test_missing_or_foreign_document_is_404(self) -> None:
        summary = self.create("A")
        self.assert_http(404, self.update, summary.id, 1, user=self.other, title="B")
        self.assert_http(404, self.update, 999_999, 1, title="B")

    def test_category_only_change_keeps_chunks(self) -> None:
        summary = self.create("A", "# 一\n正文", category="旧")
        before_ids = [c.id for c in self.chunks(summary.id)]
        updated = self.update(summary.id, 1, category=None)
        self.assertEqual((updated.category, updated.version), (None, 2))
        self.assertEqual([c.id for c in self.chunks(summary.id)], before_ids)

    def test_title_change_rebuilds_search_text(self) -> None:
        summary = self.create("旧标题", "# 一\n正文")
        self.update(summary.id, 1, title="新标题")
        [chunk] = self.chunks(summary.id)
        self.assertTrue(chunk.search_text.startswith("新标题\n"))

    def test_content_change_rebuilds_chunks_and_records_audit(self) -> None:
        summary = self.create("A", "# 一\n旧正文", category="学习")
        later = datetime(2026, 11, 24, 11, 0, tzinfo=timezone.utc)
        updated = self.update(summary.id, 1, at=later, title="B", content="# 二\n新正文\r\n")
        self.assertEqual((updated.title, updated.content, updated.version), ("B", "# 二\n新正文", 2))
        self.assertEqual(updated.updated_at, later)
        self.assertEqual([c.heading_path for c in self.chunks(summary.id)], [["二"]])

        event = self.events(summary.id)[-1]
        self.assertEqual(event.event_type, "document_updated")
        self.assertEqual(event.details["changed"], ["title", "content"])
        self.assertEqual(event.details["before"]["title"], "A")
        self.assertEqual(event.details["after"]["version"], 2)
        self.assertNotEqual(event.details["before"]["content_hash"], event.details["after"]["content_hash"])


class DeleteTests(DocumentServiceTestCase):
    def test_delete_checks_version_and_keeps_audit(self) -> None:
        summary = self.create("要删的", "# 一\n正文")
        self.assert_http(409, self.delete, summary.id, 2)
        self.assert_http(404, self.delete, summary.id, 1, user=self.other)
        self.delete(summary.id, 1)
        self.assert_http(404, self.get, summary.id)
        self.assertEqual(self.chunks(summary.id), [])
        event = self.events(summary.id)[-1]
        self.assertEqual((event.event_type, event.details["before"]["title"]), ("document_deleted", "要删的"))


class ReadTests(DocumentServiceTestCase):
    def test_detail_has_outline_and_resolved_links(self) -> None:
        older = self.create("Redis 笔记", at=datetime(2026, 11, 20, tzinfo=timezone.utc))
        newer = self.create("Redis 笔记", at=datetime(2026, 11, 22, tzinfo=timezone.utc))
        self.create("Redis 笔记", user=self.other)
        content = "前言\n# 缓存\n见 [[Redis 笔记#击穿|笔记]] 和 [[不存在的笔记]]"
        detail = self.get(self.create("复盘", content).id)
        self.assertEqual(detail.content, content)
        self.assertEqual([(o.section_seq, o.heading_path) for o in detail.outline], [(0, []), (1, ["缓存"])])
        self.assertEqual(
            [(link.title, link.heading, link.text, link.document_ids) for link in detail.links],
            [("Redis 笔记", "击穿", "笔记", [newer.id, older.id]), ("不存在的笔记", None, "不存在的笔记", [])],
        )

    def test_excerpt_is_readable(self) -> None:
        summary = self.create("A", "# 标题\n> 引用\n看图 ![监控截图](u)\n\n" + "字" * 200)
        self.assertTrue(summary.excerpt.startswith("标题 引用 看图 [图片：监控截图] 字"))
        self.assertTrue(summary.excerpt.endswith("…"))
        self.assertEqual(len(summary.excerpt), 121)

    def test_list_pagination_and_category(self) -> None:
        ids = [self.create(f"第{i}篇", category="学习" if i % 2 else None,
                           at=datetime(2026, 11, 1 + i, tzinfo=timezone.utc)).id for i in range(5)]
        self.create("别人的", user=self.other)
        with Session(self.engine) as session:
            first = document_service.list_documents(session, user_id=self.user, limit=2)
            self.assertEqual([d.id for d in first.items], [ids[4], ids[3]])
            self.assertEqual(first.next_before_id, ids[3])
            rest = document_service.list_documents(session, user_id=self.user, before_id=ids[3], limit=10)
            self.assertEqual([d.id for d in rest.items], [ids[2], ids[1], ids[0]])
            self.assertIsNone(rest.next_before_id)
            learning = document_service.list_documents(session, user_id=self.user, category="学习")
            self.assertEqual([d.id for d in learning.items], [ids[3], ids[1]])

    def test_invalid_cursor_is_422(self) -> None:
        foreign = self.create("别人的", user=self.other)
        with Session(self.engine) as session:
            for before_id in (foreign.id, 999_999):
                with self.subTest(before_id=before_id), self.assertRaises(HTTPException) as raised:
                    document_service.list_documents(session, user_id=self.user, before_id=before_id)
                self.assertEqual(raised.exception.status_code, 422)

    def test_categories(self) -> None:
        self.create("a", category="学习")
        self.create("b", category="学习")
        self.create("c")
        with Session(self.engine) as session:
            categories = document_service.list_categories(session, user_id=self.user)
        self.assertEqual([(c.name, c.count) for c in categories], [("学习", 2)])


del DocumentServiceTestCase

if __name__ == "__main__":
    unittest.main()
