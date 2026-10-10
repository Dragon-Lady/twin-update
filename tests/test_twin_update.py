import io
import json
import os
import unittest

from fakes import PW, FakeUI, Twin
from twin_update import orchestrator
from twin_update.config import ConfigError, load
from twin_update.peer import serve
from twin_update.system import Proc

CUR = "/usr/share/cursor/cursor"
GPT = "/usr/lib/chatgpt/ChatGPT"


def standard(t, cursor_cached=True):
    t.setup("cursor", "3.23.23-1", "3.24.9-1", cached=cursor_cached)
    t.setup("chatgpt", "26.1002.52244", "26.1007.21434")
    t.setup("grok-bot", "0.68.1", "0.68.1")


def kinds(t, kind, user=None):
    return [e for e in t.events if e[1] == kind and (user is None or e[0] == user)]


def all_argv_text(t):
    return "\n".join(" ".join(c[0]) for s in t.sys.values() for c in s.calls)


class RunFlow(unittest.TestCase):
    def test_happy_path_updates_both_to_same_version(self):
        t = Twin(); standard(t)
        t.sys["machine-a"].add_app_procs(CUR)
        rep = orchestrator.run(t.session(FakeUI()), ["cursor", "chatgpt", "grok-bot"], dry=False)
        self.assertEqual(rep["result"], "ok", rep.get("error"))
        for l in ("machine-a", "machine-b"):
            self.assertEqual(t.sys[l].pkgs["cursor"], "3.24.9-1")
            self.assertEqual(t.sys[l].pkgs["chatgpt"], "26.1007.21434")
        self.assertEqual(rep["plan"]["grok-bot"]["action"], "current")
        self.assertIn("Cursor machine-a 3.23.23-1\u21923.24.9-1 \u2713", rep["summary"])
        # cursor was open on machine-a only -> reopened on machine-a only
        self.assertEqual(rep["reopen"], {"machine-a": {"cursor": {"ok": True}}})
        # pinned exact version
        self.assertIn("cursor=3.24.9-1", all_argv_text(t))

    def test_abort_on_both_when_one_cannot_close(self):
        t = Twin(); standard(t)
        t.sys["machine-a"].add_app_procs(CUR)
        main = t.sys["machine-b"].add_app_procs(GPT, n_children=1)
        t.sys["machine-b"].stubborn.add(main)
        rep = orchestrator.run(t.session(FakeUI()), ["cursor", "chatgpt"], dry=False)
        self.assertEqual(rep["result"], "aborted")
        self.assertIn("chatgpt on machine-b still running", rep["error"])
        self.assertEqual(kinds(t, "INSTALL"), [])
        for s in t.sys.values():
            self.assertEqual(s.pkgs["cursor"], "3.23.23-1")
        # machine-a cursor was closed and is reopened after the abort
        self.assertIn("cursor", rep["close"]["reopened_after_abort"]["machine-a"])
        # never force-killed: only TERM events exist
        self.assertTrue(all(e[1] != "KILL" for e in t.events))

    def test_no_update_before_both_closed(self):
        t = Twin(); standard(t)
        t.sys["machine-a"].add_app_procs(CUR)
        t.sys["machine-b"].add_app_procs(CUR)
        orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False, smoke=False)
        order = [e[1] for e in t.events if e[1] in ("TERM", "INSTALL")]
        self.assertEqual(order[:2], ["TERM", "TERM"])
        first_install = order.index("INSTALL")
        self.assertEqual(order[:first_install].count("TERM"), 2)

    def test_password_never_in_argv_env_logs_or_report(self):
        t = Twin(); standard(t)
        ui = FakeUI()
        rep = orchestrator.run(t.session(ui), ["cursor", "chatgpt"], dry=False)
        self.assertEqual(rep["result"], "ok", rep.get("error"))
        self.assertEqual(len(ui.prompts), 1)  # prompted exactly once for both machines
        sudo_calls = 0
        for s in t.sys.values():
            for argv, inp, env in s.calls:
                self.assertNotIn(PW, " ".join(argv))
                self.assertNotIn(PW, json.dumps(env or {}))
                if argv[0] == "sudo":
                    sudo_calls += 1
                    self.assertEqual(inp, (PW + "\n").encode())
                elif inp is not None:
                    self.assertNotIn(PW.encode(), inp)
        self.assertGreater(sudo_calls, 4)
        self.assertNotIn(PW, "\n".join(t.logs))
        self.assertNotIn(PW, json.dumps(rep))
        for s in t.sys.values():
            for root, _, files in os.walk(s.home()):
                for f in files:
                    with open(os.path.join(root, f), "rb") as fh:
                        self.assertNotIn(PW.encode(), fh.read())
        for e in t.eng.values():
            self.assertIsNone(e._pw)  # dropped after release

    def test_password_zeroed_on_release(self):
        t = Twin()
        e = t.eng["machine-a"]
        e.set_password(PW)
        buf = e._pw
        e.release()
        self.assertEqual(bytes(buf), b"\x00" * len(PW))

    def test_wrong_password_aborts_before_anything(self):
        t = Twin(); standard(t)
        t.sys["machine-a"].add_app_procs(CUR)
        rep = orchestrator.run(t.session(FakeUI(password="nope")), ["cursor"], dry=False)
        self.assertEqual(rep["result"], "aborted")
        self.assertEqual(kinds(t, "TERM"), [])

    def test_missing_rollback_copy_skips_app_on_both(self):
        t = Twin()
        t.setup("cursor", "3.23.23-1", "3.24.9-1", machines=("machine-a",))
        t.setup("cursor", "3.23.23-1", "3.24.9-1", cached=False, machines=("machine-b",))
        t.setup("chatgpt", "26.1002.52244", "26.1007.21434")
        rep = orchestrator.run(t.session(FakeUI()), ["cursor", "chatgpt"], dry=False, smoke=False)
        self.assertEqual(rep["plan"]["cursor"]["action"], "skip")
        self.assertIn("no rollback copy on machine-b", rep["plan"]["cursor"]["reason"])
        self.assertEqual(t.sys["machine-a"].pkgs["cursor"], "3.23.23-1")
        self.assertEqual(t.sys["machine-a"].pkgs["chatgpt"], "26.1007.21434")

    def test_rollback_copy_from_repo_download_and_local_file(self):
        t = Twin()
        t.setup("cursor", "3.23.23-1", "3.24.9-1", cached=False)
        t.sys["machine-a"].repo["cursor"].append("3.23.23-1")           # still in repo
        s = t.sys["machine-b"]
        s.make_deb(os.path.join(s.home(), "Downloads"), "cursor", "3.23.23-1")  # local copy
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False, smoke=False)
        self.assertEqual(rep["result"], "ok", rep.get("error"))
        caps = rep["rollback_capture"]["cursor"]
        self.assertEqual(caps["machine-a"]["source"], "apt-get download")
        self.assertEqual(caps["machine-b"]["source"], "local-file")
        self.assertTrue(caps["machine-b"]["verified"])

    def test_fallback_copy_is_flagged(self):
        t = Twin(fallback=["cursor"])
        t.setup("cursor", "3.22.12-1", "3.24.9-1", cached=False)
        for s in t.sys.values():
            s.make_deb(os.path.join(s.home(), "Downloads"), "cursor", "3.22.7-1")
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=True)
        self.assertEqual(rep["plan"]["cursor"]["action"], "update")
        self.assertEqual(sorted(rep["plan"]["cursor"]["rollback_fallback_on"]), ["machine-a", "machine-b"])
        self.assertEqual(rep["rollback_capture"]["cursor"]["machine-b"]["fallback_version"], "3.22.7-1")

    def test_mismatched_candidates_skip_both(self):
        t = Twin()
        t.setup("cursor", "3.23.23-1", {"machine-a": "3.24.9-1", "machine-b": "3.24.8-1"})
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        self.assertEqual(rep["plan"]["cursor"]["action"], "skip")
        self.assertIn("candidate versions differ", rep["plan"]["cursor"]["reason"])
        self.assertEqual(kinds(t, "INSTALL"), [])

    def test_installed_differs_but_same_candidate_updates_only_behind(self):
        t = Twin()
        t.setup("cursor", {"machine-a": "3.24.9-1", "machine-b": "3.22.12-1"}, "3.24.9-1")
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False, smoke=False)
        self.assertEqual(rep["plan"]["cursor"]["machines"], ["machine-b"])
        self.assertEqual([e[0] for e in kinds(t, "INSTALL")], ["bob"])

    def test_upgrade_failure_leaves_versions_flagged(self):
        t = Twin(); standard(t)
        t.sys["machine-b"].fail_install.add("cursor")
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False, smoke=False)
        self.assertEqual(rep["result"], "failed")
        self.assertTrue(any("versions differ now" in l for l in rep["summary"]))

    def test_failed_smoke_offers_rollback(self):
        t = Twin(); standard(t)
        t.sys["machine-b"].smoke_dies.add("cursor")
        ui = FakeUI(confirm=True)
        rep = orchestrator.run(t.session(ui), ["cursor"], dry=False)
        self.assertTrue(any("Roll back" in p for p in ui.prompts))
        self.assertEqual(t.sys["machine-b"].pkgs["cursor"], "3.23.23-1")
        self.assertIn("cursor", t.sys["machine-b"].held)
        self.assertTrue(rep["results"]["cursor"]["machine-b"]["rollback"]["ok"])

    def test_failed_smoke_without_consent_keeps_new_version(self):
        t = Twin(); standard(t)
        t.sys["machine-b"].smoke_dies.add("cursor")
        rep = orchestrator.run(t.session(FakeUI(confirm=False)), ["cursor"], dry=False)
        self.assertEqual(rep["result"], "failed")
        self.assertEqual(kinds(t, "DOWNGRADE"), [])

    def test_identity_mismatch_aborts(self):
        t = Twin(); standard(t)
        t.sys["machine-b"].user = "mallory"
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        self.assertEqual(rep["result"], "aborted")
        self.assertIn("identity", rep["error"])
        self.assertEqual(t.sys["machine-b"].calls and [c for c in t.sys["machine-b"].calls if c[0][0] == "sudo"], [])

    def test_lock_busy_aborts_real_run_and_is_blocker_in_dry_run(self):
        t = Twin(); standard(t)
        t.sys["machine-b"].locked = True
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        self.assertEqual(rep["result"], "aborted")
        self.assertIn("lock", rep["error"])
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=True)
        self.assertTrue(any("lock" in b for b in rep["blockers"]))

    def test_apt_busy_aborts(self):
        t = Twin(); standard(t)
        t.sys["machine-a"].apt_busy_flag = True
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        self.assertEqual(rep["result"], "aborted")
        self.assertIn("apt/dpkg busy", rep["error"])

    def test_report_written_privately_with_labels_only(self):
        t = Twin(); standard(t)
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False, smoke=False)
        for l, s in t.sys.items():
            d = os.path.join(s.home(), ".local/state/twin-update/runs")
            files = os.listdir(d)
            self.assertEqual(len(files), 1)
            path = os.path.join(d, files[0])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path) as fh:
                text = fh.read()
            for leak in ("alice", "bob", s.home()):
                self.assertNotIn(leak, text)
        self.assertEqual(rep["report_paths"]["machine-a"]["path"][:1], "~")


