import asyncio
import json
import unittest
from uuid import uuid4
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.agent import agent_runner
from backend.app.agent.llm import LLMResult
from backend.app.api import chat as chat_api
from backend.app.api.deps import current_user_id
from backend.app.db.isolated import create_user, isolated_engine
from backend.app.main import app
from backend.app.models.request import ChatTurnORM
from backend.app.models.task import TaskORM
from backend.app.schemas.conversation import ChatRequest
from backend.app.services import chat_service, chat_turn_service


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


CREATE_CALL = {
    "id": "create_1", "type": "function", "function": {
        "name": "create_task",
        "arguments": json.dumps(
            {"title": "写周报", "start_at": "2030-01-02T15:00:00+08:00"}, ensure_ascii=False
        ),
    },
}


async def tool_then_text(messages, tools, *, on_delta):
    if messages[-1]["role"] == "user":
        return LLMResult(
            assistant_message={"role": "assistant", "content": "", "tool_calls": [CREATE_CALL]},
            tool_calls=[CREATE_CALL],
        )
    for part in ["已创建", "写周报"]:
        await on_delta(part)
    return LLMResult(
        assistant_message={"role": "assistant", "content": "已创建写周报"},
        content="已创建写周报",
    )


class ChatStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.user_id = create_user(self.engine, "alice")
        self.session_factory = sessionmaker(bind=self.engine)
        app.dependency_overrides[current_user_id] = lambda: self.user_id
        self.session_patch = patch.object(chat_service, "SessionLocal", self.session_factory)
        self.session_patch.start()

    def tearDown(self) -> None:
        self.session_patch.stop()
        app.dependency_overrides.clear()
        self.database.__exit__(None, None, None)

    async def post_stream(self, body: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/chat/stream", json=body)

    async def test_stream_emits_progress_then_persisted_answer_and_replays(self) -> None:
        request_id = str(uuid4())
        body = {"user_text": "后天下午三点写周报", "request_id": request_id}

        with patch.object(agent_runner, "ask_llm", side_effect=tool_then_text):
            response = await self.post_stream(body)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = parse_sse(response.text)
        self.assertEqual(events[:3], [
            ("tool", {"name": "create_task"}),
            ("delta", {"text": "已创建"}),
            ("delta", {"text": "写周报"}),
        ])
        self.assertEqual(events[3][0], "done")
        self.assertEqual(events[3][1]["answer"], "已创建写周报")
        self.assertEqual(events[3][1]["request_id"], request_id)
        with Session(self.engine) as session:
            self.assertEqual(session.get(ChatTurnORM, request_id).status, "completed")
            self.assertEqual(len(session.scalars(select(TaskORM)).all()), 1)

        # 同一 request_id 重试：不再调用模型，只回放 done。
        with patch.object(agent_runner, "ask_llm", side_effect=AssertionError("不应再调用模型")):
            replay = await self.post_stream(body)
        self.assertEqual(parse_sse(replay.text), [events[3]])

    async def test_errors_before_streaming_keep_http_status(self) -> None:
        response = await self.post_stream({
            "user_text": "你好", "request_id": str(uuid4()), "conversation_id": str(uuid4()),
        })

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "会话不存在")

    async def test_failure_during_turn_is_sent_as_error_event_and_releases_turn(self) -> None:
        request_id = str(uuid4())

        async def broken_llm(messages, tools, *, on_delta):
            await on_delta("我想")
            raise RuntimeError("模型流式响应未正常结束")

        with (
            patch.object(agent_runner, "ask_llm", side_effect=broken_llm),
            self.assertLogs(chat_api.logger, "ERROR"),
        ):
            response = await self.post_stream({"user_text": "你好", "request_id": request_id})

        events = parse_sse(response.text)
        self.assertEqual(events[0], ("delta", {"text": "我想"}))
        self.assertEqual(events[-1][0], "error")
        # 内部异常信息不外泄，只提示用同一请求 ID 重试。
        self.assertNotIn("模型流式响应", events[-1][1]["detail"])
        with Session(self.engine) as session:
            turn = session.get(ChatTurnORM, request_id)
            self.assertEqual(turn.status, "processing")
            self.assertLessEqual(turn.lease_until, chat_turn_service.now_timestamp())

    async def test_turn_finishes_after_client_disconnects(self) -> None:
        request_id = str(uuid4())
        release_model = asyncio.Event()

        async def slow_llm(messages, tools, *, on_delta):
            await on_delta("正在")
            await release_model.wait()
            return LLMResult(
                assistant_message={"role": "assistant", "content": "好的"}, content="好的"
            )

        with patch.object(agent_runner, "ask_llm", side_effect=slow_llm):
            response = await chat_api.stream_message(
                ChatRequest(user_text="你好", request_id=request_id), self.user_id
            )
            body = response.body_iterator
            first = await anext(body)
            # 模拟客户端断开：关闭生成器后，后台任务仍应继续执行。
            await body.aclose()
            self.assertEqual(parse_sse(first), [("delta", {"text": "正在"})])
            self.assertEqual(len(chat_api.running_turns), 1)

            release_model.set()
            await asyncio.gather(*chat_api.running_turns)

        self.assertEqual(chat_api.running_turns, set())
        with Session(self.engine) as session:
            turn = session.get(ChatTurnORM, request_id)
            self.assertEqual((turn.status, turn.answer), ("completed", "好的"))


if __name__ == "__main__":
    unittest.main()
