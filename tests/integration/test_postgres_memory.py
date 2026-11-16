from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from psycopg import sql

from devpilot.core.config import DevPilotConfig, MemoryConfig
from devpilot.core.events.bus import EventBus
from devpilot.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from devpilot.core.memory.models import MemoryFact
from devpilot.core.memory.service import MemoryService
from devpilot.core.memory.store import PostgresMemoryStore
from devpilot.core.runner import AgentRunner
from devpilot.core.session.manager import SessionManager
from devpilot.core.session.model import Session
from devpilot.core.session.store import SessionStore


@pytest.fixture
# 为每个测试建立独立用户命名空间，退出时仅清理本测试写入的数据
async def memory() -> AsyncIterator[MemoryService]:
    dsn = os.environ.get("DEVPILOT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set DEVPILOT_TEST_POSTGRES_DSN to run real PostgreSQL tests")
    config = MemoryConfig(dsn=dsn, user_id=f"test-memory-{uuid.uuid4().hex}")
    store = PostgresMemoryStore(dsn)
    await store.open()
    try:
        yield MemoryService(store, config)
    finally:
        with psycopg.connect(dsn) as conn:
            for table in ("profiles", "observations", "profile_history", "short_term"):
                conn.execute(
                    sql.SQL("DELETE FROM devpilot_memory.{} WHERE user_id LIKE %s").format(
                        sql.Identifier(table)
                    ),
                    (config.user_id + "%",),
                )
        await store.close()


# 创建标准画像事实用于验证数据库状态机
def _fact(value: str = "中文", intent: str = "stable") -> MemoryFact:
    return MemoryFact.model_validate(
        {
            "key": "preferred_language",
            "value": value,
            "quote": f"以后默认{value}",
            "intent": intent,
        }
    )


# 功能：验证正式画像跨会话读取且重复证据不增加版本
# 设计：使用真实数据库查询当前值和版本历史，避免模拟存储掩盖唯一约束错误
async def test_profile_persists_across_sessions_and_deduplicates(memory: MemoryService) -> None:
    store, user = memory.store, memory.config.user_id
    assert await store.observe(user, "s1", "m1", _fact()) == "accepted"
    assert await store.observe(user, "s1", "m1", _fact()) == "duplicate"
    assert await store.observe(user, "s2", "m2", _fact()) == "accepted"
    profile, _ = await memory.snapshot("s3")
    assert "中文" in profile
    assert (await store.profiles(user))[0]["version"] == 1
    assert len(await store.history(user, "preferred_language")) == 1


# 功能：验证冲突偏好保持旧值，重复候选合并，用户确认后更新
# 设计：分别检查候选队列与正式画像，并尝试再次确认已经失效的候选
async def test_conflict_is_pending_until_confirmed(memory: MemoryService) -> None:
    store, user = memory.store, memory.config.user_id
    await store.observe(user, "s1", "m1", _fact())
    await store.observe(user, "s2", "m2", _fact("English"))
    await store.observe(user, "s3", "m3", _fact("English"))
    assert (await store.profiles(user))[0]["value"] == "中文"
    candidates = await store.profiles(user, pending=True)
    assert len(candidates) == 1
    candidate_id = str(candidates[0]["id"])
    await store.confirm(user, "s3", candidate_id)
    assert (await store.profiles(user))[0]["value"] == "English"
    assert (await store.profiles(user))[0]["source_session"] == "s2"
    with pytest.raises(ValueError):
        await store.confirm(user, "s3", candidate_id)


# 功能：验证明确更正立即更新，同时锁定阻止自动更新
# 设计：按版本检查观察、更正、锁定和解锁后的完整状态流转
async def test_explicit_correction_and_lock(memory: MemoryService) -> None:
    store, user = memory.store, memory.config.user_id
    await store.observe(user, "s", "m1", _fact())
    await store.observe(user, "s", "m2", _fact("English", "correction"))
    assert (await store.profiles(user))[0]["value"] == "English"
    await store.edit(user, "s", "preferred_language", "lock")
    await store.observe(user, "s", "m3", _fact("日本語", "correction"))
    assert (await store.profiles(user))[0]["value"] == "English"
    await store.edit(user, "s", "preferred_language", "unlock")
    await store.observe(user, "s", "m4", _fact("中文", "correction"))
    assert (await store.profiles(user))[0]["value"] == "中文"
    assert len(await store.history(user, "preferred_language")) == 5