class DryRun(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        t = Twin(); standard(t)
        t.sys["machine-b"].held.add("chatgpt")
        t.sys["machine-a"].add_app_procs(CUR)
        before = {l: (dict(s.pkgs), set(s.held), dict(s.proc_table)) for l, s in t.sys.items()}
        homes_before = {l: sorted(os.walk(s.home())) for l, s in t.sys.items()}
        ui = FakeUI()
        rep = orchestrator.run(t.session(ui), ["cursor", "chatgpt", "grok-bot"], dry=True)
        self.assertEqual(rep["result"], "dry-run")
        self.assertEqual(ui.prompts, [])
        for l, s in t.sys.items():
            self.assertEqual((s.pkgs, s.held, s.proc_table), before[l])
            self.assertEqual(sorted(os.walk(s.home())), homes_before[l])
            self.assertFalse([c for c in s.calls if c[0][0] in ("sudo", "systemd-run", "notify-send")])
            self.assertFalse([c for c in s.calls if c[0][:2] == ["apt-get", "download"]])
        self.assertEqual(t.events, [])
        self.assertEqual(rep["holds"]["machine-b"][0]["action"], "would clear hold")
        self.assertEqual(rep["close"]["start"]["machine-a"]["cursor"]["action"], "would send SIGTERM")
        self.assertTrue(any(l.startswith("Cursor: update") for l in rep["summary"]))

    def test_single_machine_dry_run(self):
        t = Twin(); standard(t)
        del t.peers["machine-b"]
        s = orchestrator.Session(t.cfg, t.peers, ["machine-a", "machine-b"], FakeUI())
        rep = orchestrator.run(s, ["cursor"], dry=True, unreachable={"machine-b": "skipped"})
        self.assertEqual(rep["result"], "dry-run")
        self.assertIn("note", rep)

    def test_unreachable_peer_aborts_real_run(self):
        t = Twin(); standard(t)
        del t.peers["machine-b"]
        s = orchestrator.Session(t.cfg, t.peers, ["machine-a", "machine-b"], FakeUI())
        rep = orchestrator.run(s, ["cursor"], dry=False, unreachable={"machine-b": "ssh failed"})
        self.assertEqual(rep["result"], "aborted")


class Holds(unittest.TestCase):
    def test_foreign_hold_cleared_with_report_line(self):
        t = Twin(); standard(t)
        t.sys["machine-b"].held.add("chatgpt")
        t.sys["machine-b"].held.add("linux-generic")
        rep = orchestrator.run(t.session(FakeUI()), ["chatgpt"], dry=False, smoke=False)
        self.assertEqual(rep["result"], "ok", rep.get("error"))
        self.assertEqual(rep["holds"]["machine-b"], [{"app": "chatgpt", "owner": "foreign",
                                                  "action": "cleared hold", "ok": True}])
        self.assertNotIn("chatgpt", t.sys["machine-b"].held)
        self.assertIn("linux-generic", t.sys["machine-b"].held)  # unrelated holds untouched
        self.assertEqual(t.sys["machine-b"].pkgs["chatgpt"], "26.1007.21434")

    def test_kept_hold_skips_app_on_both(self):
        t = Twin(keep=["chatgpt"]); standard(t)
        t.sys["machine-b"].held.add("chatgpt")
        rep = orchestrator.run(t.session(FakeUI()), ["chatgpt"], dry=False)
        self.assertEqual(rep["plan"]["chatgpt"]["action"], "skip")
        self.assertIn("chatgpt", t.sys["machine-b"].held)
        self.assertEqual(kinds(t, "UNHOLD"), [])
        self.assertEqual(t.sys["machine-a"].pkgs["chatgpt"], "26.1002.52244")

    def test_holds_listing_and_owner(self):
        t = Twin(); standard(t)
        t.eng["machine-a"].set_password(PW)
        t.eng["machine-a"].hold("cursor", "test")
        t.sys["machine-a"].held.add("grok-bot")
        h = orchestrator.holds(t.session(FakeUI()))["machines"]["machine-a"]["managed"]
        self.assertEqual(h, {"cursor": "twin-update", "grok-bot": "foreign"})

    def test_unhold_command(self):
        t = Twin(); standard(t)
        t.sys["machine-b"].held.add("cursor")
        out = orchestrator.unhold(t.session(FakeUI()), "cursor", ["machine-a", "machine-b"], dry=True)
        self.assertEqual(out["machines"]["machine-b"]["action"], "would unhold")
        self.assertIn("cursor", t.sys["machine-b"].held)
        out = orchestrator.unhold(t.session(FakeUI()), "cursor", ["machine-a", "machine-b"], dry=False)
        self.assertEqual(out["machines"]["machine-b"]["action"], "unheld")
        self.assertNotIn("cursor", t.sys["machine-b"].held)


class Retention(unittest.TestCase):
    def _versions(self, t, l, app="cursor"):
        return sorted(t.eng[l].stored_copies(t.cfg.apps[app]))

    def test_one_rollback_copy_kept_and_older_pruned_after_grace(self):
        t = Twin()
        t.setup("cursor", "3.22.0-1", "3.23.23-1")
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False, smoke=False)
        self.assertEqual(rep["result"], "ok", rep.get("error"))
        self.assertEqual(self._versions(t, "machine-a"), ["3.22.0-1"])
        # second update: 3.23.23 captured; 3.22.0 must survive until grace passes
        for s in t.sys.values():
            s.make_deb(s.archives_dir, "cursor", "3.23.23-1")
            s.cands["cursor"] = "3.24.9-1"
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)  # smoke -> 1 launch
        self.assertEqual(rep["result"], "ok", rep.get("error"))
        self.assertEqual(self._versions(t, "machine-a"), ["3.22.0-1", "3.23.23-1"])
        # second good launch observed by status -> grace met -> prune leaves exactly one
        t.sys["machine-a"].add_app_procs(CUR, start=5000)
        t.eng["machine-a"].status(["cursor"])
        out = t.eng["machine-a"].prune(["cursor"], dry=False)
        self.assertEqual(out["cursor"]["removed"], ["3.22.0-1"])
        self.assertEqual(self._versions(t, "machine-a"), ["3.23.23-1"])
        # machine-b: no second launch yet -> kept; after 7 days -> pruned
        self.assertEqual(t.eng["machine-b"].prune(["cursor"], dry=False)["cursor"]["removed"], [])
        t.clock.t += 7 * 86400 + 1
        self.assertEqual(t.eng["machine-b"].prune(["cursor"], dry=False)["cursor"]["removed"],
                         ["3.22.0-1"])
        self.assertEqual(self._versions(t, "machine-b"), ["3.23.23-1"])

    def test_unverified_update_never_prunes(self):
        t = Twin()
        t.setup("cursor", "3.22.0-1", "3.23.23-1")
        t.sys["machine-a"].smoke_dies.add("cursor")
        orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        t.clock.t += 30 * 86400
        self.assertEqual(t.eng["machine-a"].prune(["cursor"], dry=False)["cursor"]["removed"], [])

    def test_rollback_command_restores_and_holds(self):
        t = Twin()
        t.setup("cursor", "3.23.23-1", "3.24.9-1")
        orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False, smoke=False)
        t.sys["machine-b"].add_app_procs(CUR)
        dry = orchestrator.rollback(t.session(FakeUI()), "cursor", ["machine-b"], dry=True)
        self.assertEqual(dry["result"], "dry-run")
        self.assertEqual(t.sys["machine-b"].pkgs["cursor"], "3.24.9-1")
        rep = orchestrator.rollback(t.session(FakeUI()), "cursor", ["machine-b"], dry=False)
        self.assertEqual(rep["result"], "ok", rep.get("error"))
        self.assertEqual(t.sys["machine-b"].pkgs["cursor"], "3.23.23-1")
        self.assertEqual(t.sys["machine-a"].pkgs["cursor"], "3.24.9-1")
        self.assertIn("cursor", t.sys["machine-b"].held)
        self.assertEqual(rep["reopen"], {"machine-b": {"cursor": {"ok": True}}})
        # the next deliberate run releases our own hold
        h = t.eng["machine-b"].holds()["managed"]
        self.assertEqual(h["cursor"], "twin-update")

    def test_rollback_refuses_tampered_copy(self):
        t = Twin()
        t.setup("cursor", "3.23.23-1", "3.24.9-1")
        orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False, smoke=False)
        copy = t.eng["machine-b"].rollback_target("cursor")["path"]
        with open(copy, "a") as fh:
            fh.write(" ")
        t.eng["machine-b"].set_password(PW)
        res = t.eng["machine-b"].rollback_install("cursor")
        self.assertFalse(res["ok"])
        self.assertIn("sha256", res["reason"])
        self.assertEqual(kinds(t, "DOWNGRADE"), [])

    def test_rollback_without_copy_aborts(self):
        t = Twin(); standard(t)
        rep = orchestrator.rollback(t.session(FakeUI()), "cursor", ["machine-a", "machine-b"], dry=False)
        self.assertEqual(rep["result"], "aborted")
        self.assertIn("no rollback copy", rep["error"])


