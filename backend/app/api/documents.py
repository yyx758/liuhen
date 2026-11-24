"""资料库接口（spec 2.8）。

静态路径（/search、/categories、/images）必须声明在 /{document_id} 之前：FastAPI 按声明顺序匹配，
"/documents/search" 如果先匹配到 /{document_id}，会因为 "search" 不是整数直接返回 422。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

from backend.app.api.deps import CurrentUserId, DbSession
from backend.app.config.settings import IMAGE_MAX_BYTES
from backend.app.schemas.document import (
    DocumentCategory,
    DocumentCreate,
    DocumentListPage,
    DocumentRead,
    DocumentSearchGroup,
    DocumentSummary,
    DocumentUpdate,
    ImageRead,
)
from backend.app.services import document_service, image_service
from backend.app.services.document_search import normalize_queries, search_chunks

router = APIRouter(prefix="/documents", tags=["Documents"])

_TOO_LARGE = f"图片不能超过 {IMAGE_MAX_BYTES // (1024 * 1024)} MB"


async def read_image_body(request: Request) -> bytes:
    """读取图片原始字节，超过上限立即返回 413，不把超大请求整个读进内存。"""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > IMAGE_MAX_BYTES:
        raise HTTPException(413, _TOO_LARGE)
    body = bytearray()
    async for part in request.stream():
        body += part
        if len(body) > IMAGE_MAX_BYTES:
            raise HTTPException(413, _TOO_LARGE)
    return bytes(body)


@router.get("/search", response_model=list[DocumentSearchGroup])
def search_documents(
    session: DbSession,
    user_id: CurrentUserId,
    q: str = Query(min_length=1, max_length=200, description="关键词，多个用空格分隔"),
    category: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=20, ge=1, le=50),
) -> list[DocumentSearchGroup]:
    try:
        queries = normalize_queries(q.split())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    hits = search_chunks(session, queries, user_id=user_id, category=category, limit=limit)
    summaries = document_service.get_summaries(session, [hit.document_id for hit in hits], user_id=user_id)
    groups: dict[int, DocumentSearchGroup] = {}
    for hit in hits:  # 保持文档第一次出现的顺序
        if hit.document_id not in groups:
            groups[hit.document_id] = DocumentSearchGroup(document=summaries[hit.document_id], hits=[])
        groups[hit.document_id].hits.append(hit)
    return list(groups.values())


@router.get("/categories", response_model=list[DocumentCategory])
def list_categories(session: DbSession, user_id: CurrentUserId) -> list[DocumentCategory]:
    return document_service.list_categories(session, user_id=user_id)


@router.post("/images", response_model=ImageRead, status_code=status.HTTP_201_CREATED)
def upload_image(
    session: DbSession,
    user_id: CurrentUserId,
    data: bytes = Depends(read_image_body),
    name: str | None = Query(default=None, max_length=255, description="原文件名"),
) -> ImageRead:
    # 路由是同步函数，在线程池里执行；请求体已由上面的 async 依赖在事件循环里读好。
    return image_service.upload_image(session, data, name, user_id=user_id)


@router.get("/images/{public_id}", response_class=Response)
def get_image(public_id: str, session: DbSession, user_id: CurrentUserId) -> Response:
    data, content_type = image_service.get_image(session, public_id, user_id=user_id)
    return Response(content=data, media_type=content_type, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
        # 同一个图片 ID 的内容不会改变；private 表示只允许浏览器缓存，不允许共享缓存。
        "Cache-Control": "private, max-age=31536000, immutable",
    })


@router.get("", response_model=DocumentListPage)
def list_documents(
    session: DbSession,
    user_id: CurrentUserId,
    category: str | None = Query(default=None, max_length=30),
    before_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=30, ge=1, le=50),
) -> DocumentListPage:
    return document_service.list_documents(
        session, user_id=user_id, category=category, before_id=before_id, limit=limit
    )


@router.post("", response_model=DocumentSummary, status_code=status.HTTP_201_CREATED)
def create_document(
    data: DocumentCreate,
    session: DbSession,
    user_id: CurrentUserId,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> DocumentSummary:
    return document_service.create_document(session, data, str(idempotency_key), user_id=user_id)


@router.get("/{document_id}", response_model=DocumentRead)
def get_document(document_id: int, session: DbSession, user_id: CurrentUserId) -> DocumentRead:
    return document_service.get_document(session, document_id, user_id=user_id)


@router.patch("/{document_id}", response_model=DocumentRead)
def update_document(
    document_id: int,
    data: DocumentUpdate,
    session: DbSession,
    user_id: CurrentUserId,
    expected_version: int = Header(alias="If-Match", gt=0),
) -> DocumentRead:
    return document_service.update_document(session, document_id, data, expected_version, user_id=user_id)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: int,
    session: DbSession,
    user_id: CurrentUserId,
    expected_version: int = Header(alias="If-Match", gt=0),
) -> Response:
    document_service.delete_document(session, document_id, expected_version, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
