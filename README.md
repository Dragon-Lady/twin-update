# Twin Update

Update the same desktop apps on two Linux machines in **one run**, keep a
verified rollback copy of what was installed, and roll back with **one command**.

It targets Debian/Ubuntu-family desktops (tested on Ubuntu 24.04-based
systems with Python 3.12; needs Python 3.11+, systemd user sessions and apt)
and apps installed as **apt packages from their vendor repos**. Built-in
entries: Cursor (`cursor`), ChatGPT desktop (`chatgpt`) and Grok Bot desktop
(`grok-bot`). Other apt-packaged apps can be added in the config. Snaps,
Flatpaks, AppImages and CLIs are out of scope.

Machine labels are whatever you name them in the config; the examples below
use `desktop-a` and `laptop-b`.

## Security model, in short

* **Two machines you control, one trusted link.** The machine you start the
  run on reaches the other over `ssh` (BatchMode, host keys checked):
  Tailscale SSH or an ordinary ssh key. Without that link only
  `--local-only --dry-run` works.
* **sudo is asked once per run** and checked on both machines. The password
  goes only to `sudo -S` on stdin (over the run's ssh session for the other
  machine); never argv, environment, files, logs or reports. See
  [SECURITY.md](SECURITY.md).
* **apt only.** Root runs only `apt-get update`, `apt-get install` of a pinned
  version or of a stored, sha256-checked `.deb`, and `apt-mark hold/unhold`.
  Each machine installs from its own configured apt sources.
* **Graceful closes only.** SIGTERM, never SIGKILL. If an app does not close
  in time, the run **aborts on both machines before anything is updated**.
* **Rollback copy before every update**, verified against what dpkg installed;
  no verified copy on either machine means the app is skipped on both.
* **Nothing resident.** No daemon, timer or listener; apps close only inside a
  run you start.
* It restores program files, not app data (see below).

## What a run does

```
twin-update run [--apps cursor,chatgpt] [--dry-run]
```

1. **Preflight on both machines**: identity (`whoami`, optional machine-id) must
   match the config; apt/dpkg must be idle; every `lock_paths` file is locked
   for the whole run, so a sync or backup job cannot run during the update.
   Asks for the sudo password **once** and checks it on both machines, then
   `apt-get update` on both.
2. **Plan**: an app is updated only if both machines have it installed, are
   offered the **same** candidate version, and no kept hold blocks it. Otherwise
   it is skipped on both, with the reason. The exact version is pinned.
3. **Rollback copy first**: the installed `.deb` is stored in
   `~/.local/share/twin-update/rollback/<app>/<version>/` with a sha256, taken
   from the apt cache, `apt-get download <pkg>=<installed>`, or a matching local
   `.deb`. It is checked against dpkg's md5sums of the installed build (for
   vendor debs without an md5sums member, the deb's files are hashed in a
   stream and compared). A mismatch is refused. No copy on either machine =
   the app is skipped on both.
4. **Holds**: holds on these apps that Twin Update did not set are cleared (one
   report line each) unless `keep_holds` lists them. Holds on other packages
   are only reported.
5. **Graceful close on both**: SIGTERM (via pidfd) to the main processes of the
   app's process tree, found by executable path and the package's file list.
   No force-kill. If anything is still running after `close_s`, the run
   **aborts on both machines before any update**, names the app and process,
   and reopens what it closed.
6. **Update** each machine from its own repo: `apt-get install --only-upgrade
   <pkg>=<version>`. Nothing is copied between machines.
7. **Verify**: dpkg version, `dpkg -V`, and a launch smoke test in the graphical
   session (`systemd-run --user`): stays up `smoke_s` seconds, no crash in the
   journal, closes gracefully. On failure you are offered a rollback.
8. **Reopen** only the apps that were open before.
9. **Prune**: exactly one rollback copy per app is kept (the version before the
   latest update). Older copies are removed only after the new version passed
   its checks **and** a grace period (7 days or 2 good launches).
