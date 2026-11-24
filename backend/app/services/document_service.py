"""文档的增删改查与互链解析（spec 2.3、2.5）。

服务层负责校验后的业务规则、事务和审计；数据库读写在 document_repository。
"""

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.config.settings import DOCUMENT_MAX_PER_USER, USER_TIMEZONE
from backend.app.models.document import DocumentORM
from backend.app.models.request import DocumentCreateRequestORM
from backend.app.repositories import document_repository
from backend.app.schemas.document import (
    DocumentCategory,
    DocumentCreate,
    DocumentLinkRead,
    DocumentListPage,
    DocumentRead,
    DocumentSummary,
    DocumentUpdate,
    OutlineItem,
)
from backend.app.services import audit_service
from backend.app.services.document_chunker import split_markdown
from backend.app.services.document_markdown import DocLink, extract_doc_links, to_search_text

NOT_FOUND = "文档不存在"
STALE_VERSION = "文档已在别处修改，请刷新后再改"
EXCERPT_CHARS = 120

# advisory lock 的第一个参数区分用途，避免不同业务的锁互相等待。
_LOCK_CREATE_KEY = 1  # 与任务创建共用"幂等键"这一类锁：键是全局唯一的 UUID，不会冲突
_LOCK_USER_DOCUMENTS = 2

_LINE_MARKERS = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?)+", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _local(value: datetime) -> datetime:
    return value.astimezone(USER_TIMEZONE)


def excerpt(content: str) -> str:
    """列表卡片上的摘要：可读化、去掉标题和引用标记、压缩空白，取前 120 字。"""
    plain = _WHITESPACE.sub(" ", _LINE_MARKERS.sub("", to_search_text(content))).strip()
    return plain if len(plain) <= EXCERPT_CHARS else plain[:EXCERPT_CHARS] + "…"


def _meta(document: DocumentORM) -> dict:
    """审计与幂等回放用的元数据快照，不含正文（PRD D14）。"""
    return {
        "title": document.title,
        "category": document.category,
        "content_chars": len(document.content),
        "content_hash": document.content_hash,
        "version": document.version,
    }


def to_summary(document: DocumentORM) -> DocumentSummary:
    return DocumentSummary(
        id=document.id,
        title=document.title,
        category=document.category,
        excerpt=excerpt(document.content),
        content_chars=len(document.content),
        version=document.version,
        created_at=_local(document.created_at),
        updated_at=_local(document.updated_at),
    )


def _to_read(session: Session, document: DocumentORM) -> DocumentRead:
    outline = document_repository.get_outline(session, document.id, user_id=document.user_id)
    return DocumentRead(
        **to_summary(document).model_dump(),
        content=document.content,
        outline=[OutlineItem(section_seq=s.section_seq, heading_path=s.heading_path, chars=s.chars) for s in outline],
        links=resolve_links(session, extract_doc_links(document.content), user_id=document.user_id),
    )


# ---------- 查询 ----------

def get_document(session: Session, document_id: int, *, user_id: int) -> DocumentRead:
    document = document_repository.get_document(session, document_id, user_id=user_id)
    if document is None:
        raise HTTPException(404, NOT_FOUND)
    return _to_read(session, document)


def get_summaries(session: Session, document_ids: list[int], *, user_id: int) -> dict[int, DocumentSummary]:
    """批量取文档摘要（页面搜索按文档分组时用），只返回本人的文档。"""
    documents = document_repository.get_documents(session, document_ids, user_id=user_id)
    return {document.id: to_summary(document) for document in documents}


def list_documents(
    session: Session, *, user_id: int, category: str | None = None,
    before_id: int | None = None, limit: int = 30,
) -> DocumentListPage:
    before = None
    if before_id is not None:
        anchor = document_repository.get_document(session, before_id, user_id=user_id)
        if anchor is None:
            raise HTTPException(422, "分页位置已失效，请刷新")
        before = (anchor.updated_at, anchor.id)
    # 多取一条，用来判断是否还有下一页。
    rows = document_repository.list_documents(
        session, user_id=user_id, category=category, before=before, limit=limit + 1
    )
    items = [to_summary(document) for document in rows[:limit]]
    has_more = len(rows) > limit
    total = document_repository.count_documents(session, user_id=user_id, category=category)
    return DocumentListPage(items=items, next_before_id=items[-1].id if has_more else None, total=total)


def list_categories(session: Session, *, user_id: int) -> list[DocumentCategory]:
    return [
        DocumentCategory(name=name, count=count)
        for name, count in document_repository.list_categories(session, user_id=user_id)
    ]


