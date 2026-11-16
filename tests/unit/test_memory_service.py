from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from devpilot.core.config import DevPilotConfig, MemoryConfig, _apply_memory, get_config
from devpilot.core.context import ExecutionContext
from devpilot.core.llm.types import LlmResponse
from devpilot.core.memory.models import MemoryFact
from devpilot.core.memory.service import MemoryService
from devpilot.core.memory.store import PostgresMemoryStore
from devpilot.core.runner import AgentRunner


# 创建无网络的记忆服务并替换全部存储操作
def _service() -> MemoryService:
    return MemoryService(MagicMock(spec=PostgresMemoryStore), MemoryConfig())


# 创建带原文证据的标准语言偏好候选
def _fact(quote: str, intent: str = "stable") -> MemoryFact:
    return MemoryFact.model_validate(
        {
            "key": "preferred_language",
            "value": "中文",
            "quote": quote,
            "intent": intent,
        }
    )


# 功能：验证临时要求即使被模型误标也不能修改长期画像
# 设计：覆盖中英文临时表达，以原文校验作为模型输出之外的独立防线
@pytest.mark.parametrize(
    "quote", ["这次请用英文", "For this task I prefer English", "今天详细解释"]
)
def test_temporary_never_becomes_profile(quote: str) -> None:
    fact = _service().validate_fact(_fact(quote, "correction"), quote)
    assert fact is not None and fact.intent == "temporary"


# 功能：验证凭空生成的证据和凭据内容不会进入记忆
# 设计：分别构造原文缺失和敏感内容，检查都在数据库写入前被拒绝
def test_invalid_evidence_and_credentials_rejected() -> None:
    service = _service()
    assert service.validate_fact(_fact("我默认中文"), "运行测试") is None
    assert service.validate_fact(_fact("我的密码是示例"), "我的密码是示例") is None


# 功能：验证模糊陈述和缺少更正标记的冲突只作为候选
# 设计：给模型错误标记的输入，确认规则层能够降低其可信状态
def test_unclear_statement_is_candidate() -> None:
    service = _service()
    assert service.validate_fact(_fact("中文"), "中文").intent == "inferred"
    assert (
        service.validate_fact(_fact("我喜欢中文", "correction"), "我喜欢中文").intent == "inferred"
    )
    assert (
        service.validate_fact(_fact("以后默认中文", "correction"), "以后默认中文").intent
        == "correction"
    )


# 功能：验证常见字段别名归一化，项目规则和假设内容不会自动成为画像
# 设计：覆盖同义字段和明确的非个人事实，减少重复字段与上下文污染
def test_canonical_keys_and_project_statements() -> None:
    assert MemoryFact(key="language", value="中文", quote="我喜欢中文").key == "preferred_language"
    service = _service()
    for text in ("本项目默认中文", "例如我喜欢中文"):
        assert service.validate_fact(_fact(text), text).intent == "inferred"
    assert service.validate_fact(_fact("我喜欢英文"), "我喜欢英文") is None


# 功能：验证提取调用只接收用户原始消息且临时要求仅保存到短期层
# 设计：使用模拟 provider 截获消息，直接断言数据库画像写入从未发生
async def test_capture_only_original_user_message() -> None:
    service = _service()
    original = "这次请用英文"
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(
            stop_reason="end_turn",
            text=json.dumps({"facts": [_fact(original).model_dump()]}),
        )
    )
    await service.capture(original, "session", "message", provider)
    assert provider.chat.call_args.kwargs["messages"] == [{"role": "user", "content": original}]
    service.store.observe.assert_not_called()
    service.store.save_short.assert_awaited_once()


# 功能：验证提取格式错误和网络异常不会阻止任务继续
# 设计：模拟非 JSON 输出和 provider 异常，确认均不落库且不向外抛异常
@pytest.mark.parametrize("invalid", ["not json", '{"facts":[{"key":"made.up"}]}'])
async def test_capture_invalid_output_is_safe(invalid: str) -> None:
    service = _service()
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text=invalid))
    await service.capture("我默认中文", "s", "m", provider)
    provider.chat.side_effect = RuntimeError("network unavailable")
    await service.capture("我默认中文", "s", "m2", provider)
    service.store.observe.assert_not_called()


