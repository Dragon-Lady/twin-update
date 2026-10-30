"""Leftover Cursor sandbox helpers: idle check and SIGTERM-only close (0.5.2)."""

from __future__ import annotations

import json
import os
import signal
import tempfile
import unittest
from unittest import mock

from fakes import CONFIG, FakeUI, Twin
from twin_update import cli, helpers, orchestrator
from twin_update.helpers import (ACTIVE, ORPHANED_IDLE, UNKNOWN, HelperFacts, classify,
                                 is_data_fd, parse_net_tcp, parse_stat)
from twin_update.system import Proc, System

SPEC = helpers.SPECS["cursor"]
HOME = "/home/tanya"
LOG = HOME + "/.config/Cursor/logs/20261029T191759/main.log"


def idle_facts(**kw):
    f = HelperFacts(pid=105637, ppid=1, exe=SPEC.exe, comm="cursorsandbox", start=100,
                    label="cursor_sandbox (enforce)", fds=["/dev/null", LOG, "socket:[1]"],
                    sockets=[("0A", True)], cpu=[0, 0], children=[], age_s=4 * 3600,
                    flags=["run-proxy"])
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def cls(f, main=False, reapers=(), min_age_s=600):
    return classify(f, SPEC, main_present=main, reapers=set(reapers), min_age_s=min_age_s,
                    home=HOME)


class Classifier(unittest.TestCase):
    def test_tonights_shape_is_idle(self):
        r = cls(idle_facts())
        self.assertEqual(r["class"], ORPHANED_IDLE, r["reasons"])
        self.assertEqual(r["orphan_of"], "init")

    def test_main_present_is_active(self):
        self.assertEqual(cls(idle_facts(), main=True)["class"], ACTIVE)

    def test_live_parent_is_active(self):
        self.assertEqual(cls(idle_facts(ppid=4242))["class"], ACTIVE)

    def test_user_systemd_subreaper_counts_as_orphaned(self):
        r = cls(idle_facts(ppid=1700), reapers={1700})
        self.assertEqual((r["class"], r["orphan_of"]), (ORPHANED_IDLE, "user-systemd"))

    def test_child_is_active(self):
        self.assertEqual(cls(idle_facts(children=[200000]))["class"], ACTIVE)

    def test_database_fds_are_active(self):
        for path in (HOME + "/.config/Cursor/User/globalStorage/state.vscdb",
                     HOME + "/.config/Cursor/User/globalStorage/state.vscdb-wal",
                     HOME + "/.config/Cursor/User/workspaceStorage/x/state.vscdb-shm",
                     "/tmp/anything.sqlite3"):
            r = cls(idle_facts(fds=[path]))
            self.assertEqual(r["class"], ACTIVE, path)
            self.assertEqual(r["db_fds"], 1)

    def test_log_file_only_stays_idle(self):
        self.assertFalse(is_data_fd(LOG, HOME, SPEC))
        self.assertEqual(cls(idle_facts(fds=[LOG]))["class"], ORPHANED_IDLE)

    def test_cpu_growth_is_active(self):
        r = cls(idle_facts(cpu=[5, 6]))
        self.assertEqual((r["class"], r["cpu_delta"]), (ACTIVE, 1))

    def test_missing_cpu_sample_is_active(self):
        self.assertEqual(cls(idle_facts(cpu=[0]))["class"], ACTIVE)

    def test_established_tcp_is_active(self):
        for state in ("01", "02", "03", "08"):
            self.assertEqual(cls(idle_facts(sockets=[("0A", True), (state, True)]))["class"],
                             ACTIVE, state)

    def test_listen_on_non_loopback_is_active(self):
        self.assertEqual(cls(idle_facts(sockets=[("0A", False)]))["class"], ACTIVE)

    def test_too_young_is_active(self):
        self.assertEqual(cls(idle_facts(age_s=300))["class"], ACTIVE)

    def test_read_error_is_unknown(self):
        self.assertEqual(cls(idle_facts(error="PermissionError"))["class"], UNKNOWN)

    def test_other_exe_or_comm_is_active(self):
        self.assertEqual(cls(idle_facts(exe="/usr/share/cursor/chrome_crashpad_handler"))["class"],
                         ACTIVE)
        self.assertEqual(cls(idle_facts(comm="bash"))["class"], ACTIVE)

    def test_wrong_label_is_active(self):
        for label in ("unconfined", "", "grok-bot (unconfined)"):
            self.assertEqual(cls(idle_facts(label=label))["class"], ACTIVE, label)


class Parsing(unittest.TestCase):
    def test_parse_stat_with_spaces_in_comm(self):
        raw = "42 (a b) c) S 1 " + " ".join(["0"] * 9) + " 7 3 " + " ".join(["0"] * 6) + " 555 0"
        st = parse_stat(raw)
        self.assertEqual((st["comm"], st["ppid"], st["cpu"], st["start"]), ("a b) c", 1, 10, 555))

    def test_parse_stat_bad(self):
        with self.assertRaises(ValueError):
            parse_stat("garbage")

    def test_parse_net_tcp_v4_and_v6(self):
        text = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:9BCD 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 111 1\n"
            "   1: 0101A8C0:D2F0 0201A8C0:01BB 01 00000000:00000000 00:00000000 00000000  1000 0 222 1\n"
            "   2: 0100007F:9BCE 0100007F:0050 06 00000000:00000000 00:00000000 00000000  1000 0 0 1\n"
            "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 00000000000000000000000001000000:1F90 00000000000000000000000000000000:0000 0A 0:0 0:0 0 1000 0 333 1\n"
            "   1: 0000000000000000FFFF00000100007F:1F91 00000000000000000000000000000000:0000 0A 0:0 0:0 0 1000 0 444 1\n"
            "   2: 00000000000000000000000000000000:1F92 00000000000000000000000000000000:0000 0A 0:0 0:0 0 1000 0 555 1\n")
        t = parse_net_tcp(text)
        self.assertEqual(t["111"], ("0A", True))
        self.assertEqual(t["222"], ("01", False))
        self.assertNotIn("0", t)
        self.assertEqual(t["333"], ("0A", True))
        self.assertEqual(t["444"], ("0A", True))
        self.assertEqual(t["555"], ("0A", False))


def orphans(t, label="machine-b", n=28):
    t.setup("cursor", "3.23.23-1", "3.24.9-1")
    return t.sys[label].add_sandbox_orphans(n)


class CloseHelpers(unittest.TestCase):
    def test_28_orphans_are_cleared_and_update_runs_without_launching_cursor(self):
        t = Twin()
        pids = orphans(t)
        t0 = t.clock.t
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        self.assertEqual(rep["result"], "ok", rep.get("error"))
        s = t.sys["machine-b"]
        self.assertTrue(all(p not in s.proc_table for p in pids))
        self.assertEqual(sorted(p for p, _ in s.term_calls), pids)   # one SIGTERM each
        self.assertEqual(s.pkgs["cursor"], "3.24.9-1")
        self.assertNotIn("machine-b", rep.get("reopen", {}))          # was not open
        h = rep["close"]["wait"]["machine-b"]["cursor"]["helpers"]
        self.assertEqual(h["status"], helpers.HELPERS_CLEARED)
        self.assertEqual(h["counts"][ORPHANED_IDLE], 28)
        self.assertLess(t.clock.t - t0, 60)

    def test_grok_bot_label_sends_nothing_and_says_how_to_fix(self):
        t = Twin()
        pids = orphans(t)
        t.sys["machine-b"].self_label = "grok-bot (unconfined)"
        t0 = t.clock.t
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        self.assertEqual(rep["result"], "aborted")
        self.assertIn("HELPERS_NEED_USER_SESSION", rep["error"])
        self.assertIn("twin-update close-helpers", rep["error"])
        self.assertIn("log out", rep["error"])
        s = t.sys["machine-b"]
        self.assertEqual(s.term_calls, [])
        self.assertTrue(all(p in s.proc_table for p in pids))
        self.assertEqual(s.pkgs["cursor"], "3.23.23-1")
        self.assertLess(t.clock.t - t0, 30)                           # no 90s wait-out

    def test_apparmor_denial_is_reported_once_with_no_retry(self):
        t = Twin()
        pids = orphans(t)
        s = t.sys["machine-b"]
        s.denied.update(pids)
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        self.assertEqual(rep["result"], "aborted")
        self.assertIn("HELPERS_STILL_RUNNING", rep["error"])
        self.assertIn("journalctl -k", rep["error"])
        self.assertEqual(sorted(p for p, _ in s.term_calls), pids)   # exactly one try each
        self.assertFalse(any(e[1] == "KILL" for e in t.events))

    def test_survivor_stops_and_reports_without_sigkill(self):
        t = Twin()
        pids = orphans(t, n=4)
        s = t.sys["machine-b"]
        s.stubborn.add(pids[0])
        e = t.eng["machine-b"]
        rep = e.close_helpers("cursor")
        self.assertEqual(rep["status"], helpers.HELPERS_STILL_RUNNING)
        self.assertEqual(rep["survivors"], [pids[0]])
        self.assertEqual(len(s.term_calls), 4)
        self.assertFalse(any(ev[1] == "KILL" for ev in t.events))

    def test_pid_reused_between_checks_is_skipped(self):
        t = Twin()
        pids = orphans(t, n=2)
        s = t.sys["machine-b"]
        # Reads 1-2 are the first check; read 3 (second check) sees a new process.
        s.reuse_at[pids[0]] = (3, 999)
        rep = t.eng["machine-b"].close_helpers("cursor")
        self.assertNotIn(pids[0], [p for p, _ in s.term_calls])
        self.assertIn(pids[0], s.proc_table)
        self.assertEqual(rep["status"], helpers.HELPERS_ACTIVE)

    def test_pid_reused_during_cpu_sample_is_unknown(self):
        t = Twin()
        pids = orphans(t, n=1)
        t.sys["machine-b"].reuse_at[pids[0]] = (2, 999)
        rep = t.eng["machine-b"].close_helpers("cursor")
        self.assertEqual(rep["counts"][UNKNOWN], 1)
        self.assertEqual(t.sys["machine-b"].term_calls, [])

    def test_one_active_helper_is_left_and_blocks_the_update(self):
        t = Twin()
        pids = orphans(t, n=6)
        s = t.sys["machine-b"]
        s.cpu_growth[pids[3]] = 2
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        self.assertEqual(rep["result"], "aborted")
        self.assertIn("HELPERS_ACTIVE", rep["error"])
        self.assertEqual([p for p in pids if p in s.proc_table], [pids[3]])
        self.assertNotIn(pids[3], [p for p, _ in s.term_calls])
        self.assertEqual(s.pkgs["cursor"], "3.23.23-1")

    def test_unreadable_helper_is_unknown_and_not_signalled(self):
        t = Twin()
        pids = orphans(t, n=3)
        s = t.sys["machine-b"]
        s.unreadable.add(pids[1])
        rep = t.eng["machine-b"].close_helpers("cursor")
        self.assertEqual(rep["counts"][UNKNOWN], 1)
        self.assertEqual(rep["status"], helpers.HELPERS_ACTIVE)
        self.assertNotIn(pids[1], [p for p, _ in s.term_calls])

    def test_main_running_closes_first_then_helpers(self):
        t = Twin()
        pids = orphans(t, label="machine-a", n=4)
        t.setup("cursor", "3.23.23-1", "3.24.9-1")
        t.sys["machine-a"].add_app_procs("/usr/share/cursor/cursor")
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        self.assertEqual(rep["result"], "ok", rep.get("error"))
        s = t.sys["machine-a"]
        self.assertTrue(all(p not in s.proc_table for p in pids))
        self.assertIn("machine-a", rep["reopen"])                      # Cursor itself was open

    def test_classifier_sees_main_present(self):
        t = Twin()
        pids = orphans(t, n=2)
        s = t.sys["machine-b"]
        s.proc_table[50] = Proc(50, 1, "/usr/share/cursor/cursor", "cursor", 10)
        rep = t.eng["machine-b"].close_helpers("cursor")
        self.assertEqual(rep["counts"][ACTIVE], 2)
        self.assertTrue(all("main app running" in r["reasons"] for r in rep["helpers"]))
        self.assertEqual(s.term_calls, [])

    def test_dry_run_reports_without_signals(self):
        t = Twin()
        orphans(t)
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=True)
        s = t.sys["machine-b"]
        self.assertEqual(s.term_calls, [])
        info = rep["close"]["start"]["machine-b"]["cursor"]
        self.assertFalse(info["was_open"])
        self.assertEqual(info["helpers"]["status"], helpers.HELPERS_WOULD_CLOSE)

    def test_no_cmdline_or_policy_in_reports_or_logs(self):
        t = Twin()
        orphans(t)
        t.sys["machine-b"].self_label = "grok-bot (unconfined)"
        rep = orchestrator.run(t.session(FakeUI()), ["cursor"], dry=False)
        blob = json.dumps(rep) + "\n".join(t.logs)
        self.assertNotIn("SECRET-TOKEN", blob)
        self.assertNotIn("policy-json", blob)
        self.assertIn("run-proxy", json.dumps(t.eng["machine-b"].close_helpers("cursor", dry=True)))

    def test_non_cursor_apps_unchanged(self):
        self.assertNotIn("chatgpt", helpers.SPECS)
        self.assertEqual(Twin().eng["machine-a"].close_helpers("chatgpt")["status"],
                         helpers.HELPERS_NONE)


class CloseHelpersCli(unittest.TestCase):
    def _cfg(self, t):
        fh = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fh.write(CONFIG.format(keep="[]", fallback="[]"))
        fh.close()
        self.addCleanup(os.remove, fh.name)
        return fh.name

    def test_cli_closes_idle_helpers(self):
        t = Twin()
        orphans(t, label="machine-a", n=4)
        ui = FakeUI()
        rc = cli.main(["--config", self._cfg(t), "close-helpers"], ui=ui, system=t.sys["machine-a"])
        self.assertEqual(rc, 0, ui.lines)
        self.assertTrue(any("now closed" in l for l in ui.lines))

    def test_cli_need_session_exit_1(self):
        t = Twin()
        orphans(t, label="machine-a", n=2)
        t.sys["machine-a"].self_label = "grok-bot (unconfined)"
        ui = FakeUI()
        rc = cli.main(["--config", self._cfg(t), "close-helpers"], ui=ui, system=t.sys["machine-a"])
        self.assertEqual(rc, 1)
        self.assertTrue(any("own terminal" in l for l in ui.lines))
        self.assertEqual(t.sys["machine-a"].term_calls, [])


class RealSystemSignals(unittest.TestCase):
    """System.term_result: SIGTERM only, EPERM reported as denied, never SIGKILL."""

    def _run(self, send_side_effect=None):
        sent = []

        def fake_send(fd, sig, *a, **kw):
            sent.append(sig)
            if send_side_effect:
                raise send_side_effect

        sysobj = System()
        with mock.patch("os.pidfd_open", return_value=99), \
             mock.patch("os.close"), \
             mock.patch.object(System, "pid_start", return_value=555), \
             mock.patch("signal.pidfd_send_signal", side_effect=fake_send), \
             mock.patch("os.kill") as kill:
            res = sysobj.term_result(1234, 555)
        self.assertFalse(any(c.args[1:2] == (signal.SIGKILL,) for c in kill.call_args_list))
        return res, sent

    def test_sent_is_sigterm(self):
        res, sent = self._run()
        self.assertEqual((res, sent), ("sent", [signal.SIGTERM]))

    def test_eperm_is_denied_once(self):
        res, sent = self._run(PermissionError(1, "Operation not permitted"))
        self.assertEqual((res, sent), ("denied", [signal.SIGTERM]))

    def test_reused_pid_not_signalled(self):
        with mock.patch("os.pidfd_open", return_value=99), mock.patch("os.close"), \
             mock.patch.object(System, "pid_start", return_value=1), \
             mock.patch("signal.pidfd_send_signal") as send:
            self.assertEqual(System().term_result(1234, 555), "reused")
        send.assert_not_called()

    def test_no_sigkill_anywhere_in_helper_code(self):
        import inspect
        from twin_update import engine
        for mod in (helpers, engine):
            self.assertNotIn("SIGKILL", inspect.getsource(mod).replace("never SIGKILL", "")
                             .replace("Never SIGKILL", ""))


class ConfigKeys(unittest.TestCase):
    def test_close_table(self):
        from twin_update.config import ConfigError, load
        base = CONFIG.format(keep="[]", fallback="[]")
        cfg = load(text=base + "\n[close]\nhelper_sample_s = 1\nhelper_min_age_min = 5\n", home="/h")
        self.assertEqual((cfg.helper_sample_s, cfg.helper_min_age_min, cfg.helper_wait_s),
                         (1.0, 5.0, 10.0))
        for bad in ("helper_kill = true", "helper_wait_s = -1", "helper_sample_s = \"x\""):
            with self.assertRaises(ConfigError):
                load(text=base + "\n[close]\n" + bad + "\n", home="/h")


if __name__ == "__main__":
    unittest.main()