def resolve_links(session: Session, links: list[DocLink], *, user_id: int) -> list[DocumentLinkRead]:
    """按标题把互链解析到本人的文档；同名多篇按最近更新排序，找不到时 document_ids 为空。"""
    found = document_repository.find_documents_by_titles(session, [link.title for link in links], user_id=user_id)
    ids_by_title: dict[str, list[int]] = defaultdict(list)
    for document in found:  # 仓储已按最近更新排序
        ids_by_title[document.title.strip()].append(document.id)
    return [
        DocumentLinkRead(text=link.text, title=link.title, heading=link.heading, document_ids=ids_by_title.get(link.title, []))
        for link in links
    ]


# ---------- 新建 ----------

def create_document(
    session: Session, data: DocumentCreate, request_id: str | None = None, *, user_id: int
) -> DocumentSummary:
    with session.begin():
        payload_hash = None
        if request_id is not None:
            session.execute(
                text("SELECT pg_advisory_xact_lock(:kind, hashtext(:key))"),
                {"kind": _LOCK_CREATE_KEY, "key": request_id},
            )
            payload_hash = _hash(json.dumps(data.model_dump(mode="json"), sort_keys=True, ensure_ascii=False))
            previous = session.get(DocumentCreateRequestORM, request_id)
            if previous is not None:
                # 幂等键全局唯一：别人用过的键不能回放出别人的文档信息。
                if previous.user_id != user_id:
                    raise HTTPException(409, "幂等键已被使用，请换一个新的幂等键")
                if previous.payload_hash != payload_hash:
                    raise HTTPException(409, "幂等键已用于不同的创建请求")
                return DocumentSummary.model_validate(previous.result_snapshot)

        # 同一用户的新建串行执行，并发时也不会超过篇数上限。
        session.execute(
            text("SELECT pg_advisory_xact_lock(:kind, :user_id)"),
            {"kind": _LOCK_USER_DOCUMENTS, "user_id": user_id},
        )
        if document_repository.count_documents(session, user_id=user_id) >= DOCUMENT_MAX_PER_USER:
            raise HTTPException(409, f"资料库已满 {DOCUMENT_MAX_PER_USER} 篇，请删除不用的笔记后再试")

        now = now_utc()
        document = document_repository.insert_document(session, DocumentORM(
            user_id=user_id,
            title=data.title,
            category=data.category,
            content=data.content,
            content_hash=_hash(data.content),
            version=1,
            created_at=now,
            updated_at=now,
        ))
        document_repository.replace_chunks(session, document, split_markdown(document.content))
        audit_service.record_document_event(
            session, "document_created", document.id, user_id=user_id,
            details={"document": _meta(document)}, request_id=request_id,
        )
        summary = to_summary(document)
        if request_id is not None:
            session.add(DocumentCreateRequestORM(
                request_id=request_id,
                user_id=user_id,
                payload_hash=payload_hash,
                result_snapshot=summary.model_dump(mode="json"),
                created_at=int(now.timestamp()),
            ))
    return summary


# ---------- 修改与删除 ----------

def update_document(
    session: Session, document_id: int, data: DocumentUpdate, expected_version: int, *, user_id: int
) -> DocumentRead:
    with session.begin():
        document = document_repository.get_document(session, document_id, user_id=user_id, for_update=True)
        if document is None:
            raise HTTPException(404, NOT_FOUND)
        if document.version != expected_version:
            raise HTTPException(409, STALE_VERSION)

        before = _meta(document)
        provided = data.model_dump(include=data.model_fields_set)
        changed = [
            field for field in ("title", "category", "content")
            if field in provided and provided[field] != getattr(document, field)
        ]
        if not changed:
            raise HTTPException(409, "修改前后没有变化")

        values = {field: provided[field] for field in changed}
        values["updated_at"] = now_utc()
        if "content" in values:
            values["content_hash"] = _hash(values["content"])
        if document_repository.update_if_version(
            session, document_id, expected_version, values, user_id=user_id
        ) != 1:
            raise HTTPException(409, STALE_VERSION)

        session.refresh(document)
        # 标题也在 search_text 里，改标题同样要重建片段；只改分类时片段不变。
        if "title" in changed or "content" in changed:
            document_repository.replace_chunks(session, document, split_markdown(document.content))
        audit_service.record_document_event(
            session, "document_updated", document_id, user_id=user_id,
            details={"before": before, "after": _meta(document), "changed": changed},
        )
        result = _to_read(session, document)
    return result


def delete_document(session: Session, document_id: int, expected_version: int, *, user_id: int) -> None:
    with session.begin():
        document = document_repository.get_document(session, document_id, user_id=user_id, for_update=True)
        if document is None:
            raise HTTPException(404, NOT_FOUND)
        if document.version != expected_version:
            raise HTTPException(409, STALE_VERSION)
        before = _meta(document)
        if document_repository.delete_if_version(session, document_id, expected_version, user_id=user_id) != 1:
            raise HTTPException(409, STALE_VERSION)
        # 片段由外键级联删除；图片不在这里删除，由清理任务处理（PRD D24）。
        audit_service.record_document_event(
            session, "document_deleted", document_id, user_id=user_id, details={"before": before},
        )
