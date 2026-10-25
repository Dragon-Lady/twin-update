import os
import unittest

from fakes import CONFIG, FakeUI, Twin
from test_twin_update import Check, standard
from twin_update import notice


class Launcher(unittest.TestCase):
    def test_prefers_cosmic_term(self):
        which = lambda n: "/usr/bin/" + n if n == "cosmic-term" else None
        argv = notice.launcher_argv(which, ["twin-update", "prompt"])
        self.assertEqual(argv[:3], ["cosmic-term", "-e", "bash"])
        self.assertIn("twin-update prompt", argv[-1])

    def test_falls_back_to_x_terminal_emulator(self):
        which = lambda n: "/usr/bin/" + n if n == "x-terminal-emulator" else None
        argv = notice.launcher_argv(which, ["twin-update", "prompt"])
        self.assertEqual(argv[0], "x-terminal-emulator")

    def test_none_when_no_launcher(self):
        self.assertIsNone(notice.launcher_argv(lambda n: None, ["twin-update"]))


class GraphicalEnv(unittest.TestCase):
    def test_reads_session_environment(self):
        parsed = notice.parse_session_environment(
            "WAYLAND_DISPLAY=wayland-1\nDISPLAY=:0\nHOME=/home/user\n")
        self.assertEqual(parsed["WAYLAND_DISPLAY"], "wayland-1")
        self.assertEqual(parsed["DISPLAY"], ":0")
        self.assertNotIn("HOME", parsed)

    def test_from_systemctl_show_environment(self):
        t = Twin(); s = t.sys["machine-a"]
        env = notice.graphical_env(s)
        self.assertIsNotNone(env)
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")

    def test_none_without_a_graphical_session(self):
        t = Twin(); s = t.sys["machine-a"]
        s.no_graphical = True
        self.assertIsNone(notice.graphical_env(s))


class WindowLock(unittest.TestCase):
    def test_second_launch_is_blocked_while_pid_is_alive(self):
        t = Twin(); s = t.sys["machine-a"]
        path = notice.window_lock_path(t.cfg.state_dir)
        first = notice.try_begin_window(s, path)
        self.assertIsNotNone(first)
        s.unlock(first)
        notice.write_window_pid(s, path, 4000, started=True)
        s.alive_pids.add(4000)
        self.assertIsNone(notice.try_begin_window(s, path))
        s.alive_pids.discard(4000)
        self.assertIsNotNone(notice.try_begin_window(s, path))

    def test_flock_blocks_a_race(self):
        t = Twin(); s = t.sys["machine-a"]
        s.window_flock = True
        path = notice.window_lock_path(t.cfg.state_dir)
        self.assertIsNone(notice.try_begin_window(s, path))


class PromptAnswers(unittest.TestCase):
    def _items(self):
        return [{"display": "Cursor", "installed": "1", "candidate": "2",
                 "where": "both machines"}]

    def test_already_updated_guard(self):
        ui = FakeUI()
        ran = []
        ans = notice.run_prompt(ui, [], run_fn=lambda: ran.append("run"))
        self.assertEqual(ans, "already-updated")
        self.assertIn(notice.ALREADY_UPDATED, ui.lines)
        self.assertEqual(ran, [])

    def test_yes_runs(self):
        ui = FakeUI(confirm=True)
        ran = []
        ans = notice.run_prompt(ui, self._items(), run_fn=lambda: ran.append("run"))
        self.assertEqual(ans, "run")
        self.assertEqual(ran, ["run"])
        self.assertTrue(any("BOTH machines" in l for l in ui.lines))
        self.assertTrue(any("Press Enter" in l for l in ui.lines))

    def test_no_does_nothing(self):
        ui = FakeUI(confirm=False)
        ran = []
        self.assertEqual(notice.run_prompt(ui, self._items(), run_fn=lambda: ran.append("x")),
                         "no")
        self.assertEqual(ran, [])

    def test_eof_does_nothing(self):
        ui = FakeUI(eof=True)
        ran = []
        self.assertEqual(notice.run_prompt(ui, self._items(), run_fn=lambda: ran.append("x")),
                         "eof")
        self.assertEqual(ran, [])

    def test_test_mode_yes_does_not_run(self):
        ui = FakeUI(confirm=True)
        ran = []
        ans = notice.run_prompt(ui, notice.TEST_PENDING, test=True,
                                run_fn=lambda: ran.append("x"))
        self.assertEqual(ans, "run")
        self.assertEqual(ran, [])
        self.assertIn(notice.TEST_ONLY, ui.lines)


