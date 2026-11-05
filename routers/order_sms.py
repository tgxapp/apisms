import hmac
import json
import time

import aiohttp
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from config.config import COUNTRIES, ORDER_TTL, PROVIDERS, SERVICES, check_api_key, normalize
from data.redis import save_order

router = APIRouter()
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers=UA,
            connector=aiohttp.TCPConnector(limit=200, ttl_dns_cache=300, keepalive_timeout=60),
        )
    return session


async def close_session() -> None:
    global session
    if session is not None and not session.closed:
        await session.close()
    session = None


class OrderRequest(BaseModel):
    api_key: str = Field(..., description="API key dari data/providers.json")
    providers: str = Field(..., description="hero_sms, sms_bower, atau grizzly_sms")
    layanan: str = Field(..., description="ID/nama layanan, misal whatsapp")
    negara: str = Field(..., description="Nama/kode negara, misal indonesia atau id")
    modal_usd: float = Field(..., gt=0, description="Modal dari provider (USD)")

    @field_validator("providers")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        if value.strip().lower() not in PROVIDERS:
            raise ValueError(f"provider tidak dikenal: {value}")
        return value.strip().lower()


class OrderResponse(BaseModel):
    status: str = "waiting"
    providers: str = ""
    activation_id: str = ""
    service: str = ""
    country: str = ""
    phone: str = ""
    cost_usd: float | None = None
    modal_usd: float = 0
    can_get_another_sms: bool = False
    operator: str | None = None
    activation_time: str | None = None
    expired_time: str | None = None


@router.post("/order-sms", response_model=OrderResponse)
async def order(payload: OrderRequest) -> OrderResponse:
    print(f"[ORDER] masuk {payload.providers} | {payload.layanan} / {payload.negara} | modal ${payload.modal_usd}", flush=True)

    if not check_api_key(payload.api_key):
        print("[ORDER] 401 api_key tidak valid", flush=True)
        raise HTTPException(status_code=401, detail="api_key tidak valid.")

    key = payload.layanan.strip().lower()
    svc = SERVICES.get(key) or next(
        (item for item in SERVICES.values() if str(item.get("name") or "").lower() == key),
        None,
    )
    cty = COUNTRIES.get(payload.negara.strip().lower()) or next(
        (item for item in COUNTRIES.values() if str(item.get("code") or "").lower() == payload.negara.strip().lower()),
        None,
    )
    service_code = svc.get(payload.providers) if svc else None
    country_id = cty.get(payload.providers) if cty else None
    if not service_code or not country_id:
        print(f"[ORDER] 404 {payload.layanan} / {payload.negara} tidak tersedia di {payload.providers}", flush=True)
        raise HTTPException(status_code=404, detail=f"{payload.layanan} / {payload.negara} tidak tersedia di {payload.providers}.")

    cfg = PROVIDERS[payload.providers]
    params = {
        "api_key": cfg["api_key"],
        "action": "getNumberV2",
        "service": service_code,
        "country": country_id,
    }
    started = time.perf_counter()
    async with get_session().get(cfg["base_url"], params=params) as resp:
        text = (await resp.text()).strip()
    elapsed = time.perf_counter() - started
    print(f"[ORDER] provider {payload.providers} kode={service_code}/{country_id} {elapsed:.2f}s -> {text[:200]}", flush=True)

    try:
        raw = json.loads(text)
    except ValueError:
        raw = {}

    if not isinstance(raw, dict):
        raw = {}
    if not raw.get("activationId"):
        head, _, value = text.partition(":")
        activation_id, _, phone = value.partition(":")
        if head == "ACCESS_NUMBER":
            raw = {"activationId": activation_id, "phoneNumber": phone}

    if not raw.get("activationId") or not raw.get("phoneNumber"):
        error = {"code": raw.get("title") or text.split(":", 1)[0] or "empty_response", "message": raw.get("details") or text}
        print(f"[ORDER] 502 gagal {payload.providers}: {error}", flush=True)
        raise HTTPException(status_code=502, detail={"provider": payload.providers, **error})

    result = OrderResponse(**normalize(payload.providers, payload.layanan, payload.negara, payload.modal_usd, raw))

    if result.cost_usd is not None and result.cost_usd > payload.modal_usd:
        print(f"[ORDER] 422 ditolak {payload.providers}: cost_usd={result.cost_usd} > modal_usd={payload.modal_usd}", flush=True)
        raise HTTPException(
            status_code=422,
            detail={
                "status": "cost_too_high",
                "providers": result.providers,
                "activation_id": result.activation_id,
                "service": result.service,
                "country": result.country,
                "phone": result.phone,
                "cost_usd": result.cost_usd,
                "modal_usd": result.modal_usd,
            },
        )

    await save_order(result.providers, result.activation_id, result.service, result.phone, result.model_dump())
    print(f"[ORDER] 200 sukses {result.providers}:{result.activation_id}:{result.service}:{result.phone} cost_usd={result.cost_usd} ttl={ORDER_TTL}s", flush=True)
    return result
