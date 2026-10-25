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
* **Nothing resident.** No daemon. An optional user timer can open one
  terminal window for the daily prompt; apps close only inside a run you
  start or confirm.
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
   journal, closes gracefully. On failure you are offered a rollback (the question times out to no).
8. **Reopen** only the apps that were open before.
9. **Prune**: exactly one rollback copy per app is kept (the version before the
   latest update). Older copies are removed only after the new version passed
   its checks **and** a grace period (7 days or 2 good launches).
10. **Report**: `~/.local/state/twin-update/runs/<ts>-run.json` (0600, machine
    labels only) on both machines, plus a desktop notice such as
    `Cursor laptop-b 2.4.1→2.5.0 ✓` (via `notify-send`, or `gdbus`
    when `notify-send` is not installed). Daily update reminders use a
    terminal window (`twin-update prompt`), not notification buttons.

Apps are only ever closed inside a run you start or confirm. There is no
resident daemon. An optional user timer may open one prompt window; the ssh
session to the other machine exists only for the command. A `run` you start
from a terminal (without `--no-detach`) re-execs into
`systemd-run --user --scope`. A `run` confirmed with `y` in the prompt
window uses the same `--scope` path with a named `--unit=` so the window's
tty is kept: the sudo password and the rollback question stay interactive.
The run is in its own session: Ctrl-C in the prompt window does not stop
it, and closing the window (SIGHUP) does not stop it either. Cancel with
`systemctl --user stop <the update unit>` (the window prints the unit
name). That SIGTERM reopens apps that were closed for the run. systemd
gives the run scope 90 minutes to finish apt and reopen apps before it
is force-killed.
If the run is killed outright or apt is interrupted, recover with
`sudo dpkg --configure -a` (also noted by `twin-update doctor`).

## Other commands

```
twin-update check [--notify] [--json]      # read-only: are updates available? (no sudo)
twin-update prompt [--test]                # ask Update now? [y/N] (used by check --notify)
twin-update status [--local-only]          # per machine/app: installed, candidate, running, rollback copy, hold
twin-update rollback <app> [--machine <label>|both] [--dry-run] [--no-hold]
twin-update holds
twin-update unhold <app> [--machine ...] [--dry-run]
twin-update doctor                         # identity, sudo group, apt idle, locks, session, terminal, timer; exit 4 on problems
twin-update local --serve                  # per-machine engine; the peer runs this over ssh
```

### Daily check (optional)

`twin-update check` asks both machines, without root, whether the enabled apps
have newer versions. First it fetches fresh package lists for **only the
apps' own repositories** into a user-owned cache
(`~/.cache/twin-update/apt`, or `$XDG_CACHE_HOME/twin-update/apt`) with
`apt-get update` pointed at that cache: the repositories' existing
`sources.list.d` entries are linked in unchanged, so their `Signed-By` keyrings
and apt's signature checks apply as configured; the system's apt update hooks
are not run; `/var/lib/apt` is never written. Then `apt-cache policy` reads that
same cache. If the fetch fails or times out (2 minutes), it uses the system
lists and says so, with how long ago they were last refreshed. `--no-fetch`
skips the fetch. It never asks for sudo, closes nothing, installs nothing and
stores no rollback copies. (`twin-update run` still refreshes and installs
with the system apt, as root.)

With `--notify` it opens a **terminal window** on the machine it runs on
when updates are available (`cosmic-term --no-daemon` if present, else
x-terminal-emulator). That window runs `twin-update prompt`: it re-checks,
lists pending apps and versions (each machine's own version jump, joined
with ` / ` when they differ), warns which machines would close those apps
for about 10 minutes (it says "both machines" only when both need the
update), and asks `Update now? [y/N]`. If one machine could not be reached,
that status is in the question. Only a typed `y` starts `twin-update run`
in its own user unit; Ctrl-C or closing the prompt window does not stop
the update. Cancel with `systemctl --user stop <the update unit>` (the
window prints the name). The window waits for the run to finish. N or
Enter declines without
starting a run. The question is recorded for the calendar day only after
it has been shown (closing a still-blank window does not use up the day).
The timer service is not a graphical session, so the window is started
with WAYLAND_DISPLAY, DISPLAY, XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS
from the running user session. If no graphical session is up, or the window
fails to start, nothing is recorded and the launch is not retried until the
next timer run. `Persistent=true` only catches up a missed timer run (for
example if the machine was off at 09:13). A lock/pid file keeps a second
fire from stacking windows (`prompt --test` uses a separate test slot).
A plain, button-less desktop notice is only an extra reminder
("Updates waiting: see the Twin Update window").

`twin-update prompt --test` opens the same style of window with a fake
pending list and "Test only, nothing will be updated".

Unreachable machines are reported in plain words: name lookup failed, ssh
login refused, connection refused, Tailscale SSH asked for an interactive
check, or timed out (off, asleep or offline).

Exit codes for `check`: **0** no updates, **10** updates available (also when
only one machine could be checked), **1** check incomplete (error or other
machine unreachable, and no updates found), **2** usage or config error.

The package ships the units (also under `twin_update/systemd/`):

