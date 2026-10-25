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
import time
from typing import Callable

APP_NAME = "Twin Update"
WINDOW_LOCK = "prompt.window"
WINDOW_LOCK_TEST = "prompt.window.test"
LAUNCH_VERIFY_S = 2.0
MAINPID_POLL_S = 0.6
MAINPID_STEP_S = 0.1
STARTING_SLOT_S = 15.0
STALE_WINDOW_S = 86400.0
ALREADY_UPDATED = "Nothing is pending (the other machine may already have updated)."
TEST_ONLY = "Test only, nothing will be updated"
TEST_NOTHING_RAN = "test only, nothing ran"
EXTRA_NOTIFY_BODY = "Updates waiting: see the Twin Update window"
UPDATE_NOW = "Update now?"
CHECKING = "Checking..."
CLOSE_DOES_NOT_STOP = "Closing this window does not stop the update."
GUI_KEYS = ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")

# basename of the resolved launcher -> argv inserted before the inner command.
# gnome-terminal style launchers treat -e as a title/deprecated and need --.
TERMINAL_FLAGS = {
    "cosmic-term": ["--no-daemon", "-e"],
    "gnome-terminal": ["--"],
    "gnome-terminal.wrapper": ["--"],
    "kgx": ["--"],
    "ptyxis": ["--"],
    "xfce4-terminal": ["-x"],
    "konsole": ["-e"],
    "xterm": ["-e"],
    "alacritty": ["-e"],
    "kitty": ["-e"],
    "x-terminal-emulator": ["-e"],
}
DEFAULT_TERMINAL_FLAGS = ["-e"]

TEST_PENDING = [
    {"display": "Example App", "installed": "1.0.0", "candidate": "1.1.0",
     "jump": "1.0.0\u21921.1.0", "where": "both machines",
     "machines": []},
]


def launcher_name(which: Callable[[str], str | None]) -> str | None:
    for name in ("cosmic-term", "x-terminal-emulator"):
        if which(name):
            return name
    return None


def resolve_launcher_base(which: Callable[[str], str | None],
                          realpath: Callable[[str], str] | None,
                          name: str) -> str:
    path = which(name)
    if not path:
        return name
    target = realpath(path) if realpath is not None else path
    return os.path.basename(target) or name


def terminal_flags(base: str) -> list[str]:
    return list(TERMINAL_FLAGS.get(base) or DEFAULT_TERMINAL_FLAGS)


def parse_unit_show(text: str) -> tuple[int, str]:
    """Return (MainPID, ActiveState) from ``systemctl show`` output."""
    pid, state = 0, ""
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "MainPID" and value.strip().isdigit():
            pid = int(value.strip())
        elif key == "ActiveState":
            state = value.strip()
        elif key == "MainPID" and value.strip() == "0":
            pid = 0
    # bare "123" from older callers / fakes that only print MainPID
    stripped = text.strip()
    if pid == 0 and stripped.isdigit():
        pid = int(stripped)
    return pid, state


def start_prompt_window(system, argv: list[str], env: dict) -> int | None:
    """Start the terminal via a user transient unit (the oneshot can then exit).

    ``systemd-run --no-block`` can report MainPID 0 while the unit is still
    activating. Poll briefly; only a unit that is inactive or failed is a
    launch failure. A still-activating unit with MainPID 0 returns 0 so the
    caller can wait for the prompt to mark itself started.
    """
    if not system.which("systemd-run"):
        return None
    unit = f"twin-update-prompt-{int(system.now())}"
    started = system.run(
        ["systemd-run", "--user", "--collect", "--quiet", "--no-block",
         f"--unit={unit}", "--", *argv],
        env=env, timeout=30)
    if not started.ok:
        return None
    deadline = system.now() + MAINPID_POLL_S
    pid, state = 0, ""
    while True:
        shown = system.run(
            ["systemctl", "--user", "show", unit,
             "--property=MainPID", "--property=ActiveState"],
            env=env, timeout=10)
        pid, state = parse_unit_show(shown.out)
        if pid > 0:
            return pid
        if state in ("failed", "inactive", "dead"):
            return None
        if system.now() >= deadline:
            break
        system.sleep(MAINPID_STEP_S)
    if state in ("active", "activating") or not state:
        return 0
    return None


