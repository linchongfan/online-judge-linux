import redis
from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import Db
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.models import utcnow

router = APIRouter()

# Shared pooled client: health checks run every few seconds and must not
# open a fresh connection per probe.
_redis_client: redis.Redis | None = None


def _health_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1
        )
    return _redis_client


@router.get("/health/live")
def live():
    return {"status": "ok", "serverTime": utcnow()}


@router.get("/health/ready")
def ready(db: Db):
    db.execute(text("SELECT 1"))
    # Rate limiting, verification codes and the judge result queue all depend
    # on Redis; an API that cannot reach it is not ready to serve traffic.
    try:
        _health_redis().ping()
    except redis.RedisError as exc:
        raise AppError(503, "REDIS_UNAVAILABLE", "缓存服务暂时不可用") from exc
    return {"status": "ready", "serverTime": utcnow()}
