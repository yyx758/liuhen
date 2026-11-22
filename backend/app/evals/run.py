"""Run Agent scenarios with the configured real model and isolated PostgreSQL databases."""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.config.env import get_judge_config, get_model_config
from backend.app.config.settings import USER_TIMEZONE
from backend.app.db.isolated import isolated_engine
from backend.app.evals.grading import score_business
from backend.app.evals.judge import judge_reply
from backend.app.evals.scenarios import (
    CASE_MESSAGES, CASE_NAMES, ScenarioContext, execute_case, seed_case,
)
from backend.app.models.action import PendingActionORM
from backend.app.models.conversation import ConversationORM, ConversationMessageORM  # register tables
from backend.app.models.operation_event import OperationEventORM
from backend.app.models.request import ChatTurnORM, TaskCreateRequestORM  # register tables
from backend.app.models.task import TaskORM
from backend.app.services.action_service import to_action_read
from backend.app.services.task_service import to_task_read


RESULTS_DIR = Path(__file__).resolve().parents[2] / "evals" / "results"

# 固定的测评时间点，保证"明天""下周三"等表达在不同日期运行时含义不变、结果可比。
# 默认只跑 standard；--anchors all 依次跑全部，专门检查跨周、跨年的时间理解。
ANCHORS = {
    "standard": "2026-11-18T10:00:00+08:00",      # 周三上午
    "sunday_night": "2026-11-22T21:30:00+08:00",  # 周日晚上："明天"跨周、"下周三"只隔三天
    "year_end": "2026-12-31T16:00:00+08:00",      # 年末："明天""后天"跨年
}


def local_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(USER_TIMEZONE)
    if value.utcoffset() is None:
        raise ValueError("测评时间必须包含时区")
    return value.astimezone(USER_TIMEZONE)


def collect_state(session_factory, turns: list[dict], *, user_id: int, other_user_id: int) -> dict:
    """测评用户的任务、提案与审计；另一个用户的数据单独返回，用来断言它没有被读到或改动。"""
    with session_factory() as session:
        for entry in turns:
            stored = session.get(ChatTurnORM, entry["request_id"])
            if stored is not None:
                entry["status"] = stored.status
                entry["messages"] = stored.messages
                if entry.get("reply") is None:
                    entry["reply"] = stored.answer

        def tasks_of(owner: int) -> list[dict]:
            return [
                to_task_read(task).model_dump(mode="json")
                for task in session.scalars(select(TaskORM).where(TaskORM.user_id == owner).order_by(TaskORM.id))
            ]

        def actions_of(owner: int) -> list[dict]:
            return [
                to_action_read(action).model_dump(mode="json")
                for action in session.scalars(
                    select(PendingActionORM).where(PendingActionORM.user_id == owner).order_by(PendingActionORM.id)
                )
            ]

        tasks = tasks_of(user_id)
        actions = actions_of(user_id)
        audit_events = [
            {
                "id": event.id,
                "event_type": event.event_type,
                "task_id": event.task_id,
                "action_id": event.action_id,
                "request_id": event.request_id,
                "conversation_id": event.conversation_id,
                "tool_call_id": event.tool_call_id,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "reason": event.reason,
                "details": event.details,
                "created_at": datetime.fromtimestamp(
                    event.created_at, timezone.utc,
                ).isoformat(),
            }
            for event in session.scalars(
                select(OperationEventORM).where(OperationEventORM.user_id == user_id).order_by(OperationEventORM.id)
            )
        ]
        other_audit = session.scalars(
            select(OperationEventORM.event_type).where(OperationEventORM.user_id == other_user_id)
        ).all()
        return {
            "tasks": tasks, "actions": actions, "audit_events": audit_events,
            "other_user": {"tasks": tasks_of(other_user_id), "actions": actions_of(other_user_id), "audit_events": list(other_audit)},
        }


