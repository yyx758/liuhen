from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.app.config.settings import USER_TIMEZONE, USER_TIMEZONE_NAME

#客户端创建时需要提供哪些数据
class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")#禁止出现未定义的字段
    #代办的名称
    title:str = Field(min_length=1,max_length=100)

    #优先级,Literal类似枚举
    priority:Literal["low","medium","high"] = "medium"

    # 截止时间是任务期限；开始时间和时长是独立的日程安排
    start_at: datetime | None = Field(default=None, description=f"可选的任务开始时间；不带时区时按用户本地时间 {USER_TIMEZONE_NAME} 理解")
    duration_minutes: int | None = Field(default=None, gt=0, description="可选的任务占用分钟数；未指定时为空")

    # 截止时间不参与结束时间计算或日程冲突判断
    due_at: datetime | None = Field(default=None, description="可选的任务截止时间；与开始时间和时长独立")

    @field_validator("start_at", "due_at")
    @classmethod
    def normalize_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            return value.replace(tzinfo=USER_TIMEZONE)
        return value

    @model_validator(mode="after")
    def validate_schedule(self):
        if self.start_at is None and self.due_at is None:
            raise ValueError("开始时间和截止时间至少提供一个")
        if self.duration_minutes is not None and self.start_at is None:
            raise ValueError("提供任务时长时必须同时提供开始时间")
        return self

    #自定义方法校验
    @field_validator("title")
    @classmethod
    def validate_title(cls,value:str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("任务标题不能为空")

        return value


#服务端响应时需要返回哪些数据
class TaskRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id:int
    title: str = Field(min_length=1, max_length=100)
    priority: Literal["low", "medium", "high"] = "medium"
    # 兼容升级前尚未排期的任务和旧操作提案快照
    start_at: datetime | None = None
    end_at: datetime | None = None
    duration_minutes: int | None = None
    due_at: datetime | None = None
    status:Literal["todo","done"] = "todo"
    version: int = 1


#修改task请求
class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=100
    )

    priority: Literal["low", "medium", "high"] | None = None

    start_at: datetime | None = Field(
        default=None,
        description=f"新的开始时间；不带时区时按 {USER_TIMEZONE_NAME} 理解"
    )
    end_at: datetime | None = Field(
        default=None,
        description=f"新的结束时间；不带时区时按 {USER_TIMEZONE_NAME} 理解"
    )
    duration_minutes: int | None = Field(default=None, gt=0, description="新的占用分钟数")
    due_at: datetime | None = None
    status: Literal["todo", "done"] | None = None

    @field_validator("start_at", "end_at", "due_at")
    @classmethod
    def normalize_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            return value.replace(tzinfo=USER_TIMEZONE)
        return value

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None

        value = value.strip()

        if not value:
            raise ValueError("任务标题不能为空")

        return value
