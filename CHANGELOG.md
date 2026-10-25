# Changelog

## 0.4.1 - 2026-10-25

- Confirming Update now starts the update in its own user scope that keeps
  this window's keyboard. Ctrl-C and closing the window do not stop it.
  Cancel with `systemctl --user stop <the update unit>` (the window prints
  the name). That stop reopens apps that were closed. The window waits for
  the update to finish cleanup and never force-kills it.
- A stop or a dropped connection while closing the second machine still
  reopens apps already closed on the first. The run is always recorded.
- Stopping during the final record/notify step still writes the run and
  tries every machine. A failed reopen is named in the report.
- `sudo apt-get` / `dpkg` is not force-killed on interrupt. If apt was
  mid-install, recover with `sudo dpkg --configure -a`. `doctor` warns
  when `/var/lib/dpkg/updates` is not empty.
- A failed check records that version only and does not upgrade the other
  machine. A newer candidate is still offered. A failed `apt-mark showhold`
  does not erase the rejected-version record. A failed hold after a failed
  check is reported, not swallowed.
- Broken or unreadable state files (wrong JSON type, a directory in the
  way) are ignored. `doctor` names the file. They never block an update
  and never crash the timer.
- Exit 3 means you stopped it. Exit 5 means the update stopped before
  finishing (see the report; a failed check may have left a version
  installed). A broken `config.toml` is a config error (exit 2) and the
  window waits for Enter.
- Both machines need twin-update 0.4.1 or newer. A peer with no version
  stops before anything is closed and says how to install it. On rollback,
  an older peer that does not understand `running` is still tolerated.
- After the window is gone, later questions have no keyboard and take the
  non-interactive stop path. A kill of the process cannot run cleanup.

## 0.4.0 (2026-10-25)

- `check --notify` opens a terminal window running `twin-update prompt`
  (cosmic-term --no-daemon, else x-terminal-emulator) instead of relying on notification
  buttons. The prompt lists pending apps and versions, warns that updating
  closes those apps on both machines for about 10 minutes, and asks
  `Update now? [y/N]`. Only a typed `y` starts `twin-update run` in that
  window; N, Enter, EOF or closing the window does nothing. The reminder
  returns the next day while updates remain pending.
- The timer unit is not a graphical session: the window is launched with
  WAYLAND_DISPLAY, DISPLAY, XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS
  from `systemctl --user show-environment`. If no graphical session is up,
  nothing is recorded; a failed launch is not retried until the next timer
  run. Persistent=true only catches up a missed timer run.
- A single-window lock/pid file prevents stacked windows. The prompt does
  not hold the sync lock while waiting; only `run` takes it.
- A plain, button-less desktop notice is only an extra reminder
  ("Updates waiting: see the Twin Update window").
- A window is recorded only after launch and a short process check, not
  when the launch call returns. Wording is "opened a window".
- `twin-update prompt --test` opens the same style of window with a fake
  pending list and "Test only, nothing will be updated".
- `doctor` reports the graphical session, terminal launcher, timer next
  fire, and last window-opened time. Notification-service state is info
  only.
- Smoke treats "transient unit exited and the app is running" as a
  single-instance hand-off success. Before upgrade and before smoke, a
  re-opened app is left alone and reported.
- The rollback question after a failed check times out to no. Ctrl-C or a
  dropped peer after close reopens the apps that were closed.
- Package ships `twin-update-check.service` and `.timer` with
  `SuccessExitStatus=10`. cosmic-term is started with `--no-daemon` so
  the window's main process stays alive.

## 0.3.0 (2026-10-23)

- Preserve per-app no-launch controls and default Cursor to close/update/stay closed.
- Add an opt-in locked snapshot hook with bounded background upload and snapshot-only external adapters.
- Bound peer hook waits and recover reopening through a fresh connection after timeout.
- Reap only new same-user Cursor smoke-test workers with guarded SIGTERM.


## 0.2.1 (2026-10-09)

* New `twin-update check [--notify] [--json] [--no-fetch]`: update check on
  both machines without root. It fetches fresh lists for only the enabled
  apps' repositories into a user cache (`~/.cache/twin-update/apt`), keeping
  the repositories' Signed-By keyrings and apt's signature checks, without
  running the system's apt hooks or writing `/var/lib/apt`; on failure it
  falls back to the system lists and says why. No sudo, no closes, no
  installs, no rollback captures. Exit codes: 0 no updates, 10 updates
  available, 1 check incomplete.
* List age is the real last refresh (fresh fetch time, or the system's
  update stamp / last `apt-get update`), not the last change of a file.
* `--notify` notifies only on the machine running the check; the same notice
  at most once per day.
* ssh failures are explained: name lookup failed, login refused, connection
  refused, interactive Tailscale SSH check, or timed out.
* `doctor` exits 4 when it finds a problem, including an unreachable other
  machine; a busy sync lock is a warning only.
* Works against a peer running 0.2.0 (falls back to `status`).
* README: example user-level systemd timer for a daily check.

## 0.2.0 (2026-10-09)

First public release.

* Fixed: `doctor`, `holds` and `unhold` printed nothing without `--json`
  (also when stdout is not a terminal). Every command now prints plain text;
  anything without a formatter falls back to the JSON report.
* Machines can use the same user name: the config's `machine_id_sha256`
  decides which table is "this machine".
* Machine labels are fully user-defined; docs and examples use generic labels.
* README: security model summary and generic Tailscale policy guidance.
* Packaging metadata for PyPI (classifiers, project URLs).

## 0.1.0 (2026-10-09, not published)

First version.

* `status`, `run`, `rollback`, `holds`, `unhold`, `doctor`, `local --serve`.
* Two machines, one run: identity check, lock files held, sudo asked once,
  same pinned version on both or skipped on both.
* Graceful close only (pidfd SIGTERM); a close timeout aborts on both machines
  before any update, and the apps it closed are reopened.
* Verified rollback copy captured before every update; exactly one kept per app,
  older ones pruned after verification plus a grace period.
* Holds not set by Twin Update are cleared unless `keep_holds` lists them.
* Launch smoke test, per-run JSON report (0600, labels only), desktop notification.
* Single-file zipapp build (compressed); standard library only.
* Rollback copies of vendor debs without an md5sums member are verified by
  hashing the deb's files against dpkg's record; a mismatch is refused, also
  in a dry run.
* Notification falls back to `gdbus` when `notify-send` is not installed and
  soft-fails when neither is; `doctor` reports `notify_tool`.
