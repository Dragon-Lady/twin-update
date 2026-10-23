"""Opt-in closed-profile snapshots. External adapters never read the live profile."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone

SCRIPTS = ("backup_cursor.py", "export_cursor_return.py", "verify_cursor_payload.py",
           "make_return_manifest.py")


def _root(engine):
    root = Path(engine.cfg.state_dir) / "raft"
    if root.is_symlink():
        raise ValueError("snapshot root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def _adapters(engine):
    root = Path(engine.cfg.raft.script_dir)
    # Legacy scripts have incompatible fixed-path behavior. Never execute them
    # merely because their filenames match the new protocol.
    if json.loads((root / "twin-update-raft.json").read_text()) != {"protocol": 1}:
        raise ValueError("snapshot-only adapter protocol 1 required")
    if not all((root / name).is_file() for name in SCRIPTS):
        raise ValueError("snapshot-only adapters missing")
    return root


def _snapshot(engine, value):
    path = Path(value)
    root = _root(engine).resolve()
    if path.is_symlink() or path.parent.resolve() != root or not path.name.startswith("snapshot-") or not path.is_dir():
        raise ValueError("invalid snapshot path")
    for entry in path.rglob("*"):
        if entry.is_symlink():
            raise ValueError("snapshot contains a symlink")
        entry.chmod(0o700 if entry.is_dir() else 0o600)
    return path


def pre_reopen(engine, app_name, smoke_ok):
    cfg = engine.cfg.raft
    skip = lambda why, **kw: {"ok": False, "skipped": why, **kw}
    if app_name != "cursor" or not cfg.enabled:
        return skip("disabled")
    if app_name in engine.cfg.no_launch:
        return skip("no_launch: left closed")
    if cfg.machines and engine.label not in cfg.machines:
        return skip("machine excluded")
    if smoke_ok is not True:
        return skip("no smoke test")
    lock = None
    path = None
    try:
        lock = engine.sys.try_flock(cfg.lock_path)
        if lock is None:
            return skip("raft lock busy")
        root = _root(engine)
        old = sorted((p for p in root.glob("snapshot-*") if p.is_dir() and not p.is_symlink()),
                     key=lambda p: p.stat().st_mtime, reverse=True)
        # Reserve one slot for the next snapshot. Uploads hold this same lock.
        for p in old[max(0, cfg.keep - 1):]:
            shutil.rmtree(p)
        found = engine.app_tree(engine.cfg.apps["cursor"])
        tree = found["tree"]
        if tree:
            return skip("Cursor still running", processes=[{"pid": pid, "comm": found["procs"][pid].comm} for pid in tree])
        source = Path(engine.sys.home()) / ".config/Cursor/User/globalStorage/state.vscdb"
        paths = [str(source) + suffix for suffix in ("", "-wal", "-shm")]
        holders = engine.sys.fd_holders(paths)
        if holders:
            return skip("holder check incomplete" if any(h.get("unreadable") for h in holders) else "database holders", holders=holders)
        scripts = _adapters(engine)
        path = Path(tempfile.mkdtemp(prefix="snapshot-", dir=root))
        path.chmod(0o700)
        result = engine.sys.run([sys.executable, str(scripts / SCRIPTS[0]),
                                 "--source", str(source), "--output", str(path / "state.vscdb")],
                                timeout=cfg.snapshot_timeout_s)
        if not result.ok:
            raise RuntimeError("snapshot timed out" if result.rc == 124 else "snapshot failed")
        if engine.sys.fd_holders(paths) or engine.app_tree(engine.cfg.apps["cursor"])["tree"]:
            raise RuntimeError("profile became active during snapshot")
        _snapshot(engine, path)
        db = path / "state.vscdb"
        if not db.is_file() or db.stat().st_size == 0:
            raise RuntimeError("snapshot is empty")
        return {"ok": True, "snapshot": str(path), "size": db.stat().st_size}
    except Exception as exc:
        if path is not None:
            shutil.rmtree(path, ignore_errors=True)
        return skip(str(exc))
    finally:
        if lock is not None:
            engine.sys.unlock(lock)


def upload_start(engine, snapshot):
    try:
        cfg = engine.cfg.raft
        if not cfg.enabled:
            return {"started": False, "reason": "disabled"}
        path = _snapshot(engine, snapshot)
        command = Path(os.path.expanduser(engine.cfg.machines[engine.label].remote_command))
        if not command.is_absolute() or not command.is_file():
            raise ValueError("remote_command must be an absolute installed executable path")
        unit = f"twin-raft-cursor-{int(engine.sys.now())}-{path.name[9:]}"
        config_args = ["--config", engine.cfg.config_path] if engine.cfg.config_path else []
        result = engine.sys.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
                                 "-p", f"RuntimeMaxSec={cfg.upload_timeout_s}", "-p", "UMask=0077",
                                 "--", os.path.abspath(sys.executable), str(command), *config_args,
                                 "raft-upload", str(path)], env=engine._user_env(), timeout=15)
        return {"unit": unit, "started": result.ok,
                "reason": "" if result.ok else "background start failed; snapshot retained"}
    except Exception as exc:
        return {"started": False, "reason": str(exc)}


def upload(engine, snapshot):
    """Run under the background unit; bounded lock wait, no live-profile reads."""
    cfg = engine.cfg.raft
    if not cfg.enabled:
        return {"ok": False, "reason": "disabled"}
    lock = None
    root = _root(engine)
    log = root / "upload.log"
    old_mask = os.umask(0o077)
    try:
        deadline = engine.sys.now() + 30
        while lock is None:
            lock = engine.sys.try_flock(cfg.lock_path)
            if lock is not None or engine.sys.now() >= deadline:
                break
            engine.sys.sleep(1)
        if lock is None:
            raise RuntimeError("raft lock busy; snapshot retained")
        path = _snapshot(engine, snapshot)
        scripts = _adapters(engine)
        for name in SCRIPTS[1:]:
            result = engine.sys.run([sys.executable, str(scripts / name), str(path)], timeout=cfg.upload_timeout_s)
            if not result.ok:
                raise RuntimeError(f"{name} failed; snapshot retained")
        payload = path / "payload"
        _snapshot(engine, path)
        if not payload.is_dir() or not any(payload.iterdir()):
            raise RuntimeError("empty payload; snapshot retained")
        label = re.sub(r"[^A-Za-z0-9_-]", "-", engine.label)
        stamp = datetime.fromtimestamp(engine.sys.now(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = cfg.remote.rstrip("/") + f"/{cfg.upload_prefix}-{label}-{stamp}-{path.name[9:]}"
        for op in ("copy", "check"):
            args = ["rclone", op, str(payload), target]
            if op == "check":
                args += ["--one-way", "--download"]
            result = engine.sys.run(args, timeout=cfg.upload_timeout_s)
            if not result.ok:
                raise RuntimeError(f"remote {op} failed; snapshot retained")
        shutil.rmtree(path)
        result = {"ok": True, "uploaded": True}
    except Exception as exc:
        result = {"ok": False, "reason": str(exc)}
    finally:
        if lock is not None:
            engine.sys.unlock(lock)
        os.umask(old_mask)
    # Only outcome metadata, never exported content or subprocess output.
    with log.open("a") as fh:
        os.chmod(log, 0o600)
        fh.write(json.dumps(result) + "\n")
    return result
