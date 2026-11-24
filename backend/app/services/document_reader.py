"""Agent read_document 的范围计算、分段续读与阅读量（spec 3.3）。

工具本身不保存状态：本轮已读字数由调用方从持久化的消息算出后传入（chars_read_before）。
"""

from dataclasses import asdict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.config.settings import READ_MAX_CHARS_PER_CALL, READ_MAX_CHARS_PER_TURN, USER_TIMEZONE
from backend.app.models.document import DocumentChunkORM
from backend.app.repositories import document_repository
from backend.app.services.document_markdown import extract_doc_links, images_to_text
from backend.app.services.document_service import resolve_links

STALE_CHUNK = "片段不存在或已失效（文档可能已被修改），请重新搜索或先读目录"
BUDGET_USED_UP = "本轮阅读量已用完，请基于已读内容回答，不要再调用 read_document"
TOP_LEVEL_PARENT = "已是顶层小节，返回目录"


def read(
    session: Session,
    document_id: int,
    *,
    user_id: int,
    chunk_ids: list[int] | None,
    scope: str,
    continue_from: int | None,
    chars_read_before: int,
) -> dict:
    document = document_repository.get_document(session, document_id, user_id=user_id)
    if document is None:
        raise HTTPException(404, "文档不存在")
    result: dict = {
        "document": {
            "id": document.id,
            "title": document.title,
            "category": document.category,
            "updated_at": document.updated_at.astimezone(USER_TIMEZONE),
        },
        "scope": scope,
    }
    left_before = max(READ_MAX_CHARS_PER_TURN - chars_read_before, 0)

    def outline(note: str | None = None) -> dict:
        sections = document_repository.get_outline(session, document.id, user_id=user_id)
        result["sections"] = [asdict(section) for section in sections]
        if note:
            result["note"] = note
        result.update(chars_returned=0, chars_left_this_turn=left_before)
        return result

    if scope == "outline":
        return outline()

    # 一篇文档最多 5 万字，整篇片段一次取出后在内存里算范围。
    chunks = document_repository.get_chunks(session, document.id, user_id=user_id)
    position = {chunk.id: index for index, chunk in enumerate(chunks)}
    if chunk_ids and any(chunk_id not in position for chunk_id in chunk_ids):
        raise ValueError(STALE_CHUNK)
    starts = [position[chunk_id] for chunk_id in chunk_ids] if chunk_ids else ([0] if chunks else [])

    selected: set[int] = set()
    parent_starts = 0
    for start in starts:
        if scope == "parent" and len(chunks[start].heading_path) <= 1:
            continue
        parent_starts += 1
        selected.update(_scope_range(chunks, start, scope))
    if scope == "parent" and starts and parent_starts == 0:
        return outline(TOP_LEVEL_PARENT)
    in_range = [chunks[index] for index in sorted(selected)]

    if continue_from is not None:
        if continue_from not in {chunk.id for chunk in in_range}:
            raise ValueError("continue_from 不在本次阅读范围内，请使用上一次结果返回的 next_chunk_id")
        resume_seq = chunks[position[continue_from]].seq
        in_range = [chunk for chunk in in_range if chunk.seq >= resume_seq]

    budget = min(READ_MAX_CHARS_PER_CALL, left_before)
    if budget <= 0 and in_range:
        result.update(chunks=[], links=[], truncated=True, next_chunk_id=in_range[0].id,
                      skipped_chunk_ids=[chunk.id for chunk in in_range],
                      chars_returned=0, chars_left_this_turn=0, note=BUDGET_USED_UP)
        return result

    returned: list[DocumentChunkORM] = []
    used = 0
    for chunk in in_range:
        # 第一个片段总会返回（单个片段最多 1500 字），之后超出预算就停止。
        if returned and used + len(chunk.content) > budget:
            break
        returned.append(chunk)
        used += len(chunk.content)
    rest = in_range[len(returned):]

    links = {}
    for chunk in returned:
        for link in extract_doc_links(chunk.content):
            links.setdefault((link.title, link.heading), link)

    result.update(
        chunks=[
            {
                "chunk_id": c.id, "seq": c.seq, "section_seq": c.section_seq,
                "heading_path": c.heading_path, "content": images_to_text(c.content),
            }
            for c in returned
        ],
        links=[
            link.model_dump() for link in resolve_links(session, list(links.values()), user_id=user_id)
        ] if links else [],
        truncated=bool(rest),
        next_chunk_id=rest[0].id if rest else None,
        # 测评试跑时模型漏看了 truncated，跳过几段却说"全读"：把没返回的片段明确列出来。
        skipped_chunk_ids=[chunk.id for chunk in rest],
        chars_returned=used,
        chars_left_this_turn=max(left_before - used, 0),
    )
    if rest:
        result["note"] = truncation_note(len(returned), rest)
    return result


def truncation_note(returned: int, rest: list[DocumentChunkORM]) -> str:
    shown = "、".join(str(chunk.id) for chunk in rest[:20]) + ("等" if len(rest) > 20 else "")
    return (
        f"单次最多 {READ_MAX_CHARS_PER_CALL} 字，只返回了 {returned} 段，还有 {len(rest)} 段没有返回（片段 {shown}）。"
        f"需要的话用同样的 chunk_ids、scope 加 continue_from={rest[0].id} 续读；"
        "不续读就在回答里说明这部分没有读，不要说成已经读完"
    )


def _scope_range(chunks: list[DocumentChunkORM], start: int, scope: str) -> range | list[int]:
    """单个起点的范围（spec 1.9 的规则），返回 chunks 里的下标。"""
    chunk = chunks[start]
    if scope == "chunk":
        return [start]
    if scope == "neighbors":
        return range(max(start - 1, 0), min(start + 2, len(chunks)))
    if scope == "section":
        return [i for i, c in enumerate(chunks) if c.section_seq == chunk.section_seq]
    if scope == "subtree":
        return _subtree(chunks, start)
    if scope == "parent":
        parent_path = chunk.heading_path[:-1]
        for index in range(start - 1, -1, -1):
            if chunks[index].heading_path == parent_path:
                return _subtree(chunks, index)
        return _subtree(chunks, start)
    raise ValueError(f"不支持的阅读范围：{scope}")


def _subtree(chunks: list[DocumentChunkORM], start: int) -> range:
    """所在小节加上其后连续的、路径以本节路径为前缀且更长的小节；同名兄弟小节不会被并入。"""
    path = chunks[start].heading_path
    section = chunks[start].section_seq
    first = start
    while first > 0 and chunks[first - 1].section_seq == section:
        first -= 1
    end = start + 1
    while end < len(chunks):
        candidate = chunks[end]
        if candidate.section_seq != section and not (
            len(candidate.heading_path) > len(path) and candidate.heading_path[: len(path)] == path
        ):
            break
        end += 1
    return range(first, end)
