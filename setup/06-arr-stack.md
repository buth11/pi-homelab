# 06 -- Media Automation Stack (Prowlarr / Sonarr / Radarr)

> Adds automated indexer management and library-driven downloading on top
> of the existing qBittorrent + Jellyfin pair: Prowlarr aggregates
> indexers, Sonarr/Radarr use them to find and grab episodes/movies via
> qBittorrent, then import completed files straight into the Jellyfin
> library with correct naming.

## Why a separate namespace

`arr` (Prowlarr, Sonarr, Radarr) is its own namespace rather than living in
`qbittorrent` or `jellyfin`, since it's a distinct concern with its own
lifecycle -- consistent with how `dashboard`, `pihole`, etc. are already
split out.

## Storage decisions

Two lessons from `docs/troubleshooting.md` (2026-07-25 entry) applied
directly here:

- **Config volumes are `local-path`, never SMB.** Prowlarr/Sonarr/Radarr
  all use SQLite for their internal databases -- the exact failure mode
  that broke Syncthing's config on SMB (`SQLITE_BUSY`, permanent crash
  loop) would hit these the same way. `prowlarr-config`, `sonarr-config`,
  `radarr-config` are all `local-path`, pinned to `g3-worker3` on first
  schedule.
- **Hardlinks disabled in both Sonarr and Radarr.** CIFS/SMB doesn't
  support hardlinks. Settings -> Media Management -> Advanced Settings
  (`Show Advanced`) -> `Use Hardlinks instead of Copy` is turned **off**
  in both apps, so completed downloads are copied into `/media` instead --
  costs temporary double disk usage during the copy, but works reliably;
  a hardlink attempt on this storage backend fails or silently no-ops.

`arr-downloads` and `arr-media` are new PVCs against the existing
`smb-tank-bulk-downloads` / `smb-tank-bulk-media` StorageClasses (same
underlying TrueNAS shares qBittorrent and Jellyfin already use -- see
architecture.md §4.2) -- not new shares, just new PVC objects, since PVCs
are namespace-scoped and `arr` needs its own reference to the same
storage qBittorrent/Jellyfin already point at.

## Manifests

k8s/arr/
├── namespace.yaml
├── pvc.yaml # 3x local-path config PVCs + arr-downloads + arr-media
├── prowlarr.yaml
├── sonarr.yaml
└── radarr.yaml


All three Deployments pin `nodeSelector: kubernetes.io/hostname: g3-worker3`
(same node as qBittorrent) and expose via `LoadBalancer` service through
the existing MetalLB pool.

| Service  | IP             | Port |
| -------- | -------------- | ---- |
| Prowlarr | 192.168.50.60  | 9696 |
| Sonarr   | 192.168.50.61  | 8989 |
| Radarr   | 192.168.50.62  | 7878 |

## MetalLB pool exhaustion

Deploying all three at once hit the pool limit immediately: `homelab-pool`
was `192.168.50.50-192.168.50.60` (11 addresses), already fully consumed
by pre-existing `LoadBalancer` services (traefik, jellyfin, filebrowser,
pihole x2, qbittorrent, firefox-sidecar, grafana, dashboard, syncthing) --
Prowlarr took the last free address (`.60`) and Sonarr/Radarr sat in
`<pending>` indefinitely. `kubectl describe svc` wasn't even necessary to
diagnose it -- `kubectl get ipaddresspool -n metallb-system -o yaml`
cross-referenced against `kubectl get svc -A | grep LoadBalancer` made the
exhaustion obvious immediately.

**Fix:** widened the pool in `k8s/metallb/ippool.yaml`:
```yaml
spec:
  addresses:
  - 192.168.50.50-192.168.50.80
```
Confirmed the new range doesn't collide with router DHCP reservations
before applying. `kubectl apply` picked up the change live -- no need to
delete/recreate the pending Services, MetalLB assigned addresses to them
within seconds of the pool update.

**Lesson for next time:** with `LoadBalancer`-per-service as the pattern
for every new deployment in this cluster, check remaining pool capacity
*before* adding a multi-service stack, not after hitting `<pending>`.

## App-to-app wiring

Prowlarr -> Sonarr/Radarr integration (Settings -> Apps in Prowlarr) needs
the **Prowlarr Server** field set to Prowlarr's own external address
(`http://192.168.50.60:9696`), not `localhost` -- the default value is
correct only from a browser's perspective (or if everything ran in one
pod); Sonarr/Radarr run in separate pods and need the real address to
reach Prowlarr over the pod network.

Once wired, indexers added once in Prowlarr sync automatically to both
apps -- no need to configure indexers twice.

## Importing a pre-existing manual download

Sonarr will only auto-recognize existing files if the on-disk folder name
matches what it expects for the series' root path. A folder that predates
Sonarr management (`Rick_and_Morty`, underscores, manually downloaded) had
to be renamed to match Sonarr's naming convention (`Rick and Morty`,
spaces) before `Series -> Refresh & Scan` picked up the 8 already-present
S09 episodes and correctly matched them by filename/quality -- no manual
import step needed once the folder name lined up.

Also worth noting: **adding a series with the full target path typed
directly into the Root Folder field fails** ("Root folder path ... contains
series folder ...") -- Root Folder must be the *parent* directory only
(e.g. `/media/Seriale`), Sonarr appends the series name itself.

## End-to-end verification

Tested both directions same day:
- **Radarr:** added a movie, confirmed grab in qBittorrent, confirmed
  `Movie File Imported` in Radarr's Activity -> History, confirmed the
  file landed in `/media/Movies/<title>/` with clean naming via `kubectl
  exec ... -- find /media`, confirmed Jellyfin's pod could see the same
  path (same underlying SMB mount).
- **Sonarr:** existing library already had S09E01-E08 of an ongoing show;
  added the series pointing at the existing folder, confirmed Sonarr
  matched all 8 as "have", searched only for the one genuinely missing
  episode (E09) rather than re-grabbing anything already present.
