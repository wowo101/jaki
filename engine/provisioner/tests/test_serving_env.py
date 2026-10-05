import tempfile, unittest
from pathlib import Path
from engine.provisioner.config import MachineConfig
from engine.provisioner.platform import PlatformInfo
from engine.provisioner.resolve import resolve
from engine.provisioner.emit import link_env_for_units, write_resolved_env, read_resolved_env

def _plat():
    return PlatformInfo(os="linux", opener="xdg-open", git_candidates=[], path_dirs=["/usr/local/bin"],
                        capabilities={"devonthink": False, "macwhisper": False})

class T(unittest.TestCase):
    def test_serving_values_derive_from_one_endpoint(self):
        cfg = MachineConfig("engine", "", {}, {"serve_url": "http://10.0.0.5:9090/v1"}, {})
        e = resolve(cfg, _plat()).env
        self.assertEqual(e["HATCH_SERVE_ADDR"], "10.0.0.5:9090")
        self.assertEqual(e["HATCH_SERVE_HOST"], "10.0.0.5")
        self.assertEqual(e["HATCH_SERVE_URL"], "http://10.0.0.5:9090/v1")
        self.assertTrue(e["HATCH_WEBUI_URL"].endswith(":3000"))
        self.assertTrue(e["HATCH_HOME"])

    def test_the_old_key_name_still_resolves(self):
        """`ollama_url` was this key's name until 2026-09-10 and is still read.

        A machine toml provisioned before the rename must not stop serving because the
        installer learned a new word for the same endpoint."""
        old = resolve(MachineConfig("engine", "", {}, {"ollama_url": "http://10.0.0.5:9090/v1"}, {}), _plat()).env
        new = resolve(MachineConfig("engine", "", {}, {"serve_url": "http://10.0.0.5:9090/v1"}, {}), _plat()).env
        for k in ("HATCH_SERVE_ADDR", "HATCH_SERVE_HOST", "HATCH_SERVE_URL", "OLLAMA_URL"):
            self.assertEqual(old[k], new[k], k)

    def test_serve_url_wins_when_both_are_set(self):
        cfg = MachineConfig("engine", "", {},
                            {"serve_url": "http://new:9090/v1", "ollama_url": "http://old:9090/v1"}, {})
        e = resolve(cfg, _plat()).env
        self.assertEqual(e["HATCH_SERVE_HOST"], "new")

    def test_old_env_name_still_emitted(self):
        """OLLAMA_URL keeps being written while `keep`, `note` and `gpu-lease` read it.

        Dropping it in the same commit that renames the key would switch a capture sweep
        off with no signal anywhere."""
        cfg = MachineConfig("engine", "", {}, {"serve_url": "http://10.0.0.5:9090/v1"}, {})
        e = resolve(cfg, _plat()).env
        self.assertEqual(e["OLLAMA_URL"], e["HATCH_SERVE_URL"])

    def test_no_endpoint_emits_no_derived_address(self):
        """A machine that serves nothing gets no address to bind.

        The URL keys are still emitted EMPTY, which is the established shape: `cfg()` never
        lets an empty value win, so a declared-but-empty key is how a machine says it has no
        endpoint. The DERIVED ones are absent instead, so llama-swap's `${env.…}` lookup fails
        by name at config load rather than binding something arbitrary."""
        e = resolve(MachineConfig("surface", "", {}, {}, {}), _plat()).env
        for k in ("HATCH_SERVE_ADDR", "HATCH_SERVE_HOST"):
            self.assertNotIn(k, e)
        self.assertEqual(e["HATCH_SERVE_URL"], "")
        self.assertEqual(e["OLLAMA_URL"], "")

    def test_an_unparseable_address_is_refused(self):
        """A serve_url without a scheme has no host, and used to fall through to the same
        empty result as an unset key. Empty reaches systemd as an EMPTY ARGUMENT, which
        llama-swap answers by binding 0.0.0.0:8080 and reporting active."""
        for bad in ("10.0.0.5:9090/v1", "somehost:9090", "nonsense"):
            with self.assertRaises(ValueError, msg=bad):
                resolve(MachineConfig("engine", "", {}, {"serve_url": bad}, {}), _plat())

    def test_serving_infra_without_an_address_is_refused(self):
        """Two units that start, bind 0.0.0.0 and report active is worse than a failed install."""
        with self.assertRaises(ValueError):
            resolve(MachineConfig("engine", "", {}, {}, {"infra": ["model-serving"]}), _plat())
        # A machine that does not deploy the tier is entitled to state no address.
        resolve(MachineConfig("surface", "", {}, {}, {"infra": ["qmd"]}), _plat())

    def test_https_default_port(self):
        e = resolve(MachineConfig("engine", "", {}, {"serve_url": "https://box/v1"}, {}), _plat()).env
        self.assertEqual(e["HATCH_SERVE_ADDR"], "box:443")

    def test_link_replaces_a_real_file(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t); target = d / "resolved.env"; target.write_text("A=1\n")
            link = d / "cfg" / "env"; link.parent.mkdir(); link.write_text("stale")
            link_env_for_units(target, link)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), target.resolve())

    def test_emitted_file_parses_as_systemd_would(self):
        """The emitted values survive systemd's EnvironmentFile parser.

        A SPACE IS NOT THE HAZARD, whatever a reading of this test suggests:
        systemd.exec(5) keeps interior whitespace verbatim, so a vault at `My Notes`
        parses fine. What does bite is POSIX escape handling in an unquoted value: a
        backslash escapes the next character, and a leading quote starts a quoted
        string that swallows to the next one.
        """
        with tempfile.TemporaryDirectory() as t:
            cfg = MachineConfig("engine", "", {},
                                {"serve_url": "http://10.0.0.5:9090/v1",
                                 "vault": "/x/My Notes"}, {})
            out = Path(t) / "resolved.env"
            write_resolved_env(resolve(cfg, _plat()), out)
            back = read_resolved_env(out)
            self.assertEqual(back["HATCH_SERVE_ADDR"], "10.0.0.5:9090")
            self.assertTrue(back["VAULT"].endswith("My Notes"), back["VAULT"])
            for k, v in back.items():
                self.assertNotIn(chr(92), v, f"{k} has a backslash, which systemd reads as an escape")
                self.assertNotIn(v[:1], ("'", '"'),
                                 f"{k} starts with a quote, which systemd reads as a quoted string")

if __name__ == "__main__":
    unittest.main()
