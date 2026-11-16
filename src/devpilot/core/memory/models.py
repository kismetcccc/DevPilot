from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KEY_ALIASES = {
    "language": "preferred_language",
    "response_language": "preferred_language",
    "language_preference": "preferred_language",
    "name": "preferred_name",
    "communication_style": "response_style",
    "programming_language": "preferred_programming_language",
}


class MemoryFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=1000)
    quote: str = Field(min_length=1, max_length=2000)
    intent: Literal["stable", "correction", "temporary", "inferred"] = "inferred"

    @field_validator("key")
    @classmethod
    # 校验画像字段名，防止同一字段因大小写或空格产生重复条目
    def validate_key(cls, value: str) -> str:
        value = value.strip().lower()
        value = KEY_ALIASES.get(value, value)
        if not re.fullmatch(r"[a-z][a-z0-9_.]{0,79}", value):
            raise ValueError("key must be a lowercase identifier")
        return value

    @field_validator("value", "quote")
    @classmethod
    # 去除无意义空白并拒绝空内容
    def validate_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("memory text must not be empty")
        return value


class ExtractedMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[MemoryFact] = Field(default_factory=list, max_length=8)
