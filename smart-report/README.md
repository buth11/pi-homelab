# smart-report — on-demand SMART wear report for every disk

One command collects SMART data from every disk in the homelab (K3s nodes,
Proxmox, TrueNAS) over SSH. It prints health, how much has been written,
wear, write rate per day and projected remaining life. Each run also
appends a snapshot to a local history, and the trend comes from that
history. Run it every week or two and the projection settles into
something you can plan replacements and purchases from.

```bash
./smart-report/smart-report.py                    # all hosts, records a snapshot
./smart-report/smart-report.py --only truenas     # one host (repeatable)
./smart-report/smart-report.py --no-save          # look without recording
./smart-report/smart-report.py --history          # recorded history, no SSH
./smart-report/smart-report.py --json             # normalized data, for scripts
```

Exit code: `0` all OK, `1` a warning or an unreachable host, `2` something
critical.

Requirements on the machine running it: Python 3.9+ (standard library only)
and SSH keys to the hosts in [hosts.conf](hosts.conf).

## Reading the table

| Column | Meaning |
|---|---|
| WRITTEN | Total host writes over the drive's life. NVMe: `data_units_written`. SATA: ATA Device Statistics `Logical Sectors Written`. Attribute 241 is a last resort because its unit differs by vendor (it is GiB on the g3 SATA SSD). |
| WEAR | The drive's own endurance counter: NVMe `percentage_used`, SATA "Percentage Used Endurance Indicator", or a named life-remaining attribute. It can go past 100%. |
| GB/DAY | Write rate. With history, it is measured per calendar day against the **oldest** snapshot. Without history it is the lifetime average per *power-on* day (marked `*`). That overstates the rate on g3-worker3, which is shut down part of the day. |
| LIFE LEFT | The more pessimistic of two estimates: (a) the % used trend (needs ≥ 60 days of history, because the counter moves in whole percent), otherwise the lifetime average; (b) rated TBW from [endurance.conf](endurance.conf) at the current GB/DAY. |
| STATUS | `OK` / `WARN` / `CRIT`. `SKIP` = HDD spun down. `INFO` = no SMART to read (SD card, virtual disk). |

Warnings are raised for: SMART health failed, NVMe critical warning or
spare blocks at the threshold, media errors, reallocated, pending or
uncorrectable sectors, CRC errors, wear ≥ 70% (critical at 90%), ≥ 80% of
rated TBW, projected life under 3 years (critical under 1), and
temperature ≥ 70 °C for SSDs or ≥ 50 °C for HDDs. All thresholds are
constants at the top of the script.

**Fill in [endurance.conf](endurance.conf)** for each SSD (TBW from the
datasheet). Without it the projection depends only on the drive's
coarse % used counter.

## Spun-down HDDs are not woken

smartctl runs with `-n standby` and disks are found with `--scan`, not
`--scan-open`. Neither sends a command to a sleeping disk. A tank-bulk HDD
that [truenas/hdd-idle](../truenas/hdd-idle/hdd-idle.sh) has spun down shows
as `SKIP`. To see its data, run the report while the pool is in use.

## History

Stored in `~/.local/share/smart-report/history.jsonl`, one line per disk per
run. It is kept outside the repo on purpose: it holds serial numbers, and
the repo syncs to GitHub. Disks are matched by serial number, so moving a
disk to another host or slot does not break its trend. Delete the file to
start over.

## Host setup

The SSH user must log in with a key and either be root or have
passwordless sudo for **`/usr/sbin/smartctl` only**.

**K3s nodes**: `ansible/roles/smartmontools` installs the package
(currently only g3-worker3; the Pis boot from SD cards, which have no
SMART). `buth11` already has passwordless sudo there.

```bash
cd ansible && ansible-playbook -i inventory.ini site.yml --tags smartmontools
```

**Proxmox** (smartmontools is preinstalled), as root:

```bash
apt install -y sudo
useradd -m -s /bin/bash smartreport
install -d -m 700 -o smartreport -g smartreport /home/smartreport/.ssh
# restrict = no shell PTY, no forwarding; from = LAN only (Tailscale traffic
# arrives SNATed from the subnet router's LAN address, so it still matches)
echo 'restrict,from="192.168.50.0/24" <contents of ~/.ssh/id_ed25519.pub>' \
  > /home/smartreport/.ssh/authorized_keys
chown smartreport: /home/smartreport/.ssh/authorized_keys
chmod 600 /home/smartreport/.ssh/authorized_keys
echo 'smartreport ALL=(root) NOPASSWD: /usr/sbin/smartctl' > /etc/sudoers.d/smartreport
chmod 440 /etc/sudoers.d/smartreport && visudo -cf /etc/sudoers.d/smartreport
```

**TrueNAS SCALE** (WebUI only, per the TrueNAS config policy):

1. *Credentials → Users → Add*: username `smartreport`, *Disable Password*
   on, shell `bash`, paste the public key into *Authorized Keys*, and under
   *Allowed sudo commands with no password* enter `/usr/sbin/smartctl`.
2. *System → Services → SSH*: enable it and tick *Start Automatically*.
   In its settings, make sure *Allow Password Authentication* is **off**.

Check with `ssh smartreport@<host> 'sudo -n /usr/sbin/smartctl --version'`.

**Security note:** sudo on `smartctl` also lets that account change drive
settings or start self-tests (`-s`, `-t`), but it cannot read data or get
root. The account is key-only, SSH-only and does nothing else. That is
the accepted trade-off. Sudoers argument patterns would not narrow it
reliably, because `*` also matches extra arguments.

## Not covered

- **SD cards** on pi4-master and pi4-worker2: listed (name, manufacture
  date), but they have no SMART and wear cannot be read.
- **k3s-burst-worker**: virtual disk. Its physical disks show under
  `proxmox`.
- **//ASUS/Crucial_2TB**: USB disk on the router, SMART not reachable.
