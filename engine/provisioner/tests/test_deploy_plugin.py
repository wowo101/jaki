import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from engine.provisioner.deploy import render_data, build_plugin, link_plugin
from engine.provisioner.manifest import ArtefactManifest


class _R:  # minimal Resolved stand-in
    env = {"GIT_BIN": "/opt/homebrew/bin/git", "PATH_PREPEND": "/opt/homebrew/bin",
           "VAULT": "/tmp/v", "HATCH_REPO": "/repo"}


class TestRenderData(unittest.TestCase):
    def test_substitutes_tokens(self):
        out = render_data(
            {"gitBinaryPath": "{git_bin}", "pathPrepend": "{path_prepend}", "vaultRootOverride": ""},
            _R())
        self.assertEqual(out["gitBinaryPath"], "/opt/homebrew/bin/git")
        self.assertEqual(out["pathPrepend"], "/opt/homebrew/bin")
        self.assertEqual(out["vaultRootOverride"], "")

    def test_substitutes_repo(self):
        out = render_data({"scriptPath": "{repo}/engine/tools/keep/keep"}, _R())
        self.assertEqual(out["scriptPath"], "/repo/engine/tools/keep/keep")

    def test_passes_non_strings_through(self):
        out = render_data({"enabled": True, "count": 3}, _R())
        self.assertEqual(out, {"enabled": True, "count": 3})


class TestBuildPlugin(unittest.TestCase):
    def test_runs_build_and_writes_data_once(self):
        with tempfile.TemporaryDirectory() as d:
            art = Path(d)
            m = ArtefactManifest("drafting-diff-plugin", "plugin", plugin_id="drafting-diff",
                                 build=[["true"]], data={"gitBinaryPath": "{git_bin}"})
            build_plugin(m, _R(), art)
            written = json.loads((art / "data.json").read_text())
            self.assertEqual(written["gitBinaryPath"], "/opt/homebrew/bin/git")

    def test_no_data_writes_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            art = Path(d)
            m = ArtefactManifest("p", "plugin", plugin_id="p", build=[], data={})
            build_plugin(m, _R(), art)
            self.assertFalse((art / "data.json").exists())

    def test_build_failure_propagates(self):
        with tempfile.TemporaryDirectory() as d:
            m = ArtefactManifest("p", "plugin", plugin_id="p", build=[["false"]])
            with self.assertRaises(subprocess.CalledProcessError):
                build_plugin(m, _R(), Path(d))


class TestLinkPlugin(unittest.TestCase):
    def _manifest(self):
        return ArtefactManifest("drafting-diff-plugin", "plugin", plugin_id="drafting-diff")

    def test_symlinks_into_vault(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = root / "art"; art.mkdir()
            vault = root / "vault"
            res = link_plugin(self._manifest(), art, vault)
            link = vault / ".obsidian" / "plugins" / "drafting-diff"
            self.assertEqual(res.status, "deployed")
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), art.resolve())

    def test_idempotent_replaces_existing_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = root / "art"; art.mkdir()
            vault = root / "vault"
            link_plugin(self._manifest(), art, vault)
            res = link_plugin(self._manifest(), art, vault)   # second run
            self.assertEqual(res.status, "deployed")

    def test_refuses_to_overwrite_a_real_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = root / "art"; art.mkdir()
            vault = root / "vault"
            real = vault / ".obsidian" / "plugins" / "drafting-diff"
            real.mkdir(parents=True)          # hand-installed copy, not a symlink
            res = link_plugin(self._manifest(), art, vault)
            self.assertEqual(res.status, "error")
            self.assertIn("not a symlink", res.detail)


if __name__ == "__main__":
    unittest.main()
