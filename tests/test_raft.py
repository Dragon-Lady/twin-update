import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fakes import CONFIG, FakeUI, Twin
from test_twin_update import CUR, standard
from twin_update.config import ConfigError, Raft, load
from twin_update import orchestrator, raft
from twin_update.system import Proc, Result, System


def enabled(t):
    for engine in t.eng.values():
        root = Path(engine.sys.home())
        scripts = root / "adapters"
        scripts.mkdir()
        (scripts / "twin-update-raft.json").write_text('{"protocol": 1}')
        for name in raft.SCRIPTS:
            (scripts / name).touch()
        command = root / "twin-update.pyz"
        command.touch()
        engine.cfg.machines[engine.label].remote_command = str(command)
        engine.cfg.raft = Raft(enabled=True, script_dir=str(scripts), remote="backup:archive",
                               lock_path=str(root / ".local/state/sync/sync.lock"))
    return t.eng["machine-a"]


def snapshot_runner(engine, rc=0):
    original = engine.sys.run
    def run(argv, **kw):
        if "--source" in argv:
            if rc == 0:
                Path(argv[argv.index("--output") + 1]).write_bytes(b"invented snapshot")
            return Result(rc)
        return original(argv, **kw)
    return run


class LaunchPolicy(unittest.TestCase):
    def test_default_blocks_cursor_launch(self):
        cfg = load(text=CONFIG.format(keep="[]", fallback="[]").replace("no_launch = []\n", ""))
        self.assertEqual(cfg.no_launch, ["cursor"])

    def test_open_cursor_closes_updates_and_stays_closed(self):
        t = Twin(); standard(t)
        for engine in t.eng.values():
            engine.cfg.no_launch = ["cursor"]
        t.sys["machine-a"].add_app_procs(CUR)
        result = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        self.assertEqual(result["result"], "ok")
        self.assertTrue(any(e[1] == "TERM" for e in t.events))
        self.assertTrue(any(e[1] == "INSTALL" for e in t.events))
        self.assertFalse(any(e[1] == "LAUNCH" for e in t.events))
        self.assertIn("skipped", result["reopen"]["machine-a"]["cursor"])

    def test_rollback_closes_but_never_launches_cursor(self):
        t = Twin(); standard(t)
        orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False, smoke=False)
        for engine in t.eng.values():
            engine.cfg.no_launch = ["cursor"]
        t.sys["machine-a"].add_app_procs(CUR)
        t.events.clear()
        result = orchestrator.rollback(t.session(FakeUI()), "cursor", list(t.eng), dry=False)
        self.assertEqual(result["result"], "ok")
        self.assertTrue(any(e[1] == "TERM" for e in t.events))
        self.assertFalse(any(e[1] == "LAUNCH" for e in t.events))

    def test_no_launch_also_skips_raft(self):
        t = Twin(); e = enabled(t); e.cfg.no_launch = ["cursor"]
        self.assertIn("no_launch", e.pre_reopen_hook("cursor", True)["skipped"])
        self.assertEqual(e.sys.calls, [])


