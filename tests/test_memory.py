from __future__ import annotations

from pathlib import Path

from wechat_ai_companion.config import MemorySettings
from wechat_ai_companion.memory import MemoryStore, estimate_tokens
from wechat_ai_companion.models import StructuredMemory


def settings() -> MemorySettings:
    return MemorySettings(
        hot_min_turns=2,
        hot_max_turns=4,
        context_token_budget=200,
        compression_trigger_ratio=0.7,
        long_term_extract_every_turns=4,
    )


def test_agent_is_bound_by_wechat_user(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "test.db", settings())
    first = store.get_or_create_agent("user-a", "The One", "default")
    updated = store.update_persona("user-a", None, "new persona")
    second_user = store.get_or_create_agent("user-b", "The One", "default")

    assert first.wx_user_id == "user-a"
    assert updated.persona == "new persona"
    assert second_user.persona == "default"
    store.close()


def test_structured_memory_rules(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "test.db", settings())
    store.upsert_structured("user-a", StructuredMemory("profile", "name", "Alice"))
    store.upsert_structured("user-a", StructuredMemory("profile", "name", "Alicia"))
    store.upsert_structured("user-a", StructuredMemory("event", "2026-06-15", "分手了"))
    store.upsert_structured("user-a", StructuredMemory("event", "2026-06-15", "面试挂了"))

    memories = store.list_structured("user-a")
    profile_names = [m.value for m in memories if m.kind == "profile" and m.key == "name"]
    events = [m for m in memories if m.kind == "event"]

    assert profile_names == ["Alicia"]
    assert len(events) == 2
    store.close()


def test_should_compress_after_hot_window_exceeded(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "test.db", settings())
    for index in range(10):
        store.add_message("user-a", "user" if index % 2 == 0 else "assistant", f"message {index}")

    assert store.should_compress("user-a") is True
    rows, from_id, to_id = store.messages_for_compression("user-a")
    assert len(rows) == 6
    assert from_id == 1
    assert to_id == 6
    store.close()


def test_token_estimate_is_positive() -> None:
    assert estimate_tokens("你好") >= 2
    assert estimate_tokens("hello world") >= 1

