import asyncio
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from config.config import (
    COUNTRIES,
    KURS_FALLBACK,
    KURS_TTL,
    KURS_URL,
    PRICE_TTL,
    PROVIDERS,
    SERVICES,
    check_api_key,
)
from data.redis import get_kurs, get_price_raw, save_kurs, save_price_raw
from pricing import apply_mark_up, capital_idr, otp_price, parse_mark_up

router = APIRouter()

PRICE_PROVIDERS = ("hero_sms", "sms_bower", "grizzly_sms")


class CheckPriceRequest(BaseModel):
    api_key: str = Field(..., description="API key dari data/providers.json")
    device: str = Field("", description="Nama store (di-echo di respons, tidak mempengaruhi modal provider)")
    service: str = Field(..., description="ID/nama layanan, mis. telegram")
    country: str = Field(..., description="Nama/kode negara, mis. indonesia")
    mark_up: float | str | None = Field(
        None,
        description='Tambahan harga jual: 500 = +Rp500, "10%" = +10% dari harga dasar',
    )


class CheckPriceResponse(BaseModel):
    status: bool
    device: str = ""
    service: str = ""
    country: str = ""
    kurs_usdt_idr: float = 0.0
    providers: dict[str, dict] = Field(default_factory=dict)
    harga_jual: dict[str, dict] = Field(default_factory=dict)
    message: str = ""


def _price_map(raw: dict) -> dict[float, int]:
    """Format {harga: jumlah_kumulatif} provider → {harga: stok individu}."""
    price_map: dict[float, int] = {}
    prev = 0
    for price_str, cum in sorted(raw.items(), key=lambda x: float(x[0])):
        price_map[float(price_str)] = max(int(cum) - prev, 0)
        prev = int(cum)
    return price_map


async def _fetch_handler(session, base_url: str, api_key: str, country, service: str) -> dict:
    try:
        async with session.get(
            base_url,
            params={
                "action": "getPricesV2",
                "api_key": api_key,
                "country": country,
                "service": service,
            },
        ) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:
        return {"error": str(exc), "price_map": {}}

    raw = data.get(str(country), {}).get(service, {}) if isinstance(data, dict) else {}
    return {"error": None, "price_map": _price_map(raw)}


async def _fetch_hero(session, api_key: str, country, service: str) -> dict:
    try:
        async with session.get(
            "https://hero-sms.com/api/v1/activations/offers/sms",
            params={"services": service, "countries": str(country)},
            headers={"Authorization": f"ApiKey {api_key}"},
        ) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:
        return {"error": str(exc), "price_map": {}}

    svc = (
        (data.get("data") or {}).get(service, {}).get(str(country), {})
        if isinstance(data, dict)
        else {}
    )
    if not svc:
        return {"error": "Data kosong", "price_map": {}}
    return {"error": None, "price_map": _price_map(svc.get("map") or {})}


async def _fetch_provider(session, key: str, country_item: dict, service_item: dict) -> dict:
    cfg = PROVIDERS.get(key) or {}
    api_key = str(cfg.get("api_key") or "").strip()
    if not api_key:
        return {"error": "api_key kosong", "price_map": {}}

    code = service_item.get(key)
    country_id = country_item.get(key)
    if not code or not country_id:
        return {"error": "layanan/negara tidak tersedia", "price_map": {}}

    if key == "hero_sms":
        return await _fetch_hero(session, api_key, country_id, str(code))
    return await _fetch_handler(session, str(cfg.get("base_url") or ""), api_key, country_id, str(code))


async def _get_kurs(session) -> Decimal:
    """Kurs USDT → IDR dari Redis; kosong → ambil CoinGecko (TTL 30 menit); gagal → fallback."""
    cached = await get_kurs()
    if cached and cached > 0:
        return Decimal(str(cached))

    rate = 0
    try:
        async with session.get(KURS_URL, params={"ids": "tether", "vs_currencies": "idr"}) as resp:
            data = await resp.json(content_type=None)
        rate = int(str((data or {}).get("tether", {}).get("idr") or 0))
    except Exception as exc:
        print(f"[PRICE] gagal ambil kurs USDT: {exc}", flush=True)

    if rate <= 0:
        return Decimal(KURS_FALLBACK)

    await save_kurs(float(rate), KURS_TTL)
    return Decimal(str(rate))


