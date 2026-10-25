import os
import unittest

from fakes import CONFIG, FakeUI, Twin
from test_twin_update import Check, standard
from twin_update import notice


class Launcher(unittest.TestCase):
    def test_prefers_cosmic_term(self):
        which = lambda n: "/usr/bin/" + n if n == "cosmic-term" else None
        argv = notice.launcher_argv(which, ["twin-update", "prompt"])
        self.assertEqual(argv[:4], ["cosmic-term", "--no-daemon", "-e", "bash"])
        self.assertLess(argv.index("--no-daemon"), argv.index("-e"))
        self.assertIn("twin-update prompt", argv[-1])

    def test_falls_back_to_x_terminal_emulator(self):
        which = lambda n: "/usr/bin/" + n if n == "x-terminal-emulator" else None
        argv = notice.launcher_argv(which, ["twin-update", "prompt"])
        self.assertEqual(argv[0], "x-terminal-emulator")
        self.assertNotIn("--no-daemon", argv)
        self.assertEqual(argv[1], "-e")

    def test_gnome_terminal_uses_double_dash(self):
        which = lambda n: "/usr/bin/" + n if n == "x-terminal-emulator" else None
        argv = notice.launcher_argv(which, ["twin-update", "prompt"],
                                    realpath=lambda p: "/usr/bin/gnome-terminal")
        self.assertEqual(argv[:4], ["x-terminal-emulator", "--wait", "--", "bash"])
        self.assertNotIn("-e", argv)

    def test_terminal_flags_table(self):
        expected = {
            "cosmic-term": ["--no-daemon", "-e"],
            "gnome-terminal": ["--wait", "--"],
            "gnome-terminal.wrapper": ["--wait", "--"],
            "kgx": ["--"],
            "ptyxis": ["--"],
            "xfce4-terminal": ["-x"],
            "konsole": ["-e"],
            "xterm": ["-e"],
            "x-terminal-emulator": ["-e"],
        }
        for name, flags in expected.items():
            self.assertEqual(notice.terminal_flags(name), flags)

    def test_none_when_no_launcher(self):
        self.assertIsNone(notice.launcher_argv(lambda n: None, ["twin-update"]))

    def test_mainpid_zero_then_pid_is_success(self):
        t = Twin(); s = t.sys["machine-a"]
        s.mainpid_zero_first = True
        env = notice.graphical_env(s)
        argv = notice.launcher_argv(s.which, ["twin-update", "prompt"])
        pid, unit = notice.start_prompt_window(s, argv, env)
        self.assertIsNotNone(pid)
        self.assertGreater(pid, 0)
        self.assertTrue(unit)

    def test_daemonized_launcher_is_not_a_successful_window(self):
        """Main pid exits at once while a child window pid lives; that is not success."""
        t = Twin(); s = t.sys["machine-a"]
        s.spawn_dies_immediately = True
        env = notice.graphical_env(s)
        argv = notice.launcher_argv(s.which, ["twin-update", "prompt"])
        self.assertEqual(argv[0], "cosmic-term")
        self.assertLess(argv.index("--no-daemon"), argv.index("-e"))
        pid, _unit = notice.start_prompt_window(s, argv, env)
        self.assertIsNotNone(pid)
        child = pid + 1
        s.alive_pids.add(child)  # the forked window, not the unit main pid
        path = notice.window_lock_path(t.cfg.state_dir)
        notice.write_window_pid(s, path, pid, started=False)
        self.assertFalse(s.pid_alive(pid))
        self.assertTrue(s.pid_alive(child))
        self.assertFalse(notice.verify_window(s, path, pid))
        self.assertTrue(t.eng["machine-a"].notice_due("updates", "x"))


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

    def test_inplace_write_keeps_flock(self):
        t = Twin(); s = t.sys["machine-a"]
        path = notice.window_lock_path(t.cfg.state_dir)
        first = notice.try_begin_window(s, path)
        self.assertIsNotNone(first)
        notice.write_window_pid(s, path, 4001, started=False)
        self.assertIsNone(notice.try_begin_window(s, path))
        s.unlock(first)
        s.alive_pids.discard(4001)
        self.assertIsNotNone(notice.try_begin_window(s, path))

    def test_dead_pid_does_not_block(self):
        t = Twin(); s = t.sys["machine-a"]
        path = notice.window_lock_path(t.cfg.state_dir)
        notice.write_window_pid(s, path, 4242, started=True)
        self.assertFalse(s.pid_alive(4242))
        self.assertIsNotNone(notice.try_begin_window(s, path))

    def test_real_flock_survives_inplace_write(self):
        import tempfile
        from twin_update.system import System
        s = System()
        tmpd = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)
        path = os.path.join(tmpd, "prompt.window")
        fd = notice.try_begin_window(s, path)
        self.assertIsNotNone(fd)
        notice.write_window_pid(s, path, 1, started=False)
        self.assertIsNone(s.try_lockfile(path))
        s.unlock(fd)
        second = s.try_lockfile(path)
        self.assertIsNotNone(second)
        s.unlock(second)


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
        self.assertTrue(any("Press Enter" in l for l in ui.lines))

    def test_check_error_shows_real_text_and_pauses(self):
        ui = FakeUI()
        ran = []
        ans = notice.run_prompt(ui, [], errors={"laptop": "timed out: the machine may be off"},
                                run_fn=lambda: ran.append("run"))
        self.assertEqual(ans, "error")
        self.assertTrue(any("laptop: timed out" in l for l in ui.lines))
        self.assertNotIn(notice.ALREADY_UPDATED, ui.lines)
        self.assertEqual(ran, [])
        self.assertTrue(any("Press Enter" in l for l in ui.lines))

    def test_unreachable_status_in_question(self):
        ui = FakeUI(confirm=False)
        items = [{"display": "Cursor", "jump": "1\u21922", "where": "machine-a",
                  "machines": ["machine-a"]}]
        notice.run_prompt(ui, items, errors={"machine-b": "timed out: asleep"})
        self.assertTrue(any("machine-b: unreachable" in p for p in ui.prompts))
        self.assertTrue(any("Update now?" in p for p in ui.prompts))

    def test_record_after_question_shown(self):
        ui = FakeUI(confirm=False)
        shown = []
        notice.run_prompt(ui, self._items(), on_shown=lambda: shown.append(1))
        self.assertEqual(shown, [1])

    def test_no_record_when_nothing_pending(self):
        ui = FakeUI()
        shown = []
        notice.run_prompt(ui, [], on_shown=lambda: shown.append(1))
        self.assertEqual(shown, [])

    def test_no_record_when_only_errors(self):
        ui = FakeUI()
        shown = []
        notice.run_prompt(ui, [], errors={"x": "down"}, on_shown=lambda: shown.append(1))
        self.assertEqual(shown, [])

    def test_yes_runs(self):
        ui = FakeUI(confirm=True)
        ran = []
        ans = notice.run_prompt(ui, self._items(), run_fn=lambda: ran.append("run"))
        self.assertEqual(ans, "run")
        self.assertEqual(ran, ["run"])
        self.assertTrue(any("both machines" in l for l in ui.lines))
        self.assertTrue(any("Press Enter" in l for l in ui.lines))
        self.assertIn(notice.CLOSE_DOES_NOT_STOP, ui.lines)

    def test_version_jumps_joined_like_check(self):
        items = notice.items_from_check(
            {"updates": [{"app": "cursor",
                          "machines": {"machine-a": {"installed": "1", "candidate": "2"},
                                       "machine-b": {"installed": "1.1", "candidate": "2"}}}]},
            type("C", (), {"__contains__": lambda self, k: False})())
        self.assertEqual(items[0]["jump"], "1.1\u21922 / 1\u21922")
        self.assertEqual(items[0]["where"], "both machines")
        lines = notice.pending_lines(items)
        self.assertIn("1.1\u21922 / 1\u21922", lines[0])

    def test_warning_lists_closures_per_machine(self):
        items = [{"display": "Cursor", "where": "machine-a", "machines": ["machine-a"]}]
        line = notice.warning_line(items)
        self.assertIn("machine-a", line)
        self.assertIn("Cursor", line)
        self.assertNotIn("both machines", line)

    def test_warning_lists_one_machine_app_with_both(self):
        items = [
            {"display": "Cursor", "machines": ["machine-a"]},
            {"display": "Grok Bot", "machines": ["machine-a", "machine-b"],
             "where": "both machines"},
        ]
        line = notice.warning_line(items)
        self.assertIn("Cursor", line)
        self.assertIn("Grok Bot", line)
        self.assertIn("machine-a", line)
        self.assertIn("machine-b", line)

    def test_no_does_nothing(self):
        ui = FakeUI(confirm=False)
        ran = []
        self.assertEqual(notice.run_prompt(ui, self._items(), run_fn=lambda: ran.append("x")),
                         "no")
        self.assertEqual(ran, [])
        self.assertFalse(any("Press Enter" in l for l in ui.lines))

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
        self.assertEqual(ans, "test")
        self.assertEqual(ran, [])
        self.assertIn(notice.TEST_ONLY, ui.lines)
        self.assertIn(notice.TEST_NOTHING_RAN, ui.lines)
        self.assertTrue(any("Press Enter" in l for l in ui.lines))


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
        self.assertIn("no graphical session; not retried until the next timer run", out)
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
        launched = t.sys["machine-a"].spawned[0][0]
        self.assertEqual(launched[0], "cosmic-term")
        self.assertLess(launched.index("--no-daemon"), launched.index("-e"))
        inner = launched[-1]
        self.assertIn("--test", inner)
        self.assertIn("--inside", inner)
        self.assertIn("prompt.window.test", inner)
        self.assertNotIn("prompt.window ", inner + " ")
        self.assertEqual(t.eng["machine-a"].notice_due("updates", ""), True)

    def test_prompt_inside_test_prints_answer(self):
        t = Twin()
        rc, out, ui = self._run(t, ["prompt", "--test", "--inside"])
        self.assertEqual(rc, 0)
        self.assertIn("answer: no", out)
        self.assertIn(notice.TEST_ONLY, ui.lines)

    def test_prompt_inside_test_yes_says_nothing_ran(self):
        import contextlib
        import io
        import tempfile
        from fakes import patched_paths
        from twin_update import cli
        t = Twin()
        tmpd = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)
        cfgp = os.path.join(tmpd, "c.toml")
        with open(cfgp, "w") as fh:
            fh.write(CONFIG.format(keep="[]", fallback="[]"))
        ui = FakeUI(confirm=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), patched_paths(t):
            rc = cli.main(["--config", cfgp, "prompt", "--test", "--inside"],
                          ui=ui, system=t.sys["machine-a"])
        out = buf.getvalue() + "\n".join(ui.lines)
        self.assertEqual(rc, 0)
        self.assertIn(notice.TEST_NOTHING_RAN, out)
        self.assertNotIn("answer: run", out)
        self.assertTrue(any("Press Enter" in l for l in ui.lines))

    def test_prompt_inside_error_shows_text_and_pauses(self):
        from twin_update.peer import PeerError
        t = Twin()
        t.setup("cursor", "3.24.9-1", "3.24.9-1")
        t.setup("chatgpt", "26.1007.21434", "26.1007.21434")
        t.setup("grok-bot", "0.68.1", "0.68.1")

        def down(label, target, cmd):
            raise PeerError(f"{label}: timed out: the machine may be off, asleep or offline")

        rc, out, ui = self._run_factory(t, ["prompt", "--inside"], down)
        self.assertEqual(rc, 1)
        self.assertIn("Checking...", out)
        self.assertIn("timed out", out)
        self.assertNotIn(notice.ALREADY_UPDATED, out)
        self.assertTrue(any("Press Enter" in l for l in ui.lines))

    def test_prompt_inside_records_only_after_question(self):
        t = Twin(); standard(t)
        rc, out, ui = self._run(t, ["prompt", "--inside"])
        self.assertEqual(rc, 0)
        self.assertIn("Checking...", out)
        self.assertIn("Update now?", " ".join(ui.prompts))
        self.assertFalse(t.eng["machine-a"].notice_due("updates", "x"))

    def test_launch_does_not_record_the_day(self):
        t = Twin(); standard(t)
        rc, out = Check._cli(Check(), t, ["--notify"])
        self.assertEqual(rc, 10)
        self.assertIn("opened a window", out)
        self.assertTrue(t.eng["machine-a"].notice_due("updates", "x"))

    def _run_factory(self, t, args, factory):
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
                          peer_factory=factory)
        return rc, buf.getvalue() + "\n".join(ui.lines), ui


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

    def test_stale_real_window_is_a_warning(self):
        from twin_update.orchestrator import doctor_findings
        t = Twin(); standard(t)
        s = t.sys["machine-a"]
        path = notice.window_lock_path(t.cfg.state_dir, test=False)
        notice.write_window_pid(s, path, 4000, started=True, opened_at=s.now() - 90000)
        s.alive_pids.add(4000)
        d = t.eng["machine-a"].doctor()
        self.assertTrue(d["window_stale"])
        _, warnings = doctor_findings(d)
        self.assertTrue(any("over 24 hours" in w for w in warnings))

    def test_unit_missing_success_exit_is_a_warning(self):
        from twin_update.orchestrator import doctor_findings
        t = Twin(); standard(t)
        home = t.sys["machine-a"].home()
        unit_dir = os.path.join(home, ".config/systemd/user")
        os.makedirs(unit_dir, exist_ok=True)
        with open(os.path.join(unit_dir, "twin-update-check.service"), "w") as fh:
            fh.write("[Service]\nType=oneshot\nExecStart=/bin/twin-update check --notify\n")
        with open(os.path.join(unit_dir, "twin-update-check.timer"), "w") as fh:
            fh.write("[Timer]\nOnCalendar=*-*-* 09:13:00\n")
        d = t.eng["machine-a"].doctor()
        _, warnings = doctor_findings(d)
        self.assertTrue(any("SuccessExitStatus=10" in w for w in warnings))
        self.assertTrue(any("Persistent=true" in w for w in warnings))


