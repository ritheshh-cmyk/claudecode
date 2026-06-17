from __future__ import annotations

import pytest

from api.services import extract_tokens_from_chunk
from core.reliability.analytics import AnalyticsEngine, estimate_cost
from core.reliability.cache import ResponseCache
from core.reliability.key_encrypt import decrypt_key, encrypt_key
from core.reliability.key_pool import KeyPool


def test_key_encryption() -> None:
    original = "sk-aerolink-1234567890abcdef"
    encrypted = encrypt_key(original)
    assert encrypted != original
    decrypted = decrypt_key(encrypted)
    assert decrypted == original


def test_response_cache_exact_and_semantic() -> None:
    cache = ResponseCache(ttl=10, semantic_threshold=0.75)
    req = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "How do I build a caching layer?"}],
        "system": None,
        "tools": None,
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    chunks = ["data: chunk1\n\n", "data: chunk2\n\n"]

    # Cache miss
    assert cache.get(req) is None

    # Set cache
    cache.set(req, chunks)

    # Exact hit
    assert cache.get(req) == chunks

    # Semantic hit
    similar_req = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "How do I build a cache layer?"}],
        "system": None,
        "tools": None,
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    # Check that exact is None, but semantic matches
    assert cache.get(similar_req, enable_semantic=False) is None
    assert cache.get(similar_req, enable_semantic=True) == chunks

    # Non-matching prompt
    diff_req = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "Tell me a story about coding."}],
        "system": None,
        "tools": None,
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    assert cache.get(diff_req, enable_semantic=True) is None

    # Test LRU eviction
    small_cache = ResponseCache(ttl=10, max_size=2)
    req1 = {"model": "m1", "messages": [{"role": "user", "content": "p1"}]}
    req2 = {"model": "m1", "messages": [{"role": "user", "content": "p2"}]}
    req3 = {"model": "m1", "messages": [{"role": "user", "content": "p3"}]}

    small_cache.set(req1, ["c1"])
    small_cache.set(req2, ["c2"])

    # Hits req1, making it MRU (so req2 is now LRU)
    assert small_cache.get(req1) == ["c1"]

    # Adding req3 evicts the LRU item (req2)
    small_cache.set(req3, ["c3"])
    assert small_cache.get(req1) == ["c1"]
    assert small_cache.get(req3) == ["c3"]
    assert small_cache.get(req2) is None


def test_analytics_and_cost_estimation() -> None:
    # Estimate cost
    cost = estimate_cost("openai", "claude-3-5-sonnet-20241022", 1000, 2000)
    assert cost > 0.0

    # Aerolink is free
    free_cost = estimate_cost("aerolink", "claude-3-5-sonnet", 1000, 2000)
    assert free_cost == 0.0

    # Summary
    engine = AnalyticsEngine()
    engine.record_request(
        "req1", "aerolink", "sonnet", 100, 200, 150.0, 50.0, 200, True
    )
    engine.record_request(
        "req2", "openai", "sonnet", 1000, 2000, 800.0, 100.0, 200, True
    )

    summary = engine.get_summary()
    assert summary["total_requests"] == 2
    assert summary["total_cost_usd"] > 0.0
    assert summary["avg_latency_ms"] == 475.0
    assert (
        summary["p50_latency_ms"] == 150.0
    )  # sorted: [150.0, 800.0] -> index ceil(0.5 * 2) - 1 = 0
    assert summary["error_rate"] == 0.0

    csv_out = engine.export_csv()
    assert "req1" in csv_out
    assert "req2" in csv_out


