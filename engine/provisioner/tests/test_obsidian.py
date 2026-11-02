import json
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from engine.provisioner import obsidian
from engine.provisioner.config import load_machine_config
from engine.provisioner.doctor import run_doctor
from engine.provisioner.obsidian import (RELEASE_URL, community_plugin_problem,
                                         install_community_plugin, plugin_dir)

SPEC = {"id": "readwise-official", "repo": "readwiseio/obsidian-readwise", "version": "3.0.4"}


def _release(spec=SPEC, manifest=None, styles=True):
    """A fetcher serving one release's assets; every other URL is a 404."""
    manifest = manifest or {"id": spec["id"], "version": spec["version"]}
    files = {"manifest.json": json.dumps(manifest).encode(), "main.js": b"module.exports = 1;"}
    if styles:
        files["styles.css"] = b".x{}"
    urls = {RELEASE_URL.format(repo=spec["repo"], version=spec["version"], file=k): v
            for k, v in files.items()}
    calls = []

    def fetcher(url):
        calls.append(url)
        return urls.get(url)
    fetcher.calls = calls
    return fetcher


def _enabled(vault):
    return json.loads((vault / ".obsidian" / "community-plugins.json").read_text())


class TestInstallCommunityPlugin(unittest.TestCase):
    def test_fresh_vault_gets_files_and_enabled_list(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            res = install_community_plugin(SPEC, vault, _release())
            self.assertEqual(res.status, "deployed", res.detail)
            target = plugin_dir(vault, SPEC["id"])
            self.assertEqual((target / "main.js").read_bytes(), b"module.exports = 1;")
            self.assertTrue((target / "styles.css").is_file())
            self.assertEqual(_enabled(vault), [SPEC["id"]])
            self.assertEqual(list(target.glob("*.hatch-tmp")), [])

    def test_missing_optional_styles_is_fine(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            res = install_community_plugin(SPEC, vault, _release(styles=False))
            self.assertEqual(res.status, "deployed", res.detail)
            self.assertFalse((plugin_dir(vault, SPEC["id"]) / "styles.css").exists())

    def test_manifest_disagreeing_with_the_pin_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            res = install_community_plugin(SPEC, vault, _release(manifest={"id": SPEC["id"],
                                                                           "version": "9.9.9"}))
            self.assertEqual(res.status, "error")
            self.assertIn("9.9.9", res.detail)
            self.assertFalse(plugin_dir(vault, SPEC["id"]).exists())
            self.assertFalse((vault / ".obsidian" / "community-plugins.json").exists())

    def test_missing_required_asset_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            res = install_community_plugin(SPEC, Path(d), lambda url: None)
            self.assertEqual(res.status, "error")
            self.assertIn("404", res.detail)

    def test_network_failure_is_reported_not_raised(self):
        def broken(url):
            raise OSError("network unreachable")
        with tempfile.TemporaryDirectory() as d:
            res = install_community_plugin(SPEC, Path(d), broken)
            self.assertEqual(res.status, "error")
            self.assertIn("network unreachable", res.detail)

    def test_present_plugin_is_left_alone_and_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            target = plugin_dir(vault, SPEC["id"])
            target.mkdir(parents=True)
            (target / "manifest.json").write_text(json.dumps({"id": SPEC["id"], "version": "3.1.0"}))
            (target / "main.js").write_text("old")
            (target / "data.json").write_text('{"token": "keep me"}')
            (vault / ".obsidian" / "community-plugins.json").write_text('["obsidian-git", "typographer"]')
            fetcher = _release()
            res = install_community_plugin(SPEC, vault, fetcher)
            self.assertEqual(res.status, "deployed", res.detail)
            self.assertEqual(fetcher.calls, [])
            self.assertIn("3.1.0", (target / "manifest.json").read_text())
            self.assertEqual((target / "data.json").read_text(), '{"token": "keep me"}')
            self.assertEqual(_enabled(vault), ["obsidian-git", "typographer", SPEC["id"]])

    def test_second_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            install_community_plugin(SPEC, vault, _release())
            fetcher = _release()
            res = install_community_plugin(SPEC, vault, fetcher)
            self.assertEqual(fetcher.calls, [])
            self.assertIn("present", res.detail)
            self.assertEqual(_enabled(vault), [SPEC["id"]])

    def test_directory_without_manifest_keeps_its_settings(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            target = plugin_dir(vault, SPEC["id"])
            target.mkdir(parents=True)
            (target / "data.json").write_text('{"token": "keep me"}')
            res = install_community_plugin(SPEC, vault, _release())
            self.assertEqual(res.status, "deployed", res.detail)
            self.assertTrue((target / "main.js").is_file())
            self.assertEqual((target / "data.json").read_text(), '{"token": "keep me"}')

    def test_unreadable_enabled_list_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            (vault / ".obsidian").mkdir()
            (vault / ".obsidian" / "community-plugins.json").write_text("not json")
            res = install_community_plugin(SPEC, vault, _release())
            self.assertEqual(res.status, "error")
            self.assertEqual((vault / ".obsidian" / "community-plugins.json").read_text(), "not json")

    def test_interrupted_install_is_fetched_again(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            target = plugin_dir(vault, SPEC["id"])
            target.mkdir(parents=True)
            # What a crash mid-write leaves: an earlier file in place, main.js only staged.
            (target / "styles.css").write_bytes(b".x{}")
            (target / "main.js.hatch-tmp").write_bytes(b"partial")
            fetcher = _release()
            res = install_community_plugin(SPEC, vault, fetcher)
            self.assertEqual(res.status, "deployed", res.detail)
            self.assertTrue(fetcher.calls)
            self.assertEqual((target / "main.js").read_bytes(), b"module.exports = 1;")

    def test_manifest_is_written_last(self):
        written = []
        real = obsidian._write_atomic

        def record(path, data):
            written.append(path.name)
            real(path, data)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(obsidian, "_write_atomic", record):
            install_community_plugin(SPEC, Path(d), _release())
        self.assertEqual(written[:3][-1], "manifest.json")

    def test_write_failure_is_reported_not_raised(self):
        def boom(path, data):
            raise OSError("No space left on device")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(obsidian, "_write_atomic", boom):
            res = install_community_plugin(SPEC, Path(d), _release())
        self.assertEqual(res.status, "error")
        self.assertIn("No space left", res.detail)

    def test_missing_vault_is_refused_and_not_created(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d) / "Notes"
            res = install_community_plugin(SPEC, vault, _release())
            self.assertEqual(res.status, "error")
            self.assertFalse(vault.exists())


class TestCommunityPluginProblem(unittest.TestCase):
    def _installed(self, vault, version="3.0.4", enabled=True):
        target = plugin_dir(vault, SPEC["id"])
        target.mkdir(parents=True)
        (target / "manifest.json").write_text(json.dumps({"id": SPEC["id"], "version": version}))
        (target / "main.js").write_text("x")
        (vault / ".obsidian" / "community-plugins.json").write_text(
            json.dumps(["obsidian-git"] + ([SPEC["id"]] if enabled else [])))

    def test_missing_plugin_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            problem, _ = community_plugin_problem(SPEC, Path(d))
            self.assertIn("not installed", problem)

    def test_installed_but_disabled_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            self._installed(Path(d), enabled=False)
            problem, _ = community_plugin_problem(SPEC, Path(d))
            self.assertIn("not enabled", problem)

    def test_no_enabled_list_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            self._installed(Path(d))
            (Path(d) / ".obsidian" / "community-plugins.json").unlink()
            problem, _ = community_plugin_problem(SPEC, Path(d))
            self.assertIn("not enabled", problem)

    def test_manifest_without_main_js_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            self._installed(Path(d))
            (plugin_dir(Path(d), SPEC["id"]) / "main.js").unlink()
            problem, _ = community_plugin_problem(SPEC, Path(d))
            self.assertIn("main.js missing", problem)

    def test_manifest_that_is_not_an_object_is_a_problem_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            self._installed(Path(d))
            (plugin_dir(Path(d), SPEC["id"]) / "manifest.json").write_text("[]")
            problem, _ = community_plugin_problem(SPEC, Path(d))
            self.assertIn("unreadable", problem)

    def test_missing_vault_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            problem, _ = community_plugin_problem(SPEC, Path(d) / "Notes")
            self.assertIn("does not exist", problem)

    def test_installed_and_enabled_passes(self):
        with tempfile.TemporaryDirectory() as d:
            self._installed(Path(d))
            problem, detail = community_plugin_problem(SPEC, Path(d))
            self.assertIsNone(problem)
            self.assertEqual(detail, "3.0.4 enabled")

    def test_other_version_passes_and_names_the_pin(self):
        with tempfile.TemporaryDirectory() as d:
            self._installed(Path(d), version="3.1.0")
            problem, detail = community_plugin_problem(SPEC, Path(d))
            self.assertIsNone(problem)
            self.assertIn("3.1.0", detail)
            self.assertIn("3.0.4", detail)


class TestConfig(unittest.TestCase):
    def _load(self, body):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.toml"
            p.write_text('role = "full"\n' + body)
            return load_machine_config(p)

    def test_entries_load(self):
        cfg = self._load('[obsidian]\ncommunity_plugins = [{ id = "a", repo = "o/a", version = "1" }]\n')
        self.assertEqual(cfg.obsidian["community_plugins"][0]["repo"], "o/a")

    def test_absent_section_means_none(self):
        self.assertEqual(self._load("").obsidian, {})

    def test_non_string_version_is_refused(self):
        with self.assertRaises(ValueError):
            self._load('[obsidian]\ncommunity_plugins = [{ id = "a", repo = "o/a", version = 3.0 }]\n')

    def test_duplicate_id_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            self._load('[obsidian]\ncommunity_plugins = [{ id = "a", repo = "o/a", version = "1" },'
                       ' { id = "a", repo = "o/b", version = "2" }]\n')
        self.assertIn("twice", str(cm.exception))

    def test_obsidian_that_is_not_a_table_is_refused(self):
        with self.assertRaises(ValueError):
            self._load('obsidian = "x"\n')

    def test_doctor_reports_a_bad_section_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "machines").mkdir()
            (root / "machines" / "host.toml").write_text(
                'role = "full"\n[obsidian]\ncommunity_plugins = [{ id = "a", repo = "o/a" }]\n')
            checks = run_doctor(root, "host", bin_dir=root / "bin", systemd_dir=root / "sd")
        config = [c for c in checks if c.name == "config"]
        self.assertEqual(len(config), 1)
        self.assertFalse(config[0].ok)
        self.assertIn("version", config[0].detail)

    def test_entry_missing_a_key_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            self._load('[obsidian]\ncommunity_plugins = [{ id = "a", version = "1" }]\n')
        self.assertIn("repo", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
