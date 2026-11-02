import tempfile
import unittest
from pathlib import Path

import json
from unittest import mock

from engine.provisioner import install as install_mod
from engine.provisioner.install import deploy_hatch_shim
from engine.provisioner.tests.test_doctor import LINUX


class TestDeployHatchShim(unittest.TestCase):
    def _root(self, d):
        root = Path(d)
        (root / "hatch").write_text("#!/usr/bin/env python3\n")
        return root

    def test_symlinks_the_shim(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            res = deploy_hatch_shim(root, root / "bin")
            self.assertEqual(res.status, "deployed")
            link = root / "bin" / "hatch"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (root / "hatch").resolve())

    def test_refuses_real_file_unless_adopt(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            bin_dir = root / "bin"; bin_dir.mkdir()
            (bin_dir / "hatch").write_text("hand-placed\n")
            res = deploy_hatch_shim(root, bin_dir)
            self.assertEqual(res.status, "error")
            self.assertIn("refusing to overwrite", res.detail)
            res = deploy_hatch_shim(root, bin_dir, adopt=True)
            self.assertEqual(res.status, "deployed")
            self.assertTrue((bin_dir / "hatch").is_symlink())

    def test_idempotent_second_run(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._root(d)
            deploy_hatch_shim(root, root / "bin")
            res = deploy_hatch_shim(root, root / "bin")
            self.assertEqual(res.status, "deployed")


class TestInstallEnablesPlugins(unittest.TestCase):
    """`install` end to end for one plugin, with every home-directory target redirected."""

    def _install(self, d, extra_vault=False):
        home = Path(d)
        root, vault, extra = home / "repo", home / "Notes", home / "Other"
        for p in (root / "machines", vault, extra):
            p.mkdir(parents=True)
        (root / "hatch").write_text("#!/usr/bin/env python3\n")
        art = root / "engine" / "plugins" / "pg"
        art.mkdir(parents=True)
        (art / "provision.toml").write_text('kind = "plugin"\nid = "pg"\noutputs = ["main.js"]\n')
        (art / "main.js").write_text("x")
        extras = f'extra_vaults = ["{extra}"]\n' if extra_vault else ""
        (root / "machines" / "host.toml").write_text(
            f'role = "full"\n[endpoints]\nvault = "{vault}"\n{extras}[artefacts]\nplugins = ["pg"]\n')
        with mock.patch.object(install_mod, "BIN_DIR", home / "bin"), \
             mock.patch.object(install_mod, "link_env_for_units", return_value=home / "env"), \
             mock.patch.object(install_mod.platform, "detect", return_value=LINUX):
            results = install_mod.install(root, "host")
        return results, vault, extra

    def test_plugin_is_enabled_in_the_canonical_vault(self):
        with tempfile.TemporaryDirectory() as d:
            results, vault, _ = self._install(d)
            enabled = json.loads((vault / ".obsidian" / "community-plugins.json").read_text())
        self.assertEqual(enabled, ["pg"])
        self.assertIn("pg:enable", [r.name for r in results])

    def test_extra_vault_is_linked_but_not_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            _, vault, extra = self._install(d, extra_vault=True)
            self.assertTrue((extra / ".obsidian" / "plugins" / "pg").is_symlink())
            self.assertFalse((extra / ".obsidian" / "community-plugins.json").exists())
            self.assertTrue((vault / ".obsidian" / "community-plugins.json").exists())


if __name__ == "__main__":
    unittest.main()
