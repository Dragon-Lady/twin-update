"""Configuration (~/.config/twin-update/config.toml). No personal defaults here."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field

DEFAULT_PATH = "~/.config/twin-update/config.toml"

# Generic package facts only. Paths are where the vendor .debs install.
BUILTIN_APPS = {
    "cursor": {
        "package": "cursor",
        "roots": ["/usr/share/cursor"],
        "launch": ["/usr/share/cursor/cursor"],
        "display": "Cursor",
    },
    "chatgpt": {
        "package": "chatgpt",
        "roots": ["/usr/lib/chatgpt"],
        "launch": ["/usr/bin/chatgpt"],
        "display": "ChatGPT",
    },
    "grok-bot": {
        "package": "grok-bot",
        "roots": ["/opt/Grok Bot"],
        "launch": ["/usr/bin/grok-bot"],
        "display": "Grok Bot",
    },
}


class ConfigError(Exception):
    pass


@dataclass
class Machine:
    label: str
    user: str
    ssh_target: str = ""
    remote_command: str = "~/.local/bin/twin-update"
    machine_id_sha256: str = ""
    lock_paths: list[str] = field(default_factory=list)


@dataclass
class App:
    name: str
    package: str
    roots: list[str]
    launch: list[str]
    display: str


@dataclass
class Raft:
    enabled: bool = False
    script_dir: str = ""
    remote: str = ""
    lock_path: str = ""
    snapshot_timeout_s: int = 120
    upload_timeout_s: int = 1200
    keep: int = 2
    upload_prefix: str = "cursor-liferaft"
    machines: list[str] = field(default_factory=list)


@dataclass
class Config:
    machines: dict[str, Machine]
    apps: dict[str, App]
    enabled: list[str]
    keep_holds: list[str]
    fallback_rollback: list[str]
    close_timeout_s: float = 90.0
    smoke_s: float = 20.0
    apt_lock_timeout_s: int = 300
    lock_wait_s: float = 600.0
    store: str = "~/.local/share/twin-update/rollback"
    state_dir: str = "~/.local/state/twin-update"
    search_dirs: list[str] = field(default_factory=lambda: ["~/Downloads"])
    grace_days: float = 7.0
    grace_launches: int = 2
    # Apps twin-update must never start: no smoke launch, no reopen after a run.
    # They are still closed gracefully before an update and still updated.
    no_launch: list[str] = field(default_factory=lambda: ["cursor"])
    raft: Raft = field(default_factory=Raft)
    config_path: str = ""

    def self_label(self, whoami: str, machine_id_sha256: str = "") -> str:
        """Find this machine's label: by user, then by machine-id when the
        same user name exists on both machines."""
        hits = [m for m in self.machines.values() if m.user == whoami]
        if len(hits) > 1:
            hits = [m for m in hits if m.machine_id_sha256 and m.machine_id_sha256 == machine_id_sha256]
            if len(hits) != 1:
                raise ConfigError(
                    "both machines use the same user name: set machine_id_sha256 on each "
                    "[machines.*] table (value: tr -d '\\n' < /etc/machine-id | sha256sum)")
        if len(hits) != 1:
            raise ConfigError(
                f"whoami does not match exactly one [machines.*].user (matches: {len(hits)})")
        return hits[0].label

    def peer_label(self, me: str) -> str:
        others = [k for k in self.machines if k != me]
        if len(others) != 1:
            raise ConfigError("config must list exactly two machines")
        return others[0]


def _expand(p: str, home: str) -> str:
    if p == "~" or p.startswith("~/"):
        return home + p[1:]
    return p


def load(path: str | None = None, home: str | None = None, text: str | None = None) -> Config:
    home = home or os.path.expanduser("~")
    if text is None:
        real = _expand(path or DEFAULT_PATH, home)
        try:
            with open(real, "rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            raise ConfigError(f"config not found: {path or DEFAULT_PATH}") from None
    else:
        data = tomllib.loads(text)

    msec = data.get("machines") or {}
    if len(msec) != 2:
        raise ConfigError("config must define exactly two [machines.<label>] tables")
    machines = {}
    for label, m in msec.items():
        if not isinstance(m, dict) or not m.get("user"):
            raise ConfigError(f"[machines.{label}] needs user")
        machines[label] = Machine(
            label=label, user=m["user"], ssh_target=m.get("ssh_target", ""),
            remote_command=m.get("remote_command", "~/.local/bin/twin-update"),
            machine_id_sha256=m.get("machine_id_sha256", ""),
            lock_paths=[_expand(p, home) for p in m.get("lock_paths", [])],
        )

    asec = data.get("apps") or {}
    apps: dict[str, App] = {}
    for name, base in BUILTIN_APPS.items():
        over = asec.get(name, {}) if isinstance(asec.get(name), dict) else {}
        merged = {**base, **over}
        apps[name] = App(name, merged["package"], list(merged["roots"]),
                         list(merged["launch"]), merged["display"])
    for name, over in asec.items():
        if isinstance(over, dict) and name not in apps:
            for k in ("package", "roots", "launch"):
                if k not in over:
                    raise ConfigError(f"[apps.{name}] needs {k}")
            apps[name] = App(name, over["package"], list(over["roots"]),
                             list(over["launch"]), over.get("display", name))
    enabled = list(asec.get("enabled", list(BUILTIN_APPS)))
    for n in enabled:
        if n not in apps:
            raise ConfigError(f"unknown app in enabled: {n}")
    no_launch = list(asec.get("no_launch", ["cursor"]))
    for n in no_launch:
        if n not in apps:
            raise ConfigError(f"unknown app in no_launch: {n}")

    t = data.get("timeouts") or {}
    r = data.get("rollback") or {}
    rs = data.get("raft") or {}
    if not isinstance(rs, dict) or set(rs) - set(Raft.__dataclass_fields__):
        raise ConfigError("unknown raft configuration key")
    raft = Raft(**rs)
    if not isinstance(raft.enabled, bool):
        raise ConfigError("raft.enabled must be boolean")
    for key in ("script_dir", "remote", "lock_path", "upload_prefix"):
        if not isinstance(getattr(raft, key), str):
            raise ConfigError(f"raft.{key} must be text")
    raft.script_dir = _expand(raft.script_dir or "~/.local/share/twin-update/raft", home)
    raft.lock_path = _expand(raft.lock_path, home)
    for key, limit in (("snapshot_timeout_s", 600), ("upload_timeout_s", 86400), ("keep", 100)):
        value = getattr(raft, key)
        if type(value) is not int or not 1 <= value <= limit:
            raise ConfigError(f"raft.{key} must be between 1 and {limit}")
    if not isinstance(raft.machines, list) or any(x not in machines for x in raft.machines):
        raise ConfigError("raft.machines must contain configured machine labels")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", raft.upload_prefix):
        raise ConfigError("raft.upload_prefix must be a simple name")
    if raft.enabled and (not os.path.isabs(raft.lock_path) or not raft.remote or raft.remote.startswith("-")):
        raise ConfigError("enabled raft requires an absolute lock_path and an explicit remote")
    return Config(
        machines=machines, apps=apps, enabled=enabled,
        keep_holds=list(asec.get("keep_holds", [])),
        fallback_rollback=list(r.get("fallback", [])),
        close_timeout_s=float(t.get("close_s", 90)),
        smoke_s=float(t.get("smoke_s", 20)),
        apt_lock_timeout_s=int(t.get("apt_lock_s", 300)),
        lock_wait_s=float(t.get("lock_wait_s", 600)),
        store=_expand(r.get("store", "~/.local/share/twin-update/rollback"), home),
        state_dir=_expand(data.get("state_dir", "~/.local/state/twin-update"), home),
        search_dirs=[_expand(p, home) for p in r.get("search_dirs", ["~/Downloads"])],
        grace_days=float(r.get("grace_days", 7)),
        grace_launches=int(r.get("grace_launches", 2)),
        no_launch=no_launch,
        raft=raft,
        config_path=_expand(path or DEFAULT_PATH, home) if text is None else "",
    )
