"""Utilidades de texto, precios y URLs (sin dependencias externas)."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """Minúsculas, sin acentos y con espacios colapsados (para comparar textos)."""
    return re.sub(r"\s+", " ", strip_accents(text or "").casefold()).strip()


_NUM = re.compile(r"\d[\d.,]*")


def parse_price(value, *, from_text: bool = False) -> float | None:
    """Convierte un valor a float.

    * ``19.54`` / ``"19.54"`` (JSON)           -> 19.54
    * ``"15,99 €"`` / ``"1.299,99 €"`` (texto)  -> 15.99 / 1299.99
    ``from_text=True`` indica que el texto viene de la página (formato español),
    por lo que ``"1.299"`` se interpreta como 1299 (punto de millares).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    m = _NUM.search(str(value))
    if not m:
        return None
    num = m.group(0).rstrip(".,")
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        num = num.replace(",", ".")
    elif "." in num and from_text:
        parts = num.split(".")
        if len(parts[-1]) == 3:
            num = "".join(parts)
    try:
        return round(float(num), 2)
    except ValueError:
        return None


_PRICE_TEXT = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+),(\d{2})\s*€")


def find_prices(text: str) -> list[float]:
    """Todos los precios con formato español (``15,99 €``) de un texto, en orden."""
    out = []
    for m in _PRICE_TEXT.finditer(text or ""):
        p = parse_price(m.group(0), from_text=True)
        if p is not None:
            out.append(p)
    return out


def format_eur(value: float | None) -> str:
    if value is None:
        return "N/D"
    s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def canonical_url(url: str) -> str:
    """URL sin query ni fragmento y con host en minúsculas."""
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme or "https", p.netloc.lower(), p.path, "", ""))


def with_query_param(url: str, key: str, value) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != key]
    q.append((key, str(value)))
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))
