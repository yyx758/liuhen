"""用户 B 通过各个入口访问用户 A 的数据，全部应表现为"不存在"或被拒绝。"""

import json
import unittest
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.agent.tool_registry import ToolRegistry
from backend.app.api import actions, audit, chat, tasks
from backend.app.api.deps import current_session_user, current_user, current_user_id, require_admin
from backend.app.db.isolated import create_user, isolated_engine
from backend.app.db.session import get_db
from backend.app.main import app as main_app
from backend.app.models.action import PendingActionORM
from backend.app.models.conversation import ConversationORM
from backend.app.models.request import ChatTurnORM, DeletedChatRequestORM
from backend.app.schemas.task import TaskCreate, TaskUpdate
from backend.app.services import action_service, chat_turn_service, task_service

SECRET_ANSWER = "A 的私密回复"


class UserIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.factory = sessionmaker(bind=self.engine)
        self.alice = create_user(self.engine, "alice")
        self.bob = create_user(self.engine, "bob")

        with Session(self.engine) as session:
            self.task = task_service.create_task(TaskCreate(
                title="A 的任务", start_at="2030-01-02T10:00:00+08:00", duration_minutes=60,
            ), session, user_id=self.alice)
        with Session(self.engine) as session:
            self.action = action_service.propose_update(
                session, self.task.id, TaskUpdate(title="A 的修改"), user_id=self.alice,
            )
        self.task_key = str(uuid4())
        with Session(self.engine) as session:
            task_service.create_task(TaskCreate(
                title="A 的幂等任务", due_at="2030-01-03T10:00:00+08:00",
            ), session, self.task_key, user_id=self.alice)

        self.conversation_id = str(uuid4())
        self.request_id = str(uuid4())
        self.deleted_request_id = str(uuid4())
        with self.factory() as session, session.begin():
            session.add(ConversationORM(
                id=self.conversation_id, user_id=self.alice,
                created_at=0, updated_at=0, message_count=0,
            ))
            session.flush()
            session.add(ChatTurnORM(
                request_id=self.request_id, user_id=self.alice,
                conversation_id=self.conversation_id, user_text="你好",
                status="completed", active_conversation_id=None, owner_token=None,
                lease_until=None, base_message_count=0, messages=[],
                answer=SECRET_ANSWER, created_at=0,
            ))
            session.add(DeletedChatRequestORM(
                request_id=self.deleted_request_id, user_id=self.alice, deleted_at=0, turn_created_at=0,
            ))

        api = FastAPI()
        for module in (tasks, actions, chat, audit):
            api.include_router(module.router)

        def test_db():
            with Session(self.engine) as session:
                yield session

        self.as_user = self.bob
        api.dependency_overrides[get_db] = test_db
        api.dependency_overrides[current_user_id] = lambda: self.as_user
        self.client = TestClient(api)

    def tearDown(self) -> None:
        self.client.close()
        self.database.__exit__(None, None, None)

    def test_tasks_of_others_look_missing(self) -> None:
        task_id = self.task.id
        self.assertEqual(self.client.get("/tasks").json(), [])
        self.assertEqual(self.client.get(f"/tasks/{task_id}").status_code, 404)
        patched = self.client.patch(f"/tasks/{task_id}", headers={"If-Match": "1"}, json={"title": "B 改的"})
        self.assertEqual(patched.status_code, 404)
        self.assertEqual(self.client.delete(f"/tasks/{task_id}", headers={"If-Match": "1"}).status_code, 404)
        self.assertEqual(self.client.post(f"/tasks/{task_id}/proposals/update", json={"title": "x"}).status_code, 404)
        self.assertEqual(self.client.post(f"/tasks/{task_id}/proposals/delete").status_code, 404)

        self.as_user = self.alice
        self.assertEqual(self.client.get(f"/tasks/{task_id}").json()["title"], "A 的任务")

    def test_actions_of_others_look_missing(self) -> None:
        action_id = self.action.id
        self.assertEqual(self.client.get("/actions/pending").json(), [])
        self.assertEqual(self.client.get(f"/actions/conversations/{self.conversation_id}").json(), [])
        for method, path in (
            ("get", f"/actions/{action_id}"),
            ("post", f"/actions/{action_id}/confirm"),
            ("post", f"/actions/{action_id}/cancel"),
            ("delete", f"/actions/{action_id}"),
        ):
            with self.subTest(path=path, method=method):
                self.assertEqual(getattr(self.client, method)(path).status_code, 404)
        with Session(self.engine) as session:
            self.assertEqual(session.get(PendingActionORM, action_id).status, "pending")

    def test_idempotency_key_of_others_is_rejected_without_replay(self) -> None:
        response = self.client.post("/tasks", headers={"Idempotency-Key": self.task_key}, json={
            "title": "A 的幂等任务", "due_at": "2030-01-03T10:00:00+08:00",
        })
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("A 的幂等任务", response.text)

    def test_chat_ids_of_others_are_rejected_without_replay(self) -> None:
        def acquire(request_id: str, conversation_id: str | None):
            return chat_turn_service.acquire_turn(
                request_id, "你好", conversation_id, self.factory, user_id=self.bob,
            )

        for request_id in (self.request_id, self.deleted_request_id):
            with self.subTest(request_id=request_id), self.assertRaises(HTTPException) as raised:
                acquire(request_id, None)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertNotIn(SECRET_ANSWER, str(raised.exception.detail))

        with self.assertRaises(HTTPException) as raised:
            acquire(str(uuid4()), self.conversation_id)
        self.assertEqual(raised.exception.status_code, 404)
        with self.factory() as session:
            turns = session.scalar(select(func.count()).select_from(ChatTurnORM).where(
                ChatTurnORM.conversation_id == self.conversation_id
            ))
        self.assertEqual(turns, 1)

        self.assertEqual(self.client.delete(f"/chat/conversations/{self.conversation_id}").status_code, 404)
        with self.factory() as session:
            self.assertIsNotNone(session.get(ConversationORM, self.conversation_id))

    def test_audit_only_shows_own_events(self) -> None:
        self.assertEqual(self.client.get("/audit/events").json(), [])
        self.assertEqual(self.client.get("/audit/events/page").json()["total"], 0)
        self.assertEqual(self.client.get(f"/audit/events?task_id={self.task.id}").json(), [])
        self.assertEqual(self.client.get("/audit/summary").json()["total"], 0)

        self.as_user = self.alice
        self.assertGreater(self.client.get("/audit/summary").json()["total"], 0)

    def test_schedule_conflicts_are_per_user(self) -> None:
        same_slot = {"title": "同一时段", "start_at": "2030-01-02T10:30:00+08:00", "duration_minutes": 30}
        response = self.client.post("/tasks", headers={"Idempotency-Key": str(uuid4())}, json=same_slot)
        self.assertEqual(response.status_code, 201, response.text)

        self.as_user = self.alice
        response = self.client.post("/tasks", headers={"Idempotency-Key": str(uuid4())}, json=same_slot)
        self.assertEqual(response.status_code, 409)

    def test_agent_tools_stay_within_the_turn_owner(self) -> None:
        registry = ToolRegistry()
        with Session(self.engine) as session:
            self.assertEqual(json.loads(registry.execute_tool("list_tasks", "{}", session, user_id=self.bob)), [])
        for name, arguments in (
            ("get_task", {"task_id": self.task.id}),
            ("get_action", {"action_id": self.action.id}),
            ("propose_update", {"task_id": self.task.id, "changes": {"title": "B 改的"}}),
            ("propose_delete", {"task_id": self.task.id}),
        ):
            # 与线上一致：每次工具调用使用独立的 Session。
            with self.subTest(tool=name), Session(self.engine) as session:
                with self.assertRaises(HTTPException) as raised:
                    registry.execute_tool(name, json.dumps(arguments), session, user_id=self.bob)
                self.assertEqual(raised.exception.status_code, 404)
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(PendingActionORM)), 1)

        # 模型无法通过参数指定归属：参数模型禁止额外字段。
        with Session(self.engine) as session:
            for name, arguments in (
                ("list_tasks", {"user_id": self.alice}),
                ("create_task", {"title": "x", "due_at": "2030-01-05T10:00:00+08:00", "user_id": self.alice}),
            ):
                with self.subTest(tool=name), self.assertRaises(ValidationError):
                    registry.execute_tool(name, json.dumps(arguments), session, user_id=self.bob)


