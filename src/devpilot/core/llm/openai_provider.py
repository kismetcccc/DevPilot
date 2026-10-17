from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from devpilot.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from devpilot.core.events.bus import EventBus
from devpilot.core.llm.provider import _SYSTEM_PROMPT, _now
from devpilot.core.llm.types import LlmResponse, ToolCallBlock, UsageStats


# 对限流和服务暂时不可用执行有限重试，并保留平台错误详情
@asynccontextmanager
async def _stream(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any], key: str,
) -> AsyncIterator[httpx.Response]:
    for attempt in range(3):
        delay = float(2 ** attempt)
        async with client.stream("POST", url, json=payload, headers={
            "Authorization": f"Bearer {key}",
        }) as response:
            if not response.is_error:
                yield response
                return
            await response.aread()
            try:
                error = response.json().get("error", {})
                detail = str(error).replace(key, "[REDACTED]")[:500]
            except (ValueError, AttributeError):
                detail = "Non-JSON error response"
            retry = response.status_code in (429, 500, 502, 503, 504)
            if not retry or attempt == 2:
                raise RuntimeError(f"Model API HTTP {response.status_code}: {detail}")
            try:
                delay = min(30.0, max(delay, float(response.headers.get("Retry-After", "0"))))
            except ValueError:
                pass
            logging.getLogger(__name__).warning(
                "Model API HTTP %s; retry %s/2 in %.1fs: %s",
                response.status_code, attempt + 1, delay, detail,
            )
        await asyncio.sleep(delay)


# 将内部内容块转换为 Chat Completions 消息及工具结果
def _messages(messages: list[dict[str, object]], system: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            result.append({"role": message["role"], "content": content})
            continue
        if not isinstance(content, list):
            raise ValueError("Unsupported message content")
        texts, calls, tool_results = [], [], []
        for block in content:
            kind = block.get("type")
            if kind == "text":
                texts.append(block["text"])
            elif kind == "tool_use":
                calls.append({"id": block["id"], "type": "function", "function": {
                    "name": block["name"], "arguments": json.dumps(block["input"]),
                }})
            elif kind == "tool_result":
                value = block.get("content", "")
                tool_results.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                                     "content": value if isinstance(value, str)
                                     else json.dumps(value, ensure_ascii=False)})
            elif kind not in ("thinking", "redacted_thinking"):
                raise ValueError(f"Unsupported content block: {kind}")
        result.extend(tool_results)
        if texts or calls:
            item: dict[str, Any] = {"role": message["role"], "content": "\n".join(texts)}
            if calls:
                item["tool_calls"] = calls
            result.append(item)
    return result


class OpenAICompatibleProvider:
    # 读取兼容接口配置，允许测试注入 HTTP 传输层
    def __init__(self, model: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._key = os.environ.get("OPENAI_API_KEY", "")
        if not self._key:
            raise SystemExit("OPENAI_API_KEY not set")
        self._url = os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions"
        self._model = model
        self._transport = transport

    # 解析流式文本和分片工具参数，发布事件并返回统一模型响应
    async def chat(
        self, messages: list[dict[str, object]], tool_schemas: list[dict[str, object]],
        bus: EventBus, run_id: str, *, step: int = 0, system: str | None = None,
    ) -> LlmResponse:
        payload: dict[str, Any] = {
            "model": self._model, "messages": _messages(messages, system or _SYSTEM_PROMPT),
            "stream": True, "max_tokens": 8192,
        }
        # GLM 使用非思考模式，避免工具续轮依赖未保存的 reasoning_content
        if self._model.lower().startswith("glm-"):
            payload["thinking"] = {"type": "disabled"}
        if tool_schemas:
            payload["tools"] = [{"type": "function", "function": {
                "name": schema["name"], "description": schema.get("description", ""),
                "parameters": schema["input_schema"],
            }} for schema in tool_schemas]
        await bus.publish(LlmModelSelectedEvent(
            run_id=run_id, model=self._model, strategy="static", ts=_now(),
        ))
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage: dict[str, Any] = {}
        finish = None
        async with httpx.AsyncClient(transport=self._transport, timeout=120.0) as client:
            async with _stream(client, self._url, payload, self._key) as response:
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    if not data:
                        continue
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        raise ValueError("Model API returned a stream error")
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        finish = choice.get("finish_reason") or finish
                        delta = choice.get("delta", {})
                        token = delta.get("content")
                        if token:
                            text_parts.append(token)
                            await bus.publish(LlmTokenEvent(
                                run_id=run_id, token=token, ts=_now(),
                            ))
                        for fragment in delta.get("tool_calls") or []:
                            call = calls.setdefault(fragment["index"], {
                                "id": "", "name": "", "arguments": "",
                            })
                            if fragment.get("id"):
                                call["id"] = fragment["id"]
                            function = fragment.get("function") or {}
                            for key in ("name", "arguments"):
                                call[key] += function.get(key) or ""
        if finish not in ("stop", "tool_calls", "length"):
            raise ValueError(f"Incomplete or unsupported model response: {finish}")
        tool_calls = []
        for _, call in sorted(calls.items()):
            arguments = json.loads(call["arguments"] or "{}")
            if not call["id"] or not call["name"] or not isinstance(arguments, dict):
                raise ValueError("Invalid model tool call")
            tool_calls.append(ToolCallBlock(call["id"], call["name"], arguments))
        stats = None
        if usage:
            stats = UsageStats(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            )
            await bus.publish(LlmUsageEvent(
                run_id=run_id, input_tokens=stats.input_tokens,
                output_tokens=stats.output_tokens, cache_read_input_tokens=0,
                cache_creation_input_tokens=0, context_pct=stats.context_pct, ts=_now(),
            ))
        return LlmResponse(
            stop_reason="max_tokens" if finish == "length" else
            "tool_use" if tool_calls else "end_turn",
            tool_calls=tool_calls, text="".join(text_parts), usage=stats,
        )
