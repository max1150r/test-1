"""CarrefourAdapter: implementación de StoreAdapter para carrefour.es."""
from __future__ import annotations

import re
from urllib.parse import quote, urljoin, urlsplit

from monitor.models import ProductInfo, SearchPage, Status
from stores.base import Fetcher, StoreAdapter
from stores.carrefour import detection as det
from utils.text import normalize, with_query_param


class CarrefourAdapter(StoreAdapter):
    name = "carrefour"

    def __init__(self, base_url: str = "https://www.carrefour.es",
                 search_url: str = "https://www.carrefour.es/?q={query}",
                 page_size: int = 24, offset_param: str = "offset", min_positive_signals: int = 1):
        self.base_url = base_url.rstrip("/")
        self.search_url = search_url
        self.page_size = page_size
        self.offset_param = offset_param
        self.min_positive_signals = min_positive_signals

    # ------------------------------------------------------------ URLs
    def is_product_url(self, url: str) -> bool:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        return p.scheme in ("http", "https") and (host == "carrefour.es" or host.endswith(".carrefour.es"))

    def build_search_url(self, query: str, page: int = 0) -> str:
        url = self.search_url.replace("{query}", quote(query.strip()))
        return self.page_url(url, page)

    def page_url(self, listing_url: str, page: int) -> str:
        return listing_url if page <= 0 else with_query_param(listing_url, self.offset_param, page * self.page_size)

    # ------------------------------------------------------------ parte pura
    def looks_blocked(self, status_code: int, text: str) -> bool:
        return det.looks_blocked(status_code, text)

    def detect_stock(self, html: str, url: str) -> tuple[Status, list[str]]:
        a = det.analyze_product_page(html, url, self.min_positive_signals)
        return a.status, [str(s) for s in a.signals]

    def extract_price(self, html: str, url: str) -> float | None:
        return det.analyze_product_page(html, url, self.min_positive_signals).price

    def extract_metadata(self, html: str, url: str) -> dict:
        a = det.analyze_product_page(html, url, self.min_positive_signals)
        return {"name": a.name, "image": a.image, "seller": a.seller, "sku": a.sku, "ean": a.ean, "ref": a.ref,
                "list_price": a.list_price}

    def parse_product(self, html: str, url: str) -> ProductInfo:
        a = det.analyze_product_page(html, url, self.min_positive_signals)
        return ProductInfo(store=self.name, url=url, status=a.status, sku=a.sku, name=a.name, price=a.price,
                           list_price=a.list_price, image=a.image, seller=a.seller, reason=a.reason,
                           signals=[str(s) for s in a.signals])

    def parse_listing(self, html: str, base_url: str) -> list[ProductInfo]:
        soup = det.make_soup(html)
        by_id: dict[str, list] = {}
        order: list[str] = []
        for a in soup.find_all("a", href=True):
            if not det.is_product_path(a["href"]):
                continue
            pid = det.product_id_from_url(urljoin(base_url, a["href"]))
            if not pid:
                continue
            if pid not in by_id:
                by_id[pid] = []
                order.append(pid)
            by_id[pid].append(a)

        items: list[ProductInfo] = []
        for pid in order:
            anchors = by_id[pid]
            card = self._card_for(anchors[0], pid)
            url = urljoin(base_url, anchors[0]["href"]).split("?")[0].split("#")[0]
            text = card.get_text("\n", strip=True) if card is not None else ""
            name = None
            for tag in (card.find_all(["h2", "h3", "h4"]) if card is not None else []):
                t = tag.get_text(" ", strip=True)
                if t:
                    name = t
                    break
            name = name or next((a.get("title") or a.get_text(" ", strip=True) for a in anchors
                                 if a.get("title") or a.get_text(" ", strip=True)), None)
            if not name and card is not None:
                img = card.find("img", alt=True)
                name = img["alt"].strip() if img and img["alt"].strip() else None
            image = None
            if card is not None:
                for img in card.find_all("img"):
                    src = img.get("src") or img.get("data-src") or ""
                    if src and not src.startswith("data:"):
                        image = urljoin(base_url, src)
                        break
            price, list_price = det.pick_prices(text)
            status = self._card_status(card, text)
            seller = None
            if m := re.search(r"(?im)^vendido por\s*(.+)$", text):
                seller = re.sub(r"(?i)\s+con:?$", "", m.group(1)).strip() or None
            items.append(ProductInfo(store=self.name, url=url, status=status, sku=pid, name=name, price=price,
                                     list_price=list_price, image=image, seller=seller,
                                     reason="listado" if status != Status.UNKNOWN else "listado_sin_estado"))
        return items

    @staticmethod
    def _card_status(card, text: str) -> Status:
        if card is None:
            return Status.UNKNOWN
        if "agotado" in normalize(text):
            return Status.OUT_OF_STOCK
        for b in det._buy_buttons(card):
            if not det._is_disabled(b):
                return Status.IN_STOCK
        return Status.UNKNOWN

    @staticmethod
    def _card_for(anchor, pid: str):
        """Sube desde el enlace mientras el contenedor solo contenga este producto."""
        node = anchor
        while node.parent is not None and getattr(node.parent, "name", None) not in (None, "[document]", "body", "html"):
            parent = node.parent
            foreign = False
            for a in parent.find_all("a", href=True):
                if det.is_product_path(a["href"]) and pid not in a["href"]:
                    foreign = True
                    break
            if foreign or len(parent.get_text(" ", strip=True)) > 2000:
                break
            node = parent
        return node

    # ------------------------------------------------------------ parte asíncrona
    async def get_product(self, client: Fetcher, url: str) -> ProductInfo:
        res = await client.fetch(url)
        base = dict(store=self.name, url=url, sku=det.product_id_from_url(url), response_time=res.elapsed,
                    proxy=res.proxy, http_status=res.status_code)
        if not res.ok:
            st = Status.BLOCKED if res.blocked else Status.ERROR
            return ProductInfo(status=st, reason=res.error or "error", **base)
        final = res.final_url or url
        if final and not det.product_id_from_url(final):
            return ProductInfo(status=Status.UNKNOWN, reason=f"redirigido fuera de la ficha ({urlsplit(final).path})", **base)
        info = self.parse_product(res.text, url)
        info.response_time, info.proxy, info.http_status = res.elapsed, res.proxy, res.status_code
        return info

    async def get_listing(self, client: Fetcher, url: str, page: int = 0) -> SearchPage:
        target = self.page_url(url, page)
        res = await client.fetch(target)
        if not res.ok:
            return SearchPage(ok=False, blocked=res.blocked, error=res.error, elapsed=res.elapsed,
                              proxy=res.proxy, http_status=res.status_code, url=target)
        items = self.parse_listing(res.text, res.final_url or target)
        return SearchPage(ok=True, items=items, elapsed=res.elapsed, proxy=res.proxy,
                          http_status=res.status_code, url=target)

    async def search(self, client: Fetcher, query: str, page: int = 0) -> SearchPage:
        return await self.get_listing(client, self.build_search_url(query, 0), page)
