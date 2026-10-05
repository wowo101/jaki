import unittest
from unittest import mock

from engine.provisioner import platform as plat


class TestPlatform(unittest.TestCase):
    def test_darwin(self):
        with mock.patch.object(plat._sys, "system", return_value="Darwin"), \
             mock.patch.object(plat.Path, "exists", return_value=False):  # no DT/MW apps
            info = plat.detect()
        self.assertEqual(info.os, "darwin")
        self.assertEqual(info.opener, "open")
        self.assertIn("/opt/homebrew/bin", info.path_dirs)
        self.assertEqual(info.capabilities, {"devonthink": False, "macwhisper": False})

    def test_linux(self):
        with mock.patch.object(plat._sys, "system", return_value="Linux"):
            info = plat.detect()
        self.assertEqual(info.os, "linux")
        self.assertEqual(info.opener, "xdg-open")
        self.assertFalse(info.capabilities["devonthink"])


if __name__ == "__main__":
    unittest.main()
