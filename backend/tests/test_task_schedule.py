import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.app.agent.tool_registry import ToolRegistry
from backend.app.db.isolated import isolated_engine
from backend.app.models.task import TaskORM
from backend.app.services.task_service import list_tasks


class TaskScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.registry = ToolRegistry()

    def tearDown(self) -> None:
        self.database.__exit__(None, None, None)

    def create(self, title: str, start_at: str, duration_minutes: int) -> dict:
        arguments = json.dumps({
            "title": title,
            "start_at": start_at,
            "duration_minutes": duration_minutes,
        }, ensure_ascii=False)
        with Session(self.engine) as session:
            return json.loads(self.registry.execute_tool("create_task", arguments, session))

    def test_requires_a_time_and_valid_duration(self) -> None:
        with Session(self.engine) as session:
            for data in (
                {"title": "缺时间"},
                {"title": "缺开始时间", "duration_minutes": 30},
                {"title": "仅截止不能有时长", "due_at": "2026-11-18T15:00:00+08:00", "duration_minutes": 30},
                {"title": "零时长", "start_at": "2026-11-18T14:00:00+08:00", "duration_minutes": 0},
            ):
                with self.subTest(data=data), self.assertRaises(ValidationError):
                    self.registry.execute_tool("create_task", json.dumps(data, ensure_ascii=False), session)

    def test_overlap_rejected_but_adjacent_interval_allowed(self) -> None:
        first = self.create("A", "2026-11-18T14:00:00", 60)
        self.assertEqual(first["start_at"], "2026-11-18T14:00:00+08:00")
        self.assertEqual(first["end_at"], "2026-11-18T15:00:00+08:00")
        self.assertEqual(first["duration_minutes"], 60)

        with self.assertRaises(HTTPException) as raised:
            self.create("冲突", "2026-11-18T06:30:00Z", 30)
        self.assertEqual(raised.exception.status_code, 409)

        second = self.create("B", "2026-11-18T15:00:00+08:00", 30)
        self.assertNotEqual(first["id"], second["id"])
        with Session(self.engine) as session:
            self.assertEqual(len(list_tasks(session)), 2)

    def test_missing_duration_stays_empty(self) -> None:
        arguments = json.dumps({
            "title": "默认时长",
            "start_at": "2026-11-18T14:00:00",
        }, ensure_ascii=False)
        with Session(self.engine) as session:
            task = json.loads(self.registry.execute_tool("create_task", arguments, session))
        self.assertIsNone(task["duration_minutes"])
        self.assertIsNone(task["end_at"])

    def test_due_only_and_start_only_do_not_claim_time(self) -> None:
        with Session(self.engine) as session:
            due_only = json.loads(self.registry.execute_tool("create_task", json.dumps({
                "title": "截止任务", "due_at": "2026-11-18T15:00:00+08:00",
            }, ensure_ascii=False), session))
        self.assertIsNone(due_only["start_at"])
        self.assertIsNone(due_only["duration_minutes"])
        self.assertEqual(due_only["due_at"], "2026-11-18T15:00:00+08:00")

        self.create("占用时间", "2026-11-18T14:00:00+08:00", 60)
        with Session(self.engine) as session:
            start_only = json.loads(self.registry.execute_tool("create_task", json.dumps({
                "title": "只有开始", "start_at": "2026-11-18T14:30:00+08:00",
            }, ensure_ascii=False), session))
        self.assertEqual(start_only["start_at"], "2026-11-18T14:30:00+08:00")
        self.assertIsNone(start_only["end_at"])
        self.assertIsNone(start_only["duration_minutes"])

    def test_due_date_and_scheduled_interval_are_independent(self) -> None:
        arguments = json.dumps({
            "title": "两个时间维度",
            "start_at": "2026-11-18T14:00:00+08:00",
            "duration_minutes": 60,
            "due_at": "2026-11-17T12:00:00+08:00",
        }, ensure_ascii=False)
        with Session(self.engine) as session:
            task = json.loads(self.registry.execute_tool("create_task", arguments, session))
        self.assertEqual(task["end_at"], "2026-11-18T15:00:00+08:00")
        self.assertEqual(task["due_at"], "2026-11-17T12:00:00+08:00")

    def test_done_tasks_do_not_block(self) -> None:
        first = self.create("已完成", "2026-11-18T14:00:00+08:00", 60)
        with Session(self.engine) as session, session.begin():
            session.get(TaskORM, first["id"]).status = "done"
        self.create("可复用时间", "2026-11-18T14:30:00+08:00", 30)

    def test_database_rejects_invalid_interval(self) -> None:
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            with Session(self.engine) as session, session.begin():
                session.add(TaskORM(
                    title="无效区间",
                    priority="medium",
                    status="todo",
                    version=1,
                    start_at=datetime(2026, 11, 18, 7, tzinfo=timezone.utc).replace(tzinfo=None),
                    end_at=datetime(2026, 11, 18, 6, tzinfo=timezone.utc).replace(tzinfo=None),
                ))

    def test_concurrent_creation_keeps_one_task(self) -> None:
        with isolated_engine() as engine:
            barrier = Barrier(2)

            def create_once(title: str) -> str:
                arguments = json.dumps({
                    "title": title,
                    "start_at": "2026-11-18T14:00:00+08:00",
                    "duration_minutes": 60,
                }, ensure_ascii=False)
                barrier.wait()
                with Session(engine) as session:
                    try:
                        self.registry.execute_tool("create_task", arguments, session)
                        return "created"
                    except HTTPException as exc:
                        self.assertEqual(exc.status_code, 409)
                        return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(create_once, ["A", "B"]))
            self.assertCountEqual(outcomes, ["created", "conflict"])

if __name__ == "__main__":
    unittest.main()
