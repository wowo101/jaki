import tempfile
import unittest
from pathlib import Path

from engine.provisioner import config as cfg


class TestConfig(unittest.TestCase):
    def test_normalise_hostname(self):
        self.assertEqual(cfg.normalise_hostname("Alices-MacBook-Air.local"), "alices-macbook-air")
        self.assertEqual(cfg.normalise_hostname("thinkbook"), "thinkbook")

    def test_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = cfg.machine_config_path(Path(d), "Alices-MacBook-Air.local")
            self.assertEqual(p, Path(d) / "machines" / "alices-macbook-air.toml")

    def test_load(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "m.toml"
            f.write_text('role = "full"\ndescription = "x"\n[endpoints]\nvault = "~/Documents/Notes"\n[artefacts]\nverbs = ["recall"]\n')
            m = cfg.load_machine_config(f)
            self.assertEqual(m.role, "full")
            self.assertEqual(m.endpoints["vault"], "~/Documents/Notes")
            self.assertEqual(m.artefacts["verbs"], ["recall"])

    def test_bad_role(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "m.toml"
            f.write_text('role = "wrong"\n')
            with self.assertRaises(ValueError):
                cfg.load_machine_config(f)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                cfg.load_machine_config(Path(d) / "nope.toml")


if __name__ == "__main__":
    unittest.main()
