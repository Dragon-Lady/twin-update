"""Thin, fakeable wrapper around the operating system.

Everything that touches processes, files or subprocesses goes through a
``System`` object so the engine can be tested with fakes.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import shutil
import signal
import subprocess
import tarfile
import time
from dataclasses import dataclass


@dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass
class Proc:
    pid: int
    ppid: int
    exe: str          # resolved executable path, " (deleted)" stripped
    comm: str
    start: int        # starttime in clock ticks, guards against PID reuse
    deleted: bool = False
    argv: tuple[str, ...] = ()


class System:
    """Real implementation. Methods are small on purpose."""

    def pid_alive(self, pid: int) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def pid_start(self, pid: int) -> int | None:
        """Starttime ticks for a live pid, used to reject PID reuse in the slot."""
        if not isinstance(pid, int) or pid <= 0:
            return None
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
            fields = raw[raw.rindex(")") + 2:].split()
            return int(fields[19])
        except (OSError, ValueError, IndexError):
            return None

    def try_lockfile(self, path: str, blocking: bool = False):
        """Exclusive flock on a lockfile, created if needed. None if busy."""
        import fcntl
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except OSError:
            os.close(fd)
            return None
        return fd

    def run(self, argv, input: bytes | None = None, timeout: float | None = None,
            env: dict | None = None, cwd: str | None = None) -> Result:
        """Run a command. Never SIGKILL the child on interrupt or timeout.

        KeyboardInterrupt waits for the child to finish, then is re-raised
        so apt/dpkg/sudo can complete or fail on their own.
        """
        try:
            proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, cwd=cwd)
        except FileNotFoundError as exc:
            return Result(127, "", str(exc))
        interrupted = False
        try:
            out, err = proc.communicate(input=input, timeout=timeout)
        except KeyboardInterrupt:
            interrupted = True
            try:
                out, err = proc.communicate(timeout=3600)
            except subprocess.TimeoutExpired:
                raise KeyboardInterrupt
        except subprocess.TimeoutExpired:
            try:
                out, err = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                return Result(124, "", "timeout")
        result = Result(proc.returncode if proc.returncode is not None else 1,
                        out.decode("utf-8", "replace"),
                        err.decode("utf-8", "replace"))
        if interrupted:
            raise KeyboardInterrupt
        return result

    def run_inherit(self, argv, timeout: float | None = None,
                    env: dict | None = None, cwd: str | None = None,
                    wait_after_s: float = 3600) -> Result:
        """Run without capturing stdio so a window tty stays attached.

        The child is in its own session so Ctrl-C in this window does not hit
        it. SIGINT/SIGHUP are ignored here while it exists. The child is
        never SIGKILL'd: a timeout waits further, then reports that it is
        still running.
        """
        prev = {}
        for sig in (signal.SIGINT, signal.SIGHUP):
            try:
                prev[sig] = signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
        try:
            try:
                proc = subprocess.Popen(list(argv), env=env, cwd=cwd,
                                        start_new_session=True)
            except FileNotFoundError as exc:
                return Result(127, "", str(exc))
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    rc = proc.wait(timeout=wait_after_s)
                except subprocess.TimeoutExpired:
                    return Result(
                        124, "",
                        "the update is still running; cancel with "
                        "systemctl --user stop <the update unit>")
            return Result(rc if rc is not None else 1, "", "")
        finally:
            for sig, old in prev.items():
                try:
                    signal.signal(sig, old)
                except (ValueError, OSError):
                    pass

    # identity -----------------------------------------------------------
    def whoami(self) -> str:
        return pwd.getpwuid(os.getuid()).pw_name

    def uid(self) -> int:
        return os.getuid()

    def home(self) -> str:
        return os.path.expanduser("~")

    def groups(self) -> list[str]:
        import grp
        user = self.whoami()
        names = {g.gr_name for g in grp.getgrall() if user in g.gr_mem}
        names.add(grp.getgrgid(os.getgid()).gr_name)
        return sorted(names)

    def machine_id(self) -> str:
        try:
            with open("/etc/machine-id", "rb") as fh:
                return hashlib.sha256(fh.read().strip()).hexdigest()
        except OSError:
            return ""

    def env(self) -> dict:
        return dict(os.environ)

    # processes ----------------------------------------------------------
    def procs(self) -> dict[int, Proc]:
        out: dict[int, Proc] = {}
        uid = os.getuid()
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                if os.stat(f"/proc/{pid}").st_uid != uid:
                    continue
                with open(f"/proc/{pid}/stat", "rb") as fh:
                    raw = fh.read().decode("utf-8", "replace")
                comm = raw[raw.index("(") + 1:raw.rindex(")")]
                fields = raw[raw.rindex(")") + 2:].split()
                ppid, start = int(fields[1]), int(fields[19])
                exe = os.readlink(f"/proc/{pid}/exe")
            except (OSError, ValueError, IndexError):
                continue
            deleted = exe.endswith(" (deleted)")
            if deleted:
                exe = exe[: -len(" (deleted)")]
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    argv = tuple(x.decode("utf-8", "replace") for x in fh.read(8192).split(b"\0")[:2])
            except OSError:
                argv = ()
            out[pid] = Proc(pid, ppid, exe, comm, start, deleted, argv)
        return out

    def fd_holders(self, paths) -> list[dict]:
        targets = {os.path.realpath(p) for p in paths}
        holders = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            root = f"/proc/{name}"
            try:
                if os.stat(root).st_uid != self.uid():
                    continue
                for fd in os.listdir(root + "/fd"):
                    try:
                        target = os.readlink(root + "/fd/" + fd).removesuffix(" (deleted)")
                    except FileNotFoundError:
                        continue
                    if os.path.realpath(target) in targets:
                        with open(root + "/comm") as fh:
                            holders.append({"pid": int(name), "comm": fh.read().strip()})
                        break
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                # A same-user non-dumpable process can hide its descriptors.
                # Keep this uncertainty blocking instead of declaring no holder.
                holders.append({"pid": int(name), "comm": "unreadable descriptors", "unreadable": True})
        return holders

    def term(self, pid: int, start: int) -> bool:
        """SIGTERM via pidfd, only if the pid still has the same start time."""
        return self.term_result(pid, start) == "sent"

    def term_result(self, pid: int, start: int) -> str:
        """SIGTERM via pidfd with a reason: sent, gone, reused, denied or error.

        ``denied`` is EPERM/EACCES, e.g. an AppArmor profile that refuses
        signals from this process's label. SIGTERM only, one attempt.
        """
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return "gone"
        except PermissionError:
            return "denied"
        except OSError:
            return "error"
        try:
            if self.pid_start(pid) != start:
                return "reused"
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            return "sent"
        except ProcessLookupError:
            return "gone"
        except PermissionError:
            return "denied"
        except OSError:
            return "error"
        finally:
            os.close(fd)

    # raw /proc reads for the helper idle check (twin_update.helpers) -----
    def proc_stat(self, pid: int) -> str:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return fh.read().decode("utf-8", "replace")

    def proc_exe(self, pid: int) -> str:
        return os.readlink(f"/proc/{pid}/exe")

    def proc_label(self, pid: int | str) -> str:
        with open(f"/proc/{pid}/attr/current", "rb") as fh:
            return fh.read().decode("utf-8", "replace").strip("\0\n ")

    def own_label(self) -> str | None:
        """AppArmor label of this process, or None if it cannot be read."""
        try:
            return self.proc_label("self")
        except OSError:
            return None

    def proc_fds(self, pid: int) -> list[str]:
        out = []
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                out.append(os.readlink(f"/proc/{pid}/fd/{fd}"))
            except FileNotFoundError:
                continue
        return out

    def proc_net_tcp(self, pid: int) -> str:
        parts = []
        for name in ("tcp", "tcp6"):
            try:
                with open(f"/proc/{pid}/net/{name}", encoding="ascii", errors="replace") as fh:
                    parts.append(fh.read())
            except FileNotFoundError:
                if name == "tcp":
                    raise
        return "\n".join(parts)

    def ppid_map(self) -> dict[int, int]:
        """pid -> ppid for every process (any user), to find children."""
        out = {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as fh:
                    raw = fh.read().decode("utf-8", "replace")
                out[int(name)] = int(raw[raw.rindex(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
        return out

    def user_subreapers(self) -> set[int]:
        """PIDs of this user's ``systemd --user`` managers (they adopt orphans)."""
        out = set()
        for pid, p in self.procs().items():
            if p.comm == "systemd" and "--user" in p.argv:
                out.add(pid)
        return out

    def uptime_s(self) -> float:
        with open("/proc/uptime", encoding="ascii") as fh:
            return float(fh.read().split()[0])

    def clock_ticks(self) -> int:
        return os.sysconf("SC_CLK_TCK")

    def own_pid(self) -> int:
        return os.getpid()

    def all_comms(self) -> list[str]:
        """Command names of every process (any user), for apt/dpkg busy checks."""
        names = []
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    with open(f"/proc/{name}/comm", encoding="utf-8") as fh:
                        names.append(fh.read().strip())
                except OSError:
                    pass
        return names

    # files ----------------------------------------------------------------
    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def isdir(self, path: str) -> bool:
        return os.path.isdir(path)

    def symlink(self, src: str, dst: str) -> None:
        os.symlink(src, dst)

    def remove(self, path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def dir_size(self, path: str) -> int:
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        return total

    def mtime(self, path: str) -> float | None:
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def listdir(self, path: str) -> list[str]:
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def read_text(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    def write_private(self, path: str, text: str) -> bool:
        tmp = None
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            tmp = path + f".tmp.{os.getpid()}.{time.monotonic_ns()}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            return True
        except OSError:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return False

    def write_inplace(self, path: str, text: str) -> bool:
        """Write without replacing the inode, so an flock on this file survives."""
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, text.encode("utf-8"))
                os.fsync(fd)
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            return True
        except OSError:
            return False

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def makedirs(self, path: str) -> None:
        os.makedirs(path, mode=0o700, exist_ok=True)

    def copy_private(self, src: str, dst: str) -> None:
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)

    def rmtree(self, path: str) -> None:
        shutil.rmtree(path)

    def sha256(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def deb_file_md5s(self, path: str) -> str | None:
        """md5sums-style listing of the regular files inside a .deb.

        Streams ``dpkg-deb --fsys-tarfile`` through tarfile; nothing is
        extracted to disk. Used when a vendor deb ships no md5sums member.
        """
        p = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", path],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        lines = []
        try:
            with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
                for m in tf:
                    if not m.isreg():
                        continue
                    fh = tf.extractfile(m)
                    h = hashlib.md5(usedforsecurity=False)
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                    name = m.name[2:] if m.name.startswith("./") else m.name.lstrip("/")
                    lines.append(f"{h.hexdigest()}  {name}")
        except (tarfile.TarError, OSError):
            p.kill()
            p.wait()
            return None
        finally:
            p.stdout.close()
        return "\n".join(lines) if p.wait() == 0 else None

    def is_executable_file(self, path: str) -> bool:
        return os.path.isfile(path) and os.access(path, os.X_OK)

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    # locks ----------------------------------------------------------------
    def try_flock(self, path: str):
        """Return an open fd holding an exclusive flock, or None if busy/missing."""
        import fcntl
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        return fd

    def unlock(self, fd) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    # time -----------------------------------------------------------------
    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)
