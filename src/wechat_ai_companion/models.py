from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

Role = Literal["user", "assistant", "system"]
MemoryKind = Literal["profile", "preference", "event", "relation"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: str
    created_at: str


@dataclass(slots=True)
class AgentProfile:
    wx_user_id: str
    ai_name: str
    persona: str


@dataclass(slots=True)
class StructuredMemory:
    kind: MemoryKind
    key: str
    value: str
    confidence: float = 0.7

