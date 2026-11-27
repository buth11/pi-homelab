# Postmortem: qBittorrent and Firefox traffic left without VPN for months

**Date:** 2026-11-27
**Author:** Bartosz Suszko
**Status:** Resolved
**Severity:** SEV1. A core requirement failed silently: torrent traffic must
never leave through the home IP.

## Summary

From 2026-07-29 to 2026-10-29, and again from 2026-11-04 to 2026-11-27,
qBittorrent and the Firefox sidecar sent all their traffic from the home IP
(ISP) instead of ProtonVPN. The WireGuard tunnel on g3-worker3 stayed "up",
but no traffic was routed into it. unattended-upgrades restarted
systemd-networkd, and networkd deleted wg-quick's policy routing rules.
Nothing checked the actual egress IP, so it went unnoticed.

Fixed by running Gluetun as a native sidecar in the qBittorrent pod. Its
firewall drops all traffic outside the tunnel, so the pod has no route
except through the VPN. qBittorrent is also bound to `tun0`.

## Impact

- **qBittorrent.** Peers and trackers saw the home (ISP) IP, not the
  Proton IP 205.147.16.83, for about 3 months (Jul 29 to Oct 29) plus 23 days
  (Nov 4 to Nov 27).
- **Firefox.** The noVNC browser shares the pod's network namespace, so it
  also browsed from the home IP during those windows.
- **DNS.** Tracker lookups went through CoreDNS to the node's normal
  upstream, outside the VPN.

## Timeline

All times UTC.

| Time | Event |
|------|-------|
| 2026-05-30 | qBittorrent deployed behind a Gluetun sidecar (`711b217`). Gluetun's firewall acted as a kill switch. |
| 2026-05-31 | Gluetun removed from the pod in `b04ffc8`, a commit titled "docs: …". The VPN moved to host-level `wg-quick@proton` on g3, so the pod no longer had a kill switch. |
| 2026-07-12 09:22 | g3 boot. wg-quick adds `not fwmark 51820 lookup 51820` and `suppress_prefixlength 0`. Traffic goes through the VPN. |
| **2026-07-29 06:13** | unattended-upgrades restarts systemd-networkd, which removes the "foreign" wg-quick rules. **Leak starts.** Further networkd restarts follow on Jul 31, Aug 12, Aug 26, Sep 1, Sep 12, Oct 1 and Oct 15. |
| 2026-10-29 16:09 | g3 cold start after the power outage. wg-quick re-adds the rules and traffic goes through the VPN again. |
| **2026-11-04 06:32** | unattended-upgrades upgrades `libpcre2`, and networkd restarts 2 s later. **Leak starts again.** |
| 2026-11-13 06:14 | Another networkd restart (libc / kernel upgrades). |
| 2026-11-27 ~20:00 | User asks for confirmation that torrents go through the VPN. Check: pod egress IP = home IP. Prometheus shows 0 bytes sent on `proton` in the last 7 days, against 57 GB on `enp3s0`. |
| 2026-11-27 ~20:15 | qBittorrent scaled to 0 (stops the leak). |
| 2026-11-27 20:24 | qBittorrent bound to `tun0` in its config (backup `qBittorrent.conf.bak-20261127-vpn`). |
| 2026-11-27 20:27 | Host `wg-quick@proton` disabled, Gluetun pod deployed. Egress = 205.147.16.83 from both qBittorrent and Firefox. |
| 2026-11-27 20:35 | Kill-switch test passed. With the VPN endpoint blocked, both containers lose connectivity instead of falling back to eth0. |

## Root Cause

The design relied on host routing state that nothing enforced or monitored.

- `wg-quick` with `AllowedIPs = 0.0.0.0/0` puts the default route in table
  51820 and adds `ip rule` entries pointing traffic there. The pod's traffic,
  SNATed by the node, only followed those rules.
- `systemd-networkd` runs with the default `ManageForeignRoutingPolicyRules=yes`,
  so it deletes routing rules it did not create whenever it (re)starts.
- unattended-upgrades restarts networkd whenever a library it links
  (libpcre2, libc, systemd …) is upgraded. That happened roughly every one
  to two weeks.
- After that, the `proton` interface still existed, still handshook every
  2 minutes and looked healthy (`wg show`, `systemctl is-active`). It just
  carried no traffic, and there was no kill switch to stop the fallback to
  `enp3s0`.

## Trigger

Routine unattended upgrades on g3-worker3 (first 2026-07-29, latest
2026-11-04).

## Detection

A user question, not monitoring. Nothing compared the pod's public IP with
the VPN IP, and every check that existed ("is the service active", "is
there a handshake") measured the wrong thing.

## Resolution

1. Scaled qBittorrent to 0.
2. Refreshed the `protonvpn-wireguard` Secret from the working host config.
   The private key was piped, never printed. Checked that its public key
   matched the tunnel's.
3. Bound qBittorrent to `tun0` (`Session\Interface=tun0`) while the pod was
   stopped.
4. Disabled host `wg-quick@proton`. Two clients must not share the same
   Proton key. `/etc/wireguard/proton.conf` is kept.
5. Deployed Gluetun v3.41.3 (pinned by digest) as a native sidecar with a
   startupProbe, so the app containers start only after the tunnel is
   healthy. Other settings:
   - outbound traffic only through the tunnel;
   - inbound allowed only on 8080/3000/3001;
   - pod DNS pointed at Gluetun (`dnsPolicy: None`, 127.0.0.1).
6. Verified:
   - egress from qBittorrent and Firefox = Proton IP;
   - `curl --interface eth0` is blocked;
   - qBittorrent listens on 10.2.0.2 only;
   - WebUI and noVNC are reachable from the LAN;
   - with the VPN endpoint blocked, both containers lose connectivity, and
     the tunnel recovers on its own.

## Action Items

- [x] Kill switch in the pod (Gluetun firewall) plus qBittorrent bound to `tun0`.
- [x] Host `wg-quick@proton` disabled; docs updated (CLAUDE.md, setup/05).
- [ ] Alert when the qBittorrent pod's egress IP is not a Proton IP. The
      kill switch prevents leaks, but a broken tunnel would otherwise go
      unnoticed as "no downloads".
- [ ] Optional: Proton port forwarding (NAT-PMP) in Gluetun for incoming
      peers. This affects speed, not privacy.

## Lessons Learned

- "The VPN service is active" is not a check. The only check that matters
  is **which IP the workload actually egresses from**. Test that, from
  inside the workload.
- A privacy requirement needs a **fail-closed** design: if the VPN is gone,
  traffic stops. A host routing trick fails *open*, silently.
- Removing a safety component must not hide in a commit called "docs: …".
- On Ubuntu, wg-quick plus systemd-networkd is a known trap. Any policy
  routing that networkd does not own disappears on its next restart.
