from core.anthropic.native_messages_request import (
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
