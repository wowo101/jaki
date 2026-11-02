"""The fourth deploy strategy: a file symlinked to a path nothing can derive."""
import os
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


class TestRenderedFiles(unittest.TestCase):
    """`render = true`: a template filled in with this machine's paths, for a consumer
    that needs an absolute path inside the file (a native-messaging host manifest)."""

    def _setup(self, root: Path):
        art = root / "repo" / "engine" / "tools" / "keep"
        art.mkdir(parents=True)
        (art / "host.json.in").write_text('{"path": "{repo}/engine/tools/keep/host"}\n')
        dest = root / "home" / ".mozilla" / "native-messaging-hosts" / "hatch_keep.json"
        m = ArtefactManifest("keep", "verb", exec_name="keep",
                             files=[FileEntry("host.json.in", dest, render=True)])
        return art, dest, m

    def test_renders_under_hatch_and_links_the_dest_to_the_result(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art, dest, m = self._setup(root)
            repo = root / "repo"
            res = deploy_files(m, art, env={"HATCH_REPO": str(repo)})
            self.assertEqual([r.status for r in res], ["deployed"], res)
            rendered = repo / "engine/.hatch/rendered/keep/host.json"
            self.assertTrue(dest.is_symlink())
            self.assertEqual(dest.resolve(), rendered.resolve())
            self.assertEqual(dest.read_text(), f'{{"path": "{repo}/engine/tools/keep/host"}}\n')
            # The template stays a template.
            self.assertIn("{repo}", (art / "host.json.in").read_text())

    def test_the_template_mode_carries_over(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art, dest, m = self._setup(root)
            (art / "host.json.in").chmod(0o755)
            deploy_files(m, art, env={"HATCH_REPO": str(root / "repo")})
            self.assertTrue(os.access(dest.resolve(), os.X_OK))

    def test_a_json_render_that_does_not_parse_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art, dest, m = self._setup(root)
            res = deploy_files(m, art, env={"HATCH_REPO": str(root / 'repo"quoted')})
            self.assertEqual(res[0].status, "error")
            self.assertIn("not valid JSON", res[0].detail)
            self.assertFalse(dest.is_symlink())

    def test_two_templates_with_one_dest_name_render_to_two_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art, dest, _ = self._setup(root)
            (art / "chromium.json.in").write_text('{"path": "{repo}/chromium"}\n')
            other = root / "home" / ".config/chromium/NativeMessagingHosts" / "hatch_keep.json"
            m = ArtefactManifest("keep", "verb", exec_name="keep", files=[
                FileEntry("host.json.in", dest, render=True),
                FileEntry("chromium.json.in", other, render=True)])
            deploy_files(m, art, env={"HATCH_REPO": str(root / "repo")})
            self.assertIn("/host", dest.read_text())
            self.assertIn("/chromium", other.read_text())

    def test_without_resolved_config_it_is_an_error_and_nothing_is_linked(self):
        with tempfile.TemporaryDirectory() as d:
            art, dest, m = self._setup(Path(d))
            res = deploy_files(m, art)
            self.assertEqual(res[0].status, "error")
            self.assertIn("HATCH_REPO", res[0].detail)
            self.assertFalse(dest.exists() or dest.is_symlink())


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

    def test_render_is_parsed_and_defaults_off(self):
        on = self._load('[[files]]\nsource = "a.in"\ndest = "/tmp/a"\nrender = true\n')
        off = self._load('[[files]]\nsource = "a"\ndest = "/tmp/a"\n')
        self.assertTrue(on.files[0].render)
        self.assertFalse(off.files[0].render)

    def test_a_non_boolean_render_is_refused(self):
        with self.assertRaises(ValueError):
            self._load('[[files]]\nsource = "a.in"\ndest = "/tmp/a"\nrender = "yes"\n')

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
