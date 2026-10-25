"""Talking to an Engine: in-process (LocalPeer) or over one ssh session (SshPeer).

The ssh session runs ``twin-update local --serve`` on the peer. Requests and
responses are JSON lines on the session's stdin/stdout. The session exists
only for the length of one command; nothing listens afterwards.
"""

from __future__ import annotations

import json
import os
import signal
import time
import selectors
import subprocess
import tempfile

from twin_update.engine import Engine, EngineError

OPS = {
    "identity", "set_password", "validate_sudo", "status", "acquire_locks", "apt_update",
    "capture", "holds", "clear_holds", "close_start", "close_wait", "upgrade", "verify",
    "reopen", "notify", "rollback_target", "rollback_install", "prune", "doctor", "release",
    "hold", "unhold", "record_run", "check", "notice_due", "record_notice", "running",
    "pre_reopen_hook", "raft_upload_start",
}


class PeerError(Exception):
    pass


def classify_ssh_error(stderr: str, exit_code: int | None, no_answer: bool = False) -> str:
    """Plain-words reason for a failed ssh session (no host names or addresses)."""
    t = stderr.lower()
    if any(k in t for k in ("could not resolve hostname", "name or service not known",
                            "temporary failure in name resolution", "no address associated")):
        return ("name lookup failed: its host name does not resolve on this machine "
                "(check DNS/MagicDNS, or use its address in ssh_target)")
    if "tailscale ssh requires an additional check" in t or "login.tailscale.com" in t:
        return ("Tailscale SSH asked for an interactive check; a non-interactive run needs "
                "the policy action 'accept' for this pair")
    if any(k in t for k in ("permission denied", "host key verification failed",
                            "too many authentication failures", "remote host identification")):
        return "ssh login refused (authentication or host key)"
    if "connection refused" in t:
        return "connection refused (no ssh server listening there, or it is blocked)"
    if no_answer or any(k in t for k in ("timed out", "no route to host", "network is unreachable",
                                         "host is down", "operation timed out")):
        return "timed out: the machine may be off, asleep or offline"
    if exit_code not in (None, 255) and "not found" in t:
        return "twin-update not found there (check remote_command)"
    return f"ssh session failed (exit {exit_code})"


class LocalPeer:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.label = engine.label

    def call(self, op: str, call_timeout: float | None = None, **kw):
        if op not in OPS:
            raise PeerError(f"op not allowed: {op}")
        return getattr(self.engine, op)(**kw)

    def close(self) -> None:
        self.engine.release()


