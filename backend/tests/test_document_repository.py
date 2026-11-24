"""文档仓储（spec 1.9）：重点是用户隔离、版本条件更新、片段重建与图片清理。使用隔离数据库。"""

import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.isolated import create_user, isolated_engine
from backend.app.models.document import DocumentChunkORM, DocumentImageORM, DocumentORM
from backend.app.repositories import document_repository as repo
from backend.app.services.document_chunker import normalize_content, split_markdown

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


class DocumentRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = isolated_engine()
        cls.engine = cls.database.__enter__()
        cls.alice = create_user(cls.engine, "alice")
        cls.bob = create_user(cls.engine, "bob")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.database.__exit__(None, None, None)

    def setUp(self) -> None:
        # 每个用例在一个事务里执行，结束时回滚，用例之间互不影响。
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection, join_transaction_mode="create_savepoint")

    def tearDown(self) -> None:
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    def add_document(self, user_id: int, title: str, content: str = "", *, category: str | None = None,
                     updated_at: datetime = NOW) -> DocumentORM:
        content = normalize_content(content)
        document = repo.insert_document(self.session, DocumentORM(
            user_id=user_id, title=title, category=category, content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            created_at=updated_at, updated_at=updated_at,
        ))
        repo.replace_chunks(self.session, document, split_markdown(content))
        return document

    def add_image(self, user_id: int, data: bytes, *, created_at: datetime) -> DocumentImageORM:
        return repo.insert_image(self.session, DocumentImageORM(
            public_id=hashlib.md5(data + str(user_id).encode()).hexdigest(), user_id=user_id,
            content_type="image/png", byte_size=len(data), sha256=hashlib.sha256(data).hexdigest(),
            data=data, original_name="a.png", created_at=created_at,
        ))


class DocumentQueryTests(DocumentRepositoryTests):
    def test_get_document_is_scoped_to_owner(self) -> None:
        document = self.add_document(self.alice, "A")
        self.assertEqual(repo.get_document(self.session, document.id, user_id=self.alice).title, "A")
        self.assertIsNone(repo.get_document(self.session, document.id, user_id=self.bob))

    def test_list_documents_order_cursor_category_and_isolation(self) -> None:
        old = self.add_document(self.alice, "旧", category="学习", updated_at=NOW - timedelta(days=2))
        middle = self.add_document(self.alice, "中", updated_at=NOW - timedelta(days=1))
        new = self.add_document(self.alice, "新", category="学习", updated_at=NOW)
        self.add_document(self.bob, "别人的", updated_at=NOW)

        page = repo.list_documents(self.session, user_id=self.alice, limit=2)
        self.assertEqual([d.id for d in page], [new.id, middle.id])
        next_page = repo.list_documents(
            self.session, user_id=self.alice, before=(page[-1].updated_at, page[-1].id), limit=2
        )
        self.assertEqual([d.id for d in next_page], [old.id])
        self.assertEqual(
            [d.id for d in repo.list_documents(self.session, user_id=self.alice, category="学习")],
            [new.id, old.id],
        )

    def test_cursor_breaks_ties_by_id(self) -> None:
        first = self.add_document(self.alice, "一")
        second = self.add_document(self.alice, "二")
        page = repo.list_documents(self.session, user_id=self.alice, limit=1)
        self.assertEqual([d.id for d in page], [second.id])
        rest = repo.list_documents(self.session, user_id=self.alice, before=(NOW, second.id))
        self.assertEqual([d.id for d in rest], [first.id])

    def test_count_and_categories(self) -> None:
        self.add_document(self.alice, "a", category="学习")
        self.add_document(self.alice, "b", category="学习")
        self.add_document(self.alice, "c", category="复盘")
        self.add_document(self.alice, "d")
        self.add_document(self.bob, "e", category="学习")
        self.assertEqual(repo.count_documents(self.session, user_id=self.alice), 4)
        self.assertEqual(repo.list_categories(self.session, user_id=self.alice), [("学习", 2), ("复盘", 1)])

    def test_find_documents_by_titles(self) -> None:
        older = self.add_document(self.alice, " Redis 笔记 ", updated_at=NOW - timedelta(days=1))
        newer = self.add_document(self.alice, "Redis 笔记", updated_at=NOW)
        self.add_document(self.bob, "Redis 笔记")
        found = repo.find_documents_by_titles(self.session, ["Redis 笔记 ", "不存在", "  "], user_id=self.alice)
        self.assertEqual([d.id for d in found], [newer.id, older.id])
        self.assertEqual(repo.find_documents_by_titles(self.session, [], user_id=self.alice), [])


class DocumentWriteTests(DocumentRepositoryTests):
    def test_update_requires_owner_and_current_version(self) -> None:
        document = self.add_document(self.alice, "A")
        values = {"title": "B", "updated_at": NOW + timedelta(minutes=1)}
        self.assertEqual(repo.update_if_version(self.session, document.id, 1, values, user_id=self.bob), 0)
        self.assertEqual(repo.update_if_version(self.session, document.id, 2, values, user_id=self.alice), 0)
        self.assertEqual(repo.update_if_version(self.session, document.id, 1, values, user_id=self.alice), 1)
        self.session.expire_all()
        updated = repo.get_document(self.session, document.id, user_id=self.alice)
        self.assertEqual((updated.title, updated.version), ("B", 2))

    def test_delete_requires_owner_and_version_and_cascades_chunks(self) -> None:
        document = self.add_document(self.alice, "A", "# 一\n正文\n# 二\n正文")
        self.assertEqual(repo.delete_if_version(self.session, document.id, 1, user_id=self.bob), 0)
        self.assertEqual(repo.delete_if_version(self.session, document.id, 2, user_id=self.alice), 0)
        self.assertEqual(repo.delete_if_version(self.session, document.id, 1, user_id=self.alice), 1)
        remaining = self.session.scalars(
            select(DocumentChunkORM).where(DocumentChunkORM.document_id == document.id)
        ).all()
        self.assertEqual(remaining, [])

    def test_blank_title_is_rejected_by_database(self) -> None:
        with self.assertRaises(IntegrityError):
            self.add_document(self.alice, "   ")


