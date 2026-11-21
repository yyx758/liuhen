import unittest
from unittest.mock import patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.isolated import isolated_engine
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.models.action import PendingActionORM
from backend.app.models.operation_event import OperationEventORM
from backend.app.models.task import TaskORM
from backend.app.schemas.task import TaskCreate, TaskUpdate
from backend.app.services import action_service, audit_service, task_service


class ManualTaskOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.session_factory = sessionmaker(bind=self.engine)

    def tearDown(self) -> None:
        self.database.__exit__(None, None, None)

    def create(self, title: str, start: str):
        with Session(self.engine) as session:
            return task_service.create_task(
                TaskCreate(title=title, start_at=start, duration_minutes=60), session
            )

    def events(self, task_id: int):
        with Session(self.engine) as session:
            return list(session.scalars(
                select(OperationEventORM)
                .where(OperationEventORM.task_id == task_id)
                .order_by(OperationEventORM.id)
            ))

    async def test_manual_http_update_and_delete_skip_proposals(self) -> None:
        task = self.create("手动任务", "2030-01-02T14:00:00+08:00")

        def isolated_db():
            with self.session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = isolated_db
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                missing_version = await client.patch(
                    f"/tasks/{task.id}", json={"status": "done"}
                )
                self.assertEqual(missing_version.status_code, 422)

                updated = await client.patch(
                    f"/tasks/{task.id}", json={"status": "done"},
                    headers={"If-Match": "1"},
                )
                self.assertEqual(updated.status_code, 200)
                self.assertEqual((updated.json()["status"], updated.json()["version"]), ("done", 2))

                stale = await client.patch(
                    f"/tasks/{task.id}", json={"title": "过时修改"},
                    headers={"If-Match": "1"},
                )
                self.assertEqual(stale.status_code, 409)

                stale_delete = await client.delete(
                    f"/tasks/{task.id}", headers={"If-Match": "1"}
                )
                self.assertEqual(stale_delete.status_code, 409)

                deleted = await client.delete(
                    f"/tasks/{task.id}", headers={"If-Match": "2"}
                )
                self.assertEqual(deleted.status_code, 204)
                self.assertEqual((await client.get(f"/tasks/{task.id}")).status_code, 404)
        finally:
            app.dependency_overrides.clear()

        with Session(self.engine) as session:
            self.assertEqual(session.scalars(select(PendingActionORM)).all(), [])
        self.assertEqual(
            [event.event_type for event in self.events(task.id)],
            ["task_created", "task_updated", "task_deleted"],
        )

    def test_manual_change_replaces_agent_proposal(self) -> None:
        task = self.create("原任务", "2030-01-02T14:00:00+08:00")
        with Session(self.engine) as session:
            proposal = action_service.propose_update(
                session, task.id, TaskUpdate(title="Agent 修改")
            )
        with Session(self.engine) as session:
            changed = task_service.update_task(
                session, task.id, TaskUpdate(title="手动修改"), task.version
            )
        self.assertEqual((changed.title, changed.version), ("手动修改", 2))
        with Session(self.engine) as session:
            self.assertEqual(session.get(PendingActionORM, proposal.id).status, "replaced")
        self.assertEqual(
            [event.event_type for event in self.events(task.id)],
            ["task_created", "proposal_created", "proposal_replaced", "task_updated"],
        )
        with Session(self.engine) as session:
            delete_proposal = action_service.propose_delete(session, task.id)
        with Session(self.engine) as session:
            task_service.delete_task(session, task.id, changed.version)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(TaskORM, task.id))
            self.assertEqual(session.get(PendingActionORM, delete_proposal.id).status, "replaced")
        self.assertEqual(
            [event.event_type for event in self.events(task.id)][-3:],
            ["proposal_created", "proposal_replaced", "task_deleted"],
        )

    def test_conflict_and_audit_failure_leave_task_unchanged(self) -> None:
        task = self.create("原任务", "2030-01-02T14:00:00+08:00")
        self.create("占用时段", "2030-01-02T16:00:00+08:00")
        with Session(self.engine) as session, self.assertRaises(HTTPException) as conflict:
            task_service.update_task(
                session, task.id,
                TaskUpdate(start_at="2030-01-02T16:00:00+08:00"), task.version,
            )
        self.assertEqual(conflict.exception.status_code, 409)

        with patch.object(audit_service, "record_event", side_effect=RuntimeError("审计失败")):
            with Session(self.engine) as session, self.assertRaises(RuntimeError):
                task_service.update_task(
                    session, task.id, TaskUpdate(title="不能半提交"), task.version
                )
        with Session(self.engine) as session:
            current = session.get(TaskORM, task.id)
            self.assertEqual((current.title, current.version), ("原任务", 1))
        self.assertEqual(
            [event.event_type for event in self.events(task.id)], ["task_created"]
        )
