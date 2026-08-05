# Postmortem: qBittorrent config edits silently reverted; three orphaned config PVCs found along the way

**Date:** 2026-07-25
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV3 (WebUI access issue and stalled torrent transfer, no data loss)

## Summary
Two related qBittorrent issues on the same day: (1) peers were visible
in the swarm but transfer stayed at 0 B/s despite a healthy VPN tunnel,
traced to a stale forwarded port after a NAT-PMP rotation; and (2) a
WebUI config setting kept reverting after every restart, traced to
editing a live process's config file while it was still running --
compounded by discovering three different, similarly-named PVCs all
plausibly holding qBittorrent's config, only one of which was actually
mounted.

## Impact
Torrent transfer stalled for roughly 15-30 minutes until the port was
manually corrected. WebUI configuration change required three separate
attempts (each reverting) before the actual mounted volume was
correctly identified, costing significant debugging time.

## Timeline
| Time | Event |
|---|---|
| T+0 | Peers visible in swarm (dozens), transfer stuck at 0 B/s despite `wg show` reporting a recent, healthy WireGuard handshake |
| T+10m | Confirmed a healthy tunnel only proves outbound connectivity -- checked ProtonVPN's NAT-PMP-forwarded port and found it had rotated after a tunnel restart; qBittorrent's `Listening Port` didn't auto-follow it (UPnP/NAT-PMP was off) |
| T+15m | Manually updated `Listening Port` to the currently-forwarded value |
| T+30-45m | Transfer resumed as tracker/DHT/PEX propagated the new port through the swarm |
| (separately) T+0 | Edited `WebUI\ServerDomains` in `qBittorrent.conf` via `sed`; verified correct immediately after |
| T+1m | `kubectl rollout restart` performed; setting reverted |
| T+5m, T+10m | Repeated the edit two more times, same revert each time |
| T+15m | Recognized the pattern: the running process rewrites its own config from memory on shutdown, racing any on-disk edit made while it's still alive |
| T+20m | While investigating which PVC was actually mounted, discovered three separate, plausibly-named PVCs: `qbittorrent-config` (local-path/pi4-worker2), `qbittorrent-config-nas` (SMB), `qbittorrent-config-local` (local-path/g3-worker3) -- had been editing the wrong one, since `kubectl get deployment -o yaml`'s `last-applied-configuration` annotation reflected an older manifest, not the live one |
| T+25m | Identified the actually-mounted volume via `df -h` inside the pod instead of trusting the annotation |
| T+30m | Scaled to 0, waited for full pod removal, edited on disk, scaled back to 1 -- edit persisted correctly |

## Root Cause
**Port drift:** ProtonVPN's NAT-PMP-forwarded port rotates on tunnel
restart; qBittorrent has no built-in mechanism to detect and follow
that change without UPnP/NAT-PMP enabled in its own settings.

**Config race:** the qBittorrent process rewrites its config file from
its in-memory state on shutdown. Editing the file on disk while the
process is still running is a race that a graceful `SIGTERM` reliably
loses, since the process's stale in-memory state wins on write.

**PVC confusion:** three PVCs had accumulated across earlier migrations,
all plausibly named for the same purpose, with no clear signal in
tooling about which one the live Deployment actually referenced --
`last-applied-configuration` is not a reliable source of truth for
this.

## Trigger
Port drift: a routine ProtonVPN tunnel restart (unrelated maintenance)
rotated the forwarded port. Config race: attempting to change a WebUI
setting without accounting for the running process owning that file.

## Detection
Manual in both cases -- observed via the qBittorrent WebUI (0 B/s
despite peers) and via re-checking the setting after each restart.

## Resolution
- **Port drift:** manually updated `Listening Port` to match the
  currently-forwarded value. Longer-term fix designed but not yet
  deployed: a `natpmpc`-based systemd timer that re-checks the forwarded
  port and pushes changes to qBittorrent's WebUI API automatically
- **Config race:** established the rule to never edit a config file
  belonging to a live process expected to rewrite it on exit -- scale to
  0, wait for full pod removal (not just `Terminating`), edit on disk,
  scale back to 1
- **PVC confusion:** to find the volume actually mounted, cross-check
  live spec + `df -h` inside the pod rather than trusting annotations:
```bash
  kubectl get deployment <name> -n <ns> -o jsonpath='{.spec.template.spec.volumes}' | jq
  kubectl exec -n <ns> deployment/<name> -- df -h /config
```

## Action Items
- [ ] Deploy the designed `natpmpc` systemd timer so port drift
      self-corrects instead of requiring manual intervention on every
      ProtonVPN tunnel restart
- [ ] Audit and remove the two orphaned qBittorrent config PVCs still
      sitting in the cluster unreferenced by anything live
- [x] Documented the "never edit a live process's config file directly"
      rule for reuse on any future SQLite/config-owning workload

## Lessons Learned
- A healthy tunnel/connection only proves one property (in this case,
  outbound connectivity) -- it doesn't validate the whole path a
  service depends on. Peers-visible-but-zero-transfer specifically
  pointed at an inbound-connectivity problem, not a tunnel problem
- Never trust a Kubernetes object's `last-applied-configuration`
  annotation as the current live state -- it can silently lag behind
  reality after out-of-band changes. Cross-check with what's actually
  mounted inside the running pod
- Accumulated, similarly-named resources from past migrations are a
  real operational hazard, not just clutter -- they cost real debugging
  time when multiple plausible candidates exist for "the" config volume
