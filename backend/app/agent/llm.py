
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


async def ask_llm(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str = "auto"
) -> LLMResult:

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
        "stream": False,
        "thinking": {"type": "enabled"}
    }

    # 如果提供了工具，就加入请求体
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            base_url,
            headers=headers,
            json=payload
        )

        response.raise_for_status()
        data = response.json()

    message = data["choices"][0]["message"]
    usage = data.get("usage") or {}

    return LLMResult(
        content=message.get("content") or "",
        tool_calls=message.get("tool_calls") or [],
        assistant_message=message,
        inputTokens=usage.get("prompt_tokens"),
        outputTokens=usage.get("completion_tokens")
    )
