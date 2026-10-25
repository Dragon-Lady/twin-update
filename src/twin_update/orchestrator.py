"""Cross-machine flows: status, run, rollback, holds, unhold, doctor."""

from __future__ import annotations

import signal
import time

from twin_update import __version__
from twin_update.config import Config
from twin_update.peer import PeerError

ROLLBACK_CMD = "twin-update rollback"


class Abort(Exception):
    pass


def _ok(mark: bool) -> str:
    return "\u2713" if mark else "\u2717"


class Session:
    """Holds the peers for one command and guarantees cleanup."""

    def __init__(self, cfg: Config, peers: dict, order: list[str], ui):
        self.cfg = cfg
        self.peers = peers
        self.order = [l for l in order if l in peers]
        self.ui = ui

    def both(self, op: str, labels=None, **kw) -> dict:
        return {l: self.peers[l].call(op, **kw) for l in (labels or self.order)}

    def password(self, labels) -> None:
        pw = self.ui.getpass("sudo password (same on both machines): ")
        try:
            for l in labels:
                self.peers[l].call("set_password", password=pw)
        finally:
            pw = None
        res = self.both("validate_sudo", labels=labels)
        bad = [l for l, r in res.items() if not r.get("ok")]
        if bad:
            raise Abort(f"sudo password not accepted on: {', '.join(bad)}")

    def close(self) -> None:
        for p in self.peers.values():
            try:
                p.close()
            except Exception:
                pass


def _new_report(kind: str, dry: bool, apps, labels, unreachable) -> dict:
    return {"tool": "twin-update", "version": __version__, "kind": kind, "dry_run": dry,
            "started_ts": int(time.time()), "apps": list(apps), "machines": list(labels),
            "unreachable": dict(unreachable), "steps": [], "result": None, "summary": []}


def _identity(s: Session, rep: dict) -> None:
    ids = s.both("identity")
    rep["identity"] = ids
    bad = [l for l, r in ids.items() if not r.get("ok")]
    if bad:
        raise Abort(f"identity check failed on: {', '.join(bad)} (whoami/machine-id vs config)")


def _version_detail(s: Session, rep: dict) -> str:
    ids = rep.get("identity") or {}
    known = {l: (ids.get(l) or {}).get("version") for l in s.order}
    known = {l: v for l, v in known.items() if v}
    return ", ".join(f"{l} {v}" for l, v in known.items())


def _versions_differ(s: Session, rep: dict) -> bool:
    ids = rep.get("identity") or {}
    known = {(ids.get(l) or {}).get("version") for l in s.order}
    known.discard(None)
    known.discard("")
    return len(known) > 1


def _require_same_version(s: Session, rep: dict) -> None:
    """Stop before any close when versions differ or a peer omits version."""
    ids = rep.get("identity") or {}
    known = {l: (ids.get(l) or {}).get("version") for l in s.order}
    missing = [l for l, v in known.items() if not v]
    if missing:
        raise Abort(
            f"{', '.join(missing)} is running an older twin-update "
            "(no version reported); update that machine to this version before running")
    if _versions_differ(s, rep):
        raise Abort(
            f"twin-update versions differ ({_version_detail(s, rep)}); "
            "update both machines to the same version before running")


def _warn_version_mismatch(s: Session, rep: dict) -> None:
    """Rollback proceeds per machine when versions differ."""
    if not _versions_differ(s, rep):
        return
    msg = (f"twin-update versions differ ({_version_detail(s, rep)}); "
           "continuing rollback on each machine as far as it supports it")
    rep.setdefault("warnings", []).append(msg)
    s.ui.say(f"warning: {msg}")
    rep["summary"].append(f"warning: {msg}")


def _install_stop_handlers() -> dict:
    """Treat SIGTERM/SIGHUP like Ctrl-C so a stop can reopen closed apps."""
    prev = {}

    def _on_stop(signum, frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM,):
        try:
            prev[sig] = signal.signal(sig, _on_stop)
        except (ValueError, OSError):
            pass
    return prev


def _restore_stop_handlers(prev: dict) -> None:
    for sig, old in prev.items():
        try:
            signal.signal(sig, old)
        except (ValueError, OSError):
            pass


def _ui_can_prompt(ui) -> bool:
    can = getattr(ui, "can_prompt", None)
    if callable(can):
        return bool(can())
    return True


