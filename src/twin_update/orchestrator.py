"""Cross-machine flows: status, run, rollback, holds, unhold, doctor."""

from __future__ import annotations

import time

from twin_update import __version__
from twin_update.config import Config
from twin_update.peer import PeerError


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
        cands = {a[l]["candidate"] for l in labels}
        if missing:
            plan[app] = {"action": "skip", "reason": f"not installed on {', '.join(missing)}"}
        elif kept:
            plan[app] = {"action": "skip", "reason": f"hold kept by config on {', '.join(kept)}"}
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


def run(s: Session, apps, dry: bool, smoke: bool = True, unreachable=None) -> dict:
    rep = _new_report("run", dry, apps, s.order, unreachable or {})
    rep["blockers"] = [f"{l} unreachable: {why}" for l, why in (unreachable or {}).items()]
    labels = s.order
    try:
        if unreachable and not dry:
            raise Abort(rep["blockers"][0])
        _identity(s, rep)
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
            return _finish(s, rep, dry)

        results = {}
        for app, p in todo.items():
            for l in p["machines"]:
                up = s.peers[l].call("upgrade", app_name=app, version=p["target"])
                ver = s.peers[l].call("verify", app_name=app, version=p["target"], smoke=smoke) \
                    if up["ok"] else {"ok": False}
                r = {"upgrade": up, "verify": ver}
                if not ver["ok"]:
                    if s.ui.confirm(f"{s.cfg.apps[app].display} {p['target']} failed its check on "
                                    f"{l}. Roll back to {up.get('before')}?"):
                        rb = s.peers[l].call("rollback_install", app_name=app, hold=True)
                        r["rollback"] = rb
                        if rb.get("ok"):
                            r["rollback_verify"] = s.peers[l].call(
                                "verify", app_name=app, version=rb["to"], smoke=smoke)
                results.setdefault(app, {})[l] = r
                mark = ver["ok"]
                txt = f"{s.cfg.apps[app].display} {l} {up.get('before')}\u2192{up.get('after')} {_ok(mark)}"
                if "rollback" in r:
                    txt += f" (rolled back to {r['rollback'].get('to')})"
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
        rep["reopen"] = {l: s.peers[l].call("reopen", apps=apps_)
                         for l, apps_ in was_open.items() if apps_}
        rep["prune"] = {l: s.peers[l].call("prune", apps=list(todo), dry=False) for l in labels}
        all_ok = all(r["verify"]["ok"] for m in results.values() for r in m.values())
        rep["result"] = "ok" if all_ok else "failed"
    except (Abort, PeerError) as exc:
        rep["result"] = "aborted"
        rep["error"] = str(exc)
        rep["summary"].append(f"aborted: {exc}")
    return _finish(s, rep, dry)


def rollback(s: Session, app: str, labels, dry: bool, smoke: bool = True,
             hold: bool = True) -> dict:
    rep = _new_report("rollback", dry, [app], labels, {})
    rep["blockers"] = []
    try:
        _identity(s, rep)
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
            return _finish(s, rep, dry)
        res = {}
        for l in labels:
            rb = s.peers[l].call("rollback_install", app_name=app, hold=hold)
            ver = s.peers[l].call("verify", app_name=app, version=rb.get("to") or "",
                                  smoke=smoke) if rb.get("ok") else {"ok": False}
            res[l] = {"rollback": rb, "verify": ver}
            rep["summary"].append(f"{s.cfg.apps[app].display} {l} rollback "
                                  f"{rb.get('from')}\u2192{rb.get('to')} {_ok(ver['ok'])}"
                                  + (" hold set" if rb.get("hold") else ""))
        rep["results"] = res
        rep["reopen"] = {l: s.peers[l].call("reopen", apps=a) for l, a in was_open.items() if a}
        rep["result"] = "ok" if all(r["verify"]["ok"] for r in res.values()) else "failed"
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


def doctor(s: Session, unreachable: dict) -> dict:
    out = {"kind": "doctor", "machines": {}, "unreachable": unreachable}
    for l in s.order:
        try:
            out["machines"][l] = s.peers[l].call("doctor")
        except PeerError as exc:
            out["machines"][l] = {"error": str(exc)}
    return out