@pytest.mark.asyncio
async def test_key_pool_operations() -> None:
    pool = KeyPool()
    await pool.add_key("aerolink", "key1", alias="k1", quota=5)
    await pool.add_key("aerolink", "key2", alias="k2", quota=0)

    assert pool.has_keys("aerolink") is True
    assert pool.has_keys("openai") is False

    # Get keys and rotate
    k_first = await pool.get_key("aerolink")
    k_second = await pool.get_key("aerolink")
    assert k_first in ("key1", "key2")
    assert k_second in ("key1", "key2")
    assert k_first != k_second

    # Test revocation
    status = await pool.get_all_keys_status()
    k1_hash = next(s["key_hash"] for s in status if s["alias"] == "k1")

    await pool.toggle_revoke_key("aerolink", k1_hash)
    status_after = await pool.get_all_keys_status()
    assert next(s["revoked"] for s in status_after if s["alias"] == "k1") is True

    # After revoking key1, we should only get key2
    for _ in range(5):
        key = await pool.get_key("aerolink")
        assert key == "key2"


def test_extract_tokens_from_chunk() -> None:
    chunk_start = 'event: message_start\ndata: {"type": "message_start", "message": {"id": "msg_123", "usage": {"input_tokens": 15, "output_tokens": 0}}}\n\n'
    inp, out = extract_tokens_from_chunk(chunk_start)
    assert inp == 15
    assert out == 0

    chunk_delta = 'event: message_delta\ndata: {"type": "message_delta", "usage": {"output_tokens": 27}}\n\n'
    inp2, out2 = extract_tokens_from_chunk(chunk_delta)
    assert inp2 == 0
    assert out2 == 27


