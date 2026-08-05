# Postmortem: Polish-diacritic filenames corrupted over CIFS (missing nls_utf8 kernel module)

**Date:** 2026-07-25 to 2026-07-26
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV3 (no data loss, no service outage; specific files unreadable by one client)

## Summary
Syncthing repeatedly failed to sync ~850 files across two folders because
their names contained Polish diacritics (ą, ę, ż, ń, ś, ó, ł). Root cause
was a missing `nls_utf8` kernel module on one Kubernetes node, causing its
CIFS mount to silently mis-decode multi-byte UTF-8 filenames from the
NAS. Fixed by installing the module and adding `iocharset=utf8` to the
mount options.

## Impact
- Two Syncthing folders (`Projects`, `Syncthing`) stuck at "sync error"
  state, 66 and 782 files respectively unable to sync
- No data loss: source files on the NAS (verified via direct ZFS
  inspection) were correct throughout; only this node's CIFS view of
  them was corrupted
- ~6 hours of engineering time across two sessions to fully resolve

## Timeline
| Time (UTC) | Event |
|---|---|
| 2026-07-25 ~11:00 | Syncthing UI shows scan errors on `Projects` folder: `hashing: open ...: no such file or directory` for filenames containing diacritics |
| 2026-07-25 ~11:30 | Confirmed via `od -An -tx1` hex dump that the pod sees literal `?` (0x3F) where a diacritic should be |
| 2026-07-25 ~12:00 | Cross-checked the same file via ZFS Shell on TrueNAS directly (bypassing Samba entirely): bytes are correct UTF-8 (`c4 85` = ą). Confirms corruption happens only in the CIFS layer for this specific client |
| 2026-07-25 ~13:00 | Deleted and recreated the PV with `vers=3.0` (up from `2.0`) — no change, same corruption |
| 2026-07-25 ~14:00 | Attempted explicit `iocharset=utf8` mount option — mount failed outright: `mount error(79): Can not access a needed shared library` |
| 2026-07-25 ~14:30 | Root cause found: `find /lib/modules/$(uname -r) -iname nls_utf8*` returns nothing on `k3s-burst-worker` — the module genuinely isn't present in the base kernel image |
| 2026-07-25 ~15:00 | Reverted to `vers=3.0` without explicit `iocharset` (silent-fallback behavior) to restore working state for the day; error count unchanged (still present, but no worse) |
| 2026-07-26 ~09:00 | Installed `linux-modules-extra-<kernel>`, ran `modprobe nls_utf8`, confirmed loaded |
| 2026-07-26 ~09:15 | Recreated the PV with `iocharset=utf8` explicit — mount succeeded immediately (pod Running in ~2s vs. the earlier repeated failures) |
| 2026-07-26 ~09:20 | Verified via hex dump: filenames now show correct UTF-8. Both folders' error counts dropped to 0 within one scan cycle |

## Root Cause
The `k3s-burst-worker` node (a Proxmox VM running Ubuntu 24.04) was
missing the `nls_utf8` kernel module. This module is not included in the
base `linux-modules` package on this Ubuntu cloud image — it ships
separately in `linux-modules-extra`. Without it, and without an explicit
`iocharset` mount option, the CIFS kernel driver silently fell back to a
different character set for decoding filenames, corrupting any
multi-byte UTF-8 character (i.e. any Polish diacritic) into a literal
`?`.

## Trigger
This wasn't triggered by a specific change — it was a latent gap since
the node was provisioned. It surfaced when Syncthing performed a
full folder rescan (itself triggered by an unrelated config-loss
incident the same day), which touched every file including ones with
diacritics for the first time in a while.

## Detection
Manual: noticed via the Syncthing web UI showing a persistent error
count on two folders. Not caught by any automated alert — there was no
monitoring for this class of failure at the time.

## Resolution
1. Diagnosed by comparing raw bytes at three independent layers: the
   NAS filesystem directly (ZFS shell), an unrelated client's CIFS mount
   (`pi4-master`, unaffected), and the affected client's CIFS mount
   (`k3s-burst-worker`) — isolating the fault to one specific mount
2. Ruled out SMB protocol version as the cause (tested `2.0` vs `3.0`,
   no change)
3. Installed the missing kernel module: `apt install
   linux-modules-extra-$(uname -r)`
4. Recreated the PersistentVolume with `iocharset=utf8` explicitly set
   in `mountOptions`
5. Updated the StorageClass's default `mountOptions` to include
   `iocharset=utf8` so any future PV on this class inherits the fix

## Action Items
- [ ] Audit the other 3 cluster nodes for the same `nls_utf8` gap before
      scheduling any future SMB-mounting workload on them
- [ ] Add a Prometheus alert on Syncthing folder error count > 0 sustained
      for >30m, so this class of issue pages instead of requiring someone
      to notice it in a web UI
- [ ] Document the `iocharset=utf8` requirement in the NAS storage class
      README so it isn't silently dropped in a future rewrite

## Lessons Learned
- When a symptom only affects one specific client, isolate by testing
  the same data path from at least one other client and, if possible,
  directly at the source (bypassing every intermediate layer) before
  chasing configuration changes on the affected client
- A failing `mount` command with an unfamiliar error (`error(79)`) is
  more informative than a mount that "succeeds" but silently
  misbehaves — the failed `iocharset=utf8` attempt, while frustrating in
  the moment, was the fastest route to the real root cause
- Kernel module availability is not implied by kernel version alone;
  base cloud images frequently split "extra" modules into a separate
  package that isn't installed by default
