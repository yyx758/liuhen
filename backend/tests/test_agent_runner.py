import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import URL, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.agent import agent_runner
from backend.app.agent.llm import LLMResult
from backend.app.agent.tool_registry import ToolRegistry
from backend.app.db.base import Base
from backend.app.models.action import PendingActionORM
from backend.app.models.task import TaskORM
from backend.app.services import chat_service, conversation_service


class AgentRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            URL.create(
                "sqlite+pysqlite",
                database=str(Path(self.directory.name) / "agent_test.db"),
            )
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.session_patch = patch.object(
            agent_runner, "SessionLocal", self.session_factory
        )
        self.session_patch.start()

    def tearDown(self) -> None:
        self.session_patch.stop()
        self.engine.dispose()
        self.directory.cleanup()

    async def run_with_tool_call(self, title: str, final_reply: str) -> tuple[str, list[dict]]:
        arguments = json.dumps(
            {
                "title": title,
                "start_at": "2030-01-02T14:00:00+08:00",
                "duration_minutes": 60,
            },
            ensure_ascii=False,
        )
        call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "create_task", "arguments": arguments},
        }
        messages_seen = []

        async def fake_ask_llm(messages, tools):
            if not messages_seen:
                self.assertEqual(
                    {tool["function"]["name"] for tool in tools},
                    {"create_task", "list_tasks", "get_task", "get_action", "propose_update", "propose_delete"},
                )
                messages_seen.append(list(messages))
                return LLMResult(
                    assistant_message={"role": "assistant", "tool_calls": [call]},
                    tool_calls=[call],
                )

            messages_seen.append(list(messages))
            return LLMResult(
                assistant_message={"role": "assistant", "content": final_reply},
                content=final_reply,
            )

        with patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm):
            answer = await agent_runner.run_agent_loop("后天下午两点学习 Python，一小时", [])

        self.assertEqual(len(messages_seen), 2)
        self.assertEqual(messages_seen[1][-1]["role"], "tool")
        self.assertEqual(messages_seen[1][-1]["tool_call_id"], "call_1")
        return answer, messages_seen[1]

    async def test_create_task_reports_actual_result(self) -> None:
        answer, messages = await self.run_with_tool_call("学习 Python", "已创建学习 Python 任务")

        tool_result = json.loads(messages[-1]["content"])
        with Session(self.engine) as session:
            tasks = list(session.scalars(select(TaskORM)).all())

        self.assertEqual(answer, "已创建学习 Python 任务")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].id, tool_result["id"])
        self.assertEqual(tasks[0].title, "学习 Python")
        self.assertEqual(tool_result["start_at"], "2030-01-02T14:00:00+08:00")
        self.assertEqual(tool_result["end_at"], "2030-01-02T15:00:00+08:00")

    async def test_time_conflict_returns_error_without_creating_task(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(TaskORM(
                title="已有任务",
                priority="medium",
                status="todo",
                version=1,
                start_at=datetime(2030, 1, 2, 6),
                end_at=datetime(2030, 1, 2, 7),
            ))

        answer, messages = await self.run_with_tool_call("冲突任务", "这个时间已有任务，未创建")

        tool_result = json.loads(messages[-1]["content"])
        with Session(self.engine) as session:
            tasks = list(session.scalars(select(TaskORM)).all())

        self.assertEqual(answer, "这个时间已有任务，未创建")
        self.assertEqual(tool_result["ok"], False)
        self.assertIn("409", tool_result["error"])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "已有任务")

    async def test_query_and_proposals_keep_task_unchanged(self) -> None:
        registry = ToolRegistry()
        create_args = json.dumps({
            "title": "学习 Python",
            "start_at": "2030-01-02T14:00:00+08:00",
        }, ensure_ascii=False)
        with Session(self.engine) as session:
            created = json.loads(registry.execute_tool("create_task", create_args, session))
        task_id = created["id"]

        # 模拟下一轮：新注册表仍可用对话中已明确的 ID 查询当前数据库状态。
        registry = ToolRegistry()
        with Session(self.engine) as session:
            found = json.loads(registry.execute_tool(
                "get_task", json.dumps({"task_id": task_id}), session
            ))
        self.assertEqual(found["title"], "学习 Python")

        update_args = json.dumps({
            "task_id": task_id,
            "changes": {
                "title": "复习 Python",
                "start_at": "2030-01-02T16:00:00+08:00",
            },
        }, ensure_ascii=False)
        with Session(self.engine) as session:
            update = json.loads(registry.execute_tool("propose_update", update_args, session))
        self.assertEqual(update["status"], "pending")
        self.assertEqual(update["before"]["title"], "学习 Python")
        self.assertEqual(update["after"]["title"], "复习 Python")
        self.assertEqual(update["after"]["start_at"], "2030-01-02T16:00:00+08:00")
        self.assertIsNone(update["after"]["end_at"])
        self.assertIsNone(update["after"]["duration_minutes"])
        with Session(self.engine) as session:
            current_action = json.loads(registry.execute_tool(
                "get_action", json.dumps({"action_id": update["id"]}), session
            ))
        self.assertEqual(current_action["status"], "pending")
        self.assertEqual(current_action["before"]["title"], "学习 Python")
        self.assertEqual(current_action["after"]["title"], "复习 Python")
        self.assertIn(
            "开始时间：2030-01-02 14:00 → 2030-01-02 16:00",
            registry.terminal_reply("propose_update", json.dumps(update, ensure_ascii=False)),
        )

        with Session(self.engine) as session:
            listed = json.loads(registry.execute_tool("list_tasks", "{}", session))
        self.assertEqual([task["id"] for task in listed], [task_id])

        with Session(self.engine) as session:
            deletion = json.loads(registry.execute_tool(
                "propose_delete", json.dumps({"task_id": task_id}), session
            ))
            task = session.get(TaskORM, task_id)
            self.assertEqual(task.title, "学习 Python")
            self.assertEqual(task.version, 1)
        self.assertEqual(deletion["status"], "pending")
        self.assertIsNone(deletion["after"])
        with Session(self.engine) as session:
            replaced_action = json.loads(registry.execute_tool(
                "get_action", json.dumps({"action_id": update["id"]}), session
            ))
        self.assertEqual(replaced_action["status"], "replaced")
        self.assertIn(
            "任务尚未删除",
            registry.terminal_reply("propose_delete", json.dumps(deletion, ensure_ascii=False)),
        )

    async def test_proposal_stops_agent_and_uses_saved_snapshots(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(TaskORM(
                title="学习 Python",
                priority="medium",
                status="todo",
                version=1,
                start_at=datetime(2030, 1, 2, 6),
                end_at=datetime(2030, 1, 2, 7),
            ))

        calls = [
            {"id": "list_1", "type": "function", "function": {"name": "list_tasks", "arguments": "{}"}},
            {"id": "update_1", "type": "function", "function": {
                "name": "propose_update",
                "arguments": json.dumps({
                    "task_id": 1,
                    "changes": {"title": "复习 Python"},
                }, ensure_ascii=False),
            }},
        ]
        seen = []

        async def fake_ask_llm(messages, tools):
            seen.append(list(messages))
            if len(seen) > 2:
                self.fail("提案创建后不应再次调用模型")
            call = calls[len(seen) - 1]
            return LLMResult(
                assistant_message={"role": "assistant", "tool_calls": [call]},
                tool_calls=[call],
            )

        history = []
        with patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm):
            answer = await agent_runner.run_agent_loop("把学习 Python 改名为复习 Python", history)

        self.assertEqual(len(seen), 2)
        self.assertIn("标题：学习 Python → 复习 Python", answer)
        self.assertIn("任务尚未修改", answer)
        self.assertEqual(
            [message["role"] for message in history],
            ["user", "assistant", "tool", "assistant", "tool", "assistant"],
        )
        self.assertEqual(history[-1]["content"], answer)
        with Session(self.engine) as session:
            task = session.get(TaskORM, 1)
            action = session.scalars(select(PendingActionORM)).one()
            self.assertEqual(task.title, "学习 Python")
            self.assertEqual(action.status, "pending")

    async def test_conversation_history_is_loaded_for_next_turn(self) -> None:
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
            conversation_id, first = await chat_service.send_message("把周会改名")
            same_id, second = await chat_service.send_message("明天下午那条", conversation_id)

        self.assertEqual(same_id, conversation_id)
        self.assertEqual(first, replies[0])
        self.assertEqual(second, replies[1])
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
            saved = conversation_service.load_history(session, conversation_id)
        self.assertEqual(saved.message_count, 4)
        self.assertEqual(
            [message["content"] for message in saved.to_llm_history()],
            ["把周会改名", replies[0], "明天下午那条", replies[1]],
        )


if __name__ == "__main__":
    unittest.main()
