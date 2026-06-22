from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from pathlib import Path

from .config import MemorySettings
from .llm import ModelRouter
from .models import AgentProfile, ChatMessage, StructuredMemory, utc_now_iso


RELATION_SCORE_KEYS = {"familiarity", "trust"}
RELATION_KEY_ALIASES = {
    "familiarity": "familiarity",
    "熟悉度": "familiarity",
    "熟悉": "familiarity",
    "trust": "trust",
    "信任": "trust",
    "信任度": "trust",
    "信任等级": "trust",
}


def estimate_tokens(text: str) -> int:
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    other_chars = max(0, len(text) - chinese_chars)
    return max(1, chinese_chars + other_chars // 4)


def _normalize_relation_key(key: str) -> str:
    clean_key = key.strip()
    return RELATION_KEY_ALIASES.get(clean_key.lower(), clean_key)


def _parse_relation_score(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)(?:\s*(?:/100|%))?", text)
    if not match:
        return None
    score = int(round(float(match.group(1))))
    return max(0, min(100, score))


def _relation_score(value: str | int | float | None, default: int = 0) -> int:
    score = _parse_relation_score(value)
    return default if score is None else score


class MemoryStore:
    def __init__(self, db_path: str | Path, settings: MemorySettings) -> None:
        self.db_path = Path(db_path)
        self.settings = settings
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS agents (
                wx_user_id TEXT PRIMARY KEY,
                ai_name TEXT NOT NULL,
                persona TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wx_user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                token_estimate INTEGER NOT NULL,
                compressed INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wx_user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                from_message_id INTEGER,
                to_message_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS structured_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wx_user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.7,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source_message_id INTEGER,
                UNIQUE(wx_user_id, kind, key)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(wx_user_id, id);
            CREATE INDEX IF NOT EXISTS idx_memories_user_kind ON structured_memories(wx_user_id, kind);

            CREATE TABLE IF NOT EXISTS plugin_state (
                plugin_name TEXT NOT NULL,
                wx_user_id TEXT NOT NULL,
                state_key TEXT NOT NULL,
                state_value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(plugin_name, wx_user_id, state_key)
            );
            """
        )
        self.conn.commit()

    def get_or_create_agent(self, wx_user_id: str, default_name: str, default_persona: str) -> AgentProfile:
        row = self.conn.execute(
            "SELECT wx_user_id, ai_name, persona FROM agents WHERE wx_user_id = ?",
            (wx_user_id,),
        ).fetchone()
        if row:
            return AgentProfile(row["wx_user_id"], row["ai_name"], row["persona"])

        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO agents(wx_user_id, ai_name, persona, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (wx_user_id, default_name, default_persona, now, now),
        )
        self.conn.commit()
        return AgentProfile(wx_user_id, default_name, default_persona)

    def update_persona(self, wx_user_id: str, ai_name: str | None, persona: str) -> AgentProfile:
        agent = self.get_or_create_agent(wx_user_id, ai_name or "The One", persona)
        final_name = ai_name or agent.ai_name
        now = utc_now_iso()
        self.conn.execute(
            "UPDATE agents SET ai_name = ?, persona = ?, updated_at = ? WHERE wx_user_id = ?",
            (final_name, persona, now, wx_user_id),
        )
        self.conn.commit()
        return AgentProfile(wx_user_id, final_name, persona)

    def add_message(self, wx_user_id: str, role: str, content: str) -> int:
        now = utc_now_iso()
        cur = self.conn.execute(
            """
            INSERT INTO messages(wx_user_id, role, content, created_at, token_estimate)
            VALUES (?, ?, ?, ?, ?)
            """,
            (wx_user_id, role, content, now, estimate_tokens(content)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_messages(self, wx_user_id: str, limit_messages: int | None = None) -> list[ChatMessage]:
        limit = limit_messages or self.settings.hot_max_turns * 2
        rows = self.conn.execute(
            """
            SELECT role, content, created_at
            FROM messages
            WHERE wx_user_id = ? AND compressed = 0
            ORDER BY id DESC
            LIMIT ?
            """,
            (wx_user_id, limit),
        ).fetchall()
        return [ChatMessage(row["role"], row["content"], row["created_at"]) for row in reversed(rows)]

    def list_known_users(self) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT wx_user_id FROM agents
            UNION
            SELECT wx_user_id FROM messages
            ORDER BY wx_user_id
            """
        ).fetchall()
        return [row["wx_user_id"] for row in rows]

    def list_agents(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT
                a.wx_user_id,
                a.ai_name,
                a.persona,
                a.created_at,
                a.updated_at,
                (SELECT COUNT(*) FROM messages m WHERE m.wx_user_id = a.wx_user_id) AS message_count,
                (SELECT MAX(created_at) FROM messages m WHERE m.wx_user_id = a.wx_user_id) AS latest_message_at
            FROM agents a
            ORDER BY latest_message_at DESC, updated_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_message_dicts(self, wx_user_id: str, limit: int = 40) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, role, content, created_at, token_estimate, compressed
            FROM messages
            WHERE wx_user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (wx_user_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def list_summary_dicts(self, wx_user_id: str, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, content, from_message_id, to_message_id, created_at
            FROM summaries
            WHERE wx_user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (wx_user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_structured_dicts(self, wx_user_id: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, kind, key, value, confidence, created_at, updated_at, source_message_id
            FROM structured_memories
            WHERE wx_user_id = ?
            ORDER BY kind, updated_at DESC, id DESC
            """,
            (wx_user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_message_at(self, wx_user_id: str, role: str | None = None) -> str | None:
        if role:
            row = self.conn.execute(
                """
                SELECT created_at FROM messages
                WHERE wx_user_id = ? AND role = ?
                ORDER BY id DESC LIMIT 1
                """,
                (wx_user_id, role),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT created_at FROM messages
                WHERE wx_user_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (wx_user_id,),
            ).fetchone()
        return row["created_at"] if row else None

    def get_plugin_state(self, plugin_name: str, wx_user_id: str, key: str, default: str = "") -> str:
        row = self.conn.execute(
            """
            SELECT state_value FROM plugin_state
            WHERE plugin_name = ? AND wx_user_id = ? AND state_key = ?
            """,
            (plugin_name, wx_user_id, key),
        ).fetchone()
        return row["state_value"] if row else default

    def set_plugin_state(self, plugin_name: str, wx_user_id: str, key: str, value: str) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO plugin_state(plugin_name, wx_user_id, state_key, state_value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(plugin_name, wx_user_id, state_key)
            DO UPDATE SET state_value = excluded.state_value, updated_at = excluded.updated_at
            """,
            (plugin_name, wx_user_id, key, value, now),
        )
        self.conn.commit()

    def list_plugin_state(self, plugin_name: str, state_key: str | None = None) -> list[dict]:
        if state_key is None:
            rows = self.conn.execute(
                """
                SELECT plugin_name, wx_user_id, state_key, state_value, updated_at
                FROM plugin_state
                WHERE plugin_name = ?
                ORDER BY updated_at DESC
                """,
                (plugin_name,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT plugin_name, wx_user_id, state_key, state_value, updated_at
                FROM plugin_state
                WHERE plugin_name = ? AND state_key = ?
                ORDER BY updated_at DESC
                """,
                (plugin_name, state_key),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_summary(self, wx_user_id: str) -> str:
        row = self.conn.execute(
            "SELECT content FROM summaries WHERE wx_user_id = ? ORDER BY id DESC LIMIT 1",
            (wx_user_id,),
        ).fetchone()
        return row["content"] if row else ""

    def list_structured(self, wx_user_id: str) -> list[StructuredMemory]:
        rows = self.conn.execute(
            """
            SELECT kind, key, value, confidence
            FROM structured_memories
            WHERE wx_user_id = ?
            ORDER BY kind, updated_at DESC, id DESC
            """,
            (wx_user_id,),
        ).fetchall()
        return [
            StructuredMemory(row["kind"], row["key"], row["value"], float(row["confidence"]))
            for row in rows
        ]

    def upsert_structured(self, wx_user_id: str, memory: StructuredMemory, source_message_id: int | None = None) -> None:
        now = utc_now_iso()
        if memory.kind == "relation":
            key = _normalize_relation_key(memory.key)
            if key in RELATION_SCORE_KEYS:
                score = _parse_relation_score(memory.value)
                if score is None:
                    logging.warning(
                        "[memory] ignored invalid relation score user=%s key=%s value=%r",
                        wx_user_id,
                        memory.key,
                        memory.value,
                    )
                    return
                memory = StructuredMemory("relation", key, str(score), memory.confidence)
        if memory.kind == "event":
            key = f"{memory.key}:{now}:{source_message_id or uuid.uuid4().hex[:8]}"
            self.conn.execute(
                """
                INSERT INTO structured_memories(wx_user_id, kind, key, value, confidence, created_at, updated_at, source_message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (wx_user_id, memory.kind, key, memory.value, memory.confidence, now, now, source_message_id),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO structured_memories(wx_user_id, kind, key, value, confidence, created_at, updated_at, source_message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wx_user_id, kind, key)
                DO UPDATE SET value = excluded.value,
                              confidence = excluded.confidence,
                              updated_at = excluded.updated_at,
                              source_message_id = excluded.source_message_id
                """,
                (wx_user_id, memory.kind, memory.key, memory.value, memory.confidence, now, now, source_message_id),
            )
        self.conn.commit()

    def relation_delta(self, wx_user_id: str, familiarity_delta: int = 1, trust_delta: int = 0) -> None:
        existing = {m.key: m.value for m in self.list_structured(wx_user_id) if m.kind == "relation"}
        familiarity = max(0, min(100, _relation_score(existing.get("familiarity"), 0) + familiarity_delta))
        trust = max(0, min(100, _relation_score(existing.get("trust"), 0) + trust_delta))
        self.upsert_structured(wx_user_id, StructuredMemory("relation", "familiarity", str(familiarity), 0.9))
        self.upsert_structured(wx_user_id, StructuredMemory("relation", "trust", str(trust), 0.9))

    def context_token_estimate(self, wx_user_id: str) -> int:
        messages = self.recent_messages(wx_user_id, self.settings.hot_max_turns * 2)
        structured = self.list_structured(wx_user_id)
        summary = self.latest_summary(wx_user_id)
        return (
            sum(estimate_tokens(m.content) for m in messages)
            + sum(estimate_tokens(f"{m.kind}:{m.key}:{m.value}") for m in structured)
            + estimate_tokens(summary)
        )

    def should_compress(self, wx_user_id: str) -> bool:
        token_limit = int(self.settings.context_token_budget * self.settings.compression_trigger_ratio)
        recent_count = self.conn.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE wx_user_id = ? AND compressed = 0",
            (wx_user_id,),
        ).fetchone()["count"]
        return recent_count > self.settings.hot_max_turns * 2 or self.context_token_estimate(wx_user_id) >= token_limit

    def messages_for_compression(self, wx_user_id: str) -> tuple[list[sqlite3.Row], int | None, int | None]:
        keep = self.settings.hot_min_turns * 2
        rows = self.conn.execute(
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE wx_user_id = ? AND compressed = 0
            ORDER BY id ASC
            """,
            (wx_user_id,),
        ).fetchall()
        if len(rows) <= keep:
            return [], None, None
        compress_rows = rows[: max(0, len(rows) - keep)]
        return compress_rows, int(compress_rows[0]["id"]), int(compress_rows[-1]["id"])

    async def compress_if_needed(self, wx_user_id: str, llm: ModelRouter) -> dict | None:
        if not self.should_compress(wx_user_id):
            return None
        rows, from_id, to_id = self.messages_for_compression(wx_user_id)
        if not rows or from_id is None or to_id is None:
            return None

        prior = self.latest_summary(wx_user_id)
        transcript = "\n".join(f"{row['created_at']} {row['role']}: {row['content']}" for row in rows)
        prompt = (
            "把以下微信对话压缩成中期摘要，保留事实、未完成事项、情绪状态、关系变化和用户偏好。"
            "不要编造，不要加入原文没有的信息。用中文，控制在 800 字以内。"
        )
        if prior:
            prompt += f"\n\n已有摘要：\n{prior}"
        response = await llm.chat(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": transcript},
            ],
            max_tokens=900,
        )
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO summaries(wx_user_id, content, from_message_id, to_message_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (wx_user_id, response.content.strip(), from_id, to_id, now),
        )
        self.conn.execute(
            "UPDATE messages SET compressed = 1 WHERE wx_user_id = ? AND id BETWEEN ? AND ?",
            (wx_user_id, from_id, to_id),
        )
        self.conn.commit()
        return {
            "from_message_id": from_id,
            "to_message_id": to_id,
            "message_count": len(rows),
            "summary_preview": response.content.strip()[:120],
        }

    async def extract_long_term_if_due(self, wx_user_id: str, source_message_id: int, llm: ModelRouter) -> list[StructuredMemory]:
        count = self.conn.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE wx_user_id = ? AND role = 'user'",
            (wx_user_id,),
        ).fetchone()["count"]
        if count % self.settings.long_term_extract_every_turns != 0:
            return []

        recent = self.recent_messages(wx_user_id, limit_messages=8)
        transcript = "\n".join(f"{m.role}: {m.content}" for m in recent)
        schema = (
            "只返回 JSON 数组。每项格式："
            '{"kind":"profile|preference|event|relation","key":"短键","value":"事实值","confidence":0.0-1.0}。'
            "profile 用 overwrite；preference 用 latest-wins；event 只记录有日期或明显时间锚点的重要事件；"
            "relation 只输出 familiarity/trust 等数值变化依据。没有可记忆内容时返回 []。"
        )
        response = await llm.chat(
            [
                {"role": "system", "content": schema},
                {"role": "user", "content": transcript},
            ],
            max_tokens=500,
        )
        items = _parse_json_array(response.content)
        extracted: list[StructuredMemory] = []
        for item in items:
            try:
                memory = StructuredMemory(
                    kind=item["kind"],
                    key=str(item["key"])[:80],
                    value=str(item["value"])[:1000],
                    confidence=float(item.get("confidence", 0.7)),
                )
            except Exception:
                continue
            if memory.kind in {"profile", "preference", "event", "relation"} and memory.key and memory.value:
                self.upsert_structured(wx_user_id, memory, source_message_id)
                extracted.append(memory)
        return extracted

    def build_prompt_messages(self, agent: AgentProfile, system_rules: str, user_text: str) -> list[dict[str, str]]:
        structured = self.list_structured(agent.wx_user_id)
        structured_text = "\n".join(
            f"- {m.kind}.{m.key}: {m.value}" for m in structured
        ) or "- 暂无长期结构化记忆"
        summary = self.latest_summary(agent.wx_user_id) or "暂无中期摘要"
        hot = self.recent_messages(agent.wx_user_id, self.settings.hot_max_turns * 2)

        system = (
            f"{system_rules}\n\n"
            f"AI 名称：{agent.ai_name}\n"
            f"AI 人设：{agent.persona}\n\n"
            f"长期结构化记忆：\n{structured_text}\n\n"
            f"中期摘要：\n{summary}"
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for item in hot:
            if item.role in {"user", "assistant", "system"}:
                messages.append({"role": item.role, "content": item.content})
        messages.append({"role": "user", "content": user_text})
        return messages


def _parse_json_array(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*]", text)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []
