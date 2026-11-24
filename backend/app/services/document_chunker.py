"""把文档正文切成片段（spec 1.8）。纯函数，不查数据库。

每个片段都是归一化正文的一个区间 content[start:end]：去掉首尾空白只靠收缩区间，
从不把几段文本重新拼接，所以不会改变空行、缩进和 Markdown 结构。
"""

import re
from dataclasses import dataclass

from backend.app.config.settings import CHUNK_MAX_CHARS, CHUNK_TARGET_CHARS
from backend.app.services.document_markdown import (
    fence_closes,
    fence_open,
    find_inline_spans,
    to_search_text,
)

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_SENTENCE_ENDS = frozenset("。！？；.!?;")


@dataclass(frozen=True)
class Chunk:
    seq: int
    section_seq: int
    heading_path: list[str]
    start_offset: int
    end_offset: int
    is_heading_only: bool
    content: str


@dataclass(frozen=True)
class _Section:
    heading_path: list[str]
    start: int
    end: int
    heading_end: int | None  # 标题行结束位置（不含换行）；前言和 front matter 为 None


@dataclass(frozen=True)
class _Block:
    start: int
    end: int
    is_code: bool


def normalize_content(content: str) -> str:
    """换行统一为 \\n，去掉末尾空白。哈希、切片、偏移量都基于归一化结果。"""
    return content.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def build_search_text(title: str, chunk: Chunk) -> str:
    return f"{title}\n{' > '.join(chunk.heading_path)}\n{to_search_text(chunk.content)}"


def split_markdown(
    content: str, *, target: int = CHUNK_TARGET_CHARS, limit: int = CHUNK_MAX_CHARS
) -> list[Chunk]:
    """content 必须是 normalize_content 的结果。"""
    chunks: list[Chunk] = []
    for section_seq, section in enumerate(_split_sections(content)):
        for start, end, heading_only in _split_section(content, section, target, limit):
            chunks.append(Chunk(
                seq=len(chunks),
                section_seq=section_seq,
                heading_path=list(section.heading_path),
                start_offset=start,
                end_offset=end,
                is_heading_only=heading_only,
                content=content[start:end],
            ))
    return chunks


# ---------- 第一层：按标题切成小节 ----------

def _lines(content: str, start: int = 0, end: int | None = None) -> list[tuple[int, str]]:
    """返回 (行首位置, 行文本) 列表，行文本不含换行。"""
    end = len(content) if end is None else end
    result = []
    position = start
    for line in content[start:end].split("\n"):
        result.append((position, line))
        position += len(line) + 1
    return result


def _front_matter_end(lines: list[tuple[int, str]]) -> int | None:
    """front matter 闭合行的下标；不是 front matter 时返回 None。"""
    if not lines or lines[0][1].rstrip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index][1].rstrip() in ("---", "..."):
            return index
    return None


def _split_sections(content: str) -> list[_Section]:
    lines = _lines(content)
    starts: list[tuple[list[str], int, int | None]] = []  # (路径, 起点, 标题行结束)

    first_line = 0
    front_matter_end = _front_matter_end(lines)
    if front_matter_end is not None:
        starts.append(([], 0, None))
        first_line = front_matter_end + 1
    preamble_start = lines[first_line][0] if first_line < len(lines) else len(content)
    if first_line < len(lines):
        starts.append(([], preamble_start, None))

    stack: list[tuple[int, str]] = []
    marker: str | None = None
    for line_start, line in lines[first_line:]:
        if marker is not None:
            if fence_closes(line, marker):
                marker = None
            continue
        marker = fence_open(line)
        if marker is not None:
            continue
        match = _HEADING.match(line)
        if match is None or not match.group(2).strip():
            continue
        level = len(match.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, match.group(2).strip()))
        starts.append(([title for _, title in stack], line_start, line_start + len(line)))

    sections = []
    for index, (path, start, heading_end) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else len(content)
        if front_matter_end is not None and index == 0:
            fm_line_start, fm_line = lines[front_matter_end]
            end = fm_line_start + len(fm_line)
        section = _Section(path, start, end, heading_end)
        # 空白的前言（文档直接以标题开头）不算一个小节。
        if heading_end is None and not content[start:end].strip():
            continue
        sections.append(section)
    return sections


# ---------- 第二层：把小节切成片段 ----------

def _trim(content: str, start: int, end: int) -> tuple[int, int]:
    while start < end and content[start].isspace():
        start += 1
    while end > start and content[end - 1].isspace():
        end -= 1
    return start, end


def _split_section(
    content: str, section: _Section, target: int, limit: int
) -> list[tuple[int, int, bool]]:
    """返回 (start, end, is_heading_only) 列表，区间已去掉首尾空白。"""
    start, end = _trim(content, section.start, section.end)
    heading_end = section.heading_end
    if end - start <= limit:
        heading_only = heading_end is not None and not content[heading_end:end].strip()
        return [(start, end, heading_only)]
    return _Packer(content, target, limit).pack(start, end, heading_end)


