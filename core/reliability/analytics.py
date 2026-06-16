from __future__ import annotations

import csv
import io
import math
import time
from collections import deque
from typing import Any

from loguru import logger

# Model cost definitions per 1 Million tokens (input, output)
MODEL_PRICING = {
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-3-haiku": (0.25, 1.25),
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-opus": (15.0, 75.0),
}


def estimate_cost(
    provider_id: str, model_id: str, input_tokens: int, output_tokens: int
) -> float:
    """Estimate request cost in USD. Free providers (Aerolink, GitHub Models) cost 0."""
    if provider_id in ("aerolink", "github_models", "github"):
        return 0.0

    # Match pricing based on substrings in model_id
    model_lower = model_id.lower()
    pricing = (0.0, 0.0)
    for model_name, prices in MODEL_PRICING.items():
        if model_name in model_lower:
            pricing = prices
            break
    else:
        # Generic fallback pricing (e.g. average API pricing)
        pricing = (1.0, 3.0)

    input_cost = (input_tokens / 1_000_000.0) * pricing[0]
    output_cost = (output_tokens / 1_000_000.0) * pricing[1]
    return input_cost + output_cost


class AnalyticsEngine:
    """Manages rolling request logs, calculates latency percentiles, tracks token consumption/costs, and monitors error rates."""

    def __init__(self, history_limit: int = 500) -> None:
        self.history_limit = history_limit
        # Thread-safe/async-safe deque of request logs
        self._history: deque[dict[str, Any]] = deque(maxlen=history_limit)

    def record_request(
        self,
        request_id: str,
        provider_id: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        total_latency_ms: float,
        first_byte_latency_ms: float,
        status_code: int,
        is_streaming: bool,
        fallback_triggered: bool = False,
    ) -> None:
        """Log a completed request to the analytics history."""
        cost = estimate_cost(provider_id, model_id, input_tokens, output_tokens)
        log_entry = {
            "timestamp": time.time(),
            "request_id": request_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost,
            "latency_ms": total_latency_ms,
            "first_byte_latency_ms": first_byte_latency_ms,
            "status_code": status_code,
            "is_streaming": is_streaming,
            "fallback_triggered": fallback_triggered,
        }
        self._history.append(log_entry)
        logger.debug(
            "Logged request {} to analytics: provider={}, latency={}ms, status={}, cost=${:.5f}",
            request_id,
            provider_id,
            round(total_latency_ms),
            status_code,
            cost,
        )

    def get_history(self) -> list[dict[str, Any]]:
        """Return full history list."""
        return list(self._history)

    def get_summary(self) -> dict[str, Any]:
        """Generate summary report, including latencies, provider split, cost, error rates, and metrics."""
        history = list(self._history)
        if not history:
            return {
                "total_requests": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "error_rate": 0.0,
                "avg_latency_ms": 0.0,
                "p50_latency_ms": 0.0,
                "p95_latency_ms": 0.0,
                "p99_latency_ms": 0.0,
                "avg_first_byte_latency_ms": 0.0,
                "provider_split": {},
                "streaming_percentage": 0.0,
                "fallback_count": 0,
            }

        total_requests = len(history)
        total_tokens = sum(item["total_tokens"] for item in history)
        total_cost = sum(item["cost_usd"] for item in history)
        errors = sum(1 for item in history if item["status_code"] >= 400)
        error_rate = (errors / total_requests) * 100.0

        latencies = sorted(item["latency_ms"] for item in history)
        avg_latency = sum(latencies) / total_requests

        # Percentile calculation
        def get_percentile(sorted_list: list[float], pct: float) -> float:
            idx = math.ceil((pct / 100.0) * len(sorted_list)) - 1
            return sorted_list[max(0, min(idx, len(sorted_list) - 1))]

        p50 = get_percentile(latencies, 50)
        p95 = get_percentile(latencies, 95)
        p99 = get_percentile(latencies, 99)

        fb_latencies = [
            item["first_byte_latency_ms"]
            for item in history
            if item["first_byte_latency_ms"] > 0
        ]
        avg_fb = sum(fb_latencies) / len(fb_latencies) if fb_latencies else 0.0

        # Provider splits
        provider_counts: dict[str, int] = {}
        for item in history:
            prov = item["provider_id"]
            provider_counts[prov] = provider_counts.get(prov, 0) + 1

        provider_split = {
            prov: (count / total_requests) * 100.0
            for prov, count in provider_counts.items()
        }

        streaming_count = sum(1 for item in history if item["is_streaming"])
        streaming_percentage = (streaming_count / total_requests) * 100.0

        fallback_count = sum(1 for item in history if item["fallback_triggered"])

        return {
            "total_requests": total_requests,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 4),
            "error_rate": round(error_rate, 1),
            "avg_latency_ms": round(avg_latency, 1),
            "p50_latency_ms": round(p50, 1),
            "p95_latency_ms": round(p95, 1),
            "p99_latency_ms": round(p99, 1),
            "avg_first_byte_latency_ms": round(avg_fb, 1),
            "provider_split": provider_split,
            "streaming_percentage": round(streaming_percentage, 1),
            "fallback_count": fallback_count,
        }

    def export_csv(self) -> str:
        """Export history to a CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "timestamp",
                "request_id",
                "provider_id",
                "model_id",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cost_usd",
                "latency_ms",
                "first_byte_latency_ms",
                "status_code",
                "is_streaming",
                "fallback_triggered",
            ]
        )
        for item in self._history:
            writer.writerow(
                [
                    item["timestamp"],
                    item["request_id"],
                    item["provider_id"],
                    item["model_id"],
                    item["input_tokens"],
                    item["output_tokens"],
                    item["total_tokens"],
                    item["cost_usd"],
                    item["latency_ms"],
                    item["first_byte_latency_ms"],
                    item["status_code"],
                    item["is_streaming"],
                    item["fallback_triggered"],
                ]
            )
        return output.getvalue()
