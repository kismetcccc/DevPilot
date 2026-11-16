from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from devpilot.core.memory.service import MemoryService
from devpilot.core.transport.socket_client import SocketClient
from tests.integration.test_postgres_memory import memory as memory


# 功能：验证独立 Core 通过真实 HTTP、TCP 和 PostgreSQL 完成记忆并在重启后恢复
# 设计：仅替换外部模型响应，保留生产配置、连接池、存储、协议和 runner 全链路
async def test_real_core_process_memory_and_restart(
    memory: MemoryService,
    tmp_path: Path,
    free_port: int,
) -> None:
    captured_systems: list[str] = []

    # 提供本地流式模型响应，避免真实模型费用与不确定性
    async def model_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode()
            length = int(
                next(
                    line.split(":", 1)[1]
                    for line in header.splitlines()
                    if line.lower().startswith("content-length:")
                )
            )
            payload = json.loads(await reader.readexactly(length))
            system = payload["messages"][0]["content"]
            original = payload["messages"][-1]["content"]
            if system.startswith("Extract user memory"):
                facts = []
                if original == "我默认使用中文":
                    facts = [
                        {
                            "key": "preferred_language",
                            "value": "中文",
                            "quote": original,
                            "intent": "stable",
                        }
                    ]
                elif original == "这次请用英文":
                    facts = [
                        {
                            "key": "preferred_language",
                            "value": "English",
                            "quote": original,
                            "intent": "correction",
                        }
                    ]
                text = json.dumps({"facts": facts}, ensure_ascii=False)
            else:
                captured_systems.append(system)
                text = "Task completed."
            chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}]}
            body = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
            writer.write(
                (
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    model_server = await asyncio.start_server(model_http, "127.0.0.1", 0)
    model_port = model_server.sockets[0].getsockname()[1]
    env = os.environ.copy()
    env.update(
        {
            "DEVPILOT_PORT": str(free_port),
            "DEVPILOT_HOST": "127.0.0.1",
            "DEVPILOT_MEMORY_DSN": memory.config.dsn,
            "DEVPILOT_MEMORY_USER_ID": memory.config.user_id,
            "DEVPILOT_MEMORY_AUTO_EXTRACT": "true",
            "DEVPILOT_SESSIONS_DIR": str(tmp_path / "sessions"),
            "DEVPILOT_TRACE_ENABLED": "false",
            "DEVPILOT_LOG_FILE": "",
            "DEVPILOT_CONFIG": str(tmp_path / "missing.toml"),
            "OPENAI_API_KEY": "local-test-key",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{model_port}",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    session_id = ""
    log_path = tmp_path / "core.log"
    try:
        for restart in range(2):
            with log_path.open("ab") as log_file:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "devpilot.core"],
                    env=env,
                    stdout=log_file,
                    stderr=log_file,
                )
            client = SocketClient("127.0.0.1", free_port)
            loop_task = None
            try:
                for _ in range(100):
                    if proc.poll() is not None:
                        pytest.fail("Core exited: " + log_path.read_text(encoding="utf-8"))
                    try:
                        await client.connect()
                        break
                    except OSError:
                        await asyncio.sleep(0.1)
                else:
                    pytest.fail("Core startup timed out")
                loop_task = asyncio.create_task(client.run_event_loop())
                if restart == 0:
                    created = await client.send_command("session.create", {"mode": "chat"})
                    session_id = created["session_id"]
                    await asyncio.wait_for(
                        client.send_command(
                            "session.send_message",
                            {
                                "session_id": session_id,
                                "content": "我默认使用中文",
                            },
                        ),
                        timeout=15,
                    )
                    await memory.save_note(session_id, "当前进度：等待验收", "manual-test")
                    await asyncio.wait_for(
                        client.send_command(
                            "session.send_message",
                            {
                                "session_id": session_id,
                                "content": "这次请用英文",
                            },
                        ),
                        timeout=15,
                    )
                    assert "这次请用英文" in captured_systems[-1]
                    await client.send_command("session.close", {"session_id": session_id})
                else:
                    await client.send_command("session.resume", {"session_id": session_id})
                    await asyncio.wait_for(
                        client.send_command(
                            "session.send_message",
                            {
                                "session_id": session_id,
                                "content": "继续",
                            },
                        ),
                        timeout=15,
                    )
                    assert "等待验收" in captured_systems[-1]
                    assert "这次请用英文" not in captured_systems[-1]
                result = await client.send_command(
                    "memory.manage",
                    {
                        "session_id": session_id,
                        "action": "list",
                    },
                )
                assert result["items"][0]["value"] == "中文"
                assert result["items"][0]["version"] == 1
            finally:
                await client.close()
                if loop_task is not None:
                    loop_task.cancel()
                    await asyncio.gather(loop_task, return_exceptions=True)
                proc.terminate()
                await asyncio.to_thread(proc.wait, timeout=5)
    finally:
        model_server.close()
        await model_server.wait_closed()
