
import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI

from backend.app.api import tasks, actions, chat, audit
from backend.app.db.base import Base
from backend.app.db.session import engine
from backend.app.db.session import SessionLocal
from backend.app.services import action_service

# 确保 ORM 模型都已注册
from backend.app.models.task import TaskORM
from backend.app.models.action import PendingActionORM
from backend.app.models.conversation import ConversationORM, ConversationMessageORM
from backend.app.models.request import ChatTurnORM, DeletedChatRequestORM, TaskCreateRequestORM
from backend.app.models.operation_event import OperationEventORM


logger = logging.getLogger(__name__)


def expire_due_proposals() -> int:
    with SessionLocal() as session:
        return action_service.purge_expired_actions(session)


async def proposal_expiry_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(expire_due_proposals)
        except Exception:
            logger.exception("提案过期清理失败")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):

    # 本地学习阶段自动创建缺失的表
    Base.metadata.create_all(bind=engine)
    expiry_task = asyncio.create_task(proposal_expiry_loop())
    try:
        yield
    finally:
        expiry_task.cancel()
        with suppress(asyncio.CancelledError):
            await expiry_task


app = FastAPI(
    title="留痕 API",
    version="0.3.0",
    lifespan=lifespan
)

# 注册路由模块
app.include_router(tasks.router)
app.include_router(actions.router)
app.include_router(chat.router)
app.include_router(audit.router)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000
    )
