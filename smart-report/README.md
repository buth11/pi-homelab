# smart-report — SMART wear report for every disk

Collects SMART data from every disk in the homelab (K3s nodes, Proxmox,
TrueNAS) over SSH and answers one question: **how fast is each drive wearing
out, and how long will it last at the current usage?** Every scan is stored
in SQLite, and the projections come from that history. Scan every few days
and the numbers settle into something you can plan purchases from.

There are two front ends with the same code and the same numbers:

- **Dashboard, Disks tab** (http://192.168.50.58). *Scan now* runs a scan from
  the dashboard backend. The long-term history lives there.
- **CLI.** By default it asks the dashboard to scan and prints the result;
  `--local` scans over SSH from your machine.

```bash
./smart-report/smart_report.py                 # scan via the dashboard, print the table
./smart-report/smart_report.py --no-scan       # last dashboard scan, no new scan
./smart-report/smart_report.py --history [SN]  # per-disk history
./smart-report/smart_report.py --json          # analysed data, for scripts
./smart-report/smart_report.py --local         # SSH from here, local SQLite history
```

Exit code: `0` all OK, `1` a warning or an unreachable host, `2` something
critical. The CLI needs Python 3.9+ and nothing else.

**Dashboard login for the CLI.** The dashboard sits behind HTTP Basic Auth.
Put the login in `~/.netrc` and `chmod 600` it:

```
machine 192.168.50.58 login <user> password <password>
```

Alternatively, set `SMART_DASHBOARD_USER` / `SMART_DASHBOARD_PASSWORD`.

## Columns beyond wear

**FORM.** The physical format: 3.5", 2.5", M.2 or microSD.

- **SATA:** read from the drive's own IDENTIFY data, unless the model name
  clearly says M.2. The no-name "M.2 SSD 512GB" in g3-worker3 reports
  2.5".
- **NVMe:** there is no such field. Every consumer NVMe line in the lab is
  M.2, so M.2 is assumed.
- **SD cards in a Raspberry Pi:** shown as microSD.

The dashboard tooltip shows the source of the value.

**MADE.** The production date (year-month). It is only available for SD and
microSD cards, from their CID register. The maker is decoded from the same
register; manfid 0x00 is not a valid ID and usually means a no-name or
counterfeit card. SATA, NVMe and HDD SMART data has **no** production date,
so the column is empty for those drives. The date is printed on the drive
label. It is not estimated from power-on hours, because those are
unreliable (see below).

**Dashboard table.**
- Click a column name to sort: ascending, then descending, then the default
  order.
- **▾** opens an Excel-style menu with sort, search, and a checkbox list of
  values with counts. The list only offers values left after the other
  columns' filters.
- The sort and filters are remembered in the browser.

## How wear is judged

**SSD / NVMe.** Two measures are computed, and the **higher** one is shown
as WEAR:

1. **Bytes written as a % of the manufacturer's TBW** (from
   [endurance.conf](endurance.conf)). TBW LEFT = rated TBW minus bytes
   written. This measure is continuous and its basis is clear, which makes it
   the main planning number.
2. **The drive's own counter**: NVMe `percentage_used` or SATA "Percentage
   Used Endurance Indicator". It counts actual NAND wear, including write
   amplification. When it runs ahead of the TBW measure, the drive wears
   faster than its host writes suggest. The Micron 2200 in TrueNAS does
   this: 9% on its own counter against 6% of TBW.

LIFE LEFT is the smaller of:

- TBW left divided by the current write rate;
- the trend of the drive's own counter, once there are ≥ 60 days of
  history (the counter moves in whole percent, so shorter spans are noise).

TBW is a warranty figure, not the point of failure, so these projections are
conservative.

**HDD.** Spinning disks do not wear out by bytes written. The report tracks:

- **Load/unload cycles vs. rating.** WD Red is rated for 600,000 cycles. This
  is the WEAR column for HDDs, and LIFE LEFT is the remaining cycle budget at
  the current cycle rate.
- **Workload.** Read + written TB per year against the rated workload (WD
  Red: 180 TB/year). A WARN is raised above 100%.
- **Start/stop per day.** This shows the cost of the spin-down from
  [truenas/hdd-idle](../truenas/hdd-idle/hdd-idle.sh).
- **Media errors.** HDDs mostly fail suddenly rather than linearly, and the
  best predictors are *growing* reallocated, pending or uncorrectable sector
  counts. See the alerts below.

**Rates** (GB/day, cycles/day) are least-squares slopes over calendar time
in the last 30 and 90 days. The higher of the two is used, so a recent burst
of writes is not averaged away. Counter resets are handled.

Without history, the report falls back to a lifetime average per power-on
hour. That value is marked `*` (GB/DAY) and `~` (LIFE LEFT) and never raises
an alert, because power-on hours are unreliable:

- Some SATA firmware reports nonsense in Device Statistics: SanDisk X400
  shows 135 h there and 8710 h in attribute 9. The larger value is used.
- Some NVMe drives stop counting in low-power states: the Micron 2200 shows
  296 h after months in service.
- Drives under 720 h get no lifetime estimate at all.

## Alerts

