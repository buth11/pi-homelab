# Part 9: Deploying a Custom Tool as a systemd Timer

## Goal

A repeatable procedure for taking a custom script (Python, Bash,
whatever) from "works when I run it by hand" to "runs unattended, on
schedule, on a real cluster node" — without the usual string of
environment-mismatch surprises between where you wrote it and where it
actually has to run.

Worked example throughout: **`cluster_doctor.py`** (`SRE/scripts/`) — a
small CLI that queries Alertmanager + `kubectl`, diagnoses what's
currently wrong on the cluster, and logs a snapshot to SQLite. Built and
debugged in a devcontainer, deployed to run daily on `pi4-master`. Every
gotcha below is a real bug hit during that deployment, not a
hypothetical.

## Where the code lives

This repo's working directory is synced to every node via Syncthing, at
`/mnt/truenas-syncthing/Pi-Homelab/`. That means a script written and
tested in the devcontainer (`/workspaces/pi-homelab/...`) already exists,
byte-for-byte identical, on `pi4-master`
(`/mnt/truenas-syncthing/Pi-Homelab/...`) within seconds of saving —
**no manual copying, no `scp`, no git push required** for iterating on a
script that will run on a node.

Two consequences worth knowing up front:

- **Never hardcode the devcontainer's path** (`/workspaces/pi-homelab/...`)
  inside a script that will also run on a node — it doesn't exist there.
  Use `cd "$(dirname "$0")"` at the top of any wrapper shell script so
  the same file resolves its own location correctly regardless of which
  machine it's running on.
- **Syncthing conflicts are normal, not a sign something broke.** Editing
  the same file from two machines close together in time produces
  `*.sync-conflict-<timestamp>-*` files. Check `diff` against the current
  version before deleting — usually they're identical or an earlier draft,
  safe to remove, but check, don't assume.

## Gotcha 1: `kubectl` on a k3s node doesn't behave like kubectl

On a devcontainer or workstation, `kubectl` reads `~/.kube/config` by
default. On an actual k3s node, `/usr/local/bin/kubectl` is usually a
**symlink to the `k3s` binary itself** (`k3s kubectl ...`), and `k3s
kubectl` has its own default: it always tries
`/etc/rancher/k3s/k3s.yaml` first, **ignoring** `~/.kube/config` unless
you override it — even if `~/.kube/config` exists and is perfectly
valid. And `/etc/rancher/k3s/k3s.yaml` is root-only (`0600`), so a
non-root user (like the `buth11` service account used for automation
here) gets `permission denied`.

```bash
which kubectl              # confirm: is it a symlink to k3s?
ls -la $(which kubectl)
```

**Fix — give the automation user their own readable copy, once:**

```bash
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown "$USER:$USER" ~/.kube/config
chmod 600 ~/.kube/config
```

That alone isn't enough, though — `k3s kubectl` still won't look there on
its own. You have to **force it** via the `KUBECONFIG` environment
variable, set explicitly wherever the tool runs (see the systemd unit
below). This keeps the script itself portable — it never needs to know
about `/etc/rancher/k3s/k3s.yaml`, `sudo`, or any of this; it just calls
plain `kubectl` and trusts whatever `KUBECONFIG` its environment gives
it.

## Gotcha 2: the dev machine and the target node may run different Python versions

```bash
python3 --version
```

The devcontainer here runs Python 3.12. `pi4-master` (Debian 12
"bookworm" on a Raspberry Pi) ships Python 3.11. Code that's valid on
3.12 but not on 3.11 will pass every local test and then fail with a
`SyntaxError` the moment it actually runs on the real node — the exact
failure mode is invisible until deployment, by definition.

Concretely: PEP 701 (Python 3.12) allows reusing the same quote
character inside an f-string's `{}` — `f"{d["key"]}"` is legal on 3.12,
a `SyntaxError` on 3.11. Before relying on any newish syntax, check the
**target's** Python version, not just the one you're developing against.
Safe, version-independent form: use a different quote character inside
the braces than the one wrapping the f-string (`f"{d['key']}"`).

## Gotcha 3: a wrapper script can silently swallow the real exit code

A shell script's own exit code is the exit code of its **last** command
— not of whatever line actually did the meaningful work. This one is
easy to write by accident:

```bash
# BROKEN: reports success even if the python step crashed
kubectl port-forward -n monitoring alertmanager-... 9093:9093 &
sleep 2
python3 cluster_doctor.py
kill %1              # <- this is what determines the script's exit code
```

If `python3` throws an unhandled exception, `kill %1` still runs and
still succeeds (the port-forward really was there to kill) — so the
*script* exits `0`, and `systemctl status` cheerfully reports
`SUCCESS`, while the actual diagnostic silently failed and logged
nothing. This is worse than an outright crash: nothing looks wrong until
you check the tool's actual output, not its exit status.

**Fix — capture the meaningful command's exit code explicitly, do
cleanup, then propagate it:**

