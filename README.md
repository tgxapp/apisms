# apisms

API nomor SMS. Endpoint `POST /order-sms` (beli nomor), `POST /check-sms` (cek kode SMS), `POST /another-sms` (minta SMS tambahan), `POST /check-country` (daftar negara yang tersedia per layanan), `POST /check-price` (harga jual OTP + stok), dan `POST /webhook/{providers}` (notifikasi SMS dari provider). Hasil order disimpan ke Redis (TTL 24 jam).

```powershell
cd apisms
pip install -r requirements.txt
python main.py
```

```
apisms/
├─ main.py                 # jalankan server
├─ config/config.py        # konfigurasi + pembacaan json + normalize()
├─ routers/
│  ├─ order_sms.py         # endpoint POST /order-sms
│  ├─ check_sms.py         # endpoint POST /check-sms
│  ├─ check_country.py     # endpoint POST /check-country
│  ├─ check_price.py       # endpoint POST /check-price (modal provider → harga jual + mark_up)
│  ├─ another_sms.py       # endpoint POST /another-sms (minta SMS tambahan)
│  ├─ webhook.py           # endpoint POST /webhook/{providers} (notifikasi SMS dari provider)
└─ data/
   ├─ providers.json       # api_key provider + api_key klien
   ├─ id_service.json      # daftar layanan per provider
   ├─ id_country.json      # daftar negara per provider
   └─ redis.py             # simpan hasil order (key {providers}:{activation_id}:{service}:{phone}) + cache country:{service}
```

## Deploy ke VPS

Dengan domain (nginx + HTTPS otomatis):

```bash
chmod +x install.sh
sudo ./install.sh apisms.domainkamu.com
```

Script akan: install python3/nginx/certbot → copy ke `/var/www/apisms` → virtualenv + `requirements.txt` → systemd `apisms.service` (hypercorn 2 worker di `127.0.0.1:2020`) → nginx reverse proxy → HTTPS Let's Encrypt.

Tanpa domain (pakai IP VPS langsung):

```bash
chmod +x install.sh
sudo ./install.sh
```

nginx/certbot dilewati; hypercorn bind `0.0.0.0:2020` jadi API bisa diakses via `http://IP_VPS:2020` (order/check dilindungi `api_key`). Callback webhook di dashboard provider: `http://IP_VPS:2020/webhook/{providers}`. Port dibuka otomatis di ufw bila firewall aktif (security group cloud tetap dibuka manual di console). `/docs` ikut terbuka ke publik; tutup dengan `docs_url=None, redoc_url=None, openapi_url=None` di `main.py` bila perlu.

Cek log: `journalctl -u apisms -f`

## Dokumentasi otomatis

| URL | Isi |
|---|---|
| `/docs` | Swagger UI — bisa coba request langsung |
| `/redoc` | ReDoc |
| `/openapi.json` | schema OpenAPI |

Ingin dokumentasi tidak bisa diakses publik? Ubah `main.py` menjadi:

```python
app = FastAPI(title="apisms", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
```

`data/providers.json` berisi `api_key` per provider, `api_key` klien (field `api_key` di body request), dan `redis_url`. File ini **rahasia dan tidak ikut di repo** (ada di `.gitignore`) — buat manual di server dengan bentuk:

```json
{"api_key": "<api_key klien>", "redis_url": "<redis://... — opsional; kosong = Redis lokal 127.0.0.1:6379>", "providers": {"grizzly_sms": {"name": "grizzly_sms", "base_url": "https://...", "api_key": "..."}, "sms_bower": {"name": "sms_bower", "base_url": "https://...", "api_key": "..."}, "hero_sms": {"name": "hero_sms", "base_url": "https://...", "api_key": "..."}}}
```

## POST /order-sms

```json
{"api_key": "<api_key dari data/providers.json>", "providers": "hero_sms", "layanan": "telegram", "negara": "indonesia", "modal_usd": 0.2}
```

```json
{"status": true, "providers": "hero_sms", "activation_id": "1066819917", "service": "telegram", "country": "indonesia", "phone": "6285926556006", "cost_usd": 0.102, "modal_usd": 0.2, "can_get_another_sms": true, "operator": "axis", "activation_time": "2026-11-03 16:22:16", "expired_time": "2026-11-03 16:42:16", "message": ""}
```