class RaftTests(unittest.TestCase):
    def test_requires_lock_and_remote(self):
        text = CONFIG.format(keep="[]", fallback="[]")
        for extra in ('', 'remote="backup:archive"', 'lock_path="/tmp/example.lock"'):
            with self.assertRaises(ConfigError):
                load(text=text + '\n[raft]\nenabled=true\n' + extra)

    def test_holder_skips_no_signal_and_reopens(self):
        t = Twin(); standard(t); e = enabled(t)
        e.sys.holder_table = [{"pid": 42, "comm": "db-reader"}]
        rep = {"summary": []}
        res = orchestrator._reopen(t.session(FakeUI()), rep, {e.label: ["cursor"]},
                                   {"cursor": {e.label: {"verify": {"smoke": {"ok": True}}}}})
        self.assertEqual(rep["raft"][e.label]["holders"][0]["pid"], 42)
        self.assertTrue(res[e.label]["cursor"]["ok"])
        self.assertFalse(any(x[1] == "TERM" for x in t.events))

    def test_lock_precedes_profile_inspection(self):
        t = Twin(); e = enabled(t)
        with patch.object(e.sys, "try_flock", return_value=None), patch.object(e, "app_tree", side_effect=AssertionError):
            self.assertEqual(e.pre_reopen_hook("cursor", True)["skipped"], "raft lock busy")

    def test_skip_gates_and_legacy_adapters(self):
        t = Twin(); e = enabled(t)
        self.assertEqual(e.pre_reopen_hook("cursor", False)["skipped"], "no smoke test")
        self.assertEqual(e.pre_reopen_hook("chatgpt", True)["skipped"], "disabled")
        e.cfg.raft.machines = ["machine-b"]
        self.assertEqual(e.pre_reopen_hook("cursor", True)["skipped"], "machine excluded")
        e.cfg.raft.machines = []
        Path(e.cfg.raft.script_dir, "twin-update-raft.json").unlink()
        self.assertFalse(e.pre_reopen_hook("cursor", True)["ok"])
        self.assertEqual(e.sys.calls, [])

    def test_happy_snapshot_permissions_and_background(self):
        t = Twin(); e = enabled(t)
        with patch.object(e.sys, "run", side_effect=snapshot_runner(e)):
            result = e.pre_reopen_hook("cursor", True)
        self.assertTrue(result["ok"], result)
        path = Path(result["snapshot"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual((path / "state.vscdb").stat().st_mode & 0o777, 0o600)
        started = e.raft_upload_start(str(path))
        self.assertTrue(started["started"])
        argv, _, env = e.sys.calls[-1]
        self.assertIn("RuntimeMaxSec=1200", argv)
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertTrue(os.path.isabs(argv[argv.index("--") + 1]))
        self.assertTrue(os.path.isabs(argv[argv.index("--") + 2]))
        with patch.object(e.sys, "run", return_value=Result(1)):
            self.assertFalse(e.raft_upload_start(str(path))["started"])
        self.assertTrue(path.exists())

    def test_race_discards_snapshot(self):
        t = Twin(); e = enabled(t)
        with patch.object(e.sys, "fd_holders", side_effect=[[], [{"pid": 42, "comm": "reader"}]]), \
             patch.object(e.sys, "run", side_effect=snapshot_runner(e)):
            result = e.pre_reopen_hook("cursor", True)
        self.assertFalse(result["ok"])
        self.assertEqual(list(raft._root(e).glob("snapshot-*")), [])

    def test_timeout_or_exception_reopens(self):
        for mode in ("raise", "timeout"):
            with self.subTest(mode=mode):
                t = Twin(); standard(t); e = enabled(t)
                if mode == "raise":
                    hook = patch.object(e, "pre_reopen_hook", side_effect=RuntimeError("hook failed"))
                else:
                    hook = patch.object(e.sys, "run", side_effect=snapshot_runner(e, 124))
                with hook:
                    rep = {"summary": []}
                    reopened = orchestrator._reopen(t.session(FakeUI()), rep, {e.label: ["cursor"]},
                                                    {"cursor": {e.label: {"verify": {"smoke": {"ok": True}}}}})
                self.assertTrue(reopened[e.label]["cursor"]["ok"])
                self.assertFalse(rep["raft"][e.label]["ok"])

    def test_dry_run_does_not_snapshot_or_launch(self):
        t = Twin(); standard(t); e = enabled(t); e.sys.add_app_procs(CUR)
        result = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=True)
        self.assertTrue(any("would raft" in x for x in result["summary"]))
        self.assertFalse(any("systemd-run" in c[0] or "--source" in c[0] for c in e.sys.calls))

    def test_retention_and_upload_failure_preserve_current(self):
        t = Twin(); e = enabled(t)
        with patch.object(e.sys, "run", side_effect=snapshot_runner(e)):
            for _ in range(4):
                result = e.pre_reopen_hook("cursor", True)
                self.assertTrue(result["ok"])
        self.assertEqual(len(list(raft._root(e).glob("snapshot-*"))), 2)
        with patch.object(e.sys, "run", return_value=Result(1)):
            self.assertFalse(raft.upload(e, result["snapshot"])["ok"])
        self.assertTrue(Path(result["snapshot"]).exists())

    def test_upload_order_target_and_verified_cleanup(self):
        t = Twin(); e = enabled(t)
        with patch.object(e.sys, "run", side_effect=snapshot_runner(e)):
            snap = e.pre_reopen_hook("cursor", True)["snapshot"]
        payload = Path(snap) / "payload"; payload.mkdir(); (payload / "archive").write_text("invented")
        calls = []
        def run(argv, **kw):
            calls.append(argv); return Result(0)
        with patch.object(e.sys, "run", side_effect=run):
            self.assertTrue(raft.upload(e, snap)["ok"])
        self.assertEqual([Path(c[1]).name for c in calls[:3]], list(raft.SCRIPTS[1:]))
        self.assertEqual([c[1] for c in calls[3:]], ["copy", "check"])
        self.assertRegex(calls[3][3], r"backup:archive/cursor-liferaft-machine-a-\d{8}T\d{6}Z-")
        self.assertFalse(Path(snap).exists())

    def test_real_holder_scan_with_invented_file(self):
        with tempfile.NamedTemporaryFile() as f:
            self.assertIn(os.getpid(), [h["pid"] for h in System().fd_holders([f.name])])

    def test_unreadable_descriptors_block_snapshot(self):
        with patch("os.listdir", return_value=[str(os.getpid())]), patch("os.readlink", side_effect=PermissionError):
            holders = System().fd_holders(["/tmp/invented.sqlite"])
        self.assertTrue(holders[0]["unreadable"])
        t = Twin(); e = enabled(t); e.sys.holder_table = holders
        result = e.pre_reopen_hook("cursor", True)
        self.assertEqual(result["skipped"], "holder check incomplete")
        self.assertEqual(e.sys.calls, [])

    def test_reaper_preserves_old_and_unrelated_workers(self):
        t = Twin(); e = t.eng["machine-a"]; s = e.sys
        root = s.home() + "/.config/Cursor/User/globalStorage/anysphere.cursor-agent-worker/"
        s.proc_table[50] = Proc(50, 1, root + "worker", "old", 10)
        original = s.run
        def run(argv, **kw):
            if argv[0] == "systemd-run":
                s.proc_table[60] = Proc(60, 1, CUR, "main", 100)
                s.proc_table[70] = Proc(70, 1, root + "worker", "new", 101)
                s.proc_table[71] = Proc(71, 1, "/usr/bin/node", "new", 102, argv=("/usr/bin/node", root + "worker.js"))
                s.proc_table[80] = Proc(80, 1, "/usr/bin/other", "other", 103)
            if argv[:3] == ["systemctl", "--user", "show"]:
                return Result(0, "60\n")
            return original(argv, **kw)
        with patch.object(s, "run", side_effect=run):
            result = e.smoke("cursor")
        self.assertEqual({r["pid"] for r in result["stray_reaped"]}, {70, 71})
        self.assertIn(50, s.proc_table); self.assertIn(80, s.proc_table)

    def test_rollback_hook_failure_still_reopens_when_permitted(self):
        t = Twin(); standard(t); e = enabled(t)
        orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False, smoke=False)
        e.sys.add_app_procs(CUR)
        with patch.object(e, "pre_reopen_hook", side_effect=RuntimeError("hook error")):
            result = orchestrator.rollback(t.session(FakeUI()), "cursor", list(t.eng), dry=False)
        self.assertEqual(result["raft"][e.label]["error"], "hook error")
        self.assertTrue(result["reopen"][e.label]["cursor"]["ok"])

    def test_peer_timeout_recovery_uses_fresh_connection(self):
        from twin_update.peer import SshPeer, PeerError
        streams = []
        class P:
            def __init__(self, argv, **kw):
                read_fd, write_fd = os.pipe()
                self.stdout = os.fdopen(read_fd, 'rb', buffering=0)
                self.stdin = io.BytesIO()
                streams.append((self.stdout, write_fd))
                os.write(write_fd, b'{"id":1,"ok":true,"result":{"ok":true}}\n')
            def wait(self, timeout=None): return 0
        peer = SshPeer("machine-b", "user@host.example", "/opt/tool.pyz", popen=P)
        try:
            self.assertTrue(peer.call("identity")["ok"])
            with self.assertRaises(PeerError):
                peer.call("pre_reopen_hook", call_timeout=0.01, app_name="cursor", smoke_ok=True)
            self.assertTrue(peer.call("reopen", apps=["cursor"])["ok"])
            self.assertEqual(len(streams), 2)
        finally:
            peer.close()
            for stream, fd in streams:
                stream.close(); os.close(fd)
