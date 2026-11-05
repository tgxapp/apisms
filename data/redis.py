import json
from datetime import datetime
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from config.config import ORDER_TTL, REDIS_URL

WIB = ZoneInfo("Asia/Jakarta")
_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _redis


def order_key(providers: str, activation_id: str, service: str, phone: str) -> str:
    return f"{providers}:{activation_id}:{service}:{phone}"


async def save_order(providers: str, activation_id: str, service: str, phone: str, data: dict) -> None:
    key = order_key(providers, activation_id, service, phone)
    await get_redis().set(key, json.dumps(data), ex=ORDER_TTL)
    print(f"[REDIS] set {key} ttl={ORDER_TTL}", flush=True)


async def get_order(providers: str, activation_id: str, service: str, phone: str) -> dict | None:
    raw = await get_redis().get(order_key(providers, activation_id, service, phone))
    return json.loads(raw) if raw else None


def block_key(service: str) -> str:
    return f"blok:{str(service or '').strip().lower()}"


async def get_blocked(service: str) -> set[str] | None:
    """Negara yang diblok (SET), ``None`` bila key belum ada (perlu cek provider)."""
    client = get_redis()
    key = block_key(service)
    if not await client.exists(key):
        return None
    return {str(item) for item in await client.smembers(key)}


async def save_blocked(service: str, countries: list[str], ttl: int) -> None:
    """Tambah negara yang tidak tersedia ke SET ``blok:{service}``; TTL diset saat set masih kosong."""
    client = get_redis()
    key = block_key(service)
    if not countries:
        return
    await client.sadd(key, *[str(item) for item in countries])
    if int(await client.ttl(key)) < 0:
        await client.expire(key, int(ttl))
    print(f"[REDIS] sadd {key} {len(countries)} negara ttl={ttl}", flush=True)
