"""Invariants over the repo's real provision.toml files, not fixtures.

These catch a manifest that parses and deploys and still cannot work. The
executable-bit one is not hypothetical: `qmd-guard.sh`, `owui-bringup.sh` and
`pull-image-resumable.sh` were all mode 644 in the repo and were deployed for
months by `install -Dm755`, which adds the bit. A symlink cannot — it inherits the
target's mode — so moving those three to the symlink strategy shipped three shims
that PATH would not run, and `~/.local/bin/qmd` was the recall substrate's guard.
"""
import os
import unittest
from pathlib import Path

from engine.provisioner.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[3]


def _manifests():
    for pt in sorted(ROOT.glob("engine/*/*/provision.toml")):
        yield pt.parent, load_manifest(pt.parent)


class TestRepoManifests(unittest.TestCase):
    def test_every_manifest_parses(self):
        found = list(_manifests())
        self.assertTrue(found, "no provision.toml found — the glob is wrong")

    def test_every_deployed_executable_has_its_exec_bit(self):
        """A symlink inherits the target's mode, so the repo's bit is the deploy's."""
        bad = []
        for d, m in _manifests():
            sources = [e.source for e in m.bin]
            if m.kind == "verb" and m.exec_name:
                sources.append(m.exec_name)
            for rel in sources:
                f = d / rel
                if f.is_file() and not os.access(f, os.X_OK):
                    bad.append(str(f.relative_to(ROOT)))
        self.assertEqual(bad, [], f"not executable in the repo, so not runnable once symlinked: {bad}")

    def test_every_declared_source_exists(self):
        missing = []
        for d, m in _manifests():
            rels = [e.source for e in m.bin] + list(m.units) + [e.source for e in m.files]
            if m.kind == "verb" and m.exec_name:
                rels.append(m.exec_name)
            if m.kind == "plugin":
                rels = []          # outputs are build products, absent before a build
            missing += [str((d / r).relative_to(ROOT)) for r in rels if not (d / r).is_file()]
        self.assertEqual(missing, [], f"declared but not in the repo: {missing}")

    def test_no_two_artefacts_claim_one_file_destination(self):
        seen = {}
        for d, m in _manifests():
            for e in m.files:
                prior = seen.get(e.dest)
                self.assertIsNone(prior, f"{e.dest} claimed by both {prior} and {d.name}")
                seen[e.dest] = d.name


if __name__ == "__main__":
    unittest.main()
