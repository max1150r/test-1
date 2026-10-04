import io
import logging
import tempfile
import unittest
from pathlib import Path

from utils import logger as lg


class LoggerTests(unittest.TestCase):
    def test_custom_levels_and_redaction_in_file_with_rotation_handler(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "logs" / "monitor.log"
            lg.setup_logging("INFO", str(f), max_bytes=1000, backup_count=2, console=False)
            lg.register_secret("SuperSecretToken123")
            lg.log.check("Pokemon ETB")
            lg.log.oos("Pokemon ETB")
            lg.log.stock("Producto XYZ - 54,99 €")
            lg.log.proxy("Proxy cambiado")
            lg.log.error("falló https://discord.com/api/webhooks/123/AbCdEf-ghi y http://u:pw@1.2.3.4:80 SuperSecretToken123")
            for h in logging.getLogger("carrefour_monitor").handlers:
                h.flush()
            text = f.read_text(encoding="utf-8")
            for lvl in ("[CHECK]", "[OOS]", "[STOCK]", "[PROXY]", "[ERROR]"):
                self.assertIn(lvl, text)
            self.assertNotIn("AbCdEf", text)
            self.assertNotIn("pw@", text)
            self.assertNotIn("SuperSecretToken123", text)
            handlers = logging.getLogger("carrefour_monitor").handlers
            self.assertTrue(any(type(h).__name__ == "RotatingFileHandler" for h in handlers))
            for h in handlers:
                h.close()
            logging.getLogger("carrefour_monitor").handlers.clear()


if __name__ == "__main__":
    unittest.main()
