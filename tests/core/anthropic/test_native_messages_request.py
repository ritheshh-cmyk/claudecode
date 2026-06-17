from core.anthropic.native_messages_request import (
    build_base_native_anthropic_request_body,
    sanitize_native_messages_thinking_policy,
    sanitize_native_messages_tools,
)


def test_sanitize_native_messages_thinking_policy_disabled():
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "some thinking"},
                {"type": "redacted_thinking", "data": "abc"},
                {"type": "text", "text": "actual response"},
            ],
        },
    ]
    sanitized = sanitize_native_messages_thinking_policy(
        messages, thinking_enabled=False
    )
    assert sanitized == [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "actual response"},
            ],
        },
    ]


def test_sanitize_native_messages_thinking_policy_enabled():
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "valid thinking",
                    "signature": "sig123",
                },
                {"type": "thinking", "thinking": "", "signature": "sig456"},
                {"type": "thinking", "thinking": "   ", "signature": "sig789"},
                {"type": "thinking", "thinking": "unsigned thinking"},
                {"type": "text", "text": "actual response"},
            ],
        },
    ]
    sanitized = sanitize_native_messages_thinking_policy(
        messages, thinking_enabled=True
    )
    assert sanitized == [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "valid thinking",
                    "signature": "sig123",
                },
                {"type": "text", "text": "actual response"},
            ],
        },
    ]


def test_sanitize_native_messages_tools():
    tools = [
        {"name": "my_custom_tool", "description": "some tool"},
        {"name": "bash_tool", "type": "bash_20250124"},
        {"name": "unsupported_tool", "type": "advisor_20260301"},
        {"name": "web_search_tool", "type": "web_search_20260209"},
    ]
    sanitized = sanitize_native_messages_tools(tools)
    assert sanitized == [
        {"name": "my_custom_tool", "description": "some tool"},
        {"name": "bash_tool", "type": "bash_20250124"},
        {"name": "web_search_tool", "type": "web_search_20260209"},
    ]


class MockRequest:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_build_base_native_anthropic_request_body_clamping():
    # 1. Budget < 1024 should be clamped to 1024
    req_small = MockRequest(
        model="claude-3-5-sonnet",
        messages=[{"role": "user", "content": "hello"}],
        thinking={"budget_tokens": 512},
    )
    body_small = build_base_native_anthropic_request_body(
        req_small, default_max_tokens=4096, thinking_enabled=True
    )
    assert body_small["thinking"] == {"type": "enabled", "budget_tokens": 1024}

    # 2. Budget >= 1024 should be preserved
    req_large = MockRequest(
        model="claude-3-5-sonnet",
        messages=[{"role": "user", "content": "hello"}],
        thinking={"budget_tokens": 2048},
    )
    body_large = build_base_native_anthropic_request_body(
        req_large, default_max_tokens=4096, thinking_enabled=True
    )
    assert body_large["thinking"] == {"type": "enabled", "budget_tokens": 2048}

    # 3. Thinking disabled should omit/pop the thinking payload
    body_disabled = build_base_native_anthropic_request_body(
        req_small, default_max_tokens=4096, thinking_enabled=False
    )
    assert "thinking" not in body_disabled
