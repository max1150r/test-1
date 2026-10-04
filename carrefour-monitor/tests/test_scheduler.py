import asyncio
import random
import unittest

from monitor.models import CheckResult
from monitor.scheduler import Scheduler, TargetLoop, backoff_delay, next_delay
from utils.config import MonitorCfg, TargetCfg


class FakeMonitor:
    def __init__(self, name, interval=0.01, behavior=None):
        self.name, self.interval, self.calls = name, interval, 0
        self.behavior = behavior or (lambda n: CheckResult(ok=True))
        self.target = TargetCfg(name=name, kind="url", value="https://www.carrefour.es/x/VC4A-1/p")

    async def check(self):
        self.calls += 1
        r = self.behavior(self.calls)
        if isinstance(r, Exception):
            raise r
        return r


class BackoffTests(unittest.TestCase):
    def test_exponential_sequence_with_cap(self):
        self.assertEqual([backoff_delay(l, 5, 40) for l in range(0, 6)], [0, 5, 10, 20, 40, 40])

    def test_next_delay_respects_interval_and_jitter(self):
        rng = random.Random(1)
        d = next_delay(10, 2, 0, 5, 300, rng)
        self.assertTrue(10 <= d <= 12)
        self.assertEqual(next_delay(10, 0, 3, 5, 300), 20)       # backoff supera al intervalo
        self.assertEqual(next_delay(30, 0, 1, 5, 300), 30)       # intervalo supera al backoff


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_failing_target_never_kills_others_and_recovers(self):
        def flaky(n):
            if n == 1:
                return RuntimeError("boom")
            if n == 2:
                return CheckResult(ok=False, error="x")
            return CheckResult(ok=True)

        bad = FakeMonitor("bad", behavior=flaky)
        good = FakeMonitor("good")
        cfg = MonitorCfg(jitter=0, backoff_base=0.01, backoff_max=0.02, concurrency=2, timeout=1, retries=0)
        sch = Scheduler([bad, good], cfg)
        sch.start()
        await asyncio.sleep(0.4)
        await sch.stop(grace=2)
        self.assertGreater(bad.calls, 3)           # sigue vivo tras excepción y fallo
        self.assertGreater(good.calls, 3)
        lp = sch.loops[0]
        self.assertEqual(lp.level, 0)              # recuperación progresiva hasta el nivel 0

    async def test_concurrency_limit(self):
        running = peak = 0

        class Slow(FakeMonitor):
            async def check(self_inner):
                nonlocal running, peak
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.05)
                running -= 1
                return CheckResult(ok=True)

        mons = [Slow(f"m{i}", interval=0.01) for i in range(8)]
        sch = Scheduler(mons, MonitorCfg(jitter=0, concurrency=2, timeout=1, retries=0))
        sch.start()
        await asyncio.sleep(0.3)
        await sch.stop(grace=2)
        self.assertEqual(peak, 2)

    async def test_stop_is_prompt(self):
        sch = Scheduler([FakeMonitor("a", interval=60)], MonitorCfg(jitter=0, timeout=1, retries=0))
        sch.start()
        await asyncio.sleep(0.05)
        t0 = asyncio.get_running_loop().time()
        await sch.stop(grace=5)
        self.assertLess(asyncio.get_running_loop().time() - t0, 1.0)


if __name__ == "__main__":
    unittest.main()
