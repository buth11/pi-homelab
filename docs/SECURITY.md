# Security Policy

> The rules this repo is supposed to follow, why, and what happens when
> they're checked. Point-in-time audit findings are tracked internally
> (not published in exploit-usable detail while anything they cover is
> still open) — this document is the standing policy those audits check
> against, and the lessons they produced once resolved.

## Credential handling

**Nothing that authenticates to anything real is ever committed as a
literal value.** This has held up well in practice — a full history scan
during the 2026-08-24 audit found zero committed API keys, SSH private
keys, `.env` files, `.tfstate` files, or TLS private keys across the
entire git history. The one exception (`WEBPASSWORD: "admin"` in
`k8s/pihole/deployment.yaml`) is a known, open finding — see the audit.

The pattern to follow, by resource type:

| Resource type | Pattern |
|---|---|
| Terraform | `sensitive = true` variable, real value supplied outside the repo at `apply` time (see `terraform/variables.tf`) |
| Helm | `REPLACE_ME` / templated placeholder in `values.yaml`, real value via `--set` at install/upgrade (see `helm/node-alerting/values.yaml`) |
| Raw K8s manifest | `SOMETHING_PLACEHOLDER` literal in `stringData`, filled in only on the live cluster via `kubectl create secret ... --dry-run=client -o yaml \| kubectl apply -f -` — **never** edited in place and committed |
| Local working files | staged outside the repo tree entirely (e.g. `~/secrets`), wiped with `shred -u` immediately after use — see `setup/07-vaultwarden-tls-hostido.md` |

Before committing anything touching a Secret, Terraform var, or Helm
value: search the diff for the literal you just typed, not just for the
word "password" — the ntfy incident (`git show 1bc8f85`) and the MinIO
Terraform-backend near-miss referenced in that same commit message both
happened because a real value was pasted into a file that looked like a
template.

## `.gitignore` baseline

Required patterns, kept current as the stack grows (dashboard's
Python/Node code is why the last three exist):
```
*.pem
*fullchain*
*privkey*
secrets/
.env
.kube/
node_modules/
dist/
__pycache__/
.venv/
id_rsa
id_ed25519
*.key
*kubeconfig*
```
Nested `.gitignore` files (e.g. `terraform/.gitignore` for
`*.tfstate`/`*.tfvars`) are fine and expected — don't try to collapse
everything into the root file.

## RBAC

**Principle: `Role`/`RoleBinding` scoped to one namespace by default.
`ClusterRole`/`ClusterRoleBinding` only when the workload genuinely needs
cross-namespace access, and even then, scope the verb list to exactly
what's used** — not "get/list/watch" copied onto resources the workload
never actually reads, and never `delete`/`patch` on `nodes` for anything
that isn't node lifecycle tooling.

Two ServiceAccounts currently violate this (see the audit for detail and
fix plan):
- `shutdown-sa` (`k8s/cronjobs/rbac.yaml`) — ClusterRole grants node
  delete/patch for a job that only scales two Deployments to zero.
- `dashboard-backend` (`k8s/dashboard/rbac.yaml`) — ClusterRole is
  arguably right-sized for the dashboard's *features*, but nothing gates
  who can invoke those features (see below).

## Authentication on internal services

**Every service reachable from the LAN or the Tailscale tailnet needs an
authentication boundary of some kind before it ships** — a login, an API
key, or an identity-aware proxy (`tailscale serve` with header checks).
"It's only reachable on the home network" is not a boundary once a
Tailscale subnet router advertises that network to the tailnet — see
[k8s/tailscale/connector.yaml](../k8s/tailscale/connector.yaml), which
advertises the full `192.168.50.0/24`.

This rule exists because of a concrete internal finding against one of
this cluster's own services — tracked and remediated outside this public
repo, not written up here in exploit-usable detail. Don't repeat the
pattern on the next internal tool: if it can act on the cluster or the
network, it needs an identity check in front of it before it ships, full
stop.

## Network segmentation

**There are currently no `NetworkPolicy` resources anywhere in this
cluster** — confirmed during the 2026-08-24 audit (`kind: NetworkPolicy`
returns zero matches repo-wide). Every pod can reach every other pod on
the pod network regardless of namespace. This is a known, accepted gap
for a single-operator homelab today, but it means the blast radius of
*any* pod compromise (not just the dashboard) is "the whole cluster's pod
network," not "one namespace." Worth revisiting once more than one
untrusted or internet-adjacent workload runs here — first candidate would
be namespace-scoped default-deny policies for `dashboard` and anything
Tailscale-exposed.

## Commit conventions relevant to security

This repo already follows Conventional Commits consistently
(`feat(scope): ...`, `fix(scope): ...`, `chore: ...`, `docs(scope): ...`)
— keep using them. For anything security-relevant specifically:
- A commit that removes or rotates a leaked credential should say so
  explicitly in the subject (`fix(monitoring): remove leaked ntfy topic
  from alertmanager-config.yaml` — `1bc8f85` is the model to follow),
  including confirmation that the old credential was rotated, not just
  removed from the file going forward. Git history is permanent; removal
  without rotation leaves the old value valid forever in `git log -p`.
- A commit that hardens an existing exposure (e.g. narrowing a
  `LoadBalancer` to `ClusterIP`, removing an unused package from an
  image) should reference what it's mitigating, even briefly — makes the
  audit trail legible later (`e907dd3`, `d43f2ff` are both good examples
  already in this repo's history).

## See also

Raw audit reports and the external pentest scope/output live under
`docs/security/` locally but are intentionally not tracked in this public
repo (see `.gitignore`) — findings get folded back into this policy once
resolved, rather than published as a standing list of what's still open.

- [KUBERNETES.md](KUBERNETES.md) — the mechanics behind the credential
  patterns referenced above
