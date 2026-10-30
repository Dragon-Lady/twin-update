"""Command line: twin-update status|run|rollback|holds|unhold|check|prompt|doctor|local."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import select
import shutil
import signal
import sys
import time

from twin_update import __version__, notice, orchestrator
from twin_update.config import ConfigError, load
from twin_update.engine import Engine
from twin_update.peer import LocalPeer, PeerError, SshPeer, serve
from twin_update.system import System


class ConsoleUI:
    def say(self, msg: str) -> None:
        try:
            print(msg, flush=True)
        except OSError:  # terminal went away (e.g. the app hosting it was closed)
            pass

    def can_prompt(self) -> bool:
        return sys.stdin.isatty()

    def getpass(self, prompt: str) -> str:
        if not sys.stdin.isatty():
            raise EOFError(notice.SUDO_NEEDED)
        # Read this process's stdin (echo off). getpass() uses /dev/tty and
        # would miss a systemd-run --scope that inherited the window tty.
        fd = sys.stdin.fileno()
        try:
            import termios
            old = termios.tcgetattr(fd)
            cfg = list(old)
            cfg[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSADRAIN, cfg)
            try:
                print(prompt, end="", flush=True)
                line = sys.stdin.readline()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                print(flush=True)
        except (OSError, AttributeError):
            try:
                return getpass.getpass(prompt)
            except (EOFError, OSError):
                raise EOFError(notice.SUDO_NEEDED) from None
        if not line:
            raise EOFError(notice.SUDO_NEEDED)
        return line.rstrip("\n")

    def confirm(self, prompt: str, timeout_s: float | None = None) -> bool:
        if not sys.stdin.isatty():
            raise EOFError("no terminal for confirm")
        try:
            # Read inherited stdin (same fd as getpass). input() would use
            # the process tty and miss a systemd-run --scope window.
            print(prompt + " [y/N] ", end="", flush=True)
            if timeout_s is not None:
                ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
                if not ready:
                    print("(timed out; defaulting to no)", flush=True)
                    return False
            line = sys.stdin.readline()
            if not line:
                raise EOFError("no terminal for confirm")
            return line.strip().lower() in ("y", "yes")
        except (EOFError, OSError) as exc:
            if isinstance(exc, EOFError):
                raise
            return False

    def pause(self, msg: str = "Press Enter to close.") -> None:
        try:
            input(msg + " ")
        except (EOFError, OSError):
            pass


def _detach_if_needed(args) -> None:
    """Re-exec in its own systemd user scope so closing the app that hosts our
    terminal (e.g. Grok Bot) does not kill the run. The scope ends with us."""
    if args.no_detach or os.environ.get("TWIN_UPDATE_SCOPE") == "1":
        # Closing the prompt window sends SIGHUP; ignore it so apt continues.
        # SIGTERM (systemctl --user stop) is handled in the orchestrator.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        return
    if not shutil.which("systemd-run"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        return
    unit = f"twin-update-run-{int(time.time())}-{os.getpid()}-{os.urandom(2).hex()}"
    argv = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}",
            "-p", "TimeoutStopSec=5400", "--",
            "env", "TWIN_UPDATE_SCOPE=1", sys.executable, *sys.argv]
    os.execvp(argv[0], argv)


def _without_label(label: str, why: str) -> str:
    """Drop a leading "<label>: " so output lines don't repeat the machine label."""
    prefix = f"{label}: "
    while why.startswith(prefix):
        why = why[len(prefix):]
    return why


def _argv_cmd(argv) -> str | None:
    """First positional token, skipping global flags. Used to hide internal commands."""
    args = list(argv) if argv is not None else sys.argv[1:]
    takes_value = {"--config"}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in takes_value and i + 1 < len(args):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok
    return None


def _build(args, ui, system=None, peer_factory=SshPeer):
    system = system or System()
    cfg = load(args.config)
    me = cfg.self_label(system.whoami(), system.machine_id())
    peer = cfg.peer_label(me)
    peers = {me: LocalPeer(Engine(cfg, system, me, log=lambda m: None))}
    unreachable = {}
    if not getattr(args, "local_only", False):
        mach = cfg.machines[peer]
        sp = None
        try:
            sp = peer_factory(peer, mach.ssh_target, mach.remote_command)
            sp.call("identity")
            peers[peer] = sp
        except (PeerError, OSError, ValueError) as exc:
            unreachable[peer] = _without_label(peer, str(exc))
            if sp is not None:
                try:
                    (getattr(sp, "abort", None) or sp.proc.terminate)()
                except Exception:
                    pass
    else:
        unreachable[peer] = "skipped (--local-only)"
    return cfg, orchestrator.Session(cfg, peers, [me, peer], ui), unreachable, me, peer