class TestSlotDoesNotBlockReal(unittest.TestCase):
    def test_live_test_slot_does_not_block_notify(self):
        t = Twin(); standard(t)
        s = t.sys["machine-a"]
        test_slot = notice.window_lock_path(t.cfg.state_dir, test=True)
        notice.write_window_pid(s, test_slot, 4111, started=True)
        s.alive_pids.add(4111)
        rc, out = Check._cli(Check(), t, ["--notify"])
        self.assertEqual(rc, 10)
        self.assertIn("opened a window", out)
        inner = s.spawned[0][0][-1]
        self.assertNotIn("prompt.window.test", inner)

    def test_already_open_says_since_when(self):
        t = Twin(); standard(t)
        s = t.sys["machine-a"]
        slot = notice.window_lock_path(t.cfg.state_dir, test=False)
        notice.write_window_pid(s, slot, 4222, started=True, opened_at=s.now())
        s.alive_pids.add(4222)
        rc, out = Check._cli(Check(), t, ["--notify"])
        self.assertEqual(rc, 10)
        self.assertIn("window already open since", out)


class DetachedRun(unittest.TestCase):
    def test_yes_starts_a_run_unit(self):
        import contextlib
        import io
        import tempfile
        from fakes import patched_paths
        from twin_update import cli
        t = Twin(); standard(t)
        tmpd = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)
        cfgp = os.path.join(tmpd, "c.toml")
        with open(cfgp, "w") as fh:
            fh.write(CONFIG.format(keep="[]", fallback="[]"))
        ui = FakeUI(confirm=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), patched_paths(t):
            rc = cli.main(["--config", cfgp, "prompt", "--inside"],
                          ui=ui, system=t.sys["machine-a"],
                          peer_factory=lambda label, target, cmd: t.peers[label])
        out = buf.getvalue() + "\n".join(ui.lines)
        self.assertIn(notice.CHECKING, ui.lines)
        self.assertIn(notice.CLOSE_DOES_NOT_STOP, ui.lines)
        self.assertTrue(t.sys["machine-a"].run_units)
        unit, inner = t.sys["machine-a"].run_units[0]
        self.assertTrue(unit.startswith("twin-update-run-"))
        self.assertIn("run", inner)
        self.assertIn("--no-detach", inner)
        self.assertNotIn("--sudo-from-stdin", inner)
        started = [c for c in t.sys["machine-a"].calls if c[0][:1] == ["systemd-run"]
                   and any(a.startswith("--unit=twin-update-run-") for a in c[0])]
        self.assertTrue(started)
        argv, _stdin, _env = started[0]
        self.assertIn("--scope", argv)
        self.assertNotIn("--pipe", argv)
        self.assertNotIn("--collect", argv)
        self.assertNotIn("c0rrect-Horse!pw", " ".join(argv))
        self.assertIn(notice.UPDATE_FINISHED, ui.lines)
        self.assertTrue(any("systemctl --user stop" in l for l in ui.lines))
        self.assertEqual(ui.lines.count(notice.CLOSE_DOES_NOT_STOP), 1)
        self.assertEqual(rc, 0)
        self.assertIn("Checking...", out)