```bash
#!/bin/bash
cd "$(dirname "$0")"

kubectl port-forward -n monitoring alertmanager-... 9093:9093 &
sleep 2
python3 cluster_doctor.py --db ~/cluster_doctor.sqlite3
exit_code=$?          # capture immediately — before any other command touches $?
kill %1                # cleanup still always runs
exit $exit_code         # the script's real exit code is the one that matters
```

## Gotcha 4: don't put a live SQLite database inside a synced folder

If the tool's own output (a database, a log, any file that changes on
every run) lives inside the same Syncthing-synced directory as the code,
you get `sqlite3.OperationalError: database is locked` — Syncthing
scanning/transferring the file and the running process writing to it
collide, intermittently and non-deterministically, which makes it easy
to misdiagnose as "flaky" rather than structural.

**Fix — code stays synced, runtime data doesn't:**

```bash
python3 cluster_doctor.py --db ~/cluster_doctor.sqlite3   # not a path under the synced repo
```

Each machine running the tool gets its own independent, unsynced
database — which is usually what you want anyway: a node's own
diagnostic history doesn't need to be identical to every other node's.

## Writing the systemd units

Two files: a `oneshot` service that runs the wrapper once, and a timer
that decides when.

**`cluster_doctor.service`:**

```ini
[Unit]
Description=Run cluster-doctor health check and log to SQLite
After=network-online.target k3s.service
Wants=network-online.target

[Service]
Type=oneshot
User=buth11
Environment=KUBECONFIG=/home/buth11/.kube/config
ExecStart=/mnt/truenas-syncthing/Pi-Homelab/SRE/scripts/cluster_doctor_run.sh
```

Two details that are easy to get wrong:

- **`Type=oneshot`, never `Restart=always`.** `Restart=always` tells
  systemd to immediately relaunch the service whenever it exits — for a
  task that's *supposed* to run once and stop (because a timer controls
  its schedule), that produces an infinite back-to-back loop that
  completely ignores the timer. `Restart=` belongs on long-running
  daemons (`Type=simple`/`Type=notify`) meant to recover from crashes,
  not on a `oneshot` unit triggered externally.
- **`Environment=KUBECONFIG=...`** is what makes Gotcha 1's fix actually
  take effect for this specific service, without touching the script or
  the user's shell profile globally.

**`cluster_doctor.timer`:**

```ini
[Unit]
Description=Timer for cluster_doctor.service

[Timer]
# how long after boot before the first run
OnBootSec=10m
# how often to repeat, measured from the end of the previous run
OnUnitActiveSec=24h
Unit=cluster_doctor.service

[Install]
WantedBy=timers.target
```

**Comments must be their own line.** systemd unit files do *not*
support a trailing `# comment` after a real directive on the same
line — `OnBootSec=10m   # comment` is parsed as the *entire remainder of
the line* being the value, which fails with `Failed to parse timer
value, ignoring`. If every directive in a file has this problem, the
unit ends up with zero valid settings and systemd refuses to load it
outright (`Timer unit lacks value setting. Refusing.`). Put comments on
the line above, never trailing.

## Verify before installing

Don't wait for `systemctl` to reject a bad unit file — check it first,
on the actual target machine (unit file syntax support can vary subtly
by systemd version):

```bash
systemd-analyze verify ./cluster_doctor.service
systemd-analyze verify ./cluster_doctor.timer
echo "exit code: $?"        # 0 = clean
```

## Installing

```bash
# register units that live outside the standard search path
sudo systemctl link /mnt/truenas-syncthing/Pi-Homelab/SRE/scripts/cluster_doctor.service
sudo systemctl link /mnt/truenas-syncthing/Pi-Homelab/SRE/scripts/cluster_doctor.timer

sudo systemctl daemon-reload
sudo systemctl enable --now cluster_doctor.timer
```

**Use `systemctl link`, not a hand-rolled `ln -sf`.** A manual symlink
placed directly in `/etc/systemd/system/` isn't wrong, exactly, but
`systemctl enable` refuses to operate on a unit it detects as already a
manually-placed symlink (`Refusing to operate on alias name or linked
unit file`). `systemctl link` is systemd's own tool for "this unit file
lives somewhere else, please track it properly" — it creates the same
kind of symlink but registers it the way the rest of `systemctl`
expects. (If you do use raw `ln -sf`: remember a relative source path in
a symlink resolves relative to *where the symlink itself lives*, not
where you ran the command from — an easy way to accidentally create a
symlink pointing at itself.)

## Verifying it actually works — don't trust "SUCCESS" alone

```bash
sudo systemctl start cluster_doctor.service   # trigger one run on demand, don't wait for the timer
systemctl status cluster_doctor.service --no-pager -l
journalctl -u cluster_doctor.service --no-pager -n 30
systemctl list-timers cluster_doctor.timer    # confirm NEXT/LEFT populate a real future time
```

Per Gotcha 3, `status=0/SUCCESS` is necessary but **not sufficient** —
always also read the actual journal output for this run and confirm the
tool did what it claims (for `cluster_doctor`, specifically: a `logged
as snapshot N` line, not a traceback). The first real deployment run is
exactly when gotchas 1–4 above tend to surface, one at a time, each
looking like a different problem until you've seen the pattern.
