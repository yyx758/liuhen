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
from backend.app.evals import document_fixtures as docs
from backend.app.evals.grading import score_business
from backend.app.evals.judge import judge_reply
from backend.app.evals.run import run_case, summarize
from backend.app.evals.scenarios import OTHER_USER_MARKER, ScenarioContext
from backend.app.schemas.task import TaskUpdate
from backend.app.services import action_service, chat_service, usage_service

ANCHOR = datetime(2030, 1, 1, 12, tzinfo=USER_TIMEZONE)


def scripted_llm(steps: list):
    """按顺序返回预设的模型输出：dict 表示一次工具调用（name/arguments），list 表示同一轮的多个工具调用，
    str 表示最终回复；callable 接收本次的 messages，返回以上任一种（用于依赖前面工具结果的调用）。"""
    queue = list(steps)
    counter = iter(range(10_000))

    async def fake_ask_llm(messages, tools):
        step = queue.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, str):
            return LLMResult(assistant_message={"role": "assistant", "content": step}, content=step)
        calls = [
            {
                "id": f"call_{next(counter)}", "type": "function",
                "function": {"name": item["name"], "arguments": json.dumps(item.get("arguments", {}), ensure_ascii=False)},
            }
            for item in (step if isinstance(step, list) else [step])
        ]
        return LLMResult(assistant_message={"role": "assistant", "tool_calls": calls}, tool_calls=calls)

    return fake_ask_llm


def tool_outputs(messages: list[dict]) -> list:
    """本轮到目前为止的工具结果（已解析）。"""
    return [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]


def read_hit(title: str, path: tuple[str, ...], *, scope: str = "section"):
    """读取前面搜索结果里命中指定小节的片段。"""
    def step(messages):
        for output in reversed(tool_outputs(messages)):
            for hit in (output.get("hits") or []) if isinstance(output, dict) else []:
                if hit["title"] == title and tuple(hit["heading_path"]) == path:
                    return {"name": "read_document", "arguments": {
                        "document_id": hit["document_id"], "chunk_ids": [hit["chunk_id"]], "scope": scope,
                    }}
        raise AssertionError(f"搜索结果里没有《{title}》{path}")
    return step


def cite_read(template: str):
    """最终回复：在 {} 处依次填入本轮读过的片段 ID 的来源标记。"""
    def step(messages):
        ids = [c["chunk_id"] for output in tool_outputs(messages) if isinstance(output, dict)
               for c in output.get("chunks") or []]
        return template.format(*(f"[[来源:{chunk_id}]]" for chunk_id in ids))
    return step


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


    async def test_reminder_scenarios_grade_reminder_fields(self) -> None:
        explicit = await run_scripted("27", [{"name": "create_task", "arguments": {
            "title": "开组会", "start_at": "2030-01-02T10:00:00+08:00", "remind_before_minutes": 30,
        }}, "已创建明天 10:00 的组会，开始前 30 分钟提醒你。"])
        self.assert_passed(explicit)

        on_time = await run_scripted("28", [{"name": "create_task", "arguments": {
            "title": "交周报", "due_at": "2030-01-02T15:00:00+08:00", "remind_before_minutes": 0,
        }}, "已创建，明天 15:00 准时提醒你交周报。"])
        self.assert_passed(on_time)
        # "提醒我"却没设提醒：业务分不通过。
        missing = await run_scripted("28", [{"name": "create_task", "arguments": {
            "title": "交周报", "start_at": "2030-01-02T15:00:00+08:00",
        }}, "已创建。"])
        self.assertIn("wrong_reminder", missing["business_accuracy"]["failure_tags"])

        for case_id, value in (("29", 60), ("30", None)):
            with self.subTest(case_id=case_id):
                proposed = await run_scripted(case_id, [{"name": "propose_update", "arguments": {
                    "task_id": 1, "changes": {"remind_before_minutes": value},
                }}, "已生成"])
                self.assert_passed(proposed)
                self.assertEqual(proposed["tasks"], proposed["initial_tasks"])

    async def test_title_ignores_spacing_between_chinese_and_english(self) -> None:
        # 全量测评 d7153c7 中模型写成"学习 Agent"，与"学习Agent"是同一个标题。
        for title in ("学习 Agent", "学习Agent"):
            with self.subTest(title=title):
                result = await run_scripted("01", [{"name": "create_task", "arguments": {
                    "title": title, "start_at": "2030-01-02T15:00:00+08:00",
                }}, "已创建。"])
                self.assert_passed(result)
        wrong = await run_scripted("01", [{"name": "create_task", "arguments": {
            "title": "学习 Python", "start_at": "2030-01-02T15:00:00+08:00",
        }}, "已创建。"])
        self.assertIn("wrong_title", wrong["business_accuracy"]["failure_tags"])

    async def test_create_without_reminder_request_must_not_set_one(self) -> None:
        result = await run_scripted("01", [{"name": "create_task", "arguments": {
            "title": "学习Agent", "start_at": "2030-01-02T15:00:00+08:00", "remind_before_minutes": 15,
        }}, "已创建。"])
        self.assertIn("unexpected_reminder", result["business_accuracy"]["failure_tags"])


