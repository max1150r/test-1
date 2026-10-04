"""Planificador: un ciclo independiente por objetivo, con jitter, backoff exponencial y concurrencia limitada."""
from __future__ import annotations

import asyncio
import random

from monitor.base import BaseMonitor
from monitor.models import CheckResult
from utils.config import MonitorCfg
from utils.logger import log


def backoff_delay(level: int, base: float, cap: float) -> float:
    """5, 10, 20, 40... hasta `cap`. level 0 => sin backoff."""
    if level <= 0:
        return 0.0
    return min(cap, base * (2 ** (level - 1)))


def next_delay(interval: float, jitter: float, level: int, base: float, cap: float,
               rng: random.Random | None = None) -> float:
    rng = rng or random
    return max(interval, backoff_delay(level, base, cap)) + (rng.uniform(0, jitter) if jitter > 0 else 0.0)


class TargetLoop:
    def __init__(self, monitor: BaseMonitor, cfg: MonitorCfg, semaphore: asyncio.Semaphore, stop: asyncio.Event):
        self.monitor = monitor
        self.cfg = cfg
        self.sem = semaphore
        self.stop = stop
        self.level = 0
        self.max_level = 12
        self.hard_timeout = cfg.timeout * (cfg.retries + 2) + 120 + cfg.max_enrich_per_cycle * cfg.timeout * (cfg.retries + 1)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def run_once(self) -> CheckResult:
        async with self.sem:
            if self.stop.is_set():
                return CheckResult(ok=True)
            try:
                return await asyncio.wait_for(self.monitor.check(), timeout=self.hard_timeout)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                log.error("%s: la comprobación superó el tiempo máximo", self.monitor.name)
                return CheckResult(ok=False, error="timeout global")
            except Exception as exc:   # un objetivo nunca debe tumbar al resto
                log.exception("Error inesperado en %s: %s", self.monitor.name, exc)
                return CheckResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    async def run(self) -> None:
        await self._sleep(random.uniform(0, min(self.monitor.interval, 3.0)))   # desincroniza el arranque
        while not self.stop.is_set():
            result = await self.run_once()
            if result.ok:
                self.level = max(0, self.level - 1)       # recuperación progresiva
            else:
                self.level = min(self.max_level, self.level + 1)
            delay = next_delay(self.monitor.interval, self.cfg.jitter, self.level,
                               self.cfg.backoff_base, self.cfg.backoff_max)
            if self.level:
                log.warning("%s: backoff nivel %d, próxima comprobación en %.0fs", self.monitor.name, self.level, delay)
            await self._sleep(delay)


class Scheduler:
    def __init__(self, monitors: list[BaseMonitor], cfg: MonitorCfg):
        self.stop_event = asyncio.Event()
        self.sem = asyncio.Semaphore(cfg.concurrency)
        self.loops = [TargetLoop(m, cfg, self.sem, self.stop_event) for m in monitors]
        self.cfg = cfg
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [asyncio.create_task(l.run(), name=f"loop-{l.monitor.name}") for l in self.loops]

    async def run_all_once(self) -> list[CheckResult]:
        return list(await asyncio.gather(*(l.run_once() for l in self.loops)))

    async def stop(self, grace: float = 30.0) -> None:
        self.stop_event.set()
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=grace)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks = []
