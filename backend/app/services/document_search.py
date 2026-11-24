"""文档关键词搜索（spec 2.4）。资料库页搜索与 Agent 的 search_documents 共用。"""

from collections.abc import Iterable

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import Session

from backend.app.config.settings import (
    SEARCH_MAX_QUERIES,
    SEARCH_PER_DOCUMENT,
    SEARCH_QUERY_MAX_CHARS,
    USER_TIMEZONE,
)
from backend.app.models.document import DocumentChunkORM, DocumentORM
from backend.app.schemas.document import SearchHit
from backend.app.services.document_markdown import to_search_text

SNIPPET_CONTEXT = 60
SNIPPET_FALLBACK = 120


def normalize_queries(raw: Iterable[str]) -> list[str]:
    """去掉首尾空白、丢弃空关键词、忽略大小写去重；不合法时抛出 ValueError。"""
    queries: list[str] = []
    seen: set[str] = set()
    for value in raw:
        query = value.strip()
        if not query:
            continue
        if len(query) > SEARCH_QUERY_MAX_CHARS:
            raise ValueError(f"每个关键词不能超过 {SEARCH_QUERY_MAX_CHARS} 字")
        if query.lower() not in seen:
            seen.add(query.lower())
            queries.append(query)
    if not queries:
        raise ValueError("请提供至少一个关键词")
    if len(queries) > SEARCH_MAX_QUERIES:
        raise ValueError(f"一次最多 {SEARCH_MAX_QUERIES} 个关键词")
    return queries


def like_pattern(query: str) -> str:
    """ILIKE 的包含匹配；\\、%、_ 转义后按字面匹配。"""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_chunks(
    session: Session,
    queries: list[str],
    *,
    user_id: int,
    category: str | None = None,
    limit: int = 8,
    per_document: int = SEARCH_PER_DOCUMENT,
) -> list[SearchHit]:
    """queries 必须是 normalize_queries 的结果。"""
    chunk, document = DocumentChunkORM, DocumentORM
    hits = [chunk.search_text.ilike(like_pattern(q), escape="\\") for q in queries]
    matched = sum((case((hit, 1), else_=0) for hit in hits), literal(0))
    title_hit = sum(
        (case((document.title.ilike(like_pattern(q), escape="\\"), 1), else_=0) for q in queries), literal(0)
    )
    lowered = func.lower(chunk.search_text)
    occurrences = sum(
        ((func.length(lowered) - func.length(func.replace(lowered, q.lower(), ""))) / len(q) for q in queries),
        literal(0),
    )

    def ranking(matched, is_heading_only, title_hit, occurrences, updated_at, seq):
        # 命中关键词数 → 纯标题片段在后 → 标题命中 → 出现次数 → 文档更新时间 → 文档内顺序
        return (matched.desc(), is_heading_only.asc(), title_hit.desc(),
                occurrences.desc(), updated_at.desc(), seq.asc())

    rank_in_document = func.row_number().over(
        partition_by=chunk.document_id,
        order_by=ranking(matched, chunk.is_heading_only, title_hit, occurrences, document.updated_at, chunk.seq),
    )
    stmt = (
        select(
            chunk.id.label("chunk_id"), chunk.document_id, chunk.heading_path, chunk.content,
            chunk.search_text, chunk.is_heading_only, chunk.seq, document.title, document.category, document.updated_at,
            matched.label("matched"), title_hit.label("title_hit"), occurrences.label("occurrences"),
            rank_in_document.label("rank_in_document"),
        )
        .join(document, document.id == chunk.document_id)
        # 两张表都带 user_id 条件。
        .where(chunk.user_id == user_id, document.user_id == user_id, or_(*hits))
    )
    if category is not None:
        stmt = stmt.where(document.category == category)
    ranked = stmt.subquery()
    c = ranked.c
    rows = session.execute(
        select(ranked)
        .where(c.rank_in_document <= per_document)
        .order_by(*ranking(c.matched, c.is_heading_only, c.title_hit, c.occurrences, c.updated_at, c.seq))
        .limit(limit)
    ).all()

    results = []
    for row in rows:
        searchable = row.search_text.lower()
        results.append(SearchHit(
            document_id=row.document_id,
            title=row.title,
            category=row.category,
            chunk_id=row.chunk_id,
            heading_path=row.heading_path,
            snippet=snippet(row.content, queries),
            matched_queries=[q for q in queries if q.lower() in searchable],
            is_heading_only=row.is_heading_only,
            updated_at=row.updated_at.astimezone(USER_TIMEZONE),
        ))
    return results


def snippet(content: str, queries: list[str]) -> str:
    """以最早出现的关键词为中心，前后各约 60 字；正文里没有命中时取正文开头 120 字。"""
    text = to_search_text(content)
    lowered = text.lower()
    positions = [(lowered.find(q.lower()), q) for q in queries]
    positions = [(position, q) for position, q in positions if position >= 0]
    if not positions:
        return text if len(text) <= SNIPPET_FALLBACK else text[:SNIPPET_FALLBACK] + "…"
    position, query = min(positions)
    start = max(0, position - SNIPPET_CONTEXT)
    end = min(len(text), position + len(query) + SNIPPET_CONTEXT)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")