class FollowUnit(unittest.TestCase):
    def _follow(self, script):
        t = Twin()
        s = t.sys["machine-a"]
        s.follow_script = script
        s.follow_idx = 0
        ui = FakeUI()
        rc = notice.follow_unit(s, "twin-update-run-1", {}, ui, poll_s=0)
        return rc, ui, s

    def test_failed_unit_is_not_success(self):
        """Old fake always returned Result=success; that hid a failed run."""
        rc, ui, s = self._follow([
            {"active": "activating",
             "show": "Result=\nExecMainStatus=0\nActiveState=activating\nLoadState=loaded\n"},
            {"active": "inactive",
             "show": "Result=exit-code\nExecMainStatus=3\nActiveState=failed\nLoadState=loaded\n"},
        ])
        self.assertEqual(rc, 3)
        self.assertTrue(any(notice.UPDATE_FAILED in l for l in ui.lines))
        self.assertTrue(any("exit 3" in l for l in ui.lines))
        self.assertTrue(s.cleaned_units)

    def test_unloaded_failed_unit_is_not_success(self):
        rc, ui, s = self._follow([
            {"active": "inactive", "show": "", "rc": 0},
        ])
        self.assertNotEqual(rc, 0)
        self.assertTrue(any(notice.UPDATE_FAILED in l for l in ui.lines))
        self.assertTrue(s.cleaned_units)

    def test_early_inactive_then_activating_failure(self):
        """A queued unit looks inactive first; the old follow ended there as success."""
        rc, ui, _s = self._follow([
            {"active": "inactive",
             "show": "Result=\nExecMainStatus=0\nActiveState=inactive\nLoadState=loaded\n"},
            {"active": "inactive",
             "show": "Result=\nExecMainStatus=0\nActiveState=inactive\nLoadState=loaded\n"},
            {"active": "activating",
             "show": "Result=\nExecMainStatus=0\nActiveState=activating\nLoadState=loaded\n"},
            {"active": "inactive",
             "show": "Result=exit-code\nExecMainStatus=7\nActiveState=failed\nLoadState=loaded\n"},
        ])
        self.assertEqual(rc, 7)
        self.assertTrue(any(notice.UPDATE_FAILED in l for l in ui.lines))


