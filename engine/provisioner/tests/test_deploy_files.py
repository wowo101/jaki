"""The fourth deploy strategy: a file symlinked to a path nothing can derive."""
import tempfile
import unittest
from pathlib import Path

from engine.provisioner.manifest import ArtefactManifest, FileEntry, load_manifest
from engine.provisioner.deploy import deploy_files


def _art(root: Path) -> Path:
    art = root / "chat"
    art.mkdir(parents=True)
    (art / "llama-swap.yaml").write_text("models: {}\n")
    return art


def _m(dest: Path, source: str = "llama-swap.yaml") -> ArtefactManifest:
    return ArtefactManifest("chat", "app", files=[FileEntry(source, dest)])


class TestDeployFiles(unittest.TestCase):
    def test_symlinks_to_an_arbitrary_dest_creating_parents(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _art(root)
            dest = root / "conf" / "llama-swap" / "config.yaml"
            res = deploy_files(_m(dest), art)
            self.assertEqual([r.status for r in res], ["deployed"])
            self.assertTrue(dest.is_symlink())
            self.assertEqual(dest.resolve(), (art / "llama-swap.yaml").resolve())
            # The point of a symlink here: the consumer reads the repo's bytes.
            self.assertEqual(dest.read_text(), "models: {}\n")

    def test_idempotent_second_run(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _art(root)
            dest = root / "conf" / "config.yaml"
            deploy_files(_m(dest), art)
            res = deploy_files(_m(dest), art)
            self.assertEqual([r.status for r in res], ["deployed"])
            self.assertTrue(dest.is_symlink())

    def test_refuses_a_real_file_and_adopt_replaces_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _art(root)
            dest = root / "conf" / "config.yaml"
            dest.parent.mkdir(parents=True)
            dest.write_text("the hand-placed copy that was running\n")

            res = deploy_files(_m(dest), art)
            self.assertEqual(res[0].status, "error")
            self.assertIn("refusing to overwrite", res[0].detail)
            # The evidence of what was running must survive the refusal.
            self.assertEqual(dest.read_text(), "the hand-placed copy that was running\n")

            res = deploy_files(_m(dest), art, adopt=True)
            self.assertEqual(res[0].status, "deployed")
            self.assertTrue(dest.is_symlink())

    def test_missing_source_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _art(root)
            res = deploy_files(_m(root / "conf" / "x.yaml", source="nope.yaml"), art)
            self.assertEqual(res[0].status, "error")
            self.assertIn("not found", res[0].detail)

    def test_duplicate_dest_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _art(root)
            (art / "other.yaml").write_text("x\n")
            dest = root / "conf" / "config.yaml"
            m = ArtefactManifest("chat", "app", files=[
                FileEntry("llama-swap.yaml", dest), FileEntry("other.yaml", dest)])
            res = deploy_files(m, art)
            self.assertEqual([r.status for r in res], ["deployed", "error"])
            self.assertIn("duplicate dest", res[1].detail)


class TestFileManifestParsing(unittest.TestCase):
    def _load(self, body: str):
        d = tempfile.mkdtemp()
        (Path(d) / "provision.toml").write_text('kind = "app"\n' + body)
        return load_manifest(Path(d))

    def test_dest_is_expanded_and_absolute(self):
        m = self._load('[[files]]\nsource = "llama-swap.yaml"\ndest = "~/.config/ls/config.yaml"\n')
        self.assertEqual(m.files[0].source, "llama-swap.yaml")
        self.assertEqual(m.files[0].dest, Path.home() / ".config/ls/config.yaml")
        self.assertTrue(m.files[0].dest.is_absolute())

    def test_relative_dest_is_refused(self):
        with self.assertRaises(ValueError) as e:
            self._load('[[files]]\nsource = "a.yaml"\ndest = "conf/a.yaml"\n')
        self.assertIn("absolute", str(e.exception))

    def test_missing_dest_is_refused(self):
        with self.assertRaises(ValueError):
            self._load('[[files]]\nsource = "a.yaml"\n')

    def test_unknown_key_is_refused(self):
        # `copy` in particular: there is no copy mode, and a manifest asking for
        # one must fail loudly rather than silently getting a symlink.
        with self.assertRaises(ValueError) as e:
            self._load('[[files]]\nsource = "a.yaml"\ndest = "/tmp/a.yaml"\ncopy = true\n')
        self.assertIn("copy", str(e.exception))

    def test_a_plugin_style_string_list_is_refused(self):
        # `files` used to mean a plugin's build outputs; that key is now
        # `outputs`. A manifest left on the old spelling must not parse quietly.
        with self.assertRaises(ValueError):
            self._load('files = ["main.js"]\n')


class TestBinNameMapping(unittest.TestCase):
    def _load(self, body: str):
        d = tempfile.mkdtemp()
        (Path(d) / "provision.toml").write_text('kind = "app"\n' + body)
        return load_manifest(Path(d))

    def test_string_entry_derives_the_name(self):
        m = self._load('bin = ["scripts/dictate-toggle.sh"]\n')
        self.assertEqual((m.bin[0].source, m.bin[0].name),
                         ("scripts/dictate-toggle.sh", "dictate-toggle"))

    def test_table_entry_takes_the_declared_name(self):
        m = self._load('[[bin]]\nsource = "qmd-guard.sh"\nname = "qmd"\n')
        self.assertEqual((m.bin[0].source, m.bin[0].name), ("qmd-guard.sh", "qmd"))

    def test_table_entry_without_a_name_is_refused(self):
        with self.assertRaises(ValueError):
            self._load('[[bin]]\nsource = "qmd-guard.sh"\n')

    def test_unknown_key_in_a_bin_table_is_refused(self):
        with self.assertRaises(ValueError):
            self._load('[[bin]]\nsource = "a.sh"\nname = "a"\ndest = "/tmp/a"\n')


if __name__ == "__main__":
    unittest.main()
