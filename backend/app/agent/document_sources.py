"""来源标记的解析、校验、去除，以及来源卡片与引文的生成（spec 3.4）。

全部是纯函数：输入是本轮已持久化的消息（chat_turns.messages），不查数据库，
所以首次完成、崩溃恢复、按同一 request_id 回放得到的卡片完全一致。
"""

import json
import logging
import re
from typing import Any

from backend.app.config.settings import SOURCES_MAX
from backend.app.schemas.conversation import DocumentSource

logger = logging.getLogger(__name__)

# 规范写法是 [[来源:片段ID]]。真实模型会写成全角或混用的方括号、带"片段"或"ID"前缀
# （如 ［[来源:片段 16］］），这些也要识别，否则标记会漏进回答、卡片丢失。
# 必须以"来源"开头并以数字结尾，普通的 [1]、【注意】不受影响。
MARKER = re.compile(r"[ \t]*[\[［【]{1,2}\s*来源\s*[:：]\s*(?:片段\s*)?(?:ID\s*[:：]?\s*)?(\d+)\s*[\]］】]{1,2}")
QUOTE_MAX = 30
QUOTE_MIN = 4

# 引文只取渲染后仍原样可见的文字：先去掉看不到的部分，再按 Markdown 符号切段。
_IMAGE_PLACEHOLDER = re.compile(r"\[图片(?:：[^\]\n]*)?\]")
_LINK_TARGET = re.compile(r"\]\([^()\n]*\)")
_WIKILINK_ALIAS = re.compile(r"\[\[[^\[\]|\n]*\|([^\[\]\n]*)\]\]")
_LIST_MARKER = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+", re.MULTILINE)
_HEADING_LINE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_SEPARATORS = re.compile(r"[\n#>*_`~\[\]()!|<]+")


def strip_markers(text: str) -> str:
    return MARKER.sub("", text)


def _current_turn(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只看最后一条用户消息之后的部分：历史轮次读过的片段不能作为本轮来源。"""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return messages[index:]
    return messages


def _tool_results(messages: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """本轮名为 name 的工具调用中，执行成功的结果（已解析为 dict）。"""
    outputs = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}
    parsed: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if call.get("function", {}).get("name") != name or call.get("id") not in outputs:
                continue
            try:
                value = json.loads(outputs[call["id"]])
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and value.get("ok") is not False:
                parsed.append(value)
    return parsed


def chars_read_in_turn(messages: list[dict[str, Any]]) -> int:
    """本轮 read_document 已返回的正文字数之和，作为下一次读取的 chars_read_before。"""
    return sum(int(r.get("chars_returned") or 0) for r in _tool_results(_current_turn(messages), "read_document"))


def build_sources(messages: list[dict[str, Any]], *, request_id: str | None = None) -> list[DocumentSource]:
    turn = _current_turn(messages)
    if not turn or turn[-1].get("role") != "assistant" or turn[-1].get("tool_calls"):
        return []

    read_chunks: dict[int, tuple[int, str, list[str], str, int | None]] = {}
    for result in _tool_results(turn, "read_document"):
        document = result.get("document") or {}
        for chunk in result.get("chunks") or []:
            read_chunks[chunk["chunk_id"]] = (
                document["id"], document["title"], chunk["heading_path"], chunk["content"], chunk.get("section_seq"),
            )

    matched: dict[int, list[str]] = {}
    for result in _tool_results(turn, "search_documents"):
        for hit in result.get("hits") or []:
            matched.setdefault(hit["chunk_id"], []).extend(hit.get("matched_queries") or [])

    sources: list[DocumentSource] = []
    seen: set[tuple[int, int | tuple[str, ...]]] = set()
    for raw_id in MARKER.findall(turn[-1].get("content") or ""):
        chunk_id = int(raw_id)
        if chunk_id not in read_chunks:
            logger.warning("来源标记引用了本轮未读过的片段 chunk_id=%s request_id=%s", chunk_id, request_id)
            continue
        document_id, title, heading_path, content, section_seq = read_chunks[chunk_id]
        # 按小节合并：同名兄弟小节路径相同，要靠 section_seq 区分；
        # 加这个字段之前保存的工具结果没有它，退回按路径合并。
        key = (document_id, section_seq if section_seq is not None else tuple(heading_path))
        if key in seen:
            continue
        seen.add(key)
        sources.append(DocumentSource(
            document_id=document_id, title=title, heading_path=heading_path,
            quote=quote(content, matched.get(chunk_id, [])),
        ))
        if len(sources) == SOURCES_MAX:
            break
    return sources


def plain_segments(content: str) -> list[str]:
    """渲染后原样可见的纯文本段；正文段优先，片段里没有正文时才用标题行。"""
    text = _IMAGE_PLACEHOLDER.sub("\n", content)
    text = _LINK_TARGET.sub("]", text)
    text = _WIKILINK_ALIAS.sub(r"\1", text)
    text = _LIST_MARKER.sub("", text)
    body: list[str] = []
    headings: list[str] = []
    for line in text.split("\n"):
        target = headings if _HEADING_LINE.match(line) else body
        for segment in _SEPARATORS.split(line):
            segment = segment.strip()
            if len(segment) >= QUOTE_MIN and any(ch.isalnum() for ch in segment):
                target.append(segment)
    return body or headings


def quote(content: str, queries: list[str]) -> str:
    segments = plain_segments(content)
    for segment in segments:
        lowered = segment.lower()
        for query in queries:
            position = lowered.find(query.lower())
            if position < 0:
                continue
            if len(segment) <= QUOTE_MAX:
                return segment
            start = max(0, min(position + len(query) // 2 - QUOTE_MAX // 2, len(segment) - QUOTE_MAX))
            return segment[start:start + QUOTE_MAX]
    return segments[0][:QUOTE_MAX] if segments else ""
