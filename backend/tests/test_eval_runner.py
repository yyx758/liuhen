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
from backend.app.evals.grading import score_business
from backend.app.evals.judge import judge_reply
from backend.app.evals.run import run_case, summarize
from backend.app.evals.scenarios import OTHER_USER_MARKER, ScenarioContext
from backend.app.schemas.task import TaskUpdate
from backend.app.services import action_service, chat_service, usage_service

ANCHOR = datetime(2030, 1, 1, 12, tzinfo=USER_TIMEZONE)


def scripted_llm(steps: list):
    """按顺序返回预设的模型输出：dict 表示一次工具调用（name/arguments），str 表示最终回复。"""
    queue = list(steps)

    async def fake_ask_llm(messages, tools):
        step = queue.pop(0)
        if isinstance(step, str):
            return LLMResult(assistant_message={"role": "assistant", "content": step}, content=step)
        call = {
            "id": f"call_{len(queue)}", "type": "function",
            "function": {"name": step["name"], "arguments": json.dumps(step.get("arguments", {}), ensure_ascii=False)},
        }
        return LLMResult(assistant_message={"role": "assistant", "tool_calls": [call]}, tool_calls=[call])

    return fake_ask_llm


async def run_scripted(case_id: str, steps: list) -> dict:
    with patch.object(agent_runner, "ask_llm", side_effect=scripted_llm(steps)):
        result = await run_case(case_id, ANCHOR, anchor="test")
    result["business_accuracy"] = score_business(result)
    return result


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
                        session, task_id, TaskUpdate(title=title), user_id=context.user_id,
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


class ScenarioGradingTests(unittest.IsolatedAsyncioTestCase):
    """用脚本化的模型输出跑新场景，确认场景、状态收集与业务评分彼此配合。"""

    def assert_passed(self, result: dict) -> None:
        self.assertIsNone(result["run_error"])
        failed = [c["item"] for c in result["business_accuracy"]["checks"] if not c["passed"]]
        self.assertEqual(failed, [])

    async def test_ambiguous_delete_resolved_in_second_turn(self) -> None:
        result = await run_scripted("05", [
            {"name": "list_tasks"}, "有两个整理材料：明天10:00 和后天16:00，删哪一个？",
            {"name": "propose_delete", "arguments": {"task_id": 2}}, "已生成",
        ])
        self.assert_passed(result)
        self.assertEqual(result["actions"][0]["task_id"], 2)

    async def test_other_users_task_is_invisible(self) -> None:
        result = await run_scripted("23", [
            {"name": "propose_delete", "arguments": {"task_id": 1}}, "没有找到 ID 为 1 的任务，没有删除任何内容。",
        ])
        self.assert_passed(result)
        self.assertEqual(len(result["other_user"]["tasks"]), 1)
        self.assertEqual(result["other_user"]["tasks"], result["other_user"]["initial_tasks"])

        listed = await run_scripted("24", [{"name": "list_tasks"}, "我只能看到你自己的任务：今天 7:00 晨跑。"])
        self.assert_passed(listed)
        self.assertEqual([t["title"] for t in listed["tasks"]], ["晨跑"])

        refused = await run_scripted("24", ["我只能查看你自己的任务，需要的话可以帮你列出来。"])
        self.assert_passed(refused)

    async def test_today_query_lists_overdue_and_ignores_exclusion_notes(self) -> None:
        listed = "今天开始；今天截止；今天开始且截止；跨午夜任务；逾期：昨天已逾期"
        result = await run_scripted("03", [{"name": "list_tasks"}, f"{listed}\n今天已完成已经完成，所以没有列入。"])
        self.assert_passed(result)

        # 单列"未列入的任务"小节时，小节内的标题不影响业务分（是否多列交给裁判）。
        section = await run_scripted("03", [{"name": "list_tasks"}, f"{listed}\n**未列入的任务**\n- 跨越今天的任务：没有时长"])
        self.assert_passed(section)
        missing = await run_scripted("03", [{"name": "list_tasks"}, "今天开始；今天截止；今天开始且截止；昨天已逾期\n跨午夜任务没有列入。"])
        self.assertIn("missing_task", missing["business_accuracy"]["failure_tags"])

    async def test_leak_in_reply_fails_business_check(self) -> None:
        result = await run_scripted("24", [{"name": "list_tasks"}, f"晨跑；另外别人还有 项目报告-{OTHER_USER_MARKER}"])
        self.assertEqual(result["business_accuracy"]["score"], 0)
        self.assertIn("cross_user_leak", result["business_accuracy"]["failure_tags"])

    async def test_multi_field_update_and_template_reply_skips_judge(self) -> None:
        result = await run_scripted("25", [{"name": "propose_update", "arguments": {
            "task_id": 1, "changes": {"title": "最终报告", "due_at": "2030-01-03T17:00:00+08:00"},
        }}, "已生成"])
        self.assert_passed(result)
        self.assertEqual(len(result["actions"]), 2)
        judged = await judge_reply(result)
        self.assertEqual((judged["score"], judged["skipped"]), (None, "template_reply"))

    async def test_cancel_request_in_chat_keeps_proposal_and_ignores_env_limit(self) -> None:
        # 测评用户有自己的上限，部署环境把默认上限调得再低也不影响多轮场景。
        with patch.object(usage_service, "DEFAULT_DAILY_CHAT_LIMIT", 1):
            result = await run_scripted("26", [
                {"name": "propose_update", "arguments": {"task_id": 1, "changes": {"status": "done"}}},
                "已生成",
                "我不能替你取消提案，它仍待确认；可以在确认入口点取消，或等它过期。",
            ])
        self.assert_passed(result)
        self.assertEqual(result["actions"][0]["status"], "pending")
        judged = await judge_reply(result)
        self.assertNotEqual(judged.get("skipped"), "template_reply")


class SummaryTests(unittest.TestCase):
    def test_summary_reports_unstable_groups(self) -> None:
        def run(case_id: str, score: int, anchor: str = "standard") -> dict:
            return {
                "case_id": case_id, "anchor": anchor, "variant": None,
                "business_accuracy": {"score": score}, "agent_reply_quality": {"score": None},
            }

        report = {"results": [
            run("01", 100), run("01", 100), run("01", 100),
            run("02", 100), run("02", 0), run("02", 100),
            run("02", 100, "year_end"),
        ]}
        summarize(report)
        summary = report["summary"]
        self.assertEqual(summary["business_all_runs_pass_rate"], round(2 / 3, 4))
        self.assertEqual(summary["unstable_or_failing"], {"02:default@standard": "2/3"})


if __name__ == "__main__":
    unittest.main()
