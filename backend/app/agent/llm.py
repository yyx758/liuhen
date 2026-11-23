import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, Field

from backend.app.config.env import get_model_config


class LLMResult(BaseModel):
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    assistant_message: dict[str, Any]
    inputTokens: int | None = None
    outputTokens: int | None = None


class StreamAccumulator:
    """把上游流式 chunk 中的增量拼成与非流式响应一致的完整消息。"""

    def __init__(self) -> None:
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        # 同一个工具调用的片段分散在多个 chunk 中，按 index 归并。
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] = {}

    def add(self, chunk: dict[str, Any]) -> str:
        """吸收一个 chunk，返回其中新增的回复文本（可能为空）。"""
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        choice = choices[0]
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]

        delta = choice.get("delta") or {}
        if delta.get("reasoning_content"):
            self.reasoning_parts.append(delta["reasoning_content"])
        for fragment in delta.get("tool_calls") or []:
            call = self.tool_calls.setdefault(fragment["index"], {
                "index": fragment["index"],
                "id": None,
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if fragment.get("id"):
                call["id"] = fragment["id"]
            if fragment.get("type"):
                call["type"] = fragment["type"]
            function = fragment.get("function") or {}
            if function.get("name"):
                call["function"]["name"] += function["name"]
            if function.get("arguments"):
                call["function"]["arguments"] += function["arguments"]

        text = delta.get("content") or ""
        if text:
            self.content_parts.append(text)
        return text

    def result(self) -> LLMResult:
        if self.finish_reason is None:
            # 连接中途断开时内容可能只有一半，不能当作完整回复使用。
            raise RuntimeError("模型流式响应未正常结束")
        content = "".join(self.content_parts)
        tool_calls = [self.tool_calls[index] for index in sorted(self.tool_calls)]
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if self.reasoning_parts:
            message["reasoning_content"] = "".join(self.reasoning_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls
        return LLMResult(
            content=content,
            tool_calls=tool_calls,
            assistant_message=message,
            inputTokens=self.usage.get("prompt_tokens"),
            outputTokens=self.usage.get("completion_tokens"),
        )


async def ask_llm(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str = "auto",
    *,
    on_delta: Callable[[str], Awaitable[None]] | None = None,
) -> LLMResult:
    """以流式读取模型输出；每收到一段回复文本就交给 on_delta，最后返回完整结果。"""

    config = get_model_config()
    base_url = config["base_url"]
    api_key = config["api_key"]
    model = config["model"]

    if not all([base_url, api_key, model]):
        raise ValueError("LLM 配置不全")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        # 让最后一个 chunk 带上 token 用量。
        "stream_options": {"include_usage": True},
        "thinking": {"type": "enabled"}
    }

    # 如果提供了工具，就加入请求体
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    accumulator = StreamAccumulator()
    # 流式读取时 timeout 约束的是相邻两次收到数据的间隔，而不是整次请求的总时长。
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", base_url, headers=headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                body = line[len("data:"):].strip()
                if body == "[DONE]":
                    break
                text = accumulator.add(json.loads(body))
                if text and on_delta is not None:
                    await on_delta(text)

    return accumulator.result()