# 功能：验证反复推测和重复处理不会自动提升为正式画像
# 设计：提供多个独立来源的相同推测，检查始终只有一个待确认条目
async def test_inference_never_auto_promotes(memory: MemoryService) -> None:
    for index in range(6):
        await memory.store.observe(memory.config.user_id, "s", str(index), _fact(intent="inferred"))
    assert await memory.store.profiles(memory.config.user_id) == []
    assert len(await memory.store.profiles(memory.config.user_id, pending=True)) == 1


# 功能：验证相同偏好不会制造锁定冲突，手动确认新值后仍保持锁定
# 设计：覆盖锁定字段的重复证据和主动变更，防止确认操作隐式解除保护
async def test_manual_confirmation_preserves_lock(memory: MemoryService) -> None:
    store, user = memory.store, memory.config.user_id
    await store.observe(user, "s", "m1", _fact())
    await store.edit(user, "s", "preferred_language", "lock")
    await store.observe(user, "s", "m2", _fact())
    assert await store.profiles(user, pending=True) == []
    await store.observe(user, "s", "m3", _fact("English", "correction"))
    candidate = (await store.profiles(user, pending=True))[0]
    await store.confirm(user, "s", str(candidate["id"]))
    profile = (await store.profiles(user))[0]
    assert profile["value"] == "English" and profile["locked"] is True


# 功能：验证删除后重放旧证据或重新提取都不能自动恢复画像
# 设计：检查墓碑、被清空的历史值和用户主动重建三个行为
async def test_forget_prevents_resurrection(memory: MemoryService) -> None:
    store, user = memory.store, memory.config.user_id
    await store.observe(user, "s", "m1", _fact())
    await store.edit(user, "s", "preferred_language", "forget")
    await store.observe(user, "s", "m1", _fact())
    await store.observe(user, "s", "m2", _fact(intent="correction"))
    assert await store.profiles(user) == []
    assert all(row["value"] == "" for row in await store.history(user, "preferred_language"))
    await store.edit(user, "s", "preferred_language", "set", "English")
    assert (await store.profiles(user))[0]["value"] == "English"
    assert await store.profiles(user, pending=True) == []


# 功能：验证相同字段并发首次写入只创建一个正式版本
# 设计：通过真实连接池同时发起多条观察，覆盖没有现成记录时的竞争条件
async def test_concurrent_profile_creation_is_atomic(memory: MemoryService) -> None:
    await asyncio.gather(
        *(memory.store.observe(memory.config.user_id, "s", f"m{i}", _fact()) for i in range(12))
    )
    profiles = await memory.store.profiles(memory.config.user_id)
    assert len(profiles) == 1 and profiles[0]["version"] == 1