| Condition | Level |
|---|---|
| SMART health failed, NVMe critical warning, spare blocks at threshold | CRIT |
| Reallocated / pending / uncorrectable sectors, NVMe media errors **grew** in the last 7 days | CRIT |
| …the same counters are non-zero but stable | WARN |
| UDMA CRC errors (cable) or device error log entries **grew** in the last 7 days | WARN |
| …the same counters are non-zero but stable | info only (old history) |
| Wear ≥ 70% / ≥ 90% | WARN / CRIT |
| Projected life < 3 years / < 1 year (history-based only) | WARN / CRIT |
| HDD workload above the rated TB/year | WARN |
| Temperature ≥ 70 °C SSD, ≥ 50 °C HDD | WARN |

All thresholds are constants at the top of [smart_report.py](smart_report.py).

## Storage and backup

- **Dashboard (main history).** SQLite at `/data/smart.db` on the
  `smart-data` local-path PVC, on pi4-master. The backend Deployment is
  pinned there with a matching nodeSelector, as CLAUDE.md requires.
  - **Not on SMB:** SQLite needs real file locking.
  - **Backup:** after every scan the database is copied with `VACUUM INTO` to
    MinIO bucket `smart-report`, as `smart.db` (latest) and
    `daily/smart-YYYY-MM-DD.db`.
  - **When g3-worker3 is off:** MinIO runs there, so the copy fails. The tab
    shows that, and the next scan retries.
  - **Bucket credentials:** a MinIO user `smart-report-backup` with a
    bucket-only policy. Its keys are in the Secret `smart-backup-minio`
    (`access-key`, `secret-key`), which is not committed.
- **CLI `--local`.** `~/.local/share/smart-report/smart.db`. On first use it
  imports the old `history.jsonl`, if present.
- Both stores hold serial numbers, so both are kept out of the repo. Disks
  are matched by serial number, so moving a disk keeps its history.

**Restoring the dashboard history from MinIO:**

```bash
kubectl scale deploy/dashboard-backend -n dashboard --replicas=0
# copy smart.db from the bucket into the PVC (e.g. a one-off pod on pi4-master
# mounting smart-data, then `mc cp`), then:
kubectl scale deploy/dashboard-backend -n dashboard --replicas=1
```

**Changing hosts.conf or endurance.conf.** Both are baked into the backend
image. Push to `main` (CI rebuilds on `smart-report/**`), then run
`kubectl rollout restart deployment/dashboard-backend -n dashboard`.

## Spun-down HDDs are not woken

smartctl runs with `-n standby` and disks are found with `--scan`, not
`--scan-open`. Neither sends a command to a sleeping disk. A tank-bulk HDD
that [truenas/hdd-idle](../truenas/hdd-idle/hdd-idle.sh) has spun down shows
as `SKIP`. To see its data, run the report while the pool is in use.

## Host setup

The SSH user must log in with a key and either be root or have
passwordless sudo for **`/usr/sbin/smartctl` only**.

**K3s nodes**: `ansible/roles/smartmontools` installs the package
(currently only g3-worker3; the Pis boot from SD cards, which have no
SMART). `buth11` already has passwordless sudo there.

```bash
cd ansible && ansible-playbook -i inventory.ini site.yml --tags smartmontools
```

**Proxmox** (smartmontools is preinstalled). pve-firewall allows SSH only
from pi4-master, which is where the dashboard backend runs. Your laptop's
traffic reaches Proxmox through pi4-master as well. As root:

```bash
apt install -y sudo
useradd -m -s /bin/bash smartreport
install -d -m 700 -o smartreport -g smartreport /home/smartreport/.ssh
# restrict = no shell PTY, no forwarding; from = LAN only (Tailscale traffic
# arrives SNATed from the subnet router's LAN address, so it still matches)
# Two keys: your laptop (CLI) and the dashboard backend (Disks tab).
# Dashboard public key: see "Getting the dashboard public key" below.
cat > /home/smartreport/.ssh/authorized_keys <<'KEYS'
restrict,from="192.168.50.0/24" <contents of ~/.ssh/id_ed25519.pub>
restrict,from="192.168.50.0/24" <dashboard public key>
KEYS
chown smartreport: /home/smartreport/.ssh/authorized_keys
chmod 600 /home/smartreport/.ssh/authorized_keys
echo 'smartreport ALL=(root) NOPASSWD: /usr/sbin/smartctl' > /etc/sudoers.d/smartreport
chmod 440 /etc/sudoers.d/smartreport && visudo -cf /etc/sudoers.d/smartreport
```

**TrueNAS SCALE** (WebUI only, per the TrueNAS config policy):

1. *Credentials → Users → Add*: username `smartreport`, *Disable Password*
   on, shell `bash`, paste **both** public keys (laptop + dashboard, one per
   line) into *Authorized Keys*, and under
   *Allowed sudo commands with no password* enter `/usr/sbin/smartctl`.
2. *System → Services → SSH*: enable it and tick *Start Automatically*.
   In its settings, make sure *Allow Password Authentication* is **off**.

Check with `ssh smartreport@<host> 'sudo -n /usr/sbin/smartctl --version'`.

### Getting the dashboard public key

It is derived from the private key in the Secret (the backend image has
paramiko, so nothing is copied out of the cluster):

```bash
kubectl exec -n dashboard deploy/dashboard-backend -- python3 -c \
  'import paramiko; k=paramiko.Ed25519Key.from_private_key_file("/root/.ssh/id_rsa"); print(k.get_name(), k.get_base64(), "homelab-dashboard")'
```

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
