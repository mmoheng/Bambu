import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bambu_companion.bridge import printer_setup
from bambu_companion.bridge.printer_config import load_printer_config


class TestValidate(unittest.TestCase):
    def test_good_values(self):
        self.assertEqual(printer_setup.validate("192.168.1.50", "01P00A123456789", "12345678"), [])

    def test_each_problem_is_reported(self):
        self.assertEqual(len(printer_setup.validate("", "", "")), 3)
        self.assertEqual(len(printer_setup.validate("http://192.168.1.50/", "01P00A", "1234")), 1)
        self.assertEqual(len(printer_setup.validate("192.168.1.50", "has space", "1234")), 1)


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"APPDATA": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, answers, code, argv=()):
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=answers), mock.patch(
            "getpass.getpass", return_value=code
        ), mock.patch("sys.stdout", out):
            status = printer_setup.main(list(argv))
        return status, out.getvalue()

    def test_saves_locally_and_never_prints_the_access_code(self):
        status, output = self.run_main(["192.168.1.50", "01P00A123456789"], "SECRETCODE")
        self.assertEqual(status, 0)
        self.assertNotIn("SECRETCODE", output)
        cfg = load_printer_config()
        self.assertEqual(
            (cfg["host"], cfg["serial"], cfg["access_code"]), ("192.168.1.50", "01P00A123456789", "SECRETCODE")
        )
        self.assertTrue((Path(self.tmp.name) / "BambuCompanion" / "printer_config.json").exists())

    def test_blank_answers_keep_saved_values(self):
        self.run_main(["192.168.1.50", "01P00A123456789"], "SECRETCODE")
        status, _ = self.run_main(["192.168.1.77", ""], "")
        self.assertEqual(status, 0)
        cfg = load_printer_config()
        self.assertEqual((cfg["host"], cfg["serial"], cfg["access_code"]), ("192.168.1.77", "01P00A123456789", "SECRETCODE"))

    def test_invalid_input_saves_nothing(self):
        status, output = self.run_main(["", ""], "")
        self.assertEqual(status, 1)
        self.assertIn("Not saved", output)
        self.assertEqual(load_printer_config()["host"], "")

    def test_show_hides_serial_and_code(self):
        self.run_main(["192.168.1.50", "01P00A123456789"], "SECRETCODE")
        status, output = self.run_main([], "", argv=["--show"])
        self.assertEqual(status, 0)
        self.assertIn("192.168.1.50", output)
        self.assertNotIn("01P00A123456789", output)
        self.assertNotIn("SECRETCODE", output)


if __name__ == "__main__":
    unittest.main()
