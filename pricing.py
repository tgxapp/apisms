"""Kalkulasi harga jual OTP — satu-satunya tempat aturan harga di apisms.

Alur: modal provider (USD) → IDR (kurs USDT, dibulatkan ke atas) → otp_price() → + mark_up.
mark_up boleh Rp tetap (``500`` → +Rp500) atau persen (``"10%"`` → +10%):
10.000 + 500 = 10.500; 10.000 + 10% (1.000) = 11.000.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

MarkUp = tuple[str, float]  # ("flat", Rp) atau ("percent", persen)


def otp_price(capital: int) -> int:
    """Harga jual OTP berdasarkan modal (aturan sama dengan check_stock.py)."""
    markup_ranges = [
        (250, 300), (500, 350), (750, 400), (1000, 450),
        (1250, 500), (1500, 550), (1750, 600), (2000, 650),
        (2250, 700), (2499, 750), (2749, 750), (2999, 800),
        (3249, 850), (3499, 900), (3749, 950), (3999, 1000),
        (4000, 1100), (5000, 1200), (7500, 1400), (10000, 1600),
        (15000, 2000), (20000, 2500), (30000, 3000),
        (40000, 3500), (50000, 4000), (60000, 4500),
        (75000, 5000), (90000, 6000), (100000, 6500),
        (125000, 7500), (150000, 8500),
    ]
    for batas, profit in markup_ranges:
        if capital <= batas:
            return capital + profit
    return capital + 8500


def parse_mark_up(raw: object) -> MarkUp:
    """mark_up: angka = Rp tetap; teks berakhiran ``%`` = persen. Lainnya/kosong → ("flat", 0.0)."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.endswith("%"):
            try:
                return ("percent", float(text[:-1].strip() or 0))
            except ValueError:
                pass
        raw = text
    try:
        value = float(raw or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 0.0
    return ("flat", value)


def capital_idr(modal_usd: float, kurs: Decimal) -> int:
    """Modal USD → IDR, dibulatkan ke atas (Decimal agar 0.1 × kurs tidak salah bulat)."""
    return int((Decimal(str(modal_usd)) * kurs).to_integral_value(rounding=ROUND_CEILING))


def apply_mark_up(selling: int, mark_up: MarkUp) -> int:
    """otp_price(modal) + mark_up: flat → +Rp; persen → dibulatkan ke atas ke rupiah utuh."""
    kind, value = mark_up
    if kind == "percent":
        # Decimal, bukan float: 10350 × 1.1 dalam float = 11385.000000000002 → salah bulat.
        total = Decimal(selling) * (100 + Decimal(str(value))) / 100
        return int(total.to_integral_value(rounding=ROUND_CEILING))
    return selling + int(value)