class SshPeer:
    def __init__(self, label: str, target: str, remote_command: str,
                 connect_timeout: float = 25.0, popen=subprocess.Popen):
        if not target:
            raise PeerError(f"no ssh_target configured for {label}")
        self.label = label
        self._connection = (label, target, remote_command, connect_timeout, popen)
        self._connect()

    def _connect(self) -> None:
        label, target, remote_command, connect_timeout, popen = self._connection
        self._timed_out = False
        self._dirty = False
        self._buffer = b""
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                target, f"{remote_command} local --serve --as {label}"]
        # stderr goes to an anonymous temp file (nothing on disk by name), read
        # only to explain a failure.
        self._err = tempfile.TemporaryFile()
        def _preexec():
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)

        try:
            self.proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=self._err, start_new_session=True,
                              preexec_fn=_preexec)
        except TypeError:
            # Test fakes and older Popen wrappers may not take these kwargs.
            try:
                self.proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self._err)
            except BaseException:
                self._err.close()
                raise
        except BaseException:
            self._err.close()
            raise
        self._id = 0
        self._first_timeout = connect_timeout

    def _reset_session(self) -> None:
        """Abandon a dirty session: let the remote finish, then reconnect.

        Closing stdin ends serve after the current request (no SIGKILL, so a
        remote apt upgrade is not cut off). Wait for that process, then open
        a fresh session so request ids cannot collide with a stale reply.
        """
        proc = getattr(self, "proc", None)
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5400)
            except Exception:
                pass
        self._close_err()
        self._connect()

    def _readline(self, timeout: float | None) -> bytes:
        if timeout is None and not self._buffer:
            return self.proc.stdout.readline()
        sel = selectors.DefaultSelector()
        sel.register(self.proc.stdout, selectors.EVENT_READ)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while b"\n" not in self._buffer:
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                if not sel.select(remaining):
                    self._timed_out = True
                    raise PeerError(f"{self.label}: operation timed out")
                part = os.read(self.proc.stdout.fileno(), 65536)
                if not part:
                    return b""
                self._buffer += part
            line, self._buffer = self._buffer.split(b"\n", 1)
            return line + b"\n"
        finally:
            sel.close()

    def call(self, op: str, call_timeout: float | None = None, **kw):
        if op not in OPS:
            raise PeerError(f"op not allowed: {op}")
        if self._timed_out or self._dirty:
            self._reset_session()
        self._id += 1
        line = json.dumps({"id": self._id, "op": op, "args": kw}).encode() + b"\n"
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._timed_out = True
            self._dirty = True
            raise PeerError(f"{self.label}: {self._why()}") from None
        except KeyboardInterrupt:
            self._timed_out = True
            self._dirty = True
            raise
        finally:
            del line  # may carry the sudo password; drop our reference
        timeout, self._first_timeout = call_timeout if call_timeout is not None else self._first_timeout, None
        try:
            raw = self._readline(timeout)
        except (KeyboardInterrupt, BrokenPipeError, OSError):
            self._timed_out = True
            self._dirty = True
            raise
        except PeerError:
            self._timed_out = True
            self._dirty = True
            raise
        if not raw:
            self._timed_out = True
            self._dirty = True
            raise PeerError(f"{self.label}: {self._why()}")
        try:
            msg = json.loads(raw)
        except ValueError:
            self._timed_out = True
            self._dirty = True
            raise PeerError(f"{self.label}: {self._why()}") from None
        if not isinstance(msg, dict):
            self._timed_out = True
            self._dirty = True
            raise PeerError(f"{self.label}: {self._why()}")
        while isinstance(msg.get("id"), int) and msg["id"] < self._id:
            raw = self._readline(timeout)
            if not raw:
                self._timed_out = True
                self._dirty = True
                raise PeerError(f"{self.label}: {self._why()}")
            try:
                msg = json.loads(raw)
            except ValueError:
                self._timed_out = True
                self._dirty = True
                raise PeerError(f"{self.label}: {self._why()}") from None
            if not isinstance(msg, dict):
                self._timed_out = True
                self._dirty = True
                raise PeerError(f"{self.label}: {self._why()}")
        if msg.get("id") != self._id:
            self._timed_out = True
            self._dirty = True
            raise PeerError("unexpected peer response id")
        if not msg.get("ok"):
            raise PeerError(f"{self.label}: {msg.get('error', 'error')}")
        return msg["result"]

    def _why(self, no_answer: bool = False) -> str:
        code = None
        try:
            code = self.proc.wait(timeout=0 if no_answer else 3)
        except Exception:
            pass
        text = ""
        try:
            self._err.flush()
            self._err.seek(0)
            text = self._err.read(8192).decode("utf-8", "replace")
        except (OSError, ValueError):
            pass
        return classify_ssh_error(text, code, no_answer=no_answer and code is None)

    def abort(self) -> None:
        """Stop the remote serve by closing stdin. SIGTERM is ignored by ssh."""
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        self._timed_out = True
        self._dirty = True
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass
        self._close_err()

    def _close_err(self) -> None:
        try:
            self._err.close()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
        finally:
            self._close_err()


def serve(engine: Engine, rfile, wfile) -> int:
    """Answer JSON-line requests until EOF. Never echoes request arguments."""
    try:
        for raw in iter(rfile.readline, b""):
            rid = None
            try:
                req = json.loads(raw)
                if not isinstance(req, dict):
                    raise EngineError("request is not a JSON object")
                rid = req.get("id")
                op = req.get("op")
                if op not in OPS:
                    raise EngineError(f"op not allowed: {op}")
                result = getattr(engine, op)(**(req.get("args") or {}))
                resp = {"id": rid, "ok": True, "result": result}
            except Exception as exc:  # report, keep serving
                resp = {"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            finally:
                raw = req = None
            wfile.write(json.dumps(resp).encode() + b"\n")
            wfile.flush()
    finally:
        engine.release()
    return 0
