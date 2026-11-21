import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

from backend.app.agent import agent_runner
from backend.app.agent.llm import LLMResult
from backend.app.config.clock import fixed_now, now_utc
from backend.app.config.settings import USER_TIMEZONE
from backend.app.db.isolated import isolated_engine
from backend.app.evals.scenarios import ScenarioContext
from backend.app.evals.run import run_case
from backend.app.schemas.task import TaskUpdate
from backend.app.services import action_service, chat_service


class EvalRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_evaluation_clock_covers_proposals_and_action_api(self) -> None:
        frozen = datetime(2030, 1, 1, 10, tzinfo=USER_TIMEZONE)
        with isolated_engine() as engine:
            factory = sessionmaker(bind=engine)
            context = ScenarioContext(factory, frozen)
            task_id = context.add_task("项目报告", start_at=context.local_time(1, 9))

            async def fake_send_message(*args, **kwargs):
                self.assertEqual(now_utc(), frozen.astimezone(timezone.utc))
                return "conversation", "已收到"

            with patch.object(chat_service, "send_message", side_effect=fake_send_message):
                await context.say("测试消息")
            self.assertEqual(context.turns[0]["reply"], "已收到")

            def propose(title: str):
                with factory() as session:
                    return action_service.propose_update(
                        session, task_id, TaskUpdate(title=title),
                    )

            with fixed_now(frozen):
                expired = await asyncio.to_thread(propose, "过期修改")
            self.assertEqual(expired.created_at, frozen.astimezone(timezone.utc))
            self.assertEqual(
                expired.expires_at,
                frozen.astimezone(timezone.utc) + timedelta(minutes=10),
            )

            context.evaluation_now = frozen + timedelta(minutes=11)
            rejected = await context.invoke_action("confirm", expired.id)
            self.assertEqual(rejected["status_code"], 409)
            self.assertIn("过期", rejected["detail"])

            with fixed_now(frozen):
                pending = await asyncio.to_thread(propose, "确认修改")
            context.evaluation_now = frozen + timedelta(minutes=5)
            confirmed = await context.invoke_action("confirm", pending.id)
            self.assertEqual(confirmed["status_code"], 200)
            self.assertEqual(confirmed["result"]["status"], "executed")
            self.assertNotEqual(now_utc().year, 2030)

    async def test_first_case_collects_isolated_business_evidence(self) -> None:
        call = {
            "id": "create_1", "type": "function", "function": {
                "name": "create_task",
                "arguments": json.dumps({
                    "title": "学习Agent",
                    "start_at": "2030-01-02T15:00:00+08:00",
                }, ensure_ascii=False),
            },
        }
        seen = []

        async def fake_ask_llm(messages, tools):
            seen.append(list(messages))
            if len(seen) == 1:
                return LLMResult(
                    assistant_message={"role": "assistant", "tool_calls": [call]},
                    tool_calls=[call],
                )
            return LLMResult(
                assistant_message={"role": "assistant", "content": "已创建明天下午三点的学习Agent任务"},
                content="已创建明天下午三点的学习Agent任务",
            )

        with (
            patch.object(agent_runner, "ask_llm", side_effect=fake_ask_llm),
            patch.object(chat_service, "SessionLocal", side_effect=AssertionError("访问了开发数据库")),
        ):
            result = await run_case("01", datetime(2030, 1, 1, 12, tzinfo=USER_TIMEZONE))

        self.assertEqual(result["case_id"], "01")
        self.assertEqual(result["chat_turn_status"], "completed")
        self.assertEqual(result["reply"], "已创建明天下午三点的学习Agent任务")
        self.assertIn("2030-01-01T12:00+08:00", seen[0][0]["content"])
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["title"], "学习Agent")
        self.assertEqual(result["tasks"][0]["start_at"], "2030-01-02T15:00:00+08:00")
        self.assertIsNone(result["tasks"][0]["due_at"])
        self.assertIsNone(result["tasks"][0]["duration_minutes"])
        self.assertEqual(
            [event["event_type"] for event in result["audit_events"]],
            ["task_created"],
        )
        tool_messages = [message for message in result["messages"] if message["role"] == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(json.loads(tool_messages[0]["content"])["title"], "学习Agent")


if __name__ == "__main__":
    unittest.main()