async def run_case(
    case_id: str, evaluation_now: datetime | None = None,
    *, race_preference: str | None = None, anchor: str | None = None,
) -> dict:
    """Execute one scenario and retain evidence even when its workflow fails."""
    if case_id not in CASE_MESSAGES:
        raise ValueError(f"未知测评用例：{case_id}")
    if race_preference not in {None, "confirm", "cancel"}:
        raise ValueError("并发优先方向只能是 confirm 或 cancel")
    run_now = local_now(evaluation_now)

    with isolated_engine() as engine:
        session_factory = sessionmaker(bind=engine)
        context = ScenarioContext(session_factory, run_now, race_preference)
        initial_tasks = []
        initial_other_tasks = []
        run_error = None
        owners = {"user_id": context.user_id, "other_user_id": context.other_user_id}
        try:
            seed_case(case_id, context)
            initial = collect_state(session_factory, [], **owners)
            initial_tasks = initial["tasks"]
            initial_other_tasks = initial["other_user"]["tasks"]
            await execute_case(case_id, context)
        except Exception as exc:
            run_error = f"{type(exc).__name__}: {exc}"

        final_state = collect_state(session_factory, context.turns, **owners)
        last_turn = context.turns[-1] if context.turns else {}
        return {
            "case_id": case_id,
            "case_name": CASE_NAMES[case_id],
            "variant": (
                f"{race_preference}_first" if race_preference
                else "simultaneous" if case_id == "14" else None
            ),
            "planned_user_messages": CASE_MESSAGES[case_id],
            "evaluation_now": run_now.isoformat(timespec="seconds"),
            "anchor": anchor,
            "model": get_model_config()["model"],
            "initial_tasks": initial_tasks,
            "turns": context.turns,
            "external_events": context.external_events,
            "tasks": final_state["tasks"],
            "actions": final_state["actions"],
            "audit_events": final_state["audit_events"],
            "other_user": {**final_state["other_user"], "initial_tasks": initial_other_tasks},
            "run_error": run_error,
            # Keep the single-turn fields convenient for the first case.
            "request_id": last_turn.get("request_id"),
            "conversation_id": context.conversation_id,
            "chat_turn_status": last_turn.get("status"),
            "reply": last_turn.get("reply"),
            "messages": last_turn.get("messages", []),
        }


def group_key(result: dict) -> str:
    """同一场景、变体、时间点的多次运行归为一组，用来看稳定性。"""
    anchor = result.get("anchor") or result.get("evaluation_now")
    return f"{result['case_id']}:{result.get('variant') or 'default'}@{anchor}"


