import json
import tempfile
import unittest
from datetime import datetime
from uuid import uuid4
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import URL, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.agent import agent_runner
from backend.app.agent.llm import LLMResult
from backend.app.config.settings import USER_TIMEZONE
from backend.app.db.base import Base
from backend.app.main import app
from backend.app.models.operation_event import OperationEventORM
from backend.app.models.request import ChatTurnORM
from backend.app.models.task import TaskORM
from backend.app.services import chat_service, conversation_service


class ChatApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine(URL.create(
            "sqlite+pysqlite",
            database=str(Path(self.directory.name) / "chat_api.db"),
        ))
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.directory.cleanup()

    async def test_injected_clock_and_session_factory_cover_full_turn(self) -> None:
        request_id = str(uuid4())
        evaluation_now = datetime(2030, 1, 1, 12, tzinfo=USER_TIMEZONE)
        seen = []
        call = {
            "id": "create_1", "type": "function", "function": {
                "name": "create_task",
                "arguments": json.dumps({
                    "title": "学习Agent",
                    "start_at": "2030-01-02T15:00:00+08:00",
                }, ensure_ascii=False),
            },
        }

        async def fake_ask_llm(messages, tools):
            seen.append(list(messages))
            if len(seen) == 1:
                return LLMResult(
                    assistant_message={"role": "assistant", "tool_calls": [call]},
                    tool_calls=[call],
                )
            return LLMResult(
                assistant_message={"role": "assistant", "content": "已创建学习Agent任务"},
                content="已创建学习Agent任务",
            )

        with (
            patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm),
            patch.object(chat_service, "SessionLocal", side_effect=AssertionError("使用了开发数据库")),
        ):
            _, answer = await chat_service.send_message(
                "明天下午三点我要学习Agent",
                request_id=request_id,
                session_factory=self.session_factory,
                evaluation_now=evaluation_now,
            )

        self.assertEqual(answer, "已创建学习Agent任务")
        self.assertIn("当前本地时间为 2030-01-01T12:00+08:00", seen[0][0]["content"])
        with Session(self.engine) as session:
            tasks = list(session.scalars(select(TaskORM)).all())
            events = list(session.scalars(select(OperationEventORM)).all())
            turn = session.get(ChatTurnORM, request_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "学习Agent")
        self.assertEqual([event.event_type for event in events], ["task_created"])
        self.assertEqual(turn.status, "completed")
        self.assertEqual(
            [item["function"]["name"] for message in turn.messages
             for item in message.get("tool_calls", [])],
            ["create_task"],
        )

    async def test_two_http_requests_share_saved_history(self) -> None:
        seen = []
        replies = ["你指哪一条周会？", "明天下午那条，已了解。"]

        async def fake_ask_llm(messages, tools):
            seen.append(list(messages))
            reply = replies[len(seen) - 1]
            return LLMResult(
                assistant_message={"role": "assistant", "content": reply},
                content=reply,
            )

        with (
            patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm),
            patch.object(chat_service, "SessionLocal", self.session_factory),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                first_request_id = str(uuid4())
                second_request_id = str(uuid4())
                first = await client.post("/chat", json={
                    "user_text": "把周会改名", "request_id": first_request_id,
                })
                self.assertEqual(first.status_code, 200)
                conversation_id = first.json()["conversation_id"]

                second = await client.post("/chat", json={
                    "user_text": "明天下午那条",
                    "conversation_id": conversation_id,
                    "request_id": second_request_id,
                })
                self.assertEqual(second.status_code, 200)
                self.assertEqual(second.json(), {
                    "conversation_id": conversation_id,
                    "answer": replies[1],
                    "request_id": second_request_id,
                })

                empty = await client.post("/chat", json={
                    "user_text": "   ", "request_id": str(uuid4()),
                })
                self.assertEqual(empty.status_code, 422)

        self.assertEqual(
            [(message["role"], message["content"]) for message in seen[1]],
            [
                ("system", seen[1][0]["content"]),
                ("user", "把周会改名"),
                ("assistant", replies[0]),
                ("user", "明天下午那条"),
            ],
        )
        with Session(self.engine) as session:
            stored = conversation_service.load_history(session, conversation_id)
        self.assertEqual(stored.message_count, 4)


if __name__ == "__main__":
    unittest.main()
