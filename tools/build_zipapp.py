"""Build dist/twin-update.pyz (single file, stdlib only)."""

import pathlib
import shutil
import tempfile
import zipapp

root = pathlib.Path(__file__).resolve().parent.parent
dist = root / "dist"
dist.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    shutil.copytree(root / "src" / "twin_update", pathlib.Path(tmp) / "twin_update",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (pathlib.Path(tmp) / "__main__.py").write_text(
        "import sys\nfrom twin_update.cli import main\nsys.exit(main())\n")
    out = dist / "twin-update.pyz"
    zipapp.create_archive(tmp, out, interpreter="/usr/bin/env python3", compressed=True)
    out.chmod(0o755)
print(out)