def summarize(report: dict) -> None:
    results = [r for r in report["results"] if "business_accuracy" in r]
    business = [r["business_accuracy"]["score"] for r in results]
    replies = [
        r["agent_reply_quality"]["score"] for r in results
        if r.get("agent_reply_quality", {}).get("score") is not None
    ]
    # 平均分会掩盖不稳定（3 次里过 2 次也有 67 分），所以同时给出每组的通过次数，
    # 以及"每次运行都通过"的组占比（pass^k）。
    groups: dict[str, list[bool]] = {}
    for result in results:
        groups.setdefault(group_key(result), []).append(result["business_accuracy"]["score"] == 100)
    report["summary"] = {
        "runs": len(report["results"]),
        "business_accuracy_mean": round(sum(business) / len(business), 2) if business else None,
        "business_all_runs_pass_rate": (
            round(sum(all(passes) for passes in groups.values()) / len(groups), 4) if groups else None
        ),
        "agent_reply_quality_mean": round(sum(replies) / len(replies), 2) if replies else None,
        "judge_error_count": sum(bool(r.get("agent_reply_quality", {}).get("judge_error")) for r in results),
        "judge_skipped_count": sum(bool(r.get("agent_reply_quality", {}).get("skipped")) for r in results),
        "unstable_or_failing": {
            key: f"{sum(passes)}/{len(passes)}" for key, passes in groups.items() if not all(passes)
        },
    }


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(path.name + ".tmp")
    temporary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def next_report_path(label: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{label}.json"
    sequence = 2
    while path.exists():
        path = RESULTS_DIR / f"{label}-{sequence}.json"
        sequence += 1
    return path


async def grade_result(result: dict) -> None:
    result["business_accuracy"] = score_business(result)
    result["agent_reply_quality"] = await judge_reply(result)


async def grade_existing_report(report: dict, output_path: Path) -> dict:
    for result in report["results"]:
        await grade_result(result)
        summarize(report)
        write_report(output_path, report)
    return report


async def run_suite(
    case_ids: list[str], trials: int = 1, anchors: dict[str, datetime] | None = None,
    *, output_path: Path | None = None,
) -> dict:
    if trials < 1:
        raise ValueError("运行次数必须至少为 1")
    if not anchors:
        anchors = {"standard": datetime.fromisoformat(ANCHORS["standard"])}
    report = {
        "evaluation_nows": {label: local_now(value).isoformat(timespec="seconds") for label, value in anchors.items()},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": get_model_config()["model"],
        "judge_model": get_judge_config()["model"],
        "results": [],
    }
    for label, anchor_now in anchors.items():
        for case_id in case_ids:
            for trial in range(1, trials + 1):
                variants = ("confirm", "cancel", None) if case_id == "14" else (None,)
                for variant in variants:
                    result = await run_case(case_id, anchor_now, race_preference=variant, anchor=label)
                    result["trial"] = trial
                    await grade_result(result)
                    report["results"].append(result)
                    summarize(report)
                    if output_path is not None:
                        write_report(output_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="运行真实模型的隔离 Agent 测评场景")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case", choices=sorted(CASE_MESSAGES))
    selection.add_argument("--all", action="store_true", help=f"运行全部 {len(CASE_MESSAGES)} 条场景")
    selection.add_argument(
        "--grade-report", type=Path,
        help="对已有运行报告评分，不重新调用 Agent",
    )
    parser.add_argument("--trials", type=int, default=1, help="每条场景运行次数")
    timing = parser.add_mutually_exclusive_group()
    timing.add_argument(
        "--now", type=datetime.fromisoformat,
        help="自定义测评时钟，例如 2026-11-17T10:00:00+08:00；不填时使用固定时间点 standard",
    )
    timing.add_argument(
        "--anchors", choices=["standard", "all"], default="standard",
        help="all：依次在 " + "、".join(ANCHORS) + " 三个固定时间点运行",
    )
    parser.add_argument(
        "--output", type=Path,
        help="报告路径；默认保存到 backend/evals/results/",
    )
    args = parser.parse_args()

    if args.grade_report is not None:
        if not args.grade_report.is_file():
            parser.error(f"找不到运行报告：{args.grade_report}")
        report = json.loads(args.grade_report.read_text(encoding="utf-8"))
        case_ids = {item["case_id"] for item in report["results"]}
        label = next(iter(case_ids)) if len(case_ids) == 1 else "all"
        output_path = args.output or next_report_path(label)
        report["judge_model"] = get_judge_config()["model"]
        report["graded_at"] = datetime.now(timezone.utc).isoformat()
        report = asyncio.run(grade_existing_report(report, output_path))
        print(f"评分完成：{output_path}\n{json.dumps(report['summary'], ensure_ascii=False)}")
        return

    missing = [name for name, value in get_model_config().items() if not value]
    if missing:
        raise SystemExit(
            f"缺少模型环境配置：{', '.join(missing)}；"
            "请填写项目根目录的 .env"
        )
    case_ids = sorted(CASE_MESSAGES) if args.all else [args.case or "01"]
    report_name = "all" if args.all else case_ids[0]
    output_path = args.output or next_report_path(report_name)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if args.now is not None:
        anchors = {"custom": args.now}
    else:
        labels = ANCHORS if args.anchors == "all" else {"standard": ANCHORS["standard"]}
        anchors = {label: datetime.fromisoformat(value) for label, value in labels.items()}
    report = asyncio.run(run_suite(
        case_ids, args.trials, anchors, output_path=output_path,
    ))
    print(f"测评完成：{output_path}\n{json.dumps(report['summary'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
