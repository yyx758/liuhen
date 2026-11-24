
import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI

from backend.app.api import admin, auth, tasks, actions, chat, audit, documents, notifications
from backend.app.api.security import check_origin
from backend.app.db.migrations import ensure_schema_current
from backend.app.db.session import engine
from backend.app.db.session import SessionLocal
from backend.app.config.settings import IMAGE_CLEANUP_INTERVAL_SECONDS
from backend.app.services import action_service, auth_service, image_service, reminder_service


logger = logging.getLogger(__name__)


def expire_due_proposals() -> int:
    with SessionLocal() as session:
        return action_service.purge_expired_actions(session)


def purge_expired_sessions() -> int:
    with SessionLocal() as session:
        return auth_service.purge_expired_sessions(session)


async def expiry_loop() -> None:
    while True:
        for job, label in ((expire_due_proposals, "提案过期清理"), (purge_expired_sessions, "登录会话过期清理")):
            try:
                await asyncio.to_thread(job)
            except Exception:
                logger.exception("%s失败", label)
        await asyncio.sleep(60)


def fire_reminders() -> int:
    with SessionLocal() as session:
        return reminder_service.fire_due_reminders(session)


def send_reminder_emails() -> int:
    with SessionLocal() as session:
        return reminder_service.send_pending_emails(session)


async def reminder_loop() -> None:
    """每 30 秒触发到点的提醒并发送提醒邮件（PRD R1、R2）。状态都在数据库里，重启后接着处理。"""
    while True:
        for job, label in ((fire_reminders, "提醒触发"), (send_reminder_emails, "提醒邮件发送")):
            try:
                # 同步的数据库与 SMTP 调用放到线程池，不阻塞事件循环。
                await asyncio.to_thread(job)
            except Exception:
                logger.exception("%s失败", label)
        await asyncio.sleep(30)


def cleanup_images() -> int:
    with SessionLocal() as session:
        return image_service.cleanup_unreferenced_images(session)


async def image_cleanup_loop() -> None:
    """每小时删除超过 24 小时、且没有任何本人文档引用的图片（PRD D24）。"""
    while True:
        try:
            await asyncio.to_thread(cleanup_images)
        except Exception:
            logger.exception("图片清理失败")
        await asyncio.sleep(IMAGE_CLEANUP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):

    # 表结构由 Alembic 迁移维护；版本不是最新时拒绝启动，避免新代码跑在旧表上。
    ensure_schema_current(engine)
    expiry_task = asyncio.create_task(expiry_loop())
    reminder_task = asyncio.create_task(reminder_loop())
    image_cleanup_task = asyncio.create_task(image_cleanup_loop())
    try:
        yield
    finally:
        for task in (expiry_task, reminder_task, image_cleanup_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="留痕 API",
    version="0.3.0",
    lifespan=lifespan
)

app.middleware("http")(check_origin)

# 注册路由模块
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(tasks.router)
app.include_router(actions.router)
app.include_router(chat.router)
app.include_router(audit.router)
app.include_router(documents.router)
app.include_router(notifications.router)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000
    )
