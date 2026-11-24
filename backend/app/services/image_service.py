"""文档图片的上传、读取与清理（spec 2.6，PRD D21–D24）。"""

import hashlib
import re
import secrets
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.config.settings import IMAGE_MAX_BYTES, IMAGE_MAX_TOTAL_BYTES_PER_USER, IMAGE_ORPHAN_GRACE
from backend.app.models.document import DocumentImageORM
from backend.app.repositories import document_repository
from backend.app.schemas.document import ImageRead

IMAGE_URL_PREFIX = "/documents/images/"
_LOCK_USER_IMAGES = 3  # 与 document_service 的锁用途编号区分
_PUBLIC_ID = re.compile(r"^[0-9a-f]{32}$")


def sniff_content_type(data: bytes) -> str | None:
    """按文件头判断格式，不信任扩展名和请求里的 Content-Type。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _clean_name(name: str | None) -> str | None:
    if name is None:
        return None
    # 只保留最后一段文件名，Windows 与类 Unix 的路径分隔符都去掉。
    name = re.split(r"[\\/]", name)[-1].strip()
    return name[:255] or None


def _to_read(image: DocumentImageORM, *, reused: bool) -> ImageRead:
    return ImageRead(
        id=image.public_id,
        url=IMAGE_URL_PREFIX + image.public_id,
        content_type=image.content_type,
        byte_size=image.byte_size,
        original_name=image.original_name,
        reused=reused,
    )


def upload_image(session: Session, data: bytes, original_name: str | None, *, user_id: int) -> ImageRead:
    if not data:
        raise HTTPException(422, "图片内容为空")
    if len(data) > IMAGE_MAX_BYTES:
        raise HTTPException(413, f"图片不能超过 {IMAGE_MAX_BYTES // (1024 * 1024)} MB")
    content_type = sniff_content_type(data)
    if content_type is None:
        raise HTTPException(415, "只支持 PNG、JPEG、GIF、WebP 图片")
    sha256 = hashlib.sha256(data).hexdigest()

    with session.begin():
        # 同一用户的上传串行执行：复用判断和总量检查不会被并发上传绕过。
        session.execute(
            text("SELECT pg_advisory_xact_lock(:kind, :user_id)"),
            {"kind": _LOCK_USER_IMAGES, "user_id": user_id},
        )
        existing = document_repository.get_image_by_sha256(session, sha256, user_id=user_id)
        if existing is not None:
            return _to_read(existing, reused=True)
        used = document_repository.total_image_bytes(session, user_id=user_id)
        if used + len(data) > IMAGE_MAX_TOTAL_BYTES_PER_USER:
            raise HTTPException(
                409,
                f"图片总量已达 {IMAGE_MAX_TOTAL_BYTES_PER_USER // (1024 * 1024)} MB 上限；"
                "不再被笔记引用的图片会在一天后自动清理",
            )
        image = document_repository.insert_image(session, DocumentImageORM(
            public_id=secrets.token_hex(16),
            user_id=user_id,
            content_type=content_type,
            byte_size=len(data),
            sha256=sha256,
            data=data,
            original_name=_clean_name(original_name),
            created_at=now_utc(),
        ))
        return _to_read(image, reused=False)


def get_image(session: Session, public_id: str, *, user_id: int) -> tuple[bytes, str]:
    """返回 (图片内容, Content-Type)。别人的图片与不存在一样返回 404。"""
    image = None
    if _PUBLIC_ID.match(public_id):
        image = document_repository.get_image(session, public_id, user_id=user_id)
    if image is None:
        raise HTTPException(404, "图片不存在")
    return image.data, image.content_type


def cleanup_unreferenced_images(session: Session, *, now: datetime | None = None) -> int:
    """删除创建超过 IMAGE_ORPHAN_GRACE、且本人没有任何文档引用的图片，返回删除数量。"""
    with session.begin():
        return document_repository.delete_unreferenced_images(
            session, created_before=(now or now_utc()) - IMAGE_ORPHAN_GRACE
        )
