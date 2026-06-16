"""Structured TRACE logging assertions."""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from config.logging_config import configure_logging
from core.trace import TRACE_PAYLOAD_BINDING, trace_event


def test_trace_payload_merged_into_json_line(tmp_path) -> None:
    log_file = str(tmp_path / "t.log")
    configure_logging(log_file, force=True)
    trace_event(stage="s", event="e.v1", source="unit", hello="world", n=42)
    logger.complete()
    text = Path(log_file).read_text(encoding="utf-8").strip().split("\n")[-1]
    row = json.loads(text)
    assert row["trace"] is True
    assert row["stage"] == "s"
    assert row["event"] == "e.v1"
    assert row["source"] == "unit"
    assert row["hello"] == "world"
    assert row["n"] == 42
    assert TRACE_PAYLOAD_BINDING == "trace_payload"


def test_sanitize_masks_nested_api_key_strings() -> None:
    """Credential-shaped keys redact without touching normal message text."""
    from core.trace import _sanitize_trace_value

    out = _sanitize_trace_value(
        {"outer": {"api_key": "secret", "text": "visible"}},
    )
    assert out["outer"]["api_key"] == "<redacted>"
    assert out["outer"]["text"] == "visible"
