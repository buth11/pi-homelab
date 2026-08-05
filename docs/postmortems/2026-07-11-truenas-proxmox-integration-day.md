# Postmortem: TrueNAS/Proxmox integration day -- six minor incidents

**Date:** 2026-07-11
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV3 (each individually minor; grouped here because they
share a common thread -- first-day integration friction, not one root cause)

## Summary
Bringing a new Proxmox + TrueNAS storage tier online in one day surfaced
six unrelated but instructive issues, each fixed within the same session.
Grouped into one document rather than six, since none alone justifies a
full incident writeup, but the set is a useful reference for "day one of
a new integration" classes of problems.

## Impact
No production outage (new infrastructure, not yet serving traffic to
end users at the time). Delayed the migration timeline by roughly a day.

## Incidents and Fixes

**1. Proxmox host had no working DNS**
`/etc/resolv.conf` pointed at an unrelated `192.168.1.1`, blocking image
downloads. Fixed by pointing at a working resolver. Trivial, but a good
reminder to verify basic connectivity before debugging anything
downstream of it.

**2. ASUS router SMB shares are one-share-per-folder, not one share with
subfolders**
Assumed `mount -t cifs //router/New_Volume` would expose subfolders;
it didn't -- each top-level folder is its own separate share. Discovered
via `smbclient -L`. Lesson: verify actual share topology with a listing
tool before assuming a mount path structure.

**3. CIFS mount `error(13)` Permission denied**
Traced to the SMB user lacking per-folder share permissions on the
router side, not a client-side mount-options problem. A reminder that
permission errors on network filesystems are often server-side ACL
issues, not local misconfiguration -- check the server before the client.

**4. Jellyfin/FileBrowser stuck in `ContainerCreating`**
An unrelated physical disk swap severed the old `nas-smb` mount.
Resolved by provisioning new SMB-backed PVCs and patching the
Deployments' volume claims rather than attempting to resurrect the dead
mount -- faster and safer than chasing a mount that no longer has
anything behind it.

**5. Zigbee2MQTT onboarding form silently reset adapter/port selection**
The main onboarding tab reset the adapter/port fields on page reload,
causing the Zigbee radio's auto-discovery to fail. Worked around by
using the onboarding wizard's dedicated "Serial" tab to set values
explicitly instead of the auto-populated main tab.

**6. `ember` Zigbee adapter driver failed to initialize the radio**
Despite the dongle's multiprotocol marketing, the `ember` driver hit a
repeated `ASH` handshake reset loop (`HOST_FATAL_ERROR`). Switching the
driver to `zstack` worked on the first attempt -- not every
manufacturer-recommended driver is the right one for a given firmware
revision.

## Root Cause
No single root cause -- six independent first-contact issues typical of
onboarding new infrastructure: stale host config, undocumented vendor
share topology, server-side ACLs, a severed mount from unrelated
hardware maintenance, a UI bug in an onboarding wizard, and a
driver/firmware mismatch.

## Detection
All manual -- each surfaced immediately when attempting the relevant
step (DNS failure on first `wget`, mount failure on first `mount`
attempt, etc.). No monitoring existed yet at this stage of the build.

## Resolution
Each fixed independently as listed above; total session time roughly
one day for all six.

## Action Items
- [x] Document the ASUS one-share-per-folder topology in architecture
      notes so it isn't rediscovered later
- [ ] None of these individually warranted a recurring monitoring check;
      revisit if any recurs

## Lessons Learned
- On unfamiliar vendor hardware (routers, Zigbee dongles), verify actual
  behavior with a listing/diagnostic tool (`smbclient -L`, driver logs)
  rather than assuming documentation or marketing claims hold
- When a pod is stuck in `ContainerCreating` after unrelated hardware
  maintenance, check whether the maintenance itself broke a dependency
  (a mount, a network path) before assuming a Kubernetes-side problem