def _optional_call(peer, op: str, **kw):
    """Call a peer op; None if the peer is older and rejects the op."""
    try:
        return peer.call(op, **kw)
    except PeerError as exc:
        msg = str(exc)
        if "op not allowed" in msg:
            return None
        if "unexpected keyword" in msg or "TypeError" in msg:
            try:
                return peer.call(op, **{k: v for k, v in kw.items()
                                        if k in ("app_name",)})
            except PeerError as exc2:
                if "op not allowed" in str(exc2):
                    return None
                raise
        raise


def _running_again(peer, app_name: str) -> bool:
    """True if the app's real main process is up. Older peers: treat as not running."""
    got = _optional_call(peer, "running", app_name=app_name, main_only=True)
    return bool(got)


def _locks(s: Session, rep: dict, dry: bool, labels=None) -> None:
    res = s.both("acquire_locks", labels=labels, dry=dry, wait_s=0 if dry else s.cfg.lock_wait_s)
    rep["locks"] = res
    bad = [l for l, r in res.items() if not r.get("ok")]
    if bad:
        msg = f"sync/backup lock not available on: {', '.join(bad)}"
        if dry:
            rep["blockers"].append(msg)
        else:
            raise Abort(msg)


def _wait_apt(s: Session, rep: dict, dry: bool, labels) -> dict:
    deadline = time.time() + (0 if dry else s.cfg.apt_lock_timeout_s)
    while True:
        st = s.both("status", labels=labels, apps=rep["apps"])
        busy = [l for l, r in st.items() if r.get("apt_busy")]
        if not busy:
            return st
        if time.time() >= deadline:
            msg = f"apt/dpkg busy on: {', '.join(busy)}"
            if dry:
                rep["blockers"].append(msg)
                return st
            raise Abort(msg)
        time.sleep(5)


def plan_updates(cfg: Config, apps, status: dict, labels) -> dict:
    plan = {}
    for app in apps:
        a = {l: status[l]["apps"][app] for l in labels}
        missing = [l for l in labels if not a[l]["installed"]]
        kept = [l for l in labels if a[l]["hold"] == "kept"]
        rolled = [l for l in labels if a[l]["hold"] == "rollback"]
        cands = {a[l]["candidate"] for l in labels}
        if missing:
            plan[app] = {"action": "skip", "reason": f"not installed on {', '.join(missing)}"}
        elif kept:
            plan[app] = {"action": "skip", "reason": f"hold kept by config on {', '.join(kept)}"}
        elif rolled:
            plan[app] = {"action": "skip", "reason": (
                f"rollback hold on {', '.join(rolled)} "
                f"(clear with twin-update unhold {app})")}
        elif len(cands) != 1 or None in cands:
            plan[app] = {"action": "skip", "reason": "candidate versions differ between machines: "
                         + ", ".join(f"{l}={a[l]['candidate']}" for l in labels)}
        else:
            target = cands.pop()
            todo = [l for l in labels if a[l]["installed"] != target]
            if not todo:
                plan[app] = {"action": "current", "target": target}
            else:
                plan[app] = {"action": "update", "target": target, "machines": todo,
                             "from": {l: a[l]["installed"] for l in labels}}
    return plan


def _close_all(s: Session, rep: dict, per_machine: dict, dry: bool) -> dict:
    """per_machine: label -> [apps]. Returns was_open map; raises Abort if anything stays."""
    started = {l: s.peers[l].call("close_start", apps=apps, dry=dry)
               for l, apps in per_machine.items() if apps}
    rep["close"] = {"start": started}
    was_open = {l: [a for a, r in res.items() if r["was_open"]] for l, res in started.items()}
    if dry:
        return was_open
    waited = {l: s.peers[l].call("close_wait", apps=apps, timeout=s.cfg.close_timeout_s)
              for l, apps in per_machine.items() if apps}
    rep["close"]["wait"] = waited
    stuck = [(l, a, r) for l, res in waited.items() for a, r in res.items() if not r["closed"]]
    if stuck:
        # Abort on BOTH before any update; reopen what we closed.
        reopened = {}
        for l, apps in was_open.items():
            closed_ok = [a for a in apps if waited[l][a]["closed"]]
            if closed_ok:
                reopened[l] = s.peers[l].call("reopen", apps=closed_ok)
        rep["close"]["reopened_after_abort"] = reopened
        detail = "; ".join(
            f"{a} on {l} still running ({r['remaining_count']} proc, e.g. "
            f"{r['remaining'][0]['exe'] if r['remaining'] else '?'})" for l, a, r in stuck)
        raise Abort(f"graceful close timed out, nothing was updated on either machine: {detail}")
    return was_open


