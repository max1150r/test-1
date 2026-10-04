import asyncio
import unittest

from monitor.models import Event, EventKind, ProductInfo, Status
from notifications import discord as dc
from notifications.discord import DiscordNotifier
from utils.config import DiscordCfg
from utils.logger import redact


def ev(kind, **kw):
    info = ProductInfo(store="carrefour", url="https://www.carrefour.es/x/VC4A-1/p", status=Status.IN_STOCK,
                       sku="VC4A-1", name="Pokémon Caja Elite", price=54.99,
                       image="https://static.carrefour.es/img.jpg", seller="Carrefour")
    return Event(kind=kind, info=info, target_name="Caja", source=kw.pop("source", "url"), **kw)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.cfg = DiscordCfg(webhook_url="https://discord.com/api/webhooks/1/abc", mention_on_stock="")

    def fields(self, payload):
        return {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}

    def test_stock_payload(self):
        p = dc.build_stock_payload(ev(EventKind.STOCK_AVAILABLE), self.cfg)
        e = p["embeds"][0]
        self.assertEqual(e["title"], "🟢 STOCK DISPONIBLE")
        f = self.fields(p)
        for k in ("Producto", "Precio", "Estado", "Hora", "Fuente", "Comprar"):
            self.assertIn(k, f)
        self.assertEqual(f["Precio"], "54,99 €")
        self.assertIn("COMPRAR", f["Comprar"])
        self.assertIn("https://www.carrefour.es/x/VC4A-1/p", f["Comprar"])
        self.assertEqual(e["image"]["url"], "https://static.carrefour.es/img.jpg")
        self.assertEqual(e["url"], "https://www.carrefour.es/x/VC4A-1/p")
        self.assertEqual(p["allowed_mentions"], {"parse": []})

    def test_mention_enables_allowed_mentions(self):
        cfg = DiscordCfg(webhook_url=self.cfg.webhook_url, mention_on_stock="@everyone")
        p = dc.build_stock_payload(ev(EventKind.STOCK_AVAILABLE), cfg)
        self.assertEqual(p["content"], "@everyone")
        self.assertIn("everyone", p["allowed_mentions"]["parse"])

    def test_new_product_payload(self):
        p = dc.build_new_product_payload(ev(EventKind.NEW_PRODUCT, source="search", query="pokemon 30 aniversario"), self.cfg)
        self.assertEqual(p["embeds"][0]["title"], "🔔 NUEVO PRODUCTO DETECTADO")
        f = self.fields(p)
        self.assertEqual(f["Palabra clave"], "`pokemon 30 aniversario`")
        for k in ("Nombre", "Precio", "URL", "Hora"):
            self.assertIn(k, f)

    def test_price_payload_math(self):
        p = dc.build_price_payload(ev(EventKind.PRICE_CHANGE, previous_price=50.0, new_price=40.0), self.cfg)
        f = self.fields(p)
        self.assertEqual(p["embeds"][0]["title"], "💰 CAMBIO DE PRECIO")
        self.assertEqual(f["Precio anterior"], "50,00 €")
        self.assertEqual(f["Precio nuevo"], "40,00 €")
        self.assertEqual(f["Diferencia"], "-10,00 €")
        self.assertEqual(f["Cambio"], "-20.0 %")

    def test_error_and_startup_payloads(self):
        p = dc.build_error_payload("Bloqueo", "HTTP 429", self.cfg, "Producto X")
        self.assertEqual(p["embeds"][0]["title"], "⚠️ MONITOR ERROR")
        s = dc.build_startup_payload(self.cfg, url_products=2, searches=3, proxies=4, interval=10, concurrency=5)
        self.assertEqual(s["embeds"][0]["title"], "🚀 Carrefour Monitor iniciado")
        self.assertEqual(self.fields(s)["Intervalo"], "10 segundos")

    def test_discord_limits_respected(self):
        e = ev(EventKind.STOCK_AVAILABLE)
        e.info.name = "A" * 5000
        p = dc.build_stock_payload(e, self.cfg)
        emb = p["embeds"][0]
        self.assertLessEqual(len(emb["title"]), 256)
        self.assertTrue(all(len(f["value"]) <= 1024 and len(f["name"]) <= 256 for f in emb["fields"]))
        self.assertLessEqual(len(emb["fields"]), 25)

    def test_event_gating_by_config(self):
        cfg = DiscordCfg(notify_price_changes=False, notify_out_of_stock=False)
        self.assertIsNone(dc.build_event_payload(ev(EventKind.PRICE_CHANGE, previous_price=1, new_price=2), cfg))
        self.assertIsNone(dc.build_event_payload(ev(EventKind.STOCK_LOST), cfg))
        cfg = DiscordCfg(notify_price_changes=True, notify_out_of_stock=True)
        self.assertIsNotNone(dc.build_event_payload(ev(EventKind.PRICE_CHANGE, previous_price=1, new_price=2), cfg))
        self.assertIsNotNone(dc.build_event_payload(ev(EventKind.STOCK_LOST), cfg))

    def test_webhook_is_never_in_payload_and_is_redacted_in_logs(self):
        import json
        p = dc.build_stock_payload(ev(EventKind.STOCK_AVAILABLE), self.cfg)
        self.assertNotIn("webhooks", json.dumps(p))
        self.assertNotIn("abc", redact("POST https://discord.com/api/webhooks/123456/abcDEF-xyz_1 failed"))
        self.assertEqual(redact("proxy http://user:pa55@1.2.3.4:80"), "proxy http://***:***@1.2.3.4:80")


class ErrorDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_alert_cooldown(self):
        n = DiscordNotifier(DiscordCfg(webhook_url="https://discord.com/api/webhooks/1/abc", error_cooldown=900))
        n._queue = asyncio.Queue()
        self.assertTrue(n.notify_error("k", "Errores repetidos", "x"))
        for _ in range(5):
            self.assertFalse(n.notify_error("k", "Errores repetidos", "x"))
        self.assertTrue(n.notify_error("otra", "Errores repetidos", "x"))
        self.assertEqual(n._queue.qsize(), 2)

    async def test_error_alert_resumes_after_cooldown_with_suppressed_count(self):
        n = DiscordNotifier(DiscordCfg(webhook_url="https://discord.com/api/webhooks/1/abc", error_cooldown=0.05))
        n._queue = asyncio.Queue()
        n.notify_error("k", "t", "d")
        n.notify_error("k", "t", "d")
        await asyncio.sleep(0.06)
        self.assertTrue(n.notify_error("k", "t", "d"))
        payload, _ = n._queue.get_nowait(), None
        last = list(n._queue._queue)[-1][0]
        self.assertIn("suprimidos", last["embeds"][0]["description"])

    async def test_disabled_without_webhook(self):
        n = DiscordNotifier(DiscordCfg(webhook_url=""))
        self.assertFalse(n.enabled)
        n.enqueue({"x": 1})      # no debe fallar
        self.assertFalse(await n.send_now({"x": 1}))


if __name__ == "__main__":
    unittest.main()
