import json

from fastapi import APIRouter, Request
from pydantic import BaseModel

from config.config import PROVIDERS, check_api_key
from data.redis import get_order
from routers.check_sms import CheckRequest
from routers.retry_sms import MAX_MESSAGES

router = APIRouter()


class CancelResponse(BaseModel):
    status: str
    providers: str = ""
    activation_id: str = ""
    service: str = ""
    nomor: str = ""
    message: str = ""


@router.post("/cancel-sms", response_model=CancelResponse)
async def cancel_sms(payload: CheckRequest, request: Request) -> CancelResponse:
    print(f"[CANCEL] masuk {payload.providers} | {payload.activation_id} | {payload.nomor}", flush=True)

    if not check_api_key(payload.api_key):
        print("[CANCEL] 401 api_key tidak valid", flush=True)
        return CancelResponse(status="cancel_failed", message="api_key tidak valid.")

    saved = await get_order(payload.providers, payload.activation_id, payload.service, payload.nomor)
    # Sudah ada OTP → tidak dibatalkan. Selain itu langsung cancel (batas waktu milik provider).
    if saved and any(f"message_{n}" in saved for n in range(1, MAX_MESSAGES + 1)):
        print(f"[CANCEL] gagal {payload.providers}:{payload.activation_id} sudah ada OTP", flush=True)
        return CancelResponse(
            status="cancel_failed",
            providers=payload.providers,
            activation_id=payload.activation_id,
            service=payload.service,
            nomor=payload.nomor,
            message="Order sudah menerima OTP.",
        )

    cfg = PROVIDERS[payload.providers]
    # Semua provider kompatibel sms-activate: setStatus status=8 → pending dibatalkan & saldo kembali.
    params = {"api_key": cfg["api_key"], "action": "setStatus", "id": payload.activation_id, "status": 8}

    async with request.app.state.session.get(cfg["base_url"], params=params) as resp:
        body = (await resp.text()).strip()
    print(f"[CANCEL] provider {payload.providers} http={resp.status} -> {body[:200]}", flush=True)

    # hero_sms menjawab JSON {"title": "...", "details": "..."}; grizzly/sms_bower menjawab teks.
    if body.startswith("{"):
        raw = json.loads(body)
        head = str(raw.get("title") or "")
        detail = str(raw.get("details") or "")
    else:
        head = body.partition(":")[0]
        detail = body

    # Berhasil: cukup status, field lain dikosongkan.
    if head == "ACCESS_CANCEL":
        print(f"[CANCEL] cancel_ok {payload.providers}:{payload.activation_id}", flush=True)
        return CancelResponse(status="cancel_ok")

    print(f"[CANCEL] cancel_failed {payload.providers}:{payload.activation_id} {head or detail}", flush=True)
    return CancelResponse(
        status="cancel_failed",
        providers=payload.providers,
        activation_id=payload.activation_id,
        service=payload.service,
        nomor=payload.nomor,
        message=head or detail or "empty_response",
    )
