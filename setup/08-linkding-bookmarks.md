# 08 -- Linkding: Self-Hosted Bookmark Sync Across Browsers

> Self-hosted bookmark manager with official browser extensions (Firefox,
> Chrome) that sync to one central instance — replaces per-browser bookmark
> sync with a single source of truth, reachable the same way Vaultwarden
> already is: split-DNS, no public exposure.

## Architecture

Deliberately mirrors [setup/07-vaultwarden-tls-hostido.md](07-vaultwarden-tls-hostido.md)
exactly, since it's the same problem (personal service, needs to work from
anywhere, must never be publicly resolvable):

- **Domain**: `links.analitykbiznesowy.pl`, plus `links.home.local` as a LAN
  fallback (same dual-host pattern as Vaultwarden's Ingress)
- **DNS**: split-DNS only. Pi-hole's `dns.hosts` config (`/etc/pihole/pihole.toml`,
  `[dns].hosts` array) now carries both new hostnames pointing at
  `192.168.50.50` (the existing Traefik LoadBalancer -- no new MetalLB IP
  needed, routing is by Host header/SNI same as Vaultwarden). Set live via:
  ```bash
  kubectl exec -n pihole deploy/pihole -- pihole-FTL --config dns.hosts \
    '[ "192.168.50.50 vault.home.local", "192.168.50.50 vault.analitykbiznesowy.pl", "192.168.50.50 links.home.local", "192.168.50.50 links.analitykbiznesowy.pl" ]'
  ```
  (Pi-hole v6 uses this TOML-backed config, not the old `custom.list` file --
  verify current value first with `pihole-FTL --config dns.hosts` before
  overwriting, since this replaces the whole array, not appends to it.)
- **Ingress**: [k8s/linkding/ingress.yaml](../k8s/linkding/ingress.yaml), Traefik
  `websecure` entrypoint, same shape as `k8s/vaultwarden/ingress.yaml`
- **Storage**: SQLite (Linkding's default) on `local-path`, pinned to
  `pi4-worker2` via `nodeSelector` -- same reasoning as every other
  SQLite-backed app in this repo (CIFS doesn't support the WAL locking mode)

## Current state vs. Vaultwarden's pattern

| | Vaultwarden | Linkding (today) |
|---|---|---|
| Cert | Real Let's Encrypt via Hostido AutoSSL | **mkcert only** -- trusted on devices with the local mkcert root CA, not publicly trusted |
| Tailscale split-DNS nameserver entry | Configured | **Not yet done** -- manual step in Tailscale Admin Console, same as documented in setup/07 |

Both of these are manual, account-console actions (DirectAdmin panel,
Tailscale Admin Console) that can't be scripted from the cluster -- follow
[setup/07's exact procedure](07-vaultwarden-tls-hostido.md) for both, just
substituting `links.*` for `vault.*`. Once done, replace the `linkding-tls`
Secret the same way:
```bash
kubectl create secret tls linkding-tls -n linkding \
  --cert=fullchain.pem --key=privkey.pem \
  --dry-run=client -o yaml | kubectl apply -f -
```

## Manifests

```
k8s/linkding/
├── namespace.yaml
├── pvc.yaml            # 1Gi, local-path
├── auth-secret.yaml    # placeholder only -- generation command in the file
├── deployment.yaml
├── service.yaml
└── ingress.yaml
```

Superuser credentials (`linkding-auth` Secret) follow the same
generate-on-the-live-cluster pattern as `dashboard-auth` -- see
[k8s/linkding/auth-secret.yaml](../k8s/linkding/auth-secret.yaml) for the
exact commands, never committed as a real value.

## Incident during first deploy: liveness probe crash-looped the pod

First deploy used a plain `livenessProbe` with `initialDelaySeconds: 15`.
On this hardware, first boot runs 50+ Django migrations plus initial
superuser creation (password hashing is deliberately slow, by design, and
this is a Raspberry Pi 4 ARM CPU) -- the app wasn't listening on `:9090`
until well past 15s. Kubelet started failing the liveness check at 15s,
hit `failureThreshold` a few checks later, and killed the container --
which restarted the whole slow startup sequence from scratch, so it never
got far enough to pass a health check. Exit code 137, confirmed via
`kubectl describe pod` (`Reason: Error`, `Killing ... failed liveness
probe`), not an OOM as the exit code alone might suggest.

**Fix:** added a `startupProbe` (`failureThreshold: 30`, `periodSeconds: 10`
-- 5 minute startup budget) ahead of the `livenessProbe`. `startupProbe`
disables liveness/readiness checking entirely until it succeeds once, which
is exactly the mechanism Kubernetes provides for slow-starting
applications -- a plain `livenessProbe` with a generous `initialDelaySeconds`
would have worked too, but wastes that whole delay on every normal restart,
not just the slow first boot. Added a matching `readinessProbe` at the same
time so the Service doesn't route traffic to the pod before Django is
actually serving, either.

## Using it

1. Log in at `https://links.home.local` (or the public hostname, once DNS
   split-DNS is wired) with the `linkding-auth` credentials.
2. Settings -> Integrations -> generate an API token.
3. Install the official Linkding browser extension (Firefox/Chrome), point
   it at the instance URL + API token, on every browser/device that should
   sync.
