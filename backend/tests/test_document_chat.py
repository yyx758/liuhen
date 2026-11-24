"""资料库问答的回复链路（spec 3.5、3.7 第 5 步）：来源标记去除、sources 的保存与回放、
崩溃恢复、阅读量经持久化路径累计、循环上限。用模型替身和隔离数据库。"""

import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.agent import agent_runner
from backend.app.agent.llm import LLMResult
from backend.app.api.deps import current_user_id
from backend.app.db.isolated import create_user, isolated_engine
from backend.app.main import app
from backend.app.models.request import ChatTurnORM
from backend.app.models.task import TaskORM
from backend.app.schemas.document import DocumentCreate
from backend.app.services import chat_service, chat_turn_service, conversation_service, document_service
from backend.tests.test_chat_stream import parse_sse

QUESTION = "我笔记里缓存击穿怎么处理"


def tool_call(call_id, name, **arguments):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def tools_reply(*calls):
    return LLMResult(assistant_message={"role": "assistant", "content": "", "tool_calls": list(calls)},
                     tool_calls=list(calls))


def text_reply(text):
    return LLMResult(assistant_message={"role": "assistant", "content": text}, content=text)


class ChatTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.user_id = create_user(self.engine, "alice")
        self.session_factory = sessionmaker(bind=self.engine)
        with Session(self.engine) as session:
            self.doc = document_service.create_document(session, DocumentCreate(
                title="Redis 笔记",
                content="# 缓存击穿\n热点 key 过期时用互斥锁重建缓存。\n\n# 长文\n"
                        + "\n\n".join("长文段落" * 225 for _ in range(4)),
            ), user_id=self.user_id).id
        self.patches = [
            patch.object(chat_service, "SessionLocal", self.session_factory),
            patch.object(agent_runner, "SessionLocal", self.session_factory),
        ]
        for item in self.patches:
            item.start()
        app.dependency_overrides[current_user_id] = lambda: self.user_id

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        for item in self.patches:
            item.stop()
        self.database.__exit__(None, None, None)


