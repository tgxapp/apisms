import asyncio
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config.config import COUNTRIES, COUNTRY_TTL, PROVIDERS, SERVICES, check_api_key
from data.redis import get_blocked, save_blocked
from routers.order_sms import get_session

router = APIRouter()

HERO_URL = "https://hero-sms.com/api/v1/activations/offers/sms"


class CheckCountryRequest(BaseModel):
    api_key: str = Field(..., description="API key dari data/providers.json")
    service: str = Field(..., description="ID/nama layanan, misal telegram")
    force: bool = Field(False, description="true = abaikan cache Redis, cek ulang ke provider")


class CheckCountryResponse(BaseModel):
    status: bool
    service: str = ""
    countries: list[str] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)
    message: str = ""


async def _get(session, url: str, params: dict, headers: dict | None = None):
    async with session.get(url, params=params, headers=headers) as resp:
        text = (await resp.text()).strip()
    try:
        return json.loads(text)
    except ValueError:
        return None


async def _hero(api_key: str, code: str) -> dict[str, dict] | None:
    """Satu layanan di semua negara: {country_id: map}. None = gagal."""
    data = await _get(get_session(), HERO_URL, {"services": code}, {"Authorization": f"ApiKey {api_key}"})
    if not isinstance(data, dict):
        return None
    svc = (data.get("data") or {}).get(code) or {}
    return {
        str(cid): (item or {}).get("map") or {}
        for cid, item in svc.items()
        if isinstance(item, dict)
    }


async def _handler(base_url: str, api_key: str, code: str) -> dict[str, dict] | None:
    """getPricesV2 tanpa country → {country_id: {harga: kumulatif}}. None = gagal."""
    data = await _get(get_session(), base_url, {"action": "getPricesV2", "api_key": api_key, "service": code})
    if not isinstance(data, dict) or "error" in data:
        return None
    return {
        str(cid): (services or {}).get(code) or {}
        for cid, services in data.items()
        if isinstance(services, dict)
    }


@router.post("/check-country", response_model=CheckCountryResponse)
async def check_country(payload: CheckCountryRequest) -> CheckCountryResponse:
    print(f"[COUNTRY] masuk {payload.service}", flush=True)

    if not check_api_key(payload.api_key):
        print("[COUNTRY] 401 api_key tidak valid", flush=True)
        raise HTTPException(status_code=401, detail="api_key tidak valid.")

    key = payload.service.strip().lower()
    services = SERVICES.get(key) or next(
        (item for item in SERVICES.values() if str(item.get("name") or "").lower() == key),
        None,
    )
    if not services:
        print(f"[COUNTRY] 404 layanan {payload.service} tidak dikenal", flush=True)
        raise HTTPException(status_code=404, detail=f"layanan tidak dikenal: {payload.service}")

    service_id = str(services.get("id") or payload.service)
    blocked = await get_blocked(service_id)
    if blocked is not None and not payload.force:
        # Cache SET blok:{service} = negara yang tidak tersedia (dipakai juga oleh bot).
        available = [item["name"] for item in COUNTRIES.values() if item["name"] not in blocked]
        unavailable = [item["name"] for item in COUNTRIES.values() if item["name"] in blocked]
        print(f"[COUNTRY] 200 {service_id} dari redis blok (tersedia={len(available)})", flush=True)
        return CheckCountryResponse(
            status=True,
            service=service_id,
            countries=available,
            unavailable=unavailable,
        )

    used: list[str] = []
    tasks = []
    for name in ("hero_sms", "sms_bower", "grizzly_sms"):
        code = services.get(name)
        cfg = PROVIDERS.get(name) or {}
        if not code or not cfg.get("api_key"):
            continue
        used.append(name)
        if name == "hero_sms":
            tasks.append(_hero(cfg["api_key"], str(code)))
        else:
            tasks.append(_handler(cfg["base_url"], cfg["api_key"], str(code)))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    found: set[str] = set()
    failed = 0
    for res in results:
        if isinstance(res, Exception) or res is None:
            failed += 1
            continue
        for cid, prices in res.items():
            if any(int(stock or 0) > 0 for stock in prices.values()):
                found.add(cid)

    if used and failed == len(used):
        print(f"[COUNTRY] 502 semua provider gagal {service_id}", flush=True)
        raise HTTPException(status_code=502, detail={"service": service_id, "message": "semua provider gagal"})

    available: list[str] = []
    unavailable: list[str] = []
    for item in COUNTRIES.values():
        ids = {str(item.get(name)) for name in used if item.get(name)}
        (available if ids & found else unavailable).append(item["name"])

    result = CheckCountryResponse(
        status=True,
        service=service_id,
        countries=available,
        unavailable=unavailable,
    )
    if failed < len(used):
        try:
            await save_blocked(service_id, unavailable, COUNTRY_TTL)
        except Exception as exc:
            print(f"[COUNTRY] gagal simpan redis: {exc}", flush=True)
    print(
        f"[COUNTRY] 200 {service_id} tersedia={len(available)} tidak={len(unavailable)} gagal={failed}/{len(used)}",
        flush=True,
    )
    return result
