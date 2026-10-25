# Changelog

## Unreleased

- Confirming `y` starts `run` with `systemd-run --user --scope` (named
  `--unit=`, no `--pipe`/`--collect`/`--no-block`) so the window's tty is
  inherited: the sudo password and the rollback question stay interactive.
  The run is in its own session. Ctrl-C in the prompt window and closing
  that window (SIGHUP) do not stop the update. Cancel with
  `systemctl --user stop <unit>` (SIGTERM): that reopens apps that were
  closed. The prompt waits for the run to finish its own cleanup and
  never SIGKILL's it. A missing tty password fails with a clear message
  and no traceback.
- A failed check (no tty, declined rollback, timeout, or closed window)
  does not upgrade the other machine. The failed version is recorded
  (only that version is hidden; a newer candidate is still offered).
  Apps that were closed are reopened; the run is recorded; the report
  says `twin-update rollback`.
- SIGTERM / `systemctl --user stop` during close, install, or the
  rollback question reopens closed apps exactly once. SIGHUP does not
  cancel the run. If the process is SIGKILL'd or apt is killed, recover
  with `sudo dpkg --configure -a`; `doctor` warns when
  `/var/lib/dpkg/updates` is not empty.
- Smoke retries sampling the processes it started, then stops its own
  unit if none appeared. It never signals a process that was already
  running. Watch uses those started processes / the smoke unit, not
  leftover helpers or an unrecognized name.
- The window slot merges under the flock. Once the window marked itself
  started, a later launcher write keeps its pid and start ticks and may
  only add `unit`. MainPID 0 does not clear the slot while the unit is
  up. A reused pid or a window open more than 24 hours does not block a
  new window; `doctor` reports both.
- Version mismatch still stops `run` before any close. `rollback` warns and
  proceeds per machine.
- A rollback or failed-check hold stores the rejected version and
  suppresses only that version. `unhold` clears the record even if apt
  already says the package is not held; `check` and `doctor` ignore a
  leftover record. The prompt and `run` agree: a hold on one machine
  that would skip the app is shown, not offered as a normal update.
  Both machines need this same twin-update installed before the first
  mixed `run`; a peer with no `version` stops before any close and says
  how to install it. Nothing was changed in that case.
- Config errors in a prompt window show the error and wait for Enter.
  Any error before the prompt question also waits for Enter.
  `Checking...` is printed before the ssh build. A failed `record_notice`
  is shown in the window and in `doctor`. A one-machine pending app
  is listed in the close warning even when another app is on both machines.
  A peer that reports no `version` is treated as older and `run` stops
  before any close. `doctor` warns if the check service ExecStart path
  does not exist. Prompt/run unit names include the pid and a random
  suffix so two launches in the same second do not collide.
  gnome-terminal / kgx fallback uses `--wait`; unknown
  `x-terminal-emulator` targets stay `-e` and are verified via unit
  ActiveState, not the client pid. The CI workflow display name is
  `Checks`.
  README says terminal `run` uses `--scope`.
- Test prompt windows use their own slot (`prompt.window.test`) so a leftover
  test window cannot block the real daily window. `check --notify` says since
  when a real window is already open. `doctor` warns if that real window has
  been open for more than 24 hours. The slot file is written in place so the
  flock is not dropped; a dead pid does not block a new window, and two
  launchers cannot both start one.
- The prompt prints "Checking..." before the re-check. A check error shows
  the real per-machine error and waits for Enter; it never claims nothing is
  pending. "Nothing pending" also waits for Enter. Only an explicit N closes
  without a pause. The day is recorded after the question is shown, not when
  the window is launched. If one machine is unreachable, the question names
  that machine.
- Window text lists closures per machine and only says "both machines" when
  both need the update. Version jumps are joined with " / " like `check`.
- A failed window launch is not retried until the next timer run.
  `Persistent=true` only catches up a missed timer run.
- `systemd-run --no-block` reporting MainPID 0 is treated as "still
  starting" and polled briefly; only an inactive or failed unit is a launch
  failure.
- Smoke keeps watching for the rest of the interval after a single-instance
  hand-off, so a later crash is not missed. The "app is running again" guard
  matches the app's main process only, not leftover helpers.
- An older peer that rejects the `running` op is tolerated (the old
  behaviour). Apps still reopen at the end of `run`.
- Terminal fallback flags depend on the resolved launcher (gnome-terminal
  style uses `--`). `doctor` warns when the installed check units lack
  `SuccessExitStatus=10` or differ from the shipped units in a way that
  matters. `twin-update --help` no longer shows a hidden `raft-upload`
  line. The zipapp build is reproducible (fixed timestamps, honours
  `SOURCE_DATE_EPOCH`).
- The ssh child ignores SIGTERM/SIGINT and stale replies are skipped by
  request id so a stop of the run scope can still reopen the other
  machine. An unexpected error writes `~/.local/state/twin-update/last-error.log`
  and prints a short line pointing at it.
- Known limits: a real desktop window, a real `systemctl --user stop` of
  a run scope on two machines, and the first real timer run were not
  exercised here. After the window is gone, later questions have no tty
  and take the non-interactive stop path. SIGKILL cannot run cleanup.

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
