import hashlib
import hmac
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WIB = ZoneInfo("Asia/Jakarta")

# Isi password Redis di server (jangan di-commit): redis://default:<PASSWORD>@<host>:<port>
REDIS_URL = "redis://default:<PASSWORD_REDIS>@redis-12797.c292.ap-southeast-1-1.ec2.cloud.redislabs.com:12797"
ORDER_TTL = 24 * 60 * 60
# Hasil /check-country: 30 hari (daftar negara di provider jarang berubah).
COUNTRY_TTL = 30 * 24 * 60 * 60

# Kode mata uang provider -> USD (currency 840 = USD, 643 = RUB).
CURRENCY_USD = {840: 1.0, 643: 0.0125}


def read_json(path: Path) -> list | dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


APP = read_json(DATA_DIR / "providers.json")
PROVIDERS = APP.get("providers") or {}
API_KEY = str(APP.get("api_key") or "")

SERVICES = {item["id"].lower(): item for item in read_json(DATA_DIR / "id_service.json")}
COUNTRIES = {item["name"].lower(): item for item in read_json(DATA_DIR / "id_country.json")}


def check_api_key(value: str) -> bool:
    given = hashlib.sha256(str(value or "").strip().encode()).digest()
    expected = hashlib.sha256(API_KEY.encode()).digest()
    return bool(API_KEY) and hmac.compare_digest(given, expected)


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_wib(value) -> str:
    """Ubah waktu provider (UTC) ke WIB ``YYYY-MM-DD HH:MM:SS``; kosong/invalid diganti sekarang WIB."""
    try:
        dt = datetime.strptime(str(value or "").strip()[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S")


def normalize(providers: str, layanan: str, negara: str, modal_usd: float, raw: dict) -> dict:
    """Satukan respons semua provider (grizzly_sms, sms_bower, hero_sms) ke satu bentuk."""
    currency = _int_or_none(raw.get("currency"))
    cost = _float_or_none(raw.get("activationCost"))

    if currency in CURRENCY_USD:
        cost_usd = round(cost * CURRENCY_USD[currency], 4) if cost is not None else None
    else:
        cost_usd = cost  # sms_bower tanpa field currency, nilainya sudah USD

    # activationTime provider umumnya UTC (contoh grizzly_sms); simpan ke Redis dalam WIB.
    ordered_at = _to_wib(raw.get("activationTime"))

    try:
        expired_time = (datetime.strptime(ordered_at, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        expired_time = (datetime.now(WIB) + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "providers": providers,
        "activation_id": str(raw.get("activationId") or ""),
        "service": layanan,
        "country": negara,
        # Telethon membuang awalan "+": 628xx → 8628xx. Kirim tanpa "+" agar sama di webhook/Redis.
        "phone": str(raw.get("phoneNumber") or "").strip().lstrip("+"),
        "cost_usd": cost_usd,
        "modal_usd": modal_usd,
        "can_get_another_sms": str(raw.get("canGetAnotherSms")).lower() in {"1", "true"},
        "operator": raw.get("activationOperator"),
        "activation_time": ordered_at,
        "expired_time": expired_time,
        "raw": raw,
    }
