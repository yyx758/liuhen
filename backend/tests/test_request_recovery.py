import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.agent import agent_runner
from backend.app.agent.llm import LLMResult
from backend.app.agent.tool_registry import ToolRegistry
from backend.app.db.isolated import isolated_engine
from backend.app.models.conversation import ConversationORM
from backend.app.models.operation_event import OperationEventORM
from backend.app.models.request import ChatTurnORM
from backend.app.models.task import TaskORM
from backend.app.schemas.task import TaskCreate
from backend.app.services import chat_service, chat_turn_service, conversation_service, task_service


def add_conversation(session_factory) -> str:
    conversation_id = str(uuid4())
    with session_factory() as session, session.begin():
        session.add(ConversationORM(id=conversation_id, created_at=0, updated_at=0, message_count=0))
    return conversation_id


class RequestRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.session_factory = sessionmaker(bind=self.engine)

    def tearDown(self) -> None:
        self.database.__exit__(None, None, None)

    def test_direct_create_request_is_idempotent(self) -> None:
        request_id = str(uuid4())
        data = TaskCreate(title="学习 Python", start_at="2030-01-02T14:00:00+08:00")
        with Session(self.engine) as session:
            first = task_service.create_task(data, session, request_id)
        with Session(self.engine) as session:
            second = task_service.create_task(data, session, request_id)
        self.assertEqual(first, second)
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(TaskORM)).all()), 1)
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            task_service.create_task(
                TaskCreate(title="不同内容", start_at="2030-01-03T14:00:00+08:00"),
                session, request_id,
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_concurrent_retries_return_same_task(self) -> None:
        request_id = str(uuid4())
        barrier = Barrier(2)

        def create_once() -> int:
            barrier.wait()
            with Session(self.engine) as session:
                result = task_service.create_task(
                    TaskCreate(title="并发重试", start_at="2030-01-04T14:00:00+08:00"),
                    session, request_id,
                )
                return result.id

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: create_once(), range(2)))
        self.assertEqual(ids[0], ids[1])
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(TaskORM)).all()), 1)

    def test_same_conversation_is_claimed_before_model_call(self) -> None:
        first = chat_turn_service.acquire_turn(
            str(uuid4()), "第一轮", None, self.session_factory
        )
        with self.assertRaises(HTTPException) as raised:
            chat_turn_service.acquire_turn(
                str(uuid4()), "第二轮", first.conversation_id, self.session_factory
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_concurrent_claim_has_one_winner(self) -> None:
        conversation_id = add_conversation(self.session_factory)
        barrier = Barrier(2)

        def claim() -> str:
            barrier.wait()
            try:
                chat_turn_service.acquire_turn(
                    str(uuid4()), "新消息", conversation_id, self.session_factory
                )
                return "claimed"
            except HTTPException as exc:
                self.assertEqual(exc.status_code, 409)
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: claim(), range(2)))
        self.assertCountEqual(outcomes, ["claimed", "conflict"])
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(ChatTurnORM)).all()), 1)

    def test_tool_result_failure_rolls_back_task(self) -> None:
        request_id = str(uuid4())
        turn = chat_turn_service.acquire_turn(
            request_id, "创建学习任务", None, self.session_factory
        )
        arguments = json.dumps({
            "title": "学习 Python", "start_at": "2030-01-02T14:00:00+08:00",
        })
        call = {
            "id": "call_rollback", "type": "function",
            "function": {"name": "create_task", "arguments": arguments},
        }
        chat_turn_service.append_message(
            request_id, turn.owner_token,
            {"role": "assistant", "tool_calls": [call]}, self.session_factory,
        )
        with patch.object(conversation_service, "normalize_message", side_effect=RuntimeError("保存失败")):
            with self.assertRaises(RuntimeError):
                chat_turn_service.execute_tool(
                    request_id, turn.owner_token, ToolRegistry(),
                    "create_task", arguments, call["id"], self.session_factory,
                )
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(TaskORM)).all()), 0)
            self.assertEqual(len(session.get(ChatTurnORM, request_id).messages), 2)

        chat_turn_service.execute_tool(
            request_id, turn.owner_token, ToolRegistry(),
            "create_task", arguments, call["id"], self.session_factory,
        )
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(TaskORM)).all()), 1)
            self.assertEqual(len(session.get(ChatTurnORM, request_id).messages), 3)

    async def test_tool_success_then_model_failure_resumes_without_duplicate(self) -> None:
        request_id = str(uuid4())
        call = {
            "id": "call_create_1",
            "type": "function",
            "function": {
                "name": "create_task",
                "arguments": json.dumps({
                    "title": "学习 Python",
                    "start_at": "2030-01-02T14:00:00+08:00",
                }),
            },
        }
        calls = 0

        async def fake_ask_llm(messages, tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                return LLMResult(
                    assistant_message={"role": "assistant", "tool_calls": [call]},
                    tool_calls=[call],
                )
            if calls == 2:
                raise RuntimeError("模型暂时不可用")
            return LLMResult(
                assistant_message={"role": "assistant", "content": "任务已创建"},
                content="任务已创建",
            )

        with (
            patch.object(chat_service, "SessionLocal", self.session_factory),
            patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm),
        ):
            with self.assertRaises(RuntimeError):
                await chat_service.send_message("明天下午学习 Python", request_id=request_id)

            with Session(self.engine) as session:
                turn = session.get(ChatTurnORM, request_id)
                self.assertEqual([m["role"] for m in turn.messages], ["user", "assistant", "tool"])
                self.assertEqual(session.get(ConversationORM, turn.conversation_id).message_count, 0)
                self.assertEqual(len(session.scalars(select(TaskORM)).all()), 1)
                audit = session.scalars(select(OperationEventORM)).one()
                self.assertEqual(audit.request_id, request_id)
                self.assertEqual(audit.conversation_id, turn.conversation_id)
                self.assertEqual(audit.tool_call_id, call["id"])

            conversation_id, answer = await chat_service.send_message(
                "明天下午学习 Python", request_id=request_id
            )
            again = await chat_service.send_message(
                "明天下午学习 Python", request_id=request_id
            )

        self.assertEqual(answer, "任务已创建")
        self.assertEqual(again, (conversation_id, answer))
        self.assertEqual(calls, 3)
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(TaskORM)).all()), 1)
            self.assertEqual(session.get(ConversationORM, conversation_id).message_count, 4)
            self.assertEqual(session.get(ChatTurnORM, request_id).status, "completed")

    async def test_commit_before_in_memory_result_resumes_without_duplicate(self) -> None:
        request_id = str(uuid4())
        call = {
            "id": "call_committed",
            "type": "function",
            "function": {
                "name": "create_task",
                "arguments": json.dumps({
                    "title": "已提交的任务", "start_at": "2030-01-05T14:00:00+08:00",
                }),
            },
        }
        model_calls = 0

        async def fake_ask_llm(messages, tools):
            nonlocal model_calls
            model_calls += 1
            if model_calls == 1:
                return LLMResult(
                    assistant_message={"role": "assistant", "tool_calls": [call]},
                    tool_calls=[call],
                )
            return LLMResult(
                assistant_message={"role": "assistant", "content": "已创建"},
                content="已创建",
            )

        original_execute = chat_turn_service.execute_tool
        interrupted = False

        def commit_then_interrupt(*args):
            nonlocal interrupted
            result = original_execute(*args)
            if not interrupted:
                interrupted = True
                raise RuntimeError("提交后进程中断")
            return result

        with (
            patch.object(chat_service, "SessionLocal", self.session_factory),
            patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm),
        ):
            with patch.object(chat_turn_service, "execute_tool", side_effect=commit_then_interrupt):
                with self.assertRaises(RuntimeError):
                    await chat_service.send_message("创建任务", request_id=request_id)
            with Session(self.engine) as session:
                self.assertEqual(len(session.scalars(select(TaskORM)).all()), 1)
                self.assertEqual(session.get(ChatTurnORM, request_id).messages[-1]["role"], "tool")

            _, answer = await chat_service.send_message("创建任务", request_id=request_id)

        self.assertEqual(answer, "已创建")
        self.assertEqual(model_calls, 2)
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(TaskORM)).all()), 1)

    async def test_history_save_failure_replays_final_answer(self) -> None:
        request_id = str(uuid4())
        calls = 0

        async def fake_ask_llm(messages, tools):
            nonlocal calls
            calls += 1
            return LLMResult(
                assistant_message={"role": "assistant", "content": "收到"},
                content="收到",
            )

        with (
            patch.object(chat_service, "SessionLocal", self.session_factory),
            patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm),
        ):
            with patch.object(conversation_service, "append_messages", side_effect=RuntimeError("保存失败")):
                with self.assertRaises(RuntimeError):
                    await chat_service.send_message("你好", request_id=request_id)
            with Session(self.engine) as session:
                turn = session.get(ChatTurnORM, request_id)
                self.assertEqual(turn.messages[-1]["content"], "收到")
                self.assertEqual(session.get(ConversationORM, turn.conversation_id).message_count, 0)

            conversation_id, answer = await chat_service.send_message("你好", request_id=request_id)

        self.assertEqual(answer, "收到")
        self.assertEqual(calls, 1)
        with Session(self.engine) as session:
            self.assertEqual(session.get(ConversationORM, conversation_id).message_count, 2)
