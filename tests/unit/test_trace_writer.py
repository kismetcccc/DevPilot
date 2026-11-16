import asyncio
import json
from pathlib import Path

import pytest

from devpilot.core.trace.record import TraceRecord
from devpilot.core.trace.writer import TraceWriter


def _record(direction: str = "CORE", kind: str = "event") -> TraceRecord:
    return TraceRecord(
        ts="2026-01-01T00:00:00.000Z",
        direction=direction,  # type: ignore[arg-type]
        layer="event",
        kind=kind,
        data={"type": "run.started", "run_id": "r1"},
    )


# 功能：验证 emit 后 stop 能将 record 写入文件
# 设计：用临时目录避免污染；await stop() 保证 drain 完成后再读文件
@pytest.mark.asyncio
async def test_emit_writes_record_to_file(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()

    writer.emit(_record())
    await writer.stop()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["direction"] == "CORE"
    assert parsed["kind"] == "event"


# 功能：验证多条 record 按 emit 顺序写入文件
# 设计：emit 三条方向各异的 record，断言顺序与方向均保持一致
@pytest.mark.asyncio
async def test_emit_multiple_records_in_order(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()

    writer.emit(_record("CLIENT→CORE", "command"))
    writer.emit(_record("CORE", "event"))
    writer.emit(_record("LLM→CORE", "api_response"))
    await writer.stop()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["direction"] == "CLIENT→CORE"
    assert json.loads(lines[1])["direction"] == "CORE"
    assert json.loads(lines[2])["direction"] == "LLM→CORE"


# 功能：验证 emit 是同步非阻塞的（不需要 await）
# 设计：在 start() 之前调用 emit 会放入队列而不抛异常，start 后正常 drain
@pytest.mark.asyncio
async def test_emit_is_nonblocking(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()

    # emit 是同步调用，不应阻塞事件循环
    for _ in range(10):
        writer.emit(_record())
    await writer.stop()

    assert len(path.read_text(encoding="utf-8").splitlines()) == 10


# 功能：验证 TraceWriter 自动创建不存在的父目录
# 设计：指定一个深层嵌套路径，start() 后 emit 能正常写入
@pytest.mark.asyncio
async def test_start_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "c" / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()
    writer.emit(_record())
    await writer.stop()

    assert path.exists()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


# 功能：验证 stop 后再次 start 可以追加写入（文件已存在时）
# 设计：两次 start/stop 循环，断言文件行数累加而非覆盖
@pytest.mark.asyncio
async def test_append_mode_on_restart(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"

    writer = TraceWriter(path)
    await writer.start()
    writer.emit(_record())
    await writer.stop()

    writer2 = TraceWriter(path)
    await writer2.start()
    writer2.emit(_record())
    await writer2.stop()

    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


# 功能：验证中文及表情日志始终以 UTF-8 写入，不依赖 Windows 系统区域设置
# 设计：使用默认中文编码也不能表示的表情，直接检查文件字节以捕获隐式编码
async def test_trace_always_uses_utf8(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()
    writer.emit(_record("CLIENT→CORE").model_copy(update={"data": {"text": "用户画像🚀"}}))
    await asyncio.wait_for(writer.stop(), timeout=1)
    assert "用户画像🚀".encode() in path.read_bytes()


# 功能：验证后台日志打开失败时 stop 能及时抛出原始异常而不会永久等待
# 设计：将目录当作目标文件触发真实 I/O 错误，超时保护防止测试本身挂起
async def test_trace_failure_does_not_deadlock_shutdown(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path)
    await writer.start()
    writer.emit(_record())
    with pytest.raises(OSError):
        await asyncio.wait_for(writer.stop(), timeout=1)