class PasswordHandoff(unittest.TestCase):
    def test_console_getpass_no_tty_raises(self):
        from unittest import mock
        from twin_update import cli
        ui = cli.ConsoleUI()
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(EOFError) as ctx:
                ui.getpass("sudo password: ")
        self.assertIn("sudo password needed", str(ctx.exception))

    def test_main_catches_eof_without_traceback(self):
        import contextlib
        import io
        import tempfile
        from fakes import patched_paths
        from twin_update import cli
        t = Twin(); standard(t)
        tmpd = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)
        cfgp = os.path.join(tmpd, "c.toml")
        with open(cfgp, "w") as fh:
            fh.write(CONFIG.format(keep="[]", fallback="[]"))
        ui = FakeUI(eof=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), patched_paths(t):
            rc = cli.main(["--config", cfgp, "run", "--no-detach", "--no-smoke"],
                          ui=ui, system=t.sys["machine-a"],
                          peer_factory=lambda label, target, cmd: t.peers[label])
        out = buf.getvalue() + "\n".join(ui.lines)
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", out)
        self.assertTrue("sudo" in out.lower() or notice.SUDO_NEEDED in out)


class SlotMerge(unittest.TestCase):
    def test_launch_then_window_keeps_started(self):
        t = Twin(); s = t.sys["machine-a"]
        path = notice.window_lock_path(t.cfg.state_dir)
        notice.write_window_pid(s, path, 0, started=False, unit="twin-update-prompt-1")
        s.alive_pids.add(5001)
        s.pid_starts[5001] = 42
        notice.write_window_pid(s, path, 5001, started=True, start=42)
        data = notice.read_slot(s, path)
        self.assertTrue(data["started"])
        self.assertEqual(data["pid"], 5001)
        self.assertEqual(data["start"], 42)

    def test_window_then_launch_keeps_started(self):
        t = Twin(); s = t.sys["machine-a"]
        path = notice.window_lock_path(t.cfg.state_dir)
        s.alive_pids.add(5002)
        s.pid_starts[5002] = 7
        notice.write_window_pid(s, path, 5002, started=True, start=7,
                                unit="twin-update-prompt-2")
        notice.write_window_pid(s, path, 0, started=False, unit="twin-update-prompt-2")
        data = notice.read_slot(s, path)
        self.assertTrue(data["started"])
        self.assertEqual(data["pid"], 5002)
        self.assertEqual(data["start"], 7)

    def test_pid_zero_does_not_clear_live_unit(self):
        t = Twin(); s = t.sys["machine-a"]
        path = notice.window_lock_path(t.cfg.state_dir)
        s.mainpid_zero_first = True
        env = notice.graphical_env(s)
        argv = notice.launcher_argv(s.which, ["twin-update", "prompt"])
        # Force a pid-0 return by making show stay at 0 / activating.
        s.prompt_pids["held"] = 0
        notice.write_window_pid(s, path, 0, started=False, unit="twin-update-prompt-9")
        s._mainpid_reads["twin-update-prompt-9"] = 0
        s.prompt_pids["twin-update-prompt-9"] = 0
        self.assertTrue(notice.unit_still_up(s, "twin-update-prompt-9", env))
        from twin_update import cli
        kept = cli._keep_launched_window(s, path, 0, "twin-update-prompt-9", env)
        self.assertTrue(kept)
        self.assertTrue(notice.read_slot(s, path).get("unit"))
        self.assertNotEqual(notice.read_slot(s, path), {})

    def test_reused_pid_does_not_hold_slot(self):
        t = Twin(); s = t.sys["machine-a"]
        path = notice.window_lock_path(t.cfg.state_dir)
        s.alive_pids.add(6000)
        s.pid_starts[6000] = 11
        notice.write_window_pid(s, path, 6000, started=True, start=11)
        self.assertTrue(notice.slot_held(s, path))
        s.pid_starts[6000] = 99
        self.assertFalse(notice.slot_held(s, path))
        self.assertTrue(notice.slot_pid_reused(s, path))

    def test_window_older_than_a_day_does_not_hold(self):
        t = Twin(); s = t.sys["machine-a"]
        path = notice.window_lock_path(t.cfg.state_dir)
        s.alive_pids.add(6001)
        s.pid_starts[6001] = 1
        notice.write_window_pid(s, path, 6001, started=True, start=1,
                                opened_at=s.now() - 90000)
        self.assertFalse(notice.slot_held(s, path))
        stale, _since = notice.slot_stale(s, path)
        self.assertTrue(stale)

    def test_prompt_config_error_pauses(self):
        import contextlib
        import io
        import tempfile
        from twin_update import cli
        tmpd = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)
        cfgp = os.path.join(tmpd, "c.toml")
        with open(cfgp, "w") as fh:
            fh.write("[machines]\n")
        ui = FakeUI()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--config", cfgp, "prompt", "--inside"], ui=ui)
        out = buf.getvalue() + "\n".join(ui.lines)
        self.assertEqual(rc, 2)
        self.assertIn("config error", out)
        self.assertTrue(any("Press Enter" in l for l in ui.lines))

    def test_checking_prints_before_build(self):
        import contextlib
        import io
        import tempfile
        from unittest import mock
        from fakes import patched_paths
        from twin_update import cli
        t = Twin(); standard(t)
        tmpd = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmpd, True)
        cfgp = os.path.join(tmpd, "c.toml")
        with open(cfgp, "w") as fh:
            fh.write(CONFIG.format(keep="[]", fallback="[]"))
        ui = FakeUI(confirm=False)
        seen = []
        real = cli._build

        def wrapped(*a, **k):
            seen.append(list(ui.lines))
            return real(*a, **k)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), patched_paths(t), \
                mock.patch.object(cli, "_build", side_effect=wrapped):
            cli.main(["--config", cfgp, "prompt", "--inside"], ui=ui,
                     system=t.sys["machine-a"],
                     peer_factory=lambda label, target, cmd: t.peers[label])
        self.assertTrue(seen)
        self.assertIn(notice.CHECKING, seen[0])

    def test_console_confirm_on_pty(self):
        import os
        import pty
        from unittest import mock
        from twin_update import cli
        master, slave = pty.openpty()
        try:
            slave_in = os.fdopen(slave, "r", buffering=1)
            os.write(master, b"y\n")
            with mock.patch.object(cli.sys, "stdin", slave_in):
                ui = cli.ConsoleUI()
                self.assertTrue(ui.can_prompt())
                self.assertTrue(ui.confirm("Update now?"))
        finally:
            try:
                slave_in.close()
            except Exception:
                pass
            try:
                os.close(master)
            except OSError:
                pass

    def test_doctor_warns_when_execstart_missing(self):
        from twin_update.orchestrator import doctor_findings
        t = Twin(); standard(t)
        home = t.sys["machine-a"].home()
        unit_dir = os.path.join(home, ".config/systemd/user")
        os.makedirs(unit_dir, exist_ok=True)
        with open(os.path.join(unit_dir, "twin-update-check.service"), "w") as fh:
            fh.write("[Service]\nType=oneshot\nSuccessExitStatus=10\n"
                     "ExecStart=/no/such/twin-update check --notify\n")
        with open(os.path.join(unit_dir, "twin-update-check.timer"), "w") as fh:
            fh.write("[Timer]\nOnCalendar=*-*-* 09:13:00\nPersistent=true\n")
        d = t.eng["machine-a"].doctor()
        _, warnings = doctor_findings(d)
        self.assertTrue(any("ExecStart does not exist" in w for w in warnings))


class HelpHidesRaft(unittest.TestCase):
    def test_help_has_no_suppress_or_raft_upload(self):
        import contextlib
        import io
        from twin_update import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                cli.main(["--help"])
            except SystemExit as exc:
                self.assertEqual(exc.code, 0)
        text = buf.getvalue()
        self.assertNotIn("==SUPPRESS==", text)
        self.assertNotIn("raft-upload", text)
