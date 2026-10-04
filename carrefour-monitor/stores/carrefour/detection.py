"""TODA la lógica de detección de Carrefour España está aquí.

Si Carrefour cambia su web, este es el único fichero que hay que tocar
(patrones de texto, etiquetas de botón, lectura de JSON-LD / JSON incrustado).

Estrategia (capas de señales independientes):
  1. JSON-LD (schema.org Product/Offer -> availability).
  2. JSON incrustado en <script> (solo objetos cuyo id/sku/ref/ean coincide con el producto).
  3. HTML visible del bloque principal del producto (alrededor del <h1>): texto
     «Agotado temporalmente» y botón «Añadir» habilitado. Se restringe al bloque principal
     para no confundirse con los carruseles de otros productos (que también tienen «Añadir»).

Decisión conservadora (prioridad: evitar falsos positivos):
  * texto visible de agotado  -> OUT_OF_STOCK (manda sobre cualquier dato estructurado)
  * señales de stock y de agotado contradictorias -> UNKNOWN
  * IN_STOCK solo si hay >= min_positive_signals fuentes distintas y ninguna negativa.

IMPORTANTE: los textos observados («Agotado temporalmente», «Añadir», «Vendido por», «REF./EAN»,
precio «15,99 €», og:image) provienen de la web real; los selectores/estructura JSON internos
NO se han podido verificar (no se inventan): por eso se usan señales genéricas. Valida con
`python main.py --inspect <URL>` contra la web real.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlsplit

from monitor.models import Status
from utils.text import find_prices, normalize, parse_price

try:
    from bs4 import BeautifulSoup, Tag
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Falta beautifulsoup4: pip install -r requirements.txt") from exc


def make_soup(html: str) -> "BeautifulSoup":
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # lxml no disponible
        return BeautifulSoup(html, "html.parser")


# ----------------------------------------------------------------- patrones
OOS_TEXT_PATTERNS = [re.compile(p) for p in (
    r"agotado temporalmente", r"\bagotado\b", r"\bsin stock\b", r"producto no disponible",
    r"temporalmente no disponible", r"no disponible (?:temporalmente|online|para (?:entrega|envio|compra))",
    r"\bavisame cuando\b", r"\bte avisamos cuando\b",
)]
BUY_LABELS = {"anadir", "anadir a la cesta", "anadir al carrito", "anadir a mi cesta", "anadir a cesta",
              "comprar", "comprar ahora", "anadir al carro"}
_BLOCK_TITLE_MARKERS = ("access denied", "acceso denegado", "just a moment", "attention required",
                        "pardon our interruption", "captcha", "robot", "request unsuccessful",
                        "verificacion", "verification")
_BLOCK_BODY_MARKERS = ("captcha", "px-captcha", "datadome", "cf-chl", "cf-browser-verification",
                       "incapsula incident", "unusual traffic", "are you a robot", "eres un robot",
                       "access denied", "acceso denegado", "pardon our interruption", "request unsuccessful")


@dataclass(frozen=True)
class Signal:
    source: str              # jsonld | embedded | html
    status: Status
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.source}:{self.status.value}({self.detail})"


@dataclass
class PageAnalysis:
    status: Status = Status.UNKNOWN
    reason: str = ""
    signals: list[Signal] = field(default_factory=list)
    price: float | None = None
    list_price: float | None = None
    name: str | None = None
    image: str | None = None
    seller: str | None = None
    sku: str | None = None
    ean: str | None = None
    ref: str | None = None


# ----------------------------------------------------------------- utilidades
def product_id_from_url(url: str) -> str | None:
    """https://www.carrefour.es/<slug>/<ID>/p  ->  <ID>"""
    path = [s for s in urlsplit(url).path.split("/") if s]
    if len(path) >= 2 and path[-1] == "p":
        return path[-2]
    return None


def is_product_path(href: str) -> bool:
    path = urlsplit(href).path
    return bool(re.search(r"/[^/]+/[^/]+/p/?$", path))


def interpret_availability(value: Any) -> Status | None:
    """Interpreta valores de disponibilidad (schema.org, booleanos, textos)."""
    if isinstance(value, bool):
        return Status.IN_STOCK if value else Status.OUT_OF_STOCK
    if isinstance(value, dict):
        return interpret_availability(value.get("@id") or value.get("name") or value.get("value"))
    if not isinstance(value, str):
        return None
    v = re.sub(r"[^a-z]", "", normalize(value).split("/")[-1])
    if not v:
        return None
    if any(k in v for k in ("outofstock", "soldout", "unavailable", "discontinued", "agotado",
                            "nostock", "sinstock", "nodisponible", "outofstock")):
        return Status.OUT_OF_STOCK
    if v in ("instock", "available", "disponible", "limitedavailability", "onlineonly") or v.endswith("instock"):
        return Status.IN_STOCK
    return None   # InStoreOnly, PreOrder, BackOrder...: no es compra online inmediata


def _is_disabled(tag: "Tag") -> bool:
    cls = " ".join(tag.get("class") or []).lower()
    return (tag.has_attr("disabled") or str(tag.get("aria-disabled", "")).lower() == "true"
            or "disabled" in cls)