def _finish(s: Session, rep: dict, dry: bool) -> dict:
    try:
        s.both("release")
    except PeerError:
        pass
    if not dry:
        title = "Twin Update: " + ("done" if rep["result"] == "ok" else rep["result"] or "?")
        body = "\n".join(rep["summary"][:12]) or "nothing to do"
        for l in s.order:
            try:
                rep.setdefault("report_paths", {})[l] = s.peers[l].call("record_run", report=rep)
                s.peers[l].call("notify", summary=title, body=body)
            except PeerError:
                pass
    return rep


def status(s: Session) -> dict:
    return {"kind": "status", "machines": s.both("status", apps=s.cfg.enabled)}


def _raft_dry(s, rep, was_open):
    if s.cfg.raft.enabled:
        for label, apps in was_open.items():
            if "cursor" in apps and "cursor" not in s.cfg.no_launch:
                rep["summary"].append(f"Cursor {label}: would raft (snapshot before reopen, upload to {s.cfg.raft.remote})")


def _reopen(s, rep, was_open, results):
    reopened = {}
    for label, apps in was_open.items():
        if not apps:
            continue
        hook = None
        try:
            if "cursor" in apps and s.cfg.raft.enabled and "cursor" not in s.cfg.no_launch:
                result = results.get("cursor", {}).get(label, {})
                verification = result.get("rollback_verify", {}) if "rollback" in result else result.get("verify", {})
                smoke = verification.get("smoke", {})
                try:
                    hook = s.peers[label].call("pre_reopen_hook", app_name="cursor",
                                              smoke_ok=smoke.get("ok") is True and not smoke.get("skipped"),
                                              call_timeout=s.cfg.raft.snapshot_timeout_s + 30)
                except Exception as exc:
                    hook = {"ok": False, "error": str(exc)}
                rep.setdefault("raft", {})[label] = hook
        finally:
            # The engine honors no_launch. A hook failure never suppresses
            # reopening of apps whose launch policy explicitly permits it.
            reopened[label] = s.peers[label].call("reopen", apps=apps)
        if hook:
            if hook.get("ok"):
                try:
                    started = s.peers[label].call("raft_upload_start", snapshot=hook["snapshot"], call_timeout=30)
                except Exception as exc:
                    started = {"started": False, "reason": str(exc)}
                hook["upload"] = started
                detail = f"upload running in {started['unit']}" if started.get("started") else "upload not started; snapshot retained"
                rep["summary"].append(f"Cursor {label}: raft snapshot taken, {detail}")
            else:
                rep["summary"].append(f"Cursor {label}: raft skipped ({hook.get('skipped') or hook.get('error')})")
    return reopened


def _reopen_after_interrupt(s: Session, rep: dict, was_open: dict, results: dict) -> None:
    """Reopen apps closed for the run after Ctrl-C or a dropped peer."""
    if not was_open or rep.get("reopen"):
        return
    try:
        rep["reopen"] = _reopen(s, rep, was_open, results or {})
        rep["summary"].append("apps reopened after the run was interrupted")
    except Exception as exc:
        rep["summary"].append(f"could not reopen apps: {exc}")


def run(s: Session, apps, dry: bool, smoke: bool = True, unreachable=None) -> dict:
    prev = _install_stop_handlers()
    try:
        return _run(s, apps, dry, smoke, unreachable)
    finally:
        _restore_stop_handlers(prev)


