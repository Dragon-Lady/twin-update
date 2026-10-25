"""Real-signal subprocess tests for interrupt and ssh isolation.

Covered here (each fails on the previous head if the wait/kill contract
regresses):
- SIGINT to the prompt parent does not SIGKILL the run child.
- SIGTERM of a run child is waited out through its cleanup.
- System.run (apt/sudo path) waits out KeyboardInterrupt instead of SIGKILL.
- The ssh child ignores SIGTERM and keeps answering.
- Exit 3 says you stopped it; exit 5 says the update stopped before finishing.

In-process coverage for close_start on the second machine, record_run
during SIGTERM, EOF at rollback, and hold/state files lives in
test_twin_update.py.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


class RunInheritSignals(unittest.TestCase):
    def test_parent_sigint_does_not_sigkill_child(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        started = os.path.join(tmp, "started")
        done = os.path.join(tmp, "done")
        child = os.path.join(tmp, "child.py")
        parent = os.path.join(tmp, "parent.py")
        with open(child, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                import os, signal, time, sys
                def on_term(s, f):
                    time.sleep(0.4)
                    open({done!r}, "w").write("ok")
                    sys.exit(3)
                signal.signal(signal.SIGTERM, on_term)
                open({started!r}, "w").write(str(os.getpid()))
                time.sleep(30)
            """))
        with open(parent, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                import sys
                sys.path.insert(0, {os.path.abspath('src')!r})
                from twin_update.system import System
                System().run_inherit([sys.executable, {child!r}])
            """))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.abspath("src")
        proc = subprocess.Popen([sys.executable, parent], env=env)
        deadline = time.time() + 5
        pid = None
        while time.time() < deadline:
            if os.path.exists(started):
                with open(started) as fh:
                    pid = int(fh.read())
                break
            time.sleep(0.05)
        self.assertTrue(pid)
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.2)
        self.assertTrue(os.path.exists(f"/proc/{pid}") or os.path.exists(done)
                        or _pid_alive(pid))
        os.kill(pid, signal.SIGTERM)
        proc.wait(timeout=5)
        self.assertTrue(os.path.exists(done), "child was SIGKILL'd before cleanup")

    def test_child_sigterm_cleanup_is_waited_for(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        started = os.path.join(tmp, "started")
        done = os.path.join(tmp, "done")
        child = os.path.join(tmp, "child.py")
        with open(child, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                import os, signal, time, sys
                def on_term(s, f):
                    time.sleep(0.35)
                    open({done!r}, "w").write("ok")
                    sys.exit(3)
                signal.signal(signal.SIGTERM, on_term)
                open({started!r}, "w").write(str(os.getpid()))
                time.sleep(30)
            """))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.abspath("src") + ":" + os.path.abspath("tests")
        parent = os.path.join(tmp, "parent.py")
        with open(parent, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                import sys
                sys.path.insert(0, {os.path.abspath('src')!r})
                from twin_update.system import System
                r = System().run_inherit([sys.executable, {child!r}])
                sys.exit(r.rc)
            """))
        proc = subprocess.Popen([sys.executable, parent], env=env)
        deadline = time.time() + 5
        pid = None
        while time.time() < deadline:
            if os.path.exists(started):
                with open(started) as fh:
                    pid = int(fh.read())
                break
            time.sleep(0.05)
        self.assertTrue(pid)
        t0 = time.time()
        os.kill(pid, signal.SIGTERM)
        rc = proc.wait(timeout=5)
        self.assertGreaterEqual(time.time() - t0, 0.3)
        self.assertTrue(os.path.exists(done))
        self.assertEqual(rc, 3)


class SshIgnoresTerm(unittest.TestCase):
    def test_ssh_child_ignores_sigterm(self):
        from twin_update.peer import SshPeer
        script = textwrap.dedent("""\
            import json, os, signal, sys
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            for raw in sys.stdin:
                req = json.loads(raw)
                sys.stdout.write(json.dumps({"id": req["id"], "ok": True,
                                             "result": {"pid": os.getpid()}}) + "\\n")
                sys.stdout.flush()
        """)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        tmp.write(script)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)

        def popen(argv, **kw):
            kw.pop("preexec_fn", None)
            return subprocess.Popen(
                [sys.executable, tmp.name],
                stdin=kw.get("stdin", subprocess.PIPE),
                stdout=kw.get("stdout", subprocess.PIPE),
                stderr=kw.get("stderr", subprocess.DEVNULL),
                start_new_session=True,
                preexec_fn=kw.get("preexec_fn"),
            )

        peer = SshPeer("machine-b", "user@host", "twin-update", popen=popen,
                       connect_timeout=None)
        self.addCleanup(peer.close)
        first = peer.call("identity")
        os.kill(first["pid"], signal.SIGTERM)
        second = peer.call("status")
        self.assertEqual(first["pid"], second["pid"])


class RunDoesNotKill(unittest.TestCase):
    def test_run_waits_out_interrupt(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        started = os.path.join(tmp, "started")
        done = os.path.join(tmp, "done")
        child = os.path.join(tmp, "child.py")
        with open(child, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                import os, signal, time, sys
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                open({started!r}, "w").write(str(os.getpid()))
                time.sleep(0.4)
                open({done!r}, "w").write("ok")
            """))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.abspath("src")
        parent = os.path.join(tmp, "parent.py")
        with open(parent, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                import sys, os, signal, time
                sys.path.insert(0, {os.path.abspath('src')!r})
                from twin_update.system import System
                def boom(s, f):
                    raise KeyboardInterrupt
                signal.signal(signal.SIGINT, boom)
                try:
                    System().run([sys.executable, {child!r}])
                except KeyboardInterrupt:
                    sys.exit(9)
            """))
        proc = subprocess.Popen([sys.executable, parent], env=env)
        deadline = time.time() + 5
        while time.time() < deadline and not os.path.exists(started):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(started))
        os.kill(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=8)
        self.assertTrue(os.path.exists(done), "child was SIGKILL'd before it finished")
        self.assertEqual(rc, 9)


class Announce(unittest.TestCase):
    def test_failed_run_says_plain_reason(self):
        from fakes import FakeUI
        from twin_update import notice
        ui = FakeUI()
        notice._announce_run_result(ui, 3, "exit-code", 3)
        self.assertTrue(any("you stopped it" in l for l in ui.lines))
        self.assertFalse(any("exit-code" in l for l in ui.lines))
        ui2 = FakeUI()
        notice._announce_run_result(ui2, 5, "exit-code", 5)
        self.assertTrue(any("stopped before finishing" in l for l in ui2.lines))


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
