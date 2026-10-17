from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

CURRENCY_BY_SYMBOL = {
    "$": "USD",
    "US$": "USD",
    "€": "EUR",
    "£": "GBP",
    "₹": "INR",
    "¥": "JPY",
    "₦": "NGN",
    "₽": "RUB",
    "R$": "BRL",
    "C$": "CAD",
    "A$": "AUD",
    "kr": "SEK",
    "zł": "PLN",
    "Kč": "CZK",
    "₺": "TRY",
    "₩": "KRW",
    "CHF": "CHF",
}

CURRENCY_CODES = {
    "USD",
    "EUR",
    "GBP",
    "INR",
    "JPY",
    "NGN",
    "RUB",
    "BRL",
    "CAD",
    "AUD",
    "SEK",
    "PLN",
    "CZK",
    "TRY",
    "KRW",
    "CHF",
    "NZD",
    "SGD",
    "ZAR",
    "MXN",
    "DKK",
    "NOK",
    "HUF",
    "RON",
}

# Currency symbol before amount, or ISO code before/after.
PRICE_RE = re.compile(
    r"(?P<symbol>US\$|C\$|A\$|R\$|[$€£₹¥₦₽₺₩]|CHF|zł|Kč|kr)?\s*"
    r"(?P<amount>\d{1,3}(?:[ \u00a0,.]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"\s*(?P<code>USD|EUR|GBP|INR|JPY|NGN|RUB|BRL|CAD|AUD|SEK|PLN|CZK|TRY|KRW|CHF|NZD|SGD|ZAR|MXN|DKK|NOK|HUF|RON)?"
    r"\s*(?P<symbol_after>US\$|C\$|A\$|R\$|[$€£₹¥₦₽₺₩]|CHF|zł|Kč|kr)?(?!\s*\d)",
    re.IGNORECASE,
)

PERIOD_PATTERNS = [
    (re.compile(r"(?:/|per\s*|a\s*)(?:mo|month)\b|\bmonthly\b|\bp/?m\b", re.I), "month", 1),
    (re.compile(r"(?:/|per\s*|a\s*)(?:yr|year)\b|\byearly\b|\bannual(?:ly)?\b|\bp/?y\b", re.I), "year", 12),
    (re.compile(r"(?:/|per\s*)week\b|\bweekly\b", re.I), "week", None),
    (re.compile(r"(?:/|per\s*)day\b|\bdaily\b", re.I), "day", None),
    (re.compile(r"billed\s+(?:every\s+)?(\d+)\s*(?:months?|years?)", re.I), "months", None),
    (re.compile(r"(?:/|per\s*)(\d+)\s*(?:months?|mo)\b", re.I), "months", None),
    (re.compile(r"(?:once|one[- ]time|setup fee)", re.I), "one_time", 0),
]

DISK_RE = re.compile(
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>TB|GB|MB)\s*"
    r"(?P<kind>NVMe|SSD|HDD|storage)\b",
    re.I,
)
DISK_SIZE_RE = re.compile(
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>TB|GB|MB)\b"
    r"(?!\s*(?:of\s*)?(?:RAM|memory))",
    re.I,
)
RAM_RE = re.compile(r"(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>TB|GB|MB)\s*(?:of\s*)?(?:RAM|memory)", re.I)
BANDWIDTH_RE = re.compile(
    r"(?P<amount>unlimited|unmetered|\d+(?:[.,]\d+)?)\s*(?P<unit>TB|GB|MB)?\s*"
    r"(?:of\s*)?(?:bandwidth|traffic|transfer)",
    re.I,
)


