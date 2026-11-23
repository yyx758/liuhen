import json
import unittest
from unittest.mock import patch

import httpx

from backend.app.agent import llm


def sse_lines(chunks: list[dict], done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def delta_chunk(delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> dict:
    return {
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        "usage": usage,
    }


class AskLLMStreamTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, body: bytes, **kwargs) -> llm.LLMResult:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        real_client = httpx.AsyncClient

        def client_factory(**client_kwargs):
            return real_client(transport=httpx.MockTransport(handler), **client_kwargs)

        config = {"base_url": "https://llm.test/chat", "api_key": "test-key", "model": "test-model"}
        with (
            patch.object(llm, "get_model_config", return_value=config),
            patch.object(llm.httpx, "AsyncClient", side_effect=client_factory),
        ):
            result = await llm.ask_llm([{"role": "user", "content": "hi"}], **kwargs)
        self.payload = captured["payload"]
        return result

    async def test_text_reply_streams_deltas_and_assembles_message(self) -> None:
        body = sse_lines([
            delta_chunk({"role": "assistant", "content": None, "reasoning_content": ""}),
            delta_chunk({"content": None, "reasoning_content": "想一想"}),
            delta_chunk({"content": "你好"}),
            delta_chunk({"content": "，世界"}),
            delta_chunk({"content": ""}, "stop", {"prompt_tokens": 10, "completion_tokens": 4}),
        ])
        received = []

        async def on_delta(text: str) -> None:
            received.append(text)

        result = await self.ask(body, on_delta=on_delta)

        self.assertTrue(self.payload["stream"])
        self.assertEqual(received, ["你好", "，世界"])
        self.assertEqual(result.content, "你好，世界")
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(result.assistant_message, {
            "role": "assistant", "content": "你好，世界", "reasoning_content": "想一想",
        })
        self.assertEqual((result.inputTokens, result.outputTokens), (10, 4))

    async def test_tool_call_fragments_are_merged_by_index(self) -> None:
        body = sse_lines([
            delta_chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                         "function": {"name": "get_task", "arguments": ""}}]}),
            delta_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"task_'}}]}),
            delta_chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                                         "function": {"name": "list_tasks", "arguments": "{}"}}]}),
            delta_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'id": 7}'}}]}),
            delta_chunk({"content": ""}, "tool_calls"),
        ])

        result = await self.ask(body)

        self.assertEqual([call["id"] for call in result.tool_calls], ["call_a", "call_b"])
        self.assertEqual(json.loads(result.tool_calls[0]["function"]["arguments"]), {"task_id": 7})
        self.assertEqual(result.tool_calls[1]["function"]["name"], "list_tasks")
        self.assertEqual(result.assistant_message["tool_calls"], result.tool_calls)
        self.assertEqual(result.content, "")

    async def test_stream_without_finish_reason_is_rejected(self) -> None:
        body = sse_lines([delta_chunk({"content": "说到一半"})], done=False)

        with self.assertRaises(RuntimeError):
            await self.ask(body)


if __name__ == "__main__":
    unittest.main()
