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


KURS_KEY = "kurs:usd:idr"


async def get_kurs() -> float | None:
    """Kurs USDT → IDR (TTL 30 menit); ``None`` bila belum ada."""
    raw = await get_redis().get(KURS_KEY)
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


async def save_kurs(rate: float, ttl: int) -> None:
    await get_redis().set(KURS_KEY, str(rate), ex=int(ttl))


def price_raw_key(service: str, country: str) -> str:
    """Cache modal & stok provider (TANPA harga jual — mark_up dihitung saat dibaca)."""
    return f"priceraw:{str(service or '').strip().lower()}:{str(country or '').strip().lower()}"


async def get_price_raw(service: str, country: str) -> dict | None:
    raw = await get_redis().get(price_raw_key(service, country))
    return json.loads(raw) if raw else None


async def save_price_raw(service: str, country: str, data: dict, ttl: int) -> None:
    await get_redis().set(price_raw_key(service, country), json.dumps(data), ex=int(ttl))