@dataclass(frozen=True)
class ParsedPrice:
    amount: Decimal
    currency: str | None
    raw: str


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = html.unescape(value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_label(value: str | None) -> str:
    return clean_text(value).lower()


def parse_price(raw: str, *, default_currency: str | None = None, symbol: str | None = None) -> ParsedPrice | None:
    """Parse the most plausible price from ``raw``.

    Several numbers can appear in a feature string ("8 GB RAM, $5/mo"), so
    candidates are scored by currency markers, billing-period proximity and
    spec-unit penalties instead of blindly taking the first number.
    """
    if not raw:
        return None
    text = clean_text(raw)
    text = re.sub(r"\bfrom\b", "", text, flags=re.I)
    candidates: list[tuple[float, ParsedPrice, int]] = []
    for match in PRICE_RE.finditer(text):
        amount_text = match.group("amount")
        if not amount_text:
            continue
        amount = _decimal_from(amount_text)
        if amount is None:
            continue
        after = text[match.end() : match.end() + 16].lower()
        before = text[max(0, match.start() - 16) : match.start()].lower()
        curr_symbol = (
            match.group("symbol") or match.group("symbol_after") or symbol or ""
        ).strip()
        code = (match.group("code") or "").upper() or None
        currency = code
        if not currency and curr_symbol:
            currency = CURRENCY_BY_SYMBOL.get(
                curr_symbol, curr_symbol if curr_symbol in CURRENCY_CODES else None
            )
        if not currency:
            currency = default_currency
        score = 0.0
        if code:
            score += 3.0
        if match.group("symbol") or match.group("symbol_after"):
            score += 2.6
        if re.match(r"\s*(?:/\s*(?:mo|month|yr|year)|per\s+(?:month|year)|monthly|yearly|annually|p/?[my]\b)", after):
            score += 2.0
        if re.search(r"(?:/|per\s+)(?:mo|month|yr|year)\b", before):
            score += 1.0
        if re.match(r"\s*(?:gb|tb|mb|ram|vcpu|vcores?|cores?|cpu|%|users?|sites?|websites?|days?|months?)\b", after):
            score -= 4.0
        if amount == 0:
            score -= 1.0
        if re.match(r"\s*(?:-|–|to)\s*\d", after):
            score -= 1.5
        candidates.append((score, ParsedPrice(amount=amount, currency=currency, raw=match.group(0).strip()), match.start()))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[2]))
    return candidates[0][1]


def _decimal_from(text: str) -> Decimal | None:
    text = text.replace(" ", "").replace("\u00a0", "")
    if "," in text and "." in text:
        # Whichever separator is right-most is the decimal separator.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        left, right = text.rsplit(",", 1)
        text = f"{left}.{right}" if len(right) in (1, 2) else text.replace(",", "")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover - guarded by regex
        return None


def parse_billing_period(text: str) -> tuple[str | None, int | None]:
    """Return ``(period, months)`` where period is month/year/week/day/one_time."""
    if not text:
        return None, None
    text = clean_text(text)
    for pattern, label, months in PERIOD_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if label == "months":
            count = int(match.group(1))
            return "month", count
        if label == "year" and months is None:
            count = int(match.group(1)) if match.groups() else 12
            return "year", count * 12
        return label, months
    return None, None


def parse_monthly_price(
    text: str, *, default_currency: str | None = None
) -> tuple[ParsedPrice | None, str | None, int | None]:
    price = parse_price(text, default_currency=default_currency)
    period, months = parse_billing_period(text)
    return price, period, months


def parse_capacity(text: str) -> dict[str, str]:
    """Extract storage/RAM/bandwidth hints from a plan feature string."""
    out: dict[str, str] = {}
    disk = DISK_RE.search(text or "")
    if disk:
        kind = (disk.group("kind") or "").strip()
        out["disk"] = f"{disk.group('amount')} {disk.group('unit').upper()}"
        if kind:
            out["disk_type"] = kind.upper()
    else:
        size = DISK_SIZE_RE.search(text or "")
        if size:
            out["disk"] = f"{size.group('amount')} {size.group('unit').upper()}"
    ram = RAM_RE.search(text or "")
    if ram:
        out["ram"] = f"{ram.group('amount')} {ram.group('unit').upper()}"
    bandwidth = BANDWIDTH_RE.search(text or "")
    if bandwidth:
        unit = (bandwidth.group("unit") or "").upper()
        out["bandwidth"] = (
            f"{bandwidth.group('amount')} {unit}".strip().lower()
            if bandwidth.group("amount").lower() not in {"unlimited", "unmetered"}
            else "unlimited"
        )
    return out


def truncate(value: str, length: int = 300) -> str:
    value = clean_text(value)
    return value if len(value) <= length else value[: length - 1].rstrip() + "…"


def contains_any(haystack: str, needles: list[str] | tuple[str, ...]) -> bool:
    lowered = haystack.lower()
    return any(needle.lower() in lowered for needle in needles)


def html_to_text(markup: str) -> str:
    from bs4 import BeautifulSoup

    return clean_text(BeautifulSoup(markup, "lxml").get_text(" "))
