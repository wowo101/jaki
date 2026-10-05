import tempfile
import unittest
from pathlib import Path

from engine.provisioner.platform import PlatformInfo
from engine.provisioner.config import MachineConfig
from engine.provisioner.resolve import resolve
from engine.provisioner.emit import (read_resolved_env, resolved_env_path,
                                     write_resolved_env)


class TestEmit(unittest.TestCase):
    def test_emit(self):
        plat = PlatformInfo("linux", "xdg-open", ["/usr/bin/git"], ["/usr/local/bin"],
                            {"devonthink": False, "macwhisper": False})
        r = resolve(MachineConfig("full", "", {}, {"vault": "/tmp/v"}, {}), plat)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / ".hatch" / "resolved.env"
            write_resolved_env(r, out)
            self.assertTrue(out.parent.is_dir())   # emit created the parent dir
            text = out.read_text()
        self.assertIn("OPENER=xdg-open", text)
        self.assertIn("VAULT=/tmp/v", text)
        self.assertIn("HATCH_HAS_DEVONTHINK=0", text)
        self.assertTrue(text.endswith("\n"))


class TestReadResolvedEnv(unittest.TestCase):
    """`doctor` branches on None vs {}, so the distinction is load-bearing:
    an existing-but-empty file returning None would silently disable every
    `needs` check with nothing going red."""

    def test_absent_file_is_none_not_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(read_resolved_env(Path(d) / "nope.env"))

    def test_file_with_only_a_header_parses_to_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "r.env"
            out.write_text("# header only\n\n")
            self.assertEqual(read_resolved_env(out), {})

    def test_round_trips_what_write_emitted(self):
        plat = PlatformInfo("linux", "xdg-open", ["/usr/bin/git"], ["/usr/local/bin"],
                            {"devonthink": False, "macwhisper": False})
        r = resolve(MachineConfig("full", "", {}, {"vault": "/tmp/v"}, {}), plat)
        with tempfile.TemporaryDirectory() as d:
            out = resolved_env_path(Path(d))
            write_resolved_env(r, out)
            self.assertEqual(read_resolved_env(out), r.env)

    def test_empty_value_is_kept_as_empty_string(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "r.env"
            out.write_text("OLLAMA_URL=\nVAULT=/v\n")
            env = read_resolved_env(out)
        self.assertEqual(env["OLLAMA_URL"], "")     # present, not absent
        self.assertIn("OLLAMA_URL", env)

    def test_blank_and_comment_and_valueless_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "r.env"
            out.write_text("# c\n\n   \nNOEQUALS\nVAULT=/v\n")
            self.assertEqual(read_resolved_env(out), {"VAULT": "/v"})

    def test_a_value_containing_equals_is_kept_whole(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "r.env"
            out.write_text("PAPERLESS_URL=http://h/?a=1&b=2\n")
            self.assertEqual(read_resolved_env(out)["PAPERLESS_URL"], "http://h/?a=1&b=2")


if __name__ == "__main__":
    unittest.main()
