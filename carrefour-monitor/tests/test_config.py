import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils.config import ConfigError, load_config


def write(tmp, text, name="config.yaml"):
    p = Path(tmp) / name
    p.write_text(text, encoding="utf-8")
    return p


class ConfigTests(unittest.TestCase):
    def test_full_example_loads(self):
        root = Path(__file__).resolve().parent.parent
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc"}):
            cfg = load_config(root / "config.example.yaml")
        self.assertEqual(cfg.discord.webhook_url, "https://discord.com/api/webhooks/1/abc")
        kinds = [t.kind for t in cfg.targets]
        self.assertIn("url", kinds)
        self.assertIn("search", kinds)
        self.assertIn("listing", kinds)
        self.assertEqual(cfg.targets[0].interval, 5)

    def test_defaults_and_env_fallback(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/2/xyz"}):
            cfg = load_config(write(d, "products: []\n"))
        self.assertEqual(cfg.discord.webhook_url, "https://discord.com/api/webhooks/2/xyz")
        self.assertEqual((cfg.monitor.interval, cfg.monitor.concurrency), (10.0, 5))

    def test_conceptual_example_from_spec_is_accepted(self):
        yml = """
monitor: {interval: 10, timeout: 15, retries: 3, concurrency: 5, jitter: 2}
products:
  - {name: "Producto 1", type: "url", value: "https://www.carrefour.es/x/VC4A-1/p"}
  - {name: "Pokemon 30 aniversario", type: "search", value: "pokemon 30 aniversario"}
searches:
  - query: "pokemon"
    include_keywords: ["30 aniversario", "elite trainer"]
    exclude_keywords: ["álbum", "carpeta"]
    max_price: 100
"""
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(write(d, yml))
        self.assertEqual([t.kind for t in cfg.targets], ["url", "search", "search"])
        self.assertEqual(cfg.targets[2].max_price, 100.0)

    def test_errors_are_readable(self):
        bad = {
            "monitor: {interval: 0}": "interval",
            "monitor: {concurrencia: 3}": "desconocidas",
            "products: [{type: url, value: 'no-es-url'}]": "URL",
            "products: [{type: wat, value: 'x'}]": "type",
            "discord: {webhook_url: 'https://evil.example/hook'}": "webhook",
            "store: {search_url: 'https://x.es/?q='}": "{query}",
            "searches: [{query: 'a', include_mode: 'xx'}]": "include_mode",
            "foo: 1": "Secciones desconocidas",
        }
        for text, needle in bad.items():
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ConfigError, msg=text) as cm:
                    load_config(write(d, text))
                self.assertIn(needle, str(cm.exception), msg=text)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config("/no/existe/config.yaml")

    def test_duplicates_and_disabled_targets_removed(self):
        yml = """
products:
  - {name: a, type: url, value: "https://www.carrefour.es/x/VC4A-1/p"}
  - {name: b, type: url, value: "https://www.carrefour.es/x/VC4A-1/p"}
  - {name: c, type: url, value: "https://www.carrefour.es/x/VC4A-2/p", enabled: false}
"""
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(write(d, yml))
        self.assertEqual(len(cfg.targets), 1)

    def test_secret_not_required_in_yaml(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_WEBHOOK_URL", None)
            cfg = load_config(write(d, "products: []\n"), env_file=Path(d) / "nope.env")
        self.assertEqual(cfg.discord.webhook_url, "")


if __name__ == "__main__":
    unittest.main()
