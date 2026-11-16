from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from devpilot.core.session.store import SessionStore
from devpilot.core.tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from devpilot.core.memory.service import MemoryService


class NoteSaveParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str


class NoteSaveTool(BaseTool):
    params_model = NoteSaveParams
    name = "note_save"
    description = (
        "Save a concise fact or decision to this session's notes. "
        "These notes are visible in future turns of the same session."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The durable fact or decision to remember.",
            },
        },
        "required": ["content"],
    }

    # 绑定当前 session 与 run，使工具调用能写入对应 notes.md
    def __init__(
        self, store: SessionStore, session_id: str, run_id: str,
        memory: MemoryService | None = None,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._run_id = run_id
        self._memory = memory

    # 将非空 content 追加到 session notes.md
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        content = NoteSaveParams.model_validate(params).content.strip()
        if not content:
            return ToolResult(
                content="empty content",
                is_error=True,
                error_type="runtime_error",
            )
        if self._memory is not None:
            try:
                await self._memory.save_note(self._session_id, content, self._run_id)
            except ValueError as exc:
                return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
            except Exception:
                return ToolResult(
                    content="PostgreSQL memory unavailable; note was not saved",
                    is_error=True, error_type="runtime_error",
                )
        else:
            self._store.append_note(self._session_id, content, self._run_id)
        return ToolResult(content="saved")
