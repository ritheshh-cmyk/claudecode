"""Track content-block state for native Anthropic SSE strings we emit to clients."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from core.anthropic.sse import SSEBuilder, format_sse_event
from core.anthropic.stream_contracts import SSEEvent, parse_sse_lines
from core.anthropic.stream_recovery import (
    ToolSchema,
    accept_tool_json_repair,
    continuation_suffix,
    parse_complete_tool_input,
)


@dataclass
class EmittedBlockState:
    """Tracked downstream block payload emitted to the client."""

    index: int
    block_type: str
    open: bool = True
    tool_id: str = ""
    name: str = ""
    parts: list[str] = field(default_factory=list)

    @property
    def content(self) -> str:
        return "".join(self.parts)


class EmittedNativeSseTracker:
    """Parse emitted SSE frames so mid-stream errors can close blocks and pick a fresh index."""

    def __init__(self) -> None:
        self._buf = ""
        self._open_stack: list[int] = []
        self._max_index = -1
        self._blocks: dict[int, EmittedBlockState] = {}
        self.message_id: str | None = None
        self.model: str = ""
        self.stop_reason: str | None = None
        self.message_stopped = False

    def feed(self, chunk: str) -> None:
        """Record SSE frames completed by ``chunk`` (handles splitting across reads)."""
        self._buf += chunk
        while True:
            sep = self._buf.find("\n\n")
            if sep < 0:
                break
            frame = self._buf[:sep]
            self._buf = self._buf[sep + 2 :]
            if not frame.strip():
                continue
            for event in parse_sse_lines(frame.splitlines()):
                self._observe(event)

    def _observe(self, event: SSEEvent) -> None:
        if event.event == "message_start":
            message = event.data.get("message")
            if isinstance(message, dict):
                mid = message.get("id")
                if isinstance(mid, str) and mid:
                    self.message_id = mid
                model = message.get("model")
                if isinstance(model, str) and model:
                    self.model = model
            return

        if event.event == "content_block_start":
            raw_index = event.data.get("index")
            if not isinstance(raw_index, int):
                return
            idx = raw_index
            self._max_index = max(self._max_index, idx)
            self._open_stack.append(idx)
            block = event.data.get("content_block")
            if isinstance(block, dict):
                block_type = str(block.get("type", ""))
                state = EmittedBlockState(index=idx, block_type=block_type)
                if block_type == "tool_use":
                    tool_id = block.get("id")
                    name = block.get("name")
                    state.tool_id = tool_id if isinstance(tool_id, str) else ""
                    state.name = name if isinstance(name, str) else ""
                elif block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        state.parts.append(text)
                elif block_type == "thinking":
                    thinking = block.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        state.parts.append(thinking)
                self._blocks[idx] = state
            return

        if event.event == "content_block_delta":
            raw_index = event.data.get("index")
            if not isinstance(raw_index, int):
                return
            idx = raw_index
            state = self._blocks.get(idx)
            delta = event.data.get("delta")
            if state is not None and isinstance(delta, dict):
                if state.block_type == "text":
                    text = delta.get("text")
                    if isinstance(text, str):
                        state.parts.append(text)
                elif state.block_type == "thinking":
                    thinking = delta.get("thinking")
                    if isinstance(thinking, str):
                        state.parts.append(thinking)
                elif state.block_type == "tool_use":
                    partial = delta.get("partial_json")
                    if isinstance(partial, str):
                        state.parts.append(partial)
            return

        if event.event == "content_block_stop":
            raw_index = event.data.get("index")
            if not isinstance(raw_index, int):
                return
            idx = raw_index
            if self._open_stack and self._open_stack[-1] == idx:
                self._open_stack.pop()
            else:
                with suppress(ValueError):
                    self._open_stack.remove(idx)
            state = self._blocks.get(idx)
            if state is not None:
                state.open = False
            return

        if event.event == "message_delta":
            delta = event.data.get("delta")
            if isinstance(delta, dict):
                stop_reason = delta.get("stop_reason")
                if isinstance(stop_reason, str):
                    self.stop_reason = stop_reason
            return

        if event.event == "message_stop":
            self.message_stopped = True

    def next_content_index(self) -> int:
        """Next unused content block index based on emitted starts."""
        return self._max_index + 1

    def iter_close_unclosed_blocks(self) -> Iterator[str]:
        """Yield ``content_block_stop`` events for blocks that were started but not stopped."""
        while self._open_stack:
            idx = self._open_stack.pop()
            state = self._blocks.get(idx)
            if state is not None:
                state.open = False
            yield format_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": idx},
            )

    def emitted_text(self) -> str:
        return "".join(
            block.content
            for block in self._blocks.values()
            if block.block_type == "text"
        )

    def emitted_thinking(self) -> str:
        return "".join(
            block.content
            for block in self._blocks.values()
            if block.block_type == "thinking"
        )

    def has_tool_block(self) -> bool:
        return any(block.block_type == "tool_use" for block in self._blocks.values())

    def has_content_block(self) -> bool:
        return bool(self._blocks)

    def has_terminal_message(self) -> bool:
        return self.message_stopped

    def tool_blocks(self) -> list[EmittedBlockState]:
        return [
            block for block in self._blocks.values() if block.block_type == "tool_use"
        ]

    def can_salvage_tool_use(self, schemas: dict[str, ToolSchema]) -> bool:
        tool_blocks = self.tool_blocks()
        if not tool_blocks:
            return False
        for block in tool_blocks:
            if not block.tool_id or not block.name:
                return False
            if parse_complete_tool_input(block.content, block.name, schemas) is None:
                return False
        return True

    def append_text_suffix(self, suffix: str) -> Iterator[str]:
        if not suffix:
            return
        active = self._last_open_block("text")
        if active is None:
            index = self.next_content_index()
            self._max_index = max(self._max_index, index)
            active = EmittedBlockState(index=index, block_type="text")
            self._blocks[index] = active
            self._open_stack.append(index)
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        active.parts.append(suffix)
        yield format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": active.index,
                "delta": {"type": "text_delta", "text": suffix},
            },
        )

    def append_thinking_suffix(self, suffix: str) -> Iterator[str]:
        if not suffix:
            return
        active = self._last_open_block("thinking")
        if active is None:
            index = self.next_content_index()
            self._max_index = max(self._max_index, index)
            active = EmittedBlockState(index=index, block_type="thinking")
            self._blocks[index] = active
            self._open_stack.append(index)
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            )
        active.parts.append(suffix)
        yield format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": active.index,
                "delta": {"type": "thinking_delta", "thinking": suffix},
            },
        )

    def append_tool_repair_suffix(
        self,
        tool_index: int,
        suffix: str,
    ) -> Iterator[str]:
        tool_blocks = self.tool_blocks()
        if tool_index >= len(tool_blocks) or not suffix:
            return
        block = tool_blocks[tool_index]
        block.parts.append(suffix)
        yield format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": block.index,
                "delta": {"type": "input_json_delta", "partial_json": suffix},
            },
        )

    def iter_success_tail(self, stop_reason: str) -> Iterator[str]:
        yield from self.iter_close_unclosed_blocks()
        if self.stop_reason is None:
            yield format_sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"input_tokens": 0, "output_tokens": 1},
                },
            )
        if not self.message_stopped:
            yield format_sse_event("message_stop", {"type": "message_stop"})

    def accept_tool_repair(
        self,
        tool_index: int,
        candidate: str,
        schemas: dict[str, ToolSchema],
    ) -> str | None:
        tool_blocks = self.tool_blocks()
        if tool_index >= len(tool_blocks):
            return None
        block = tool_blocks[tool_index]
        repair = accept_tool_json_repair(
            block.content,
            candidate,
            tool_name=block.name,
            schemas=schemas,
        )
        return repair.suffix if repair is not None else None

    def continuation_text_suffix(self, candidate: str) -> str | None:
        return continuation_suffix(self.emitted_text(), candidate)

    def continuation_thinking_suffix(self, candidate: str) -> str | None:
        return continuation_suffix(self.emitted_thinking(), candidate)

    def iter_midstream_error_tail(
        self,
        error_message: str,
        *,
        request: Any,
        input_tokens: int,
        log_raw_sse_events: bool,
    ) -> Iterator[str]:
        """Close dangling blocks, emit a text error block at a fresh index, then message tail."""
        mid = self.message_id or f"msg_{uuid.uuid4()}"
        model = self.model or (getattr(request, "model", "") or "")
        sse = SSEBuilder(
            mid,
            model,
            input_tokens,
            log_raw_events=log_raw_sse_events,
        )
        sse.blocks.next_index = self.next_content_index()
        yield from sse.emit_error(error_message)
        yield sse.message_delta("end_turn", 1)
        yield sse.message_stop()

    def _last_open_block(self, block_type: str) -> EmittedBlockState | None:
        for index in reversed(self._open_stack):
            block = self._blocks.get(index)
            if block is not None and block.block_type == block_type and block.open:
                return block
        return None
