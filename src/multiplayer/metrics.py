"""Stdlib-only Prometheus text-exposition metrics for the XYZZY server.

Single-process semantics: every counter, gauge, and histogram here lives in
this process's memory and resets when it restarts. A multi-node deployment
needs an external aggregator scraping each replica's /metrics; combining
counters across replicas is out of scope here.
"""

from __future__ import annotations

from collections import defaultdict

# A handful of fixed buckets spanning "instant" to "something is wrong",
# enough to answer "is the API fast" without per-route cardinality.
_LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


def status_class(status_code: int) -> str:
    """Collapse a status code to its class (2xx/4xx/...) to bound cardinality."""
    return f"{status_code // 100}xx"


class Metrics:
    """Process-wide counters/gauges/histogram, rendered as Prometheus text."""

    def __init__(self, version: str) -> None:
        self._version = version
        self._requests_total: dict[tuple[str, str], int] = defaultdict(int)
        self._rate_limited_total = 0
        self._model_tokens_total = 0
        self._redis_publish_failures_total = 0
        self._subscriber_queue_overflows_total = 0
        self._websocket_connections = 0
        self._request_seconds_bucket_counts: dict[float, int] = dict.fromkeys(_LATENCY_BUCKETS, 0)
        self._request_seconds_sum = 0.0
        self._request_seconds_count = 0

    def record_request(self, method: str, status_code: int, elapsed_seconds: float) -> None:
        self._requests_total[(method, status_class(status_code))] += 1
        for bound in _LATENCY_BUCKETS:
            if elapsed_seconds <= bound:
                self._request_seconds_bucket_counts[bound] += 1
        self._request_seconds_sum += elapsed_seconds
        self._request_seconds_count += 1

    def record_rate_limited(self) -> None:
        self._rate_limited_total += 1

    def record_model_tokens(self, tokens: int) -> None:
        self._model_tokens_total += max(tokens, 0)

    def record_redis_publish_failure(self) -> None:
        self._redis_publish_failures_total += 1

    def record_subscriber_queue_overflow(self) -> None:
        self._subscriber_queue_overflows_total += 1

    def set_websocket_connections(self, count: int) -> None:
        self._websocket_connections = count

    def render(self) -> str:
        lines = [
            "# HELP xyzzy_http_requests_total Total HTTP requests handled.",
            "# TYPE xyzzy_http_requests_total counter",
        ]
        for (method, status), value in sorted(self._requests_total.items()):
            lines.append(
                f'xyzzy_http_requests_total{{method="{method}",status="{status}"}} {value}'
            )

        lines += [
            "# HELP xyzzy_http_request_seconds HTTP request duration in seconds.",
            "# TYPE xyzzy_http_request_seconds histogram",
        ]
        for bound in _LATENCY_BUCKETS:
            lines.append(
                f'xyzzy_http_request_seconds_bucket{{le="{bound}"}} '
                f"{self._request_seconds_bucket_counts[bound]}"
            )
        lines.append(
            f'xyzzy_http_request_seconds_bucket{{le="+Inf"}} {self._request_seconds_count}'
        )
        lines.append(f"xyzzy_http_request_seconds_sum {self._request_seconds_sum}")
        lines.append(f"xyzzy_http_request_seconds_count {self._request_seconds_count}")

        lines += [
            "# HELP xyzzy_rate_limited_total Requests refused by the rate limiter.",
            "# TYPE xyzzy_rate_limited_total counter",
            f"xyzzy_rate_limited_total {self._rate_limited_total}",
            "# HELP xyzzy_model_tokens_total Tokens the model providers reported spending.",
            "# TYPE xyzzy_model_tokens_total counter",
            f"xyzzy_model_tokens_total {self._model_tokens_total}",
            "# HELP xyzzy_websocket_connections Live WebSocket subscriptions.",
            "# TYPE xyzzy_websocket_connections gauge",
            f"xyzzy_websocket_connections {self._websocket_connections}",
            "# HELP xyzzy_redis_publish_failures_total Redis fan-out publishes that failed.",
            "# TYPE xyzzy_redis_publish_failures_total counter",
            f"xyzzy_redis_publish_failures_total {self._redis_publish_failures_total}",
            "# HELP xyzzy_subscriber_queue_overflows_total "
            "Realtime events dropped because a subscriber's queue was full.",
            "# TYPE xyzzy_subscriber_queue_overflows_total counter",
            f"xyzzy_subscriber_queue_overflows_total {self._subscriber_queue_overflows_total}",
            "# HELP xyzzy_build_info Build metadata; the value is always 1.",
            "# TYPE xyzzy_build_info gauge",
            f'xyzzy_build_info{{version="{self._version}"}} 1',
        ]
        return "\n".join(lines) + "\n"
