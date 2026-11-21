import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api.deps import current_user_id
from backend.app.db.isolated import create_user, isolated_engine
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.models.action import PendingActionORM
from backend.app.models.operation_event import OperationEventORM
from backend.app.models.request import TaskCreateRequestORM
from backend.app.models.task import TaskORM
from backend.app.repositories import task_repository
from backend.app.schemas.task import TaskCreate, TaskUpdate
from backend.app.services import action_service, audit_service, task_service


class OperationAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.user_id = create_user(self.engine, "alice")
        self.session_factory = sessionmaker(bind=self.engine)

    def tearDown(self) -> None:
        self.database.__exit__(None, None, None)

    def create(self, title="原任务", start="2030-01-02T14:00:00+08:00", request_id=None):
        with Session(self.engine) as session:
            return task_service.create_task(
                TaskCreate(title=title, start_at=start, duration_minutes=60), session, request_id, user_id=self.user_id
            )

    def propose_update(self, task_id, title):
        with Session(self.engine) as session:
            return action_service.propose_update(session, task_id, TaskUpdate(title=title), user_id=self.user_id)

    def events(self, action_id):
        with Session(self.engine) as session:
            return list(session.scalars(
                select(OperationEventORM)
                .where(OperationEventORM.action_id == action_id)
                .order_by(OperationEventORM.id)
            ))

    def test_create_retry_has_one_event_and_audit_failure_rolls_back(self) -> None:
        request_id = str(uuid4())
        first = self.create(request_id=request_id)
        second = self.create(request_id=request_id)
        self.assertEqual(first.id, second.id)
        with Session(self.engine) as session:
            events = list(session.scalars(select(OperationEventORM)))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_type, "task_created")
            self.assertEqual(events[0].request_id, request_id)
            self.assertEqual(events[0].details["task"]["id"], first.id)

        other_id = str(uuid4())
        with patch.object(audit_service, "record_event", side_effect=RuntimeError("审计写入失败")):
            with self.assertRaises(RuntimeError):
                self.create("不可提交", "2030-01-03T14:00:00+08:00", other_id)
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(TaskORM)).all()), 1)
            self.assertIsNone(session.get(TaskCreateRequestORM, other_id))
            self.assertEqual(len(session.scalars(select(OperationEventORM)).all()), 1)

    def test_confirmation_audit_failure_rolls_back_task_and_status(self) -> None:
        task = self.create()
        action = self.propose_update(task.id, "不能半提交")
        with patch.object(audit_service, "record_event", side_effect=RuntimeError("审计写入失败")):
            with Session(self.engine) as session, self.assertRaises(RuntimeError):
                action_service.confirm_action(session, action.id, user_id=self.user_id)
        with Session(self.engine) as session:
            current = session.get(TaskORM, task.id)
            self.assertEqual((current.title, current.version), ("原任务", 1))
            self.assertEqual(session.get(PendingActionORM, action.id).status, "pending")
        self.assertEqual(
            [row.event_type for row in self.events(action.id)], ["proposal_created"]
        )

    def test_proposal_replacement_confirmation_and_deletion_are_traced(self) -> None:
        task = self.create()
        old = self.propose_update(task.id, "旧提案")
        new = self.propose_update(task.id, "新提案")
        self.assertEqual(
            [row.event_type for row in self.events(old.id)],
            ["proposal_created", "proposal_replaced"],
        )
        self.assertEqual(
            self.events(old.id)[-1].details["replaced_by_action_id"], new.id
        )
        with Session(self.engine) as session:
            action_service.confirm_action(session, new.id, user_id=self.user_id)
        with Session(self.engine) as session, self.assertRaises(HTTPException):
            action_service.confirm_action(session, new.id, user_id=self.user_id)
        self.assertEqual(
            [row.event_type for row in self.events(new.id)],
            ["proposal_created", "proposal_executed"],
        )

        with Session(self.engine) as session:
            deletion = action_service.propose_delete(session, task.id, user_id=self.user_id)
        with Session(self.engine) as session:
            action_service.confirm_action(session, deletion.id, user_id=self.user_id)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(TaskORM, task.id))
        self.assertEqual(
            [row.event_type for row in self.events(deletion.id)],
            ["proposal_created", "proposal_executed"],
        )
        self.assertEqual(self.events(deletion.id)[-1].details["before"]["id"], task.id)

    def test_cancel_expire_conflict_and_failed_are_traced(self) -> None:
        task = self.create()
        cancelled = self.propose_update(task.id, "取消")
        with Session(self.engine) as session:
            action_service.cancel_action(session, cancelled.id, user_id=self.user_id)
        self.assertEqual(self.events(cancelled.id)[-1].event_type, "proposal_cancelled")

        expired = self.propose_update(task.id, "过期")
        with Session(self.engine) as session, session.begin():
            session.get(PendingActionORM, expired.id).expires_at = 0
        with Session(self.engine) as session:
            action_service.purge_expired_actions(session)
        self.assertEqual(
            [row.event_type for row in self.events(expired.id)][-2:],
            ["proposal_expired", "proposal_deleted"],
        )

        with Session(self.engine) as session:
            conflict = action_service.propose_update(
                session, task.id, TaskUpdate(start_at="2030-01-02T18:00:00+08:00"), user_id=self.user_id
            )
        self.create("占用时间", "2030-01-02T18:00:00+08:00")
        with Session(self.engine) as session, self.assertRaises(HTTPException):
            action_service.confirm_action(session, conflict.id, user_id=self.user_id)
        conflict_event = self.events(conflict.id)[-1]
        self.assertEqual(conflict_event.event_type, "proposal_conflicted")
        self.assertIn("时间与任务", conflict_event.reason)

        failed = self.propose_update(task.id, "失败")
        original_update = task_repository.update_if_version

        def update_then_fail(*args, **kwargs):
            original_update(*args, **kwargs)
            raise action_service.ActionExecutionError("确定性失败")

        with patch.object(task_repository, "update_if_version", side_effect=update_then_fail):
            with Session(self.engine) as session, self.assertRaises(HTTPException):
                action_service.confirm_action(session, failed.id, user_id=self.user_id)
        failed_event = self.events(failed.id)[-1]
        self.assertEqual(failed_event.event_type, "proposal_failed")
        self.assertEqual(failed_event.reason, "确定性失败")
        with Session(self.engine) as session:
            self.assertEqual(session.get(TaskORM, task.id).title, "原任务")

    def test_version_conflict_and_inline_expiry_are_traced(self) -> None:
        task = self.create()
        stale = self.propose_update(task.id, "旧版本")
        with Session(self.engine) as session, session.begin():
            current = session.get(TaskORM, task.id)
            current.title = "别处修改"
            current.version += 1
        with Session(self.engine) as session, self.assertRaises(HTTPException):
            action_service.confirm_action(session, stale.id, user_id=self.user_id)
        self.assertEqual(self.events(stale.id)[-1].event_type, "proposal_conflicted")
        self.assertIn("版本", self.events(stale.id)[-1].reason)

        expired_on_confirm = self.propose_update(task.id, "确认过期")
        with Session(self.engine) as session, session.begin():
            session.get(PendingActionORM, expired_on_confirm.id).expires_at = 0
        with Session(self.engine) as session, self.assertRaises(HTTPException):
            action_service.confirm_action(session, expired_on_confirm.id, user_id=self.user_id)
        self.assertEqual(self.events(expired_on_confirm.id)[-1].event_type, "proposal_expired")

        expired_on_cancel = self.propose_update(task.id, "取消过期")
        with Session(self.engine) as session, session.begin():
            session.get(PendingActionORM, expired_on_cancel.id).expires_at = 0
        with Session(self.engine) as session, self.assertRaises(HTTPException):
            action_service.cancel_action(session, expired_on_cancel.id, user_id=self.user_id)
        self.assertEqual(self.events(expired_on_cancel.id)[-1].event_type, "proposal_expired")

    def test_delete_expired_proposal_preserves_audit_and_rolls_back_on_failure(self) -> None:
        task = self.create()
        action = self.propose_update(task.id, "过期后清理")
        with Session(self.engine) as session, self.assertRaises(HTTPException) as early:
            action_service.delete_expired_action(session, action.id, user_id=self.user_id)
        self.assertEqual(early.exception.status_code, 409)

        with Session(self.engine) as session, session.begin():
            session.get(PendingActionORM, action.id).expires_at = 0
        with patch.object(audit_service, "record_event", side_effect=RuntimeError("审计写入失败")):
            with Session(self.engine) as session, self.assertRaises(RuntimeError):
                action_service.delete_expired_action(session, action.id, user_id=self.user_id)
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(PendingActionORM, action.id))

        with Session(self.engine) as session:
            action_service.delete_expired_action(session, action.id, user_id=self.user_id)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(PendingActionORM, action.id))
            self.assertIsNotNone(session.get(TaskORM, task.id))
        self.assertEqual(
            [row.event_type for row in self.events(action.id)],
            ["proposal_created", "proposal_expired", "proposal_deleted"],
        )
        with Session(self.engine) as session, self.assertRaises(HTTPException) as repeated:
            action_service.delete_expired_action(session, action.id, user_id=self.user_id)
        self.assertEqual(repeated.exception.status_code, 404)

    async def test_delete_expired_proposal_http_endpoint(self) -> None:
        task = self.create()
        action = self.propose_update(task.id, "通过接口删除")

        def isolated_db():
            with self.session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[current_user_id] = lambda: self.user_id
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                self.assertEqual((await client.delete(f"/actions/{action.id}")).status_code, 409)
                with Session(self.engine) as session, session.begin():
                    session.get(PendingActionORM, action.id).expires_at = 0
                response = await client.delete(f"/actions/{action.id}")
                self.assertEqual(response.status_code, 204)
                self.assertEqual((await client.get(f"/actions/{action.id}")).status_code, 404)
                audit = await client.get("/audit/events", params={"action_id": action.id})
                self.assertEqual(audit.json()[0]["event_type"], "proposal_deleted")
        finally:
            app.dependency_overrides.clear()

    def test_background_purge_only_removes_elapsed_proposals(self) -> None:
        first = self.create(start="2030-01-02T14:00:00+08:00")
        second = self.create(start="2030-01-03T14:00:00+08:00")
        third = self.create(start="2030-01-04T14:00:00+08:00")
        pending_expired = self.propose_update(first.id, "待清理")
        marked_expired = self.propose_update(second.id, "已标记过期")
        active = self.propose_update(third.id, "仍有效")
        with Session(self.engine) as session, session.begin():
            session.get(PendingActionORM, marked_expired.id).expires_at = 0
        with Session(self.engine) as session, self.assertRaises(HTTPException):
            action_service.confirm_action(session, marked_expired.id, user_id=self.user_id)
        with Session(self.engine) as session, session.begin():
            session.get(PendingActionORM, pending_expired.id).expires_at = 0
        with Session(self.engine) as session:
            self.assertEqual(action_service.purge_expired_actions(session), 2)
        with Session(self.engine) as session:
            self.assertEqual(action_service.purge_expired_actions(session), 0)
            self.assertIsNone(session.get(PendingActionORM, pending_expired.id))
            self.assertIsNone(session.get(PendingActionORM, marked_expired.id))
            self.assertEqual(session.get(PendingActionORM, active.id).status, "pending")
        self.assertEqual(self.events(marked_expired.id)[-1].event_type, "proposal_deleted")

    async def test_audit_read_endpoint_filters_events(self) -> None:
        task = self.create()
        action = self.propose_update(task.id, "待确认")
        with Session(self.engine) as session:
            action_service.confirm_action(session, action.id, user_id=self.user_id)

        def isolated_db():
            with self.session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[current_user_id] = lambda: self.user_id
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/audit/events", params={"action_id": action.id, "limit": 1}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [row["event_type"] for row in response.json()], ["proposal_executed"]
                )
                self.assertEqual(response.json()[0]["task_id"], task.id)
                older = await client.get("/audit/events", params={
                    "action_id": action.id,
                    "before_id": response.json()[0]["id"],
                })
                self.assertEqual(
                    [row["event_type"] for row in older.json()], ["proposal_created"]
                )
        finally:
            app.dependency_overrides.clear()