# 功能：验证画像事务中途失败时当前版本和历史都会回滚
# 设计：在历史写入后注入异常，检查同一连接事务没有留下半次变更
async def test_profile_transaction_rolls_back(
    memory: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, user = memory.store, memory.config.user_id
    original = store._write

    # 在画像及历史写入后模拟中断
    def fail_after_write(*args: object, **kwargs: object) -> object:
        original(*args, **kwargs)
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(store, "_write", fail_after_write)
    with pytest.raises(RuntimeError, match="simulated"):
        await store.observe(user, "s", "m", _fact())
    assert await store.profiles(user) == []
    assert await store.history(user, "preferred_language") == []
    assert await store.profiles(user, pending=True) == []


# 功能：验证短期笔记按用户、项目、会话隔离，重复写入不延长有效期
# 设计：比较数据库时间并从三个错误作用域读取，防止仅依赖提示词隔离
async def test_short_term_scope_and_idempotency(memory: MemoryService) -> None:
    store, user, project = memory.store, memory.config.user_id, memory.project_id
    await memory.save_note("s1", "这次使用英文", "m1")
    before = (await store.short_term(user, project, "s1"))[0]["expires_at"]
    await memory.save_note("s1", "这次使用英文", "m1")
    rows = await store.short_term(user, project, "s1")
    assert len(rows) == 1 and rows[0]["expires_at"] == before
    assert await store.short_term(user, project, "s2") == []
    assert await store.short_term(user, "other-project", "s1") == []
    assert await store.short_term(user + "-other", project, "s1") == []
    assert await store.profiles(user + "-other") == []


# 功能：验证过期和清空的短期笔记不会被旧文件导入重新激活
# 设计：以数据库时间制造过期，再执行真实的旧笔记导入和清空流程
async def test_short_term_expiry_clear_and_legacy_import(memory: MemoryService) -> None:
    user, project = memory.config.user_id, memory.project_id
    legacy = "## Note (2026-01-01, old-run)\n保留当前任务进度\n\n"
    await memory.import_notes("s", legacy)
    assert len(await memory.store.short_term(user, project, "s")) == 1
    with memory.store._pool.connection() as conn:
        conn.execute(
            "UPDATE devpilot_memory.short_term SET expires_at=now()-interval '1 second' "
            "WHERE user_id=%s",
            (user,),
        )
    await memory.import_notes("s", legacy)
    assert await memory.store.short_term(user, project, "s") == []
    await memory.save_note("s", "新进度", "m2")
    await memory.store.clear_short(user, project, "s")
    await memory.save_note("s", "新进度", "m2")
    assert await memory.store.short_term(user, project, "s") == []
    assert await memory.store.profiles(user) == []


# 功能：验证数据库连接关闭并重建后画像和未过期笔记仍然存在
# 设计：使用新的连接池实例读取，排除服务内存缓存造成的假持久化
async def test_database_reconnect_preserves_memory(memory: MemoryService) -> None:
    await memory.store.observe(memory.config.user_id, "s", "m", _fact())
    await memory.save_note("s", "记得当前任务", "m")
    other = PostgresMemoryStore(memory.config.dsn)
    await other.open()
    try:
        restored = MemoryService(other, memory.config)
        profile, notes = await restored.snapshot("s")
        assert "中文" in profile and "当前任务" in notes
    finally:
        await other.close()


# 功能：验证临时语言要求不会影响新会话中的长期语言偏好
# 设计：串联真实提取、PostgreSQL 和上下文装配，只模拟模型输出
async def test_temporary_override_does_not_change_profile(memory: MemoryService) -> None:
    await memory.store.observe(memory.config.user_id, "s1", "m1", _fact())
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(
            stop_reason="end_turn",
            text='{"facts":[{"key":"preferred_language","value":"English",'
            '"quote":"这次请用英文","intent":"correction"}]}',
        )
    )
    await memory.capture("这次请用英文", "s1", "m2", provider)
    profile, notes = await memory.snapshot("s2")
    assert "中文" in profile and not notes
    assert "这次请用英文" in (await memory.snapshot("s1", "m2"))[1]
    assert not (await memory.snapshot("s1", "next-message"))[1]


# 功能：验证自动压缩后新的最终回答仍正确持久化，画像不会被摘要内容改写
# 设计：使用多轮历史和真实 runner，模拟工具、压缩、最终回答三个模型响应
async def test_compaction_persists_new_messages(memory: MemoryService, tmp_path: Path) -> None:
    config = DevPilotConfig(memory=memory.config)
    config.memory.auto_extract = False
    config.compaction.auto_threshold = 0.5
    store = SessionStore(tmp_path)
    session = Session("sess-111111111111", "chat", "active", "", "", "")
    store.write_meta(session)
    for i in range(5):
        store.append_message(session.id, "user", "旧历史" * 100)
        store.append_message(session.id, "assistant", "旧回复" * 100)
    await memory.store.observe(memory.config.user_id, session.id, "m1", _fact())
    provider = MagicMock()
    provider.chat = AsyncMock(
        side_effect=[
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[ToolCallBlock("t", "unknown", {})],
                usage=UsageStats(2000, 100, context_pct=0.9),
            ),
            LlmResponse(stop_reason="end_turn", text="摘要说我喜欢英文，但没有用户原文证据"),
            LlmResponse(stop_reason="end_turn", text="压缩后的最终回答"),
        ]
    )
    result = await AgentRunner(config, provider=provider, memory=memory).run_and_capture(
        "继续",
        session=session,
        store=store,
    )
    assert result.status == "success"
    messages = store.read_messages(session.id)
    assert "最终回答" in str(messages[-1])
    assert len(messages) == 3
    assert (await memory.store.profiles(memory.config.user_id))[0]["value"] == "中文"


# 功能：验证 Core 管理器重建后可以恢复关闭会话，且拒绝跨用户或项目读取
# 设计：通过真实元数据文件恢复索引，覆盖重启路径和身份边界
async def test_session_restart_resume_and_identity(memory: MemoryService, tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(
        store, MagicMock(), EventBus(), user_id=memory.config.user_id, project_id=memory.project_id
    )
    session = await manager.create("chat")
    await manager.close(session.id)
    restored = SessionManager(
        store, MagicMock(), EventBus(), user_id=memory.config.user_id, project_id=memory.project_id
    )
    assert (await restored.resume(session.id)).status == "waiting_for_input"
    other = SessionManager(store, MagicMock(), EventBus(), user_id="other", project_id="other")
    from devpilot.core.bus.envelope import HandlerError

    with pytest.raises(HandlerError):
        await other.get_history(session.id)
