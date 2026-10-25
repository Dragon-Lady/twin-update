# Security notes

## sudo password

* Asked **once** per run with `getpass` (no echo).
* Locally it is written only to the stdin of `sudo -k -S -p '' -- <command>`.
  `-k` makes sudo ignore cached credentials so it always consumes exactly that
  line; nothing left over reaches the command's stdin.
* For the other machine it travels **inside the run's ssh session** as one JSON
  line on the remote `twin-update local --serve` stdin, and is used the same way
  there.
* It is never placed in argv, environment variables, files, reports or logs.
  The serve loop never echoes request arguments. Both engines keep it in a
  `bytearray` and overwrite it with zeros when the run ends (Python may still
  hold transient copies until garbage collection; that is a known limit).
* The test suite asserts that the password appears in no argv, env, log line,
  report or file, and only on sudo's stdin.

## What runs as root

Only: `true` (password check), `apt-get update`, `apt-get install
--only-upgrade <pkg>=<version>`, `apt-get install --allow-downgrades <stored
deb>` (after its sha256 check), `apt-mark hold|unhold <pkg>`.

## Processes

Apps are closed only with SIGTERM through a pidfd after re-checking the
process start time (no PID-reuse races). Twin Update never sends SIGKILL; a
close timeout aborts the run on both machines before any update.

## Network

No resident daemon. The ssh session (BatchMode, host keys checked) exists
only while a command runs. An optional user timer may run
`twin-update check --notify` and open one terminal window for the daily
prompt; that window is a normal user process and exits when the prompt
does. Notification actions are not used.

## Trust boundaries

* Both machines must be yours. The sudo password typed on one machine is used
  on the other, so the other machine and the ssh link to it must be trusted
  as much as the one you type on.
* Anyone who can already ssh in as your user on either machine could run
  Twin Update there, but still needs the sudo password for every root step.
* Apps come from each machine's own apt sources and their signing keys; Twin
  Update does not add repositories or keys and does not copy packages between
  machines.
* Rollback copies are stored per user (0700 directories, 0600 files) with a
  sha256 that is checked before any rollback install.

## Reporting

Please report issues privately through the repository's security advisories.


## Opt-in snapshot exception

The default remains no application-database access. The explicitly enabled raft
path may make a read-only SQLite backup only while Cursor has no process tree or
known same-user database descriptor holders and the shared backup lock is held.
Checks are repeated after capture; this reduces races but cannot prevent an
uncooperative external process from launching. The snapshot is discarded when
activity is detected. Background export reads only the captured snapshot.
Adapters and the configured destination are trusted operator-managed inputs.
No existing nightly scripts, credentials, live database or remote files are
modified by the draft build. Cursor remains in the default no-launch list.