class Processes(unittest.TestCase):
    def test_tree_includes_helpers_and_deleted_exe_but_not_unrelated_or_self(self):
        t = Twin()
        s, e = t.sys["machine-a"], t.eng["machine-a"]
        main = s.add_app_procs(GPT, n_children=1,
                               child_exes=[s.home() + "/.codex/plugins/x/extension-host"])
        s.proc_table[9001] = Proc(9001, 1, s.home() + "/.codex/plugins/y/extension-host", "eh", 5)
        s.proc_table[9002] = Proc(9002, 1, "/usr/lib/chatgpt/resources/codex", "codex", 6, True)
        s.proc_table[s.pid] = Proc(s.pid, main, "/usr/bin/python3", "twin-update", 7)
        tree = e.app_tree(t.cfg.apps["chatgpt"])
        self.assertIn(main + 2, tree["tree"])          # helper in the ChatGPT tree
        self.assertNotIn(9001, tree["tree"])           # unrelated codex helper
        self.assertIn(9002, tree["tree"])              # package exe, even if deleted
        self.assertNotIn(s.pid, tree["tree"])          # never ourselves
        self.assertEqual(tree["mains"], sorted([main, 9002]))
        st = e.close_start(["chatgpt"], dry=False)
        self.assertNotIn(s.home(), json.dumps(st))     # paths redacted to ~


