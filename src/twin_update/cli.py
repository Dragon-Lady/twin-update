"""Command line: twin-update status|run|rollback|holds|unhold|doctor|local."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import signal
import sys

from twin_update import __version__, orchestrator
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

    def getpass(self, prompt: str) -> str:
        return getpass.getpass(prompt)

    def confirm(self, prompt: str) -> bool:
        if not sys.stdin.isatty():
            return False
        try:
            return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, OSError):
            return False


def _detach_if_needed(args) -> None:
    """Re-exec in its own systemd user scope so closing the app that hosts our
    terminal (e.g. Grok Bot) does not kill the run. The scope ends with us."""
    if args.no_detach or os.environ.get("TWIN_UPDATE_SCOPE") == "1":
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        return
    if not shutil.which("systemd-run"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        return
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--",
            "env", "TWIN_UPDATE_SCOPE=1", sys.executable, *sys.argv]
    os.execvp(argv[0], argv)


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
            unreachable[peer] = str(exc)
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
                    ui.say(f"{l:8} notify: {v or 'none (notifications skipped)'}")
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
    if kind == "check":
        for l, m in data.get("machines", {}).items():
            ui.say(f"{l:8} {_lists_line(m.get('lists'), m.get('apt_lists_age_h'))}")
        for line in data.get("summary", []):
            ui.say(line)
        for l, why in data.get("errors", {}).items():
            ui.say(f"{l:8} error: {why}")
        n = data.get("notification")
        if n:
            ui.say(f"notification: {n}")
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
                   help="desktop notification on this machine if updates are available "
                        "(errors: at most one notice per day)")
    p = sub.add_parser("doctor"); common(p)
    p = sub.add_parser("local", help="per-machine engine (used by the peer over ssh)")
    p.add_argument("--serve", action="store_true")
    p.add_argument("--as", dest="as_label", default=None)

    args = ap.parse_args(argv)
    system = system or System()

    try:
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

        if args.cmd in ("run", "rollback") and not args.dry_run:
            if getattr(args, "local_only", False):
                ui.say("--local-only is allowed only with --dry-run")
                return 2
            _detach_if_needed(args)

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
                    data["notification"] = _check_notify(sess, me, data)
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
        return 2
    except PeerError as exc:
        ui.say(f"error: {exc}")
        return 1
    _print(ui, data, args.json)
    if data.get("kind") == "check":
        return data["exit_code"]
    if data.get("kind") == "doctor":
        return 0 if data.get("ok") else 4
    res = data.get("result")
    return 0 if res in (None, "ok", "dry-run") else (3 if res == "aborted" else 1)


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


def _check_notify(sess, me: str, data: dict) -> str:
    """Notify on this machine only. Same notice at most once per day."""
    local = sess.peers[me]
    if data["updates"]:
        summary = "Twin Update: updates available"
        body = "\n".join(data["summary"])
        if data["errors"]:
            body += "\nNot checked: " + ", ".join(sorted(data["errors"]))
        body += "\nRun: twin-update run"
        kind, sig = "updates", body
    elif data["errors"]:
        summary = "Twin Update: check incomplete"
        body = "\n".join(f"{l}: {why}" for l, why in sorted(data["errors"].items()))
        kind, sig = "error", ",".join(sorted(data["errors"]))
    else:
        return "none needed"
    if not local.call("notice_due", kind=kind, signature=sig):
        return "skipped (same notice already sent today)"
    r = local.call("notify", summary=summary, body=body)
    return f"sent via {r.get('tool')}" if r.get("ok") else f"failed ({r.get('reason') or r.get('tool')})"


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
