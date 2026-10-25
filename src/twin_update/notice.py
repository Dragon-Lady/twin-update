"""Daily update notice: open a terminal window and ask, do not use notification actions.

COSMIC (and some other desktops) can show a notice without usable buttons.
The main path is therefore a window running ``twin-update prompt``.
A plain, button-less desktop notice is only an extra reminder.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Callable

APP_NAME = "Twin Update"
WINDOW_LOCK = "prompt.window"
LAUNCH_VERIFY_S = 2.0
ALREADY_UPDATED = "Nothing is pending (the other machine may already have updated)."
TEST_ONLY = "Test only, nothing will be updated"
TEST_NOTHING_RAN = "test only, nothing ran"
EXTRA_NOTIFY_BODY = "Updates waiting: see the Twin Update window"
UPDATE_NOW = "Update now?"
GUI_KEYS = ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")

TEST_PENDING = [
    {"display": "Example App", "installed": "1.0.0", "candidate": "1.1.0",
     "where": "both machines"},
]


def launcher_name(which: Callable[[str], str | None]) -> str | None:
    for name in ("cosmic-term", "x-terminal-emulator"):
        if which(name):
            return name
    return None


def start_prompt_window(system, argv: list[str], env: dict) -> int | None:
    """Start the terminal via a user transient unit (the oneshot can then exit)."""
    if not system.which("systemd-run"):
        return None
    unit = f"twin-update-prompt-{int(system.now())}"
    started = system.run(
        ["systemd-run", "--user", "--collect", "--quiet", "--no-block",
         f"--unit={unit}", "--", *argv],
        env=env, timeout=30)
    if not started.ok:
        return None
    shown = system.run(
        ["systemctl", "--user", "show", unit, "--property=MainPID", "--value"],
        env=env, timeout=10)
    text = shown.out.strip()
    if text.isdigit() and int(text) > 0:
        return int(text)
    return None


def launcher_argv(which: Callable[[str], str | None], inner: list[str]) -> list[str] | None:
    """Build the terminal argv. Prefer cosmic-term, then x-terminal-emulator."""
    name = launcher_name(which)
    if not name:
        return None
    script = "exec " + shlex.join(inner)
    # cosmic-term forks and exits unless --no-daemon is set before -e;
    # the systemd-run unit then has no living main process.
    if name == "cosmic-term":
        return [name, "--no-daemon", "-e", "bash", "-lc", script]
    return [name, "-e", "bash", "-lc", script]


def twin_update_cmd() -> list[str]:
    """How to re-invoke this tool from a launched window."""
    exe = sys.argv[0] if sys.argv else ""
    base = os.path.basename(exe)
    if exe and (base == "twin-update" or exe.endswith(".pyz")):
        return [os.path.abspath(exe) if not os.path.isabs(exe) else exe]
    return [sys.executable, "-m", "twin_update"]


def prompt_argv(config_path: str | None = None, test: bool = False,
                slot: str | None = None) -> list[str]:
    cmd = twin_update_cmd()
    if config_path:
        cmd += ["--config", config_path]
    cmd.append("prompt")
    if test:
        cmd.append("--inside")
        cmd.append("--test")
    if slot:
        cmd += ["--slot", slot]
    return cmd


def parse_session_environment(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in GUI_KEYS or key in ("XDG_CURRENT_DESKTOP", "WAYLAND_SOCKET"):
            out[key] = value
    return out


def graphical_env(system) -> dict | None:
    """GUI env from the running user session (timer units often lack it).

    Uses ``systemctl --user show-environment`` the same way smoke/reopen
    discover the session, then requires WAYLAND_DISPLAY or DISPLAY.
    """
    env = dict(system.env())
    uid = system.uid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    r = system.run(["systemctl", "--user", "show-environment"], env=env, timeout=15)
    if r.ok:
        env.update(parse_session_environment(r.out))
    if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
        return None
    return env


def window_lock_path(state_dir: str) -> str:
    return os.path.join(state_dir, WINDOW_LOCK)


def _slot(raw: str | None) -> dict:
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def slot_pid_alive(system, path: str) -> bool:
    data = _slot(system.read_text(path))
    pid = data.get("pid")
    return isinstance(pid, int) and system.pid_alive(pid)


def try_begin_window(system, path: str):
    """Exclusive launch slot. None if a window is already open or starting."""
    if slot_pid_alive(system, path):
        return None
    parent = os.path.dirname(path)
    if parent:
        system.makedirs(parent)
    if not system.exists(path):
        system.write_private(path, "{}\n")
    fd = system.try_lockfile(path)
    if fd is None:
        return None
    if slot_pid_alive(system, path):
        system.unlock(fd)
        return None
    return fd


def write_window_pid(system, path: str, pid: int, started: bool = False) -> None:
    system.write_private(path, json.dumps({"pid": pid, "started": started}) + "\n")


def mark_started(system, path: str, pid: int) -> None:
    write_window_pid(system, path, pid, started=True)


def clear_slot(system, path: str) -> None:
    system.write_private(path, "{}\n")


def verify_window(system, path: str, pid: int, wait_s: float = LAUNCH_VERIFY_S) -> bool:
    """True if the launched process stayed up, or the prompt marked itself started."""
    deadline = system.now() + wait_s
    while system.now() < deadline:
        data = _slot(system.read_text(path))
        if data.get("started"):
            return True
        left = deadline - system.now()
        if left <= 0:
            break
        system.sleep(min(0.2, left))
    data = _slot(system.read_text(path))
    return bool(data.get("started") or system.pid_alive(pid))


def warning_line(app_names: list[str]) -> str:
    listed = ", ".join(app_names) if app_names else "the listed apps"
    return f"Updating closes {listed} on BOTH machines for about 10 minutes."


def pending_lines(items: list[dict]) -> list[str]:
    lines = []
    for it in items:
        name = it.get("display") or it.get("app") or "app"
        inst = it.get("installed") or "?"
        cand = it.get("candidate") or "?"
        where = it.get("where") or ""
        extra = f"  ({where})" if where else ""
        lines.append(f"{name} {inst}\u2192{cand}{extra}")
    return lines


def items_from_check(data: dict, apps) -> list[dict]:
    """Build prompt rows from a check report."""
    items = []
    for u in data.get("updates") or []:
        name = u.get("app")
        display = apps[name].display if name in apps else name
        machines = u.get("machines") or {}
        parts = sorted({f"{r.get('installed')}\u2192{r.get('candidate')}" for r in machines.values()})
        where = "both machines" if len(machines) == 2 else ", ".join(sorted(machines))
        inst, cand = "?", "?"
        if parts:
            inst, cand = parts[0].split("\u2192", 1)
        items.append({"app": name, "display": display, "installed": inst,
                      "candidate": cand, "where": where})
    return items


def run_prompt(ui, items: list[dict], *, test: bool = False,
               run_fn: Callable[[], int] | None = None) -> str:
    """Ask in this terminal. Never times out into a yes. Returns run|no|eof|already-updated."""
    if test:
        ui.say(TEST_ONLY)
    if not items:
        ui.say(ALREADY_UPDATED)
        return "already-updated"
    ui.say("")
    for line in pending_lines(items):
        ui.say(line)
    ui.say("")
    ui.say(warning_line([it.get("display") or it.get("app") or "app" for it in items]))
    ui.say("")
    try:
        # No timeout: a timeout must never become an automatic update.
        yes = ui.confirm(UPDATE_NOW)
    except EOFError:
        return "eof"
    if not yes:
        return "no"
    if test:
        ui.say(TEST_NOTHING_RAN)
        if hasattr(ui, "pause"):
            ui.pause("Press Enter to close.")
        return "test"
    if run_fn is not None:
        run_fn()
        if hasattr(ui, "pause"):
            ui.pause("Press Enter to close.")
    return "run"


def extra_notify_body() -> str:
    return EXTRA_NOTIFY_BODY