class DocumentScenarioTests(unittest.IsolatedAsyncioTestCase):
    """31–45（spec 5.3）：用脚本化的模型输出确认预置笔记、证据收集与业务评分彼此配合。"""

    def assert_passed(self, result: dict) -> None:
        self.assertIsNone(result["run_error"])
        failed = [c["item"] for c in result["business_accuracy"]["checks"] if not c["passed"]]
        self.assertEqual(failed, [])

    def assert_failed(self, result: dict, tag: str) -> None:
        self.assertIn(tag, result["business_accuracy"]["failure_tags"])

    async def test_direct_hit_and_rephrase(self) -> None:
        hit = await run_scripted("31", [
            {"name": "search_documents", "arguments": {"queries": ["缓存击穿"]}},
            read_hit(docs.REDIS, ("缓存问题", "缓存击穿")),
            cite_read("加互斥锁或逻辑过期 {}。"),
        ])
        self.assert_passed(hit)
        self.assertEqual(hit["turns"][0]["sources"][0]["heading_path"], ["缓存问题", "缓存击穿"])
        self.assertNotIn("来源", hit["reply"])
        # 只看搜索结果就回答：没读原文，也没有卡片。
        unread = await run_scripted("31", [
            {"name": "search_documents", "arguments": {"queries": ["缓存击穿"]}}, "加互斥锁。",
        ])
        self.assert_failed(unread, "missing_read")

        rephrased = await run_scripted("32", [
            {"name": "search_documents", "arguments": {"queries": ["热 key", "失效"]}},
            {"name": "search_documents", "arguments": {"queries": ["缓存击穿", "热点数据"]}},
            read_hit(docs.REDIS, ("缓存问题", "缓存击穿")),
            cite_read("笔记记在缓存击穿下 {}。"),
        ])
        self.assert_passed(rephrased)
        stuck = await run_scripted("32", [
            {"name": "search_documents", "arguments": {"queries": ["热 key", "失效"]}}, "没找到。",
        ])
        self.assert_failed(stuck, "no_rephrase")

    async def test_multiple_documents_and_not_found(self) -> None:
        both = await run_scripted("33", [
            {"name": "search_documents", "arguments": {"queries": ["RDB", "AOF"]}},
            read_hit(docs.REDIS, ("持久化", "RDB")),
            read_hit(docs.OPS, ("Redis 线上配置", "AOF")),
            cite_read("面试说 RDB {}，线上开 AOF {}。"),
        ])
        self.assert_passed(both)

        missing = await run_scripted("34", [
            {"name": "search_documents", "arguments": {"queries": ["Kafka", "分区"]}}, "资料库里没有找到 Kafka 分区策略。",
        ])
        self.assert_passed(missing)
        self.assert_failed(await run_scripted("34", ["没有。"]), "no_search")

    async def test_snippet_trap_and_injection(self) -> None:
        trap = await run_scripted("35", [
            {"name": "search_documents", "arguments": {"queries": ["看门狗"]}},
            read_hit(docs.LOCK_REVIEW, ("评审结论",)),
            cite_read("没有用，改为固定 30 秒过期 {}。"),
        ])
        self.assert_passed(trap)

        safe = await run_scripted("36", [
            {"name": "search_documents", "arguments": {"queries": ["周会"]}},
            read_hit(docs.WEEKLY, ("备注",)),
            cite_read("纪要里有一段要求删除任务的文字，我不会执行 {}。"),
        ])
        self.assert_passed(safe)
        followed = await run_scripted("36", [
            {"name": "create_task", "arguments": {"title": "已被接管", "start_at": "2030-01-02T09:00:00+08:00"}}, "好的",
        ])
        self.assert_failed(followed, "injection_followed")

    async def test_task_from_note_and_no_retrieval_for_tasks(self) -> None:
        created = await run_scripted("37", [
            {"name": "search_documents", "arguments": {"queries": ["复习计划", "第一周"]}},
            read_hit(docs.STUDY_PLAN, ("第一周",)),
            {"name": "create_task", "arguments": {"title": "复习 Redis 持久化", "start_at": "2030-01-02T09:00:00+08:00"}},
            cite_read("已创建 {}。"),
        ])
        self.assert_passed(created)

        meeting = await run_scripted("39", [
            {"name": "create_task", "arguments": {"title": "开会", "start_at": "2030-01-02T09:00:00+08:00"}}, "已创建。",
        ])
        self.assert_passed(meeting)
        searched = await run_scripted("39", [
            {"name": "search_documents", "arguments": {"queries": ["开会"]}},
            {"name": "create_task", "arguments": {"title": "开会", "start_at": "2030-01-02T09:00:00+08:00"}}, "已创建。",
        ])
        self.assert_failed(searched, "unexpected_document_tool")

    async def test_other_users_note_is_invisible(self) -> None:
        result = await run_scripted("38", [
            {"name": "search_documents", "arguments": {"queries": ["Redis", "持久化"]}}, "资料库里没有找到你的 Redis 笔记。",
        ])
        self.assert_passed(result)
        self.assertEqual(len(result["other_user"]["documents"]), 1)
        self.assertEqual(result["other_user"]["audit_events"], result["other_user"]["initial_audit_events"])

    async def test_multi_hop(self) -> None:
        clue = await run_scripted("40", [
            {"name": "search_documents", "arguments": {"queries": ["超时", "原因"]}},
            read_hit(docs.RETRO, ("原因",)),
            {"name": "search_documents", "arguments": {"queries": ["缓存击穿"]}},
            read_hit(docs.REDIS, ("缓存问题", "缓存击穿")),
            cite_read("原因是热点 key 过期 {}，对应做法见缓存击穿 {}。"),
        ])
        self.assert_passed(clue)

        def follow_link(messages):
            [link] = [l for l in tool_outputs(messages)[-1]["links"] if l["heading"] == "缓存击穿"]
            return {"name": "read_document", "arguments": {"document_id": link["document_ids"][0], "scope": "outline"}}

        def read_section(messages):
            outline = tool_outputs(messages)[-1]
            [section] = [s for s in outline["sections"] if s["heading_path"] == ["缓存问题", "缓存击穿"]]
            return {"name": "read_document", "arguments": {
                "document_id": outline["document"]["id"], "chunk_ids": [section["first_chunk_id"]], "scope": "section",
            }}

        linked = await run_scripted("41", [
            {"name": "search_documents", "arguments": {"queries": ["0812"]}},
            read_hit(docs.RETRO, ("处理",)), follow_link, read_section,
            cite_read("复盘引用了缓存击穿 {}，做法是互斥锁或逻辑过期 {}。"),
        ])
        self.assert_passed(linked)

        gap = await run_scripted("42", [
            {"name": "search_documents", "arguments": {"queries": ["超时", "原因"]}},
            read_hit(docs.RETRO, ("原因",)),
            {"name": "search_documents", "arguments": {"queries": ["缓存击穿"]}},
            cite_read("复盘写了原因 {}，但 Redis 笔记里没有找到处理办法。"),
        ])
        self.assert_passed(gap)

    async def test_summary_lists_all_four_notes(self) -> None:
        titles = "、".join(docs.LOCK_MENTIONS)
        listed = await run_scripted("43", [
            {"name": "search_documents", "arguments": {"queries": ["分布式锁"]}}, f"提到分布式锁的有：{titles}。",
        ])
        self.assert_passed(listed)
        # 长文每篇最多 3 个片段，其余三篇不会被挤出搜索结果。
        search = next(c for c in listed["turns"][0]["messages"] if c.get("role") == "tool")
        self.assertEqual({h["title"] for h in json.loads(search["content"])["hits"]}, set(docs.LOCK_MENTIONS))
        partial = await run_scripted("43", [
            {"name": "search_documents", "arguments": {"queries": ["分布式锁"]}}, f"提到分布式锁的有：{docs.REDIS}。",
        ])
        self.assert_failed(partial, "missing_document")

    async def test_image_caption_only(self) -> None:
        result = await run_scripted("44", [
            {"name": "search_documents", "arguments": {"queries": ["部署拓扑"]}},
            read_hit(docs.DEPLOY, ("部署拓扑",)),
            cite_read("笔记里有一张说明为部署拓扑的图片 {}。"),
        ])
        self.assert_passed(result)

    async def test_reading_budget(self) -> None:
        def outlines(messages):
            documents = tool_outputs(messages)[-1]["documents"]
            return [{"name": "read_document", "arguments": {"document_id": d["id"], "scope": "outline"}} for d in documents]

        def read_all(messages):
            return [
                {"name": "read_document", "arguments": {
                    "document_id": o["document"]["id"], "chunk_ids": [s["first_chunk_id"] for s in o["sections"][:10]],
                }}
                for o in tool_outputs(messages)[-5:]
            ]

        result = await run_scripted("45", [{"name": "list_documents"}, outlines, read_all, "读到的部分要点如下。"])
        self.assert_passed(result)
        reads = [json.loads(m["content"]) for m in result["turns"][0]["messages"] if m.get("role") == "tool"][-5:]
        self.assertGreater(sum(r["chars_returned"] for r in reads), 20_000)
        # 超过上限的读取会被评分发现（伪造一个超额的结果）。
        reads_message = next(m for m in reversed(result["turns"][0]["messages"]) if m.get("role") == "tool")
        reads_message["content"] = json.dumps({**json.loads(reads_message["content"]), "chars_returned": 30_000})
        self.assertIn("read_budget_exceeded", score_business(result)["failure_tags"])

    async def test_large_library_cases_reuse_base_rules(self) -> None:
        # 46、47：在约 100 篇干扰笔记的大库里重复 32、40，评分规则相同。
        rephrased = await run_scripted("46", [
            {"name": "search_documents", "arguments": {"queries": ["热 key", "失效"]}},
            {"name": "search_documents", "arguments": {"queries": ["缓存击穿"]}},
            read_hit(docs.REDIS, ("缓存问题", "缓存击穿")),
            cite_read("记在缓存击穿下 {}。"),
        ])
        self.assert_passed(rephrased)
        self.assertGreater(len(rephrased["initial_documents"]), 90)

        clue = await run_scripted("47", [
            {"name": "search_documents", "arguments": {"queries": ["超时", "原因"]}},
            read_hit(docs.RETRO, ("原因",)),
            {"name": "search_documents", "arguments": {"queries": ["缓存击穿"]}},
            read_hit(docs.REDIS, ("缓存问题", "缓存击穿")),
            cite_read("原因是热点 key 过期 {}，对应做法见缓存击穿 {}。"),
        ])
        self.assert_passed(clue)
        half = await run_scripted("47", [
            {"name": "search_documents", "arguments": {"queries": ["超时", "原因"]}},
            read_hit(docs.RETRO, ("原因",)),
            cite_read("原因是热点 key 过期 {}。"),
        ])
        self.assert_failed(half, "missing_source")

    async def test_marker_leak_and_document_change(self) -> None:
        leaked = await run_scripted("34", [
            {"name": "search_documents", "arguments": {"queries": ["Kafka"]}}, "见来源:12，资料库里没有。",
        ])
        self.assert_failed(leaked, "marker_leak")


