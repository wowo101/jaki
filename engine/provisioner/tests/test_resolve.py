import unittest

from engine.provisioner.platform import PlatformInfo
from engine.provisioner.config import MachineConfig
from engine.provisioner.resolve import resolve


def _plat():
    return PlatformInfo(
        os="linux", opener="xdg-open",
        git_candidates=["/usr/bin/git"], path_dirs=["/usr/local/bin"],
        capabilities={"devonthink": False, "macwhisper": False},
    )


class TestResolve(unittest.TestCase):
    def test_override_wins(self):
        cfg = MachineConfig("full", "", {"opener": "custom-open"},
                            {"vault": "~/Documents/Notes", "pi_host": "gpu-box"}, {})
        r = resolve(cfg, _plat())
        self.assertEqual(r.env["OPENER"], "custom-open")          # override beats platform default
        self.assertEqual(r.env["PI_HOST"], "gpu-box")
        self.assertEqual(r.env["HATCH_HAS_DEVONTHINK"], "0")
        self.assertTrue(r.env["VAULT"].endswith("/Notes"))         # expanduser applied
        self.assertNotIn("~", r.env["VAULT"])

    def test_platform_default_when_no_override(self):
        cfg = MachineConfig("surface", "", {}, {}, {})
        r = resolve(cfg, _plat())
        self.assertEqual(r.env["OPENER"], "xdg-open")
        self.assertEqual(r.env["PATH_PREPEND"], "/usr/local/bin")
        self.assertEqual(r.env["PI_HOST"], "local")               # default
        self.assertEqual(r.role, "surface")


if __name__ == "__main__":
    unittest.main()
