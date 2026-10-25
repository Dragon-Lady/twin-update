"""Build dist/twin-update.pyz (single file, stdlib only, reproducible).

Zip entry timestamps are fixed. Honors SOURCE_DATE_EPOCH (UTC). Two builds
of the same tree therefore hash the same. Check: build twice and compare
``sha256sum dist/twin-update.pyz``.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import tempfile
import time
import zipfile


def zip_timestamp(epoch: str | None = None) -> tuple[int, int, int, int, int, int]:
    raw = epoch if epoch is not None else os.environ.get("SOURCE_DATE_EPOCH")
    if raw:
        t = time.gmtime(int(raw))
        return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    return (1980, 1, 1, 0, 0, 0)


def collect_files(src: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    """Return (archive_name, path) pairs in sorted order."""
    out = []
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "__pycache__" or "__pycache__" in path.parts:
            continue
        if path.suffix == ".pyc":
            continue
        out.append((path.relative_to(src).as_posix(), path))
    return out


def write_zipapp(src: pathlib.Path, dest: pathlib.Path,
                 interpreter: str = "/usr/bin/env python3",
                 timestamp: tuple[int, int, int, int, int, int] | None = None) -> None:
    date_time = timestamp if timestamp is not None else zip_timestamp()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    shebang = b"#!" + interpreter.encode("ascii") + b"\n"
    with open(tmp, "wb") as fh:
        fh.write(shebang)
        with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, path in collect_files(src):
                info = zipfile.ZipInfo(filename=name, date_time=date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o644 << 16
                zf.writestr(info, path.read_bytes())
    os.chmod(tmp, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    tmp.replace(dest)


def build(root: pathlib.Path | None = None) -> pathlib.Path:
    root = root or pathlib.Path(__file__).resolve().parent.parent
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staging = pathlib.Path(tmp)
        shutil.copytree(root / "src" / "twin_update", staging / "twin_update",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (staging / "__main__.py").write_text(
            "import sys\nfrom twin_update.cli import main\nsys.exit(main())\n")
        out = dist / "twin-update.pyz"
        write_zipapp(staging, out)
    return out


if __name__ == "__main__":
    print(build())
