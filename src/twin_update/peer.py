"""Talking to an Engine: in-process (LocalPeer) or over one ssh session (SshPeer).

The ssh session runs ``twin-update local --serve`` on the peer. Requests and
responses are JSON lines on the session's stdin/stdout. The session exists
only for the length of one command; nothing listens afterwards.
"""

from __future__ import annotations

import json
import selectors
import subprocess

from twin_update.engine import Engine, EngineError

OPS = {
    "identity", "set_password", "validate_sudo", "status", "acquire_locks", "apt_update",
    "capture", "holds", "clear_holds", "close_start", "close_wait", "upgrade", "verify",
    "reopen", "notify", "rollback_target", "rollback_install", "prune", "doctor", "release",
    "hold", "unhold", "record_run", "check", "notice_due",
}


class PeerError(Exception):
    pass


class LocalPeer:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.label = engine.label

    def call(self, op: str, **kw):
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
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                target, f"{remote_command} local --serve --as {label}"]
        self.proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
        self._id = 0
        self._first_timeout = connect_timeout

    def _readline(self, timeout: float | None) -> bytes:
        if timeout is None:
            return self.proc.stdout.readline()
        sel = selectors.DefaultSelector()
        sel.register(self.proc.stdout, selectors.EVENT_READ)
        try:
            if not sel.select(timeout):
                raise PeerError(f"{self.label}: no answer within {timeout:.0f}s (ssh)")
        finally:
            sel.close()
        return self.proc.stdout.readline()

    def call(self, op: str, **kw):
        if op not in OPS:
            raise PeerError(f"op not allowed: {op}")
        self._id += 1
        line = json.dumps({"id": self._id, "op": op, "args": kw}).encode() + b"\n"
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise PeerError(f"{self.label}: ssh session closed") from None
        finally:
            del line  # may carry the sudo password; drop our reference
        timeout, self._first_timeout = self._first_timeout, None
        raw = self._readline(timeout)
        if not raw:
            raise PeerError(f"{self.label}: ssh session ended (exit {self.proc.poll()})")
        msg = json.loads(raw)
        if not msg.get("ok"):
            raise PeerError(f"{self.label}: {msg.get('error', 'error')}")
        return msg["result"]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.terminate()


def serve(engine: Engine, rfile, wfile) -> int:
    """Answer JSON-line requests until EOF. Never echoes request arguments."""
    try:
        for raw in iter(rfile.readline, b""):
            rid = None
            try:
                req = json.loads(raw)
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