class Transport(unittest.TestCase):
    def test_serve_roundtrip_never_echoes_password(self):
        t = Twin(); standard(t)
        reqs = [{"id": 1, "op": "identity", "args": {}},
                {"id": 2, "op": "set_password", "args": {"password": PW}},
                {"id": 3, "op": "validate_sudo", "args": {}},
                {"id": 4, "op": "rm_rf", "args": {}}]
        rfile = io.BytesIO(b"".join(json.dumps(r).encode() + b"\n" for r in reqs))
        wfile = io.BytesIO()
        serve(t.eng["machine-b"], rfile, wfile)
        out = [json.loads(l) for l in wfile.getvalue().splitlines()]
        self.assertTrue(out[0]["ok"] and out[0]["result"]["ok"])
        self.assertTrue(out[2]["result"]["ok"])
        self.assertFalse(out[3]["ok"])
        self.assertEqual([o["id"] for o in out], [1, 2, 3, 4])
        self.assertNotIn(PW, wfile.getvalue().decode())
        self.assertIsNone(t.eng["machine-b"]._pw)  # released at EOF

    def test_ssh_argv_has_no_secrets_and_batchmode(self):
        from twin_update.peer import SshPeer
        seen = {}

        class P:
            def __init__(self, argv, **kw):
                seen["argv"] = argv
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b'{"id":1,"ok":true,"result":{"ok":true}}\n')

            def poll(self):
                return None

        p = SshPeer("machine-b", "user@host.example", "~/.local/bin/twin-update", popen=P,
                    connect_timeout=None)
        p.call("set_password", password=PW)
        self.assertIn("BatchMode=yes", seen["argv"])
        self.assertNotIn(PW, " ".join(seen["argv"]))
        self.assertEqual(seen["argv"][-1], "~/.local/bin/twin-update local --serve --as machine-b")


