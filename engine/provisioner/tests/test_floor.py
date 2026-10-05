"""The provisioner must parse on the oldest Python it claims to support.

`hatch` is stdlib-only so that a fresh machine can run it with no install step,
and 3.11 is the floor because that is when `tomllib` landed — asserted in
`doctor.run_doctor`, the provisioner README, the spec, and `mise.toml`'s reason
for not pinning a python at all. The machines it has run on are 3.13 and 3.14,
so a syntax feature newer than the floor passes every local test and then makes
every `hatch` subcommand a SyntaxError on the machine that has the floor.

That is not hypothetical: PEP 701 (nested same-type quotes inside an f-string,
3.12+) reached `doctor.py` and took the whole CLI down under 3.11. `ast.parse`
with `feature_version=(3, 11)` does NOT catch it — the change is in the
tokenizer, not the grammar — so this compiles the package with a real 3.11.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
FLOOR = "3.11"


def _compile(interpreter: str, target: Path, cache: str) -> str:
    """Compile `target` with `interpreter`, keeping bytecode OUT of the source tree.

    `compileall` would otherwise leave .pyc files beside the sources, and CPython
    invalidates those on the source's mtime **to the second** — so a file edited
    within the same second as the cache was written keeps running the old
    bytecode. That cost a confusing half-hour once; PYTHONPYCACHEPREFIX is free.

    Returns the combined output: compileall exits 0 even on a SyntaxError, so the
    output is the artefact, not the return code.
    """
    env = dict(os.environ, PYTHONPYCACHEPREFIX=cache)
    p = subprocess.run([interpreter, "-m", "compileall", "-q", "-f", str(target)],
                       capture_output=True, text=True, env=env)
    return p.stdout + p.stderr


def _floor_interpreter():
    """A real 3.11, from PATH or from uv's managed pythons. None if unavailable."""
    if found := shutil.which(f"python{FLOOR}"):
        return found
    if shutil.which("uv"):
        p = subprocess.run(["uv", "python", "find", FLOOR],
                           capture_output=True, text=True)
        if p.returncode == 0 and (path := p.stdout.strip()) and Path(path).exists():
            return path
    return None


class TestPythonFloor(unittest.TestCase):
    def test_package_compiles_on_the_declared_floor(self):
        interpreter = _floor_interpreter()
        if interpreter is None:
            self.skipTest(
                f"no Python {FLOOR} on this machine — the floor is UNVERIFIED here. "
                f"`uv python install {FLOOR}` makes this test real.")
        with tempfile.TemporaryDirectory() as cache:
            out = _compile(interpreter, PACKAGE, cache)
        self.assertEqual(out, "", f"{PACKAGE} does not compile on Python {FLOOR} "
                                  f"({interpreter}):\n{out}")

    def test_the_floor_check_would_catch_a_newer_syntax_feature(self):
        """The detector, validated against a known-bad file rather than trusted."""
        interpreter = _floor_interpreter()
        if interpreter is None:
            self.skipTest(f"no Python {FLOOR} on this machine")
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "pep701.py"
            bad.write_text('x = f"{ {"a": 1}["a"] }"\n')   # 3.12+ only
            out = _compile(interpreter, bad, str(Path(d) / "cache"))
        self.assertIn("SyntaxError", out)
        if sys.version_info[:2] >= (3, 12):
            # The same source compiles on the interpreter running this suite —
            # which is exactly why the local suite cannot see this class of break,
            # and why the check has to shell out to the floor.
            compile('x = f"{ {\'a\': 1}[\'a\'] }"', "<probe>", "exec")


if __name__ == "__main__":
    unittest.main()