def _print(ui, data, as_json: bool) -> None:
    if as_json:
        ui.say(json.dumps(data, indent=2, sort_keys=True))
        return
    kind = data.get("kind")
    if kind == "status":
        ui.say(f"{'machine':8} {'app':9} {'installed':22} {'candidate':22} run  rollback      hold")
        for l, st in data["machines"].items():
            for a, r in st["apps"].items():
                ui.say(f"{l:8} {a:9} {str(r['installed']):22} {str(r['candidate']):22} "
                       f"{'yes' if r['running'] else 'no ':4} {','.join(r['rollback']) or '-':13} "
                       f"{r['hold'] or '-'}")
            if st.get("other_holds"):
                ui.say(f"{l:8} other held packages (not touched): {', '.join(st['other_holds'])}")
        for l, why in data.get("unreachable", {}).items():
            ui.say(f"{l:8} unreachable: {why}")
        return
    if kind == "doctor":
        for l, checks in data.get("machines", {}).items():
            if "error" in checks:
                ui.say(f"{l:8} error: {checks['error']}")
                continue
            for k in sorted(checks):
                v = checks[k]
                if k == "locks":
                    for path, state in v.items():
                        ui.say(f"{l:8} lock {path}: {state}")
                elif k == "notify_tool":
                    ui.say(f"{l:8} notify: {v or 'none (info only)'}")
                elif k == "terminal_launcher":
                    ui.say(f"{l:8} terminal: {v or 'NO'}")
                elif k == "check_timer_next":
                    ui.say(f"{l:8} timer next: {v or 'unknown'}")
                elif k == "last_window":
                    ui.say(f"{l:8} last window: {v or 'none'}")
                elif k == "window_open_since":
                    if v:
                        ui.say(f"{l:8} window open since: {v}")
                elif k == "rollback_holds":
                    if v:
                        ui.say(f"{l:8} rollback holds: {'; '.join(v)}")
                elif k == "slot_pid_reused":
                    if v:
                        ui.say(f"{l:8} slot pid reused: yes")
                elif k == "record_notice_error":
                    if v:
                        ui.say(f"{l:8} record notice: {v}")
                elif k == "dpkg_interrupted":
                    if v:
                        ui.say(f"{l:8} dpkg interrupted: run sudo dpkg --configure -a")
                elif k == "bad_state":
                    for item in (v or []):
                        ui.say(f"{l:8} state file: {item}")
                elif k in ("locks_ok", "window_stale", "unit_warnings"):
                    continue
                elif k != "locks_ok":
                    ui.say(f"{l:8} {k:18} {'ok' if v else 'NO'}")
        for l, why in data.get("unreachable", {}).items():
            ui.say(f"{l:8} unreachable: {why}")
        for l, ws in data.get("warnings", {}).items():
            for w in ws:
                ui.say(f"{l:8} warning: {w}")
        for l, ps in data.get("problems", {}).items():
            ui.say(f"{l:8} problems: {'; '.join(ps)}")
        ui.say("result: all checks ok" if data.get("ok") else "result: problems found (exit 4)")
        return
    if kind == "prompt":
        if data.get("window"):
            ui.say(f"window: {data['window']}")
        if data.get("launcher"):
            ui.say(f"launcher: {data['launcher']}")
        if data.get("answer") == "test":
            ui.say(notice.TEST_NOTHING_RAN)
        elif data.get("answer") is not None:
            ui.say(f"answer: {data['answer']}")
        elif data.get("opened"):
            ui.say("answer: given in the Twin Update window")
        return
    if kind == "check":
        for l, m in data.get("machines", {}).items():
            ui.say(f"{l:8} {_lists_line(m.get('lists'), m.get('apt_lists_age_h'))}")
        for line in data.get("summary", []):
            ui.say(line)
        for l, why in data.get("errors", {}).items():
            ui.say(f"{l:8} error: {why}")
        n = data.get("notification")
        if n:
            ui.say(f"window: {n}")
        ui.say({"updates": "result: updates available (run: twin-update run)",
                "none": "result: no updates",
                "error": "result: check incomplete"}[data["result"]])
        return
    if kind == "holds":
        for l, h in data.get("machines", {}).items():
            managed = h.get("managed", {})
            ui.say(f"{l:8} app holds: " + (", ".join(f"{a} ({o})" for a, o in sorted(managed.items()))
                                           or "none"))
            if h.get("other"):
                ui.say(f"{l:8} other held packages (not touched): {', '.join(h['other'])}")
        return
    if kind == "unhold":
        for l, r in data.get("machines", {}).items():
            ui.say(f"{l:8} {r.get('action', r.get('error', '?'))}")
        return
    printed = False
    for line in data.get("summary", []):
        ui.say(line)
        printed = True
    for b in data.get("blockers", []):
        ui.say(f"blocker: {b}")
    if data.get("result"):
        ui.say(f"result: {data['result']}")
    if data.get("error") and data.get("result") != "aborted":
        ui.say(f"error: {data['error']}")
    if not (printed or data.get("blockers") or data.get("result") or data.get("error")):
        # Never stay silent: fall back to the JSON report.
        ui.say(json.dumps(data, indent=2, sort_keys=True))


