"""Fakes for subprocess, dpkg, apt, ps, sudo and ssh. Files are real (temp dirs)."""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import tempfile

from twin_update import engine as engine_mod
from twin_update.config import load
from twin_update.engine import Engine
from twin_update.peer import LocalPeer
from twin_update.system import Proc, Result, System

CONFIG = """
[machines.machine-a]
user = "alice"
ssh_target = "alice@machine-a.example"
lock_paths = ["~/.local/state/sync/sync.lock"]

[machines.machine-b]
user = "bob"
ssh_target = "bob@machine-b.example"
lock_paths = ["~/.local/state/sync/sync.lock"]

[apps]
enabled = ["cursor", "chatgpt", "grok-bot"]
no_launch = []
keep_holds = {keep}

[rollback]
fallback = {fallback}

[timeouts]
close_s = 5
smoke_s = 3
apt_lock_s = 0
lock_wait_s = 0
"""

PW = "c0rrect-Horse!pw"


_TMP = []


@atexit.register
def _cleanup():
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def vkey(v):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", v) if x]


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0


class FakeSystem(System):
    def __init__(self, user, events, clock):
        self.user = user
        self.events = events
        self.clock = clock
        self.root = tempfile.mkdtemp(prefix=f"tu-{user}-")
        _TMP.append(self.root)
        self._home = os.path.join(self.root, "home")
        os.makedirs(self._home)
        os.makedirs(os.path.join(self._home, ".local/state/sync"))
        open(os.path.join(self._home, ".local/state/sync/sync.lock"), "w").close()
        os.makedirs(os.path.join(self._home, "Downloads"))
        self.archives_dir = os.path.join(self.root, "archives")
        self.info_dir = os.path.join(self.root, "info")
        os.makedirs(self.archives_dir)
        os.makedirs(self.info_dir)
        self.pkgs = {}            # pkg -> version
        self.cands = {}           # pkg -> candidate
        self.repo = {}            # pkg -> [versions]
        self.held = set()
        self.files = {}           # pkg -> [exe paths]
        self.proc_table = {}      # pid -> Proc
        self.holder_table = []
        self.stubborn = set()     # pids that ignore SIGTERM
        self.calls = []           # (argv, input, env)
        self.fail_install = set()
        self.smoke_dies = set()
        self.smoke_handoff = set()
        self.smoke_late = set()
        self._smoke_spawned = set()
        self.apt_busy_flag = False
        self.locked = False
        self.window_flock = False
        self.pid = 999999
        self.missing_tools = set()
        self.no_graphical = False
        self.spawned = []
        self.next_spawn_pid = 4000
        self.alive_pids = set()
        self.prompt_pids = {}
        self.spawn_dies_immediately = False
        self.spawn_forks = False  # client pid exits; unit stays activating
        self.fresh_cands = {}     # pkg -> candidate seen by a fresh user-level fetch
        self.fresh_fail = None    # apt-get update error text, or None
        self.fetched = False
        self.mainpid_zero_first = False
        self._mainpid_reads = {}
        self._lockfiles = set()
        self.launcher_target = None  # realpath of x-terminal-emulator
        self.smoke_crash_after = {}  # app -> seconds after start when a hand-off crashes
        self._smoke_started_at = {}
        self.journal_crash = set()
        self.unit_files = {}  # name -> text for systemctl cat
        self.run_units = []   # started twin-update-run-* units
        self.pid_starts = {}  # pid -> starttime ticks (slot identity)
        self.follow_script = None  # list of {active, show} snapshots
        self.follow_idx = 0
        self.cleaned_units = []  # ("reset-failed"|"stop", unit)

    # identity
    def whoami(self): return self.user
    def uid(self): return 1000
    def home(self): return self._home
    def groups(self): return ["sudo", self.user]
    def machine_id(self): return "mid-" + self.user
    def env(self): return {"PATH": "/usr/bin", "HOME": self._home}
    def own_pid(self): return self.pid
    def all_comms(self): return ["dpkg"] if self.apt_busy_flag else ["bash"]
    def which(self, name):
        return None if name in self.missing_tools else "/usr/bin/" + name
    def now(self): return self.clock.t
    def sleep(self, s): self.clock.t += s
    def is_executable_file(self, p): return True

    # processes
    def add_app_procs(self, exe_main, n_children=2, start=100, child_exes=()):
        base = max(self.proc_table or [1000]) + 1
        self.proc_table[base] = Proc(base, 1, exe_main, "main", start)
        for i in range(n_children):
            self.proc_table[base + 1 + i] = Proc(base + 1 + i, base, exe_main, "child", start + 1)
        nxt = base + 1 + n_children
        for ce in child_exes:
            self.proc_table[nxt] = Proc(nxt, base, ce, "helper", start + 2)
            nxt += 1
        return base

    def procs(self):
        return dict(self.proc_table)

    def fd_holders(self, paths):
        return list(self.holder_table)

    def term(self, pid, start):
        p = self.proc_table.get(pid)
        if not p or p.start != start:
            return False
        self.events.append((self.user, "TERM", p.exe))
        if pid in self.stubborn:
            return True
        # main exits with its tree
        doomed = {pid}
        changed = True
        while changed:
            changed = False
            for q in list(self.proc_table.values()):
                if q.ppid in doomed and q.pid not in doomed and q.pid not in self.stubborn:
                    doomed.add(q.pid)
                    changed = True
        for d in doomed:
            self.proc_table.pop(d, None)
        return True

    # locks
    def try_flock(self, path):
        if self.locked or not os.path.exists(path):
            return None
        return 42

    def try_lockfile(self, path, blocking=False):
        if self.window_flock or path in self._lockfiles:
            return None
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        self._lockfiles.add(path)
        return ("lockfile", path)

    def realpath(self, path):
        if self.launcher_target and str(path).endswith("x-terminal-emulator"):
            return self.launcher_target
        return os.path.realpath(path) if os.path.exists(path) else path

    def pid_alive(self, pid):
        return pid in self.alive_pids

    def pid_start(self, pid):
        if pid in self.proc_table:
            return self.proc_table[pid].start
        return self.pid_starts.get(pid)

    def _run_snap(self, consume=False):
        script = self.follow_script
        if script is None:
            script = [
                {"active": "activating",
                 "show": "Result=\nExecMainStatus=0\nActiveState=activating\nLoadState=loaded\n"},
                {"active": "inactive",
                 "show": "Result=success\nExecMainStatus=0\nActiveState=inactive\nLoadState=loaded\n"},
            ]
        i = self.follow_idx
        if i >= len(script):
            i = len(script) - 1
        snap = script[i]
        if consume and self.follow_idx < len(script):
            self.follow_idx += 1
        return snap

    def _maybe_smoke_crash(self, app):
        after = self.smoke_crash_after.get(app)
        started = self._smoke_started_at.get(app)
        if after is None or started is None:
            return
        if self.clock.t - started >= after:
            self.journal_crash.add(app)
            doomed = [pid for pid, p in self.proc_table.items()
                      if app in p.exe or os.path.basename(p.exe).lower() == app]
            for pid in doomed:
                self.proc_table.pop(pid, None)

    def run_inherit(self, argv, timeout=None, env=None, cwd=None, wait_after_s=3600):
        return self.run(argv, timeout=timeout, env=env, cwd=cwd)

    def unlock(self, fd):
        if isinstance(fd, tuple) and fd[0] == "lockfile":
            self._lockfiles.discard(fd[1])

    # debs
    def make_deb(self, d, pkg, ver, arch="amd64", md5="m-"):
        name = engine_mod.deb_name(pkg, ver, arch)
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            json.dump({"Package": pkg, "Version": ver, "Architecture": arch,
                       "md5sums": md5 + pkg + ver}, fh)
        return path

    def make_deb_without_md5sums(self, d, pkg, ver, files, arch="amd64"):
        """Vendor-style deb: no md5sums member, only file contents (name -> md5)."""
        name = engine_mod.deb_name(pkg, ver, arch)
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            json.dump({"Package": pkg, "Version": ver, "Architecture": arch,
                       "content": files}, fh)
        return path

    def deb_file_md5s(self, path):
        try:
            d = _load(path)
        except (OSError, ValueError):
            return None
        return "\n".join(f"{m}  {n}" for n, m in d.get("content", {}).items())

    def install_pkg(self, pkg, ver, arch="amd64"):
        self.pkgs[pkg] = ver
        with open(os.path.join(self.info_dir, f"{pkg}.md5sums"), "w") as fh:
            fh.write("m-" + pkg + ver)

    # subprocess
    def run_inherit(self, argv, timeout=None, env=None, cwd=None):
        return self.run(argv, input=None, timeout=timeout, env=env, cwd=cwd)

    def run(self, argv, input=None, timeout=None, env=None, cwd=None):
        argv = list(argv)
        self.calls.append((argv, input, env))
        if argv[:1] == ["sudo"]:
            assert argv[:6] == ["sudo", "-k", "-S", "-p", "", "--"], argv
            if input != (PW + "\n").encode():
                return Result(1, "", "Sorry, try again.")
            return self._root(argv[6:])
        return self._user(argv, cwd)

    def _user(self, argv, cwd):
        cmd = argv[0]
        if cmd == "dpkg-query":
            pkg = argv[-1]
            if pkg in self.pkgs:
                return Result(0, f"ii |{self.pkgs[pkg]}|amd64")
            return Result(1, "", "no packages found")
        if argv[:2] == ["apt-cache", "policy"]:
            pkg = argv[-1]
            fresh = any(a.startswith("Dir::State::Lists=") for a in argv)
            c = (self.fresh_cands.get(pkg, self.cands.get(pkg)) if fresh and self.fetched
                 else self.cands.get(pkg))
            table = f"     {c} 500\n        500 http://repo stable/main amd64 Packages\n" if c else ""
            return Result(0, f"{pkg}:\n  Installed: x\n  Candidate: {c or '(none)'}\n"
                             f"  Version table:\n{table}")
        if argv[0] == "apt-get" and argv[-1] == "update":
            if self.fresh_fail:
                return Result(100, "Err:1 http://repo stable InRelease\n", self.fresh_fail)
            self.fetched = True
            return Result(0, "Hit:1 http://repo stable InRelease\nReading package lists...\n")
        if argv[:2] == ["apt-cache", "madison"]:
            return Result(0, "".join(f" {argv[2]} | {v} | http://repo stable/main amd64 Packages\n"
                                     for v in self.repo.get(argv[2], [])))
        if argv[:2] == ["apt-mark", "showhold"]:
            return Result(0, "".join(h + "\n" for h in sorted(self.held)))
        if argv[:2] == ["dpkg", "--compare-versions"]:
            a, op, b = argv[2:5]
            ka, kb = vkey(a), vkey(b)
            res = {"lt": ka < kb, "gt": ka > kb, "eq": ka == kb}[op]
            return Result(0 if res else 1)
        if argv[:2] == ["dpkg", "-L"]:
            return Result(0, "\n".join(self.files.get(argv[2], [])))
        if argv[:2] == ["dpkg", "-V"]:
            return Result(0, "")
        if cmd == "dpkg-deb":
            try:
                d = _load(argv[2] if argv[1] in ("-f", "-I") else argv[-1])
            except (OSError, ValueError):
                return Result(2, "", "not a deb")
            if argv[1] == "-f":
                return Result(0, "".join(f"{k}: {d[k]}\n" for k in argv[3:]))
            if "md5sums" not in d:
                return Result(1, "", "no md5sums member")
            return Result(0, d["md5sums"])
        if argv[:2] == ["apt-get", "download"]:
            pkg, ver = argv[2].split("=", 1)
            if ver not in self.repo.get(pkg, []):
                return Result(100, "", "E: Version not found")
            self.make_deb(cwd, pkg, ver)
            return Result(0)
        if cmd == "systemd-run":
            unit = [a for a in argv if a.startswith("--unit=")][0][7:]
            if unit.startswith("twin-update-prompt-"):
                inner = argv[argv.index("--") + 1:] if "--" in argv else argv
                pid = self.next_spawn_pid
                self.next_spawn_pid += 1
                self.spawned.append((inner, None, pid))
                self.prompt_pids[unit] = pid
                if not self.spawn_dies_immediately and not self.spawn_forks:
                    self.alive_pids.add(pid)
                return Result(0)
            if unit.startswith("twin-update-run-"):
                inner = argv[argv.index("--") + 1:] if "--" in argv else argv
                self.run_units.append((unit, inner))
                self.spawned.append((inner, None, None))
                self.events.append((self.user, "RUN-UNIT", unit))
                return Result(0)
            self.events.append((self.user, "LAUNCH", unit))
            if unit.startswith("twin-smoke-"):
                app = unit[len("twin-smoke-"):].rsplit("-", 1)[0]
                self._smoke_started_at[app] = self.clock.t
                if app in self.smoke_late:
                    return Result(0)
                if app in self.smoke_handoff or app in self.smoke_crash_after:
                    exe = (self.files.get(app) or ["/usr/share/cursor/cursor"])[0]
                    self.add_app_procs(exe)
            return Result(0)
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            unit = argv[3] if len(argv) > 3 else ""
            if unit.startswith("twin-smoke-"):
                app = unit[len("twin-smoke-"):].rsplit("-", 1)[0]
                if app in self.smoke_late and app not in self._smoke_spawned:
                    exe = (self.files.get(app) or ["/usr/share/cursor/cursor"])[0]
                    self.add_app_procs(exe)
                    self._smoke_spawned.add(app)
                self._maybe_smoke_crash(app)
                if app in self.smoke_handoff or app in self.smoke_crash_after:
                    return Result(3, "inactive")
                return Result(0, "failed" if app in self.smoke_dies else "active")
            if unit.startswith("twin-update-run-"):
                snap = self._run_snap(consume=False)
                state = snap.get("active", "inactive")
                return Result(0 if state == "active" else 3, state)
            return Result(3, "inactive")
        if argv[:3] == ["systemctl", "--user", "reset-failed"]:
            unit = argv[3] if len(argv) > 3 else ""
            self.cleaned_units.append(("reset-failed", unit))
            return Result(0)
        if argv[:3] == ["systemctl", "--user", "stop"]:
            unit = argv[3] if len(argv) > 3 else ""
            self.cleaned_units.append(("stop", unit))
            return Result(0)
        if argv[:3] == ["systemctl", "--user", "is-enabled"]:
            return Result(0, "enabled\n")
        if argv[:3] == ["systemctl", "--user", "show-environment"]:
            if self.no_graphical:
                return Result(0, "LANG=C\n")
            return Result(0, "WAYLAND_DISPLAY=wayland-1\nDISPLAY=:0\n")
        if argv[:3] == ["systemctl", "--user", "cat"]:
            name = argv[3] if len(argv) > 3 else ""
            if name in self.unit_files:
                return Result(0, self.unit_files[name])
            return Result(1, "", "No files found")
        if argv[:3] == ["systemctl", "--user", "show"]:
            if any("twin-update-check.timer" in a for a in argv):
                return Result(0, "UnitFileState=enabled\nActiveState=active\n"
                              "NextElapseUSecRealtime=Mon 2026-10-26 09:13:00 UTC\n")
            unit = next((a for a in argv[3:] if not a.startswith("--")), "")
            if unit in self.prompt_pids:
                n = self._mainpid_reads.get(unit, 0)
                self._mainpid_reads[unit] = n + 1
                pid = self.prompt_pids[unit]
                if self.mainpid_zero_first and n == 0:
                    pid = 0
                if self.spawn_dies_immediately:
                    state = "inactive"
                elif self.spawn_forks:
                    state = "activating"
                else:
                    state = "active"
                return Result(0, f"MainPID={pid}\nActiveState={state}\nLoadState=loaded\n")
            if unit.startswith("twin-update-run-"):
                snap = self._run_snap(consume=True)
                text = snap.get("show", "")
                return Result(snap.get("rc", 0), text)
            if unit.startswith("twin-smoke-"):
                return Result(0, "MainPID=0\nActiveState=active\n")
            return Result(127, "", f"fake: unknown {argv}")
        if cmd == "journalctl":
            unit = ""
            if "-u" in argv:
                unit = argv[argv.index("-u") + 1]
            app = ""
            if unit.startswith("twin-smoke-"):
                app = unit[len("twin-smoke-"):].rsplit("-", 1)[0]
            if app in self.journal_crash or app in self.smoke_crash_after:
                self._maybe_smoke_crash(app)
                if app in self.journal_crash:
                    return Result(0, "segmentation fault\n")
            return Result(0, "")
        if cmd in ("notify-send", "gdbus"):
            return Result(0, "")
        return Result(127, "", f"fake: unknown {argv}")

    def _root(self, argv):
        if argv == ["true"]:
            return Result(0)
        if argv[:2] == ["apt-mark", "unhold"]:
            self.held.discard(argv[2]); self.events.append((self.user, "UNHOLD", argv[2]))
            return Result(0)
        if argv[:2] == ["apt-mark", "hold"]:
            self.held.add(argv[2]); return Result(0)
        if argv and argv[0] == "env":
            argv = argv[1:]
            while argv and "=" in argv[0] and not argv[0].startswith("-"):
                argv = argv[1:]
        if argv[:1] == ["apt-get"]:
            if "update" in argv:
                self.events.append((self.user, "APT-UPDATE", ""))
                return Result(0)
            if "install" in argv:
                tgt = argv[-1]
                if "--only-upgrade" in argv:
                    pkg, ver = tgt.split("=", 1)
                    self.events.append((self.user, "INSTALL", tgt))
                    if pkg in self.fail_install:
                        return Result(100, "", "E: broken")
                    self.install_pkg(pkg, ver)
                    return Result(0)
                if "--allow-downgrades" in argv:
                    d = _load(tgt)
                    self.events.append((self.user, "DOWNGRADE", d["Version"]))
                    self.install_pkg(d["Package"], d["Version"])
                    return Result(0)
        return Result(127, "", f"fake root: unknown {argv}")


