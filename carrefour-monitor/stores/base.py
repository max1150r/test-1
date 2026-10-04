"""Interfaz de tienda. Para añadir otra tienda (Amazon, MediaMarkt, GAME...) implementa StoreAdapter."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from monitor.models import ProductInfo, SearchPage, Status


class Fetcher(Protocol):
    async def fetch(self, url: str): ...


class StoreAdapter(ABC):
    name: str = "store"

    # ---- parte pura (HTML -> datos): fácil de testear con HTML simulado ----
    @abstractmethod
    def is_product_url(self, url: str) -> bool: ...

    @abstractmethod
    def build_search_url(self, query: str, page: int = 0) -> str: ...

    @abstractmethod
    def page_url(self, listing_url: str, page: int) -> str: ...

    @abstractmethod
    def detect_stock(self, html: str, url: str) -> tuple[Status, list[str]]: ...

    @abstractmethod
    def extract_price(self, html: str, url: str) -> float | None: ...

    @abstractmethod
    def extract_metadata(self, html: str, url: str) -> dict: ...

    @abstractmethod
    def parse_product(self, html: str, url: str) -> ProductInfo: ...

    @abstractmethod
    def parse_listing(self, html: str, base_url: str) -> list[ProductInfo]: ...

    @abstractmethod
    def looks_blocked(self, status_code: int, text: str) -> bool: ...

    # ---- parte asíncrona (usa el cliente HTTP) ----
    @abstractmethod
    async def get_product(self, client: Fetcher, url: str) -> ProductInfo: ...

    @abstractmethod
    async def search(self, client: Fetcher, query: str, page: int = 0) -> SearchPage: ...

    @abstractmethod
    async def get_listing(self, client: Fetcher, url: str, page: int = 0) -> SearchPage: ...