def launcher_argv(which: Callable[[str], str | None], inner: list[str],
                  realpath: Callable[[str], str] | None = None) -> list[str] | None:
    """Build the terminal argv. Prefer cosmic-term, then x-terminal-emulator."""
    name = launcher_name(which)
    if not name:
        return None
    script = "exec " + shlex.join(inner)
    base = resolve_launcher_base(which, realpath, name)
    flags = terminal_flags(base if name == "x-terminal-emulator" else name)
    if name == "cosmic-term":
        flags = terminal_flags("cosmic-term")
    return [name, *flags, "bash", "-lc", script]


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


def run_argv(config_path: str | None = None) -> list[str]:
    cmd = twin_update_cmd()
    if config_path:
        cmd += ["--config", config_path]
    cmd += ["run", "--no-detach"]
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


def window_lock_path(state_dir: str, test: bool = False) -> str:
    return os.path.join(state_dir, WINDOW_LOCK_TEST if test else WINDOW_LOCK)


def _slot(raw: str | None) -> dict:
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def read_slot(system, path: str) -> dict:
    return _slot(system.read_text(path))


def format_since(ts, now: float | None = None) -> str | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def slot_opened_at(system, path: str) -> str | None:
    return format_since(read_slot(system, path).get("opened_at"))


def slot_held(system, path: str) -> bool:
    """True if a live window owns the slot, or a launch is still starting."""
    data = read_slot(system, path)
    pid = data.get("pid")
    if isinstance(pid, int) and pid > 0 and system.pid_alive(pid):
        return True
    if data.get("starting") and isinstance(data.get("opened_at"), (int, float)):
        if system.now() - data["opened_at"] < STARTING_SLOT_S:
            return True
    return False


def slot_pid_alive(system, path: str) -> bool:
    data = read_slot(system, path)
    pid = data.get("pid")
    return isinstance(pid, int) and pid > 0 and system.pid_alive(pid)


def try_begin_window(system, path: str):
    """Exclusive launch slot. None if a window is already open or starting."""
    if slot_held(system, path):
        return None
    parent = os.path.dirname(path)
    if parent:
        system.makedirs(parent)
    if not system.exists(path):
        system.write_inplace(path, "{}\n")
    fd = system.try_lockfile(path)
    if fd is None:
        return None
    if slot_held(system, path):
        system.unlock(fd)
        return None
    return fd


def write_window_pid(system, path: str, pid: int, started: bool = False,
                     opened_at: float | None = None) -> None:
    prev = read_slot(system, path)
    ts = opened_at if opened_at is not None else prev.get("opened_at")
    if not isinstance(ts, (int, float)):
        ts = system.now()
    payload = {"pid": pid, "started": started, "opened_at": ts,
               "starting": bool(not started and pid <= 0)}
    system.write_inplace(path, json.dumps(payload) + "\n")


def mark_started(system, path: str, pid: int) -> None:
    write_window_pid(system, path, pid, started=True)


def clear_slot(system, path: str) -> None:
    system.write_inplace(path, "{}\n")


def already_open_message(system, path: str) -> str:
    since = slot_opened_at(system, path)
    if since:
        return f"window already open since {since}"
    return "window already open"


def verify_window(system, path: str, pid: int, wait_s: float = LAUNCH_VERIFY_S) -> bool:
    """True if the launched process stayed up, or the prompt marked itself started."""
    deadline = system.now() + wait_s
    while system.now() < deadline:
        data = read_slot(system, path)
        if data.get("started"):
            return True
        left = deadline - system.now()
        if left <= 0:
            break
        system.sleep(min(0.2, left))
    data = read_slot(system, path)
    return bool(data.get("started") or (pid > 0 and system.pid_alive(pid)))


def slot_stale(system, path: str, limit_s: float = STALE_WINDOW_S) -> tuple[bool, str | None]:
    """True when the real-slot window has been open longer than limit_s."""
    data = read_slot(system, path)
    pid = data.get("pid")
    ts = data.get("opened_at")
    if not (isinstance(pid, int) and pid > 0 and system.pid_alive(pid)):
        return False, None
    if not isinstance(ts, (int, float)):
        return False, None
    if system.now() - ts <= limit_s:
        return False, format_since(ts)
    return True, format_since(ts)


