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


class System:
    """Real implementation. Methods are small on purpose."""

    def run(self, argv, input: bytes | None = None, timeout: float | None = None,
            env: dict | None = None, cwd: str | None = None) -> Result:
        try:
            cp = subprocess.run(list(argv), input=input, capture_output=True,
                                timeout=timeout, env=env, cwd=cwd,
                                stdin=None if input is not None else subprocess.DEVNULL)
        except FileNotFoundError as exc:
            return Result(127, "", str(exc))
        except subprocess.TimeoutExpired:
            return Result(124, "", "timeout")
        return Result(cp.returncode, cp.stdout.decode("utf-8", "replace"),
                      cp.stderr.decode("utf-8", "replace"))

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
            out[pid] = Proc(pid, ppid, exe, comm, start, deleted)
        return out

    def term(self, pid: int, start: int) -> bool:
        """SIGTERM via pidfd, only if the pid still has the same start time."""
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            return False
        try:
            cur = self.procs().get(pid)
            if cur is None or cur.start != start:
                return False
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

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

    def write_private(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

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