- Semua provider (grizzly_sms, sms_bower, hero_sms) dikembalikan dengan **nama field yang sama** oleh `normalize()` di `config/config.py`; respons asli provider disimpan di field `raw`.
- `expired_time` = `activation_time` + 20 menit.
- Kalau `cost_usd` > `modal_usd` → `422` dengan `code: "cost_too_high"` dan **tidak** disimpan ke Redis.
- Error: `401` `api_key` salah, `404` layanan/negara tidak tersedia di provider, `422` body tidak valid (termasuk `api_key` tidak dikirim), `502` provider menolak.

## POST /check-sms

```json
{"api_key": "<api_key dari data/providers.json>", "providers": "hero_sms", "activation_id": "1066819917", "service": "telegram", "nomor": "6285926556006"}
```

```json
{"status": true, "providers": "hero_sms", "activation_id": "1066819917", "service": "telegram", "nomor": "6285926556006", "sms": "", "waiting": true, "message": "STATUS_WAIT_CODE", "can_get_another_sms": true}
```

Saat kode masuk (`STATUS_OK`):

```json
{"status": true, "providers": "grizzly_sms", "activation_id": "696151268", "service": "dana", "nomor": "6283823976329", "sms": "AWAS PENIPUAN! ... Kode OTP: 7814", "code": "7814", "waiting": false, "message": "", "can_get_another_sms": true, "sms_received_at": "2026-11-04 13:02:11"}
```

- `sms` terisi dan `waiting: false` saat kode sudah masuk (`STATUS_OK`); `code` dibaca langsung dari field `code` respons provider (JSON `grizzly_sms`/`sms_bower`), kosong bila provider tidak mengirimnya.
- `can_get_another_sms` diambil dari data `/order-sms` dan ikut dikirim di kedua status (dulu hilang, sehingga bot selalu menganggap `false`).
- `401` `api_key` salah, `404` bila `{providers}:{activation_id}:{service}:{nomor}` tidak ada di Redis, `502` bila provider menolak.

## POST /retry-sms

Minta SMS tambahan untuk order yang sama (provider `setStatus status=3`, hanya jika `can_get_another_sms: true`).

```json
{"api_key": "<api_key dari data/providers.json>", "providers": "hero_sms", "activation_id": "1066819917", "service": "telegram", "nomor": "6285926556006"}
```

```json
{"status": true, "providers": "hero_sms", "activation_id": "1066819917", "service": "telegram", "nomor": "6285926556006", "message": "ACCESS_RETRY_GET"}
```

- `message` = balasan mentah provider: `ACCESS_RETRY_GET` / `ACCESS_READY` dianggap sukses.
- `401` `api_key` salah, `404` order tidak ada di Redis, `429` batas 4 SMS tercapai, `502` provider menolak
  (mis. `BAD_STATUS` — belum ada SMS pertama / SMS berikutnya sudah diminta, `NO_ACTIVATION`, `BAD_ACTION`).

## POST /webhook/{providers}

Menerima notifikasi SMS dari provider (`grizzly_sms`, `sms_bower`, `hero_sms`). Didaftarkan di dashboard provider (kolom webhook/callback URL):

```
https://apisms.tgx.fyi/webhook/grizzly_sms
https://apisms.tgx.fyi/webhook/sms_bower
https://apisms.tgx.fyi/webhook/hero_sms
```

Payload yang diterima (gaya `handler_api.php`; nama field boleh `activationId`/`activation_id`/`id`/`activation`, teks boleh `text`/`sms`/`message`):

```json
{"activationId": 123456, "service": "go", "text": "Sms text", "code": "12345", "country": 2, "receivedAt": "2023-01-01 12:00:00"}
```

Respons selalu `200 {"status": true}` (kalau provider tidak dikenal → `{"status": false, ...}`), supaya provider tidak mengirim ulang.