class RecordAfterLaunch(unittest.TestCase):
    def test_record_only_after_successful_handover(self):
        t = Twin(); e = t.eng["machine-a"]
        self.assertTrue(e.notice_due("updates", "sig"))
        self.assertTrue(e.notice_due("updates", "sig"))  # still read-only
        e.record_notice("updates", "sig")
        self.assertFalse(e.notice_due("updates", "sig"))

    def test_repeat_next_day_while_still_pending(self):
        t = Twin(); e = t.eng["machine-a"]
        e.record_notice("updates", "same-sig")
        self.assertFalse(e.notice_due("updates", "same-sig"))
        t.clock.t += 86400
        self.assertTrue(e.notice_due("updates", "same-sig"))

    def test_dead_window_is_not_recorded(self):
        t = Twin(); standard(t)
        t.sys["machine-a"].spawn_dies_immediately = True
        rc, out = Check._cli(Check(), t, ["--notify"])
        self.assertEqual(rc, 10)
        self.assertIn("window exited immediately; not recorded", out)
        self.assertTrue(t.eng["machine-a"].notice_due("updates", "x"))

    def test_no_graphical_session_is_not_recorded(self):
        t = Twin(); standard(t)
        t.sys["machine-a"].no_graphical = True
        rc, out = Check._cli(Check(), t, ["--notify"])
        self.assertEqual(rc, 10)
        self.assertIn("no graphical session; will retry at next login", out)
        self.assertEqual(t.sys["machine-a"].spawned, [])
        self.assertTrue(t.eng["machine-a"].notice_due("updates", "x"))


class PromptCli(unittest.TestCase):
    def _cli(self, t, args):
        return Check._cli(Check(), t, args) if False else self._run(t, args)

    def _run(self, t, args):
        import contextlib
        import io
        import tempfile
        from fakes import patched_paths
        from twin_update import cli
        tmpd = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)
        cfgp = os.path.join(tmpd, "c.toml")
        with open(cfgp, "w") as fh:
            fh.write(CONFIG.format(keep="[]", fallback="[]"))
        buf = io.StringIO()
        ui = FakeUI(confirm=False)
        with contextlib.redirect_stdout(buf), patched_paths(t):
            rc = cli.main(["--config", cfgp, *args], ui=ui, system=t.sys["machine-a"],
                          peer_factory=lambda label, target, cmd: t.peers[label])
        return rc, buf.getvalue() + "\n".join(ui.lines), ui

    def test_prompt_test_opens_a_window(self):
        t = Twin(); standard(t)
        rc, out, _ = self._run(t, ["prompt", "--test"])
        self.assertEqual(rc, 0)
        self.assertIn("opened a window", out)
        self.assertEqual(t.sys["machine-a"].spawned[0][0][0], "cosmic-term")
        inner = t.sys["machine-a"].spawned[0][0][-1]
        self.assertIn("--test", inner)
        self.assertIn("--inside", inner)
        self.assertEqual(t.eng["machine-a"].notice_due("updates", ""), True)

    def test_prompt_inside_test_prints_answer(self):
        t = Twin()
        rc, out, ui = self._run(t, ["prompt", "--test", "--inside"])
        self.assertEqual(rc, 0)
        self.assertIn("answer: no", out)
        self.assertIn(notice.TEST_ONLY, ui.lines)


class DoctorNotice(unittest.TestCase):
    def test_doctor_reports_terminal_and_timer(self):
        t = Twin(); standard(t)
        d = t.eng["machine-a"].doctor()
        self.assertEqual(d["terminal_launcher"], "cosmic-term")
        self.assertTrue(d["check_timer"])
        self.assertIn("09:13", d["check_timer_next"] or "")
        self.assertEqual(d["last_window"], "none")
        self.assertTrue(d["graphical_session"])

    def test_missing_terminal_is_a_problem(self):
        from twin_update.orchestrator import doctor_findings
        t = Twin()
        t.sys["machine-a"].missing_tools = {"cosmic-term", "x-terminal-emulator"}
        d = t.eng["machine-a"].doctor()
        problems, _ = doctor_findings(d)
        self.assertIn("terminal_launcher", problems)
