"""Per-machine engine. Acts only on the machine it runs on.

The orchestrator drives one Engine per machine: in-process for this machine,
over the ssh session (``twin-update local --serve``) for the peer.
Rules enforced here:
  * graceful close only (SIGTERM via pidfd); never SIGKILL;
  * app data is untouched except the explicit opt-in closed-profile snapshot;
  * the sudo password only ever travels on sudo's stdin.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import time
from typing import Callable

from twin_update import __version__
from twin_update.config import App, Config
from twin_update.system import Proc, System

APT_BUSY = {"apt", "apt-get", "dpkg", "unattended-upgr", "apt.systemd.dai", "aptitude"}
CRASH_RE = re.compile(r"segmentation fault|core dumped|SIGSEGV|SIGABRT|SIGILL|SIGBUS",
                      re.IGNORECASE)
HELPER_EXE_MARKS = ("/resources/", "crashpad", "helper", "zygote", "nacl", "sandbox")
ARCHIVES = "/var/cache/apt/archives"
APT_LISTS = "/var/lib/apt/lists"
APT_UPDATE_STAMP = "/var/lib/apt/periodic/update-success-stamp"
SOURCES_DIR = "/etc/apt/sources.list.d"
# Our user-level `apt-get update` must not run the system's update hooks.
CLEAR_HOOKS = ("#clear APT::Update::Pre-Invoke;\n#clear APT::Update::Post-Invoke;\n"
               "#clear APT::Update::Post-Invoke-Success;\n#clear DPkg::Pre-Invoke;\n"
               "#clear DPkg::Post-Invoke;\n")
FETCH_FAIL = ("E:", "Err:", "GPG error", "NO_PUBKEY", "is not signed", "Failed to fetch",
              "not available", "could not be verified")
DPKG_INFO = "/var/lib/dpkg/info"


class EngineError(Exception):
    pass


def deb_name(pkg: str, version: str, arch: str) -> str:
    return f"{pkg}_{version.replace(':', '%3a')}_{arch}.deb"


class Engine:
    def __init__(self, cfg: Config, system: System, label: str,
                 log: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.sys = system
        self.label = label
        self._log = log or (lambda msg: None)
        self._pw: bytearray | None = None
        self._locks: list = []
        self._exes: dict[str, set[str]] = {}
        self.archives = ARCHIVES
        self.dpkg_info = DPKG_INFO
        self.apt_lists = APT_LISTS
        self.apt_update_stamp = APT_UPDATE_STAMP
        self.sources_dir = SOURCES_DIR
        self.dpkg_updates = "/var/lib/dpkg/updates"
        self._reopened: set[str] = set()

    # ------------------------------------------------------------------ util
    def log(self, msg: str) -> None:
        self._log(f"[{self.label}] {msg}")

    def _apps(self, names) -> list[App]:
        out = []
        for n in names:
            if n not in self.cfg.apps:
                raise EngineError(f"unknown app {n}")
            out.append(self.cfg.apps[n])
        return out

    def _redact(self, path: str) -> str:
        home = self.sys.home()
        if path == home or path.startswith(home + "/"):
            return "~" + path[len(home):]
        return path

    def _user_env(self) -> dict:
        env = self.sys.env()
        uid = self.sys.uid()
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
        return env

    # ------------------------------------------------------------- identity
    def identity(self) -> dict:
        me = self.cfg.machines[self.label]
        user = self.sys.whoami()
        mid = self.sys.machine_id()
        ok = user == me.user and (not me.machine_id_sha256 or me.machine_id_sha256 == mid)
        return {"label": self.label, "user_ok": user == me.user,
                "machine_id_ok": (not me.machine_id_sha256) or me.machine_id_sha256 == mid,
                "machine_id_pinned": bool(me.machine_id_sha256), "ok": ok,
                "groups_sudo": "sudo" in self.sys.groups(),
                "version": __version__}

    # ----------------------------------------------------------------- sudo
    def set_password(self, password: str) -> dict:
        self._drop_password()
        self._pw = bytearray(password.encode("utf-8"))
        return {"ok": True}

    def _drop_password(self) -> None:
        if self._pw is not None:
            for i in range(len(self._pw)):
                self._pw[i] = 0
        self._pw = None

    def _sudo(self, argv: list[str], timeout: float | None = None):
        if self._pw is None:
            raise EngineError("sudo needed but no password was provided")
        # -k: ignore cached credentials so sudo ALWAYS consumes the password
        # line and nothing leftover reaches the command's stdin.
        full = ["sudo", "-k", "-S", "-p", "", "--", *argv]
        data = bytes(self._pw) + b"\n"
        try:
            return self.sys.run(full, input=data, timeout=timeout)
        finally:
            del data

    def validate_sudo(self) -> dict:
        r = self._sudo(["true"], timeout=30)
        return {"ok": r.ok}

    # ----------------------------------------------------------------- apt
    def installed(self, pkg: str) -> tuple[str | None, str | None]:
        r = self.sys.run(["dpkg-query", "-W", "-f=${db:Status-Abbrev}|${Version}|${Architecture}",
                          pkg])
        if not r.ok or "|" not in r.out:
            return None, None
        status, ver, arch = r.out.strip().split("|", 2)
        if not status.startswith("ii"):
            return None, None
        return ver, arch

    def candidate(self, pkg: str, opts: list[str] | None = None) -> str | None:
        r = self.sys.run(["apt-cache", "policy", *(opts or []), pkg])
        for line in r.out.splitlines():
            line = line.strip()
            if line.startswith("Candidate:"):
                v = line.split(":", 1)[1].strip()
                return None if v in ("", "(none)") else v
        return None

    def repo_versions(self, pkg: str) -> list[str]:
        r = self.sys.run(["apt-cache", "madison", pkg])
        out = []
        for line in r.out.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2 and parts[1]:
                out.append(parts[1])
        return out

    def showhold(self) -> list[str]:
        r = self.sys.run(["apt-mark", "showhold"])
        return sorted({x.strip() for x in r.out.splitlines() if x.strip()})

    def apt_busy(self) -> bool:
        return any(c in APT_BUSY for c in self.sys.all_comms())

    def vercmp(self, a: str, op: str, b: str) -> bool:
        return self.sys.run(["dpkg", "--compare-versions", a, op, b]).ok

    # ---------------------------------------------------------------- holds
    def _holds_file(self) -> str:
        return os.path.join(self.cfg.state_dir, "holds.json")

    def _tool_holds(self) -> dict:
        raw = self.sys.read_text(self._holds_file())
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _sync_hold_records(self) -> dict:
        """Drop holds.json entries whose apt hold is gone."""
        held = set(self.showhold())
        tool = self._tool_holds()
        changed = False
        for pkg in list(tool):
            if pkg not in held:
                tool.pop(pkg, None)
                changed = True
        if changed:
            self.sys.write_private(self._holds_file(), json.dumps(tool, indent=2))
        return tool

    def _hold_record(self, pkg: str, tool: dict | None = None) -> dict:
        rec = (tool if tool is not None else self._tool_holds()).get(pkg) or {}
        return rec if isinstance(rec, dict) else {}

    def _rejected_version(self, pkg: str, rec: dict | None = None) -> str | None:
        rec = rec if rec is not None else self._hold_record(pkg)
        if rec.get("rejected"):
            return str(rec["rejected"])
        reason = str(rec.get("reason") or "")
        for prefix in ("rollback from ", "failed-check of "):
            if reason.startswith(prefix):
                return reason[len(prefix):].strip() or None
        return None

    def holds(self) -> dict:
        held = self.showhold()
        tool = self._sync_hold_records()
        managed = {a.package: a.name for a in self.cfg.apps.values()}
        out = {"managed": {}, "other": []}
        for pkg in held:
            if pkg in managed:
                name = managed[pkg]
                if name in self.cfg.keep_holds or pkg in self.cfg.keep_holds:
                    owner = "kept"
                elif pkg in tool:
                    rec = tool.get(pkg) or {}
                    reason = rec.get("reason") if isinstance(rec, dict) else ""
                    if str(reason or "").startswith("rollback"):
                        owner = "rollback"
                    elif str(reason or "").startswith("failed-check"):
                        owner = "failed-check"
                    else:
                        owner = "twin-update"
                else:
                    owner = "foreign"
                out["managed"][name] = owner
            else:
                out["other"].append(pkg)
        return out

    def _rollback_hold_reason(self, pkg: str, tool: dict | None = None) -> str | None:
        rec = self._hold_record(pkg, tool)
        reason = str(rec.get("reason") or "")
        if reason.startswith("rollback") or reason.startswith("failed-check"):
            return reason
        return None

    def clear_holds(self, apps: list[str], dry: bool) -> list[dict]:
        """Release holds on these apps unless kept or set by a rollback."""
        actions = []
        info = self.holds()["managed"]
        tool = self._tool_holds()
        for app in self._apps(apps):
            owner = info.get(app.name)
            if owner in (None, "kept"):
                continue
            if owner == "rollback" or self._rollback_hold_reason(app.package, tool):
                actions.append({"app": app.name, "owner": "rollback",
                                "action": "kept rollback hold"})
                continue
            act = {"app": app.name, "owner": owner,
                   "action": "would clear hold" if dry else "cleared hold"}
            if not dry:
                r = self._sudo(["apt-mark", "unhold", app.package], timeout=120)
                if not r.ok:
                    act["action"] = "failed to clear hold"
                    act["ok"] = False
                else:
                    act["ok"] = True
                    tool.pop(app.package, None)
            actions.append(act)
        if not dry and actions:
            self.sys.write_private(self._holds_file(), json.dumps(tool, indent=2))
        return actions

    def hold(self, app_name: str, reason: str, rejected: str | None = None) -> dict:
        app = self._apps([app_name])[0]
        r = self._sudo(["apt-mark", "hold", app.package], timeout=120)
        if r.ok:
            tool = self._tool_holds()
            ver, _ = self.installed(app.package)
            rec = {"reason": reason, "version": ver, "at": int(self.sys.now())}
            rec["rejected"] = rejected or self._rejected_version(app.package, {"reason": reason})
            tool[app.package] = rec
            self.sys.write_private(self._holds_file(), json.dumps(tool, indent=2))
        return {"ok": r.ok}

    def unhold(self, app_name: str, dry: bool = False) -> dict:
        app = self._apps([app_name])[0]
        tool = self._tool_holds()
        held = app.package in self.showhold()
        if not held:
            if app.package in tool:
                if not dry:
                    tool.pop(app.package, None)
                    self.sys.write_private(self._holds_file(), json.dumps(tool, indent=2))
                return {"ok": True,
                        "action": "would clear leftover record" if dry else "cleared leftover record"}
            return {"ok": True, "action": "not held"}
        if dry:
            return {"ok": True, "action": "would unhold"}
        r = self._sudo(["apt-mark", "unhold", app.package], timeout=120)
        if r.ok:
            tool.pop(app.package, None)
            self.sys.write_private(self._holds_file(), json.dumps(tool, indent=2))
        return {"ok": r.ok, "action": "unheld" if r.ok else "unhold failed"}

    # ------------------------------------------------------------ processes
    def _app_exes(self, app: App) -> set[str]:
        if app.name not in self._exes:
            r = self.sys.run(["dpkg", "-L", app.package])
            exes = set()
            if r.ok:
                for line in r.out.splitlines():
                    p = line.strip()
                    if p and self.sys.is_executable_file(p):
                        exes.add(p)
            self._exes[app.name] = exes
        return self._exes[app.name]

    def _matches(self, app: App, exe: str) -> bool:
        if exe in self._app_exes(app):
            return True
        return any(exe.startswith(root.rstrip("/") + "/") for root in app.roots)

    def app_tree(self, app: App, procs: dict[int, Proc] | None = None) -> dict:
        procs = procs if procs is not None else self.sys.procs()
        own = self._own_tree(procs)
        matched = {pid for pid, p in procs.items() if pid not in own and self._matches(app, p.exe)}
        tree = set(matched)
        children: dict[int, list[int]] = {}
        for pid, p in procs.items():
            children.setdefault(p.ppid, []).append(pid)
        stack = list(matched)
        while stack:
            for c in children.get(stack.pop(), []):
                if c not in tree and c not in own:
                    tree.add(c)
                    stack.append(c)
        mains = sorted(pid for pid in matched if procs[pid].ppid not in tree)
        return {"tree": tree, "mains": mains, "procs": procs}

    def _own_tree(self, procs: dict[int, Proc]) -> set[int]:
        me = self.sys.own_pid()
        own = {me}
        changed = True
        while changed:
            changed = False
            for pid, p in procs.items():
                if p.ppid in own and pid not in own:
                    own.add(pid)
                    changed = True
        return own

    def _is_main_exe(self, app: App, exe: str) -> bool:
        """True for the app's real main binary, not a leftover helper under the same root."""
        names = {app.name.lower(), app.package.lower()}
        if app.launch:
            if exe in app.launch:
                return True
            names.add(os.path.basename(app.launch[0]).lower())
        return os.path.basename(exe).lower() in names

    def _looks_like_helper(self, exe: str) -> bool:
        low = exe.lower()
        return any(mark in low for mark in HELPER_EXE_MARKS)

    def running(self, app_name: str, main_only: bool = False) -> bool:
        app = self._apps([app_name])[0]
        tree = self.app_tree(app)
        if not tree["tree"]:
            return False
        if not main_only:
            return True
        procs = tree["procs"]
        mains = [pid for pid in tree["mains"] if pid in procs]
        if any(self._is_main_exe(app, procs[pid].exe) for pid in mains):
            return True
        if not mains:
            self.log(f"running {app.name}: tree has no identifiable main; treating as running")
            return True
        if not app.launch:
            self.log(f"running {app.name}: no launch command; treating tree as running")
            return True
        if any(not self._looks_like_helper(procs[pid].exe) for pid in mains):
            self.log(f"running {app.name}: main exe not identified; treating as running")
            return True
        return False

    def close_start(self, apps: list[str], dry: bool) -> dict:
        out = {}
        procs = self.sys.procs()
        for app in self._apps(apps):
            t = self.app_tree(app, procs)
            info = {"was_open": bool(t["tree"]), "processes": len(t["tree"]),
                    "mains": [self._redact(procs[p].exe) for p in t["mains"]]}
            if dry:
                info["action"] = "would send SIGTERM" if t["tree"] else "not running"
            else:
                sent = 0
                for pid in t["mains"]:
                    if self.sys.term(pid, procs[pid].start):
                        sent += 1
                info["action"] = f"SIGTERM sent to {sent} main process(es)" if t["tree"] \
                    else "not running"
                self.log(f"close {app.name}: {info['action']}")
            out[app.name] = info
        return out

    def close_wait(self, apps: list[str], timeout: float) -> dict:
        deadline = self.sys.now() + timeout
        apps_l = self._apps(apps)
        while True:
            procs = self.sys.procs()
            left = {a.name: self.app_tree(a, procs) for a in apps_l}
            if all(not t["tree"] for t in left.values()) or self.sys.now() >= deadline:
                break
            self.sys.sleep(1.0)
        out = {}
        for name, t in left.items():
            rem = [{"pid": pid, "exe": self._redact(t["procs"][pid].exe)}
                   for pid in sorted(t["tree"])]
            out[name] = {"closed": not rem, "remaining": rem[:10],
                         "remaining_count": len(rem)}
        return out

    # ----------------------------------------------------------------- store
    def _app_store(self, app: App) -> str:
        return os.path.join(self.cfg.store, app.name)

    def _state_path(self, app: App) -> str:
        return os.path.join(self._app_store(app), "state.json")

    def _state(self, app: App) -> dict:
        raw = self.sys.read_text(self._state_path(app))
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _save_state(self, app: App, st: dict) -> None:
        self.sys.write_private(self._state_path(app), json.dumps(st, indent=2, sort_keys=True))

    def stored_copies(self, app: App) -> dict[str, dict]:
        base = self._app_store(app)
        out = {}
        for v in self.sys.listdir(base):
            meta_raw = self.sys.read_text(os.path.join(base, v, "meta.json"))
            if not meta_raw:
                continue
            try:
                meta = json.loads(meta_raw)
            except ValueError:
                continue
            out[meta.get("version", v)] = {**meta, "dir": os.path.join(base, v)}
        return out

    def _deb_fields(self, path: str) -> dict:
        r = self.sys.run(["dpkg-deb", "-f", path, "Package", "Version", "Architecture"])
        out = {}
        for line in r.out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
        return out

    def _build_matches_installed(self, pkg: str, arch: str, path: str) -> bool | None:
        """Compare the deb's md5sums with dpkg's record of the installed build."""
        inst = None
        for name in (f"{pkg}.md5sums", f"{pkg}:{arch}.md5sums"):
            inst = self.sys.read_text(os.path.join(self.dpkg_info, name))
            if inst is not None:
                break
        if inst is None:
            return None
        norm = lambda s: sorted(" ".join(l.split()) for l in s.splitlines() if l.strip())
        r = self.sys.run(["dpkg-deb", "-I", path, "md5sums"])
        if r.ok:
            return norm(inst) == norm(r.out)
        # Many vendor debs ship no md5sums member; dpkg then generates its own
        # record at install time. Hash the deb's files and check every
        # recorded file matches (conffiles are not in dpkg's record).
        listing = self.sys.deb_file_md5s(path)
        if listing is None:
            return None
        have = {}
        for line in listing.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                have[parts[1]] = parts[0]
        want = [l.split(None, 1) for l in inst.splitlines() if l.strip()]
        want = [w for w in want if len(w) == 2]
        if not want:
            return None
        return all(have.get(name) == md5 for md5, name in want)

    def _candidate_ok(self, app: App, path: str, version: str, arch: str) -> bool:
        f = self._deb_fields(path)
        return (f.get("Package") == app.package and f.get("Version") == version
                and f.get("Architecture") in (arch, "all"))

    def capture(self, app_name: str, dry: bool) -> dict:
        """Make sure the installed version has a verified copy in our store."""
        app = self._apps([app_name])[0]
        version, arch = self.installed(app.package)
        if version is None:
            return {"ok": False, "reason": "not installed"}
        have = self.stored_copies(app).get(version)
        if have:
            deb = os.path.join(have["dir"], have["file"])
            if self.sys.exists(deb) and self.sys.sha256(deb) == have.get("sha256"):
                return {"ok": True, "version": version, "source": "store",
                        "verified": have.get("verified"), "fallback": have.get("fallback", False)}

        found = None
        cache = os.path.join(self.archives, deb_name(app.package, version, arch))
        if self.sys.exists(cache) and self._candidate_ok(app, cache, version, arch):
            found = ("apt-cache", cache)
        elif version in self.repo_versions(app.package):
            found = ("apt-get download", None)
        else:
            for d in self.cfg.search_dirs:
                for f in self.sys.listdir(d):
                    if fnmatch.fnmatch(f, f"{app.package}_*.deb"):
                        p = os.path.join(d, f)
                        if self._candidate_ok(app, p, version, arch):
                            found = ("local-file", p)
                            break
                if found:
                    break

        fallback = False
        if found is None and app.name in self.cfg.fallback_rollback:
            best = None
            for d in [self.archives, *self.cfg.search_dirs]:
                for f in self.sys.listdir(d):
                    if not fnmatch.fnmatch(f, f"{app.package}_*.deb"):
                        continue
                    p = os.path.join(d, f)
                    fl = self._deb_fields(p)
                    v = fl.get("Version")
                    if fl.get("Package") != app.package or not v:
                        continue
                    if self.vercmp(v, "lt", version) and (best is None or self.vercmp(v, "gt", best[0])):
                        best = (v, p)
            if best:
                found = ("fallback-local-file", best[1])
                fallback = True

        if found is None:
            return {"ok": False, "version": version,
                    "reason": "no exact copy of the installed version in the apt cache, "
                              "the repo or the search dirs"}
        source, src = found
        res = {"ok": True, "version": version, "source": source, "fallback": fallback}
        if fallback:
            res["fallback_version"] = self._deb_fields(src)["Version"]
        if dry:
            res["action"] = f"would store from {source}"
            if src:
                res["verified"] = None if fallback else self._build_matches_installed(
                    app.package, arch, src)
                if res["verified"] is False:
                    return {"ok": False, "version": version, "source": source,
                            "reason": "deb does not match the installed build (md5sums differ)"}
            return res

        store_ver = res.get("fallback_version", version)
        dest_dir = os.path.join(self._app_store(app), store_ver.replace(":", "%3a"))
        self.sys.makedirs(dest_dir)
        if source == "apt-get download":
            r = self.sys.run(["apt-get", "download", f"{app.package}={version}"], cwd=dest_dir,
                             timeout=1800)
            files = [f for f in self.sys.listdir(dest_dir) if f.endswith(".deb")]
            if not r.ok or not files:
                return {"ok": False, "version": version, "reason": "apt-get download failed"}
            dest = os.path.join(dest_dir, files[0])
            if not self._candidate_ok(app, dest, version, arch):
                return {"ok": False, "version": version, "reason": "downloaded deb mismatch"}
        else:
            dest = os.path.join(dest_dir, os.path.basename(src))
            self.sys.copy_private(src, dest)
        verified = None if fallback else self._build_matches_installed(app.package, arch, dest)
        if verified is False:
            self.sys.rmtree(dest_dir)
            return {"ok": False, "version": version,
                    "reason": "deb does not match the installed build (md5sums differ)"}
        sha = self.sys.sha256(dest)
        meta = {"package": app.package, "version": store_ver, "arch": arch,
                "file": os.path.basename(dest), "sha256": sha, "source": source,
                "verified_against_installed": verified, "fallback": fallback,
                "captured_at": int(self.sys.now())}
        self.sys.write_private(os.path.join(dest_dir, "meta.json"), json.dumps(meta, indent=2))
        self.sys.write_private(os.path.join(dest_dir, "SHA256SUMS"),
                               f"{sha}  {os.path.basename(dest)}\n")
        res.update({"sha256": sha, "verified": verified, "action": "stored"})
        self.log(f"rollback copy for {app.name} {store_ver} stored from {source}")
        return res

    # --------------------------------------------------------------- status
    def status(self, apps: list[str]) -> dict:
        holds = self.holds()
        out = {"label": self.label, "apps": {}, "other_holds": holds["other"],
               "apt_busy": self.apt_busy()}
        procs = self.sys.procs()
        for app in self._apps(apps):
            ver, _ = self.installed(app.package)
            cand = self.candidate(app.package) if ver else None
            st = self._state(app)
            tree = self.app_tree(app, procs)
            if tree["tree"] and st and ver == st.get("to_version"):
                self._count_launch(app, st, tree, procs)
            copies = self.stored_copies(app)
            out["apps"][app.name] = {
                "installed": ver, "candidate": cand,
                "update_available": bool(ver and cand and cand != ver),
                "running": bool(tree["tree"]),
                "rollback": sorted(copies),
                "rollback_fallback": sorted(v for v, m in copies.items() if m.get("fallback")),
                "hold": holds["managed"].get(app.name),
                "rejected": self._rejected_version(app.package),
                "good_launches": st.get("good_launches"),
            }
        return out

    # ---------------------------------------------------------------- check
    def _age_h(self, ts: float | None) -> float | None:
        if ts is None:
            return None
        return round(max(0.0, self.sys.now() - ts) / 3600.0, 1)

    def apt_lists_age_h(self) -> float | None:
        """Hours since the system's apt lists were last refreshed: the update
        success stamp, or the lists/partial directory (touched on every
        `apt-get update`, even when no list changed)."""
        times = [t for t in (self.sys.mtime(self.apt_update_stamp),
                             self.sys.mtime(os.path.join(self.apt_lists, "partial")))
                 if t is not None]
        return self._age_h(max(times) if times else None)

    def _cache_root(self) -> str:
        base = self.sys.env().get("XDG_CACHE_HOME") or os.path.join(self.sys.home(), ".cache")
        return os.path.join(base, "twin-update", "apt")

    def _repo_uris(self, pkg: str) -> set[str]:
        """Repository base URIs that offer this package, from the system lists."""
        r = self.sys.run(["apt-cache", "policy", pkg])
        out = set()
        for line in r.out.splitlines():
            m = re.match(r"\s*-?\d+\s+((?:https?|ftp)://\S+)\s", line)
            if m:
                out.add(m.group(1).rstrip("/"))
        return out

    def _source_files(self, uris: set[str]) -> list[str]:
        files = []
        for f in self.sys.listdir(self.sources_dir):
            if not f.endswith((".sources", ".list")):
                continue
            path = os.path.join(self.sources_dir, f)
            if self._words(path) & uris:
                files.append(path)
        return files

    def _fresh_opts(self, root: str) -> list[str]:
        return ["-c", os.path.join(root, "apt.conf"),
                "-o", f"Dir::State::Lists={root}/lists",
                "-o", f"Dir::Cache={root}/cache",
                "-o", f"Dir::Etc::sourcelist={root}/sources.list",
                "-o", f"Dir::Etc::sourceparts={root}/sources.d",
                "-o", "Debug::NoLocking=1"]

    def fetch_lists(self, apps: list[str]) -> dict:
        """Fetch fresh package lists for the enabled apps' repositories into a
        user-owned cache, without root. /var/lib/apt is never written; the
        repositories' Signed-By keyrings and apt's signature checks apply as
        configured. Returns the apt options to read that cache."""
        per_app, uris = {}, set()
        for app in self._apps(apps):
            u = self._repo_uris(app.package)
            per_app[app.name] = u
            uris |= u
        files = self._source_files(uris)
        if not files:
            return {"ok": False, "reason": "no apt source file found for these apps"}
        root = self._cache_root()
        for d in ("lists/partial", "cache", "sources.d"):
            self.sys.makedirs(os.path.join(root, d))
        for f in self.sys.listdir(os.path.join(root, "sources.d")):
            self.sys.remove(os.path.join(root, "sources.d", f))
        for f in files:
            self.sys.symlink(f, os.path.join(root, "sources.d", os.path.basename(f)))
        self.sys.write_private(os.path.join(root, "sources.list"), "")
        self.sys.write_private(os.path.join(root, "apt.conf"), CLEAR_HOOKS)
        opts = self._fresh_opts(root)
        env = {**self.sys.env(), "LC_ALL": "C"}
        r = self.sys.run(["apt-get", "-q", *opts, "-o", f"APT::Sandbox::User={self.sys.whoami()}",
                          "-o", "Acquire::Retries=1", "-o", "Acquire::http::Timeout=20",
                          "-o", "Acquire::https::Timeout=20", "update"], timeout=120, env=env)
        lines = [l.strip() for l in (r.out + "\n" + r.err).splitlines()]
        bad = [l for l in lines if l.startswith(("E:", "Err:", "W:")) and any(k in l for k in FETCH_FAIL)]
        if not r.ok or bad:
            why = "timed out" if r.rc == 124 else (bad[0] if bad else f"apt-get exit {r.rc}")
            return {"ok": False, "reason": why[:200]}
        self.sys.write_private(os.path.join(root, "fetched-at"), str(self.sys.now()))
        fetched = {w for f in files for w in self._words(f)}
        covered = sorted(a for a, u in per_app.items() if u & fetched)
        return {"ok": True, "opts": opts, "apps": covered,
                "sources": sorted(os.path.basename(f) for f in files)}

    def _words(self, path: str) -> set[str]:
        text = self.sys.read_text(path) or ""
        return {w.rstrip("/") for w in re.split(r"[\s\]]+", text) if "://" in w}

    def check(self, apps: list[str], fresh: bool = True) -> dict:
        """Read-only update check. With fresh=True it first fetches the apps'
        repository lists into the user cache (no root); if that fails it uses
        the system lists and says why. Never sudo, never closes or installs."""
        lists = {"source": "system", "age_h": self.apt_lists_age_h()}
        f = None
        if fresh:
            f = self.fetch_lists(apps)
            root = self._cache_root()
            if f["ok"]:
                raw = self.sys.read_text(os.path.join(root, "fetched-at"))
                lists = {"source": "fresh", "age_h": self._age_h(float(raw)) if raw else 0.0,
                         "cache": self._redact(root), "cache_bytes": self.sys.dir_size(root),
                         "sources": f["sources"], "apps": f["apps"]}
            else:
                lists["fallback_reason"] = f["reason"]
        out = {"label": self.label, "apps": {}, "lists": lists,
               "apt_lists_age_h": lists["age_h"],
               "apt_busy": self.apt_busy(), "identity_ok": self.identity()["ok"]}
        tool = self._sync_hold_records()
        for app in self._apps(apps):
            ver, _ = self.installed(app.package)
            use_fresh = bool(f and f["ok"] and app.name in f["apps"])
            cand = self.candidate(app.package, f["opts"] if use_fresh else None) if ver else None
            newer = bool(ver and cand and cand != ver and self.vercmp(cand, "gt", ver))
            rec = self._hold_record(app.package, tool)
            rb_reason = self._rollback_hold_reason(app.package, tool)
            rejected = self._rejected_version(app.package, rec)
            apt_held = app.package in self.showhold()
            if newer and rb_reason and apt_held and rejected and cand == rejected:
                newer = False
            elif newer and rb_reason and apt_held and not rejected:
                newer = False
            out["apps"][app.name] = {"installed": ver, "candidate": cand,
                                     "update_available": newer,
                                     "lists": "fresh" if use_fresh else "system",
                                     "rollback_hold": rb_reason if apt_held else None,
                                     "rejected": rejected if apt_held else None}
        return out

    def _notice_state(self) -> dict:
        path = os.path.join(self.cfg.state_dir, "check-notices.json")
        raw = self.sys.read_text(path)
        try:
            seen = json.loads(raw) if raw else {}
        except ValueError:
            seen = {}
        return seen if isinstance(seen, dict) else {}

    def notice_due(self, kind: str, signature: str = "") -> bool:
        """True unless a notice of this kind was already recorded today.

        Read-only: does not write. Reminders repeat each calendar day while
        updates remain pending.
        """
        del signature
        today = time.strftime("%Y-%m-%d", time.localtime(self.sys.now()))
        rec = self._notice_state().get(kind) or {}
        return rec.get("date") != today

    def record_notice(self, kind: str, signature: str = "") -> dict:
        """Record that today's prompt question was shown (not merely launched)."""
        seen = self._notice_state()
        today = time.strftime("%Y-%m-%d", time.localtime(self.sys.now()))
        seen[kind] = {"date": today, "sig": signature, "handed_at": int(self.sys.now())}
        path = os.path.join(self.cfg.state_dir, "check-notices.json")
        self.sys.write_private(path, json.dumps(seen, indent=2, sort_keys=True))
        from twin_update import notice
        notice.clear_record_failure(self.sys, self.cfg.state_dir)
        return {"ok": True}

    def last_window_opened(self) -> str | None:
        rec = self._notice_state().get("updates") or {}
        ts = rec.get("handed_at")
        if not ts:
            return None
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def _count_launch(self, app: App, st: dict, tree: dict, procs) -> None:
        starts = sorted(procs[p].start for p in tree["mains"])
        if not starts:
            return
        seen = st.setdefault("seen_starts", [])
        if starts[0] not in seen:
            seen.append(starts[0])
            del seen[:-10]
            st["good_launches"] = int(st.get("good_launches", 0)) + 1
            self._save_state(app, st)

    # --------------------------------------------------------------- locks
    def acquire_locks(self, dry: bool, wait_s: float) -> dict:
        paths = self.cfg.machines[self.label].lock_paths
        results = {}
        deadline = self.sys.now() + wait_s
        for p in paths:
            shown = self._redact(p)
            if not self.sys.exists(p):
                parent = os.path.dirname(p)
                if not self.sys.isdir(parent):
                    results[shown] = "missing (parent dir missing)"
                    continue
                if dry:
                    results[shown] = "missing (a real run creates it empty and locks it)"
                    continue
                self.sys.write_private(p, "")
            while True:
                fd = self.sys.try_flock(p)
                if fd is not None:
                    break
                if self.sys.now() >= deadline:
                    break
                self.sys.sleep(2.0)
            if fd is None:
                results[shown] = "busy"
                continue
            if dry:
                self.sys.unlock(fd)
                results[shown] = "free"
            else:
                self._locks.append(fd)
                results[shown] = "held"
        ok = all(v in ("held", "free") or v.startswith("missing (a real") for v in results.values())
        return {"ok": ok, "locks": results}

    def release(self) -> dict:
        for fd in self._locks:
            self.sys.unlock(fd)
        self._locks = []
        self._drop_password()
        return {"ok": True}

    # ------------------------------------------------------------ update
    def apt_update(self) -> dict:
        r = self._sudo(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-o",
                        f"DPkg::Lock::Timeout={self.cfg.apt_lock_timeout_s}", "-q", "update"],
                       timeout=1800)
        return {"ok": r.ok, "err": r.err[-400:] if not r.ok else ""}

    def upgrade(self, app_name: str, version: str) -> dict:
        app = self._apps([app_name])[0]
        before, _ = self.installed(app.package)
        if app.package in self.showhold():
            self._sudo(["apt-mark", "unhold", app.package], timeout=120)
        r = self._sudo(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-o",
                        f"DPkg::Lock::Timeout={self.cfg.apt_lock_timeout_s}", "-o",
                        "Dpkg::Options::=--force-confold", "-y", "-q", "install",
                        "--only-upgrade", f"{app.package}={version}"], timeout=3600)
        after, _ = self.installed(app.package)
        ok = r.ok and after == version
        if ok and before and before != after:
            st = {"rollback_version": before, "to_version": after,
                  "updated_at": int(self.sys.now()), "verified": False, "good_launches": 0,
                  "seen_starts": []}
            self._save_state(app, st)
        self.log(f"upgrade {app.name} {before} -> {after} rc={r.rc}")
        return {"ok": ok, "before": before, "after": after,
                "err": "" if ok else (r.err[-400:] or r.out[-400:])}

    def smoke(self, app_name: str) -> dict:
        """Start the app in the user's graphical session, check it stays up, close gracefully."""
        app = self._apps([app_name])[0]
        if app.name in self.cfg.no_launch:
            return {"ok": True, "skipped": "no_launch: never started by twin-update"}
        # main_only: leftover helpers do not count. An unidentified main is
        # treated as running (fail closed — do not start over a live app).
        if self.running(app_name, main_only=True):
            return {"ok": False,
                    "reason": "app is running again; not killing a user-launched instance"}
        worker_root = os.path.join(self.sys.home(), ".config/Cursor/User/globalStorage/anysphere.cursor-agent-worker")
        def worker(p):
            return any(os.path.isabs(v) and os.path.realpath(v).startswith(worker_root + "/")
                       for v in (p.exe, *p.argv[:2]))
        before_all = {(p.pid, p.start) for p in self.sys.procs().values()}
        before = {(p.pid, p.start) for p in self.sys.procs().values() if worker(p)} if app.name == "cursor" else set()
        env = self._user_env()
        unit = f"twin-smoke-{app.name}-{int(self.sys.now())}.{os.urandom(2).hex()}"
        r = self.sys.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
                          "-p", "ExitType=cgroup", "--", *app.launch], env=env, timeout=60)
        if not r.ok:
            return {"ok": False, "reason": "could not start", "err": r.err[-300:]}

        def _collect_started(into):
            tree = self.app_tree(app)
            for pid in tree["mains"]:
                p = tree["procs"].get(pid)
                if p and (pid, p.start) not in before_all and (pid, p.start) not in into:
                    into.append((pid, p.start))
            return into

        started_mains = []
        sample_until = self.sys.now() + min(3.0, max(0.4, self.cfg.smoke_s))
        while True:
            _collect_started(started_mains)
            if started_mains or self.sys.now() >= sample_until:
                break
            self.sys.sleep(0.2)
        threshold = None
        if app.name == "cursor":
            main = self.sys.run(["systemctl", "--user", "show", unit, "--property=MainPID", "--value"], env=env, timeout=10)
            raw_pid = main.out.strip()
            if raw_pid.isdigit() and int(raw_pid) > 0:
                p = self.sys.procs().get(int(raw_pid))
                threshold = p.start if p else None
        stayed = True
        waited = 0.0
        handed = False

        def _smoke_up(active: bool) -> bool:
            if started_mains:
                procs = self.sys.procs()
                if any(pid in procs and procs[pid].start == start
                       for pid, start in started_mains):
                    return True
            if active:
                return True
            return False

        while waited < self.cfg.smoke_s:
            self.sys.sleep(1.0)
            waited += 1.0
            _collect_started(started_mains)
            a = self.sys.run(["systemctl", "--user", "is-active", unit], env=env)
            active = a.out.strip() in ("active", "activating")
            if not active:
                handed = True
                if not _smoke_up(False):
                    stayed = False
                    break
            if handed and not _smoke_up(False):
                stayed = False
                break
        _collect_started(started_mains)
        j = self.sys.run(["journalctl", "--user", "-u", unit, "--no-pager", "-o", "cat"], env=env)
        crashed = bool(CRASH_RE.search(j.out))
        procs = self.sys.procs()
        closed_via_unit = False
        if started_mains:
            for pid, start in started_mains:
                p = procs.get(pid)
                if p and p.start == start:
                    self.sys.term(pid, start)
        else:
            self.sys.run(["systemctl", "--user", "stop", unit], env=env, timeout=15)
            closed_via_unit = True
        deadline = self.sys.now() + self.cfg.close_timeout_s
        left = []
        while True:
            procs = self.sys.procs()
            left = [(pid, start) for pid, start in started_mains
                    if pid in procs and procs[pid].start == start]
            if not left or self.sys.now() >= deadline:
                break
            self.sys.sleep(1.0)
        rem = [{"pid": pid, "exe": self._redact(procs[pid].exe)}
               for pid, _start in left if pid in procs]
        wait = {"closed": not left, "remaining": rem[:10], "remaining_count": len(left)}
        ok = stayed and not crashed and wait["closed"]
        res = {"ok": ok, "stayed_up_s": waited if stayed else waited - 1, "crash_in_journal": crashed,
               "closed_gracefully": wait["closed"], "handoff": handed}
        if app.name == "cursor":
            res["stray_reaped"] = []
            if threshold is not None:
                for p in self.sys.procs().values():
                    if worker(p) and p.start >= threshold and (p.pid, p.start) not in before:
                        if self.sys.term(p.pid, p.start):
                            res["stray_reaped"].append({"pid": p.pid, "exe_basename": os.path.basename(p.exe)})
        if not wait["closed"]:
            res["remaining"] = wait["remaining"]
        if ok:
            st = self._state(app)
            if st:
                st["good_launches"] = int(st.get("good_launches", 0)) + 1
                self._save_state(app, st)
        return res

    def verify(self, app_name: str, version: str, smoke: bool = True) -> dict:
        app = self._apps([app_name])[0]
        inst, _ = self.installed(app.package)
        res = {"expected": version, "installed": inst, "dpkg_ok": inst == version}
        if res["dpkg_ok"]:
            vr = self.sys.run(["dpkg", "-V", app.package])
            res["dpkg_verify_clean"] = vr.ok and not vr.out.strip()
        if smoke and app.name in self.cfg.no_launch:
            res["smoke"] = {"ok": True, "skipped": "no_launch: never started by twin-update"}
            smoke = False
        if smoke and res["dpkg_ok"]:
            res["smoke"] = self.smoke(app.name)
        res["ok"] = res["dpkg_ok"] and (not smoke or res.get("smoke", {}).get("ok", False))
        st = self._state(app)
        if res["ok"] and st and st.get("to_version") == inst:
            st["verified"] = True
            self._save_state(app, st)
        return res

    def reopen(self, apps: list[str]) -> dict:
        env = self._user_env()
        out = {}
        for app in self._apps(apps):
            if app.name in self.cfg.no_launch:
                out[app.name] = {"ok": True, "skipped": "no_launch: left closed"}
                self.log(f"reopen {app.name}: skipped (no_launch)")
                continue
            if app.name in self._reopened or self.running(app.name, main_only=True):
                out[app.name] = {"ok": True, "skipped": "already open"}
                self._reopened.add(app.name)
                continue
            unit = f"twin-reopen-{app.name}-{int(self.sys.now())}"
            r = self.sys.run(["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit}",
                              "-p", "ExitType=cgroup", "--", *app.launch], env=env, timeout=60)
            out[app.name] = {"ok": r.ok}
            if r.ok:
                self._reopened.add(app.name)
        return out

    def pre_reopen_hook(self, app_name: str, smoke_ok: bool) -> dict:
        from twin_update.raft import pre_reopen
        return pre_reopen(self, app_name, smoke_ok)

    def raft_upload_start(self, snapshot: str) -> dict:
        from twin_update.raft import upload_start
        return upload_start(self, snapshot)

    def notify_tool(self) -> str | None:
        for tool in ("notify-send", "gdbus"):
            if self.sys.which(tool):
                return tool
        return None

    def notify(self, summary: str, body: str) -> dict:
        """Desktop notification; soft-fails (the JSON report is the record)."""
        tool = self.notify_tool()
        if tool == "notify-send":
            argv = ["notify-send", "--app-name=Twin Update", summary, body]
        elif tool == "gdbus":
            q = lambda s: "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
            argv = ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                    "--object-path", "/org/freedesktop/Notifications",
                    "--method", "org.freedesktop.Notifications.Notify",
                    q("Twin Update"), "0", q(""), q(summary), q(body), "[]", "{}", "8000"]
        else:
            return {"ok": False, "tool": None, "reason": "no notify-send or gdbus"}
        r = self.sys.run(argv, env=self._user_env(), timeout=15)
        return {"ok": r.ok, "tool": tool}

    # ------------------------------------------------------------ rollback
    def rollback_target(self, app_name: str) -> dict:
        app = self._apps([app_name])[0]
        inst, _ = self.installed(app.package)
        copies = {v: m for v, m in self.stored_copies(app).items() if v != inst}
        if not copies:
            return {"ok": False, "installed": inst, "reason": "no stored rollback copy"}
        best = None
        for v in copies:
            if best is None or self.vercmp(v, "gt", best):
                best = v
        m = copies[best]
        return {"ok": True, "installed": inst, "version": best,
                "fallback": m.get("fallback", False), "path": os.path.join(m["dir"], m["file"]),
                "sha256": m["sha256"]}

    def rollback_install(self, app_name: str, hold: bool = True) -> dict:
        app = self._apps([app_name])[0]
        t = self.rollback_target(app_name)
        if not t["ok"]:
            return t
        if self.sys.sha256(t["path"]) != t["sha256"]:
            return {"ok": False, "reason": "stored rollback copy fails its sha256 check"}
        r = self._sudo(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-o",
                        f"DPkg::Lock::Timeout={self.cfg.apt_lock_timeout_s}", "-o",
                        "Dpkg::Options::=--force-confold", "-y", "-q", "install",
                        "--allow-downgrades", t["path"]], timeout=3600)
        after, _ = self.installed(app.package)
        ok = r.ok and after == t["version"]
        res = {"ok": ok, "from": t["installed"], "to": after, "expected": t["version"],
               "sha256_ok": True, "fallback": t["fallback"]}
        if not ok:
            res["err"] = r.err[-400:] or r.out[-400:]
        if ok:
            st = self._state(app)
            st.update({"rolled_back_at": int(self.sys.now()), "rolled_back_from": t["installed"]})
            self._save_state(app, st)
            if hold:
                res["hold"] = self.hold(app.name, f"rollback from {t['installed']}")["ok"]
        self.log(f"rollback {app.name} {t['installed']} -> {after} ok={ok}")
        return res

    # --------------------------------------------------------------- prune
    def prune(self, apps: list[str], dry: bool) -> dict:
        out = {}
        now = self.sys.now()
        for app in self._apps(apps):
            st = self._state(app)
            inst, _ = self.installed(app.package)
            removed = []
            if st.get("verified") and (
                    now - st.get("updated_at", now) >= self.cfg.grace_days * 86400
                    or int(st.get("good_launches", 0)) >= self.cfg.grace_launches):
                keep = {st.get("rollback_version"), inst}
                for v, m in self.stored_copies(app).items():
                    if v not in keep:
                        if not dry:
                            self.sys.rmtree(m["dir"])
                        removed.append(v)
            out[app.name] = {"removed": removed, "dry": dry}
        return out

    def record_run(self, report: dict) -> dict:
        ts = report.get("started_ts", int(self.sys.now()))
        path = os.path.join(self.cfg.state_dir, "runs", f"{ts}-{report.get('kind', 'run')}.json")
        self.sys.write_private(path, json.dumps(report, indent=2, sort_keys=True))
        return {"ok": True, "path": self._redact(path)}

    # --------------------------------------------------------------- doctor
    def doctor(self) -> dict:
        ident = self.identity()
        checks = {"identity": ident["ok"], "sudo_group": ident["groups_sudo"],
                  "apt_idle": not self.apt_busy()}
        for tool in ("apt-get", "dpkg-deb", "systemd-run", "sudo"):
            checks[f"has_{tool}"] = bool(self.sys.which(tool))
        checks["notify_tool"] = self.notify_tool()
        locks = self.acquire_locks(dry=True, wait_s=0)
        checks["locks"] = locks["locks"]
        checks["locks_ok"] = locks["ok"]
        env = self._user_env()
        r = self.sys.run(["systemctl", "--user", "show-environment"], env=env, timeout=15)
        checks["graphical_session"] = r.ok and ("WAYLAND_DISPLAY=" in r.out or "DISPLAY=" in r.out)
        from twin_update.notice import launcher_name
        checks["terminal_launcher"] = launcher_name(self.sys.which)
        en = self.sys.run(["systemctl", "--user", "is-enabled", "twin-update-check.timer"],
                          env=env, timeout=15)
        checks["check_timer"] = en.ok and en.out.strip() in {
            "enabled", "enabled-runtime", "linked", "linked-runtime", "static"}
        show = self.sys.run(
            ["systemctl", "--user", "show", "twin-update-check.timer",
             "--property=UnitFileState,NextElapseUSecRealtime,ActiveState"],
            env=env, timeout=15)
        nxt = None
        for line in show.out.splitlines():
            if line.startswith("NextElapseUSecRealtime=") and line.split("=", 1)[1] not in ("", "0"):
                nxt = line.split("=", 1)[1]
        checks["check_timer_next"] = nxt
        checks["last_window"] = self.last_window_opened() or "none"
        from twin_update import notice
        slot = notice.window_lock_path(self.cfg.state_dir)
        stale, since = notice.slot_stale(self.sys, slot)
        checks["window_stale"] = stale
        checks["window_open_since"] = since
        checks["slot_pid_reused"] = notice.slot_pid_reused(self.sys, slot)
        rb = []
        for pkg, rec in self._sync_hold_records().items():
            if not isinstance(rec, dict):
                continue
            reason = str(rec.get("reason") or "")
            rejected = rec.get("rejected") or self._rejected_version(pkg, rec)
            if reason.startswith("rollback") or reason.startswith("failed-check"):
                bit = f"{pkg} at {rec.get('version') or '?'}"
                if rejected:
                    bit += f" (not {rejected})"
                bit += f" ({reason})"
                rb.append(bit)
        checks["rollback_holds"] = rb
        checks["record_notice_error"] = notice.last_record_error(self.sys, self.cfg.state_dir)
        names = self.sys.listdir(self.dpkg_updates)
        checks["dpkg_interrupted"] = any(
            n not in (".", "..") and not str(n).startswith(".") for n in (names or []))
        svc_path = os.path.join(self.sys.home(), ".config/systemd/user/twin-update-check.service")
        tmr_path = os.path.join(self.sys.home(), ".config/systemd/user/twin-update-check.timer")
        svc_text = self.sys.read_text(svc_path)
        tmr_text = self.sys.read_text(tmr_path)
        if svc_text is None:
            cat = self.sys.run(["systemctl", "--user", "cat", "twin-update-check.service"],
                               env=env, timeout=15)
            svc_text = cat.out if cat.ok and cat.out.strip() else None
        if tmr_text is None:
            cat = self.sys.run(["systemctl", "--user", "cat", "twin-update-check.timer"],
                               env=env, timeout=15)
            tmr_text = cat.out if cat.ok and cat.out.strip() else None
        checks["unit_warnings"] = notice.unit_drift_warnings(svc_text, tmr_text)
        exe = notice.check_exec_start(svc_text, self.sys.home())
        if exe:
            present = (self.sys.exists(exe) if os.path.isabs(exe)
                       else bool(self.sys.which(exe)))
            if not present:
                checks["unit_warnings"].append(
                    f"check service ExecStart does not exist: {exe}")
        return checks
