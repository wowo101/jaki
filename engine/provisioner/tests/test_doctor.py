import shutil
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from pathlib import Path

from engine.provisioner import doctor
from engine.provisioner.manifest import load_manifest
from engine.provisioner.config import machine_config_path, load_machine_config
from engine.provisioner.emit import resolved_env_path, write_resolved_env
from engine.provisioner.resolve import resolve

LINUX = doctor.platform.PlatformInfo(
    os="linux", opener="xdg-open", git_candidates=["/usr/bin/git"],
    path_dirs=["/usr/local/bin"], capabilities={"devonthink": False, "macwhisper": False})


class _Repo:
    """A throwaway repo + machine + deploy dirs, driven exactly as `install` drives them."""

    def __init__(self, d):
        # The deploy dirs sit OUTSIDE the checkout, as ~/.local/bin does relative
        # to ~/Repositories/hatch. Nesting them inside makes a relative symlink
        # lexically "under" the repo and hides the normpath the drift scan needs.
        self.home = Path(d)
        self.root = self.home / "repo"
        self.bin = self.home / "bin"
        self.systemd = self.home / "systemd"
        self.vault = self.home / "vault"
        for p in (self.root, self.bin, self.systemd, self.vault):
            p.mkdir(parents=True)

    def machine(self, artefacts, endpoints="vault = \"%s\"\n", provision=True, units=()):
        (self.root / "machines").mkdir(exist_ok=True)
        unit_block = ""
        if units:
            listed = ", ".join(f'"{u}"' for u in units)
            unit_block = f"\n[units]\nenabled = [{listed}]\n"
        (self.root / "machines" / "host.toml").write_text(
            'role = "full"\n[endpoints]\n' + (endpoints % self.vault)
            + "[artefacts]\n" + artefacts + unit_block)
        if provision:
            self.provision()

    def provision(self):
        """Emit resolved.env the way `install` does, so doctor reads a real one."""
        cfg = load_machine_config(machine_config_path(self.root, "host"))
        r = resolve(cfg, LINUX)
        r.env["HATCH_REPO"] = str(self.root)
        write_resolved_env(r, resolved_env_path(self.root))

    def artefact(self, name, slot, manifest, files=()):
        tier = {"verb": "tools", "app": "apps", "infra": "infra", "plugin": "plugins"}[slot]
        d = self.root / "engine" / tier / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "provision.toml").write_text(manifest)
        for f in files:
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            (d / f).write_text("#!/bin/sh\n")
            (d / f).chmod(0o755)      # repo verbs and shims are executable; PATH cares
        return d

    def link(self, target: Path, source: Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source)

    def shim(self):
        """Deploy the `hatch` shim, as `install` does before anything else."""
        (self.root / "hatch").write_text("#!/usr/bin/env python3\n")
        (self.root / "hatch").chmod(0o755)
        self.link(self.bin / "hatch", self.root / "hatch")

    def run(self):
        with mock.patch.object(doctor.platform, "detect", return_value=LINUX), \
             mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            return doctor.run_doctor(self.root, "host", bin_dir=self.bin,
                                     systemd_dir=self.systemd)

    def check(self, name):
        return next(c for c in self.run() if c.name == name)


