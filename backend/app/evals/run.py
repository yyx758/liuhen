"""Run Agent scenarios with the configured real model and isolated SQLite files."""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import URL, create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.config.env import get_judge_config, get_model_config
from backend.app.config.settings import USER_TIMEZONE
from backend.app.db.base import Base
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


def local_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(USER_TIMEZONE)
    if value.utcoffset() is None:
        raise ValueError("测评时间必须包含时区")
    return value.astimezone(USER_TIMEZONE)


def collect_state(session_factory, turns: list[dict]) -> dict:
    with session_factory() as session:
        for entry in turns:
            stored = session.get(ChatTurnORM, entry["request_id"])
            if stored is not None:
                entry["status"] = stored.status
                entry["messages"] = stored.messages
                if entry.get("reply") is None:
                    entry["reply"] = stored.answer

        tasks = [
            to_task_read(task).model_dump(mode="json")
            for task in session.scalars(select(TaskORM).order_by(TaskORM.id))
        ]
        actions = [
            to_action_read(action).model_dump(mode="json")
            for action in session.scalars(
                select(PendingActionORM).order_by(PendingActionORM.id)
            )
        ]
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
                select(OperationEventORM).order_by(OperationEventORM.id)
            )
        ]
        return {"tasks": tasks, "actions": actions, "audit_events": audit_events}


async def run_case(
    case_id: str, evaluation_now: datetime | None = None,
    *, race_preference: str | None = None,
) -> dict:
    """Execute one scenario and retain evidence even when its workflow fails."""
    if case_id not in CASE_MESSAGES:
        raise ValueError(f"未知测评用例：{case_id}")
    if race_preference not in {None, "confirm", "cancel"}:
        raise ValueError("并发优先方向只能是 confirm 或 cancel")
    run_now = local_now(evaluation_now)

    with TemporaryDirectory() as directory:
        database_path = Path(directory) / "scenario.db"
        engine = create_engine(URL.create(
            "sqlite+pysqlite", database=str(database_path),
        ))
        try:
            Base.metadata.create_all(engine)
            session_factory = sessionmaker(bind=engine)
            context = ScenarioContext(session_factory, run_now, race_preference)
            initial_tasks = []
            run_error = None
            try:
                seed_case(case_id, context)
                initial_tasks = collect_state(session_factory, [])["tasks"]
                await execute_case(case_id, context)
            except Exception as exc:
                run_error = f"{type(exc).__name__}: {exc}"

            final_state = collect_state(session_factory, context.turns)
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
                "clock_mode": "fixed_evaluation",
                "model": get_model_config()["model"],
                "initial_tasks": initial_tasks,
                "turns": context.turns,
                "external_events": context.external_events,
                "tasks": final_state["tasks"],
                "actions": final_state["actions"],
                "audit_events": final_state["audit_events"],
                "run_error": run_error,
                # Keep the single-turn fields convenient for the first case.
                "request_id": last_turn.get("request_id"),
                "conversation_id": context.conversation_id,
                "chat_turn_status": last_turn.get("status"),
                "reply": last_turn.get("reply"),
                "messages": last_turn.get("messages", []),
            }
        finally:
            engine.dispose()


async def run_case_01(evaluation_now: datetime | None = None) -> dict:
    return await run_case("01", evaluation_now)


def summarize(report: dict) -> None:
    results = report["results"]
    business = [r["business_accuracy"]["score"] for r in results if "business_accuracy" in r]
    replies = [
        r["agent_reply_quality"]["score"] for r in results
        if r.get("agent_reply_quality", {}).get("score") is not None
    ]
    report["summary"] = {
        "runs": len(results),
        "business_accuracy_mean": round(sum(business) / len(business), 2) if business else None,
        "agent_reply_quality_mean": round(sum(replies) / len(replies), 2) if replies else None,
        "judge_error_count": sum(bool(r.get("agent_reply_quality", {}).get("judge_error")) for r in results),
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
    case_ids: list[str], trials: int = 1, evaluation_now: datetime | None = None,
    *, output_path: Path | None = None,
) -> dict:
    if trials < 1:
        raise ValueError("运行次数必须至少为 1")
    run_now = local_now(evaluation_now)
    report = {
        "evaluation_now": run_now.isoformat(timespec="seconds"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": get_model_config()["model"],
        "judge_model": get_judge_config()["model"],
        "results": [],
    }
    for case_id in case_ids:
        for trial in range(1, trials + 1):
            variants = ("confirm", "cancel", None) if case_id == "14" else (None,)
            for variant in variants:
                result = await run_case(case_id, run_now, race_preference=variant)
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
    selection.add_argument("--all", action="store_true", help="运行全部 21 条场景")
    selection.add_argument(
        "--grade-report", type=Path,
        help="对已有运行报告评分，不重新调用 Agent",
    )
    parser.add_argument("--trials", type=int, default=1, help="每条场景运行次数")
    parser.add_argument(
        "--now", type=datetime.fromisoformat,
        help="固定测评时钟，例如 2026-11-17T10:00:00+08:00",
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
    report = asyncio.run(run_suite(
        case_ids, args.trials, args.now, output_path=output_path,
    ))
    print(f"测评完成：{output_path}\n{json.dumps(report['summary'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
