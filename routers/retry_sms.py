import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config.config import PROVIDERS, check_api_key
from data.redis import get_order, save_order
from routers.check_sms import CheckRequest

router = APIRouter()
MAX_MESSAGES = 4


class RetryResponse(BaseModel):
    status: str
    providers: str = ""
    activation_id: str = ""
    service: str = ""
    nomor: str = ""
    message: str = ""


@router.post("/retry-sms", response_model=RetryResponse)
async def retry_sms(payload: CheckRequest, request: Request) -> RetryResponse:
    print(f"[RETRY] masuk {payload.providers} | {payload.activation_id} | {payload.nomor}", flush=True)

    if not check_api_key(payload.api_key):
        print("[RETRY] 401 api_key tidak valid", flush=True)
        raise HTTPException(status_code=401, detail="api_key tidak valid.")

    saved = await get_order(payload.providers, payload.activation_id, payload.service, payload.nomor)
    if not saved:
        print(f"[RETRY] 404 order {payload.providers}:{payload.activation_id}:{payload.service}:{payload.nomor} tidak ada", flush=True)
        raise HTTPException(status_code=404, detail="Order tidak ditemukan di Redis.")

    # Status mengikuti jumlah message_* yang sudah terisi: belum ada → retry_1, message_1 → retry_2, dst.
    filled = sum(1 for n in range(1, MAX_MESSAGES + 1) if f"message_{n}" in saved)
    if filled >= MAX_MESSAGES:
        print(f"[RETRY] 429 {payload.providers}:{payload.activation_id} message_1..{MAX_MESSAGES} penuh", flush=True)
        raise HTTPException(status_code=429, detail="Batas permintaan SMS sudah tercapai.")

    cfg = PROVIDERS[payload.providers]
    params = {"api_key": cfg["api_key"], "action": "getAnotherSms", "id": payload.activation_id}

    async with request.app.state.session.get(cfg["base_url"], params=params) as resp:
        body = (await resp.text()).strip()
    print(f"[RETRY] provider {payload.providers} http={resp.status} -> {body[:200]}", flush=True)

    # hero_sms menjawab JSON {"title": "...", "details": "..."}; grizzly/sms_bower menjawab teks.
    if body.startswith("{"):
        raw = json.loads(body)
        head = str(raw.get("title") or "")
        detail = str(raw.get("details") or "")
    else:
        head = body.partition(":")[0]
        detail = body

    # Provider membalas ACCESS_READY / ACCESS_RETRY_GET saat SMS baru diminta.
    if head not in {"ACCESS_READY", "ACCESS_RETRY_GET"}:
        error = {"provider": payload.providers, "code": head or "empty_response", "message": detail}
        print(f"[RETRY] 502 gagal {payload.providers}: {error}", flush=True)
        raise HTTPException(status_code=502, detail=error)

    # Status hanya: cost_too_high, waiting, success.
    saved["status"] = "waiting"
    await save_order(payload.providers, payload.activation_id, payload.service, payload.nomor, saved)
    result = RetryResponse(
        status=saved["status"],
        providers=payload.providers,
        activation_id=payload.activation_id,
        service=payload.service,
        nomor=payload.nomor,
        message=head,
    )
    print(f"[RETRY] 200 {payload.providers}:{payload.activation_id} {result.status} {head}", flush=True)
    return result