class Config(unittest.TestCase):
    def test_needs_two_machines(self):
        with self.assertRaises(ConfigError):
            load(text='[machines.a]\nuser="x"\n')

    def test_self_label_by_whoami(self):
        cfg = load(text='[machines.a]\nuser="x"\n[machines.b]\nuser="y"\n', home="/h")
        self.assertEqual(cfg.self_label("y"), "b")
        self.assertEqual(cfg.peer_label("b"), "a")
        with self.assertRaises(ConfigError):
            cfg.self_label("z")

    def test_unknown_enabled_app(self):
        with self.assertRaises(ConfigError):
            load(text='[machines.a]\nuser="x"\n[machines.b]\nuser="y"\n[apps]\nenabled=["nope"]\n')

    def test_custom_app_and_home_expansion(self):
        cfg = load(text='[machines.a]\nuser="x"\nlock_paths=["~/l"]\n[machines.b]\nuser="y"\n'
                        '[apps]\nenabled=["example-app"]\n[apps.example-app]\npackage="example-app"\nroots=["/opt/Example"]\n'
                        'launch=["/opt/Example/example-app"]\n', home="/h")
        self.assertEqual(cfg.apps["example-app"].package, "example-app")
        self.assertEqual(cfg.machines["a"].lock_paths, ["/h/l"])