# 功能：验证关闭自动提取后不会调用模型，管理画像仍然可用
# 设计：直接切换配置，检查捕获过程没有任何 provider 或存储调用
async def test_auto_extract_can_be_disabled() -> None:
    service = _service()
    service.config.auto_extract = False
    provider = MagicMock()
    await service.capture("我默认中文", "s", "m", provider)
    provider.chat.assert_not_called()


# 功能：验证记忆装配遵守长度预算且不会从中间截断条目
# 设计：将大条目与小条目混合，逐行解析输出，验证仍是完整的结构化数据
async def test_snapshot_budget_and_order() -> None:
    service = _service()
    service.config.context_max_chars = 180
    service.store.profiles.return_value = [
        {"key": "a", "value": "中文"},
        {"key": "b", "value": "x" * 1000},
    ]
    service.store.short_term.return_value = [
        {"key": "n", "content": "本次英文", "expires_at": datetime.now(UTC)},
    ]
    profile, notes = await service.snapshot("s")
    assert len(profile) + len(notes) <= 180
    assert json.loads(profile)["value"] == "中文"
    assert json.loads(notes)["note"] == "本次英文"


# 功能：验证空笔记也有保存入口，并明确当前请求优先于历史偏好
# 设计：直接检查空记忆上下文的提示，避免首次使用永远无法产生笔记
def test_empty_memory_has_save_rules() -> None:
    context = ExecutionContext("r", "g", 2, memory_enabled=True)
    prompt = context.system_prompt("base")
    assert "note_save" in prompt
    assert "Current user instructions override" in prompt


# 功能：验证数据库密码不会出现在配置对象日志中
# 设计：以测试连接串创建配置并检查 repr，同时校验非法数据库后端被拒绝
def test_memory_config_hides_dsn_and_rejects_invalid_values() -> None:
    config = DevPilotConfig(memory=MemoryConfig(dsn="postgresql://u:private@localhost/db"))
    assert "private" not in repr(config)
    for data in (
        {"dsn": "sqlite:///db"},
        {"pool_max_size": 0},
        {"user_id": " "},
        {"short_term_ttl_hours": True},
        {"auto_extract": "false"},
        {"extra": 1},
    ):
        with pytest.raises(SystemExit):
            _apply_memory(config.memory, data)


# 功能：验证环境变量覆盖 TOML 中的记忆配置且类型正确
# 设计：在独立目录生成配置，消除用户实际 .env 干扰
def test_memory_config_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[memory]\nuser_id="file-user"\nshort_term_ttl_hours=12\n')
    monkeypatch.setenv("DEVPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("DEVPILOT_MEMORY_USER_ID", "env-user")
    monkeypatch.setenv("DEVPILOT_MEMORY_AUTO_EXTRACT", "false")
    config = get_config()
    assert config.memory.user_id == "env-user"
    assert config.memory.short_term_ttl_hours == 12
    assert config.memory.auto_extract is False


# 功能：验证运行期间数据库故障时主任务继续，并发布可见警告
# 设计：只让记忆读取失败，检查 runner 仍获得主模型结果，且不会静默假装记忆可用
async def test_memory_outage_does_not_abort_task(tmp_path: Path) -> None:
    service = _service()
    service.config.auto_extract = False
    service.store.profiles.side_effect = RuntimeError("database disconnected")
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="task done"))
    events = []

    # 收集运行事件以验证故障提示对界面可见
    async def collect(event: object) -> None:
        events.append(event)

    outcome = await AgentRunner(
        DevPilotConfig(),
        provider=provider,
        memory=service,
        runs_dir=tmp_path,
        extra_handlers=[collect],
    ).run_and_capture("普通任务")
    assert outcome.status == "success" and outcome.result == "task done"
    assert any(getattr(event, "source", "") == "memory" for event in events)