def _label(tag: "Tag") -> str:
    text = tag.get_text(" ", strip=True) or tag.get("value") or tag.get("aria-label") or ""
    return normalize(str(text))


def _buy_buttons(scope: "Tag") -> list["Tag"]:
    out = []
    for t in scope.find_all(["button", "a", "input"]):
        lab = _label(t)
        if lab in BUY_LABELS or lab.startswith(("anadir a la cesta", "anadir al carrito", "anadir a mi cesta")):
            out.append(t)
    return out


# ----------------------------------------------------------------- bloque principal
def main_block(soup: "BeautifulSoup", own_id: str | None) -> "Tag | None":
    """Contenedor más grande alrededor del <h1> que NO incluye enlaces a otros productos."""
    h1 = soup.find("h1")
    if h1 is None:
        return None
    node = h1
    while node.parent is not None and getattr(node.parent, "name", None) not in (None, "[document]", "body", "html"):
        parent = node.parent
        foreign = False
        for a in parent.find_all("a", href=True):
            href = a["href"]
            if is_product_path(href) and not (own_id and own_id in href):
                foreign = True
                break
        if foreign:
            break
        node = parent
    return node


# ----------------------------------------------------------------- JSON
def _json_blobs(soup: "BeautifulSoup", ld_only: bool) -> Iterator[Any]:
    for s in soup.find_all("script"):
        typ = (s.get("type") or "").lower()
        is_ld = "ld+json" in typ
        if ld_only != is_ld:
            continue
        text = s.string or s.get_text() or ""
        text = text.strip()
        if not text or len(text) > 3_000_000:
            continue
        candidates = []
        if is_ld or "json" in typ or text[:1] in "{[":
            candidates.append(text)
        else:
            m = re.search(r"=\s*(\{.*\})\s*;?\s*$", text, re.S)
            if m:
                candidates.append(m.group(1))
        for c in candidates:
            try:
                yield json.loads(c)
            except (ValueError, TypeError):
                continue


def _walk(obj: Any) -> Iterator[dict]:
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else ([] if v is None else [v])


def _jsonld_products(soup: "BeautifulSoup") -> list[dict]:
    out = []
    for blob in _json_blobs(soup, ld_only=True):
        for d in _walk(blob):
            types = [str(t).lower() for t in _as_list(d.get("@type") or d.get("type"))]
            if "product" in types or ("offers" in d and ("name" in d or "sku" in d)):
                out.append(d)
    return out


def _offers(product: dict) -> list[dict]:
    res = []
    for o in _as_list(product.get("offers")):
        if isinstance(o, dict):
            nested = _as_list(o.get("offers"))
            if nested:
                res.extend(x for x in nested if isinstance(x, dict))
            res.append(o)
    return res


_AVAIL_KEYS = {"availability", "stockstatus", "stock_status", "instock", "in_stock", "isavailable",
               "is_available", "available", "outofstock", "out_of_stock", "isoutofstock"}
_ID_KEYS = {"id", "sku", "productid", "product_id", "ean", "ref", "reference", "itemid", "item_id", "code"}


def _embedded_signals(soup: "BeautifulSoup", ids: set[str]) -> tuple[list[Signal], float | None]:
    signals: list[Signal] = []
    price = None
    if not ids:
        return signals, price
    for blob in _json_blobs(soup, ld_only=False):
        for d in _walk(blob):
            if not any(str(d.get(k)) in ids for k in d if k.lower() in _ID_KEYS and d.get(k) is not None):
                continue
            for k, v in d.items():
                kl = k.lower()
                if kl in _AVAIL_KEYS:
                    st = interpret_availability(v)
                    if st is not None and kl in ("outofstock", "out_of_stock", "isoutofstock"):
                        st = Status.OUT_OF_STOCK if st == Status.IN_STOCK else Status.IN_STOCK
                    if st is not None:
                        signals.append(Signal("embedded", st, k))
                elif kl in ("price", "currentprice", "saleprice", "finalprice") and price is None:
                    price = parse_price(v)
    return signals, price


# ----------------------------------------------------------------- análisis principal
def pick_prices(texts: str) -> tuple[float | None, float | None]:
    """(precio_actual, precio_anterior_tachado) a partir del texto de la tarjeta/bloque.

    Ignora «Más ofertas desde X €» y «compras superiores a X €». Si aparecen dos precios
    seguidos (X € Y € con X > Y) se interpreta X como precio tachado.
    """
    clean = re.sub(r"(?i)desde\s*[\d.,]+\s*€", " ", texts)
    clean = re.sub(r"(?i)superiores?\s+a\s+[\d.,]+\s*€", " ", clean)
    clean = re.sub(r"(?i)hasta\s+[\d.,]+\s*€", " ", clean)
    prices = find_prices(clean)
    if not prices:
        return None, None
    if len(prices) >= 2 and prices[0] > prices[1]:
        return prices[1], prices[0]
    return prices[0], None