class Cli(unittest.TestCase):
    def test_local_only_requires_dry_run(self):
        from twin_update.cli import main
        ui = FakeUI()
        self.assertEqual(main(["run", "--local-only"], ui=ui), 2)
        self.assertIn("--local-only is allowed only with --dry-run", ui.lines[0])

    def test_cli_status_local_only_with_fakes(self):
        import tempfile
        from twin_update import cli
        t = Twin(); standard(t)
        s = t.sys["machine-a"]
        tmpd = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)
        cfgp = os.path.join(tmpd, "c.toml")
        from fakes import CONFIG
        with open(cfgp, "w") as fh:
            fh.write(CONFIG.format(keep="[]", fallback="[]"))
        ui = FakeUI()
        rc = cli.main(["--config", cfgp, "status", "--local-only"], ui=ui, system=s)
        self.assertEqual(rc, 0)
        self.assertTrue(any(l.startswith("machine-a") and "cursor" in l for l in ui.lines))
        self.assertTrue(any("unreachable" in l for l in ui.lines))



class VendorDebsAndNotify(unittest.TestCase):
    def _vendor(self, b_files):
        t = Twin()
        t.setup("cursor", "3.22.12-1", "3.22.12-1", cached=False)
        inst = {"usr/share/cursor/cursor": "aa", "usr/share/cursor/resources.pak": "bb"}
        for label, s in t.sys.items():
            with open(os.path.join(s.info_dir, "cursor.md5sums"), "w") as fh:
                fh.write("".join(f"{m}  {n}\n" for n, m in inst.items()))
            files = dict(inst) if label == "machine-a" else b_files
            files["etc/cursor/conf"] = "cc"          # conffile: not in dpkg's record
            s.make_deb_without_md5sums(s.archives_dir, "cursor", "3.22.12-1", files)
        return t

    def test_deb_without_md5sums_verified_by_content(self):
        t = self._vendor({"usr/share/cursor/cursor": "aa", "usr/share/cursor/resources.pak": "bb"})
        for e in t.eng.values():
            cap = e.capture("cursor", dry=True)
            self.assertTrue(cap["ok"], cap)
            self.assertIs(cap["verified"], True)

    def test_deb_without_md5sums_content_mismatch_refused(self):
        t = self._vendor({"usr/share/cursor/cursor": "XX", "usr/share/cursor/resources.pak": "bb"})
        self.assertIs(t.eng["machine-a"].capture("cursor", dry=True)["verified"], True)
        cap = t.eng["machine-b"].capture("cursor", dry=True)
        self.assertFalse(cap["ok"])
        e = t.eng["machine-b"]
        cap = e.capture("cursor", dry=False)
        self.assertFalse(cap["ok"])
        self.assertEqual(e.stored_copies(e._apps(["cursor"])[0]), {})   # nothing left in store

    def test_notify_falls_back_to_gdbus_then_soft_fails(self):
        t = Twin()
        e, s = t.eng["machine-a"], t.sys["machine-a"]
        s.missing_tools = {"notify-send"}
        r = e.notify("Twin Update", "Cursor machine-a 1\u21922 \u2713 it's")
        self.assertEqual(r, {"ok": True, "tool": "gdbus"})
        argv = s.calls[-1][0]
        self.assertEqual(argv[:2], ["gdbus", "call"])
        self.assertIn("'Cursor machine-a 1\u21922 \u2713 it\\'s'", argv)
        self.assertEqual(e.doctor()["notify_tool"], "gdbus")
        s.missing_tools = {"notify-send", "gdbus"}
        n = len(s.calls)
        self.assertFalse(e.notify("a", "b")["ok"])
        self.assertEqual(len(s.calls), n)



