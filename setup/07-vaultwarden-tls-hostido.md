# 07 -- Vaultwarden: Real TLS Certificate via Hostido + Tailscale Split-DNS

> Replaces the self-signed `mkcert` certificate on Vaultwarden's Traefik
> Ingress with a real, publicly-trusted Let's Encrypt certificate --
> without ever exposing the homelab to the public internet. Access works
> both on the local LAN and remotely via Tailscale.

## Architecture

- **Domain**: `vault.analitykbiznesowy.pl` (subdomain of an existing
  domain hosted on Hostido)
- **Certificate source**: Hostido's DirectAdmin panel, AutoSSL (Let's
  Encrypt), issued manually through the panel -- not via a live ACME
  client tied to the cluster
- **DNS**: split-DNS only, no public record ever points at the home IP
  - Pi-hole (`192.168.50.53`, MetalLB LoadBalancer) holds local A records
    for both `vault.analitykbiznesowy.pl` and `vault.home.local` ->
    `192.168.50.50` (Traefik LoadBalancer)
  - Tailscale Admin Console -> DNS -> two separate split-DNS nameserver
    entries, both pointing at `192.168.50.53`, one restricted to
    `analitykbiznesowy.pl`, one to `home.local`
- **Ingress**: [k8s/vaultwarden/ingress.yaml](../k8s/vaultwarden/ingress.yaml)
  serves both hostnames (`vault.home.local` and
  `vault.analitykbiznesowy.pl`) off the same backend and the same
  `vaultwarden-tls` Secret; `vault.home.local` stays as a fallback host,
  not removed

Getting a working certificate hit two blockers along the way -- see the
[2026-08-23 entry in troubleshooting.md](../docs/troubleshooting.md#2026-08-23----vaultwarden-real-tls-cert-via-hostido-autossl-two-blockers)
for the full root-cause writeups (Hostido blocking `.well-known/acme-challenge/`,
and native apps rejecting the cert until the chain was built through the
`ISRG Root YR` cross-sign rather than the bare `YR2` intermediate).

## Applying the certificate to the cluster

```bash
kubectl create secret tls vaultwarden-tls \
  --cert=fullchain.pem \
  --key=privkey.pem \
  -n vaultwarden \
  --dry-run=client -o yaml | kubectl apply -f -
```

`--dry-run=client -o yaml | kubectl apply -f -` instead of a plain
`create` avoids the `AlreadyExists` error on re-issuance/renewal, and
never writes the rendered Secret manifest (with the key inside) to disk.

Private key material should be staged only outside the git working tree
(e.g. `~/secrets`, not under `/workspaces/...`), and wiped with
`shred -u` immediately after the `kubectl` call succeeds -- nothing
certificate/key-related is ever committed. `.gitignore` also has broad
`*.pem` / `*fullchain*` / `*privkey*` / `secrets/` rules as a backstop.

## Verification

- `openssl x509` on the Secret's `tls.crt` confirms
  `subject=CN=vault.analitykbiznesowy.pl`, `issuer=... Let's Encrypt ... YR2`
- Browser (desktop + mobile) loads `https://vault.analitykbiznesowy.pl`
  with no warnings
- Native **Bitwarden Android app** connects and syncs against
  `vault.analitykbiznesowy.pl` with no certificate error -- the actual
  acceptance test, since it's the strictest client in the chain (OS-level
  trust store only, no bundled root updates)
- Tested over Tailscale on cellular data (Wi-Fi off) to confirm the
  split-DNS path works from outside the LAN, not just via direct LAN
  routing

## Known follow-ups (not yet done)

- Certificate expires **2026-11-22** (90-day LE validity). Renewal is
  **manual** through the Hostido panel -- there is no live ACME client
  tied to the cluster, so this will not auto-renew. Needs a calendar
  reminder, or a proper automation (e.g. cert-manager with DNS-01 against
  Hostido, if their DNS API allows it).
- Pi-hole's `WEBPASSWORD` is hardcoded in
  [k8s/pihole/deployment.yaml](../k8s/pihole/deployment.yaml) instead of
  a Secret -- resets to `admin` on every pod restart.
- Pi-hole's PVCs use `local-path` (node-local `hostPath` on
  `pi4-worker2`), with no replication or snapshotting, unlike
  `smb-tank-fast`/`smb-tank-bulk` which are TrueNAS-backed and
  snapshotted.