class DocumentChatTests(ChatTestCase):
    def search_read_answer(self):
        """搜索 → 读命中的片段 → 带来源标记回答；返回模型替身和每轮看到的消息。"""
        seen = []

        async def fake_ask_llm(messages, tools, **_):
            seen.append(list(messages))
            if len(seen) == 1:
                return tools_reply(tool_call("s1", "search_documents", queries=["击穿"]))
            if len(seen) == 2:
                hit = json.loads(messages[-1]["content"])["hits"][0]
                self.chunk_id = hit["chunk_id"]
                return tools_reply(tool_call("r1", "read_document", document_id=self.doc, chunk_ids=[hit["chunk_id"]]))
            return text_reply(f"用互斥锁重建缓存 [[来源:{self.chunk_id}]]，再加随机过期 [[来源:999]]。")

        return fake_ask_llm, seen

    def expected_sources(self):
        return [{"document_id": self.doc, "title": "Redis 笔记", "heading_path": ["缓存击穿"],
                 "quote": "热点 key 过期时用互斥锁重建缓存。"}]

    async def post(self, path, body):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(path, json=body)

    async def test_chat_returns_stripped_answer_and_saved_sources(self) -> None:
        fake, _ = self.search_read_answer()
        request_id = str(uuid4())
        with patch.object(agent_runner, "ask_llm", side_effect=fake):
            response = await self.post("/chat", {"user_text": QUESTION, "request_id": request_id})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["answer"], "用互斥锁重建缓存，再加随机过期。")
        self.assertEqual(body["sources"], self.expected_sources())

        with Session(self.engine) as session:
            turn = session.get(ChatTurnORM, request_id)
            history = conversation_service.load_history(session, body["conversation_id"]).history
        self.assertIn(f"[[来源:{self.chunk_id}]]", turn.messages[-1]["content"])
        self.assertEqual((turn.answer, turn.sources), (body["answer"], self.expected_sources()))
        self.assertEqual(history[-1].content, body["answer"])

        # 同一 request_id 回放：不再调用模型，sources 相同。
        with patch.object(agent_runner, "ask_llm", side_effect=AssertionError("不应再调用模型")):
            replay = await self.post("/chat", {"user_text": QUESTION, "request_id": request_id})
            stream_replay = await self.post("/chat/stream", {"user_text": QUESTION, "request_id": request_id})
        self.assertEqual(replay.json(), body)
        [(event, payload)] = parse_sse(stream_replay.text)
        self.assertEqual((event, payload["sources"]), ("done", self.expected_sources()))

    async def test_next_turn_history_has_no_markers(self) -> None:
        fake, _ = self.search_read_answer()
        with patch.object(agent_runner, "ask_llm", side_effect=fake):
            conversation_id, _ = await chat_service.send_message(QUESTION, user_id=self.user_id)
        seen = []

        async def follow_up(messages, tools, **_):
            seen.append(list(messages))
            return text_reply("好的")

        with patch.object(agent_runner, "ask_llm", side_effect=follow_up):
            await chat_service.send_message("谢谢", conversation_id, user_id=self.user_id)
        self.assertFalse(any("[[来源" in (m.get("content") or "") for m in seen[0] if m["role"] == "assistant"))

    async def test_stream_done_event_carries_sources(self) -> None:
        fake, _ = self.search_read_answer()
        with patch.object(agent_runner, "ask_llm", side_effect=fake):
            response = await self.post("/chat/stream", {"user_text": QUESTION, "request_id": str(uuid4())})
        events = parse_sse(response.text)
        self.assertEqual([name for name, _ in events if name == "tool"], ["tool", "tool"])
        done = events[-1]
        self.assertEqual(done[0], "done")
        self.assertEqual(done[1]["answer"], "用互斥锁重建缓存，再加随机过期。")
        self.assertEqual(done[1]["sources"], self.expected_sources())

    async def test_recovery_after_final_reply_persisted_gives_same_sources(self) -> None:
        fake, seen = self.search_read_answer()
        request_id = str(uuid4())
        with (
            patch.object(agent_runner, "ask_llm", side_effect=fake),
            patch.object(chat_turn_service, "complete_turn", side_effect=RuntimeError("中断")),
            self.assertRaises(RuntimeError),
        ):
            await chat_service.send_message(QUESTION, request_id=request_id, user_id=self.user_id)
        self.assertEqual(len(seen), 3)

        with patch.object(agent_runner, "ask_llm", side_effect=AssertionError("不应再调用模型")):
            _, answer = await chat_service.send_message(QUESTION, request_id=request_id, user_id=self.user_id)
        self.assertEqual(answer, "用互斥锁重建缓存，再加随机过期。")
        with Session(self.engine) as session:
            self.assertEqual(session.get(ChatTurnORM, request_id).sources, self.expected_sources())

    async def test_chars_read_accumulates_through_persisted_messages(self) -> None:
        results = []

        async def fake_ask_llm(messages, tools, **_):
            if messages[-1]["role"] == "tool":
                results.append(json.loads(messages[-1]["content"]))
            if len(results) < 2:
                return tools_reply(tool_call(f"r{len(results)}", "read_document", document_id=self.doc))
            return text_reply("读完了")

        with patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm):
            await chat_service.send_message("读一下 Redis 笔记", user_id=self.user_id)
        first, second = results
        self.assertGreater(first["chars_returned"], 0)
        self.assertEqual(second["chars_left_this_turn"], 24_000 - first["chars_returned"] - second["chars_returned"])


