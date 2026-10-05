import tempfile
import unittest
from pathlib import Path

from engine.provisioner.manifest import ArtefactManifest
from engine.provisioner.deploy import deploy_verb


class _R:  # minimal Resolved stand-in (deploy_verb only needs the manifest + paths)
    env = {}


def _one(results):
    """deploy_verb returns a list (a verb may carry units); these cases declare
    none, so exactly one result is the assertion, not an unwrapping convenience."""
    assert len(results) == 1, f"expected one result, got {results}"
    return results[0]


class TestDeployVerb(unittest.TestCase):
    def test_deploy_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = root / "recall"; art.mkdir()
            (art / "recall").write_text("#!/bin/sh\necho hi\n")
            m = ArtefactManifest("recall", "verb", exec_name="recall", requires=[])
            bin_dir = root / "bin"
            res = _one(deploy_verb(m, _R(), art, bin_dir))
            self.assertEqual(res.status, "deployed")
            link = bin_dir / "recall"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (art / "recall").resolve())

    def test_skips_when_requirement_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = root / "recall"; art.mkdir(); (art / "recall").write_text("x")
            m = ArtefactManifest("recall", "verb", exec_name="recall",
                                 requires=["definitely-not-a-binary-xyz"])
            res = _one(deploy_verb(m, _R(), art, root / "bin"))
            self.assertEqual(res.status, "skipped")
            self.assertIn("definitely-not-a-binary-xyz", res.detail)

    def test_idempotent_replaces_existing_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = root / "recall"; art.mkdir(); (art / "recall").write_text("x")
            m = ArtefactManifest("recall", "verb", exec_name="recall", requires=[])
            bin_dir = root / "bin"
            deploy_verb(m, _R(), art, bin_dir)
            res = _one(deploy_verb(m, _R(), art, bin_dir))   # second run must not raise
            self.assertEqual(res.status, "deployed")
            self.assertTrue((bin_dir / "recall").is_symlink())

    def test_refuses_to_overwrite_real_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = root / "recall"; art.mkdir(); (art / "recall").write_text("x")
            m = ArtefactManifest("recall", "verb", exec_name="recall", requires=[])
            bin_dir = root / "bin"; bin_dir.mkdir()
            (bin_dir / "recall").write_text("human-placed\n")
            res = _one(deploy_verb(m, _R(), art, bin_dir))
            self.assertEqual(res.status, "error")
            self.assertIn("refusing to overwrite", res.detail)
            self.assertEqual((bin_dir / "recall").read_text(), "human-placed\n")

    def test_adopt_replaces_real_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = root / "recall"; art.mkdir(); (art / "recall").write_text("x")
            m = ArtefactManifest("recall", "verb", exec_name="recall", requires=[])
            bin_dir = root / "bin"; bin_dir.mkdir()
            (bin_dir / "recall").write_text("cp-era install\n")
            res = _one(deploy_verb(m, _R(), art, bin_dir, adopt=True))
            self.assertEqual(res.status, "deployed")
            self.assertTrue((bin_dir / "recall").is_symlink())

    def test_links_units_a_verb_declares(self):
        """The capability this file's `deploy_verb` gained: a verb that runs on
        a timer declares units like an app does, and they are linked. Before,
        `deploy_verb` ignored `m.units` entirely and the declaration was inert."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = root / "sync"; art.mkdir()
            (art / "sync").write_text("#!/bin/sh\ntrue\n")
            (art / "sync.service").write_text("[Service]\nExecStart=/bin/true\n")
            (art / "sync.timer").write_text("[Timer]\nOnBootSec=1min\n")
            m = ArtefactManifest("sync", "verb", exec_name="sync", requires=[],
                                 units=["sync.service", "sync.timer"])
            bin_dir, sd = root / "bin", root / "systemd"
            results = deploy_verb(m, _R(), art, bin_dir, systemd_dir=sd,
                                  reload_units=False, systemctl_available=True)
            by_name = {r.name: r for r in results}
            self.assertEqual(by_name["sync"].status, "deployed")
            for unit in ("sync.service", "sync.timer"):
                self.assertEqual(by_name[f"sync:{unit}"].status, "deployed", results)
                self.assertTrue((sd / unit).is_symlink(), f"{unit} not linked")
                self.assertEqual((sd / unit).resolve(), (art / unit).resolve())

    def test_units_skip_without_systemctl(self):
        """A macOS machine skips the units and still gets the verb on PATH."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = root / "sync"; art.mkdir()
            (art / "sync").write_text("x"); (art / "sync.timer").write_text("y")
            m = ArtefactManifest("sync", "verb", exec_name="sync", requires=[],
                                 units=["sync.timer"])
            bin_dir, sd = root / "bin", root / "systemd"
            results = deploy_verb(m, _R(), art, bin_dir, systemd_dir=sd,
                                  reload_units=False, systemctl_available=False)
            by_name = {r.name: r for r in results}
            self.assertEqual(by_name["sync"].status, "deployed")
            self.assertEqual(by_name["sync:units"].status, "skipped")
            self.assertTrue((bin_dir / "sync").is_symlink())
            self.assertFalse(sd.exists(), "no systemd dir should be created")


if __name__ == "__main__":
    unittest.main()