def _run(s: Session, apps, dry: bool, smoke: bool = True, unreachable=None) -> dict:
    rep = _new_report("run", dry, apps, s.order, unreachable or {})
    rep["blockers"] = [f"{l} unreachable: {why}" for l, why in (unreachable or {}).items()]
    labels = s.order
    try:
        if unreachable and not dry:
            raise Abort(rep["blockers"][0])
        _identity(s, rep)
        _require_same_version(s, rep)
        if not dry:
            s.password(labels)
        _locks(s, rep, dry)
        st = _wait_apt(s, rep, dry, labels)
        if not dry:
            upd = s.both("apt_update")
            rep["apt_update"] = upd
            if not all(r["ok"] for r in upd.values()):
                raise Abort("apt-get update failed on: "
                            + ", ".join(l for l, r in upd.items() if not r["ok"]))
            st = s.both("status", apps=apps)
        rep["status_before"] = st
        if len(labels) < 2:
            rep["note"] = "single-machine dry run: cross-machine checks are pending"
        plan = plan_updates(s.cfg, apps, st, labels)
        rep["plan"] = plan

        # rollback copies first; a missing copy on either machine skips the app on both
        caps = {}
        for app, p in plan.items():
            if p["action"] != "update":
                continue
            caps[app] = {l: s.peers[l].call("capture", app_name=app, dry=dry) for l in p["machines"]}
            bad = [l for l, c in caps[app].items() if not c.get("ok")]
            if bad:
                p.update(action="skip", reason="no rollback copy on " + ", ".join(
                    f"{l} ({caps[app][l].get('reason')})" for l in bad))
            fb = [l for l, c in caps[app].items() if c.get("fallback")]
            if fb:
                p["rollback_fallback_on"] = fb
        rep["rollback_capture"] = caps
        todo = {a: p for a, p in plan.items() if p["action"] == "update"}
        per_machine = {l: [a for a, p in todo.items() if l in p["machines"]] for l in labels}

        rep["holds"] = {l: s.peers[l].call("clear_holds", apps=per_machine[l], dry=dry)
                        for l in labels if per_machine[l]}
        if not dry and any(not a.get("ok", True) for acts in rep["holds"].values() for a in acts):
            raise Abort("could not clear a hold; nothing was updated")
        if not todo:
            rep["result"] = "ok"
            rep["summary"] = [f"{s.cfg.apps[a].display}: {p['action']}"
                              + (f" ({p.get('reason')})" if p.get("reason") else "")
                              for a, p in plan.items()]
            return _finish(s, rep, dry)

        was_open = _close_all(s, rep, per_machine, dry)
        rep["was_open"] = was_open
        if dry:
            rep["result"] = "dry-run"
            for a, p in plan.items():
                line = f"{s.cfg.apps[a].display}: {p['action']}"
                if p["action"] == "update":
                    line += " " + ", ".join(f"{l} {p['from'][l]}\u2192{p['target']}"
                                            for l in p["machines"])
                elif p.get("reason"):
                    line += f" ({p['reason']})"
                rep["summary"].append(line)
            _raft_dry(s, rep, was_open)
            return _finish(s, rep, dry)

        results = {}
        try:
            for app, p in todo.items():
                for l in p["machines"]:
                    if _running_again(s.peers[l], app):
                        raise Abort(
                            f"{s.cfg.apps[app].display} on {l} is running again; "
                            "not closing a user-launched instance")
                    up = s.peers[l].call("upgrade", app_name=app, version=p["target"])
                    ver = s.peers[l].call("verify", app_name=app, version=p["target"],
                                          smoke=smoke) if up["ok"] else {"ok": False}
                    r = {"upgrade": up, "verify": ver}
                    if not ver["ok"]:
                        want_rb = False
                        if _ui_can_prompt(s.ui):
                            want_rb = s.ui.confirm(
                                f"{s.cfg.apps[app].display} {p['target']} failed its check on "
                                f"{l}. Roll back to {up.get('before')}?",
                                timeout_s=s.cfg.rollback_prompt_s)
                        if want_rb:
                            rb = s.peers[l].call("rollback_install", app_name=app, hold=True)
                            r["rollback"] = rb
                            if rb.get("ok"):
                                r["rollback_verify"] = s.peers[l].call(
                                    "verify", app_name=app, version=rb["to"], smoke=smoke)
                        elif not _ui_can_prompt(s.ui):
                            results.setdefault(app, {})[l] = r
                            raise Abort(
                                f"{s.cfg.apps[app].display} {p['target']} failed its check on "
                                f"{l}; not upgrading the other machine. "
                                f"To roll back: {ROLLBACK_CMD}")
                    results.setdefault(app, {})[l] = r
                    mark = ver["ok"]
                    txt = (f"{s.cfg.apps[app].display} {l} {up.get('before')}\u2192"
                           f"{up.get('after')} {_ok(mark)}")
                    if "rollback" in r:
                        txt += f" (rolled back to {r['rollback'].get('to')})"
                    elif not ver["ok"]:
                        txt += f" (left installed; {ROLLBACK_CMD} to undo)"
                    rep["summary"].append(txt)
            rep["results"] = results
            finals = {}
            for app in todo:
                vs = {l: s.both("status", apps=[app])[l]["apps"][app]["installed"] for l in labels}
                finals[app] = vs
                if len(set(vs.values())) != 1:
                    rep["summary"].append(f"{s.cfg.apps[app].display}: versions differ now "
                                          + ", ".join(f"{l}={v}" for l, v in vs.items()))
            rep["final_versions"] = finals
            rep["reopen"] = _reopen(s, rep, was_open, results)
            rep["prune"] = {l: s.peers[l].call("prune", apps=list(todo), dry=False) for l in labels}
            all_ok = all(r["verify"]["ok"] for m in results.values() for r in m.values())
            rep["result"] = "ok" if all_ok else "failed"
        except (Abort, PeerError, KeyboardInterrupt) as exc:
            _reopen_after_interrupt(s, rep, was_open, results)
            if isinstance(exc, KeyboardInterrupt):
                raise Abort("interrupted after close; apps reopened") from exc
            raise
    except (Abort, PeerError) as exc:
        rep["result"] = "aborted"
        rep["error"] = str(exc)
        rep["summary"].append(f"aborted: {exc}")
    return _finish(s, rep, dry)


