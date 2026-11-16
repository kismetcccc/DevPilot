from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from textual.widgets import Static

from devpilot.tui.app import ChatTextArea, DevPilotTuiApp


# 创建连接受控客户端的 TUI，收集界面输出而不访问网络
def _app() -> tuple[DevPilotTuiApp, list[Static]]:
    app = DevPilotTuiApp("127.0.0.1", 1)
    app._session_id = "sess-111111111111"
    app._client = MagicMock()
    app._client.send_command = AsyncMock(return_value={"enabled": True, "items": []})
    rendered: list[Static] = []
    app._append = lambda widget: rendered.append(widget)
    return app, rendered


# 功能：验证 TUI 画像编辑保留多词值并使用专用 IPC，不触发模型任务
# 设计：检查发送的完整参数，覆盖输入分词和作用域绑定
async def test_memory_set_preserves_value() -> None:
    app, rendered = _app()
    await app._do_memory("/memory set response_style detailed with examples")
    app._client.send_command.assert_awaited_once_with(
        "memory.manage",
        {
            "session_id": "sess-111111111111",
            "action": "set",
            "key": "response_style",
            "value": "detailed with examples",
        },
    )
    assert rendered


# 功能：验证记忆内容包含 Rich 标记时仍以纯文本显示
# 设计：用带标记和伪造界面文本的值检查组件禁用 markup，防止内容影响交互展示
async def test_memory_renders_untrusted_content_as_plain_text() -> None:
    app, rendered = _app()
    app._client.send_command.return_value = {
        "enabled": True,
        "items": [{"key": "preferred_name", "value": "[red]name[/red]", "version": 2}],
    }
    await app._do_memory("/memory")
    assert "[red]name[/red]" in str(rendered[0].content)
    assert "[red]name[/red]" in rendered[0].render().plain


# 功能：验证非法管理命令显示帮助，不发送可能有副作用的请求
# 设计：覆盖缺少值、缺少字段和未知命令三条输入边界
async def test_memory_invalid_input_shows_help() -> None:
    for command in ("/memory set", "/memory forget", "/memory unknown"):
        app, rendered = _app()
        await app._do_memory(command)
        app._client.send_command.assert_not_called()
        assert "/memory set" in str(rendered[0].content)


# 功能：验证通过输入框提交 /memory 可显示管理结果，且不会进入忙碌的模型执行状态
# 设计：使用 Textual Pilot 驱动真实键盘提交，覆盖组件事件到异步 worker 的路径
async def test_memory_keyboard_submission() -> None:
    app = DevPilotTuiApp("127.0.0.1", 1)
    client = MagicMock()
    client.send_command = AsyncMock(
        return_value={
            "enabled": True,
            "items": [{"key": "preferred_language", "value": "中文", "version": 1}],
        }
    )

    # 测试环境直接接入可控客户端，不启动后台重连循环
    async def connect() -> None:
        app._client = client
        app._session_id = "sess-111111111111"
        prompt = app.query_one("#prompt", ChatTextArea)
        prompt.disabled = False
        prompt.focus()

    app._socket_loop = connect
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", ChatTextArea)
        prompt.text = "/memory"
        await pilot.pause()
        await pilot.press("tab")
        await pilot.press("enter")
        await pilot.pause()
        assert client.send_command.await_count == 1
        assert not app._busy
        assert any("preferred_language" in str(widget.content) for widget in app.query(Static))


# 功能：验证恢复会话成功后才切换当前会话 ID
# 设计：检查专用恢复协议调用，避免仅能读取已关闭会话却无法继续聊天
async def test_resume_uses_resume_protocol() -> None:
    app, _ = _app()
    await app._do_resume("sess-222222222222")
    assert app._session_id == "sess-222222222222"
    app._client.send_command.assert_awaited_once_with(
        "session.resume",
        {"session_id": "sess-222222222222"},
    )