```ini
# ~/.config/systemd/user/twin-update-check.service
[Unit]
Description=Twin Update: daily read-only update check

[Service]
Type=oneshot
ExecStart=%h/.local/bin/twin-update check --notify
SuccessExitStatus=10
TimeoutStartSec=5min
Nice=10
IOSchedulingClass=idle

# ~/.config/systemd/user/twin-update-check.timer
[Unit]
Description=Twin Update: daily update check

[Timer]
OnCalendar=*-*-* 09:13:00
Persistent=true

[Install]
WantedBy=timers.target
```

`systemctl --user daemon-reload && systemctl --user enable --now twin-update-check.timer`

`SuccessExitStatus=10` is required because `check` exits 10 when updates
are available. The prompt window is a separate user unit; cosmic-term is
started with `--no-daemon` so its main process stays alive for the life
of the window.

`rollback` checks the stored copy's sha256, closes the app gracefully, installs
it with `apt-get install --allow-downgrades ./<deb>`, sets `apt-mark hold`
(recorded as a Twin Update rollback hold of that rejected version). The
daily prompt does not re-offer that same version while the apt hold
remains; a newer candidate is still offered. `twin-update holds` and
`doctor` show the rejected version; `twin-update unhold <app>` clears
the record even if apt already says the package is not held. Then it
verifies and reopens the app if it was open.

Both machines need this same twin-update installed before the first
mixed `run`. If the other machine reports no `version`, `run` stops
before closing anything, says to install this twin-update there
(`pipx install twin-update` 0.4.1 or newer, or copy the zipapp to
`remote_command`), and changes nothing.

A failed smoke/check does not upgrade the other machine unless you
explicitly roll back or the check succeeded. The failed version is
held (only that version). The window inherits the tty, so the sudo
password and the rollback question stay interactive there. After the
window is gone, later questions have no tty: the run stops before the
other machine, prints `twin-update rollback`, reopens what it closed,
and exits non-zero. Closing the window or Ctrl-D at the rollback
question is treated as no: apps reopen, the failed version stays.

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

Standard library only. Exit codes (`check` differs, see above): 0 ok / dry-run,
4 `doctor` found a problem (including an unreachable other machine; a busy
sync lock is only a warning), 1 the update failed, 2 usage or config,
3 you stopped it (apps that were closed are reopened),
5 the update stopped before finishing (see the report; a failed check may
have left a version installed). If apt was mid-install, recover with
`sudo dpkg --configure -a`.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Cursor launch policy and optional life-raft

Cursor is in `apps.no_launch` by default. If it is open when an update or rollback
needs to close it, it is closed gracefully and updated, then left closed. Neither
the smoke check nor reopen starts it. The opt-in raft hook never overrides this
policy. Existing per-app no-launch lists are preserved. Other apps retain their
configured launch behavior. No installed tool or machine configuration is
changed by this policy.

The optional `[raft]` hook applies only when Cursor reopening is explicitly
permitted, a real smoke check passed, and the machine is allowed. It takes the
configured shared backup lock before checking processes and database holders,
then creates a private local SQLite snapshot. It checks holders again and discards
a raced or failed snapshot. Hook errors and timeouts cannot suppress reopening
for apps whose policy permits it; `no_launch` apps remain closed throughout.

The snapshot is queued to a bounded user service after reopen. Its helper takes
the same lock with a 30-second wait, logs lock/start/upload failures, and retains
the snapshot on failure. The latest two snapshots are retained by default; older
ones are pruned under the shared lock. A successful upload must pass a downloaded
content comparison before the local snapshot is deleted. Folders use 0700 and
files 0600. Snapshot data and external adapters are never included in this repo.

`raft.lock_path` and `raft.remote` are required when enabling this feature. Use
the actual nightly-backup lock; do not invent a second lock. `remote_command`
must resolve to the installed executable's absolute path for the background
service; the interpreter is also absolute. A custom `--config` path is retained.

External snapshot-only adapter protocol 1 is required in `script_dir`:

- `twin-update-raft.json`: exactly `{"protocol": 1}`.
- `backup_cursor.py --source DB --output SNAPSHOT_DB`: read-only SQLite backup,
  local only; no upload or application launch.
- `export_cursor_return.py SNAPSHOT_DIR`: prepare `SNAPSHOT_DIR/payload` using
  only the captured database, never the live profile.
- `verify_cursor_payload.py SNAPSHOT_DIR`: verify the prepared payload; nonzero
  aborts the upload.
- `make_return_manifest.py SNAPSHOT_DIR`: add the payload integrity manifest.

Legacy fixed-path scripts are incompatible and must not be relabeled as protocol
1. Prepare separate adapters outside the repo, test with invented data, and retain
existing nightly scripts. The hook skips safely until compatible adapters exist.

The smoke-test reaper is relevant only to apps explicitly allowed to launch. For
Cursor it records pre-existing worker identities, obtains the smoke main process
start tick, then uses guarded SIGTERM only for newly started same-user workers
under Cursor's worker directory (executable or first two argv paths). No known
main start tick means no reaping. It never sends SIGKILL.
