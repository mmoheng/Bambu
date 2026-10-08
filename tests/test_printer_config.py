import tempfile
import unittest
from pathlib import Path

from bambu_companion.bridge.printer_config import (
    load_printer_config,
    save_printer_config,
)


class TestPrinterConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "printer_config.json"

    def test_missing_file_returns_empty_defaults(self):
        cfg = load_printer_config(self.path)
        self.assertEqual(cfg, {"host": "", "serial": "", "access_code": ""})

    def test_roundtrip_save_then_load(self):
        save_printer_config("192.168.1.204", "03919D550404629", "da873f3e", self.path)
        cfg = load_printer_config(self.path)
        self.assertEqual(cfg["host"], "192.168.1.204")
        self.assertEqual(cfg["serial"], "03919D550404629")
        self.assertEqual(cfg["access_code"], "da873f3e")

    def test_corrupt_file_returns_defaults_not_a_crash(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        cfg = load_printer_config(self.path)
        self.assertEqual(cfg["host"], "")

    def test_save_creates_parent_directory(self):
        nested = Path(self.tmp.name) / "nested" / "printer_config.json"
        save_printer_config("10.0.0.5", "SER123", "code123", nested)
        self.assertTrue(nested.exists())
        cfg = load_printer_config(nested)
        self.assertEqual(cfg["host"], "10.0.0.5")


if __name__ == "__main__":
    unittest.main()