def rollback(s: Session, app: str, labels, dry: bool, smoke: bool = True,
             hold: bool = True) -> dict:
    prev = _install_stop_handlers()
    try:
        return _rollback(s, app, labels, dry, smoke, hold)
    finally:
        _restore_stop_handlers(prev)


def _rollback(s: Session, app: str, labels, dry: bool, smoke: bool = True,
              hold: bool = True) -> dict:
    rep = _new_report("rollback", dry, [app], labels, {})
    rep["blockers"] = []
    try:
        _identity(s, rep)
        _warn_version_mismatch(s, rep)
        targets = s.both("rollback_target", labels=labels, app_name=app)
        rep["targets"] = targets
        bad = [l for l, t in targets.items() if not t["ok"]]
        if bad:
            raise Abort("no rollback copy on: " + ", ".join(
                f"{l} ({targets[l].get('reason')})" for l in bad))
        if not dry:
            s.password(labels)
        _locks(s, rep, dry, labels=labels)
        _wait_apt(s, rep, dry, labels)
        was_open = _close_all(s, rep, {l: [app] for l in labels}, dry)
        if dry:
            rep["result"] = "dry-run"
            rep["summary"] = [f"{s.cfg.apps[app].display} {l}: would roll back "
                              f"{t['installed']}\u2192{t['version']}"
                              + (" (fallback copy)" if t.get("fallback") else "")
                              for l, t in targets.items()]
            _raft_dry(s, rep, was_open)
            return _finish(s, rep, dry)
        res = {}
        try:
            for l in labels:
                if _running_again(s.peers[l], app):
                    raise Abort(
                        f"{s.cfg.apps[app].display} on {l} is running again; "
                        "not closing a user-launched instance")
                rb = s.peers[l].call("rollback_install", app_name=app, hold=hold)
                ver = s.peers[l].call("verify", app_name=app, version=rb.get("to") or "",
                                      smoke=smoke) if rb.get("ok") else {"ok": False}
                res[l] = {"rollback": rb, "verify": ver}
                rep["summary"].append(f"{s.cfg.apps[app].display} {l} rollback "
                                      f"{rb.get('from')}\u2192{rb.get('to')} {_ok(ver['ok'])}"
                                      + (" hold set" if rb.get("hold") else ""))
            rep["results"] = res
            # Rollback verification is the final verification on this path.
            rep["reopen"] = _reopen(s, rep, was_open,
                                    {app: {l: {"verify": r["verify"]} for l, r in res.items()}})
            rep["result"] = "ok" if all(r["verify"]["ok"] for r in res.values()) else "failed"
        except (Abort, PeerError, KeyboardInterrupt) as exc:
            _reopen_after_interrupt(s, rep, was_open, {app: res} if res else {})
            if isinstance(exc, KeyboardInterrupt):
                raise Abort("interrupted after close; apps reopened") from exc
            raise
    except (Abort, PeerError) as exc:
        rep["result"] = "aborted"
        rep["error"] = str(exc)
        rep["summary"].append(f"aborted: {exc}")
    return _finish(s, rep, dry)


def holds(s: Session) -> dict:
    return {"kind": "holds", "machines": s.both("holds")}


def unhold(s: Session, app: str, labels, dry: bool) -> dict:
    if not dry:
        s.password(labels)
    out = s.both("unhold", labels=labels, app_name=app, dry=dry)
    s.both("release", labels=labels)
    return {"kind": "unhold", "machines": out}


