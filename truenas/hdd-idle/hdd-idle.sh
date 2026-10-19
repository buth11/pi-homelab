#!/bin/bash
# Spin down HDDs after a period without real I/O.
#
# Why this exists: TrueNAS polls disk temperatures over SMART every 5 min
# (netdata python.d.plugin and middlewared). WD Red drives treat those SMART
# reads as activity and reset their internal standby timer, so the
# "HDD Standby" setting never fires. SMART commands do not show up in
# /proc/diskstats, so idle is measured there instead: if a disk's completed
# reads + writes have not changed for IDLE_MINUTES, put it into standby.
# The pollers skip disks that are already in standby, so they stay asleep
# until real I/O wakes them. Full story: setup/11-hdd-spindown.md.
#
# Usage (run as root, e.g. from a TrueNAS Cron Job every 5 minutes):
#   hdd-idle.sh /dev/disk/by-id/ata-MODEL_SERIAL [/dev/disk/by-id/...]
#
# Environment:
#   IDLE_MINUTES  minutes without I/O before spin-down (default 20)
#   STATE_DIR     where per-disk state is kept (default: ./state next to the script)
#   DRY_RUN=1     log decisions but never spin anything down
#
# Disks are given by /dev/disk/by-id path because sdX names can change
# after a reboot.

set -u

IDLE_MINUTES=${IDLE_MINUTES:-20}
STATE_DIR=${STATE_DIR:-"$(dirname "$(readlink -f "$0")")/state"}
DRY_RUN=${DRY_RUN:-0}
HDPARM=/usr/sbin/hdparm   # full path: cron's PATH may not include /usr/sbin

log() { echo "$(date -Iseconds) $*"; }

if [ "$#" -eq 0 ]; then
    echo "usage: $0 /dev/disk/by-id/DISK [...]" >&2
    exit 2
fi

mkdir -p "$STATE_DIR" || exit 1
now=$(date +%s)
idle_limit=$((IDLE_MINUTES * 60))
rc=0

for disk in "$@"; do
    dev=$(basename "$(readlink -f "$disk")")
    if [ ! -b "/dev/$dev" ]; then
        log "ERROR $disk: not a block device"
        rc=1
        continue
    fi

    # Completed reads ($4) + completed writes ($8) since boot.
    io=$(awk -v d="$dev" '$3 == d { print $4 + $8 }' /proc/diskstats)
    state="$STATE_DIR/$(basename "$disk")"

    if [ ! -f "$state" ]; then
        echo "$io $now" > "$state"
        log "$dev: first run, baseline io=$io"
        continue
    fi

    read -r last_io since < "$state"
    if [ "$io" != "$last_io" ]; then
        echo "$io $now" > "$state"   # activity: restart the idle clock
        continue
    fi

    idle=$((now - since))
    if [ "$idle" -lt "$idle_limit" ]; then
        continue
    fi

    # Idle long enough. CHECK POWER MODE does not wake a sleeping disk.
    if "$HDPARM" -C "$disk" | grep -q standby; then
        continue
    fi

    if [ "$DRY_RUN" = "1" ]; then
        log "$dev: idle $((idle / 60)) min, would spin down (DRY_RUN)"
    elif "$HDPARM" -y "$disk" > /dev/null; then
        log "$dev: idle $((idle / 60)) min, spun down"
    else
        log "ERROR $dev: hdparm -y failed"
        rc=1
    fi
done

exit "$rc"