class TestBaseChecks(unittest.TestCase):
    def test_run_doctor_reports_checks(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            checks = r.run()
        names = [c.name for c in checks]
        self.assertTrue(any("python" in n for n in names))
        config_check = next(c for c in checks if c.name == "config")
        self.assertTrue(config_check.ok)
        self.assertIn("full", config_check.detail)

    def test_missing_config_short_circuits(self):
        with tempfile.TemporaryDirectory() as d:
            checks = doctor.run_doctor(Path(d), "nohost")
        config_check = next(c for c in checks if c.name == "config")
        self.assertFalse(config_check.ok)
        self.assertIn("init", config_check.detail)

    def test_unprovisioned_machine_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n', provision=False)
            c = r.check("resolved.env")
        self.assertFalse(c.ok)
        self.assertIn("hatch install", c.detail)

    def test_resolved_env_stale_against_the_machine_toml_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            # the endpoint moves in the toml; nobody re-runs install
            r.machine('verbs = []\n', endpoints='vault = "%s"\nollama_url = "http://box:8081"\n',
                      provision=False)
            c = r.check("resolved.env")
        self.assertFalse(c.ok)
        self.assertIn("stale", c.detail)
        self.assertIn("OLLAMA_URL", c.detail)

    def test_bin_dir_off_path_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            with mock.patch.object(doctor.platform, "detect", return_value=LINUX), \
                 mock.patch.dict(os.environ, {"PATH": "/nowhere"}):
                checks = doctor.run_doctor(r.root, "host", bin_dir=r.bin, systemd_dir=r.systemd)
        path_check = next(c for c in checks if c.name == "PATH")
        self.assertFalse(path_check.ok)
        self.assertIn(str(r.bin), path_check.detail)


    def test_missing_node_fails_only_a_machine_that_builds_plugins(self):
        """A serving box declares no plugins and has no reason to carry node.

        Failing its doctor on that made a correct install of the jaki guide end on a red line
        about plugin builds it never asked for."""
        for plugins, informational in (("plugins = []\n", True), ('plugins = ["pg"]\n', False)):
            with tempfile.TemporaryDirectory() as d:
                r = _Repo(d)
                r.machine(plugins)
                with mock.patch.object(doctor.platform, "detect", return_value=LINUX), \
                     mock.patch.object(doctor.shutil, "which", return_value=None):
                    checks = doctor.run_doctor(r.root, "host", bin_dir=r.bin, systemd_dir=r.systemd)
            node = next(c for c in checks if c.name == "node")
            self.assertFalse(node.ok)
            self.assertEqual(node.informational, informational, plugins)


_VERB = 'kind = "verb"\nexec = "kp"\nrequires = []\nneeds = []\n'


class TestVerbChecks(unittest.TestCase):
    def _repo(self, d, manifest=_VERB, deploy=True):
        r = _Repo(d)
        r.machine('verbs = ["kp"]\n')
        art = r.artefact("kp", "verb", manifest, files=["kp"])
        if deploy:
            r.link(r.bin / "kp", art / "kp")
        return r, art

    def test_deployed_verb_passes(self):
        with tempfile.TemporaryDirectory() as d:
            r, _ = self._repo(d)
            c = r.check("kp")
        self.assertTrue(c.ok, c.detail)

    def test_undeployed_verb_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r, _ = self._repo(d, deploy=False)
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("not deployed", c.detail)

    def test_dangling_symlink_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r, art = self._repo(d)
            (art / "kp").unlink()
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("dangling", c.detail)

    def test_symlink_to_the_wrong_source_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r, _ = self._repo(d, deploy=False)
            elsewhere = r.root / "elsewhere"
            elsewhere.write_text("x")
            r.link(r.bin / "kp", elsewhere)
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("expected", c.detail)

    def test_real_file_at_the_target_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r, _ = self._repo(d, deploy=False)
            (r.bin / "kp").write_text("hand-placed")
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("not hatch's symlink", c.detail)

    def test_missing_gate_binary_is_informational_and_names_it(self):
        with tempfile.TemporaryDirectory() as d:
            r, _ = self._repo(d, manifest='kind = "verb"\nexec = "kp"\nrequires = ["no-such-bin-xyz"]\n',
                              deploy=False)
            c = r.check("kp")
        self.assertTrue(c.ok)
        self.assertTrue(c.informational)
        self.assertIn("no-such-bin-xyz", c.detail)

    def test_missing_manifest_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["ghost"]\n')
            c = r.check("ghost")
        self.assertFalse(c.ok)
        self.assertIn("provision.toml", c.detail)

    def test_kind_mismatch_against_the_machine_config_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            r.artefact("kp", "verb", 'kind = "app"\nbin = []\n')
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("kind", c.detail)

    def test_inert_manifest_field_is_reported(self):
        """A field the verb strategy never reads. `bin` is one: a verb's PATH
        entry comes from `exec`, so a `bin` list deploys nothing."""
        with tempfile.TemporaryDirectory() as d:
            r, _ = self._repo(d, manifest=_VERB + 'bin = ["kp-extra"]\n')
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("bin", c.detail)

    def test_units_on_a_verb_are_not_inert(self):
        """`deploy_verb` links units, so declaring them is not a dead field.
        This is the case that made hatch-sync an app until 2026-08-23."""
        with tempfile.TemporaryDirectory() as d:
            r, _ = self._repo(d, manifest=_VERB + 'units = ["kp.service"]\n')
            c = r.check("kp")
        self.assertNotIn("inert", c.detail)


class TestNeedsChecks(unittest.TestCase):
    def _repo(self, d, needs, endpoints='vault = "%s"\n'):
        r = _Repo(d)
        r.machine('verbs = ["kp"]\n', endpoints=endpoints)
        art = r.artefact("kp", "verb",
                         f'kind = "verb"\nexec = "kp"\nrequires = []\nneeds = {needs}\n',
                         files=["kp"])
        r.link(r.bin / "kp", art / "kp")
        return r

    def test_supplied_key_passes(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._repo(d, '["VAULT"]').check("kp")
        self.assertTrue(c.ok, c.detail)

    def test_empty_key_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._repo(d, '["OLLAMA_URL"]').check("kp")
        self.assertFalse(c.ok)
        self.assertIn("OLLAMA_URL", c.detail)
        self.assertIn("empty", c.detail)

    def test_zero_valued_flag_counts_as_supplied(self):
        # HATCH_HAS_DEVONTHINK="0" is a real answer, not an absent one.
        with tempfile.TemporaryDirectory() as d:
            c = self._repo(d, '["HATCH_HAS_DEVONTHINK"]').check("kp")
        self.assertTrue(c.ok, c.detail)

    def test_needs_not_checked_when_resolved_env_was_never_written(self):
        # The resolved.env check owns that failure; repeating it per artefact
        # would bury it under one line per needs key.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n', provision=False)
            art = r.artefact("kp", "verb",
                             'kind = "verb"\nexec = "kp"\nrequires = []\nneeds = ["OLLAMA_URL"]\n',
                             files=["kp"])
            r.link(r.bin / "kp", art / "kp")
            c = r.check("kp")
        self.assertTrue(c.ok, c.detail)

    def test_needs_reads_the_emitted_file_not_a_fresh_resolve(self):
        # The artefacts read resolved.env; a toml edited without a re-install
        # must not make doctor report a value no verb can see.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')                       # emits OLLAMA_URL=
            r.machine('verbs = ["kp"]\n',                        # toml now sets it...
                      endpoints='vault = "%s"\nollama_url = "http://box:8081"\n',
                      provision=False)                            # ...but install never ran
            art = r.artefact("kp", "verb",
                             'kind = "verb"\nexec = "kp"\nrequires = []\nneeds = ["OLLAMA_URL"]\n',
                             files=["kp"])
            r.link(r.bin / "kp", art / "kp")
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("OLLAMA_URL", c.detail)

    def test_needs_not_checked_when_the_gate_binary_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            r.artefact("kp", "verb",
                       'kind = "verb"\nexec = "kp"\nrequires = ["no-such-bin-xyz"]\n'
                       'needs = ["OLLAMA_URL"]\n')
            c = r.check("kp")
        self.assertTrue(c.ok)
        self.assertNotIn("OLLAMA_URL", c.detail)


class TestAppAndPluginChecks(unittest.TestCase):
    def test_app_shims_and_units_are_checked(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('apps = ["ap"]\n')
            art = r.artefact("ap", "app",
                             'kind = "app"\nrequires = []\nbin = ["scripts/ap.sh"]\n'
                             'units = ["scripts/ap.service"]\n',
                             files=["scripts/ap.sh", "scripts/ap.service"])
            r.link(r.bin / "ap", art / "scripts" / "ap.sh")
            with mock.patch.object(doctor.shutil, "which", side_effect=lambda n: "/usr/bin/systemctl" if n == "systemctl" else None):
                probs, _ = doctor._deployed_problems(
                    doctor.load_manifest(art), art, r.bin, r.systemd, [])
        self.assertEqual(len(probs), 1)
        self.assertIn("ap.service", probs[0])
        self.assertIn("not deployed", probs[0])

    def test_app_units_skipped_without_systemctl(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            art = r.artefact("ap", "app",
                             'kind = "app"\nrequires = []\nbin = []\nunits = ["scripts/ap.service"]\n',
                             files=["scripts/ap.service"])
            with mock.patch.object(doctor.shutil, "which", return_value=None):
                probs, _ = doctor._deployed_problems(
                    doctor.load_manifest(art), art, r.bin, r.systemd, [])
        self.assertEqual(probs, [])

    def test_plugin_link_and_build_outputs_are_checked(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('plugins = ["pg"]\n')
            art = r.artefact("pg", "plugin",
                             'kind = "plugin"\nid = "pg"\noutputs = ["main.js"]\n')
            r.link(r.vault / ".obsidian" / "plugins" / "pg", art)
            c = r.check("pg")
        self.assertFalse(c.ok)
        self.assertIn("main.js", c.detail)

    def test_plugin_fully_deployed_passes(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('plugins = ["pg"]\n')
            art = r.artefact("pg", "plugin",
                             'kind = "plugin"\nid = "pg"\noutputs = ["main.js"]\n',
                             files=["main.js"])
            r.link(r.vault / ".obsidian" / "plugins" / "pg", art)
            c = r.check("pg")
        self.assertTrue(c.ok, c.detail)


class TestDrift(unittest.TestCase):
    def test_orphan_path_symlink_into_the_repo_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            stale = r.root / "engine" / "tools" / "gone" / "gone"
            stale.parent.mkdir(parents=True)
            stale.write_text("x")
            r.link(r.bin / "gone", stale)
            c = r.check("gone")
        self.assertTrue(c.ok)
        self.assertTrue(c.informational)
        self.assertIn("no declared artefact", c.detail)

    def test_dangling_orphan_fails(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            r.link(r.bin / "gone", r.root / "engine" / "tools" / "gone" / "gone")
            c = r.check("gone")
        self.assertFalse(c.ok)
        self.assertIn("dangling", c.detail)

    def test_foreign_symlink_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            outside = Path(tempfile.gettempdir()) / "hatch-doctor-foreign"
            outside.write_text("x")
            r.link(r.bin / "foreign", outside)
            names = [c.name for c in r.run()]
            outside.unlink()
        self.assertNotIn("foreign", names)

    def test_deployed_verb_shadowed_on_path_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            art = r.artefact("kp", "verb", _VERB, files=["kp"])
            r.link(r.bin / "kp", art / "kp")
            shadow_dir = r.root / "earlier"
            shadow_dir.mkdir()
            (shadow_dir / "kp").write_text("#!/bin/sh\n")
            (shadow_dir / "kp").chmod(0o755)
            with mock.patch.object(doctor.platform, "detect", return_value=LINUX), \
                 mock.patch.dict(os.environ, {"PATH": f"{shadow_dir}{os.pathsep}{r.bin}"}):
                checks = doctor.run_doctor(r.root, "host", bin_dir=r.bin, systemd_dir=r.systemd)
            c = next(c for c in checks if c.name == "kp")
        self.assertFalse(c.ok)
        self.assertIn("shadowed", c.detail)


class TestPathResolution(unittest.TestCase):
    """The claims that decide whether doctor cries wolf on a machine it has not run on."""

    def test_two_links_to_the_same_source_are_not_a_shadow(self):
        # The Mac's pre-provisioner /usr/local/bin/keep sorts earlier in PATH and
        # symlinks the same source. Reporting that would fail doctor there for nothing.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            r.shim()
            art = r.artefact("kp", "verb", _VERB, files=["kp"])
            r.link(r.bin / "kp", art / "kp")
            earlier = r.root / "usr-local-bin"
            earlier.mkdir()
            r.link(earlier / "kp", art / "kp")          # same source, different dir
            with mock.patch.object(doctor.platform, "detect", return_value=LINUX), \
                 mock.patch.dict(os.environ, {"PATH": f"{earlier}{os.pathsep}{r.bin}"}):
                checks = doctor.run_doctor(r.root, "host", bin_dir=r.bin, systemd_dir=r.systemd)
            c = next(c for c in checks if c.name == "kp")
        self.assertTrue(c.ok, c.detail)

    def test_a_relative_symlink_to_the_right_source_passes(self):
        # install writes absolute links, but the plugin-into-vault links predate
        # the provisioner and were made by hand.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            r.shim()
            art = r.artefact("kp", "verb", _VERB, files=["kp"])
            (r.bin / "kp").symlink_to(os.path.relpath(art / "kp", r.bin))
            c = r.check("kp")
        self.assertTrue(c.ok, c.detail)

    def test_deployed_but_not_executable_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            art = r.artefact("kp", "verb", _VERB, files=["kp"])
            (art / "kp").chmod(0o644)
            r.link(r.bin / "kp", art / "kp")
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("not executable", c.detail)

    def test_literal_tilde_on_path_is_not_a_match(self):
        """execve does not expand `~`, so a tilde entry is a broken PATH entry.

        HOME is pointed at the fixture so that `~/bin` WOULD expand to bin_dir:
        without that the check passes whether or not `_on_path` expands, and
        pins nothing.
        """
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            with mock.patch.object(doctor.platform, "detect", return_value=LINUX), \
                 mock.patch.dict(os.environ, {"PATH": "~/bin", "HOME": str(r.home)}):
                checks = doctor.run_doctor(r.root, "host", bin_dir=r.bin, systemd_dir=r.systemd)
            c = next(c for c in checks if c.name == "PATH")
        self.assertFalse(c.ok, "a literal ~ on PATH is not a usable entry")


class TestShimAndCollisions(unittest.TestCase):
    def test_deployed_shim_passes(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            r.shim()
            c = r.check("hatch shim")
        self.assertTrue(c.ok, c.detail)

    def test_dangling_shim_is_reported(self):
        # The shim is in no [artefacts] list and is excluded from drift, so
        # without its own check it is the one deployed thing nothing sees.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            r.shim()
            (r.root / "hatch").unlink()
            c = r.check("hatch shim")
        self.assertFalse(c.ok)
        self.assertIn("dangling", c.detail)

    def test_path_name_claimed_by_two_artefacts_is_reported_as_a_collision(self):
        # install refuses the second claimant; without this the refusal shows up
        # as "points at the wrong source", which re-running install cannot fix.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\napps = ["ap"]\n')
            r.shim()
            art = r.artefact("kp", "verb", _VERB, files=["kp"])
            r.artefact("ap", "app", 'kind = "app"\nrequires = []\nbin = ["kp"]\n',
                       files=["kp"])
            r.link(r.bin / "kp", art / "kp")
            c = r.check("ap")
        self.assertFalse(c.ok)
        self.assertIn("already claimed by kp", c.detail)


class TestManifestFaults(unittest.TestCase):
    def test_verb_without_exec_is_a_named_fault_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            r.artefact("kp", "verb", 'kind = "verb"\nrequires = []\n')
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("exec", c.detail)

    def test_plugin_without_id_is_a_named_fault_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('plugins = ["pg"]\n')
            r.artefact("pg", "plugin", 'kind = "plugin"\noutputs = []\n')
            c = r.check("pg")
        self.assertFalse(c.ok)
        self.assertIn("id", c.detail)

    def test_inert_needs_on_a_plugin_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('plugins = ["pg"]\n')
            art = r.artefact("pg", "plugin",
                             'kind = "plugin"\nid = "pg"\noutputs = []\nneeds = ["VAULT"]\n')
            r.link(r.vault / ".obsidian" / "plugins" / "pg", art)
            c = r.check("pg")
        self.assertFalse(c.ok)
        self.assertIn("needs", c.detail)

    def test_plugin_with_no_vault_configured_is_reported(self):
        # install calls this a hard error; doctor must not call it green.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('plugins = ["pg"]\n', endpoints='paperless_url = "x"  # %s\n')
            r.artefact("pg", "plugin", 'kind = "plugin"\nid = "pg"\noutputs = []\n')
            c = r.check("pg")
        self.assertFalse(c.ok)
        self.assertIn("no vault endpoint", c.detail)

    def test_needs_key_hatch_never_emits_is_reported_as_a_manifest_fault(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            art = r.artefact("kp", "verb",
                             'kind = "verb"\nexec = "kp"\nrequires = []\nneeds = ["NOPE_URL"]\n',
                             files=["kp"])
            r.link(r.bin / "kp", art / "kp")
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("not a key hatch emits", c.detail)


class TestMoreDrift(unittest.TestCase):
    def test_relative_orphan_into_the_repo_is_reported(self):
        # Every Stow-managed entry in ~/.local/bin is a relative link, so a
        # lexical is_relative_to on the raw join drops a whole class of leftover.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            stale = r.root / "engine" / "tools" / "gone" / "gone"
            stale.parent.mkdir(parents=True)
            stale.write_text("x")
            (r.bin / "gone").symlink_to(os.path.relpath(stale, r.bin))
            c = r.check("gone")
        self.assertTrue(c.ok)
        self.assertIn("no declared artefact", c.detail)

    def test_leftover_link_for_a_gated_artefact_surfaces_as_drift(self):
        # The gate says "not deployed here"; a link that is nonetheless present
        # is exactly drift, and must not fall between the two halves.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            art = r.artefact("kp", "verb",
                             'kind = "verb"\nexec = "kp"\nrequires = ["no-such-bin-xyz"]\n',
                             files=["kp"])
            r.link(r.bin / "kp", art / "kp")
            checks = r.run()
        gate = next(c for c in checks if c.name == "kp" and c.section == "artefacts")
        self.assertTrue(gate.informational)
        drift = next(c for c in checks if c.name == "kp" and c.section == "drift")
        self.assertIn("no declared artefact", drift.detail)

    def test_a_correctly_deployed_artefact_produces_no_drift_line(self):
        # The other half of the gate rule: names this machine SHOULD carry are
        # claimed, so a healthy deployment is silent in the drift section.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            r.shim()
            art = r.artefact("kp", "verb", _VERB, files=["kp"])
            r.link(r.bin / "kp", art / "kp")
            drift = [c.name for c in r.run() if c.section == "drift"]
        self.assertEqual(drift, [])

    def test_orphan_unit_in_the_systemd_dir_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('apps = []\n')
            stale = r.root / "engine" / "apps" / "gone" / "gone.service"
            stale.parent.mkdir(parents=True)
            stale.write_text("[Unit]\n")
            r.link(r.systemd / "gone.service", stale)
            c = r.check("gone.service")
        self.assertTrue(c.ok)
        self.assertIn("no declared artefact", c.detail)

    def test_missing_systemd_dir_is_not_an_error(self):
        # macOS has no ~/.config/systemd/user at all.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            self.assertEqual(doctor.drift_checks(r.root, r.root / "nope", set()), [])


class TestInfraSlot(unittest.TestCase):
    """engine/infra components are declared and checked like anything else —
    the chat surface's OOM guard was a standing drift line until it was."""

    _M = 'kind = "app"\nrequires = []\nneeds = []\nbin = ["guard"]\n'

    def test_declared_infra_artefact_is_checked_and_not_drift(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('infra = ["chat"]\n')
            r.shim()
            art = r.artefact("chat", "infra", self._M, files=["guard"])
            r.link(r.bin / "guard", art / "guard")
            checks = r.run()
        c = next(c for c in checks if c.name == "chat")
        self.assertTrue(c.ok, c.detail)
        self.assertEqual([c.name for c in checks if c.section == "drift"], [])

    def test_undeclared_infra_symlink_is_drift(self):
        # The state the box was in: deployed by hand, checked by nothing.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('infra = []\n')
            r.shim()
            art = r.artefact("chat", "infra", self._M, files=["guard"])
            r.link(r.bin / "guard", art / "guard")
            c = r.check("guard")
        self.assertTrue(c.informational)
        self.assertIn("no declared artefact", c.detail)

    def test_broken_infra_link_now_fails_instead_of_being_invisible(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('infra = ["chat"]\n')
            r.shim()
            art = r.artefact("chat", "infra", self._M, files=["guard"])
            r.link(r.bin / "guard", art / "guard")
            (art / "guard").unlink()
            c = r.check("chat")
        self.assertFalse(c.ok)
        self.assertIn("dangling", c.detail)

    def test_a_verb_manifest_listed_under_infra_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('infra = ["chat"]\n')
            r.artefact("chat", "infra", 'kind = "verb"\nexec = "x"\n')
            c = r.check("chat")
        self.assertFalse(c.ok)
        self.assertIn("[artefacts].infra", c.detail)


class TestResolvedEnvStaleness(unittest.TestCase):
    def test_a_key_no_longer_emitted_counts_as_stale(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = []\n')
            path = resolved_env_path(r.root)
            path.write_text(path.read_text() + "LEFTOVER_KEY=x\n")
            c = r.check("resolved.env")
        self.assertFalse(c.ok)
        self.assertIn("LEFTOVER_KEY", c.detail)

    def test_a_key_absent_from_the_file_is_distinguished_from_an_empty_one(self):
        # Different repairs: a re-install vs an edit to the machine toml. This is
        # the state the box was actually in.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.machine('verbs = ["kp"]\n')
            path = resolved_env_path(r.root)
            path.write_text("".join(l + "\n" for l in path.read_text().splitlines()
                                    if not l.startswith("OLLAMA_URL")))
            art = r.artefact("kp", "verb",
                             'kind = "verb"\nexec = "kp"\nrequires = []\nneeds = ["OLLAMA_URL"]\n',
                             files=["kp"])
            r.link(r.bin / "kp", art / "kp")
            c = r.check("kp")
        self.assertFalse(c.ok)
        self.assertIn("missing from resolved.env", c.detail)


class TestUnitEnablement(unittest.TestCase):
    """A linked unit is not a running one, and doctor has to tell them apart.

    Calibrated against the real failure: every highlight-sync unit sat `linked`
    with no `timers.target.wants/` entry for weeks while doctor reported
    `✓ highlight-sync — 3 shims on PATH, 6 units`. The link was present the
    whole time, so any check on the link passes in the broken state.
    """

    def _run(self, d, declared, really_enabled):
        r = _Repo(d)
        r.shim()
        art = r.artefact("sweeper", "app",
                         'kind = "app"\nrequires = []\nbin = []\n'
                         'units = ["s.timer", "s.service"]\n')
        (art / "s.timer").write_text("[Timer]\n[Install]\nWantedBy=timers.target\n")
        (art / "s.service").write_text("[Service]\nExecStart=/bin/true\n")  # no [Install]
        r.machine('apps = ["sweeper"]\n', units=declared)
        r.link(r.systemd / "s.timer", art / "s.timer")
        r.link(r.systemd / "s.service", art / "s.service")
        with mock.patch.object(doctor.shutil, "which",
                               side_effect=lambda n: "/usr/bin/systemctl" if n == "systemctl" else "/usr/bin/" + n), \
             mock.patch.object(doctor, "unit_is_enabled",
                               side_effect=lambda n: n in really_enabled):
            return {c.name: c for c in r.run()}

    def test_declared_but_not_enabled_fails(self):
        """The known-bad state. If this passes, the check is worthless."""
        with tempfile.TemporaryDirectory() as d:
            checks = self._run(d, declared=["s.timer"], really_enabled=set())
        c = checks["sweeper"]
        self.assertFalse(c.ok)
        self.assertIn("s.timer", c.detail)
        self.assertIn("NOT enabled", c.detail)

    def test_declared_and_enabled_passes(self):
        with tempfile.TemporaryDirectory() as d:
            checks = self._run(d, declared=["s.timer"], really_enabled={"s.timer"})
        c = checks["sweeper"]
        self.assertTrue(c.ok, c.detail)
        self.assertIn("1 unit enabled", c.detail)

    def test_undeclared_unit_is_named_not_faulted(self):
        """A timer belonging to the other machine is reported, never a failure."""
        with tempfile.TemporaryDirectory() as d:
            checks = self._run(d, declared=[], really_enabled=set())
        c = checks["sweeper"]
        self.assertTrue(c.ok, c.detail)
        self.assertIn("s.timer linked only", c.detail)

    def test_declared_unit_without_install_is_a_config_fault(self):
        """`install` skips it, so "not enabled" would be permanent and
        unfixable. The toml is the thing that is wrong."""
        with tempfile.TemporaryDirectory() as d:
            checks = self._run(d, declared=["s.service"], really_enabled=set())
        c = checks["sweeper"]
        self.assertFalse(c.ok)
        self.assertIn("no [Install]", c.detail)

    def test_undeclared_unit_that_is_actually_enabled_is_drift(self):
        """`install` has no disable path, so dropping a unit from the toml
        leaves it running while the report would claim it is off."""
        with tempfile.TemporaryDirectory() as d:
            checks = self._run(d, declared=[], really_enabled={"s.timer"})
        c = checks["sweeper"]
        self.assertFalse(c.ok)
        self.assertIn("NOT declared", c.detail)

    def test_timer_triggered_service_is_not_reported_as_linked_only(self):
        """`s.service` carries no [Install] and can never be enabled; listing it
        would bury the units that genuinely should be running."""
        with tempfile.TemporaryDirectory() as d:
            checks = self._run(d, declared=["s.timer"], really_enabled={"s.timer"})
        self.assertNotIn("s.service", checks["sweeper"].detail)


class TestUnknownDeclaredUnit(unittest.TestCase):
    def test_a_name_no_artefact_ships_is_reported(self):
        """A typo in [units].enabled would otherwise enable nothing and fault
        nothing — the silent-declaration class, one level lower down."""
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            r.shim()
            art = r.artefact("sweeper", "app",
                             'kind = "app"\nrequires = []\nbin = []\n'
                             'units = ["s.timer"]\n')
            (art / "s.timer").write_text("[Timer]\n[Install]\nWantedBy=timers.target\n")
            r.machine('apps = ["sweeper"]\n', units=["s.timer", "typo.timer"])
            r.link(r.systemd / "s.timer", art / "s.timer")
            with mock.patch.object(doctor.shutil, "which",
                                   side_effect=lambda n: "/usr/bin/systemctl" if n == "systemctl" else "/usr/bin/" + n), \
                 mock.patch.object(doctor, "unit_is_enabled", return_value=True):
                checks = {c.name: c for c in r.run()}
        c = checks["[units].enabled"]
        self.assertFalse(c.ok)
        self.assertIn("typo.timer", c.detail)
        self.assertNotIn("s.timer", c.detail.replace("typo.timer", ""))


class TestSystemdTimeParsing(unittest.TestCase):
    def test_spans(self):
        for text, seconds in [("15min", 900), ("4min", 240), ("1h", 3600),
                              ("1h30m", 5400), ("90", 90), ("2d", 172800),
                              ("30s", 30), ("1h 30min", 5400)]:
            self.assertEqual(doctor.parse_systemd_time(text), seconds, text)


class TestSweepStaleness(unittest.TestCase):
    """Enabled says systemd would run it; the heartbeat says it actually did.

    A service that fails every fire leaves its timer `enabled` forever, so the
    enablement check alone cannot see a sweep that has stopped working.
    """

    def _run(self, d, cadence="OnUnitActiveSec=15min\n", age_seconds=None):
        r = _Repo(d)
        r.shim()
        art = r.artefact("sweeper", "app",
                         'kind = "app"\nrequires = []\nbin = []\n'
                         'units = ["s.timer"]\n')
        (art / "s.timer").write_text("[Timer]\n" + cadence
                                     + "[Install]\nWantedBy=timers.target\n")
        r.machine('apps = ["sweeper"]\n', units=["s.timer"])
        r.link(r.systemd / "s.timer", art / "s.timer")
        state = r.home / "state"
        if age_seconds is not None:
            hb = state / "s" / "heartbeat"
            hb.parent.mkdir(parents=True)
            stamp = datetime.now().astimezone() - timedelta(seconds=age_seconds)
            hb.write_text(stamp.isoformat() + "\n")
        with mock.patch.object(doctor.shutil, "which",
                               side_effect=lambda n: "/usr/bin/systemctl" if n == "systemctl" else "/usr/bin/" + n), \
             mock.patch.object(doctor, "unit_is_enabled", return_value=True), \
             mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}):
            return {c.name: c for c in r.run()}["sweeper"]

    def test_stale_heartbeat_fails(self):
        """The known-bad state: three missed fires at a 15m cadence."""
        with tempfile.TemporaryDirectory() as d:
            c = self._run(d, age_seconds=2 * 3600)
        self.assertFalse(c.ok)
        self.assertIn("last completed run", c.detail)
        self.assertIn("cadence 15m", c.detail)

    def test_fresh_heartbeat_passes(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._run(d, age_seconds=60)
        self.assertTrue(c.ok, c.detail)
        self.assertNotIn("last completed run", c.detail)

    def test_absent_heartbeat_is_a_note_not_a_failure(self):
        """A sweep that has not run yet is not a broken one — and an absent
        stamp must not read as a huge age."""
        with tempfile.TemporaryDirectory() as d:
            c = self._run(d, age_seconds=None)
        self.assertTrue(c.ok, c.detail)
        self.assertIn("no completed run recorded yet", c.detail)

    def test_boot_only_timer_has_no_expected_rate(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._run(d, cadence="OnBootSec=4min\n", age_seconds=None)
        self.assertTrue(c.ok, c.detail)
        self.assertNotIn("no completed run", c.detail)


if __name__ == "__main__":
    unittest.main()


class TestFileEntries(unittest.TestCase):
    """`[[files]]` — the strategy for a destination nothing can derive, and the
    drift scan that has to follow it there."""

    def _m(self, r, dest):
        return ('kind = "app"\nbin = []\n[[files]]\n'
                f'source = "llama-swap.yaml"\ndest = "{dest}"\n')

    def _setup(self, r, dest):
        r.machine('infra = ["chat"]\n')
        r.shim()
        return r.artefact("chat", "infra", self._m(r, dest), files=["llama-swap.yaml"])

    def test_deployed_file_passes_and_is_counted(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            dest = r.home / "conf" / "llama-swap" / "config.yaml"
            art = self._setup(r, dest)
            r.link(dest, art / "llama-swap.yaml")
            c = r.check("chat")
        self.assertTrue(c.ok, c.detail)
        self.assertIn("1 file", c.detail)

    def test_undeployed_file_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            dest = r.home / "conf" / "config.yaml"
            self._setup(r, dest)
            c = r.check("chat")
        self.assertFalse(c.ok)
        self.assertIn("not deployed", c.detail)

    def test_a_hand_placed_copy_at_the_dest_is_reported_as_a_real_file(self):
        # The state every one of the box's twelve copies was in: present,
        # correct-looking, and carrying no link back to say which repo file it
        # came from or how old it is.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            dest = r.home / "conf" / "config.yaml"
            self._setup(r, dest)
            dest.parent.mkdir(parents=True)
            dest.write_text("models: {}\n")
            c = r.check("chat")
        self.assertFalse(c.ok)
        self.assertIn("real file, not hatch's symlink", c.detail)

    def test_a_file_pointing_somewhere_else_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            dest = r.home / "conf" / "config.yaml"
            art = self._setup(r, dest)
            (art / "decoy.yaml").write_text("x\n")
            r.link(dest, art / "decoy.yaml")
            c = r.check("chat")
        self.assertFalse(c.ok)
        self.assertIn("points at", c.detail)

    def test_drift_scan_reaches_the_declared_dest_dir(self):
        # The gap the re-audit found: the original audit scanned ~/.local/bin and
        # ~/.config/systemd/user, and llama-swap's config lives in neither.
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            dest = r.home / "conf" / "config.yaml"
            art = self._setup(r, dest)
            r.link(dest, art / "llama-swap.yaml")
            (art / "stray.yaml").write_text("x\n")
            r.link(dest.parent / "stray.yaml", art / "stray.yaml")
            checks = r.run()
        drift = [c for c in checks if c.section == "drift"]
        self.assertEqual([c.name for c in drift], ["stray.yaml"])
        self.assertIn("no declared artefact", drift[0].detail)

    def test_two_artefacts_claiming_one_dest_collide(self):
        with tempfile.TemporaryDirectory() as d:
            r = _Repo(d)
            dest = r.home / "conf" / "config.yaml"
            r.machine('infra = ["chat", "chat2"]\n')
            r.shim()
            for n in ("chat", "chat2"):
                r.artefact(n, "infra", self._m(r, dest), files=["llama-swap.yaml"])
            c = r.check("chat2")
        self.assertFalse(c.ok)
        self.assertIn("already claimed by chat", c.detail)


class TestProbeProblems(unittest.TestCase):
    """The gap the probe closes: a verb that is linked, resolvable, and dead.

    `browse` reported a clean tick while dying on import for want of a system
    package the provisioner cannot install (2026-08-23). A resolved symlink is a
    proxy for "it works", and these are the cases where the two come apart.
    """

    def _manifest(self, run, hint=""):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        body = 'kind="verb"\nexec="x"\n'
        if run is not None:
            body += "[[probe]]\nrun=" + repr(list(run)).replace("'", '"') + "\n"
            if hint:
                body += f'hint="{hint}"\n'
        (d / "provision.toml").write_text(body)
        return load_manifest(d), d

    def test_satisfied_probe_reports_nothing(self):
        m, d = self._manifest(["python3", "-c", "import sys"])
        self.assertEqual(doctor._probe_problems(m, d), [])

    def test_failing_probe_reports_the_cause_and_the_hint(self):
        m, d = self._manifest(["python3", "-c", "import no_such_module_xyz"], hint="install it")
        probs = doctor._probe_problems(m, d)
        self.assertEqual(len(probs), 1)
        # both halves: what broke, and what to do about it
        self.assertIn("no_such_module_xyz", probs[0])
        self.assertIn("install it", probs[0])

    def test_no_probe_declared_is_silent(self):
        m, d = self._manifest(None)
        self.assertEqual(doctor._probe_problems(m, d), [])

    def test_unrunnable_probe_is_reported_not_raised(self):
        m, d = self._manifest(["/nonexistent/binary/xyz"])
        probs = doctor._probe_problems(m, d)
        self.assertEqual(len(probs), 1)
        self.assertIn("could not run", probs[0])

    def test_a_failing_probe_fails_the_artefact_check(self):
        """Not just collected - it must turn the artefact's tick into a cross."""
        m, d = self._manifest(["python3", "-c", "raise SystemExit(3)"], hint="fix me")
        self.assertTrue(doctor._probe_problems(m, d))