class AgentLoopLimitTests(ChatTestCase):
    """上限只是防止失控的保险：触顶时不执行多出来的工具调用，再请模型不带工具地收尾（spec 3.5）。"""

    WRAP_UP = "已读的内容是……，还有一篇没读完。"

    def model(self, calls_per_round: int, name: str = "list_documents", arguments=None):
        """一直要求调用工具；不提供工具时（收尾）给出最终回复。记录每次调用时是否提供了工具。"""
        self.asked: list[bool] = []
        self.last_messages: list[dict] = []

        async def fake_ask_llm(messages, tools=None, **_):
            self.asked.append(bool(tools))
            self.last_messages = list(messages)
            if not tools:
                return text_reply(self.WRAP_UP)
            round_no = len(self.asked)
            return tools_reply(*(
                tool_call(f"c{round_no}_{i}", name, **(arguments(round_no, i) if arguments else {}))
                for i in range(calls_per_round)
            ))

        return fake_ask_llm

    async def run_loop(self, calls_per_round: int, **kwargs) -> str:
        with patch.object(agent_runner, "ask_llm", side_effect=self.model(calls_per_round, **kwargs)):
            return await agent_runner.run_agent_loop("列一下笔记", [], user_id=self.user_id)

    def assert_wrapped_up(self, answer: str) -> None:
        self.assertEqual(answer, self.WRAP_UP)
        self.assertEqual(self.asked[-1], False)  # 收尾这次不提供工具
        self.assertIn("不能再调用工具", self.last_messages[-1]["content"])

    async def test_round_limit_wraps_up_instead_of_failing(self) -> None:
        answer = await self.run_loop(1)
        self.assert_wrapped_up(answer)
        # 前 15 次都带工具，第 15 次要求的工具调用不执行，第 16 次不带工具收尾。
        self.assertEqual(self.asked, [True] * 15 + [False])

    async def test_tool_call_limit_wraps_up_without_running_extra_calls(self) -> None:
        answer = await self.run_loop(4)
        self.assert_wrapped_up(answer)
        # 7 轮 × 4 = 28 次；第 8 轮再要 4 次会超过 30，不执行，直接收尾。
        self.assertEqual(self.asked, [True] * 8 + [False])
        executed = {m["tool_call_id"] for m in self.last_messages if m.get("role") == "tool"}
        called = {c["id"] for m in self.last_messages for c in m.get("tool_calls") or []}
        self.assertEqual((len(executed), called), (28, executed))

    async def test_thirty_tool_calls_are_allowed(self) -> None:
        async def fake_ask_llm(messages, tools=None, **_):
            round_no = sum(m.get("role") == "assistant" for m in messages) + 1
            if round_no <= 6:
                return tools_reply(*(tool_call(f"c{round_no}_{i}", "list_documents") for i in range(5)))
            return text_reply("你有 1 篇笔记")

        with patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm):
            answer = await agent_runner.run_agent_loop("列一下笔记", [], user_id=self.user_id)
        self.assertEqual(answer, "你有 1 篇笔记")

    async def test_write_tools_have_their_own_limit(self) -> None:
        def create(round_no, _):
            return {"title": f"任务{round_no}", "start_at": f"2030-01-{round_no + 1:02d}T09:00:00+08:00"}

        with patch.object(agent_runner, "AGENT_MAX_WRITE_CALLS", 2):
            answer = await self.run_loop(1, name="create_task", arguments=create)
        self.assert_wrapped_up(answer)
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(TaskORM)), 2)

    async def test_persisted_turn_completes_after_wrap_up(self) -> None:
        with patch.object(agent_runner, "ask_llm", side_effect=self.model(4)):
            _, answer = await chat_service.send_message("列一下笔记", user_id=self.user_id)
        self.assertEqual(answer, self.WRAP_UP)
        with Session(self.engine) as session:
            turn = session.scalars(select(ChatTurnORM)).one()
        self.assertEqual(turn.status, "completed")
        # 没执行的那批工具调用不会留在已保存的消息里，恢复时也不会再去执行。
        called = {c["id"] for m in turn.messages for c in m.get("tool_calls") or []}
        answered = {m["tool_call_id"] for m in turn.messages if m.get("role") == "tool"}
        self.assertEqual(called, answered)


if __name__ == "__main__":
    unittest.main()
