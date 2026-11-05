from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from config.config import PROVIDERS, check_api_key
from data.redis import get_order

router = APIRouter()
MAX_MESSAGES = 4


class CheckRequest(BaseModel):
    api_key: str = Field(..., description="API key dari data/providers.json")
    providers: str = Field(..., description="hero_sms, sms_bower, atau grizzly_sms")
    activation_id: str = Field(..., description="activation_id dari /order-sms")
    service: str = Field(..., description="Nama layanan, misal telegram")
    nomor: str = Field(..., description="Nomor dari /order-sms")

    @field_validator("providers")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        if value.strip().lower() not in PROVIDERS:
            raise ValueError(f"provider tidak dikenal: {value}")
        return value.strip().lower()


class CheckResponse(BaseModel):
    status: bool
    providers: str = ""
    activation_id: str = ""
    service: str = ""
    nomor: str = ""
    sms: str = ""
    code: str = ""
    waiting: bool = False
    message: str = ""
    can_get_another_sms: bool = False
    sms_received_at: str = ""


@router.post("/check-sms", response_model=CheckResponse)
async def check_sms(payload: CheckRequest) -> CheckResponse:
    """Cukup baca Redis. OTP dikirim provider ke webhook ``/webhook/{providers}`` dan disimpan
    sebagai ``message_1..4``; endpoint ini tidak memanggil API provider lagi."""
    print(f"[CHECK] masuk {payload.providers} | {payload.activation_id} | {payload.nomor}", flush=True)

    if not check_api_key(payload.api_key):
        print("[CHECK] 401 api_key tidak valid", flush=True)
        raise HTTPException(status_code=401, detail="api_key tidak valid.")

    saved = await get_order(payload.providers, payload.activation_id, payload.service, payload.nomor)
    if not saved:
        print(f"[CHECK] 404 order {payload.providers}:{payload.activation_id}:{payload.service}:{payload.nomor} tidak ada", flush=True)
        raise HTTPException(status_code=404, detail="Order tidak ditemukan di Redis.")

    # SMS terakhir = slot message_* tertinggi yang terisi (message_4 > message_3 > ...).
    latest = next(
        (saved[f"message_{n}"] for n in range(MAX_MESSAGES, 0, -1) if f"message_{n}" in saved),
        None,
    )
    another = bool(saved.get("can_get_another_sms"))
    if latest:
        result = CheckResponse(
            status=True,
            providers=payload.providers,
            activation_id=payload.activation_id,
            service=payload.service,
            nomor=payload.nomor,
            sms=str(latest.get("text") or "").strip(),
            code=str(latest.get("code") or "").strip(),
            can_get_another_sms=another,
            sms_received_at=str(latest.get("received_at") or ""),
        )
    else:
        result = CheckResponse(
            status=True,
            providers=payload.providers,
            activation_id=payload.activation_id,
            service=payload.service,
            nomor=payload.nomor,
            waiting=True,
            message="STATUS_WAIT_CODE",
            can_get_another_sms=another,
        )

    print(f"[CHECK] 200 {payload.providers}:{payload.activation_id} sms={result.sms!r} code={result.code!r} waiting={result.waiting}", flush=True)
    return result
