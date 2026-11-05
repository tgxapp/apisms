import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Request

from config.config import PROVIDERS, WIB
from data.redis import get_order, get_redis

router = APIRouter()
# Lokasi activation_id di payload webhook provider (berbeda-beda antar provider).
ID_FIELDS = ("activationId", "activation_id", "id", "activation")
MAX_MESSAGES = 4


def _activation_id(raw: dict) -> str:
    for field in ID_FIELDS:
        value = raw.get(field)
        if value not in (None, ""):
            return str(value).strip()
    return ""


async def _handle(provider: str, raw: dict) -> None:
    activation_id = _activation_id(raw)
    text = str(raw.get("text") or raw.get("sms") or raw.get("message") or "").strip()
    if not activation_id:
        print(f"[WEBHOOK] {provider} payload tanpa activation_id, dilewati: {raw}", flush=True)
        return

    # Key order berformat {providers}:{activation_id}:{service}:{phone}.
    prefix = f"{provider}:{activation_id}:"
    keys = [key async for key in get_redis().scan_iter(match=f"{prefix}*", count=100)]
    if not keys:
        print(f"[WEBHOOK] {provider} order {prefix}* tidak ada di Redis (sudah expired/kosong)", flush=True)
        return

    for key in keys:
        _, _, service, phone = key.split(":", 3)
        saved = await get_order(provider, activation_id, service, phone) or {}
        code = str(raw.get("code") or "").strip()
        # Kode sama dengan slot terakhir = SMS ulangan, tidak perlu disimpan.
        last = next((saved[f"message_{n}"] for n in range(MAX_MESSAGES, 0, -1) if f"message_{n}" in saved), None)
        if last and str(last.get("code") or "").strip() == code:
            print(f"[WEBHOOK] {provider} {key} code {code!r} sudah ada, dilewati", flush=True)
            continue
        # Isi slot kosong pertama: message_1, lalu message_2, dst (maksimal 4).
        for n in range(1, MAX_MESSAGES + 1):
            slot = f"message_{n}"
            if slot not in saved:
                break
        else:
            print(f"[WEBHOOK] {provider} {key} message_1..{MAX_MESSAGES} sudah penuh, dilewati", flush=True)
            continue

        saved[slot] = {
            "text": text,
            "code": code,
            "received_at": (
                datetime.strptime(str(raw["receivedAt"])[:19], "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=ZoneInfo("UTC"))
                .astimezone(WIB)
                .strftime("%Y-%m-%d %H:%M:%S")
                if raw.get("receivedAt")
                else datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")
            ),
        }
        saved["status"] = "success"
        # keepttl: TTL 24 jam dari /order-sms tidak di-reset; tanpa ini key order jadi abadi.
        await get_redis().set(key, json.dumps(saved), keepttl=True)
        print(f"[WEBHOOK] {provider} {key} simpan {slot} text={text!r}", flush=True)


# Satu endpoint untuk ketiga provider: https://apisms.tgx.fyi/webhook/grizzly_sms dst.
@router.post("/webhook/{provider}")
async def webhook(provider: str, request: Request, tasks: BackgroundTasks) -> dict:
    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        return {"status": False, "message": f"provider tidak dikenal: {provider}"}

    body = await request.body()
    try:
        raw = json.loads(body or b"{}")
    except ValueError:
        raw = {"text": body.decode("utf-8", "ignore")}
    if not isinstance(raw, dict):
        raw = {"text": str(raw)}

    if "activationId" not in raw and "id" not in raw:
        # Sebagian provider mengirim form-encoded, bukan JSON.
        form = await request.form()
        raw = {**raw, **{k: str(v) for k, v in form.items()}}

    print(f"[WEBHOOK] {provider} masuk {raw}", flush=True)
    tasks.add_task(_handle, provider, raw)
    return {"status": True}
