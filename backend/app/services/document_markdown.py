"""文档正文里的行内元素与围栏代码块识别（spec 1.8.3、1.8.8）。

切片（不可切开的范围）、检索文本（可读化）、互链解析共用这一套规则，保证口径一致。
全部是纯函数，不查数据库。
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# 围栏：缩进 0–3 个空格，3 个及以上相同的 ` 或 ~。
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")

# 行内元素都不跨行。
_CODE = re.compile(r"(`+)([^\n]+?)(?<!`)\1(?!`)")
_IMAGE = re.compile(r'!\[([^\[\]\n]*)\]\(([^()\s]*)(?:[ \t]+"[^"\n]*")?\)')
_WIKILINK = re.compile(r"(!?)\[\[([^\[\]\n]+)\]\]")
_LINK = re.compile(r'\[([^\[\]\n]*)\]\(([^()\s]*)(?:[ \t]+"[^"\n]*")?\)')
# 带协议的地址（http:、mailto: 等）不是指向其他笔记的链接。
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def fence_open(line: str) -> str | None:
    """这一行开始一个围栏代码块时返回围栏字符串（如 "```"），否则返回 None。"""
    match = _FENCE_OPEN.match(line)
    if match is None:
        return None
    marker, info = match.groups()
    # 反引号围栏的信息串里不能再有反引号（CommonMark），否则这一行只是普通文字。
    if marker[0] == "`" and "`" in info:
        return None
    return marker


def fence_closes(line: str, marker: str) -> bool:
    """这一行能否结束以 marker 开始的代码块：同种字符、长度不少于开始围栏、后面只有空白。"""
    match = _FENCE_CLOSE.match(line)
    return match is not None and match.group(1)[0] == marker[0] and len(match.group(1)) >= len(marker)


@dataclass(frozen=True)
class InlineSpan:
    kind: Literal["image", "link", "wikilink", "code"]
    start: int  # 在传入文本中的位置，左闭右开
    end: int
    text: str  # 图片说明 / 链接文字 / 互链显示文字 / 代码内容
    target: str | None  # 图片地址、链接地址、互链目标；代码为 None


@dataclass(frozen=True)
class DocLink:
    text: str  # 显示文字
    title: str  # 目标文档标题
    heading: str | None  # 目标小节，没有为 None


def find_inline_spans(text: str) -> list[InlineSpan]:
    """识别行内元素。调用方负责跳过围栏代码块，只传入代码块之外的文本。"""
    spans: list[InlineSpan] = []
    position = 0
    while position < len(text):
        char = text[position]
        if char == "`":
            match = _CODE.match(text, position)
            if match is not None:
                spans.append(InlineSpan("code", position, match.end(), match.group(2), None))
                position = match.end()
            else:
                # 没有配对的一串反引号整体跳过，避免从中间某个反引号重新开始配对。
                while position < len(text) and text[position] == "`":
                    position += 1
            continue
        if char in "![":
            span = _match_bracket_element(text, position)
            if span is not None:
                spans.append(span)
                position = span.end
                continue
        position += 1
    return spans


def _match_bracket_element(text: str, position: int) -> InlineSpan | None:
    match = _WIKILINK.match(text, position)
    if match is not None:
        bang, target = match.groups()
        if bang and target.lower().endswith(IMAGE_EXTENSIONS):
            return InlineSpan("image", position, match.end(), target, target)
        target, _, alias = target.partition("|")
        return InlineSpan("wikilink", position, match.end(), alias or target, target)
    pattern, kind = (_IMAGE, "image") if text[position] == "!" else (_LINK, "link")
    match = pattern.match(text, position)
    if match is None:
        return None
    return InlineSpan(kind, position, match.end(), match.group(1), match.group(2))


def iter_code_regions(text: str) -> Iterator[tuple[int, int, bool]]:
    """把文本按围栏代码块切成连续区间，依次产出 (start, end, 是否代码块)。"""
    region_start = 0
    line_start = 0
    marker: str | None = None
    for line in text.split("\n"):
        line_end = min(line_start + len(line) + 1, len(text))
        if marker is None:
            opened = fence_open(line)
            if opened is not None:
                if line_start > region_start:
                    yield region_start, line_start, False
                region_start = line_start
                marker = opened
        elif fence_closes(line, marker):
            yield region_start, line_end, True
            region_start = line_end
            marker = None
        line_start += len(line) + 1
    if region_start < len(text):
        # 没有闭合的代码块延续到文末。
        yield region_start, len(text), marker is not None


def _spans_outside_code(text: str) -> list[InlineSpan]:
    spans = []
    for start, end, is_code in iter_code_regions(text):
        if is_code:
            continue
        for span in find_inline_spans(text[start:end]):
            spans.append(InlineSpan(span.kind, span.start + start, span.end + start, span.text, span.target))
    return spans


def extract_doc_links(text: str) -> list[DocLink]:
    """提取指向其他文档的链接，按出现顺序，(title, heading) 相同的只保留第一个。"""
    links: list[DocLink] = []
    seen: set[tuple[str, str | None]] = set()
    for span in _spans_outside_code(text):
        if span.kind == "wikilink":
            title, _, heading = span.target.partition("#")
        elif span.kind == "link" and _is_document_path(span.target):
            path, _, heading = span.target.partition("#")
            title = unquote(path.rsplit("/", 1)[-1])[: -len(".md")]
            heading = unquote(heading)
        else:
            continue
        title, heading = title.strip(), heading.strip() or None
        # [[#小节]] 指向本文的小节，不是其他文档。
        if not title or (title, heading) in seen:
            continue
        seen.add((title, heading))
        links.append(DocLink(text=span.text, title=title, heading=heading))
    return links


def _is_document_path(target: str) -> bool:
    if _URL_SCHEME.match(target) or target.startswith("/"):
        return False
    return target.partition("#")[0].lower().endswith(".md")


def images_to_text(text: str) -> str:
    """生成给模型读的正文（PRD D26）：只把图片换成 [图片：说明]，链接和代码原样保留。"""
    parts: list[str] = []
    last = 0
    for span in _spans_outside_code(text):
        if span.kind != "image":
            continue
        parts.append(text[last:span.start])
        parts.append(f"[图片：{span.text}]" if span.text else "[图片]")
        last = span.end
    parts.append(text[last:])
    return "".join(parts)


def to_search_text(text: str) -> str:
    """生成检索用正文：图片变成 [图片：说明]，链接只留文字；代码原样保留。"""
    parts: list[str] = []
    last = 0
    for span in _spans_outside_code(text):
        parts.append(text[last:span.start])
        if span.kind == "image":
            parts.append(f"[图片：{span.text}]" if span.text else "[图片]")
        elif span.kind == "link":
            parts.append(span.text)
        elif span.kind == "wikilink":
            parts.append(span.text if span.text == span.target else f"{span.text}（{span.target}）")
        else:
            parts.append(text[span.start:span.end])
        last = span.end
    parts.append(text[last:])
    return "".join(parts)
