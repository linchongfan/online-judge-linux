"""Rate limiter: fixed-window semantics and the atomic INCR+EXPIRE path."""

import uuid

import pytest
import redis

from app.core.config import get_settings
from app.services.rate_limit import RateLimiter


def test_local_fallback_enforces_limit_and_window(monkeypatch):
    limiter = RateLimiter()

    def unavailable():
        raise redis.RedisError("down")

    monkeypatch.setattr(limiter, "_client", unavailable)
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.services.rate_limit.monotonic", lambda: clock["now"])

    key = f"test:{uuid.uuid4().hex}"
    assert all(limiter.allow(key, 3, 60) for _ in range(3))
    assert limiter.allow(key, 3, 60) is False
    # Window expiry resets the counter.
    clock["now"] += 61
    assert limiter.allow(key, 3, 60) is True


def test_production_fails_closed_when_redis_down(monkeypatch):
    limiter = RateLimiter()

    def unavailable():
        raise redis.RedisError("down")

    monkeypatch.setattr(limiter, "_client", unavailable)
    monkeypatch.setattr(get_settings(), "app_env", "production")
    try:
        assert limiter.allow(f"test:{uuid.uuid4().hex}", 100, 60) is False
    finally:
        monkeypatch.setattr(get_settings(), "app_env", "development")


def test_redis_path_sets_ttl_atomically():
    """The first request must leave the key with a TTL; a crash between
    separate INCR and EXPIRE commands would rate-limit the key forever."""
    settings = get_settings()
    try:
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis not available in this environment")
    limiter = RateLimiter()
    key = f"test-rl:{uuid.uuid4().hex}"
    try:
        assert limiter.allow(key, 2, 60) is True
        ttl = client.ttl(key)
        assert 0 < ttl <= 60
        assert limiter.allow(key, 2, 60) is True
        assert limiter.allow(key, 2, 60) is False
    finally:
        client.delete(key)