class HumanOutput(unittest.TestCase):
    """Plain (non --json) output must print with stdout not a TTY (pipes, ssh, logs)."""

    def _cli(self, args):
        import contextlib
        import tempfile
        from fakes import CONFIG
        from twin_update import cli
        t = Twin(); standard(t)
        t.sys["machine-a"].held.add("cursor")
        tmpd = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)
        cfgp = os.path.join(tmpd, "c.toml")
        with open(cfgp, "w") as fh:
            fh.write(CONFIG.format(keep="[]", fallback="[]"))
        buf = io.StringIO()
        self.assertFalse(buf.isatty())
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--config", cfgp, *args], system=t.sys["machine-a"])  # real ConsoleUI
        return rc, buf.getvalue()

    def test_doctor_prints_without_tty(self):
        rc, out = self._cli(["doctor", "--local-only"])
        self.assertEqual(rc, 0)
        self.assertIn("machine-a identity", out)
        self.assertIn("notify:", out)
        self.assertIn("machine-b unreachable", out)

    def test_status_holds_unhold_print_without_tty(self):
        for args, want in ((["status", "--local-only"], "cursor"),
                           (["holds", "--local-only"], "cursor (foreign)"),
                           (["unhold", "cursor", "--machine", "machine-a", "--dry-run",
                             "--local-only"], "would unhold"),
                           (["run", "--dry-run", "--local-only"], "blocker: machine-b unreachable")):
            rc, out = self._cli(args)
            self.assertIn(want, out, args)
            self.assertTrue(out.strip(), args)

    def test_json_still_works(self):
        rc, out = self._cli(["doctor", "--local-only", "--json"])
        self.assertEqual(json.loads(out)["kind"], "doctor")


class SameUserName(unittest.TestCase):
    def test_same_user_on_both_needs_and_uses_machine_id(self):
        base = ('[machines.desk]\nuser = "alice"\n{a}[machines.lap]\nuser = "alice"\n{b}')
        cfg = load(text=base.format(a="", b=""), home="/h")
        with self.assertRaises(ConfigError) as cm:
            cfg.self_label("alice", "m1")
        self.assertIn("machine_id_sha256", str(cm.exception))
        cfg = load(text=base.format(a='machine_id_sha256 = "m1"\n', b='machine_id_sha256 = "m2"\n'),
                   home="/h")
        self.assertEqual(cfg.self_label("alice", "m1"), "desk")
        self.assertEqual(cfg.self_label("alice", "m2"), "lap")
        with self.assertRaises(ConfigError):
            cfg.self_label("alice", "m3")



