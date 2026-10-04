import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

from tests.test_client import make_stub
from tests.helpers import URL_CAJA, FakeClient, fixture
from utils.config import load_config

# httpx se sustituye por un módulo falso: estos tests no usan red y no exigen httpx instalado.
_patch = mock.patch.dict(sys.modules, {"httpx": make_stub([])})


def setUpModule():
    _patch.start()


def tearDownModule():
    _patch.stop()


from app import Application  # noqa: E402


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_cfg(self, d, extra=""):
        yml = f"""
discord: {{webhook_url: ""}}
monitor: {{interval: 1, jitter: 0, timeout: 2, retries: 0, confirm_stock: false}}
logging: {{file: "", console: false}}
health: {{summary_interval: 0}}
state: {{db_path: "{Path(d) / 'state.db'}"}}
network: {{proxies_file: "{Path(d) / 'proxies.txt'}"}}
products:
  - {{name: Caja, type: url, value: "{URL_CAJA}"}}
{extra}
"""
        (Path(d) / "config.yaml").write_text(yml, encoding="utf-8")
        return load_config(Path(d) / "config.yaml", env_file=Path(d) / "x.env")

    async def test_run_once_and_clean_shutdown(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.make_cfg(d)
            app = Application(cfg)
            app.client = FakeClient({URL_CAJA: fixture("product_in_stock.html")})
            for m in app.monitors:
                m.client = app.client
            app.client.aclose = lambda: asyncio.sleep(0)
            res = await app.run_once()
            self.assertTrue(res[0].ok)
            self.assertTrue(app._closed)
            with self.assertRaises(RuntimeError):      # la BD quedó cerrada
                app.store._run(lambda: None)
            # el estado quedó guardado y es legible tras reabrir
            from monitor.state import StateStore
            s = StateStore(Path(d) / "state.db")
            self.assertEqual(s._get_product("carrefour:VC4A-34535246").name.startswith("Pokémon Caja"), True)
            s.close()

    async def test_continuous_run_stops_gracefully_on_request(self):
        with tempfile.TemporaryDirectory() as d:
            app = Application(self.make_cfg(d))
            app.client = FakeClient({URL_CAJA: fixture("product_in_stock.html")})
            for m in app.monitors:
                m.client = app.client
            app.client.aclose = lambda: asyncio.sleep(0)
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.3)
            app.request_stop()
            await asyncio.wait_for(task, timeout=5)
            self.assertTrue(app._closed)

    async def test_cancellation_still_closes_everything(self):
        with tempfile.TemporaryDirectory() as d:
            app = Application(self.make_cfg(d))
            app.client = FakeClient({URL_CAJA: fixture("product_in_stock.html")})
            for m in app.monitors:
                m.client = app.client
            app.client.aclose = lambda: asyncio.sleep(0)
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.2)
            task.cancel()                                  # lo que ocurre con Ctrl+C en Windows
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(app._closed)

    async def test_foreign_domain_url_is_rejected(self):
        from utils.config import ConfigError
        with tempfile.TemporaryDirectory() as d:
            cfg = self.make_cfg(d, extra='  - {name: Mala, type: url, value: "https://www.example.com/x/VC4A-1/p"}')
            with self.assertRaises(ConfigError):
                Application(cfg)

    async def test_interval_floor_and_rate_estimate(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.make_cfg(d, extra='  - {name: Rapido, type: url, value: "https://www.carrefour.es/x/VC4A-9/p", interval: 0.01}')
            app = Application(cfg)
            self.assertEqual(min(m.interval for m in app.monitors), cfg.monitor.min_interval)
            self.assertGreater(app.estimated_rpm(), 60)
            app.store.close()


if __name__ == "__main__":
    unittest.main()