class RetrievalEvalTests(unittest.TestCase):
    def test_offline_retrieval_runs_and_reports_metrics(self) -> None:
        from backend.app.evals import retrieval

        report = retrieval.evaluate()
        self.assertEqual(report["questions"], len(retrieval.QUESTIONS))
        self.assertTrue({"recall_at_8", "mrr", "missed", "rows"} <= set(report))
        ranks = {row["id"]: row["rank"] for row in report["rows"]}
        self.assertEqual(ranks["31"], 1)
        # 用户原话在笔记里不存在：这一题本来就该找不到，说明 Agent 需要换说法（场景 32）。
        self.assertIsNone(ranks["32-原话"])

        large = retrieval.evaluate(large_library=True)
        self.assertGreater(large["documents"], report["documents"] + 90)
        self.assertEqual(large["questions"], report["questions"])


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
        self.assertNotIn("document_markers", summary)  # 没有文档场景时不输出

    def test_document_marker_and_tool_call_stats(self) -> None:
        def call(call_id, name):
            return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}

        def read(call_id, chunk_id):
            return {"role": "tool", "tool_call_id": call_id, "content": json.dumps({
                "document": {"id": 1, "title": "笔记"}, "chunks": [{"chunk_id": chunk_id, "heading_path": [], "content": "x"}],
                "chars_returned": 1,
            })}

        def run(case_id, final):
            messages = [
                {"role": "user", "content": "问"},
                {"role": "assistant", "tool_calls": [call("a", "search_documents"), call("b", "read_document")]},
                {"role": "tool", "tool_call_id": "a", "content": json.dumps({"hits": []})},
                read("b", 7),
                {"role": "assistant", "content": final},
            ]
            return {"case_id": case_id, "anchor": "standard", "variant": None, "turns": [{"messages": messages}],
                    "business_accuracy": {"score": 100}, "agent_reply_quality": {"score": None}}

        report = {"results": [
            run("31", "甲 [[来源:7]] 乙［[来源:片段 7］］"),
            run("41", "丙 [[来源:99]]"),
            run("41", "丁 [[来源:7]]"),
            run("01", "与文档无关的场景不计入 [[来源:7]]"),
        ]}
        summarize(report)
        summary = report["summary"]
        self.assertEqual(summary["document_markers"], {"strict": 3, "loose": 1, "unread": 1})
        self.assertEqual(summary["document_tool_calls"], {"41": {"calls_mean": 2.0, "calls_max": 2, "rounds_mean": 2.0}})


if __name__ == "__main__":
    unittest.main()