10. **Report**: `~/.local/state/twin-update/runs/<ts>-run.json` (0600, machine
    labels only) on both machines, plus a desktop notification such as
    `Cursor laptop-b 2.4.1→2.5.0 ✓` (via `notify-send`, or `gdbus`
    when `notify-send` is not installed).

Apps are only ever closed inside a run you start. There is no daemon, timer,
watcher or listener; the ssh session to the other machine exists only for the run.
The run moves itself into its own `systemd-run --user --scope`, so closing the
app whose terminal launched it does not stop it.

## Other commands

```
twin-update status [--local-only]          # per machine/app: installed, candidate, running, rollback copy, hold
twin-update rollback <app> [--machine <label>|both] [--dry-run] [--no-hold]
twin-update holds
twin-update unhold <app> [--machine ...] [--dry-run]
twin-update doctor                         # identity, sudo group, apt idle, locks, session, ssh
twin-update local --serve                  # per-machine engine; the peer runs this over ssh
```

`rollback` checks the stored copy's sha256, closes the app gracefully, installs
it with `apt-get install --allow-downgrades ./<deb>`, sets `apt-mark hold`
(recorded as Twin Update's own hold; the next deliberate `run` releases it),
verifies, and reopens the app if it was open.

A rollback restores **program files, not app data**. If a new version migrated
its settings or databases, keep your own data backups. Twin Update never opens,
copies or modifies app databases.

## Install (each machine)

```
pipx install twin-update          # or: uv tool install twin-update
```

or the single-file zipapp from a release:

```
install -D -m 0755 twin-update.pyz ~/.local/bin/twin-update
```

Install it at the same path on both machines (`remote_command` in the config
points to it). Then create `~/.config/twin-update/config.toml` (mode 0600)
from `config.example.toml`. The same file works on both machines. Check with
`twin-update doctor`, then `twin-update run --dry-run`.

### Transport

The machine you run on reaches the other with plain `ssh` (BatchMode, host
keys checked, no password prompts). The remote side runs
`twin-update local --serve` for the length of the run. Two options:

**Tailscale SSH.** On each machine that should accept runs:
`sudo tailscale set --ssh`. "Shields up" blocks incoming connections, Tailscale
SSH included, so it must be off on those machines; let the policy do the
limiting instead. Example policy fragment with tags (merge it into your
policy; tagging a device makes it tag-owned rather than user-owned):

```json
{
  "tagOwners": {
    "tag:twin-a": ["autogroup:admin"],
    "tag:twin-b": ["autogroup:admin"]
  },
  "grants": [
    { "src": ["tag:twin-a"], "dst": ["tag:twin-b"], "ip": ["tcp:22"] },
    { "src": ["tag:twin-b"], "dst": ["tag:twin-a"], "ip": ["tcp:22"] }
  ],
  "ssh": [
    { "action": "accept", "src": ["tag:twin-a"], "dst": ["tag:twin-b"], "users": ["alice"] },
    { "action": "accept", "src": ["tag:twin-b"], "dst": ["tag:twin-a"], "users": ["alice"] }
  ]
}
```

Use `"action": "accept"`: `"check"` needs an interactive browser login and a
BatchMode run cannot answer it. If your policy still has an allow-all rule,
it also lets everything else in the tailnet reach these machines; narrow it.
Keep only the direction you need if you always start runs on the same machine.

**Plain ssh key.** A dedicated key without a passphrase prompt (or one held by
your agent) in the other machine's `authorized_keys`, ideally limited with
`from="<the other machine's address>"`, and its host key already in
`known_hosts`.

## Development

```
PYTHONPATH=src:tests python3 -m unittest discover -s tests
python3 tools/build_zipapp.py        # -> dist/twin-update.pyz
```

Standard library only. Exit codes: 0 ok / dry-run, 1 failed, 2 usage or config,
3 aborted (nothing changed).

## License

Apache License 2.0. See [LICENSE](LICENSE).