def _helpers_line(data: dict) -> str:
    c = data["counts"]
    words = {
        "HELPERS_NONE": "No leftover Cursor sandbox helpers.",
        "HELPERS_CLEARED": "Leftover Cursor sandbox helpers were idle and are now closed.",
        "HELPERS_WOULD_CLOSE": "Dry run: the idle helpers would get a stop signal (SIGTERM).",
        "HELPERS_ACTIVE": "Some helpers look busy or could not be read, so they were left alone.",
        "HELPERS_NEED_USER_SESSION": "Nothing was sent.",
        "HELPERS_STILL_RUNNING": "Some helpers are still running.",
    }
    line = (f"{words.get(data['status'], data['status'])} (idle {c['ORPHANED_IDLE']}, "
            f"busy {c['ACTIVE']}, unreadable {c['UNKNOWN']})")
    if data.get("hint"):
        line += "\n" + data["hint"]
    return line


def main(argv=None, ui=None, system=None, peer_factory=SshPeer) -> int:
    ui = ui or ConsoleUI()
    ap = argparse.ArgumentParser(prog="twin-update", description=__doc__)
    ap.add_argument("--version", action="version", version=f"twin-update {__version__}")
    ap.add_argument("--config", default=None, help="config path (default ~/.config/twin-update/config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, local=True):
        p.add_argument("--json", action="store_true", help="print the full JSON report")
        if local:
            p.add_argument("--local-only", action="store_true",
                           help="this machine only (no ssh); run/rollback allow it with --dry-run only")

    p = sub.add_parser("status"); common(p)
    p = sub.add_parser("run"); common(p)
    p.add_argument("--apps", default=None, help="comma list (default: all enabled)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-smoke", action="store_true", help="skip the launch smoke test")
    p.add_argument("--no-detach", action="store_true")
    p = sub.add_parser("rollback"); common(p)
    p.add_argument("app")
    p.add_argument("--machine", default="both")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-hold", action="store_true")
    p.add_argument("--no-smoke", action="store_true")
    p.add_argument("--no-detach", action="store_true")
    p = sub.add_parser("holds"); common(p)
    p = sub.add_parser("unhold"); common(p)
    p.add_argument("app")
    p.add_argument("--machine", default="both")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("check", help="read-only update check on both machines (no sudo)")
    common(p)
    p.add_argument("--no-fetch", action="store_true",
                   help="use the system apt lists as they are (no user-level fetch)")
    p.add_argument("--notify", action="store_true",
                   help="open a terminal prompt on this machine if updates are available")
    p = sub.add_parser("prompt", help="ask whether to run an update (used by check --notify)")
    common(p)
    p.add_argument("--test", action="store_true",
                   help="open a harmless test window; nothing is updated")
    p.add_argument("--inside", action="store_true",
                   help="run the prompt in this terminal (do not open another window)")
    p.add_argument("--slot", default=None, help=argparse.SUPPRESS)
    p = sub.add_parser("doctor"); common(p)
    p = sub.add_parser("close-helpers",
                       help="this machine only: check leftover Cursor sandbox helpers are idle, "
                            "then SIGTERM the idle ones (run from your own terminal)")
    p.add_argument("--json", action="store_true", help="print the full JSON report")
    p.add_argument("--dry-run", action="store_true", help="check only; send nothing")
    p = sub.add_parser("local", help="per-machine engine (used by the peer over ssh)")
    p.add_argument("--serve", action="store_true")
    p.add_argument("--as", dest="as_label", default=None)
    # Hide the internal raft helper from --help (SUPPRESS prints as "==SUPPRESS==").
    if _argv_cmd(argv) == "raft-upload":
        p = sub.add_parser("raft-upload")
        p.add_argument("snapshot")

    args = ap.parse_args(argv)
    system = system or System()

    try:
        if args.cmd == "raft-upload":
            from twin_update.raft import upload
            cfg = load(args.config)
            me = cfg.self_label(system.whoami(), system.machine_id())
            return 0 if upload(Engine(cfg, system, me), args.snapshot)["ok"] else 1
        if args.cmd == "close-helpers":
            cfg = load(args.config)
            me = cfg.self_label(system.whoami(), system.machine_id())
            data = Engine(cfg, system, me, log=ui.say).close_helpers("cursor", dry=args.dry_run)
            if args.json:
                ui.say(json.dumps(data, indent=2, sort_keys=True))
            else:
                ui.say(_helpers_line(data))
            ok = ("HELPERS_NONE", "HELPERS_CLEARED", "HELPERS_WOULD_CLOSE")
            return 0 if data["status"] in ok else 1
        if args.cmd == "local":
            cfg = load(args.config)
            me = cfg.self_label(system.whoami(), system.machine_id())
            if args.as_label and args.as_label != me:
                ui.say(json.dumps({"id": None, "ok": False,
                                   "error": f"identity mismatch: this machine is {me}"}))
                return 2
            if not args.serve:
                ui.say("use --serve")
                return 2
            return serve(Engine(cfg, system, me), sys.stdin.buffer, sys.stdout.buffer)

        if args.cmd == "prompt" and args.test and not args.inside:
            data = _prompt_test_launch(ui, system, args.config)
            _print(ui, data, getattr(args, "json", False))
            return 0 if data.get("opened") else 1
        if args.cmd == "prompt" and args.test and args.inside:
            data = _prompt_inside(ui, test=True, slot=args.slot, system=system)
            _print(ui, data, getattr(args, "json", False))
            return data.get("exit_code", 0)

        if args.cmd in ("run", "rollback") and not args.dry_run:
            if getattr(args, "local_only", False):
                ui.say("--local-only is allowed only with --dry-run")
                return 2
            _detach_if_needed(args)

        if args.cmd == "prompt":
            ui.say(notice.CHECKING)
            if getattr(args, "slot", None):
                notice.mark_started(system, args.slot, system.own_pid())
        cfg, sess, unreachable, me, peer = _build(args, ui, system, peer_factory)
        try:
            if args.cmd == "status":
                data = orchestrator.status(sess)
                data["unreachable"] = unreachable
            elif args.cmd == "run":
                apps = args.apps.split(",") if args.apps else list(cfg.enabled)
                for a in apps:
                    if a not in cfg.enabled:
                        ui.say(f"app not enabled in config: {a}")
                        return 2
                data = orchestrator.run(sess, apps, args.dry_run, smoke=not args.no_smoke,
                                        unreachable=unreachable)
            elif args.cmd == "rollback":
                labels = _labels(args.machine, me, peer, sess)
                data = orchestrator.rollback(sess, args.app, labels, args.dry_run,
                                             smoke=not args.no_smoke, hold=not args.no_hold)
            elif args.cmd == "check":
                data = orchestrator.check(sess, unreachable, fresh=not args.no_fetch)
                if args.notify:
                    data["notification"] = _check_notify(sess, me, data, system, args.config)
            elif args.cmd == "prompt":
                data = _prompt_here(args, ui, sess, unreachable, system, peer_factory)
            elif args.cmd == "holds":
                data = orchestrator.holds(sess)
            elif args.cmd == "unhold":
                data = orchestrator.unhold(sess, args.app, _labels(args.machine, me, peer, sess),
                                           args.dry_run)
            else:
                data = orchestrator.doctor(sess, unreachable)
        finally:
            sess.close()
    except ConfigError as exc:
        ui.say(f"config error: {exc}")
        if getattr(args, "cmd", None) == "prompt" and hasattr(ui, "pause"):
            ui.pause("Press Enter to close.")
        return 2
    except PeerError as exc:
        ui.say(f"error: {exc}")
        if getattr(args, "cmd", None) == "prompt" and hasattr(ui, "pause"):
            ui.pause("Press Enter to close.")
        return 1
    except EOFError as exc:
        ui.say(str(exc) if str(exc) else notice.SUDO_NEEDED)
        return 1
    except KeyboardInterrupt:
        ui.say("stopped")
        return 1
    except Exception as exc:
        import traceback
        text = traceback.format_exc()
        log_path = None
        try:
            home = system.home()
            state = os.path.join(home, ".local/state/twin-update")
            log_path = os.path.join(state, "last-error.log")
            system.write_private(log_path, text)
        except Exception:
            log_path = None
        ui.say(f"internal error: {exc}")
        if log_path:
            ui.say(f"details: {log_path}")
        if getattr(args, "cmd", None) == "prompt" and hasattr(ui, "pause"):
            ui.pause("Press Enter to close.")
        return 1
    _print(ui, data, args.json)
    if data.get("kind") == "check":
        return data["exit_code"]
    if data.get("kind") == "prompt":
        return data.get("exit_code", 0)
    if data.get("kind") == "doctor":
        return 0 if data.get("ok") else 4
    res = data.get("result")
    if res in (None, "ok", "dry-run"):
        return 0
    if data.get("interrupted"):
        return 3
    if res == "aborted":
        return 5
    return 1


def _age(h) -> str:
    if h is None:
        return "at an unknown time"
    if h < 1:
        return f"{int(round(h * 60))} min ago"
    return f"{h:.1f} h ago"


def _size(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def _lists_line(lists: dict | None, age_h) -> str:
    if not lists:  # peer running 0.2.0 (status fallback)
        return "package lists: system (older twin-update there)"
    if lists["source"] == "fresh":
        return (f"package lists: fetched fresh {_age(lists['age_h'])} for "
                f"{', '.join(lists.get('apps', [])) or 'no app'} "
                f"(user cache {lists.get('cache')}, {_size(lists.get('cache_bytes', 0))})")
    line = f"package lists: system lists, last refreshed {_age(lists['age_h'])}"
    if lists.get("fallback_reason"):
        line += f" (fresh fetch failed: {lists['fallback_reason']})"
    return line


def _check_notify(sess, me: str, data: dict, system, config_path: str | None) -> str:
    """Open a prompt window on this machine. Same window at most once per day."""
    local = sess.peers[me]
    if data["updates"]:
        kind, sig = "updates", "\n".join(data["summary"])
    elif data["errors"]:
        if not local.call("notice_due", kind="error", signature=",".join(sorted(data["errors"]))):
            return "already noted today"
        body = "\n".join(f"{l}: {why}" for l, why in sorted(data["errors"].items()))
        r = local.call("notify", summary="Twin Update: check incomplete", body=body)
        if r.get("ok"):
            local.call("record_notice", kind="error", signature=",".join(sorted(data["errors"])))
            return "check incomplete (no window)"
        return f"failed ({r.get('reason') or r.get('tool')})"
    else:
        return "none needed"
    if not local.call("notice_due", kind=kind, signature=sig):
        return "already opened a window today"
    env = notice.graphical_env(system)
    if not env:
        return "no graphical session; not retried until the next timer run"
    slot = notice.window_lock_path(sess.cfg.state_dir, test=False)
    token = notice.try_begin_window(system, slot)
    if token is None:
        return notice.already_open_message(system, slot)
    try:
        inner = notice.prompt_argv(config_path, test=False, slot=slot)
        argv = notice.launcher_argv(system.which, inner, realpath=system.realpath)
        if not argv:
            return "failed (no cosmic-term or x-terminal-emulator)"
        pid, unit = notice.start_prompt_window(system, argv, env)
        if pid is None:
            return "failed (could not start the window)"
        if not _keep_launched_window(system, slot, pid, unit, env):
            return "window exited immediately; not recorded"
        # The day is recorded inside the window after the question is shown.
        try:
            local.call("notify", summary=APP_NAME, body=notice.EXTRA_NOTIFY_BODY)
        except Exception:
            pass
        return "opened a window"
    finally:
        system.unlock(token)


APP_NAME = notice.APP_NAME


def _keep_launched_window(system, slot: str, pid: int | None, unit: str | None,
                          env: dict | None) -> bool:
    """Record the launch. Do not clear a pid-0 slot while the unit is still up."""
    notice.write_window_pid(system, slot, pid if isinstance(pid, int) else 0,
                            started=False, unit=unit, already_locked=True)
    if notice.verify_window(system, slot, pid if isinstance(pid, int) else 0):
        return True
    if notice.unit_still_up(system, unit, env):
        return True
    notice.clear_slot(system, slot)
    return False


def _open_prompt_window(system, config_path: str | None, state_dir: str,
                        test: bool) -> dict:
    env = notice.graphical_env(system)
    if not env:
        return {"kind": "prompt", "opened": False, "window": "no graphical session",
                "answer": None}
    slot = notice.window_lock_path(state_dir, test=test)
    token = notice.try_begin_window(system, slot)
    if token is None:
        return {"kind": "prompt", "opened": False,
                "window": notice.already_open_message(system, slot),
                "answer": None}
    try:
        inner = notice.prompt_argv(config_path, test=test, slot=slot)
        argv = notice.launcher_argv(system.which, inner, realpath=system.realpath)
        if not argv:
            return {"kind": "prompt", "opened": False,
                    "window": "no cosmic-term or x-terminal-emulator", "answer": None}
        pid, unit = notice.start_prompt_window(system, argv, env)
        if pid is None:
            return {"kind": "prompt", "opened": False,
                    "window": "could not start the window", "answer": None}
        opened = _keep_launched_window(system, slot, pid, unit, env)
        return {"kind": "prompt", "opened": opened, "pid": pid,
                "window": "opened a window" if opened else "window exited immediately",
                "answer": None, "launcher": argv[0]}
    finally:
        system.unlock(token)


def _prompt_test_launch(ui, system, config_path: str | None) -> dict:
    """Open a test window. Read-only: no check, no run, no record."""
    try:
        cfg = load(config_path)
        state_dir = cfg.state_dir
    except ConfigError:
        state_dir = os.path.join(system.home(), ".local/state/twin-update")
    data = _open_prompt_window(system, config_path, state_dir, test=True)
    data["test"] = True
    return data


def _prompt_inside(ui, test: bool, slot: str | None, system, items=None,
                   run_fn=None) -> dict:
    if slot:
        notice.mark_started(system, slot, system.own_pid())
    try:
        if items is None:
            items = list(notice.TEST_PENDING) if test else []
        answer = notice.run_prompt(ui, items, test=test, run_fn=run_fn)
        return {"kind": "prompt", "answer": answer, "opened": False,
                "exit_code": 0, "result": answer}
    finally:
        if slot:
            notice.clear_slot(system, slot)


def _prompt_here(args, ui, sess, unreachable, system, peer_factory) -> dict:
    """Re-check, then ask. Does not hold the sync lock while waiting."""
    del peer_factory
    if args.slot:
        notice.mark_started(system, args.slot, system.own_pid())
    run_rc = [0]
    try:
        try:
            data = orchestrator.check(sess, unreachable,
                                     fresh=not getattr(args, "no_fetch", False))
        except Exception as exc:
            ui.say(str(exc))
            if hasattr(ui, "pause"):
                ui.pause("Press Enter to close.")
            return {"kind": "prompt", "answer": "error", "result": "error",
                    "exit_code": 1, "error": str(exc)}
        items = notice.items_from_check(data, sess.cfg.apps)
        errors = dict(data.get("errors") or {})
        sig = "\n".join(data.get("summary") or [])
        me = sess.order[0] if sess.order else None
        local = sess.peers.get(me) if me else None

        def on_shown():
            if local is None:
                return
            try:
                local.call("record_notice", kind="updates", signature=sig)
            except Exception as exc:
                ui.say(f"warning: could not record today's prompt ({exc})")
                notice.note_record_failure(system, sess.cfg.state_dir, str(exc))

        sess.close()

        def run_fn():
            env = notice.graphical_env(system) or system.env()
            run_rc[0] = notice.start_and_follow_run(
                system, notice.run_argv(args.config), env, ui)

        answer = notice.run_prompt(ui, items, test=False, run_fn=run_fn,
                                   errors=errors, on_shown=on_shown)
        return {"kind": "prompt", "answer": answer, "check": data,
                "result": answer, "summary": data.get("summary") or [],
                "exit_code": run_rc[0] if answer == "run" else (1 if answer == "error" else 0)}
    finally:
        if args.slot:
            notice.clear_slot(system, args.slot)


def _labels(choice: str, me: str, peer: str, sess) -> list[str]:
    if choice == "both":
        labels = [me, peer]
    elif choice in (me, peer):
        labels = [choice]
    else:
        raise ConfigError(f"--machine must be both, {me} or {peer}")
    missing = [l for l in labels if l not in sess.peers]
    if missing:
        raise PeerError(f"unreachable: {', '.join(missing)}")
    return labels


if __name__ == "__main__":
    raise SystemExit(main())