class RouteProtectionTests(unittest.TestCase):
    # 不需要登录的接口；新增公开接口必须在这里显式登记。
    PUBLIC = {
        ("POST", "/auth/login"), ("POST", "/auth/logout"), ("POST", "/auth/register"),
        ("POST", "/auth/register/email-code"), ("POST", "/auth/password-reset/email-code"),
        ("POST", "/auth/password-reset"),
    }
    # 强制改密状态下仍可访问的接口；其余 /auth/* 也必须拒绝待改密状态。
    PASSWORD_CHANGE_ALLOWED = {("GET", "/auth/me"), ("POST", "/auth/password")}

    def test_every_route_requires_login(self) -> None:
        def depends_on(dependant, target) -> bool:
            return any(dep.call is target or depends_on(dep, target) for dep in dependant.dependencies)

        problems = []
        for route in main_app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods:
                if (method, route.path) in self.PUBLIC:
                    continue
                # 业务接口还必须拒绝"待改密"状态，所以要求依赖 current_user_id；管理接口要求管理员。
                if (method, route.path) in self.PASSWORD_CHANGE_ALLOWED:
                    target = current_session_user
                elif route.path.startswith("/auth/"):
                    target = current_user
                elif route.path.startswith("/admin/"):
                    target = require_admin
                else:
                    target = current_user_id
                if not depends_on(route.dependant, target):
                    problems.append(f"{method} {route.path}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
