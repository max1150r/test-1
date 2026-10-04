"""Ensamblado de la aplicación y ciclo de vida (arranque / cierre limpio)."""
from __future__ import annotations

import asyncio
import signal
import sys

from monitor.detector import ChangeDetector
from monitor.product_monitor import ProductMonitor
from monitor.scheduler import Scheduler
from monitor.search_monitor import SearchMonitor
from monitor.state import StateStore
from monitor.stats import Stats
from network.client import HttpClient
from network.proxy_manager import ProxyManager
from notifications import discord as dc
from notifications.discord import DiscordNotifier
from stores.carrefour.adapter import CarrefourAdapter
from utils.config import AppConfig, ConfigError
from utils.logger import log, register_secret, setup_logging


class Application:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        register_secret(cfg.discord.webhook_url)
        self.stats = Stats()
        self.adapter = CarrefourAdapter(
            base_url=cfg.store.base_url, search_url=cfg.store.search_url, page_size=cfg.store.page_size,
            offset_param=cfg.store.offset_param, min_positive_signals=cfg.monitor.min_positive_signals)
        self.store = StateStore(cfg.resolve(cfg.state.db_path))
        self.proxies = ProxyManager.from_file(
            cfg.resolve(cfg.network.proxies_file), strategy=cfg.network.proxy_strategy,
            cooldown=cfg.network.proxy_cooldown, max_failures=cfg.network.proxy_max_failures)
        self.client = HttpClient(cfg.monitor, cfg.network, self.proxies, self.adapter.looks_blocked)
        self.notifier = DiscordNotifier(cfg.discord, self.stats)
        self.detector = ChangeDetector(self.store, self.stats, cfg.monitor.stock_renotify_cooldown)
        self.monitors = self._build_monitors()
        self.scheduler = Scheduler(self.monitors, cfg.monitor)
        self._health_task: asyncio.Task | None = None
        self._stopping = False
        self._closed = False

    # ------------------------------------------------------------------
    def _build_monitors(self):
        mon = self.cfg.monitor
        out = []
        for t in self.cfg.targets:
            if t.kind in ("url", "listing") and not self.adapter.is_product_url(t.value):
                raise ConfigError(f"'{t.name}': la URL no pertenece a carrefour.es -> {t.value}")
            interval = t.interval if t.interval is not None else mon.interval
            if interval < mon.min_interval:
                log.warning("'%s': intervalo %.2fs por debajo del suelo de seguridad (%.2fs); se usa el suelo.",
                            t.name, interval, mon.min_interval)
                interval = mon.min_interval
            cls = ProductMonitor if t.kind == "url" else SearchMonitor
            out.append(cls(t, interval, self.adapter, self.client, self.detector, self.notifier,
                           self.stats, mon, self.cfg.discord))
        return out

    def counts(self) -> tuple[int, int]:
        urls = sum(1 for m in self.monitors if m.target.kind == "url")
        return urls, len(self.monitors) - urls

    def estimated_rpm(self) -> float:
        return sum(60.0 / m.interval * (m.target.max_pages if m.target.kind != "url" else 1) for m in self.monitors)

    def log_banner(self) -> None:
        urls, searches = self.counts()
        log.info("Monitor iniciado")
        log.info("Productos URL: %d | Búsquedas/listados: %d | Proxies: %d | Intervalo: %gs | Concurrencia: %d",
                 urls, searches, len(self.proxies), self.cfg.monitor.interval, self.cfg.monitor.concurrency)
        rpm, cap = self.estimated_rpm(), self.cfg.monitor.max_requests_per_minute
        log.info("Carga estimada: ~%.0f peticiones/min (límite global: %d/min)", rpm, cap)
        if cap and rpm > cap:
            log.warning("La carga estimada supera el límite global: las comprobaciones se espaciarán automáticamente. "
                        "Sube los intervalos, añade proxies o aumenta monitor.max_requests_per_minute bajo tu responsabilidad.")
        if not self.monitors:
            log.warning("No hay productos ni búsquedas configurados (revisa config.yaml).")

    # ------------------------------------------------------------------
    async def _health_loop(self) -> None:
        every = self.cfg.health.summary_interval
        while True:
            await asyncio.sleep(every)
            ps = self.proxies.stats() if self.proxies.enabled else None
            text = self.stats.summary(ps)
            log.info("[HEALTH] %s", text)
            if self.cfg.health.to_discord:
                self.notifier.enqueue(dc.build_text_payload(self.cfg.discord, "📊 Estado del monitor", text), "HEALTH")

    def request_stop(self) -> None:
        if not self._stopping:
            self._stopping = True
            log.info("Parada solicitada, cerrando de forma ordenada...")
            self.scheduler.stop_event.set()

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError, ValueError):
                pass   # Windows: Ctrl+C llega como KeyboardInterrupt / cancelación y lo gestiona el finally

    async def run(self) -> None:
        try:
            await self.notifier.start()
            self.log_banner()
            if self.cfg.discord.send_startup and self.notifier.enabled:
                urls, searches = self.counts()
                self.notifier.enqueue(dc.build_startup_payload(
                    self.cfg.discord, url_products=urls, searches=searches, proxies=len(self.proxies),
                    interval=self.cfg.monitor.interval, concurrency=self.cfg.monitor.concurrency), "STARTUP")
            self._install_signals()
            self.scheduler.start()
            if self.cfg.health.summary_interval > 0:
                self._health_task = asyncio.create_task(self._health_loop(), name="health")
            await self.scheduler.stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def run_once(self) -> list:
        """Una sola pasada por todos los objetivos (útil para probar la configuración)."""
        try:
            await self.notifier.start()
            self.log_banner()
            return await self.scheduler.run_all_once()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.scheduler.stop_event.set()
        if self._health_task:
            self._health_task.cancel()
            await asyncio.gather(self._health_task, return_exceptions=True)
        try:
            await self.scheduler.stop()
        except BaseException as exc:   # noqa: BLE001 - seguimos cerrando pase lo que pase
            log.warning("Error al detener tareas: %s", type(exc).__name__)
        for step in (self.notifier.stop, self.client.aclose):
            try:
                await step()
            except BaseException as exc:   # noqa: BLE001
                log.warning("Error al cerrar %s: %s", getattr(step, "__qualname__", step), type(exc).__name__)
        try:
            self.store.close()
        except Exception as exc:
            log.warning("Error al cerrar la base de datos: %s", exc)
        log.info("Resumen final: %s", self.stats.summary(self.proxies.stats() if self.proxies.enabled else None))
        print("Monitor detenido correctamente.")
        sys.stdout.flush()


def configure_logging(cfg: AppConfig) -> None:
    lc = cfg.logging
    setup_logging(lc.level, str(cfg.resolve(lc.file)) if lc.file else None, lc.max_bytes, lc.backup_count, lc.console)
