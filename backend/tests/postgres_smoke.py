"""Exercise PostgreSQL business writes in a disposable database.

Run from the project root after configuring PostgreSQL in .env:
    python -m backend.tests.postgres_smoke
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.base import Base
from backend.app.db.session import get_db
from backend.app.db.session import DATABASE_URL
from backend.app.api import tasks as tasks_api
from backend.app.models import action, conversation, operation_event, request, task  # noqa: F401
from backend.app.models.action import PendingActionORM
from backend.app.models.task import TaskORM
from backend.app.repositories import task_repository
from backend.app.schemas.task import TaskCreate, TaskUpdate
from backend.app.services import action_service, chat_turn_service, conversation_service, task_service


def run() -> None:
    url = make_url(DATABASE_URL)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError(".env 的 database_url 必须指向 PostgreSQL")

    test_name = f"liuhen_test_{uuid4().hex[:12]}"
    admin = psycopg.connect(
        host=url.host, port=url.port, user=url.username,
        password=url.password, dbname="postgres", autocommit=True,
    )
    engine = None
    try:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_name)))
        engine = create_engine(url.set(database=test_name))
        Base.metadata.create_all(engine)

        request_id = str(uuid4())
        data = TaskCreate(
            title="PostgreSQL 测试任务",
            start_at="2030-01-02T14:00:00+08:00",
            duration_minutes=60,
            due_at="2030-01-03T12:00:00+08:00",
        )
        with Session(engine) as session:
            created = task_service.create_task(data, session, request_id)
        with Session(engine) as session:
            repeated = task_service.create_task(data, session, request_id)
            assert repeated.id == created.id
            assert repeated.due_at == created.due_at

        retry_key = str(uuid4())
        retry_barrier = Barrier(2)

        def concurrent_retry(_: int) -> int:
            retry_barrier.wait(timeout=10)
            with Session(engine) as session:
                return task_service.create_task(TaskCreate(
                    title="并发重试任务",
                    start_at="2030-03-02T14:00:00+08:00",
                    duration_minutes=60,
                ), session, retry_key).id

        with ThreadPoolExecutor(max_workers=2) as pool:
            retry_ids = list(pool.map(concurrent_retry, (1, 2)))
        assert retry_ids[0] == retry_ids[1], retry_ids

        with Session(engine) as session:
            try:
                task_service.create_task(TaskCreate(
                    title="冲突任务", start_at="2030-01-02T14:30:00+08:00",
                    duration_minutes=30,
                ), session)
            except HTTPException as exc:
                assert exc.status_code == 409
            else:
                raise AssertionError("重叠任务应返回 409")

        with Session(engine) as session:
            proposal = action_service.propose_update(
                session, created.id, TaskUpdate(title="已确认的测试任务")
            )
        with Session(engine) as session:
            confirmed = action_service.confirm_action(session, proposal.id)
            assert confirmed.status == "executed"

        barrier = Barrier(2)
        original_find = task_repository.find_overlapping_task

        def synchronized_find(session, start_at, end_at, exclude_task_id=None):
            found = original_find(session, start_at, end_at, exclude_task_id)
            if start_at.year == 2031:
                barrier.wait(timeout=10)
            return found

        def concurrent_create(index: int) -> int:
            with Session(engine) as session:
                try:
                    task_service.create_task(TaskCreate(
                        title=f"并发任务 {index}",
                        start_at="2031-01-02T14:00:00+08:00",
                        duration_minutes=60,
                    ), session)
                    return 201
                except HTTPException as exc:
                    return exc.status_code

        with patch.object(task_repository, "find_overlapping_task", synchronized_find):
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = sorted(pool.map(concurrent_create, (1, 2)))
        assert statuses == [201, 409], statuses

        proposals = []
        for index, hour in ((1, 9), (2, 11)):
            with Session(engine) as session:
                source_task = task_service.create_task(TaskCreate(
                    title=f"待改期任务 {index}",
                    start_at=f"2032-02-02T{hour:02d}:00:00+08:00",
                    duration_minutes=60,
                ), session)
            with Session(engine) as session:
                proposal = action_service.propose_update(
                    session, source_task.id,
                    TaskUpdate(start_at="2032-02-02T14:00:00+08:00"),
                )
                proposals.append(proposal.id)

        confirm_barrier = Barrier(2)
        original_conflict = action_service.find_schedule_conflict

        def synchronized_conflict(session, task_id, candidate):
            found = original_conflict(session, task_id, candidate)
            if candidate.start_at is not None and candidate.start_at.year == 2032:
                confirm_barrier.wait(timeout=10)
            return found

        def concurrent_confirm(action_id: int) -> str:
            with Session(engine) as session:
                try:
                    return action_service.confirm_action(session, action_id).status
                except HTTPException as exc:
                    assert exc.status_code == 409
                    return "conflicted"

        with patch.object(action_service, "find_schedule_conflict", synchronized_conflict):
            with ThreadPoolExecutor(max_workers=2) as pool:
                action_statuses = sorted(pool.map(concurrent_confirm, proposals))
        assert action_statuses == ["conflicted", "executed"], action_statuses

        with Session(engine) as session:
            proposal_task = task_service.create_task(TaskCreate(
                title="并发提案任务",
                start_at="2033-02-02T09:00:00+08:00",
            ), session)
        proposal_barrier = Barrier(2)

        def concurrent_propose(index: int) -> int:
            proposal_barrier.wait(timeout=10)
            with Session(engine) as session:
                return action_service.propose_update(
                    session, proposal_task.id,
                    TaskUpdate(title=f"提案标题 {index}"),
                ).id

        with ThreadPoolExecutor(max_workers=2) as pool:
            proposal_ids = list(pool.map(concurrent_propose, (1, 2)))
        assert proposal_ids[0] != proposal_ids[1]

        factory = sessionmaker(bind=engine)
        with factory() as session:
            conversation_id = conversation_service.create_conversation(session)
        turn_barrier = Barrier(2)

        def concurrent_turn(_: int) -> int:
            turn_barrier.wait(timeout=10)
            try:
                chat_turn_service.acquire_turn(
                    str(uuid4()), "测试并发对话", conversation_id, factory,
                )
                return 201
            except HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            turn_statuses = sorted(pool.map(concurrent_turn, (1, 2)))
        assert turn_statuses == [201, 409], turn_statuses

        api = FastAPI()
        api.include_router(tasks_api.router)

        def test_db():
            with Session(engine) as session:
                yield session

        api.dependency_overrides[get_db] = test_db
        with TestClient(api) as client:
            response = client.post("/tasks", headers={"Idempotency-Key": str(uuid4())}, json={
                "title": "HTTP 测试任务",
                "start_at": "2034-02-02T09:00:00+08:00",
            })
            assert response.status_code == 201, response.text
            response = client.get("/tasks")
            assert response.status_code == 200, response.text
            assert any(row["title"] == "HTTP 测试任务" for row in response.json())

        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(TaskORM)) == 7
            assert session.get(TaskORM, created.id).title == "已确认的测试任务"
            statuses = list(session.scalars(select(PendingActionORM.status).where(
                PendingActionORM.task_id == proposal_task.id
            )))
            assert sorted(statuses) == ["pending", "replaced"], statuses
        print("PostgreSQL smoke test passed: HTTP, read/write, idempotency, time, proposal, concurrent create/confirm/propose/chat")
    finally:
        if engine is not None:
            engine.dispose()
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(test_name)))
        admin.close()


if __name__ == "__main__":
    run()
