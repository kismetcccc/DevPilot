import os

from devpilot.core.llm.base import LLMProvider
from devpilot.core.llm.openai_provider import OpenAICompatibleProvider
from devpilot.core.llm.provider import AnthropicProvider


# 设置兼容接口地址时使用 Chat Completions，否则保留 Anthropic 接口
def create_provider(model: str) -> LLMProvider:
    if os.environ.get("OPENAI_BASE_URL"):
        return OpenAICompatibleProvider(model)
    return AnthropicProvider(model)