class ChunkStorageTests(DocumentRepositoryTests):
    def test_replace_chunks_rebuilds_and_takes_user_from_document(self) -> None:
        document = self.add_document(self.alice, "Redis 笔记", "# 锁\n看 ![图](u) 和 [[其他]]")
        chunks = repo.get_chunks(self.session, document.id, user_id=self.alice)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].user_id, self.alice)
        self.assertEqual(chunks[0].search_text, "Redis 笔记\n锁\n# 锁\n看 [图片：图] 和 其他")

        repo.replace_chunks(self.session, document, split_markdown("# 一\na\n# 二\nb"))
        chunks = repo.get_chunks(self.session, document.id, user_id=self.alice)
        self.assertEqual([(c.seq, c.heading_path) for c in chunks], [(0, ["一"]), (1, ["二"])])

    def test_get_chunks_range_and_isolation(self) -> None:
        document = self.add_document(self.alice, "A", "# 一\n# 二\n# 三\n# 四")
        self.assertEqual(
            [c.seq for c in repo.get_chunks(self.session, document.id, user_id=self.alice, seq_from=1, seq_to=2)],
            [1, 2],
        )
        self.assertEqual(repo.get_chunks(self.session, document.id, user_id=self.bob), [])

    def test_outline_groups_chunks_by_section(self) -> None:
        long_body = "\n\n".join(ch * 900 for ch in "ab")  # 超过 1500 字，这一节拆成两个片段
        document = self.add_document(self.alice, "A", f"前言\n# 示例\n{long_body}\n# 示例\n短")
        chunks = repo.get_chunks(self.session, document.id, user_id=self.alice)
        outline = repo.get_outline(self.session, document.id, user_id=self.alice)
        self.assertEqual([(s.section_seq, s.heading_path, s.chunk_count) for s in outline],
                         [(0, [], 1), (1, ["示例"], 2), (2, ["示例"], 1)])
        self.assertEqual(outline[1].first_chunk_id, chunks[1].id)
        self.assertEqual(outline[1].chars, len(chunks[1].content) + len(chunks[2].content))
        self.assertEqual(repo.get_outline(self.session, document.id, user_id=self.bob), [])


class ImageStorageTests(DocumentRepositoryTests):
    def test_get_image_is_scoped_and_loads_data(self) -> None:
        image = self.add_image(self.alice, b"png-bytes", created_at=NOW)
        self.session.expunge_all()
        loaded = repo.get_image(self.session, image.public_id, user_id=self.alice)
        self.assertEqual(loaded.data, b"png-bytes")
        self.assertIsNone(repo.get_image(self.session, image.public_id, user_id=self.bob))

    def test_same_content_is_unique_per_user_only(self) -> None:
        self.add_image(self.alice, b"same", created_at=NOW)
        self.add_image(self.bob, b"same", created_at=NOW)
        found = repo.get_image_by_sha256(self.session, hashlib.sha256(b"same").hexdigest(), user_id=self.alice)
        self.assertEqual(found.user_id, self.alice)
        with self.assertRaises(IntegrityError):
            self.add_image(self.alice, b"same", created_at=NOW)

    def test_total_image_bytes(self) -> None:
        self.assertEqual(repo.total_image_bytes(self.session, user_id=self.alice), 0)
        self.add_image(self.alice, b"123", created_at=NOW)
        self.add_image(self.alice, b"4567", created_at=NOW)
        self.add_image(self.bob, b"89", created_at=NOW)
        self.assertEqual(repo.total_image_bytes(self.session, user_id=self.alice), 7)

    def test_cleanup_deletes_only_old_unreferenced_images(self) -> None:
        old = NOW - timedelta(hours=25)
        referenced = self.add_image(self.alice, b"referenced", created_at=old)
        orphan = self.add_image(self.alice, b"orphan", created_at=old)
        fresh = self.add_image(self.alice, b"fresh", created_at=NOW)
        # 只被别人的文档"引用"不算引用：清理只看本人的文档。
        foreign = self.add_image(self.alice, b"foreign", created_at=old)
        self.add_document(self.alice, "A", f"![图](/documents/images/{referenced.public_id})")
        self.add_document(self.bob, "B", f"![图](/documents/images/{foreign.public_id})")

        deleted = repo.delete_unreferenced_images(self.session, created_before=NOW - timedelta(hours=24))
        self.assertEqual(deleted, 2)
        left = set(self.session.scalars(select(DocumentImageORM.public_id)).all())
        self.assertEqual(left, {referenced.public_id, fresh.public_id})
        self.assertNotIn(orphan.public_id, left)


del DocumentRepositoryTests  # 只作基类，不单独运行

if __name__ == "__main__":
    unittest.main()