def test_smart_routing_features() -> None:
    from api.model_router import ModelRouter
    from api.models.anthropic import Message, MessagesRequest, Tool
    from config.settings import Settings

    settings = Settings()
    # Reset all settings to ensure clean test environment
    settings.enable_context_length_routing = False
    settings.enable_task_type_routing = False
    settings.enable_time_based_routing = False
    settings.enable_canary_mode = False
    settings.enable_cost_aware_routing = False
    settings.enable_failover_memory = False
    settings.enable_sticky_sessions = False
    settings.enable_language_routing = False
    settings.enable_tool_use_routing = False
    settings.model_aliases = ""
    settings.provider_priority_groups = ""

    # 1. Model Alias Map (67)
    settings.model_aliases = "claude-opus-4:github_models/claude-3-7-sonnet"
    router = ModelRouter(settings)
    req = MessagesRequest(
        model="claude-opus-4",
        max_tokens=100,
        messages=[Message(role="user", content="hello")],
    )
    routed = router.resolve_messages_request(req)
    assert routed.resolved.provider_id == "github_models"
    assert routed.resolved.provider_model == "claude-3-7-sonnet"

    # Reset aliases
    settings.model_aliases = ""

    # 2. Context-Length Router (63)
    settings.enable_context_length_routing = True
    settings.context_length_threshold = 50
    settings.context_length_provider_model = "gemini/gemini-2.5-pro"
    # Content long enough to exceed 50 tokens
    long_content = "Word " * 60
    req_long = MessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content=long_content)],
    )
    router = ModelRouter(settings)
    routed = router.resolve_messages_request(req_long)
    assert routed.resolved.provider_id == "gemini"
    assert routed.resolved.provider_model == "gemini-2.5-pro"
    settings.enable_context_length_routing = False

    # 3. Tool-Use Router (74)
    settings.enable_tool_use_routing = True
    req_tools = MessagesRequest(
        model="claude-3-5-haiku",
        max_tokens=100,
        messages=[Message(role="user", content="hello")],
        tools=[Tool(name="get_weather", type="web_search_20250305")],
    )
    router = ModelRouter(settings)
    routed = router.resolve_messages_request(req_tools)
    assert routed.resolved.provider_id == "openai"
    assert routed.resolved.provider_model == "gpt-4o"
    settings.enable_tool_use_routing = False

    # 4. Language-Based Router (73)
    settings.enable_language_routing = True
    req_lang = MessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content="您好,请问今天天气怎么样?")],
    )
    router = ModelRouter(settings)
    routed = router.resolve_messages_request(req_lang)
    assert routed.resolved.provider_id == "gemini"
    assert routed.resolved.provider_model == "gemini-2.5-pro"
    settings.enable_language_routing = False

    # 5. Task-Type Detector (65) - Coding
    settings.enable_task_type_routing = True
    req_code = MessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content="def hello_world(): print('hello')")],
    )
    router = ModelRouter(settings)
    routed = router.resolve_messages_request(req_code)
    assert routed.resolved.provider_id == "github_models"
    assert routed.resolved.provider_model == "claude-3-5-sonnet"

    # Task-Type Detector - Math
    req_math = MessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content="Solve the derivative of x^2")],
    )
    routed = router.resolve_messages_request(req_math)
    assert routed.resolved.provider_id == "gemini"
    assert routed.resolved.provider_model == "gemini-2.5-pro"
    settings.enable_task_type_routing = False

    # 6. Time-Based Routing (66)
    import datetime

    current_hour = datetime.datetime.now().hour
    settings.enable_time_based_routing = True
    settings.time_based_night_start = current_hour
    settings.time_based_night_end = (current_hour + 1) % 24
    req_time = MessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content="hello")],
    )
    router = ModelRouter(settings)
    routed = router.resolve_messages_request(req_time)
    assert routed.resolved.provider_id == "aerolink"
    assert routed.resolved.provider_model == "claude-sonnet-4-6"
    settings.enable_time_based_routing = False

    # 7. Canary Mode (70)
    settings.enable_canary_mode = True
    settings.canary_provider_model = "openai/gpt-4o-mini"
    settings.canary_percentage = 100.0
    req_canary = MessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content="hello")],
    )
    router = ModelRouter(settings)
    routed = router.resolve_messages_request(req_canary)
    assert routed.resolved.provider_id == "openai"
    assert routed.resolved.provider_model == "gpt-4o-mini"
    settings.enable_canary_mode = False

    # 8. Cost-Aware Router (64)
    settings.enable_cost_aware_routing = True
    req_cost = MessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content="hello")],
    )
    router = ModelRouter(settings)
    routed = router.resolve_messages_request(req_cost)
    assert routed.resolved.provider_id == "aerolink"
    assert routed.resolved.provider_model == "claude-sonnet-4-6"
    settings.enable_cost_aware_routing = False

    # 9. Failover Memory (72)
    settings.enable_failover_memory = True
    ModelRouter.set_last_successful_provider("github_models")
    req_failover = MessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content="hello")],
    )
    router = ModelRouter(settings)
    routed = router.resolve_messages_request(req_failover)
    assert routed.resolved.provider_id == "github_models"
    settings.enable_failover_memory = False

    # 10. Provider Priority Groups (68)
    settings.provider_priority_groups = "tier-1:openai,gemini;tier-2:aerolink"
    settings.openai_api_key = "sk-test"
    req_priority = MessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content="hello")],
    )
    router = ModelRouter(settings)
    routed = router.resolve_messages_request(req_priority)
    assert routed.resolved.provider_id == "openai"
    settings.openai_api_key = ""
    settings.provider_priority_groups = ""

    # 11. Sticky Sessions (71)
    settings.enable_sticky_sessions = True
    router = ModelRouter(settings)
    headers = {"x-claude-session-id": "session_12345"}
    req_session1 = MessagesRequest(
        model="aerolink/claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content="hello")],
    )
    routed1 = router.resolve_messages_request(req_session1, headers=headers)
    assert routed1.resolved.provider_id == "aerolink"

    # Next request to generic model sonnet under same session should stick to aerolink
    req_session2 = MessagesRequest(
        model="claude-3-5-sonnet",
        max_tokens=100,
        messages=[Message(role="user", content="hello 2")],
    )
    routed2 = router.resolve_messages_request(req_session2, headers=headers)
    assert routed2.resolved.provider_id == "aerolink"
