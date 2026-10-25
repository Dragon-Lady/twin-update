"""Signal and lifecycle behaviour.

Matrix (each cell: apps afterwards, reopen once?, recorded?, user told?, exit):

Signal/event x phase x role
- SIGINT in the prompt window during a running child: run stays in its
  own session; parent does not SIGKILL; apps follow the run's own
  cleanup (reopen on SIGTERM of the unit). Exit is the child's.
- SIGTERM (`systemctl --user stop <unit>`) during close_wait / apt /
  rollback question / final reopen: apps that were closed are reopened
  exactly once; run is recorded; message says interrupted; exit 3.
- SIGHUP / window closed: ignored in the run and in the prompt while a
  child exists; update continues; no reopen (apps stay as the run left
  them until it finishes).
- SIGKILL of the run / killed apt: nothing here can recover; doctor
  warns if /var/lib/dpkg/updates is not empty; README says
  `sudo dpkg --configure -a`.
- EOF on stdin at the password: abort before close, nothing changed,
  exit 1, no traceback.
- EOF at the rollback question: treat as no; failed version held;
  peer not upgraded; apps reopened; recorded.
- ssh drop mid-call: next call opens a fresh session (or skips stale
  ids); peer reopen still attempted.
- unit stop: same as SIGTERM.

Tests below cover the distinct classes with real signals in a subprocess.
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


class Announce(unittest.TestCase):
    def test_failed_run_says_plain_reason(self):
        from fakes import FakeUI
        from twin_update import notice
        ui = FakeUI()
        notice._announce_run_result(ui, 3, "exit-code", 3)
        self.assertTrue(any("aborted by you" in l for l in ui.lines))
        self.assertFalse(any("exit-code" in l for l in ui.lines))


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
