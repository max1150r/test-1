"""Notificaciones por webhook de Discord: embeds, cola de envío, reintentos y deduplicación de errores."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from monitor.models import Event, EventKind, ProductInfo, Status
from monitor.stats import Stats
from utils.config import DiscordCfg
from utils.logger import log
from utils.text import format_eur, truncate

GREEN, BLUE, YELLOW, RED, GREY, PURPLE = 0x2ECC71, 0x3498DB, 0xF1C40F, 0xE74C3C, 0x95A5A6, 0x5865F2
FOOTER = "Carrefour Monitor"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(ts: float | None = None) -> str:
    return f"<t:{int(ts or time.time())}:T>"


def _source_label(event: Event) -> str:
    if event.source == "search":
        return f"Carrefour · Búsqueda «{event.query}»"
    if event.source == "listing":
        return f"Carrefour · Listado «{event.target_name}»"
    return "Carrefour · URL directa"


def _status_label(status: Status) -> str:
    return {Status.IN_STOCK: "✅ En stock", Status.OUT_OF_STOCK: "❌ Agotado"}.get(status, "❔ Desconocido")


def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": truncate(name, 256), "value": truncate(value or "—", 1024), "inline": inline}


def _wrap(cfg: DiscordCfg, embed: dict, content: str = "") -> dict:
    payload: dict = {"username": cfg.username or "Carrefour Monitor", "embeds": [embed]}
    if cfg.avatar_url:
        payload["avatar_url"] = cfg.avatar_url
    if content:
        payload["content"] = truncate(content, 2000)
        payload["allowed_mentions"] = {"parse": ["roles", "users", "everyone"]}
    else:
        payload["allowed_mentions"] = {"parse": []}
    return payload


def _base_embed(title: str, color: int, info: ProductInfo | None = None) -> dict:
    e: dict = {"title": truncate(title, 256), "color": color, "timestamp": _now_iso(), "footer": {"text": FOOTER}}
    if info is not None:
        e["url"] = info.url
        if info.image:
            e["image"] = {"url": info.image}
    return e


def build_stock_payload(event: Event, cfg: DiscordCfg) -> dict:
    i = event.info
    e = _base_embed("🟢 STOCK DISPONIBLE", GREEN, i)
    fields = [
        _field("Producto", f"[{truncate(i.display_name, 200)}]({i.url})", False),
        _field("Precio", format_eur(i.price)),
        _field("Estado", _status_label(i.status)),
        _field("Hora", _ts(i.detected_at)),
        _field("Fuente", _source_label(event), False),
    ]
    if i.seller:
        fields.append(_field("Vendedor", i.seller))
    if i.sku:
        fields.append(_field("SKU", i.sku))
    fields.append(_field("Comprar", f"[**COMPRAR**]({i.url})", False))
    e["fields"] = fields
    return _wrap(cfg, e, cfg.mention_on_stock)


def build_new_product_payload(event: Event, cfg: DiscordCfg) -> dict:
    i = event.info
    color = GREEN if i.status == Status.IN_STOCK else BLUE
    e = _base_embed("🔔 NUEVO PRODUCTO DETECTADO", color, i)
    fields = [
        _field("Nombre", f"[{truncate(i.display_name, 200)}]({i.url})", False),
        _field("Precio", format_eur(i.price)),
        _field("Disponibilidad", _status_label(i.status)),
        _field("Palabra clave", f"`{event.query}`" if event.query else (event.target_name or "—")),
        _field("Hora", _ts(i.detected_at)),
        _field("URL", i.url, False),
    ]
    if i.seller:
        fields.append(_field("Vendedor", i.seller))
    if i.sku:
        fields.append(_field("SKU", i.sku))
    e["fields"] = fields
    return _wrap(cfg, e, cfg.mention_on_stock if i.status == Status.IN_STOCK else "")


def build_price_payload(event: Event, cfg: DiscordCfg) -> dict:
    i = event.info
    old, new = event.previous_price, event.new_price
    diff = (new - old) if (old is not None and new is not None) else None
    pct = (diff / old * 100) if (diff is not None and old) else None
    color = GREEN if diff is not None and diff < 0 else YELLOW
    e = _base_embed("💰 CAMBIO DE PRECIO", color, i)
    sign = "+" if diff is not None and diff > 0 else ""
    e["fields"] = [
        _field("Producto", f"[{truncate(i.display_name, 200)}]({i.url})", False),
        _field("Precio anterior", format_eur(old)),
        _field("Precio nuevo", format_eur(new)),
        _field("Diferencia", f"{sign}{format_eur(diff)}" if diff is not None else "N/D"),
        _field("Cambio", f"{sign}{pct:.1f} %" if pct is not None else "N/D"),
        _field("URL", i.url, False),
    ]
    return _wrap(cfg, e)


def build_stock_lost_payload(event: Event, cfg: DiscordCfg) -> dict:
    i = event.info
    e = _base_embed("🔴 STOCK AGOTADO", GREY, i)
    e["fields"] = [
        _field("Producto", f"[{truncate(i.display_name, 200)}]({i.url})", False),
        _field("Precio", format_eur(i.price)),
        _field("Hora", _ts(i.detected_at)),
        _field("Fuente", _source_label(event), False),
    ]
    return _wrap(cfg, e)


def build_error_payload(title: str, details: str, cfg: DiscordCfg, target: str = "") -> dict:
    e = _base_embed("⚠️ MONITOR ERROR", RED)
    e["description"] = truncate(details, 1800)
    e["fields"] = [_field("Objetivo", target or "—"), _field("Tipo", title), _field("Hora", _ts())]
    return _wrap(cfg, e)


def build_startup_payload(cfg: DiscordCfg, *, url_products: int, searches: int, proxies: int,
                          interval: float, concurrency: int) -> dict:
    e = _base_embed("🚀 Carrefour Monitor iniciado", PURPLE)
    e["fields"] = [
        _field("Productos URL", str(url_products)),
        _field("Búsquedas", str(searches)),
        _field("Proxies cargados", str(proxies)),
        _field("Intervalo", f"{interval:g} segundos"),
        _field("Concurrencia", str(concurrency)),
    ]
    return _wrap(cfg, e)


def build_text_payload(cfg: DiscordCfg, title: str, text: str, color: int = PURPLE) -> dict:
    e = _base_embed(title, color)
    e["description"] = truncate(text, 3500)
    return _wrap(cfg, e)


def build_event_payload(event: Event, cfg: DiscordCfg) -> dict | None:
    """Devuelve el payload del evento o None si según la configuración no debe notificarse."""
    if event.kind == EventKind.STOCK_AVAILABLE:
        return build_stock_payload(event, cfg)
    if event.kind == EventKind.NEW_PRODUCT:
        return build_new_product_payload(event, cfg)
    if event.kind == EventKind.PRICE_CHANGE:
        return build_price_payload(event, cfg) if cfg.notify_price_changes else None
    if event.kind == EventKind.STOCK_LOST:
        return build_stock_lost_payload(event, cfg) if cfg.notify_out_of_stock else None
    return None


class DiscordNotifier:
    def __init__(self, cfg: DiscordCfg, stats: Stats | None = None):
        self.cfg = cfg
        self.stats = stats
        self.enabled = bool(cfg.webhook_url)
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._client = None
        self._error_last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._last_send = 0.0

    async def start(self) -> None:
        if not self.enabled:
            log.warning("Discord desactivado: no hay webhook configurado (DISCORD_WEBHOOK_URL).")
            return
        import httpx

        self._client = httpx.AsyncClient(timeout=self.cfg.timeout)   # TLS verificado por defecto
        self._queue = asyncio.Queue(maxsize=1000)
        self._worker = asyncio.create_task(self._run(), name="discord-worker")

    async def stop(self, drain_timeout: float = 10.0) -> None:
        if self._queue is not None and self._worker is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                log.warning("Quedaron notificaciones de Discord sin enviar al cerrar.")
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    # ---- API pública ----
    def enqueue(self, payload: dict | None, kind: str = "") -> None:
        if payload is None or not self.enabled or self._queue is None:
            return
        try:
            self._queue.put_nowait((payload, kind))
        except asyncio.QueueFull:
            log.error("Cola de Discord llena; se descarta una notificación (%s)", kind)

    def notify_event(self, event: Event) -> None:
        self.enqueue(build_event_payload(event, self.cfg), event.kind.value)

    def notify_error(self, key: str, title: str, details: str, target: str = "") -> bool:
        """Alerta de error con cooldown por clave. Devuelve True si se encola."""
        now = time.monotonic()
        last = self._error_last.get(key)
        if last is not None and now - last < self.cfg.error_cooldown:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False
        extra = self._suppressed.pop(key, 0)
        self._error_last[key] = now
        if extra:
            details += f"\n({extra} avisos iguales suprimidos)"
        self.enqueue(build_error_payload(title, details, self.cfg, target), "ERROR")
        return True

    # ---- envío ----
    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            payload, kind = await self._queue.get()
            try:
                ok = await self._post(payload)
                if self.stats:
                    if ok:
                        self.stats.notifications_sent += 1
                    else:
                        self.stats.notifications_failed += 1
                if ok:
                    log.info("Discord: notificación enviada (%s)", kind)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Discord: fallo inesperado al enviar (%s): %s", kind, type(exc).__name__)
            finally:
                self._queue.task_done()

    async def _post(self, payload: dict) -> bool:
        import httpx

        for attempt in range(4):
            gap = 0.35 - (time.monotonic() - self._last_send)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                resp = await self._client.post(self.cfg.webhook_url, json=payload)
                self._last_send = time.monotonic()
            except httpx.HTTPError as exc:
                log.warning("Discord: error de red (%s), reintento %d/4", type(exc).__name__, attempt + 1)
                await asyncio.sleep(min(2 ** attempt, 10))
                continue
            if resp.status_code in (200, 204):
                return True
            if resp.status_code == 429:
                try:
                    wait = float(resp.json().get("retry_after", 1.0))
                except Exception:
                    wait = float(resp.headers.get("retry-after", 1.0) or 1.0)
                log.warning("Discord: rate limit, esperando %.1fs", wait)
                await asyncio.sleep(min(wait, 30.0) + 0.1)
                continue
            if resp.status_code >= 500:
                await asyncio.sleep(min(2 ** attempt, 10))
                continue
            log.error("Discord: el webhook respondió HTTP %s (revisa la URL del webhook / el formato del mensaje)",
                      resp.status_code)
            if resp.status_code in (401, 403, 404):
                self.enabled = False
                log.error("Discord desactivado: el webhook parece inválido o eliminado.")
            return False
        return False

    async def send_now(self, payload: dict) -> bool:
        """Envío directo (para --test-discord). Requiere start() previo."""
        if not self.enabled or self._client is None:
            return False
        return await self._post(payload)
