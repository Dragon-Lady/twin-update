import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parent.parent / "tools" / "build_zipapp.py"
    spec = importlib.util.spec_from_file_location("build_zipapp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReproducibleZipapp(unittest.TestCase):
    def test_same_tree_same_hash(self):
        bz = _load()
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.pyz"
            b = Path(tmp) / "b.pyz"
            staging = Path(tmp) / "src"
            staging.mkdir()
            (staging / "__main__.py").write_text("print(1)\n")
            pkg = staging / "twin_update"
            pkg.mkdir()
            (pkg / "x.py").write_text("x = 1\n")
            ts = bz.zip_timestamp("1700000000")
            bz.write_zipapp(staging, a, timestamp=ts)
            bz.write_zipapp(staging, b, timestamp=ts)
            ha = hashlib.sha256(a.read_bytes()).hexdigest()
            hb = hashlib.sha256(b.read_bytes()).hexdigest()
            self.assertEqual(ha, hb)

    def test_source_date_epoch(self):
        bz = _load()
        self.assertEqual(bz.zip_timestamp("0"), (1970, 1, 1, 0, 0, 0))
        old = os.environ.pop("SOURCE_DATE_EPOCH", None)
        try:
            self.assertEqual(bz.zip_timestamp(), (1980, 1, 1, 0, 0, 0))
        finally:
            if old is not None:
                os.environ["SOURCE_DATE_EPOCH"] = old
