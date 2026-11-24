import json
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.agent import agent_runner
from backend.app.agent.llm import LLMResult
from backend.app.agent.tool_registry import ToolRegistry
from backend.app.db.isolated import create_user, isolated_engine
from backend.app.models.action import PendingActionORM
from backend.app.models.task import TaskORM
from backend.app.services import chat_service, conversation_service


class AgentRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.user_id = create_user(self.engine, "alice")
        self.session_factory = sessionmaker(bind=self.engine)
        self.session_patch = patch.object(
            agent_runner, "SessionLocal", self.session_factory
        )
        self.session_patch.start()

    def tearDown(self) -> None:
        self.session_patch.stop()
        self.database.__exit__(None, None, None)

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
                    {
                        "create_task", "list_tasks", "get_task", "get_action", "propose_update", "propose_delete",
                        "search_documents", "read_document", "list_documents",
                    },
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
            answer = await agent_runner.run_agent_loop("后天下午两点学习 Python，一小时", [], user_id=self.user_id)

        self.assertEqual(len(messages_seen), 2)
        self.assertEqual(messages_seen[1][-1]["role"], "tool")
        self.assertEqual(messages_seen[1][-1]["tool_call_id"], "call_1")
        return answer, messages_seen[1]

    async def test_on_event_reports_tool_calls_and_text_deltas_in_order(self) -> None:
        arguments = json.dumps(
            {"title": "写周报", "start_at": "2030-01-02T14:00:00+08:00"}, ensure_ascii=False
        )
        call = {"id": "call_1", "type": "function",
                "function": {"name": "create_task", "arguments": arguments}}
        rounds = []

        async def fake_ask_llm(messages, tools, *, on_delta):
            rounds.append(len(messages))
            if len(rounds) == 1:
                # 工具调用前的过渡文字也会作为 delta 发出，由前端在 tool 事件后清掉。
                await on_delta("我来创建")
                return LLMResult(
                    content="我来创建",
                    assistant_message={"role": "assistant", "content": "我来创建", "tool_calls": [call]},
                    tool_calls=[call],
                )
            for part in ["已创建", "写周报"]:
                await on_delta(part)
            return LLMResult(
                content="已创建写周报",
                assistant_message={"role": "assistant", "content": "已创建写周报"},
            )

        events = []

        async def on_event(event: str, data: dict) -> None:
            events.append((event, data))

        with patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm):
            answer = await agent_runner.run_agent_loop(
                "后天下午两点写周报", [], on_event=on_event, user_id=self.user_id
            )

        self.assertEqual(answer, "已创建写周报")
        self.assertEqual(events, [
            ("delta", {"text": "我来创建"}),
            ("tool", {"name": "create_task"}),
            ("delta", {"text": "已创建"}),
            ("delta", {"text": "写周报"}),
        ])

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
                user_id=self.user_id,
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
            created = json.loads(registry.execute_tool("create_task", create_args, session, user_id=self.user_id))
        task_id = created["id"]

        # 模拟下一轮：新注册表仍可用对话中已明确的 ID 查询当前数据库状态。
        registry = ToolRegistry()
        with Session(self.engine) as session:
            found = json.loads(registry.execute_tool(
                "get_task", json.dumps({"task_id": task_id}), session, user_id=self.user_id
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
            update = json.loads(registry.execute_tool("propose_update", update_args, session, user_id=self.user_id))
        self.assertEqual(len(update), 2)
        rename, reschedule = update
        self.assertEqual([item["status"] for item in update], ["pending", "pending"])
        self.assertEqual(rename["before"]["title"], "学习 Python")
        self.assertEqual(rename["after"]["title"], "复习 Python")
        self.assertEqual(reschedule["after"]["start_at"], "2030-01-02T16:00:00+08:00")
        self.assertIsNone(reschedule["after"]["end_at"])
        self.assertIsNone(reschedule["after"]["duration_minutes"])
        with Session(self.engine) as session:
            current_action = json.loads(registry.execute_tool(
                "get_action", json.dumps({"action_id": rename["id"]}), session, user_id=self.user_id
            ))
        self.assertEqual(current_action["status"], "pending")
        self.assertEqual(current_action["before"]["title"], "学习 Python")
        self.assertEqual(current_action["after"]["title"], "复习 Python")
        self.assertIn(
            "开始时间：2030-01-02 14:00 → 2030-01-02 16:00",
            registry.terminal_reply("propose_update", json.dumps(update, ensure_ascii=False)),
        )

        with Session(self.engine) as session:
            listed = json.loads(registry.execute_tool("list_tasks", "{}", session, user_id=self.user_id))
        self.assertEqual([task["id"] for task in listed], [task_id])

        with Session(self.engine) as session:
            deletion = json.loads(registry.execute_tool(
                "propose_delete", json.dumps({"task_id": task_id}), session, user_id=self.user_id
            ))
            task = session.get(TaskORM, task_id)
            self.assertEqual(task.title, "学习 Python")
            self.assertEqual(task.version, 1)
        self.assertEqual(deletion["status"], "pending")
        self.assertIsNone(deletion["after"])
        with Session(self.engine) as session:
            replaced_actions = [json.loads(registry.execute_tool(
                "get_action", json.dumps({"action_id": item["id"]}), session, user_id=self.user_id
            )) for item in update]
        self.assertEqual([item["status"] for item in replaced_actions], ["replaced", "replaced"])
        self.assertIn(
            "任务尚未删除",
            registry.terminal_reply("propose_delete", json.dumps(deletion, ensure_ascii=False)),
        )

    async def test_proposal_summary_uses_saved_snapshots_after_checking_remaining_work(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(TaskORM(
                title="学习 Python",
                priority="medium",
                status="todo",
                version=1,
                start_at=datetime(2030, 1, 2, 6),
                end_at=datetime(2030, 1, 2, 7),
                user_id=self.user_id,
            ))

        calls = [
            {"id": "list_1", "type": "function", "function": {"name": "list_tasks", "arguments": "{}"}},
            {"id": "update_1", "type": "function", "function": {
                "name": "propose_update",
                "arguments": json.dumps({
                    "task_id": 1,
                    "changes": {
                        "title": "复习 Python",
                        "start_at": "2030-01-02T16:00:00+08:00",
                    },
                }, ensure_ascii=False),
            }},
        ]
        seen = []

        async def fake_ask_llm(messages, tools):
            seen.append(list(messages))
            if len(seen) == 3:
                return LLMResult(
                    assistant_message={"role": "assistant", "content": "已处理"},
                    content="已处理",
                )
            call = calls[len(seen) - 1]
            return LLMResult(
                assistant_message={"role": "assistant", "tool_calls": [call]},
                tool_calls=[call],
            )

        history = []
        with patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm):
            answer = await agent_runner.run_agent_loop(
                "把学习 Python 改名为复习 Python，并改到下午四点", history, user_id=self.user_id
            )

        self.assertEqual(len(seen), 3)
        self.assertIn("标题：学习 Python → 复习 Python", answer)
        self.assertIn("开始时间：2030-01-02 14:00 → 2030-01-02 16:00", answer)
        self.assertEqual(answer.count("提案 #"), 2)
        self.assertIn("任务尚未修改", answer)
        self.assertEqual(
            [message["role"] for message in history],
            ["user", "assistant", "tool", "assistant", "tool", "assistant"],
        )
        self.assertEqual(history[-1]["content"], answer)
        with Session(self.engine) as session:
            task = session.get(TaskORM, 1)
            actions = list(session.scalars(select(PendingActionORM)).all())
            self.assertEqual(task.title, "学习 Python")
            self.assertEqual(task.start_at, datetime(2030, 1, 2, 6))
            self.assertEqual([action.status for action in actions], ["pending", "pending"])

    async def test_multiple_proposal_tool_calls_in_one_reply_are_all_kept(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(TaskORM(
                title="旧标题", priority="medium", status="todo", version=1,
                start_at=datetime(2030, 1, 2, 6),
                user_id=self.user_id,
            ))
        calls = [
            {
                "id": f"update_{index}", "type": "function",
                "function": {
                    "name": "propose_update",
                    "arguments": json.dumps({
                        "task_id": 1, "changes": changes,
                    }, ensure_ascii=False),
                },
            }
            for index, changes in enumerate((
                {"title": "新标题"},
                {"start_at": "2030-01-02T16:00:00+08:00"},
            ))
        ]
        seen = []

        async def fake_ask_llm(messages, tools):
            seen.append(list(messages))
            if len(seen) == 1:
                return LLMResult(
                    assistant_message={"role": "assistant", "tool_calls": calls},
                    tool_calls=calls,
                )
            return LLMResult(
                assistant_message={"role": "assistant", "content": "已处理"},
                content="已处理",
            )

        with patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm):
            answer = await agent_runner.run_agent_loop("改标题，并改到下午四点", [], user_id=self.user_id)

        self.assertEqual(len(seen), 2)
        self.assertEqual(answer.count("提案 #"), 2)
        with Session(self.engine) as session:
            actions = list(session.scalars(select(PendingActionORM)).all())
            self.assertEqual([action.status for action in actions], ["pending", "pending"])
            self.assertEqual(actions[0].batch_id, actions[1].batch_id)
            self.assertEqual(session.get(TaskORM, 1).title, "旧标题")

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
            conversation_id, first = await chat_service.send_message("把周会改名", user_id=self.user_id)
            same_id, second = await chat_service.send_message("明天下午那条", conversation_id, user_id=self.user_id)

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


class ReminderToolTests(unittest.TestCase):
    """提醒 PRD R12：创建时可设提醒；修改提醒是独立的一组提案，提案展示用人能读懂的说法。"""

    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.user_id = create_user(self.engine, "alice")
        self.registry = ToolRegistry()

    def tearDown(self) -> None:
        self.database.__exit__(None, None, None)

    def run_tool(self, name: str, arguments: dict):
        with Session(self.engine) as session:
            return json.loads(self.registry.execute_tool(
                name, json.dumps(arguments, ensure_ascii=False), session, user_id=self.user_id,
            ))

    def test_create_with_reminder_and_separate_reminder_proposal(self) -> None:
        created = self.run_tool("create_task", {
            "title": "开组会", "start_at": "2030-01-02T10:00:00+08:00", "remind_before_minutes": 30,
        })
        self.assertEqual(created["remind_before_minutes"], 30)

        proposals = self.run_tool("propose_update", {
            "task_id": created["id"], "changes": {"title": "开周会", "remind_before_minutes": 60},
        })
        # 改标题和改提醒是两项独立修改，分别确认。
        self.assertEqual(len(proposals), 2)
        reminder = next(p for p in proposals if p["after"]["remind_before_minutes"] == 60)
        self.assertEqual(reminder["after"]["title"], "开组会")
        reply = self.registry.terminal_reply("propose_update", json.dumps(proposals, ensure_ascii=False))
        self.assertIn("提醒：提前 30 分钟 → 提前 1 小时", reply)

        cancel = self.run_tool("propose_update", {
            "task_id": created["id"], "changes": {"remind_before_minutes": None},
        })
        reply = self.registry.terminal_reply("propose_update", json.dumps(cancel, ensure_ascii=False))
        self.assertIn("提醒：提前 30 分钟 → 不提醒", reply)

    def test_reminder_labels(self) -> None:
        from backend.app.agent.tool_registry import format_reminder

        self.assertEqual(
            [format_reminder(v) for v in (None, 0, 15, 60, 120, 1440, 90)],
            ["不提醒", "准时提醒", "提前 15 分钟", "提前 1 小时", "提前 2 小时", "提前 1 天", "提前 90 分钟"],
        )
