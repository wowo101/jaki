import unittest
from pathlib import Path

from engine.provisioner.config import MachineConfig
from engine.provisioner.manifest import ArtefactManifest, BinEntry, FileEntry
from engine.provisioner.layout import (app_bin_names, app_bin_target, artefact_dir,
                                       artefact_key, claim_path_names, file_dirs,
                                       file_target, slot_accepts,
                                       plugin_link, unit_target, vault_targets,
                                       verb_target)


class TestArtefactDir(unittest.TestCase):
    def test_slot_selects_the_tier(self):
        root = Path("/repo")
        self.assertEqual(artefact_dir(root, "keep", "verb"), root / "engine" / "tools" / "keep")
        self.assertEqual(artefact_dir(root, "transcriber", "app"), root / "engine" / "apps" / "transcriber")
        self.assertEqual(artefact_dir(root, "chat", "infra"), root / "engine" / "infra" / "chat")
        self.assertEqual(artefact_dir(root, "drafting-diff", "plugin"),
                         root / "engine" / "plugins" / "drafting-diff")

    def test_infra_lives_in_its_own_tier_but_deploys_as_an_app(self):
        # The whole point of separating slot from kind: a service directory is
        # provisioned without being relabelled machinery.
        self.assertTrue(slot_accepts("infra", "app"))
        self.assertFalse(slot_accepts("infra", "verb"))
        self.assertFalse(slot_accepts("app", "verb"))
        self.assertNotEqual(artefact_dir(Path("/repo"), "chat", "infra"),
                            artefact_dir(Path("/repo"), "chat", "app"))

    def test_artefact_key_is_the_toml_table_key(self):
        self.assertEqual([artefact_key(s) for s in ("verb", "app", "infra", "plugin")],
                         ["verbs", "apps", "infra", "plugins"])

    def test_a_plugin_dir_name_is_its_config_name(self):
        """No suffix, no fallback, and nothing on disk to consult: every slot
        resolves by arithmetic. A resolver that checked which of two candidate
        directories existed is a resolver that silently picks the wrong one when
        both do."""
        root = Path("/repo")
        self.assertEqual(artefact_dir(root, "keep-obsidian", "plugin"),
                         root / "engine" / "plugins" / "keep-obsidian")

    def test_unknown_slot_raises(self):
        with self.assertRaises(ValueError):
            artefact_dir(Path("/repo"), "x", "widget")


class TestTargets(unittest.TestCase):
    def test_verb_target_is_the_exec_name(self):
        m = ArtefactManifest("recall", "verb", exec_name="recall")
        self.assertEqual(verb_target(Path("/bin"), m), Path("/bin/recall"))

    def test_app_bin_names_strip_sh(self):
        m = ArtefactManifest("transcriber", "app",
                             bin=[BinEntry("scripts/transcriber", "transcriber"),
                                  BinEntry("scripts/dictate-toggle.sh", "dictate-toggle")])
        self.assertEqual(app_bin_names(m), ["transcriber", "dictate-toggle"])
        self.assertEqual(app_bin_target(Path("/bin"), m.bin[1]), Path("/bin/dictate-toggle"))

    def test_file_dirs_dedupes_and_keeps_order(self):
        m = ArtefactManifest("chat", "app", files=[
            FileEntry("a.yaml", Path("/etc/one/a.yaml")),
            FileEntry("b.yaml", Path("/etc/two/b.yaml")),
            FileEntry("c.yaml", Path("/etc/one/c.yaml")),
        ])
        self.assertEqual(file_dirs(m), [Path("/etc/one"), Path("/etc/two")])
        self.assertEqual(file_target(m.files[1]), Path("/etc/two/b.yaml"))

    def test_unit_target_drops_the_repo_subpath(self):
        self.assertEqual(unit_target(Path("/sd"), "scripts/taskboard.service"),
                         Path("/sd/taskboard.service"))

    def test_plugin_link_is_under_dot_obsidian(self):
        m = ArtefactManifest("drafting-diff-plugin", "plugin", plugin_id="drafting-diff")
        self.assertEqual(plugin_link(Path("/v"), m),
                         Path("/v/.obsidian/plugins/drafting-diff"))


class _R:  # minimal Resolved stand-in
    def __init__(self, vault):
        self.env = {"VAULT": vault}


def _cfg(extra_vaults=None):
    endpoints = {} if extra_vaults is None else {"extra_vaults": extra_vaults}
    return MachineConfig("full", "", {}, endpoints, {})


class TestVaultTargets(unittest.TestCase):
    def test_vault_only_when_no_extras(self):
        self.assertEqual(vault_targets(_cfg(), _R("/home/w/Notes")), [Path("/home/w/Notes")])

    def test_appends_extra_vaults_expanded(self):
        targets = vault_targets(_cfg(["~/Repositories/rp"]), _R("/home/w/Notes"))
        self.assertEqual(targets[0], Path("/home/w/Notes"))
        self.assertEqual(targets[1], Path("~/Repositories/rp").expanduser())

    def test_dedupes_primary_listed_in_extras(self):
        self.assertEqual(vault_targets(_cfg(["/home/w/Notes"]), _R("/home/w/Notes")),
                         [Path("/home/w/Notes")])

    def test_empty_when_no_vault_configured(self):
        self.assertEqual(vault_targets(_cfg(["~/x"]), _R("")), [Path("~/x").expanduser()])
        self.assertEqual(vault_targets(_cfg(), _R("")), [])


class TestPathNameClaims(unittest.TestCase):
    def test_collision_across_artefacts_is_reported(self):
        claimed = {}
        self.assertEqual(claim_path_names(claimed, "hatch", ["hatch"]), [])
        self.assertEqual(claim_path_names(claimed, "keep", ["keep"]), [])
        collisions = claim_path_names(claimed, "some-app", ["keep", "other"])
        self.assertEqual(len(collisions), 1)
        self.assertIn("'keep' already claimed by keep", collisions[0])
        # first claimant keeps the name; new names still registered
        self.assertEqual(claimed["keep"], "keep")
        self.assertEqual(claimed["other"], "some-app")


if __name__ == "__main__":
    unittest.main()
