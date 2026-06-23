from __future__ import annotations

from wechat_ai_companion.reply_format import REPLY_SEGMENT_DELIMITER, split_reply_segments


def test_split_reply_segments_uses_model_delimiter() -> None:
    text = f"先这样。{REPLY_SEGMENT_DELIMITER}我等你消息。"

    assert split_reply_segments(text) == ["先这样。", "我等你消息。"]


def test_split_reply_segments_falls_back_to_sentences() -> None:
    text = "我知道了。你先把材料发我？我看完再说。"

    assert split_reply_segments(text) == ["我知道了。", "你先把材料发我？", "我看完再说。"]


def test_split_reply_segments_does_not_drop_extra_sentences() -> None:
    text = "一。二。三。四。五。"

    assert split_reply_segments(text, max_segments=2) == ["一。", "二。", "三。", "四。", "五。"]