async def _build_raw(session, country_item: dict, service_item: dict) -> dict:
    """Modal & stok per provider (TANPA harga jual) → di-cache 10 menit; mark_up dihitung saat dibaca."""
    kurs = await _get_kurs(session)
    results = await asyncio.gather(
        *(_fetch_provider(session, key, country_item, service_item) for key in PRICE_PROVIDERS),
        return_exceptions=True,
    )

    providers: dict[str, dict] = {}
    for key, res in zip(PRICE_PROVIDERS, results):
        if isinstance(res, Exception):
            providers[key] = {"error": str(res)}
            continue
        prices: dict[str, dict] = {}
        for modal_usd, stok in (res.get("price_map") or {}).items():
            if stok <= 0:
                continue
            prices[str(capital_idr(modal_usd, kurs))] = {"modal_usd": modal_usd, "stok": stok}
        providers[key] = {"error": res.get("error"), "prices": prices}

    return {"kurs": float(kurs), "providers": providers}


def _harga_jual(raw: dict, mark_up: tuple[str, float]) -> dict[str, dict]:
    """otp_price(modal) + mark_up → {harga: {profit_ress, profit_idr, providers}} terurut naik.

    ``profit_ress`` = nilai mark_up dalam rupiah (harga_jual − otp_price(modal)):
    flat = sama dengan nilai mark_up-nya; persen = selisih rupiahnya
    (contoh dasar 10000, mark_up 10% → profit_ress 1000). Markup 0 → 0
    (bot membiarkan kolom mark_up & profit_ress di tabel kosong).
    """
    offers: dict[str, dict] = {}
    for key, res in (raw.get("providers") or {}).items():
        for capital_str, info in (res.get("prices") or {}).items():
            capital = int(capital_str)
            base = otp_price(capital)
            selling = apply_mark_up(base, mark_up)
            offer = offers.setdefault(
                str(selling),
                {"profit_ress": selling - base, "profit_idr": 0, "providers": {}},
            )
            offer["profit_idr"] = selling - capital
            offer["providers"][key] = {
                "modal_usd": info.get("modal_usd"),
                "modal_idr": capital,
                "stok": info.get("stok"),
            }
    return {price: offers[price] for price in sorted(offers, key=lambda p: int(p))}


@router.post("/check-price", response_model=CheckPriceResponse)
async def check_price(payload: CheckPriceRequest, request: Request) -> CheckPriceResponse:
    print(
        f"[PRICE] masuk {payload.service} | {payload.country} | mark_up={payload.mark_up!r}",
        flush=True,
    )

    if not check_api_key(payload.api_key):
        print("[PRICE] 401 api_key tidak valid", flush=True)
        raise HTTPException(status_code=401, detail="api_key tidak valid.")

    device = str(payload.device or "").strip().lower()
    key = payload.service.strip().lower()
    svc = SERVICES.get(key) or next(
        (item for item in SERVICES.values() if str(item.get("name") or "").lower() == key),
        None,
    )
    ckey = payload.country.strip().lower()
    cty = COUNTRIES.get(ckey) or next(
        (item for item in COUNTRIES.values() if str(item.get("code") or "").lower() == ckey),
        None,
    )
    if not svc or not cty:
        print(
            f"[PRICE] layanan/negara tidak dikenal: {payload.service} | {payload.country}",
            flush=True,
        )
        return CheckPriceResponse(
            status=False,
            device=device,
            service=payload.service,
            country=payload.country,
            message="layanan/negara tidak dikenal",
        )

    service_id = str(svc.get("id") or payload.service)
    country_name = str(cty.get("name") or payload.country)
    mark_up = parse_mark_up(payload.mark_up)

    raw = await get_price_raw(service_id, country_name)
    if raw is None:
        raw = await _build_raw(request.app.state.session, cty, svc)
        await save_price_raw(service_id, country_name, raw, PRICE_TTL)

    return CheckPriceResponse(
        status=True,
        device=device,
        service=service_id,
        country=country_name,
        kurs_usdt_idr=float(raw.get("kurs") or 0),
        providers=raw.get("providers") or {},
        harga_jual=_harga_jual(raw, mark_up),
    )
