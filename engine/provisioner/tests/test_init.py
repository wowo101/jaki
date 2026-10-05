import tempfile
import unittest
from pathlib import Path

from engine.provisioner.init import init_machine


class TestInit(unittest.TestCase):
    def test_scaffolds_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "machines").mkdir()
            (root / "machines" / "_template.toml").write_text('role = "full"\ndescription = ""\n')
            dest = init_machine(root)
            self.assertTrue(dest.exists())
            self.assertNotIn('description = ""', dest.read_text())   # filled in from hostname
            with self.assertRaises(FileExistsError):
                init_machine(root)


if __name__ == "__main__":
    unittest.main()