class _Packer:
    """超长小节的装箱（spec 1.8.5）。

    当前片段从 chunk_start 开始、到 chunk_end 结束；has_body 表示其中是否已有正文块。
    只有标题前缀时（has_body 为 False）不因 target 开新片段，避免切出纯标题片段。
    """

    def __init__(self, content: str, target: int, limit: int):
        self.content = content
        self.target = target
        self.limit = limit
        self.result: list[tuple[int, int, bool]] = []
        self.chunk_start: int | None = None
        self.chunk_end = 0
        self.has_body = False

    def pack(self, start: int, end: int, heading_end: int | None) -> list[tuple[int, int, bool]]:
        body_start = start
        if heading_end is not None:
            if heading_end - start > self.limit:
                # 唯一允许超过 limit 的情况：标题行本身超长，单独成片段。
                self.result.append((start, heading_end, True))
            else:
                self.chunk_start, self.chunk_end = start, heading_end
            body_start = heading_end
        for block in _blocks(self.content, body_start, end):
            self._add(block)
        self._flush()
        return self.result

    def _add(self, block: _Block) -> None:
        if self.chunk_start is not None:
            new_length = block.end - self.chunk_start
            image_only = not block.is_code and _is_image_only(self.content, block)
            # 纯图片块只受 limit 约束，尽量和前一块留在一起（D30）。
            if self.has_body and not image_only and new_length > self.target:
                self._flush()
            elif new_length <= self.limit:
                self.chunk_end, self.has_body = block.end, True
                return
            elif self.has_body:
                self._flush()
        self._place(block)

    def _place(self, block: _Block) -> None:
        """把块放进当前片段（可能只有标题前缀，也可能是空的），放不下时在块内切开。"""
        piece_start = block.start
        if self.chunk_start is None:
            self.chunk_start = block.start
        while block.end - self.chunk_start > self.limit:
            budget_end = self.chunk_start + self.limit
            if budget_end <= piece_start:
                # 标题前缀已经占满预算，标题单独成片段，正文从新片段开始。
                self._flush()
                self.chunk_start = piece_start
                continue
            cut = _find_cut(self.content, piece_start, block.end, budget_end, block.is_code)
            self.chunk_end = cut
            self.has_body = self.has_body or cut > piece_start
            self._flush()
            piece_start = cut
            while piece_start < block.end and self.content[piece_start].isspace():
                piece_start += 1
            self.chunk_start = piece_start
        self.chunk_end, self.has_body = block.end, True

    def _flush(self) -> None:
        if self.chunk_start is None:
            return
        start, end = _trim(self.content, self.chunk_start, self.chunk_end)
        if start < end:
            self.result.append((start, end, not self.has_body))
        self.chunk_start, self.chunk_end, self.has_body = None, 0, False


def _blocks(content: str, start: int, end: int) -> list[_Block]:
    """把小节正文切成块：一个完整代码块是一块，其余以空行分隔，连续非空行是一块。"""
    blocks: list[_Block] = []
    current: list[int] | None = None  # [start, end]
    current_is_code = False
    marker: str | None = None

    def close() -> None:
        nonlocal current
        if current is not None:
            block_start, block_end = _trim(content, current[0], current[1])
            if block_start < block_end:
                blocks.append(_Block(block_start, block_end, current_is_code))
        current = None

    for line_start, line in _lines(content, start, end):
        line_end = line_start + len(line)
        if marker is not None:
            current[1] = line_end
            if fence_closes(line, marker):
                marker = None
                close()
            continue
        opened = fence_open(line)
        if opened is not None:
            close()
            current, current_is_code, marker = [line_start, line_end], True, opened
        elif not line.strip():
            close()
        elif current is None:
            current, current_is_code = [line_start, line_end], False
        else:
            current[1] = line_end
    close()
    return blocks


def _is_image_only(content: str, block: _Block) -> bool:
    text = content[block.start:block.end]
    spans = find_inline_spans(text)
    if not spans or any(span.kind != "image" for span in spans):
        return False
    remaining = list(text)
    for span in spans:
        remaining[span.start:span.end] = " " * (span.end - span.start)
    return not "".join(remaining).strip()


def _find_cut(content: str, start: int, end: int, budget_end: int, is_code: bool) -> int:
    """在 (start, budget_end] 内找最靠后的切点：换行 → 句末 → 硬切；代码块只按行切。

    返回的切点是片段结束位置（不含该位置的字符）。
    """
    hard_limit = budget_end
    budget_end = min(budget_end, end)
    newline = content.rfind("\n", start + 1, budget_end + 1)
    if newline > start:
        return newline
    if is_code:
        return budget_end

    spans = [(span.start + start, span.end + start) for span in find_inline_spans(content[start:end])]

    def inside_span(position: int) -> bool:
        return any(span_start < position < span_end for span_start, span_end in spans)

    for position in range(budget_end, start, -1):
        if content[position - 1] in _SENTENCE_ENDS and not inside_span(position):
            return position

    cut = budget_end
    for span_start, span_end in spans:
        if span_start < cut < span_end:
            # 切点落在行内元素中间时前移到元素之前；元素从片段开头就开始时只能后移到元素之后。
            cut = span_start if span_start > start else span_end
    # 元素本身比 limit 还长时只能在元素内部硬切（实际不会出现：一个链接不可能有 1500 字）。
    return min(cut, hard_limit)
