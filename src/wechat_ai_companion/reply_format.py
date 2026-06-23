from __future__ import annotations

import re


REPLY_SEGMENT_DELIMITER = "<NEXT_MESSAGE>"
DEFAULT_MAX_REPLY_SEGMENTS = 4
DEFAULT_MAX_SEGMENT_CHARS = 180


def build_reply_format_rules(max_segments: int = DEFAULT_MAX_REPLY_SEGMENTS) -> str:
    return (
        "微信回复格式要求：像正常微信联系人一样回复。"
        "如果一句话就够，只回复一句；如果需要分几条说完，由你决定拆成 2-"
        f"{max_segments} 条。"
        f"多条消息时，必须用单独一行 {REPLY_SEGMENT_DELIMITER} 分隔。"
        "每条消息尽量只包含一句话或一个短短的语义块，不要把长段落塞进一条消息。"
        "不要向用户解释分隔符。"
    )


def split_reply_segments(
    text: str,
    *,
    max_segments: int = DEFAULT_MAX_REPLY_SEGMENTS,
    max_segment_chars: int = DEFAULT_MAX_SEGMENT_CHARS,
) -> list[str]:
    cleaned = _strip_code_fence(text.strip())
    if not cleaned:
        return []

    if REPLY_SEGMENT_DELIMITER in cleaned:
        parts = [part.strip() for part in cleaned.split(REPLY_SEGMENT_DELIMITER)]
    else:
        parts = _split_sentences(cleaned)

    segments: list[str] = []
    for part in parts:
        for segment in _split_long_part(part, max_segment_chars):
            segment = _clean_segment(segment)
            if segment:
                segments.append(segment)

    return segments


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    text = re.sub(r"^```(?:text|markdown|json)?", "", text.strip(), flags=re.IGNORECASE).strip()
    return re.sub(r"```$", "", text).strip()


def _split_sentences(text: str) -> list[str]:
    chunks: list[str] = []
    buffer: list[str] = []
    for char in text.replace("\r\n", "\n"):
        if char == "\n":
            chunk = "".join(buffer).strip()
            if chunk:
                chunks.append(chunk)
            buffer = []
            continue
        buffer.append(char)
        if char in "。！？!?…":
            chunk = "".join(buffer).strip()
            if chunk:
                chunks.append(chunk)
            buffer = []
    tail = "".join(buffer).strip()
    if tail:
        chunks.append(tail)
    return chunks or [text]


def _split_long_part(part: str, max_chars: int) -> list[str]:
    part = part.strip()
    if not part:
        return []
    if len(part) <= max_chars:
        return [part]

    chunks: list[str] = []
    remaining = part
    while len(remaining) > max_chars:
        split_at = max(
            remaining.rfind("，", 0, max_chars),
            remaining.rfind(",", 0, max_chars),
            remaining.rfind("；", 0, max_chars),
            remaining.rfind(";", 0, max_chars),
            remaining.rfind(" ", 0, max_chars),
        )
        if split_at < max_chars // 2:
            split_at = max_chars
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip(" ，,；;")
    if remaining:
        chunks.append(remaining)
    return chunks


def _clean_segment(segment: str) -> str:
    segment = segment.strip()
    segment = re.sub(r"^\s*[-*]\s+", "", segment)
    segment = re.sub(r"^\s*\d+[.)、]\s*", "", segment)
    return segment.strip()
