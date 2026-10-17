import json
from unittest.mock import AsyncMock

import httpx
import pytest

from devpilot.core.config import DevPilotConfig, _apply_env
from devpilot.core.events.bus import EventBus
from devpilot.core.llm.factory import create_provider
from devpilot.core.llm.openai_provider import OpenAICompatibleProvider, _messages


# 功能：验证限流后重试恢复以及持续失败时显示平台错误
# 设计：模拟连续限流并替换等待，检查重试上限且不发出网络请求
@pytest.mark.parametrize("recover", [True, False])
async def test_rate_limit_retry(monkeypatch, recover):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    sleep = AsyncMock()
    monkeypatch.setattr("devpilot.core.llm.openai_provider.asyncio.sleep", sleep)
    requests = []

    # 先返回限流，再按测试参数恢复或持续失败
    def handler(request):
        requests.append(request)
        if not recover or len(requests) == 1:
            return httpx.Response(429, json={"error": {"code": "test-limit",
                "message": "rate limit secret-key"}}, headers={"Retry-After": "3"})
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"},'
                              '"finish_reason":"stop"}]}\n\n')

    provider = OpenAICompatibleProvider("glm-4.7-flash", httpx.MockTransport(handler))
    if recover:
        result = await provider.chat([], [], EventBus(), "run-1")
        assert result.text == "ok"
        assert len(requests) == 2
        sleep.assert_awaited_once_with(3.0)
    else:
        with pytest.raises(RuntimeError, match="test-limit") as exc:
            await provider.chat([], [], EventBus(), "run-1")
        assert "secret-key" not in str(exc.value)
        assert len(requests) == 3


# 功能：验证流式工具参数拼接、用量统计和实际请求协议
# 设计：使用模拟 HTTP 传输检查请求并返回分片工具参数，无需真实密钥
async def test_stream_tool_call(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/api/paas/v4/")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    # 检查协议转换并构造工具调用事件流
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/api/paas/v4/chat/completions"
        assert body["thinking"] == {"type": "disabled"}
        assert body["tools"][0]["function"]["parameters"] == {"type": "object"}
        chunks = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                "function": {"name": "read_file", "arguments": '{"path":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
                "function": {"arguments": '"README.md"}'}}]},
                "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ]
        stream = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
        return httpx.Response(200, text=stream + "data: [DONE]\n\n")

    assert isinstance(create_provider("glm-4.7-flash"), OpenAICompatibleProvider)
    provider = OpenAICompatibleProvider("glm-4.7-flash", httpx.MockTransport(handler))
    response = await provider.chat(
        [{"role": "user", "content": "hello"}],
        [{"name": "read_file", "input_schema": {"type": "object"}}], EventBus(), "run-1",
    )
    assert response.stop_reason == "tool_use"
    assert response.tool_calls[0].input == {"path": "README.md"}
    assert response.usage.input_tokens == 10


# 功能：验证工具调用后的历史能转换为兼容接口格式
# 设计：覆盖内部 assistant 工具块和 user 工具结果，检查关联 ID 不丢失
def test_tool_history():
    result = _messages([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1",
            "name": "read_file", "input": {"path": "a"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
            "content": "contents"}]},
    ], "system")
    assert result[1]["tool_calls"][0]["id"] == "c1"
    assert result[2] == {"role": "tool", "tool_call_id": "c1", "content": "contents"}


# 功能：验证模型别名优先级和缺少兼容接口密钥时的提示
# 设计：隔离环境变量，防止错误复用旧平台的密钥
def test_config_and_missing_key(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "glm-4.7-flash")
    monkeypatch.setenv("DEVPILOT_LLM_DEFAULT_MODEL", "old-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = DevPilotConfig()
    _apply_env(config)
    assert config.llm.default_model == "glm-4.7-flash"
    with pytest.raises(SystemExit, match="OPENAI_API_KEY not set"):
        OpenAICompatibleProvider(config.llm.default_model)


# 功能：验证普通文本流成功返回及中途断流不会被误判为完成
# 设计：用相同文本片段搭配有无结束标记的响应，覆盖正常与异常路径
@pytest.mark.parametrize("complete", [True, False])
async def test_text_and_interrupted_stream(monkeypatch, complete):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")

    # 返回可控结束状态的文本事件流
    def handler(request):
        chunk = {"choices": [{"delta": {"content": "hello"},
                              "finish_reason": "stop" if complete else None}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\n")

    provider = OpenAICompatibleProvider("glm-4.7-flash", httpx.MockTransport(handler))
    if complete:
        result = await provider.chat([], [], EventBus(), "run-1")
        assert result.text == "hello"
        assert result.stop_reason == "end_turn"
    else:
        with pytest.raises(ValueError, match="Incomplete"):
            await provider.chat([], [], EventBus(), "run-1")
