"""关键词搜索（spec 2.4）：关键词归一化、排序、每篇上限、通配符转义、摘要、隔离。使用隔离数据库。"""

import unittest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from backend.app.config.clock import fixed_now
from backend.app.db.isolated import create_user, isolated_engine
from backend.app.schemas.document import DocumentCreate
from backend.app.services import document_service
from backend.app.services.document_search import normalize_queries, search_chunks


class NormalizeQueriesTests(unittest.TestCase):
    def test_strip_drop_empty_and_dedupe_ignoring_case(self) -> None:
        self.assertEqual(normalize_queries([" Redis ", "", "redis", "缓存", "  "]), ["Redis", "缓存"])

    def test_invalid_queries(self) -> None:
        for raw in ([], ["", "  "], ["a" * 31], [str(i) for i in range(6)]):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_queries(raw)

    def test_limits_are_inclusive(self) -> None:
        self.assertEqual(len(normalize_queries(["a" * 30])), 1)
        self.assertEqual(len(normalize_queries([str(i) for i in range(5)])), 5)


class SearchTests(unittest.TestCase):
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

    def add(self, title, content, *, category=None, user=None, day=1):
        with fixed_now(datetime(2026, 11, day, tzinfo=timezone.utc)), Session(self.engine) as session:
            return document_service.create_document(
                session, DocumentCreate(title=title, category=category, content=content), user_id=user or self.user
            ).id

    def search(self, *queries, **kwargs):
        with Session(self.engine) as session:
            return search_chunks(session, list(queries), user_id=self.user, **kwargs)

    def test_more_matched_queries_rank_first(self) -> None:
        self.add("Redis", "# 击穿\n热点 key 过期，用互斥锁。\n# 雪崩\n大量 key 同时过期。")
        hits = self.search("过期", "互斥锁")
        self.assertEqual([(h.heading_path, h.matched_queries) for h in hits],
                         [(["击穿"], ["过期", "互斥锁"]), (["雪崩"], ["过期"])])

    def test_heading_only_chunk_ranks_after_body_chunks(self) -> None:
        self.add("Redis", "# 缓存问题\n## 缓存击穿\n正文")
        hits = self.search("缓存")
        self.assertEqual([(h.heading_path, h.is_heading_only) for h in hits],
                         [(["缓存问题", "缓存击穿"], False), (["缓存问题"], True)])

    def test_title_hit_ranks_higher(self) -> None:
        self.add("杂记", "提到分布式锁", day=2)
        self.add("分布式锁", "提到分布式锁", day=1)
        self.assertEqual([h.title for h in self.search("分布式锁")], ["分布式锁", "杂记"])

    def test_more_occurrences_rank_higher_then_newer_document(self) -> None:
        self.add("一", "锁", day=3)
        self.add("二", "锁 锁 锁", day=1)
        self.add("三", "锁", day=2)
        self.assertEqual([h.title for h in self.search("锁")], ["二", "一", "三"])

    def test_at_most_three_chunks_per_document(self) -> None:
        self.add("长文", "\n".join(f"# 第{i}节\n这一节提到分布式锁。" for i in range(6)), day=2)
        self.add("短文", "分布式锁的看门狗。", day=1)
        hits = self.search("分布式锁", limit=10)
        self.assertEqual([h.title for h in hits].count("长文"), 3)
        self.assertIn("短文", [h.title for h in hits])
        self.assertEqual(len(self.search("分布式锁", limit=2)), 2)

    def test_like_wildcards_are_literal(self) -> None:
        self.add("百分号", "可靠性 100% 吗")
        self.add("下划线", "变量 max_retry")
        self.add("反斜杠", "路径 C:\\temp")
        # 如果没有转义，"100%" 会匹配 "1000"，"x_r" 会匹配 "xxr"，"C:\t" 会变成 "C:t"。
        self.add("普通", "没有特殊字符 1000 maxxretry C:t")
        self.assertEqual([h.title for h in self.search("100%")], ["百分号"])
        self.assertEqual([h.title for h in self.search("x_r")], ["下划线"])
        self.assertEqual([h.title for h in self.search("C:\\t")], ["反斜杠"])

    def test_case_insensitive_and_short_chinese_words(self) -> None:
        self.add("笔记", "Redis 的过期策略")
        self.assertEqual(len(self.search("REDIS")), 1)
        self.assertEqual(len(self.search("过期")), 1)

    def test_category_filter(self) -> None:
        self.add("学习笔记", "分布式锁", category="学习")
        self.add("复盘", "分布式锁", category="复盘")
        self.assertEqual([h.title for h in self.search("分布式锁", category="学习")], ["学习笔记"])

    def test_snippet_centers_on_first_match(self) -> None:
        content = "甲" * 100 + "看门狗续期" + "乙" * 100 + "![图](u)"
        self.add("长段落", content)
        [hit] = self.search("看门狗")
        self.assertIn("看门狗续期", hit.snippet)
        self.assertTrue(hit.snippet.startswith("…") and hit.snippet.endswith("…"))
        self.assertLessEqual(len(hit.snippet), 130)

    def test_snippet_uses_readable_text(self) -> None:
        self.add("图", "监控 ![QPS 截图](/documents/images/abc) 结束")
        [hit] = self.search("QPS")
        self.assertEqual(hit.snippet, "监控 [图片：QPS 截图] 结束")

    def test_title_only_match_uses_content_start(self) -> None:
        self.add("看门狗机制", "开头的正文")
        [hit] = self.search("看门狗")
        self.assertEqual(hit.snippet, "开头的正文")

    def test_image_url_is_not_searchable(self) -> None:
        self.add("图", "![说明](/documents/images/deadbeef)")
        self.assertEqual(self.search("deadbeef"), [])

    def test_other_users_documents_are_invisible(self) -> None:
        self.add("别人的分布式锁", "分布式锁", user=self.other)
        self.assertEqual(self.search("分布式锁"), [])


if __name__ == "__main__":
    unittest.main()
