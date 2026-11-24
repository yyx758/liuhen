"""文档、片段与图片的数据库读写（spec 1.9）。

user_id 一律是仅限关键字参数；按 ID 查找和列表查询都带 user_id 条件，查不到与无权访问一样返回空。
不提交事务，事务由服务层控制。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, exists, func, select, tuple_, update
from sqlalchemy.orm import Session, undefer

from backend.app.models.document import DocumentChunkORM, DocumentImageORM, DocumentORM
from backend.app.services.document_chunker import Chunk, build_search_text
from backend.app.services.document_search import like_pattern


@dataclass(frozen=True)
class OutlineSection:
    section_seq: int
    heading_path: list[str]
    first_chunk_id: int
    chars: int
    chunk_count: int


@dataclass(frozen=True)
class DocumentTitle:
    id: int
    title: str
    updated_at: datetime


# ---------- 文档 ----------

def insert_document(session: Session, document: DocumentORM) -> DocumentORM:
    session.add(document)
    session.flush()
    return document


def get_document(
    session: Session, document_id: int, *, user_id: int, for_update: bool = False
) -> DocumentORM | None:
    stmt = select(DocumentORM).where(DocumentORM.id == document_id, DocumentORM.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def get_documents(session: Session, document_ids: Iterable[int], *, user_id: int) -> list[DocumentORM]:
    ids = set(document_ids)
    if not ids:
        return []
    stmt = select(DocumentORM).where(DocumentORM.id.in_(ids), DocumentORM.user_id == user_id)
    return list(session.scalars(stmt).all())


def list_documents(
    session: Session,
    *,
    user_id: int,
    category: str | None = None,
    title_contains: str | None = None,
    before: tuple[datetime, int] | None = None,
    limit: int = 30,
) -> list[DocumentORM]:
    """按 (updated_at, id) 倒序的游标分页；before 是上一页最后一条的 (updated_at, id)。

    title_contains 忽略大小写、按字面匹配（%、_ 已转义）。
    """
    stmt = select(DocumentORM).where(DocumentORM.user_id == user_id)
    if category is not None:
        stmt = stmt.where(DocumentORM.category == category)
    if title_contains:
        stmt = stmt.where(DocumentORM.title.ilike(like_pattern(title_contains), escape="\\"))
    if before is not None:
        stmt = stmt.where(tuple_(DocumentORM.updated_at, DocumentORM.id) < tuple_(*before))
    stmt = stmt.order_by(DocumentORM.updated_at.desc(), DocumentORM.id.desc()).limit(limit)
    return list(session.scalars(stmt).all())


def count_documents(session: Session, *, user_id: int, category: str | None = None) -> int:
    stmt = select(func.count()).select_from(DocumentORM).where(DocumentORM.user_id == user_id)
    if category is not None:
        stmt = stmt.where(DocumentORM.category == category)
    return session.scalar(stmt) or 0


def list_categories(session: Session, *, user_id: int) -> list[tuple[str, int]]:
    """已用过的分类及篇数，篇数多的在前；没有分类的文档不计入。"""
    count = func.count().label("count")
    stmt = (
        select(DocumentORM.category, count)
        .where(DocumentORM.user_id == user_id, DocumentORM.category.is_not(None))
        .group_by(DocumentORM.category)
        .order_by(count.desc(), DocumentORM.category)
    )
    return [(category, total) for category, total in session.execute(stmt)]


def update_if_version(
    session: Session, document_id: int, expected_version: int, values: dict[str, Any], *, user_id: int
) -> int:
    """版本匹配才更新，同时 version + 1；返回受影响行数（0 表示不存在、无权访问或版本过旧）。"""
    stmt = (
        update(DocumentORM)
        .where(
            DocumentORM.id == document_id,
            DocumentORM.user_id == user_id,
            DocumentORM.version == expected_version,
        )
        .values(**values, version=expected_version + 1)
        .execution_options(synchronize_session=False)
    )
    return session.execute(stmt).rowcount


def delete_if_version(session: Session, document_id: int, expected_version: int, *, user_id: int) -> int:
    """片段由外键级联删除。"""
    stmt = (
        delete(DocumentORM)
        .where(
            DocumentORM.id == document_id,
            DocumentORM.user_id == user_id,
            DocumentORM.version == expected_version,
        )
        .execution_options(synchronize_session=False)
    )
    return session.execute(stmt).rowcount


def find_documents_by_titles(session: Session, titles: Iterable[str], *, user_id: int) -> list[DocumentTitle]:
    """按标题（去掉首尾空白后精确匹配）批量查文档，供互链解析；同名多篇全部返回。"""
    wanted = sorted({title.strip() for title in titles if title.strip()})
    if not wanted:
        return []
    stmt = (
        select(DocumentORM.id, DocumentORM.title, DocumentORM.updated_at)
        .where(DocumentORM.user_id == user_id, func.btrim(DocumentORM.title).in_(wanted))
        .order_by(DocumentORM.updated_at.desc(), DocumentORM.id.desc())
    )
    return [DocumentTitle(id, title, updated_at) for id, title, updated_at in session.execute(stmt)]


# ---------- 片段 ----------

def replace_chunks(session: Session, document: DocumentORM, chunks: list[Chunk]) -> None:
    """整篇重建片段；user_id 取自文档本身，不由调用方另外传入。"""
    session.execute(delete(DocumentChunkORM).where(DocumentChunkORM.document_id == document.id))
    session.add_all(
        DocumentChunkORM(
            document_id=document.id,
            user_id=document.user_id,
            seq=chunk.seq,
            section_seq=chunk.section_seq,
            heading_path=chunk.heading_path,
            start_offset=chunk.start_offset,
            end_offset=chunk.end_offset,
            is_heading_only=chunk.is_heading_only,
            content=chunk.content,
            search_text=build_search_text(document.title, chunk),
        )
        for chunk in chunks
    )
    session.flush()


def get_chunks(
    session: Session,
    document_id: int,
    *,
    user_id: int,
    seq_from: int | None = None,
    seq_to: int | None = None,
) -> list[DocumentChunkORM]:
    """按 seq 返回片段；seq_from / seq_to 是闭区间。"""
    stmt = select(DocumentChunkORM).where(
        DocumentChunkORM.document_id == document_id, DocumentChunkORM.user_id == user_id
    )
    if seq_from is not None:
        stmt = stmt.where(DocumentChunkORM.seq >= seq_from)
    if seq_to is not None:
        stmt = stmt.where(DocumentChunkORM.seq <= seq_to)
    return list(session.scalars(stmt.order_by(DocumentChunkORM.seq)).all())


def get_outline(session: Session, document_id: int, *, user_id: int) -> list[OutlineSection]:
    """每个小节一行，不返回正文。"""
    stmt = (
        select(
            DocumentChunkORM.id,
            DocumentChunkORM.section_seq,
            DocumentChunkORM.heading_path,
            DocumentChunkORM.start_offset,
            DocumentChunkORM.end_offset,
        )
        .where(DocumentChunkORM.document_id == document_id, DocumentChunkORM.user_id == user_id)
        .order_by(DocumentChunkORM.seq)
    )
    sections: list[OutlineSection] = []
    for chunk_id, section_seq, heading_path, start, end in session.execute(stmt):
        if sections and sections[-1].section_seq == section_seq:
            last = sections[-1]
            sections[-1] = OutlineSection(
                section_seq, last.heading_path, last.first_chunk_id, last.chars + end - start, last.chunk_count + 1
            )
        else:
            sections.append(OutlineSection(section_seq, heading_path, chunk_id, end - start, 1))
    return sections


# ---------- 图片 ----------

def insert_image(session: Session, image: DocumentImageORM) -> DocumentImageORM:
    """(user_id, sha256) 冲突时抛出 IntegrityError，由服务层处理并发上传同一张图。"""
    session.add(image)
    session.flush()
    return image


def get_image_by_sha256(session: Session, sha256: str, *, user_id: int) -> DocumentImageORM | None:
    """复用相同图片时使用；data 列延迟加载，这里不会查出来。"""
    stmt = select(DocumentImageORM).where(
        DocumentImageORM.user_id == user_id, DocumentImageORM.sha256 == sha256
    )
    return session.scalar(stmt)


def get_image(session: Session, public_id: str, *, user_id: int) -> DocumentImageORM | None:
    stmt = (
        select(DocumentImageORM)
        .options(undefer(DocumentImageORM.data))
        .where(DocumentImageORM.public_id == public_id, DocumentImageORM.user_id == user_id)
    )
    return session.scalar(stmt)


def total_image_bytes(session: Session, *, user_id: int) -> int:
    stmt = select(func.coalesce(func.sum(DocumentImageORM.byte_size), 0)).where(
        DocumentImageORM.user_id == user_id
    )
    return session.scalar(stmt) or 0


def delete_unreferenced_images(session: Session, *, created_before: datetime) -> int:
    """删除创建早于 created_before、且本人没有任何文档正文引用的图片（PRD D24），跨用户执行。

    用 strpos 而不是 LIKE：public_id 是 32 位十六进制串，不会误匹配，也不必处理通配符。
    """
    referenced = exists().where(
        DocumentORM.user_id == DocumentImageORM.user_id,
        func.strpos(DocumentORM.content, DocumentImageORM.public_id) > 0,
    )
    stmt = (
        delete(DocumentImageORM)
        .where(DocumentImageORM.created_at < created_before, ~referenced)
        .execution_options(synchronize_session=False)
    )
    return session.execute(stmt).rowcount