def warning_line(items: list[dict]) -> str:
    """Per-machine closures. Say 'both machines' only when both need the update."""
    by_machine: dict[str, list[str]] = {}
    both_items = []
    unnamed = []
    for it in items:
        name = it.get("display") or it.get("app") or "app"
        machines = list(it.get("machines") or [])
        where = it.get("where") or ""
        if len(machines) >= 2 or where == "both machines":
            both_items.append(name)
            for m in machines:
                by_machine.setdefault(m, []).append(name)
        elif machines:
            for m in machines:
                by_machine.setdefault(m, []).append(name)
        elif where:
            for m in [p.strip() for p in where.split(",") if p.strip()]:
                by_machine.setdefault(m, []).append(name)
        else:
            unnamed.append(name)
    if both_items and len(by_machine) != 1:
        listed = ", ".join(dict.fromkeys(both_items))
        return f"Updating closes {listed} on both machines for about 10 minutes."
    if by_machine:
        parts = [f"{label}: {', '.join(dict.fromkeys(names))}"
                 for label, names in sorted(by_machine.items())]
        return "Updating closes " + "; ".join(parts) + " for about 10 minutes."
    listed = ", ".join(unnamed) if unnamed else "the listed apps"
    return f"Updating closes {listed} for about 10 minutes."


def pending_lines(items: list[dict]) -> list[str]:
    lines = []
    for it in items:
        name = it.get("display") or it.get("app") or "app"
        jump = it.get("jump")
        if not jump:
            inst = it.get("installed") or "?"
            cand = it.get("candidate") or "?"
            jump = f"{inst}\u2192{cand}"
        where = it.get("where") or ""
        extra = f"  ({where})" if where else ""
        lines.append(f"{name} {jump}{extra}")
    return lines


def items_from_check(data: dict, apps) -> list[dict]:
    """Build prompt rows from a check report.

    Version jumps are joined with " / " the same way the check summary does.
    ``where`` is "both machines" only when both machines need that app.
    """
    items = []
    for u in data.get("updates") or []:
        name = u.get("app")
        display = apps[name].display if name in apps else name
        machines = u.get("machines") or {}
        parts = sorted({f"{r.get('installed')}\u2192{r.get('candidate')}"
                        for r in machines.values()})
        jump = " / ".join(parts) if parts else "?\u2192?"
        labels = sorted(machines)
        where = "both machines" if len(labels) == 2 else ", ".join(labels)
        inst, cand = "?", "?"
        if parts:
            first = parts[0].split("\u2192", 1)
            if len(first) == 2:
                inst, cand = first
        items.append({"app": name, "display": display, "installed": inst,
                      "candidate": cand, "jump": jump, "where": where,
                      "machines": labels})
    return items


def error_lines(errors: dict) -> list[str]:
    return [f"{label}: {why}" for label, why in sorted(errors.items())]


def update_question(errors: dict | None) -> str:
    """Question text; include unreachable-machine status when the check had errors."""
    if not errors:
        return UPDATE_NOW
    bits = [f"{label}: unreachable, an update would wait or fail"
            if "timed out" in str(why).lower() or "unreachable" in str(why).lower()
            or "name lookup" in str(why).lower() or "connection refused" in str(why).lower()
            or "login refused" in str(why).lower() or "ssh" in str(why).lower()
            else f"{label}: {why}"
            for label, why in sorted(errors.items())]
    return "; ".join(bits) + ". " + UPDATE_NOW


def _pause(ui, msg: str = "Press Enter to close.") -> None:
    if hasattr(ui, "pause"):
        ui.pause(msg)


def run_prompt(ui, items: list[dict], *, test: bool = False,
               run_fn: Callable[[], int] | None = None,
               errors: dict | None = None,
               on_shown: Callable[[], None] | None = None) -> str:
    """Ask in this terminal. Never times out into a yes.

    Returns run|no|eof|already-updated|error|test.
    Waits for Enter on error, exception, nothing-pending, and after a run.
    Only an explicit N/no closes without a pause.
    ``on_shown`` runs after the question is on the screen (record the day then).
    """
    errors = dict(errors or {})
    try:
        if test:
            ui.say(TEST_ONLY)
        if errors and not items:
            for line in error_lines(errors):
                ui.say(line)
            _pause(ui)
            return "error"
        if not items:
            ui.say(ALREADY_UPDATED)
            _pause(ui)
            return "already-updated"
        ui.say("")
        for line in pending_lines(items):
            ui.say(line)
        ui.say("")
        ui.say(warning_line(items))
        ui.say("")
        question = update_question(errors)
        if on_shown is not None:
            on_shown()
        try:
            # No timeout: a timeout must never become an automatic update.
            yes = ui.confirm(question)
        except EOFError:
            return "eof"
        if not yes:
            return "no"
        if test:
            ui.say(TEST_NOTHING_RAN)
            _pause(ui)
            return "test"
        if run_fn is not None:
            ui.say(CLOSE_DOES_NOT_STOP)
            run_fn()
            _pause(ui)
        return "run"
    except EOFError:
        _pause(ui)
        return "eof"
    except Exception as exc:
        ui.say(str(exc))
        _pause(ui)
        return "error"


