import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from engine.provisioner import deploy as deploy_mod
from engine.provisioner.manifest import ArtefactManifest, BinEntry, FileEntry
from engine.provisioner.deploy import deploy_app


class _R:  # minimal Resolved stand-in (deploy_app only needs the manifest + paths)
    env = {}


def _app(root: Path) -> Path:
    art = root / "transcriber"
    (art / "scripts").mkdir(parents=True)
    (art / "scripts" / "transcriber").write_text("#!/bin/sh\necho shim\n")
    (art / "scripts" / "dictate-toggle.sh").write_text("#!/bin/sh\necho toggle\n")
    (art / "scripts" / "app.service").write_text("[Unit]\nDescription=x\n")  # no [Install]
    (art / "scripts" / "app.timer").write_text(
        "[Timer]\nOnUnitActiveSec=15min\n[Install]\nWantedBy=timers.target\n")
    return art


class TestDeployApp(unittest.TestCase):
    def test_bin_shims_symlinked_without_sh_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            m = ArtefactManifest("transcriber", "app",
                                 bin=[BinEntry("scripts/transcriber", "transcriber"),
                                      BinEntry("scripts/dictate-toggle.sh", "dictate-toggle")])
            res = deploy_app(m, _R(), art, root / "bin",
                             systemd_dir=root / "sd", reload_units=False,
                             systemctl_available=True)
            self.assertEqual([r.status for r in res], ["deployed", "deployed"])
            self.assertTrue((root / "bin" / "transcriber").is_symlink())
            link = root / "bin" / "dictate-toggle"        # .sh stripped on PATH
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (art / "scripts" / "dictate-toggle.sh").resolve())

    def test_undeclared_unit_is_linked_only(self):
        """A unit the machine does not name is linked and reported, not enabled."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            m = ArtefactManifest("transcriber", "app", units=["scripts/app.timer"])
            res = deploy_app(m, _R(), art, root / "bin",
                             systemd_dir=root / "sd", reload_units=False,
                             systemctl_available=True, enable=set())
            self.assertEqual(res[-1].status, "deployed")
            self.assertIn("linked only", res[-1].detail)
            self.assertIn("[units].enabled", res[-1].detail)
            self.assertTrue((root / "sd" / "app.timer").is_symlink())

    def test_unit_without_install_is_never_offered_for_declaring(self):
        """`enable` on a timer-triggered oneshot fails, so telling the human to
        declare it hands them an instruction that breaks their next install."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            m = ArtefactManifest("transcriber", "app", units=["scripts/app.service"])
            res = deploy_app(m, _R(), art, root / "bin",
                             systemd_dir=root / "sd", reload_units=False,
                             systemctl_available=True, enable=set())
            self.assertEqual(res[-1].status, "deployed")
            self.assertNotIn("linked only", res[-1].detail)
            self.assertNotIn("[units].enabled", res[-1].detail)

    def test_declared_unit_is_enabled(self):
        """A unit named in [units].enabled gets `systemctl --user enable`."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            m = ArtefactManifest("transcriber", "app",
                                 units=["scripts/enabled.service", "scripts/app.timer"])
            (art / "scripts" / "enabled.service").write_text(
                "[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=default.target\n")
            calls = []

            def fake_run(argv, *a, **kw):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch.object(deploy_mod.subprocess, "run", fake_run):
                res = deploy_app(m, _R(), art, root / "bin",
                                 systemd_dir=root / "sd", systemctl_available=True,
                                 enable={"enabled.service", "app.timer"})
            enables = [c for c in calls if c[:3] == ["systemctl", "--user", "enable"]]
            self.assertEqual(len(enables), 2)
            # A timer is started too; a service is not, because starting a daemon
            # at install time can seize a GPU or a port.
            self.assertIn(["systemctl", "--user", "enable", "--now", "app.timer"], enables)
            self.assertIn(["systemctl", "--user", "enable", "enabled.service"], enables)

    def test_failed_enable_is_an_error_that_says_what_it_saw(self):
        """A bare "enable failed" costs a second debugging session."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            m = ArtefactManifest("transcriber", "app", units=["scripts/app.timer"])

            def fake_run(argv, *a, **kw):
                if argv[:3] == ["systemctl", "--user", "enable"]:
                    return subprocess.CompletedProcess(argv, 1, "", "Unit app.timer is masked.")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch.object(deploy_mod.subprocess, "run", fake_run):
                res = deploy_app(m, _R(), art, root / "bin",
                                 systemd_dir=root / "sd", systemctl_available=True,
                                 enable={"app.timer"})
            self.assertEqual(res[-1].status, "error")
            self.assertIn("masked", res[-1].detail)

    def test_skips_when_requirement_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            m = ArtefactManifest("transcriber", "app", bin=[BinEntry("scripts/transcriber", "transcriber")],
                                 requires=["definitely-not-a-binary-xyz"])
            res = deploy_app(m, _R(), art, root / "bin",
                             systemd_dir=root / "sd", reload_units=False,
                             systemctl_available=True)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].status, "skipped")

    def test_missing_source_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            m = ArtefactManifest("transcriber", "app",
                                 bin=[BinEntry("scripts/nope", "nope"),
                                      BinEntry("scripts/transcriber", "transcriber")])
            res = deploy_app(m, _R(), art, root / "bin",
                             systemd_dir=root / "sd", reload_units=False,
                             systemctl_available=True)
            self.assertEqual([r.status for r in res], ["error", "deployed"])

    def test_refuses_to_overwrite_real_unit_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            sd = root / "sd"; sd.mkdir()
            (sd / "app.service").write_text("hand-written\n")
            m = ArtefactManifest("transcriber", "app", units=["scripts/app.service"])
            res = deploy_app(m, _R(), art, root / "bin",
                             systemd_dir=sd, reload_units=False, systemctl_available=True)
            self.assertEqual(res[-1].status, "error")
            self.assertIn("refusing to overwrite", res[-1].detail)
            self.assertEqual((sd / "app.service").read_text(), "hand-written\n")

    def test_units_skip_cleanly_without_systemctl(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            m = ArtefactManifest("transcriber", "app",
                                 bin=[BinEntry("scripts/transcriber", "transcriber")], units=["scripts/app.service"])
            res = deploy_app(m, _R(), art, root / "bin",
                             systemd_dir=root / "sd", reload_units=False,
                             systemctl_available=False)
            self.assertEqual(res[0].status, "deployed")        # bin still deploys
            self.assertEqual(res[-1].status, "skipped")
            self.assertIn("systemctl", res[-1].detail)
            self.assertFalse((root / "sd" / "app.service").exists())

    def test_duplicate_path_name_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            (art / "scripts" / "transcriber.sh").write_text("#!/bin/sh\n")
            m = ArtefactManifest("transcriber", "app",
                                 bin=[BinEntry("scripts/transcriber", "transcriber"),
                                      BinEntry("scripts/transcriber.sh", "transcriber")])
            res = deploy_app(m, _R(), art, root / "bin",
                             systemd_dir=root / "sd", reload_units=False,
                             systemctl_available=True)
            self.assertEqual([r.status for r in res], ["deployed", "error"])
            self.assertIn("duplicate", res[1].detail)
            self.assertEqual((root / "bin" / "transcriber").resolve(),
                             (art / "scripts" / "transcriber").resolve())

    def test_refuses_to_overwrite_real_bin_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            bin_dir = root / "bin"; bin_dir.mkdir()
            (bin_dir / "transcriber").write_text("hand-written\n")
            m = ArtefactManifest("transcriber", "app", bin=[BinEntry("scripts/transcriber", "transcriber")])
            res = deploy_app(m, _R(), art, bin_dir,
                             systemd_dir=root / "sd", reload_units=False,
                             systemctl_available=True)
            self.assertEqual(res[0].status, "error")
            self.assertIn("refusing to overwrite", res[0].detail)
            self.assertEqual((bin_dir / "transcriber").read_text(), "hand-written\n")

    def test_adopt_replaces_real_bin_file_and_unit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            bin_dir = root / "bin"; bin_dir.mkdir()
            sd = root / "sd"; sd.mkdir()
            (bin_dir / "transcriber").write_text("cp-era shim\n")
            (sd / "app.service").write_text("cp-era unit\n")
            m = ArtefactManifest("transcriber", "app",
                                 bin=[BinEntry("scripts/transcriber", "transcriber")], units=["scripts/app.service"])
            res = deploy_app(m, _R(), art, bin_dir,
                             systemd_dir=sd, reload_units=False,
                             systemctl_available=True, adopt=True)
            self.assertTrue(all(r.status == "deployed" for r in res))
            self.assertTrue((bin_dir / "transcriber").is_symlink())
            self.assertTrue((sd / "app.service").is_symlink())

    def test_idempotent_second_run(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            art = _app(root)
            m = ArtefactManifest("transcriber", "app",
                                 bin=[BinEntry("scripts/transcriber", "transcriber")], units=["scripts/app.service"])
            deploy_app(m, _R(), art, root / "bin", systemd_dir=root / "sd", reload_units=False,
                             systemctl_available=True)
            res = deploy_app(m, _R(), art, root / "bin",
                             systemd_dir=root / "sd", reload_units=False,
                             systemctl_available=True)
            self.assertTrue(all(r.status == "deployed" for r in res))


if __name__ == "__main__":
    unittest.main()
