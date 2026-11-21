import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.isolated import create_user, isolated_engine
from backend.app.agent.tool_registry import ToolRegistry
from backend.app.models.action import PendingActionORM
from backend.app.models.task import TaskORM
from backend.app.schemas.task import TaskCreate, TaskUpdate
from backend.app.repositories import task_repository
from backend.app.services import action_service, task_service


class ActionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.user_id = create_user(self.engine, "alice")

    def tearDown(self) -> None:
        self.database.__exit__(None, None, None)

    def create_task(self):
        with Session(self.engine) as session:
            return task_service.create_task(
                TaskCreate(title="原任务", start_at="2030-01-02T14:00:00+08:00"), session, user_id=self.user_id
            )

    def propose(self, task_id: int, title: str):
        with Session(self.engine) as session:
            return action_service.propose_update(
                session, task_id, TaskUpdate(title=title), user_id=self.user_id
            )

    def status(self, action_id: int) -> str:
        with Session(self.engine) as session:
            return session.get(PendingActionORM, action_id).status

    def test_new_proposal_replaces_old_one(self) -> None:
        task = self.create_task()
        old = self.propose(task.id, "旧修改")
        new = self.propose(task.id, "新修改")
        self.assertEqual(self.status(old.id), "replaced")
        self.assertEqual(self.status(new.id), "pending")
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            action_service.confirm_action(session, old.id, user_id=self.user_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("替换", raised.exception.detail)
        with Session(self.engine) as session:
            action_service.confirm_action(session, new.id, user_id=self.user_id)
        with Session(self.engine) as session:
            self.assertEqual(session.get(TaskORM, task.id).title, "新修改")

    def test_one_request_creates_independent_proposals_confirmed_in_either_order(self) -> None:
        registry = ToolRegistry()
        for order in ((0, 1), (1, 0)):
            with self.subTest(order=order):
                task = self.create_task()
                arguments = json.dumps({
                    "task_id": task.id,
                    "changes": {
                        "title": "新标题",
                        "start_at": "2030-01-03T15:00:00+08:00",
                    },
                }, ensure_ascii=False)
                with Session(self.engine) as session:
                    session.info["audit_request_id"] = f"request-{task.id}"
                    proposals = json.loads(registry.execute_tool(
                        "propose_update", arguments, session, user_id=self.user_id
                    ))
                self.assertEqual(len(proposals), 2)
                self.assertNotEqual(proposals[0]["id"], proposals[1]["id"])
                self.assertEqual([item["status"] for item in proposals], ["pending", "pending"])
                with Session(self.engine) as session:
                    saved = [session.get(PendingActionORM, item["id"]) for item in proposals]
                    self.assertEqual(saved[0].batch_id, saved[1].batch_id)
                    self.assertIsNotNone(saved[0].batch_id)

                for index in order:
                    with Session(self.engine) as session:
                        confirmed = action_service.confirm_action(
                            session, proposals[index]["id"], user_id=self.user_id
                        )
                        self.assertEqual(confirmed.status, "executed")

                with Session(self.engine) as session:
                    current = session.get(TaskORM, task.id)
                    self.assertEqual(current.title, "新标题")
                    self.assertEqual(current.start_at.isoformat(), "2030-01-03T07:00:00")
                    self.assertEqual(current.version, 3)
                    self.assertEqual(
                        [session.get(PendingActionORM, item["id"]).status for item in proposals],
                        ["executed", "executed"],
                    )

    def test_expired_proposal_is_rejected_and_swept(self) -> None:
        task = self.create_task()
        expired = self.propose(task.id, "过期修改")
        with Session(self.engine) as session, session.begin():
            session.get(PendingActionORM, expired.id).expires_at = 0
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            action_service.confirm_action(session, expired.id, user_id=self.user_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("过期", raised.exception.detail)
        self.assertEqual(self.status(expired.id), "expired")
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised_again:
            action_service.confirm_action(session, expired.id, user_id=self.user_id)
        self.assertIn("已过期", raised_again.exception.detail)

        unattended = self.propose(task.id, "无人处理")
        with Session(self.engine) as session, session.begin():
            session.get(PendingActionORM, unattended.id).expires_at = 0
        with Session(self.engine) as session:
            self.assertEqual(action_service.purge_expired_actions(session), 2)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(PendingActionORM, unattended.id))
            self.assertIsNone(session.get(PendingActionORM, expired.id))
        with Session(self.engine) as session:
            self.assertEqual(action_service.purge_expired_actions(session), 0)

    def test_stale_version_marks_conflict_and_allows_new_proposal(self) -> None:
        task = self.create_task()
        old = self.propose(task.id, "计划改名")
        with Session(self.engine) as session, session.begin():
            current = session.get(TaskORM, task.id)
            current.title = "别处已修改"
            current.version += 1
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            action_service.confirm_action(session, old.id, user_id=self.user_id)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("版本", raised.exception.detail)
        self.assertEqual(self.status(old.id), "conflicted")
        with Session(self.engine) as session:
            self.assertEqual(session.get(TaskORM, task.id).title, "别处已修改")
        new = self.propose(task.id, "重新改名")
        self.assertEqual(new.before.title, "别处已修改")
        self.assertEqual(new.before.version, 2)

    def test_repeated_confirm_and_cancel_do_not_reopen_action(self) -> None:
        task = self.create_task()
        action = self.propose(task.id, "确认一次")
        with Session(self.engine) as session:
            action_service.confirm_action(session, action.id, user_id=self.user_id)
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            action_service.confirm_action(session, action.id, user_id=self.user_id)
        self.assertIn("已执行", raised.exception.detail)
        with Session(self.engine) as session:
            current = session.get(TaskORM, task.id)
            self.assertEqual((current.title, current.version), ("确认一次", 2))

        cancelled = self.propose(task.id, "要取消")
        with Session(self.engine) as session:
            action_service.cancel_action(session, cancelled.id, user_id=self.user_id)
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            action_service.confirm_action(session, cancelled.id, user_id=self.user_id)
        self.assertIn("已取消", raised.exception.detail)
        self.assertEqual(self.status(cancelled.id), "cancelled")

    def test_unknown_action_id_is_not_confirmed(self) -> None:
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            action_service.confirm_action(session, 999999, user_id=self.user_id)
        self.assertEqual(raised.exception.status_code, 404)

    def test_deterministic_failure_rolls_back_task_and_marks_failed(self) -> None:
        task = self.create_task()
        action = self.propose(task.id, "失败的修改")
        original_update = task_repository.update_if_version

        def update_then_fail(*args, **kwargs):
            self.assertEqual(original_update(*args, **kwargs), 1)
            raise action_service.ActionExecutionError("模拟确定性执行失败")

        with patch.object(task_repository, "update_if_version", side_effect=update_then_fail):
            with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
                action_service.confirm_action(session, action.id, user_id=self.user_id)
        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("模拟确定性执行失败", raised.exception.detail)
        with Session(self.engine) as session:
            current = session.get(TaskORM, task.id)
            self.assertEqual((current.title, current.version), ("原任务", 1))
            self.assertEqual(action_service.get_action(session, action.id, user_id=self.user_id).status, "failed")
        with Session(self.engine) as session, self.assertRaises(HTTPException) as retry:
            action_service.confirm_action(session, action.id, user_id=self.user_id)
        self.assertEqual(retry.exception.status_code, 409)
        self.assertIn("执行失败", retry.exception.detail)

    def test_unknown_failure_rolls_back_without_falsely_marking_failed(self) -> None:
        task = self.create_task()
        action = self.propose(task.id, "未知错误")
        original_update = task_repository.update_if_version

        def update_then_fail(*args, **kwargs):
            original_update(*args, **kwargs)
            raise RuntimeError("执行结果不明")

        with patch.object(task_repository, "update_if_version", side_effect=update_then_fail):
            with Session(self.engine) as session, self.assertRaises(RuntimeError):
                action_service.confirm_action(session, action.id, user_id=self.user_id)
        with Session(self.engine) as session:
            current = session.get(TaskORM, task.id)
            self.assertEqual((current.title, current.version), ("原任务", 1))
        self.assertEqual(self.status(action.id), "pending")

    def test_invalid_saved_snapshot_becomes_failed_and_remains_readable(self) -> None:
        task = self.create_task()
        action = self.propose(task.id, "损坏快照")
        with Session(self.engine) as session, session.begin():
            session.get(PendingActionORM, action.id).after_snapshot = {"bad": "data"}
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            action_service.confirm_action(session, action.id, user_id=self.user_id)
        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("快照", raised.exception.detail)
        with Session(self.engine) as session:
            failed = action_service.get_action(session, action.id, user_id=self.user_id)
            self.assertEqual(failed.status, "failed")
            self.assertIsNone(failed.after)
            self.assertEqual(session.get(TaskORM, task.id).title, "原任务")

    def test_confirm_and_cancel_race_has_one_winner(self) -> None:
        task = self.create_task()
        action = self.propose(task.id, "竞态修改")
        barrier = Barrier(2)

        def attempt(operation: str) -> str:
            barrier.wait()
            with Session(self.engine) as session:
                try:
                    if operation == "confirm":
                        action_service.confirm_action(session, action.id, user_id=self.user_id)
                    else:
                        action_service.cancel_action(session, action.id, user_id=self.user_id)
                    return operation
                except HTTPException as exc:
                    self.assertEqual(exc.status_code, 409)
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, ["confirm", "cancel"]))
        self.assertEqual(outcomes.count("conflict"), 1)
        final_status = self.status(action.id)
        with Session(self.engine) as session:
            current = session.get(TaskORM, task.id)
            if final_status == "executed":
                self.assertEqual((current.title, current.version), ("竞态修改", 2))
            else:
                self.assertEqual(final_status, "cancelled")
                self.assertEqual((current.title, current.version), ("原任务", 1))