def doctor_findings(checks: dict) -> tuple[list[str], list[str]]:
    """(problems, warnings) for one machine's doctor result."""
    if "error" in checks:
        return [f"error: {checks['error']}"], []
    problems, warnings = [], []
    for k, v in sorted(checks.items()):
        if k == "locks":
            for path, state in v.items():
                if state.startswith("missing (parent"):
                    problems.append(f"lock {path}: {state}")
                elif state == "busy":
                    warnings.append(f"lock {path} busy right now (a run waits for it)")
        elif k == "notify_tool":
            continue  # extra reminder only; never a doctor failure
        elif k == "terminal_launcher":
            if not v:
                problems.append("terminal_launcher")
        elif k == "check_timer":
            if not v:
                warnings.append("twin-update-check.timer is not enabled")
        elif k == "window_stale":
            if v:
                since = checks.get("window_open_since") or "an unknown time"
                warnings.append(f"prompt window has been open since {since} (over 24 hours)")
        elif k == "unit_warnings":
            warnings.extend(v or [])
        elif k == "rollback_holds":
            for item in (v or []):
                warnings.append(f"rollback hold: {item}")
        elif k == "slot_pid_reused":
            if v:
                warnings.append("prompt slot pid does not match the recorded process (pid reuse)")
        elif k in ("locks_ok", "check_timer_next", "last_window",
                   "window_open_since", "rollback_holds"):
            continue
        elif k != "locks_ok" and isinstance(v, bool) and not v:
            problems.append(k)
    return problems, warnings


def doctor(s: Session, unreachable: dict) -> dict:
    out = {"kind": "doctor", "machines": {}, "unreachable": unreachable,
           "problems": {}, "warnings": {}}
    for l in s.order:
        try:
            out["machines"][l] = s.peers[l].call("doctor")
        except PeerError as exc:
            out["machines"][l] = {"error": str(exc)}
    for l, checks in out["machines"].items():
        p, w = doctor_findings(checks)
        if p:
            out["problems"][l] = p
        if w:
            out["warnings"][l] = w
    for l, why in unreachable.items():
        if not why.startswith("skipped"):
            out["problems"].setdefault(l, []).append(f"unreachable: {why}")
    out["ok"] = not out["problems"]
    return out


def check(s: Session, unreachable: dict, fresh: bool = True) -> dict:
    """Read-only update check on both machines; never asks for sudo."""
    out = {"kind": "check", "version": __version__, "machines": {}, "errors": {},
           "updates": [], "summary": []}
    for l, why in unreachable.items():
        if not why.startswith("skipped"):
            prefix = f"{l}: "
            out["errors"][l] = why[len(prefix):] if why.startswith(prefix) else why
    for l in s.order:
        try:
            try:
                kw = {} if fresh else {"fresh": False}
                r = s.peers[l].call("check", apps=s.cfg.enabled, **kw)
            except PeerError as exc:
                if "op not allowed: check" not in str(exc):
                    raise
                # older twin-update on the other machine: fall back to status
                st = s.peers[l].call("status", apps=s.cfg.enabled)
                r = {"label": l, "apt_lists_age_h": None, "identity_ok": True,
                     "apps": {a: {k: v[k] for k in ("installed", "candidate", "update_available")}
                              for a, v in st["apps"].items()}}
        except PeerError as exc:
            out["errors"][l] = str(exc)
            continue
        if not r.get("identity_ok", True):
            out["errors"][l] = "identity does not match the config"
            continue
        out["machines"][l] = r
    for app in s.cfg.enabled:
        on = {l: m["apps"][app] for l, m in out["machines"].items()
              if m["apps"].get(app, {}).get("update_available")}
        if not on:
            continue
        name = s.cfg.apps[app].display
        parts = sorted({f"{r['installed']}\u2192{r['candidate']}" for r in on.values()})
        where = "both machines" if len(on) == 2 else ", ".join(sorted(on))
        out["updates"].append({"app": app, "machines": {l: dict(r) for l, r in on.items()}})
        out["summary"].append(f"{name} {' / '.join(parts)} on {where}")
    ages = {l: m.get("apt_lists_age_h") for l, m in out["machines"].items()}
    out["apt_lists_age_h"] = ages
    out["result"] = ("updates" if out["updates"] else ("error" if out["errors"] else "none"))
    out["exit_code"] = 10 if out["updates"] else (1 if out["errors"] else 0)
    return out
