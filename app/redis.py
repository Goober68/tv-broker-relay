"""
Async Redis connection pool.

Used for:
  - Dedup cache (SETNX + TTL)
  - Rate limiting (sorted set sliding window)
  - Stream price cache (Phase 2)
  - SSE pub/sub (Phase 3)

All callers should handle redis.RedisError gracefully — Redis is a cache,
not a critical dependency. Orders must never fail because Redis is down.
"""
import logging
import redis.asyncio as redis
from app.config import get_settings

logger = logging.getLogger(__name__)

_pool: redis.Redis | None = None


async def get_redis() -> redis.Redis | None:
    """
    Return the shared Redis connection pool.
    Returns None if Redis is not configured or connection fails.
    """
    global _pool
    if _pool is None:
        settings = get_settings()
        if not settings.redis_url:
            return None
        try:
            _pool = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            # Verify connectivity
            await _pool.ping()
            logger.info(f"Redis connected: {settings.redis_url}")
        except Exception:
            logger.warning("Redis unavailable — falling back to in-memory caches")
            _pool = None
    return _pool


async def close_redis():
    """Close the Redis connection pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