def analyze_product_page(html: str, url: str, min_positive_signals: int = 1) -> PageAnalysis:
    soup = make_soup(html)
    own_id = product_id_from_url(url)
    res = PageAnalysis()
    ids = {own_id} if own_id else set()
    if own_id and "-" in own_id:
        ids.add(own_id.split("-", 1)[1])
    res.sku = own_id

    block = main_block(soup, own_id)
    block_text = block.get_text("\n", strip=True) if block is not None else ""
    if block_text:
        if m := re.search(r"EAN\s*[:.]?\s*(\d{8,14})", block_text):
            res.ean = m.group(1)
            ids.add(res.ean)
        if m := re.search(r"REF\.?\s*[:.]?\s*(\d{5,14})", block_text):
            res.ref = m.group(1)
            ids.add(res.ref)

    # 1) JSON-LD
    ld_price = None
    for prod in _jsonld_products(soup):
        res.name = res.name or prod.get("name")
        img = prod.get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        if isinstance(img, dict):
            img = img.get("url")
        res.image = res.image or (img if isinstance(img, str) else None)
        if isinstance(prod.get("sku"), (str, int)) and not res.sku:
            res.sku = str(prod["sku"])
        for off in _offers(prod):
            if (st := interpret_availability(off.get("availability"))) is not None:
                res.signals.append(Signal("jsonld", st, "availability"))
            if ld_price is None:
                ld_price = parse_price(off.get("price") or off.get("lowPrice"))
            seller = off.get("seller")
            if isinstance(seller, dict) and seller.get("name") and not res.seller:
                res.seller = str(seller["name"])

    # 2) JSON incrustado
    emb_signals, emb_price = _embedded_signals(soup, ids)
    res.signals.extend(emb_signals)

    # 3) HTML visible del bloque principal
    if block is not None:
        low = normalize(block_text)
        for pat in OOS_TEXT_PATTERNS:
            if pat.search(low):
                res.signals.append(Signal("html", Status.OUT_OF_STOCK, pat.pattern))
                break
        buttons = _buy_buttons(block)
        if buttons:
            if any(not _is_disabled(b) for b in buttons):
                res.signals.append(Signal("html", Status.IN_STOCK, "boton_comprar"))
            else:
                res.signals.append(Signal("html", Status.OUT_OF_STOCK, "boton_comprar_deshabilitado"))
        if m := re.search(r"(?im)^vendido por\s*(.+)$", block_text):
            seller = re.sub(r"(?i)\s+con:?$", "", m.group(1)).strip()
            res.seller = res.seller or seller or None

    # metadatos
    if res.name is None:
        h1 = soup.find("h1")
        res.name = h1.get_text(" ", strip=True) if h1 else None
    if res.name is None:
        og = soup.find("meta", property="og:title")
        res.name = og.get("content") if og else None
    if res.image is None:
        og = soup.find("meta", property="og:image")
        res.image = og.get("content") if og and og.get("content") else None

    # precio
    meta_price = None
    for prop in ("product:price:amount", "og:price:amount"):
        m = soup.find("meta", attrs={"property": prop})
        if m and m.get("content"):
            meta_price = parse_price(m["content"])
            break
    text_price, text_list_price = pick_prices(block_text)
    res.price = next((p for p in (ld_price, meta_price, emb_price, text_price) if p is not None), None)
    if text_list_price and res.price is not None and text_list_price > res.price:
        res.list_price = text_list_price

    res.status, res.reason = decide(res.signals, min_positive_signals)
    if block is None and not res.signals:
        res.reason = "pagina_no_reconocida (sin <h1> ni datos estructurados)"
    return res


def decide(signals: list[Signal], min_positive_signals: int = 1) -> tuple[Status, str]:
    pos = [s for s in signals if s.status == Status.IN_STOCK]
    neg = [s for s in signals if s.status == Status.OUT_OF_STOCK]
    if any(s.source == "html" for s in neg):
        return Status.OUT_OF_STOCK, "texto_visible_agotado" + ("_prevalece_sobre_datos" if pos else "")
    if pos and neg:
        return Status.UNKNOWN, "senales_contradictorias"
    if neg:
        return Status.OUT_OF_STOCK, "datos_estructurados_agotado"
    if pos:
        sources = {s.source for s in pos}
        if len(sources) >= max(1, min_positive_signals):
            return Status.IN_STOCK, "+".join(sorted(sources))
        return Status.UNKNOWN, "confirmacion_insuficiente"
    return Status.UNKNOWN, "sin_senales"


def looks_blocked(status: int, text: str) -> bool:
    """¿La respuesta es un bloqueo / reto de verificación? (solo se CLASIFICA; no se intenta sortear)."""
    if status in (403, 429):
        return True
    m = re.search(r"<title[^>]*>(.*?)</title>", text[:20000], re.I | re.S)
    title = normalize(m.group(1)) if m else ""
    if title and any(k in title for k in _BLOCK_TITLE_MARKERS):
        return True
    if len(text) < 20000 and "<h1" not in text.lower():
        low = text.lower()
        return any(k in low for k in _BLOCK_BODY_MARKERS)
    return False