class Check(unittest.TestCase):
    """twin-update check: read-only, no sudo, notify on this machine only."""

    FORBIDDEN = ("sudo", "systemd-run", "apt-get", "notify-send-never")

    def _cli(self, t, args, factory=None):
        import contextlib
        import tempfile
        from fakes import CONFIG
        from twin_update import cli
        tmpd = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)
        cfgp = os.path.join(tmpd, "c.toml")
        with open(cfgp, "w") as fh:
            fh.write(CONFIG.format(keep="[]", fallback="[]"))
        factory = factory or (lambda label, target, cmd: t.peers[label])
        buf = io.StringIO()
        from unittest import mock
        from twin_update import engine as engine_mod
        s = t.sys["machine-a"]
        # The CLI builds its own local engine: keep its state and apt lists in the fake.
        with contextlib.redirect_stdout(buf), \
                mock.patch.dict(os.environ, {"HOME": s.home()}), \
                mock.patch.object(engine_mod, "APT_LISTS", t.eng["machine-a"].apt_lists):
            rc = cli.main(["--config", cfgp, "check", *args], system=s, peer_factory=factory)
        return rc, buf.getvalue()

    def _assert_read_only(self, t):
        for s in t.sys.values():
            bad = [c[0] for c in s.calls if c[0][0] in self.FORBIDDEN
                   or c[0][:2] in (["apt-mark", "hold"], ["apt-mark", "unhold"])]
            self.assertEqual(bad, [])
            self.assertTrue(all(c[1] is None for c in s.calls))   # nothing on stdin, no password
        self.assertEqual([e for e in t.events if e[1] in ("LAUNCH", "TERM", "UNHOLD")], [])

    def _notifies(self, t, label="machine-a"):
        return [c[0] for c in t.sys[label].calls if c[0][0] in ("notify-send", "gdbus")]

    def test_updates_available_exit_10_and_notify_once_per_day(self):
        t = Twin(); standard(t)
        t.sys["machine-a"].add_app_procs(CUR)
        rc, out = self._cli(t, ["--notify"])
        self.assertEqual(rc, 10)
        self.assertIn("Cursor 3.23.23-1\u21923.24.9-1 on both machines", out)
        self.assertIn("ChatGPT 26.1002.52244\u219226.1007.21434 on both machines", out)
        self.assertEqual(out.count("2.0 h old (cached; not refreshed by this check)"), 2)
        self.assertIn("notification: sent via notify-send", out)
        self.assertNotIn("Grok Bot", out)
        n = self._notifies(t)
        self.assertEqual(len(n), 1)
        self.assertIn("Run: twin-update run", n[0][-1])
        self.assertEqual(self._notifies(t, "machine-b"), [])     # other machine never notified
        rc, out = self._cli(t, ["--notify"])                      # same day, same updates
        self.assertEqual(rc, 10)
        self.assertIn("already sent today", out)
        self.assertEqual(len(self._notifies(t)), 1)
        t.clock.t += 86400                                        # next day: notify again
        self._cli(t, ["--notify"])
        self.assertEqual(len(self._notifies(t)), 2)
        self._assert_read_only(t)

    def test_no_updates_exit_0_and_silent(self):
        t = Twin()
        t.setup("cursor", "3.24.9-1", "3.24.9-1")
        t.setup("chatgpt", "26.1007.21434", "26.1007.21434")
        t.setup("grok-bot", "0.68.1", "0.68.1")
        rc, out = self._cli(t, ["--notify"])
        self.assertEqual(rc, 0)
        self.assertIn("result: no updates", out)
        self.assertEqual(self._notifies(t), [])
        self._assert_read_only(t)

    def test_older_candidate_is_not_an_update(self):
        t = Twin()
        t.setup("cursor", "3.24.9-1", "3.24.8-1")
        t.setup("chatgpt", "26.1007.21434", "26.1007.21434")
        t.setup("grok-bot", "0.68.1", "0.68.1")
        rc, _ = self._cli(t, [])
        self.assertEqual(rc, 0)

    def test_unreachable_peer_exit_1_error_notice_once_per_day(self):
        from twin_update.peer import PeerError
        t = Twin()
        t.setup("cursor", "3.24.9-1", "3.24.9-1")
        t.setup("chatgpt", "26.1007.21434", "26.1007.21434")
        t.setup("grok-bot", "0.68.1", "0.68.1")

        def down(label, target, cmd):
            raise PeerError(f"{label}: ssh session ended (exit 255)")
        rc, out = self._cli(t, ["--notify"], factory=down)
        self.assertEqual(rc, 1)
        self.assertIn("machine-b error: could not reach it over ssh (it may be off or asleep)", out)
        self.assertIn("result: check incomplete", out)
        self.assertEqual(len(self._notifies(t)), 1)
        self.assertIn("check incomplete", self._notifies(t)[0][-2])
        rc, out = self._cli(t, ["--notify"], factory=down)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self._notifies(t)), 1)                # at most once a day
        self._assert_read_only(t)

    def test_unreachable_peer_but_local_updates_exit_10(self):
        from twin_update.peer import PeerError
        t = Twin(); standard(t)

        def down(label, target, cmd):
            raise PeerError("no answer")
        rc, out = self._cli(t, ["--notify"], factory=down)
        self.assertEqual(rc, 10)
        self.assertIn("on machine-a", out)
        self.assertIn("Not checked: machine-b", self._notifies(t)[0][-1])

    def test_local_only_is_not_an_error(self):
        t = Twin()
        t.setup("cursor", "3.24.9-1", "3.24.9-1")
        t.setup("chatgpt", "26.1007.21434", "26.1007.21434")
        t.setup("grok-bot", "0.68.1", "0.68.1")
        rc, out = self._cli(t, ["--local-only", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["errors"], {})

    def test_older_peer_without_check_falls_back_to_status(self):
        from twin_update.peer import PeerError
        t = Twin(); standard(t)
        real = t.peers["machine-b"]

        class Old:
            def call(self, op, **kw):
                if op == "check":
                    raise PeerError("machine-b: op not allowed: check")
                return real.call(op, **kw)

            def close(self):
                real.close()
        rc, out = self._cli(t, [], factory=lambda l, tg, c: Old())
        self.assertEqual(rc, 10)
        self.assertIn("on both machines", out)

    def test_identity_mismatch_is_an_error(self):
        t = Twin(); standard(t)
        t.sys["machine-b"].user = "mallory"
        rc, out = self._cli(t, [])
        self.assertIn("machine-b error: identity does not match the config", out)
        self.assertEqual(rc, 10)          # machine-a still has updates


if __name__ == "__main__":
    unittest.main()
