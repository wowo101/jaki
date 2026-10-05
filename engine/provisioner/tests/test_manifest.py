import shutil
import tempfile
import unittest
from pathlib import Path

from engine.provisioner.manifest import load_manifest


class TestManifest(unittest.TestCase):
    def test_verb(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "provision.toml").write_text(
                'kind = "verb"\nexec = "recall"\nrequires = ["osascript"]\nneeds = ["VAULT"]\n')
            m = load_manifest(Path(d))
            self.assertEqual(m.kind, "verb")
            self.assertEqual(m.exec_name, "recall")
            self.assertEqual(m.requires, ["osascript"])
            self.assertEqual(m.needs, ["VAULT"])

    def test_plugin(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "provision.toml").write_text(
                'kind = "plugin"\nid = "drafting-diff"\nbuild = [["npm", "run", "build"]]\n'
                'outputs = ["main.js"]\n[data]\ngitBinaryPath = "{git_bin}"\n')
            m = load_manifest(Path(d))
            self.assertEqual(m.plugin_id, "drafting-diff")
            self.assertEqual(m.build, [["npm", "run", "build"]])
            self.assertEqual(m.data["gitBinaryPath"], "{git_bin}")
            self.assertEqual(m.outputs, ["main.js"])

    def test_bad_kind(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "provision.toml").write_text('kind = "widget"\n')
            with self.assertRaises(ValueError):
                load_manifest(Path(d))

    def test_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                load_manifest(Path(d))


if __name__ == "__main__":
    unittest.main()


class TestProbe(unittest.TestCase):
    """`probe` is what lets doctor answer "can it run?" rather than "is it linked?"."""

    def _art(self, body: str):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / "provision.toml").write_text(body)
        return d

    def test_parses_run_and_hint(self):
        m = load_manifest(self._art(
            'kind="verb"\nexec="x"\n[[probe]]\nrun=["true"]\nhint="do the thing"\n'))
        self.assertEqual(len(m.probe), 1)
        self.assertEqual(m.probe[0].run, ("true",))
        self.assertEqual(m.probe[0].hint, "do the thing")

    def test_hint_is_optional(self):
        m = load_manifest(self._art('kind="verb"\nexec="x"\n[[probe]]\nrun=["true"]\n'))
        self.assertEqual(m.probe[0].hint, "")

    def test_absent_probe_is_empty(self):
        self.assertEqual(load_manifest(self._art('kind="verb"\nexec="x"\n')).probe, [])

    def test_unknown_key_is_named(self):
        with self.assertRaises(ValueError) as e:
            load_manifest(self._art('kind="verb"\nexec="x"\n[[probe]]\nrun=["true"]\nwhen="always"\n'))
        self.assertIn("when", str(e.exception))

    def test_run_must_be_a_non_empty_string_list(self):
        for bad in ('run="true"', 'run=[]', 'run=[1,2]'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    load_manifest(self._art(f'kind="verb"\nexec="x"\n[[probe]]\n{bad}\n'))
