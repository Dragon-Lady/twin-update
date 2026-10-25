"""Daily update notice: open a terminal window and ask, do not use notification actions.

COSMIC (and some other desktops) can show a notice without usable buttons.
The main path is therefore a window running ``twin-update prompt``.
A plain, button-less desktop notice is only an extra reminder.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import time
from typing import Callable

APP_NAME = "Twin Update"
WINDOW_LOCK = "prompt.window"
WINDOW_LOCK_TEST = "prompt.window.test"
RECORD_NOTICE_ERROR = "record-notice-error"
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
SUDO_NEEDED = ("sudo password needed but this run has no terminal and none was "
               "supplied; start it from a Twin Update window or a terminal")
UPDATE_FINISHED = "Update finished"
UPDATE_FAILED = "Update failed"
EXIT_MEANING = {
    0: "ok",
    1: "the update failed",
    2: "usage or config error",
    3: "you stopped it",
    4: "doctor found a problem",
    5: "The update stopped before finishing (see above)",
}
GUI_KEYS = ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")

# basename of the resolved launcher -> argv inserted before the inner command.
# gnome-terminal style launchers treat -e as a title/deprecated and need --.
# Use wait/no-fork where known so a client that forks and exits does not look
# like "window exited". x-terminal-emulator stays -e until realpath resolves
# it; that fallback is best-effort and is verified via unit ActiveState, not
# the client's pid.
TERMINAL_FLAGS = {
    "cosmic-term": ["--no-daemon", "-e"],
    "gnome-terminal": ["--wait", "--"],
    "gnome-terminal.wrapper": ["--wait", "--"],
    "kgx": ["--wait", "--"],
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


def start_prompt_window(system, argv: list[str], env: dict) -> tuple[int | None, str | None]:
    """Start the terminal via a user transient unit (the oneshot can then exit).

    ``systemd-run --no-block`` can report MainPID 0 while the unit is still
    activating. Poll briefly; only a unit that is inactive or failed is a
    launch failure. A still-activating unit with MainPID 0 returns 0 so the
    caller can wait for the prompt to mark itself started.
    """
    if not system.which("systemd-run"):
        return None, None
    unit = (f"twin-update-prompt-{int(system.now())}-"
            f"{system.own_pid()}-{os.urandom(2).hex()}")
    started = system.run(
        ["systemd-run", "--user", "--collect", "--quiet", "--no-block",
         f"--unit={unit}", "--", *argv],
        env=env, timeout=30)
    if not started.ok:
        return None, unit
    deadline = system.now() + MAINPID_POLL_S
    pid, state = 0, ""
    while True:
        shown = system.run(
            ["systemctl", "--user", "show", unit,
             "--property=MainPID", "--property=ActiveState"],
            env=env, timeout=10)
        pid, state = parse_unit_show(shown.out)
        if pid > 0:
            return pid, unit
        if state in ("failed", "inactive", "dead"):
            return None, unit
        if system.now() >= deadline:
            break
        system.sleep(MAINPID_STEP_S)
    if state in ("active", "activating") or not state:
        return 0, unit
    return None, unit


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


def record_error_path(state_dir: str) -> str:
    return os.path.join(state_dir, RECORD_NOTICE_ERROR)


def note_record_failure(system, state_dir: str, msg: str) -> None:
    text = (msg or "record failed").strip() or "record failed"
    system.write_private(record_error_path(state_dir), text + "\n")


def clear_record_failure(system, state_dir: str) -> None:
    path = record_error_path(state_dir)
    if hasattr(system, "remove"):
        system.remove(path)
    elif system.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def last_record_error(system, state_dir: str) -> str | None:
    raw = system.read_text(record_error_path(state_dir))
    if not raw:
        return None
    text = raw.strip()
    return text or None


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


def _pid_matches_slot(system, data: dict) -> bool:
    """True only if the stored pid is still the same process we recorded."""
    pid = data.get("pid")
    if not (isinstance(pid, int) and pid > 0 and system.pid_alive(pid)):
        return False
    stored = data.get("start")
    if isinstance(stored, int) and hasattr(system, "pid_start"):
        cur = system.pid_start(pid)
        if cur is not None and cur != stored:
            return False
    return True


def slot_held(system, path: str) -> bool:
    """True if a live, validated window owns the slot, or a launch is starting.

    A pid that has been reused, or a window open more than 24 hours, does not
    hold the slot (doctor still reports the stale window so it can be replaced).
    """
    data = read_slot(system, path)
    if _pid_matches_slot(system, data):
        ts = data.get("opened_at")
        if isinstance(ts, (int, float)) and system.now() - ts > STALE_WINDOW_S:
            return False
        return True
    if data.get("starting") and isinstance(data.get("opened_at"), (int, float)):
        if system.now() - data["opened_at"] < STARTING_SLOT_S:
            return True
        if unit_still_up(system, data.get("unit")):
            return True
    return False


def slot_pid_alive(system, path: str) -> bool:
    data = read_slot(system, path)
    pid = data.get("pid")
    return isinstance(pid, int) and pid > 0 and system.pid_alive(pid)


def slot_pid_reused(system, path: str) -> bool:
    """True when a live pid no longer matches the recorded start time."""
    data = read_slot(system, path)
    pid = data.get("pid")
    return (isinstance(pid, int) and pid > 0 and system.pid_alive(pid)
            and not _pid_matches_slot(system, data))


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
                     opened_at: float | None = None, start: int | None = None,
                     unit: str | None = None, already_locked: bool = False) -> None:
    """Merge slot fields under the slot flock.

    Once the window has marked itself started, keep its pid and start
    ticks; a later launcher write may only add ``unit``.
    """
    fd = None
    if not already_locked and hasattr(system, "try_lockfile"):
        fd = system.try_lockfile(path, blocking=True)
    try:
        prev = read_slot(system, path)
        if prev.get("started"):
            started = True
            if isinstance(prev.get("pid"), int) and prev["pid"] > 0:
                pid = prev["pid"]
            if prev.get("start") is not None:
                start = prev["start"]
        elif (not isinstance(pid, int) or pid <= 0) and isinstance(prev.get("pid"), int) and prev["pid"] > 0:
            pid = prev["pid"]
        ts = opened_at if opened_at is not None else prev.get("opened_at")
        if not isinstance(ts, (int, float)):
            ts = system.now()
        if start is None:
            start = prev.get("start")
        if start is None and isinstance(pid, int) and pid > 0 and hasattr(system, "pid_start"):
            start = system.pid_start(pid)
        payload = {"pid": pid, "started": started, "opened_at": ts,
                   "starting": bool(not started and (not isinstance(pid, int) or pid <= 0))}
        if isinstance(start, int):
            payload["start"] = start
        if unit or prev.get("unit"):
            payload["unit"] = unit or prev.get("unit")
        system.write_inplace(path, json.dumps(payload) + "\n")
    finally:
        if fd is not None and hasattr(system, "unlock"):
            system.unlock(fd)


def mark_started(system, path: str, pid: int) -> None:
    start = system.pid_start(pid) if hasattr(system, "pid_start") else None
    write_window_pid(system, path, pid, started=True, start=start)


def clear_slot(system, path: str) -> None:
    system.write_inplace(path, "{}\n")


def already_open_message(system, path: str) -> str:
    since = slot_opened_at(system, path)
    if since:
        return f"window already open since {since}"
    return "window already open"


def verify_window(system, path: str, pid: int, wait_s: float = LAUNCH_VERIFY_S) -> bool:
    """True if the slot, unit, or (last) the client pid shows a live window.

    A still-activating unit with MainPID 0 is alive. Client pid is only a
    fallback after unit state; a forking launcher pid must not clear a live
    slot.
    """
    deadline = system.now() + wait_s
    while True:
        data = read_slot(system, path)
        if data.get("started") or unit_still_up(system, data.get("unit")):
            return True
        if system.now() >= deadline:
            break
        left = deadline - system.now()
        system.sleep(min(0.2, left))
    return bool(pid > 0 and system.pid_alive(pid))


def slot_stale(system, path: str, limit_s: float = STALE_WINDOW_S) -> tuple[bool, str | None]:
    """True when the real-slot window has been open longer than limit_s."""
    data = read_slot(system, path)
    ts = data.get("opened_at")
    if not _pid_matches_slot(system, data) and not (
            isinstance(data.get("pid"), int) and data["pid"] > 0 and system.pid_alive(data["pid"])):
        return False, None
    if not isinstance(ts, (int, float)):
        return False, None
    if system.now() - ts <= limit_s:
        return False, format_since(ts)
    return True, format_since(ts)


def unit_still_up(system, unit: str | None, env: dict | None = None) -> bool:
    if not unit:
        return False
    shown = system.run(
        ["systemctl", "--user", "show", unit,
         "--property=ActiveState", "--property=LoadState"],
        env=env, timeout=10)
    state = ""
    load = ""
    for line in shown.out.splitlines():
        if line.startswith("ActiveState="):
            state = line.split("=", 1)[1].strip()
        if line.startswith("LoadState="):
            load = line.split("=", 1)[1].strip()
    if load == "not-found":
        return False
    return state in ("active", "activating")


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
    if by_machine:
        listed_both = list(dict.fromkeys(both_items))
        sets = [tuple(dict.fromkeys(names)) for names in by_machine.values()]
        if (len(by_machine) >= 2 and listed_both and not unnamed
                and len(set(sets)) == 1 and set(sets[0]) == set(listed_both)):
            return (f"Updating closes {', '.join(listed_both)} on both machines "
                    "for about 10 minutes.")
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


def _announce_run_result(ui, rc: int, result: str = "", status: int | None = None) -> None:
    if rc == 0:
        ui.say(UPDATE_FINISHED)
        return
    why = EXIT_MEANING.get(rc) or result
    if why in ("", "exit-code", "failed", None):
        why = EXIT_MEANING.get(status) or "the update did not finish"
    ui.say(f"{UPDATE_FAILED}: {why}")


def start_and_follow_run(system, argv: list[str], env: dict, ui) -> int:
    """Start ``twin-update run`` in a user scope that keeps this window's tty.

    The child is in its own session. Ctrl-C and closing this window do not
    stop it. ``systemctl --user stop <unit>`` (SIGTERM) cancels the run and
    reopens apps that were closed. This process ignores SIGINT/SIGHUP/SIGTERM
    while the child exists and waits for it to finish its own cleanup;
    the child is never SIGKILL'd.
    """
    if not system.which("systemd-run"):
        ui.say("systemd-run not found; the run stays in this window.")
        return 1
    unit = (f"twin-update-run-{int(system.now())}-"
            f"{system.own_pid()}-{os.urandom(2).hex()}")
    ui.say(f"update unit: {unit}")
    ui.say("Ctrl-C here does not stop the update.")
    ui.say(f"To cancel and reopen closed apps: systemctl --user stop {unit}")
    prev = {}
    for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
        try:
            prev[sig] = signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
    try:
        runner = system.run_inherit if hasattr(system, "run_inherit") else system.run
        started = runner(
            ["systemd-run", "--user", "--scope", "--quiet",
             f"--unit={unit}", "--", "env", "TWIN_UPDATE_SCOPE=1", *argv],
            env=env, timeout=None)
    finally:
        for sig, old in prev.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass
    rc = 0 if started.ok else (started.rc or 1)
    _announce_run_result(ui, rc, getattr(started, "err", "") or "", rc)
    return rc


def shipped_systemd_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "systemd")


def shipped_unit_text(name: str) -> str | None:
    path = os.path.join(shipped_systemd_dir(), name)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def check_exec_start(service: str | None, home: str) -> str | None:
    """First token of ExecStart=, with %h expanded."""
    if not service:
        return None
    for line in service.splitlines():
        if line.startswith("ExecStart="):
            cmd = line.split("=", 1)[1].strip()
            if not cmd:
                return None
            return cmd.split()[0].replace("%h", home)
    return None


def unit_drift_warnings(installed_service: str | None, installed_timer: str | None,
                        shipped_service: str | None = None) -> list[str]:
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
