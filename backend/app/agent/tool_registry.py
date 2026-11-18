"""Agent 可调用的任务工具；API 路由不会自动成为 Agent 工具。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy.orm import Session

from backend.app.config.settings import USER_TIMEZONE
from backend.app.schemas.action import PendingAction
from backend.app.schemas.task import TaskCreate, TaskUpdate
from backend.app.services import action_service, task_service


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskIdArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(
        gt=0,
        description="从当前对话已明确的目标或查询结果确定的任务 ID；不得猜测",
    )


class ActionIdArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: int = Field(
        gt=0,
        description="从当前对话已明确的提案确定的提案 ID；不得猜测",
    )


class UpdateProposalArguments(TaskIdArguments):
    changes: TaskUpdate = Field(
        description="要修改的任务字段：标题、优先级、开始时间、结束时间、时长、截止时间或状态"
    )


def list_tasks(_: EmptyArguments, session: Session):
    return task_service.list_tasks(session)


def get_task(data: TaskIdArguments, session: Session):
    return task_service.get_task(data.task_id, session)


def get_action(data: ActionIdArguments, session: Session):
    return action_service.get_action(session, data.action_id)


def propose_update(data: UpdateProposalArguments, session: Session):
    return action_service.propose_update(session, data.task_id, data.changes)


def propose_delete(data: TaskIdArguments, session: Session):
    return action_service.propose_delete(session, data.task_id)


def format_time(value: datetime | None) -> str:
    if value is None:
        return "无"
    return value.astimezone(USER_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def format_proposal(result_json: str) -> str:
    """只使用服务端保存的提案快照生成给用户看的操作内容。"""
    action = PendingAction.model_validate_json(result_json)
    header = (
        f"提案 #{action.id}（任务 #{action.task_id}，待确认，"
        f"{format_time(action.expires_at)} 前有效）"
    )

    if action.action_type == "delete":
        return (
            f"{header}\n拟删除任务「{action.before.title}」"
            f"（开始时间：{format_time(action.before.start_at)}）。\n"
            "任务尚未删除，请通过确认入口决定是否执行。"
        )

    if action.after is None:
        raise ValueError("修改提案缺少新快照")

    labels = {
        "title": "标题",
        "priority": "优先级",
        "start_at": "开始时间",
        "end_at": "结束时间",
        "duration_minutes": "时长",
        "due_at": "截止时间",
        "status": "状态",
    }
    changes = []
    for field, label in labels.items():
        before = getattr(action.before, field)
        after = getattr(action.after, field)
        if before != after:
            if field in ("start_at", "end_at", "due_at"):
                before, after = format_time(before), format_time(after)
            elif field == "duration_minutes":
                before = f"{before} 分钟" if before is not None else "无"
                after = f"{after} 分钟" if after is not None else "无"
            changes.append(f"{label}：{before or '无'} → {after or '无'}")

    return (
        f"{header}\n" + "\n".join(changes) +
        "\n任务尚未修改，请通过确认入口决定是否执行。"
    )


class ToolRegistry:
    def __init__(self):
        # 所有 handler 都接受 (已校验参数, Session)，终止回复由工具自己的配置提供。
        self.tools = {
            "create_task": {
                "description": "创建待办任务。至少提供开始时间或截止时间之一。截止时间是任务期限；开始时间和可选时长是独立的日程安排，未指定时长时保持为空。只有开始时间加时长形成的时间段参与冲突检查；无需用户说明时区。",
                "schema": TaskCreate,
                "handler": task_service.create_task,
            },
            "list_tasks": {
                "description": "列出所有待办，供回答查询或定位修改、删除的目标任务。无参数。",
                "schema": EmptyArguments,
                "handler": list_tasks,
            },
            "get_task": {
                "description": "根据对话中已明确的任务 ID 查询数据库中的最新详情；目标不明确时先 list_tasks，不要猜 ID。",
                "schema": TaskIdArguments,
                "handler": get_task,
            },
            "get_action": {
                "description": "根据对话中已明确的提案 ID 只读查询提案的最新状态、操作前后快照和有效期；不能确认或取消提案，不要猜 ID。",
                "schema": ActionIdArguments,
                "handler": get_action,
            },
            "propose_update": {
                "description": "为已定位的任务创建修改提案，不立即修改。changes 可修改标题、优先级、开始时间、结束时间、时长、截止时间和状态；时间字段必须相互一致，确认时再次检查冲突。需要用户通过独立确认入口执行。",
                "schema": UpdateProposalArguments,
                "handler": propose_update,
                "terminal_reply": format_proposal,
            },
            "propose_delete": {
                "description": "为已定位的任务创建删除提案，不立即删除；需要用户通过独立确认入口执行。",
                "schema": TaskIdArguments,
                "handler": propose_delete,
                "terminal_reply": format_proposal,
            },
        }

    def get_llm_tools(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": entry["description"],
                    "parameters": entry["schema"].model_json_schema(),
                },
            }
            for name, entry in self.tools.items()
        ]

    def is_terminal(self, name: str) -> bool:
        return "terminal_reply" in self.tools.get(name, {})

    def terminal_reply(self, name: str, result_json: str) -> str:
        return self.tools[name]["terminal_reply"](result_json)

    def execute_tool(self, name: str, parameters: str, session: Session) -> str:
        entry = self.tools.get(name)
        if entry is None:
            raise ValueError("工具不存在")

        data = entry["schema"].model_validate_json(parameters)
        result = entry["handler"](data, session)
        return TypeAdapter(Any).dump_json(result).decode("utf-8")