def follow_unit(system, unit: str, env: dict, ui, poll_s: float = 0.5) -> int:
    """Print new journal lines until the unit leaves active/activating."""
    seen = 0
    while True:
        j = system.run(["journalctl", "--user", "-u", unit, "--no-pager", "-o", "cat"],
                       env=env, timeout=30)
        text = j.out if j.ok else ""
        lines = text.splitlines()
        for line in lines[seen:]:
            ui.say(line)
        seen = len(lines)
        a = system.run(["systemctl", "--user", "is-active", unit], env=env, timeout=10)
        state = a.out.strip()
        if state not in ("active", "activating"):
            shown = system.run(
                ["systemctl", "--user", "show", unit,
                 "--property=Result", "--property=ExecMainStatus"],
                env=env, timeout=10)
            rc = 0
            for line in shown.out.splitlines():
                if line.startswith("ExecMainStatus=") and line.split("=", 1)[1].isdigit():
                    rc = int(line.split("=", 1)[1])
                if line.startswith("Result=") and line.split("=", 1)[1] not in ("", "success"):
                    rc = rc or 1
            return rc
        system.sleep(poll_s)


def start_and_follow_run(system, argv: list[str], env: dict, ui) -> int:
    """Start ``twin-update run`` in its own transient unit and follow it.

    Closing the prompt window must not signal the run. The unit lives under
    the user systemd instance, not the terminal's process group.
    """
    ui.say(CLOSE_DOES_NOT_STOP)
    if not system.which("systemd-run"):
        ui.say("systemd-run not found; the run stays in this window.")
        return 1
    unit = f"twin-update-run-{int(system.now())}"
    started = system.run(
        ["systemd-run", "--user", "--collect", "--quiet", "--no-block",
         f"--unit={unit}", "--", "env", "TWIN_UPDATE_SCOPE=1", *argv],
        env=env, timeout=30)
    if not started.ok:
        ui.say("could not start the update unit")
        return 1
    ui.say(f"update unit: {unit}")
    return follow_unit(system, unit, env, ui)


def extra_notify_body() -> str:
    return EXTRA_NOTIFY_BODY


def shipped_systemd_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "systemd")


def shipped_unit_text(name: str) -> str | None:
    path = os.path.join(shipped_systemd_dir(), name)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def unit_drift_warnings(installed_service: str | None, installed_timer: str | None,
                        shipped_service: str | None = None,
                        shipped_timer: str | None = None) -> list[str]:
    """Warnings when an installed check unit differs in a way that matters."""
    warnings = []
    service = installed_service or ""
    timer = installed_timer or ""
    if installed_service is not None:
        if "SuccessExitStatus=10" not in service:
            warnings.append("check service lacks SuccessExitStatus=10 (check exits 10 when updates exist)")
        if "check --notify" not in service and "check" not in service:
            warnings.append("check service ExecStart does not run twin-update check")
        shipped = shipped_service if shipped_service is not None else shipped_unit_text(
            "twin-update-check.service")
        if shipped and "Type=oneshot" in shipped and "Type=oneshot" not in service:
            warnings.append("check service Type is not oneshot")
    if installed_timer is not None:
        if "Persistent=true" not in timer.replace(" ", "").replace("yes", "true"):
            if "Persistent=true" not in timer and "Persistent=yes" not in timer:
                warnings.append("check timer lacks Persistent=true (missed runs will not catch up)")
        if "OnCalendar=" not in timer:
            warnings.append("check timer has no OnCalendar=")
    return warnings
