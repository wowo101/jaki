import tempfile
import unittest
from pathlib import Path

from engine.provisioner.install import deploy_hatch_shim


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


if __name__ == "__main__":
    unittest.main()
