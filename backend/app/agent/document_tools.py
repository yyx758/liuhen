"""Agent 的资料库工具（spec 3.2）：全部只读，user_id 由调用方从本轮 chat_turns.user_id 传入。"""

from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.config.settings import USER_TIMEZONE
from backend.app.repositories import document_repository
from backend.app.services import document_reader
from backend.app.services.document_search import normalize_queries, search_chunks

LIST_MAX = 50
SEARCH_NOTE = "摘要只用于判断相关性；回答前必须用 read_document 读取原文"
SEARCH_EMPTY_NOTE = "资料库里没有包含这些关键词的内容；可以换同义词或更短的词再搜"

# 本轮已读字数由执行工具的一方从持久化的消息算出后放进 session.info（spec 3.3）。
CHARS_READ_KEY = "document_chars_read"


class SearchDocumentsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(
        min_length=1, max_length=5,
        description="1–5 个关键词或短语，每个不超过 30 字。把问题拆成短词，并补充同义词、别称、英文缩写",
    )
    category: str | None = Field(default=None, max_length=30, description="只搜某个分类；不确定时不要填")
    limit: int = Field(default=8, ge=1, le=15)


class ReadDocumentArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: int = Field(gt=0)
    chunk_ids: list[int] | None = Field(
        default=None, max_length=10,
        description="要读的片段 ID，来自本轮 search_documents 或 read_document 的结果；不传则从文档开头读",
    )
    scope: Literal["outline", "chunk", "neighbors", "section", "subtree", "parent"] = "chunk"
    continue_from: int | None = Field(
        default=None, gt=0,
        description="上一次结果被截断时返回的 next_chunk_id；用同样的 chunk_ids 和 scope 续读",
    )


class ListDocumentsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str | None = Field(default=None, max_length=30)
    title_contains: str | None = Field(default=None, max_length=30)
    include_outline: bool = Field(
        default=False,
        description="同时返回每篇的目录（小节路径、第一个片段 ID、字数），需要逐篇阅读多篇笔记时用它代替逐篇读目录",
    )


def search_documents(data: SearchDocumentsArguments, session: Session, *, user_id: int) -> dict:
    queries = normalize_queries(data.queries)
    hits = search_chunks(session, queries, user_id=user_id, category=data.category, limit=data.limit)
    return {"hits": hits, "note": SEARCH_NOTE if hits else SEARCH_EMPTY_NOTE}


def read_document(data: ReadDocumentArguments, session: Session, *, user_id: int) -> dict:
    return document_reader.read(
        session, data.document_id, user_id=user_id, chunk_ids=data.chunk_ids, scope=data.scope,
        continue_from=data.continue_from, chars_read_before=session.info.get(CHARS_READ_KEY, 0),
    )


def list_documents(data: ListDocumentsArguments, session: Session, *, user_id: int) -> dict:
    documents = document_repository.list_documents(
        session, user_id=user_id, category=data.category, title_contains=data.title_contains, limit=LIST_MAX,
    )
    items = []
    for document in documents:
        item = {
            "id": document.id,
            "title": document.title,
            "category": document.category,
            "content_chars": len(document.content),
            "updated_at": document.updated_at.astimezone(USER_TIMEZONE),
        }
        if data.include_outline:
            # 目录不含正文，不计入阅读量。
            item["sections"] = [
                asdict(section) for section in document_repository.get_outline(session, document.id, user_id=user_id)
            ]
        items.append(item)
    return {"documents": items, "total_shown": len(items)}


TOOLS = {
    "search_documents": {
        "description": (
            "在用户自己的资料库（笔记）里按关键词搜索片段。用户提到笔记、资料、文档、之前记的内容，"
            "或问题明显依赖个人资料时使用；纯任务操作不要调用。一次传多个短关键词并补充同义词；"
            "没找到时换说法再搜。返回的只是摘要，回答前必须用 read_document 读原文。"
        ),
        "schema": SearchDocumentsArguments,
        "handler": search_documents,
    },
    "read_document": {
        "description": (
            "读取资料库文档的原文。scope：outline 只看目录；chunk 读指定片段；neighbors 连同前后各一段；"
            "section 读整个小节；subtree 读小节及其子小节；parent 读上一级小节。结果被截断时用 continue_from 续读。"
            "结果里的 links 是文中指向其他笔记的链接，可以直接用其中的 document_id 继续读（多跳）。"
            "图片只能看到说明文字。"
        ),
        "schema": ReadDocumentArguments,
        "handler": read_document,
    },
    "list_documents": {
        "description": (
            "按分类或标题关键字列出资料库里的文档（不含正文），用于浏览或回答“我有哪些关于某主题的笔记”。"
            "需要逐篇阅读多篇笔记时传 include_outline=true，一次拿到各篇目录和片段 ID。"
        ),
        "schema": ListDocumentsArguments,
        "handler": list_documents,
    },
}