class FakeUI:
    def __init__(self, password=PW, confirm=False, eof=False, confirm_timeout=False,
                 can_prompt=True):
        self.password = password
        self.answer = confirm
        self.eof = eof
        self.confirm_timeout = confirm_timeout
        self._can_prompt = can_prompt
        self.lines = []
        self.prompts = []
        self.timeouts = []

    def say(self, msg):
        self.lines.append(msg)

    def can_prompt(self):
        return self._can_prompt

    def getpass(self, prompt):
        self.prompts.append(prompt)
        if self.eof:
            raise EOFError
        return self.password

    def confirm(self, prompt, timeout_s=None):
        self.prompts.append(prompt)
        self.timeouts.append(timeout_s)
        if self.eof:
            raise EOFError
        if self.confirm_timeout:
            return False
        return self.answer

    def pause(self, msg="Press Enter to close."):
        self.lines.append(msg)


class Twin:
    """Two fake machines wired as LocalPeers."""

    def __init__(self, keep=(), fallback=()):
        self.events = []
        self.logs = []
        self.clock = Clock()
        self.sys = {}
        self.eng = {}
        self.peers = {}
        text = CONFIG.format(keep=json.dumps(list(keep)), fallback=json.dumps(list(fallback)))
        for label, user in (("machine-a", "alice"), ("machine-b", "bob")):
            s = FakeSystem(user, self.events, self.clock)
            cfg = load(text=text, home=s.home())
            e = Engine(cfg, s, label, log=self.logs.append)
            e.archives, e.dpkg_info = s.archives_dir, s.info_dir
            e.dpkg_updates = os.path.join(s.root, "dpkg-updates")
            os.makedirs(e.dpkg_updates)
            e.apt_lists = os.path.join(s.root, "lists")
            os.makedirs(os.path.join(e.apt_lists, "partial"))
            ago = self.clock.t - 7200                      # system lists refreshed 2 h ago
            os.utime(os.path.join(e.apt_lists, "partial"), (ago, ago))
            e.apt_update_stamp = os.path.join(s.root, "periodic", "update-success-stamp")
            e.sources_dir = os.path.join(s.root, "sources.list.d")
            os.makedirs(e.sources_dir)
            with open(os.path.join(e.sources_dir, "vendor.sources"), "w") as fh:
                fh.write("Types: deb\nURIs: http://repo/\nSuites: stable\nComponents: main\n"
                         "Signed-By: /usr/share/keyrings/vendor.gpg\n")
            with open(os.path.join(e.sources_dir, "other.list"), "w") as fh:
                fh.write("deb http://elsewhere.example stable main\n")
            self.sys[label], self.eng[label] = s, e
            self.peers[label] = LocalPeer(e)
            for pkg, root in (("cursor", "/usr/share/cursor"), ("chatgpt", "/usr/lib/chatgpt"),
                              ("grok-bot", "/opt/Grok Bot")):
                s.files[pkg] = [root + "/" + pkg]
        self.cfg = self.eng["machine-a"].cfg

    def setup(self, pkg, installed, candidate, cached=True, machines=("machine-a", "machine-b")):
        for l in machines:
            s = self.sys[l]
            s.install_pkg(pkg, installed if isinstance(installed, str) else installed[l])
            s.cands[pkg] = candidate if isinstance(candidate, str) else candidate[l]
            s.repo[pkg] = [s.cands[pkg]]
            if cached:
                s.make_deb(s.archives_dir, pkg, s.pkgs[pkg])

    def session(self, ui):
        from twin_update.orchestrator import Session
        return Session(self.cfg, self.peers, ["machine-a", "machine-b"], ui)


def patched_paths(t, label="machine-a"):
    """Point a CLI-built engine (module defaults) and HOME at the fake machine."""
    import contextlib
    from unittest import mock
    e, s = t.eng[label], t.sys[label]
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.dict(os.environ, {"HOME": s.home()}))
    for name, val in (("APT_LISTS", e.apt_lists), ("APT_UPDATE_STAMP", e.apt_update_stamp),
                      ("SOURCES_DIR", e.sources_dir), ("ARCHIVES", e.archives),
                      ("DPKG_INFO", e.dpkg_info)):
        stack.enter_context(mock.patch.object(engine_mod, name, val))
    return stack
