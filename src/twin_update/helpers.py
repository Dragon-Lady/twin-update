"""Leftover sandbox helpers: decide whether they are idle, then close them.

Cursor starts ``cursorsandbox`` helpers. When Cursor exits they can be left
behind with parent PID 1, idle, but still under Cursor's install root. They
block a graceful close. This module:

* classifies each helper as ORPHANED_IDLE, ACTIVE or UNKNOWN from /proc
  facts (pure functions; the raw reads go through ``System`` so tests feed
  fake /proc text);
* closes only ORPHANED_IDLE helpers, with SIGTERM through a pidfd and a
  start-time check. Never SIGKILL. One attempt, no retry loop;
* refuses to signal unless this process runs with the AppArmor label
  ``unconfined`` (the user's own session). The ``cursor_sandbox`` profile
  only accepts signals from ``unconfined`` peers. On a denial it reports
  and stops: no sudo, no profile edits.

Command lines are never logged or returned (``--policy-json`` carries the
proxy allowlist). Only the flags ``run-proxy`` and ``policy`` are kept.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

ORPHANED_IDLE = "ORPHANED_IDLE"
ACTIVE = "ACTIVE"
UNKNOWN = "UNKNOWN"

HELPERS_NONE = "HELPERS_NONE"
HELPERS_CLEARED = "HELPERS_CLEARED"
HELPERS_ACTIVE = "HELPERS_ACTIVE"
HELPERS_NEED_USER_SESSION = "HELPERS_NEED_USER_SESSION"
HELPERS_STILL_RUNNING = "HELPERS_STILL_RUNNING"
HELPERS_WOULD_CLOSE = "HELPERS_WOULD_CLOSE"

USER_COMMAND = "twin-update close-helpers"
NEED_SESSION_HINT = (
    "These leftover helpers only accept a stop signal from your own login session. "
    f"Run `{USER_COMMAND}` in your own terminal (it checks they are idle first), "
    "or log out and back in, or reboot.")
STILL_RUNNING_HINT = (
    "The stop signal was refused or did not finish. Nothing else was tried. "
    "Check `journalctl -k | grep cursor_sandbox` for an AppArmor denial. "
    f"Run `{USER_COMMAND}` in your own terminal, or log out and back in, or reboot.")

# TCP states from include/net/tcp_states.h, as hex in /proc/net/tcp.
TCP_LISTEN = "0A"
TCP_CLOSE = "07"


@dataclass(frozen=True)
class HelperSpec:
    app: str
    exe: str
    comm: str
    label_prefix: str
    data_dir: str            # relative to $HOME
    main_comms: tuple[str, ...]


SPECS = {
    "cursor": HelperSpec(
        app="cursor",
        exe="/usr/share/cursor/resources/app/resources/helpers/cursorsandbox",
        comm="cursorsandbox",
        label_prefix="cursor_sandbox",
        data_dir=".config/Cursor/User",
        main_comms=("cursor", "cursor-agent"),
    ),
}


@dataclass
class HelperFacts:
    pid: int
    ppid: int = 0
    exe: str = ""
    comm: str = ""
    start: int = 0
    label: str = ""
    fds: list[str] = field(default_factory=list)
    sockets: list[tuple[str, bool]] = field(default_factory=list)  # (state, loopback)
    cpu: list[int] = field(default_factory=list)                     # utime+stime samples
    children: list[int] = field(default_factory=list)
    age_s: float = 0.0
    flags: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------- parsing
def parse_stat(raw: str) -> dict:
    """Fields we need from /proc/PID/stat. Raises ValueError on bad input."""
    try:
        comm = raw[raw.index("(") + 1:raw.rindex(")")]
        f = raw[raw.rindex(")") + 2:].split()
        return {"comm": comm, "ppid": int(f[1]), "cpu": int(f[11]) + int(f[12]),
                "start": int(f[19])}
    except (ValueError, IndexError) as exc:
        raise ValueError("bad stat") from exc


def _hex_ip_loopback(addr: str) -> bool:
    ip = addr.split(":")[0]
    if len(ip) == 8:                         # IPv4, little-endian per 32-bit word
        return ip[6:8] == "7F"
    if len(ip) == 32:                        # IPv6: ::1 or ::ffff:127.x.x.x
        if ip == "00000000000000000000000001000000":
            return True
        return ip[:16] == "0000000000000000" and ip[16:24] == "FFFF0000" and ip[30:32] == "7F"
    return False


def parse_net_tcp(text: str) -> dict[str, tuple[str, bool]]:
    """inode -> (state hex, local address is loopback) from /proc/PID/net/tcp{,6}."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10 or parts[0] == "sl":
            continue
        local, state, inode = parts[1], parts[3].upper(), parts[9]
        if inode != "0":
            out[inode] = (state, _hex_ip_loopback(local.upper()))
    return out


