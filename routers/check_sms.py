import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from config.config import PROVIDERS, check_api_key
from data.redis import WIB, get_order, save_order

router = APIRouter()


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
async def check_sms(payload: CheckRequest, request: Request) -> CheckResponse:
    print(f"[CHECK] masuk {payload.providers} | {payload.activation_id} | {payload.nomor}", flush=True)

    if not check_api_key(payload.api_key):
        print("[CHECK] 401 api_key tidak valid", flush=True)
        raise HTTPException(status_code=401, detail="api_key tidak valid.")

    saved = await get_order(payload.providers, payload.activation_id, payload.service, payload.nomor)
    if not saved:
        print(f"[CHECK] 404 order {payload.providers}:{payload.activation_id}:{payload.service}:{payload.nomor} tidak ada", flush=True)
        raise HTTPException(status_code=404, detail="Order tidak ditemukan di Redis.")

    cfg = PROVIDERS[payload.providers]
    params = {"api_key": cfg["api_key"], "action": "getStatus", "id": payload.activation_id}

    async with request.app.state.session.get(cfg["base_url"], params=params) as resp:
        text = (await resp.text()).strip()
    print(f"[CHECK] provider {payload.providers} -> {text[:200]}", flush=True)

    # hero_sms balas JSON {"title": "...", "details": "..."}; grizzly_sms/sms_bower balas teks
    # "<STATUS>:<sms>". Status yang dikenal: STATUS_OK / STATUS_WAIT_*.
    raw = json.loads(text) if text.startswith("{") else {}
    if raw:
        head = str(raw.get("title") or "")
        detail = str(raw.get("details") or "")
    else:
        head = text.partition(":")[0]
        detail = text
    # grizzly_sms/sms_bower: "<STATUS_OK>:<kode>" → sms = kode; hero_sms pakai field JSON.
    sms = str(raw.get("sms") or ("" if raw else text.partition(":")[2]) or "").strip()
    code = str(raw.get("code") or "").strip()
    # can_get_another_sms dicatat saat /order-sms (dibuang pydantic kalau tidak diisi ulang).
    another = bool(saved.get("can_get_another_sms"))
    if head == "STATUS_OK":
        saved["status"] = "success"
        await save_order(payload.providers, payload.activation_id, payload.service, payload.nomor, saved)
        result = CheckResponse(
            status=True,
            providers=payload.providers,
            activation_id=payload.activation_id,
            service=payload.service,
            nomor=payload.nomor,
            sms=sms,
            code=code,
            can_get_another_sms=another,
            sms_received_at=datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S"),
        )
    elif head in {"STATUS_CANCEL", "STATUS_CANCELLED"}:
        # Order sudah dibatalkan (provider menolak SMS / saldo kembali) → berhenti dipantau.
        saved["status"] = "done"
        await save_order(payload.providers, payload.activation_id, payload.service, payload.nomor, saved)
        result = CheckResponse(
            status=False,
            providers=payload.providers,
            activation_id=payload.activation_id,
            service=payload.service,
            nomor=payload.nomor,
            message=head,
            can_get_another_sms=another,
        )
    elif head in {"STATUS_WAIT_CODE", "STATUS_WAIT_RETRY", "STATUS_WAIT_RESEND"}:
        result = CheckResponse(
            status=True,
            providers=payload.providers,
            activation_id=payload.activation_id,
            service=payload.service,
            nomor=payload.nomor,
            waiting=True,
            message=head,
            can_get_another_sms=another,
        )
    else:
        error = {"provider": payload.providers, "code": head or "empty_response", "message": detail}
        print(f"[CHECK] 502 gagal {payload.providers}: {error}", flush=True)
        raise HTTPException(status_code=502, detail=error)

    print(f"[CHECK] 200 {payload.providers}:{payload.activation_id} sms={result.sms!r} code={result.code!r} waiting={result.waiting}", flush=True)
    return result