- Order dicari dari pola key Redis `{providers}:{activation_id}:*`, lalu key tersebut diperbarui **tanpa mengubah TTL** (24 jam).
- SMS disimpan ke slot kosong pertama: `message_1`, lalu `message_2`, `message_3`, `message_4`. Bentuknya `{"text": "...", "code": "12345", "received_at": "..."}`.
- Kode yang sama dengan slot terakhir dianggap SMS ulangan dan **tidak** disimpan. Kode berbeda → slot berikutnya. Bila keempat slot sudah terisi, SMS berikutnya diabaikan.
- Dari payload hanya `text` dan `code` yang disimpan; field lain tidak dicatat.
- `/check-sms` tetap sumber utama status; webhook cuma menyimpan lebih awal.
- Tanpa `api_key` — endpoint publik. Kalau ingin membatasi, filter `167.235.198.205` (IP SMSC Bower) di nginx.
- Proses simpan berjalan sebagai background task (respons dikirim lebih dulu), jadi provider tetap dapat `200` cepat.

## POST /check-country

Cek negara mana yang punya stok di provider untuk satu layanan (query `getPricesV2` tanpa `country` di sms_bower/grizzly_sms, `offers/sms` di hero_sms).

```json
{"api_key": "<api_key dari data/providers.json>", "service": "tokopedia"}
```

Opsional `"force": true` → abaikan cache Redis dan cek ulang ke provider.

```json
{"status": true, "service": "tokopedia", "countries": ["indonesia", "gibraltar"], "unavailable": ["afghanistan", "..."], "message": ""}
```

- `countries` = negara yang ada stok di **minimal satu** provider; `unavailable` = sisanya (semua `id_country.json`).
- Hasil disimpan ke Redis `blok:{service}` selama **30 hari**; request berikutnya dijawab dari Redis tanpa menghubungi provider.
- Ini **sumber kebenaran** blok negara: bot membaca lewat endpoint ini dan menyimpan mirror read-through sendiri di Redis bot (TTL 24 jam) untuk memangkas panggilan — mirror selalu diisi ulang dari sini.
- Provider yang error dilewati. Kalau **semua** provider gagal → `502` (jangan anggap semua negara kosong); kalau hanya sebagian gagal, hasil tetap disimpan.
- `401` `api_key` salah, `404` layanan tidak dikenal.
- `service` menerima id (`telegram`) atau nama (`Telegram`), huruf besar/kecil bebas.

## POST /check-price

Harga jual OTP per layanan & negara: modal provider (USD) → IDR (kurs USDT, dibulatkan ke atas) → `otp_price()` → + `mark_up`. Satu-satunya tempat cek harga — bot memanggil endpoint ini, tidak mengakses provider langsung.

```json
{"api_key": "<api_key dari data/providers.json>", "device": "tgxnokos", "service": "telegram", "country": "indonesia", "mark_up": 500}
```

- `mark_up`: angka = tambahan Rp tetap (`500` → +Rp500); teks berakhiran `%` = persen (`"10%"` → +10%). Contoh: harga dasar 10.000 + `500` = 10.500; 10.000 + `"10%"` = 11.000. Kosong/invalid = 0.
- `device` hanya di-echo di respons (identitas pemanggil), tidak mempengaruhi modal provider.
- Respons: `{"status": true, "kurs_usdt_idr": 18100, "providers": {"hero_sms": {"error": null, "prices": {...}}, ...}, "harga_jual": {"10500": {"profit_idr": 1178, "providers": {"sms_bower": {"modal_usd": 0.51, "modal_idr": 9322, "stok": 12}}}}}`.
- Modal & stok di-cache di Redis `priceraw:{service}:{country}` **10 menit** — cache ini sengaja TANPA `mark_up`/`device`, jadi perubahan mark_up langsung berlaku tanpa menunggu cache.
- Bot menyimpan mirror respons akhir di Redis bot-nya (TTL 10 menit, key memuat tag `mark_up`) — endpoint ini hanya dihubungi saat mirror kosong.
- Kurs USDT → IDR di-cache `kurs:usd:idr` **30 menit** (CoinGecko); gagal diambil → fallback 18.100.
- `harga_jual` kosong = tidak ada stok di semua provider (bukan error).
- `401` `api_key` salah; `status: false` + `message` bila layanan/negara tidak dikenal.