def socket_inodes(fd_targets: list[str]) -> set[str]:
    out = set()
    for t in fd_targets:
        if t.startswith("socket:[") and t.endswith("]"):
            out.add(t[len("socket:["):-1])
    return out


def is_data_fd(target: str, home: str, spec: HelperSpec) -> bool:
    """A Cursor database file (or its journal) is open. Log files are fine."""
    path = target.removesuffix(" (deleted)")
    base = os.path.basename(path).lower()
    if ".sqlite" in base:
        return True
    data_root = os.path.join(home, spec.data_dir).rstrip("/") + "/"
    if path.startswith(data_root):
        return (".vscdb" in base or base.endswith((".db", "-wal", "-shm", "-journal")))
    return False


def argv_flags(argv) -> list[str]:
    flags = []
    for a in argv or ():
        if a == "--run-proxy":
            flags.append("run-proxy")
        elif a.startswith("--policy"):
            flags.append("policy")
    return sorted(set(flags))


# ---------------------------------------------------------------- classify
def classify(f: HelperFacts, spec: HelperSpec, *, main_present: bool, reapers: set[int],
             min_age_s: float, home: str) -> dict:
    """One helper's class with every reason. Pure: reads nothing."""
    out = {"pid": f.pid, "class": ACTIVE, "reasons": [], "exe": f.exe, "ppid": f.ppid,
           "start": f.start, "cpu_delta": None, "children": len(f.children),
           "db_fds": 0, "tcp_live": 0, "label": f.label, "flags": list(f.flags),
           "orphan_of": None}
    if f.error:
        out["class"] = UNKNOWN
        out["reasons"].append(f"unreadable: {f.error}")
        return out
    r = out["reasons"]
    if main_present:
        r.append("main app running")
    if f.exe != spec.exe or f.comm != spec.comm:
        r.append("not the sandbox helper binary")
    if not f.label.startswith(spec.label_prefix):
        r.append("unexpected security label")
    if f.ppid == 1:
        out["orphan_of"] = "init"
    elif f.ppid in reapers:
        out["orphan_of"] = "user-systemd"
    else:
        r.append("has a live parent")
    db = [t for t in f.fds if is_data_fd(t, home, spec)]
    out["db_fds"] = len(db)
    if db:
        r.append("Cursor data file open")
    if f.children:
        r.append("has child processes")
    if len(f.cpu) < 2:
        r.append("no CPU sample")
    else:
        out["cpu_delta"] = max(f.cpu) - f.cpu[0]
        if out["cpu_delta"] > 0:
            r.append("used CPU during the check")
    live = [s for s in f.sockets if not (s[0] == TCP_LISTEN and s[1]) and s[0] != TCP_CLOSE]
    out["tcp_live"] = len(live)
    if live:
        r.append("live network connection")
    if f.age_s < min_age_s:
        r.append("started too recently")
    if not r:
        out["class"] = ORPHANED_IDLE
    return out


def collect(system, pids: list[int], argvs: dict[int, tuple], sample_s: float) -> list[HelperFacts]:
    """Read /proc facts for each pid, with two CPU samples ``sample_s`` apart."""
    facts: dict[int, HelperFacts] = {pid: HelperFacts(pid) for pid in pids}
    try:
        ppids = system.ppid_map()
        uptime = system.uptime_s()
        hz = system.clock_ticks()
    except (OSError, ValueError) as exc:
        for f in facts.values():
            f.error = f"system: {type(exc).__name__}"
        return list(facts.values())
    for pid, f in facts.items():
        try:
            st = parse_stat(system.proc_stat(pid))
            f.comm, f.ppid, f.start = st["comm"], st["ppid"], st["start"]
            f.cpu.append(st["cpu"])
            f.exe = system.proc_exe(pid).removesuffix(" (deleted)")
            f.label = system.proc_label(pid)
            f.fds = system.proc_fds(pid)
            table = parse_net_tcp(system.proc_net_tcp(pid))
            f.sockets = [table[i] for i in socket_inodes(f.fds) if i in table]
            f.children = sorted(c for c, pp in ppids.items() if pp == pid)
            f.age_s = uptime - f.start / hz
            f.flags = argv_flags(argvs.get(pid, ()))
        except (OSError, ValueError) as exc:
            f.error = type(exc).__name__
    system.sleep(sample_s)
    for pid, f in facts.items():
        if f.error:
            continue
        try:
            st = parse_stat(system.proc_stat(pid))
            if st["start"] != f.start:
                f.error = "pid reused"
                continue
            f.cpu.append(st["cpu"])
        except (OSError, ValueError) as exc:
            f.error = type(exc).__name__
    return list(facts.values())


def counts(results: list[dict]) -> dict:
    out = {ORPHANED_IDLE: 0, ACTIVE: 0, UNKNOWN: 0}
    for r in results:
        out[r["class"]] += 1
    return out
